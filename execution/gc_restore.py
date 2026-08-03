"""Restore run-keyed rows from the db_gc archive sidecar into portfolio.db.

The current db_gc sidecar format is append-only and run-keyed
(UNIQUE(gc_run_id, key) per table), so restoring is classify-then-apply, never
a bare INSERT ... SELECT. A legacy sidecar without gc_run_id is preservation-
only forensic evidence: this command will neither upgrade it in place nor
bulk-reinsert it into the live database. ``inspect_preservation_archive``
provides the read-only integrity/hash/inventory check for that artifact.

* **restorable** — archived key absent from main: restored verbatim (columns
  copied BY NAME; main columns added since archiving default to NULL).
* **identical** — key present in main with an IS-equal payload: skipped.
* **conflict** — key present in main with a DIFFERENT payload (a recycled id,
  or the row was re-ingested since the prune): NEVER touched, loudly counted
  and sampled. Resolving a conflict is a human decision.

Variant selection: by default each key restores its LATEST archived variant
(max gc_run_id); ``--run <gc_run_id>`` restores exactly one run's rows
instead. Dry-run by default; ``--apply`` writes under the portfolio write-set
run lock, the schema-compat write preflight, and (unless overridden) the
03:00-05:00 America/Los_Angeles protected-window refusal — the same guards
db_gc applies. Each applied table is one IMMEDIATE transaction with
``defer_foreign_keys=ON`` and rows inserted in key order, so supersedes-style
self-FK chains restore atomically and a violation aborts the whole table
loudly at commit. Applied restores are logged to ``gc_manifest`` with
policy='restore' (the recovery auditor's facts-depth totals filter on
policy='facts-depth', so restore rows never inflate them).

``--drill`` proves restorability WITHOUT touching main: it runs on a
throwaway temp database with the live DB and the archive attached READ-ONLY,
rebuilds each table from main's CREATE TABLE DDL, inserts every candidate,
and verifies row-for-row with a two-way EXCEPT. restore_drill.py's monthly
cron calls this so archive rot surfaces on a schedule, not during an outage.

Exit codes: 0 clean; 2 setup/abort error (or a failed drill); 4 conflicts
present (restorable rows were still restored under --apply — the conflicts
need a human).

Structured JSON events go to stderr; stdout is one JSON report.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import db_gc

from clock import now_naive_utc
from run_lock import RunLock, RunLockHeldError, acquire_run_lock
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite

DEFAULT_DB = Path(__file__).resolve().parents[1] / "data" / "portfolio.db"
DEFAULT_LOCK_TIMEOUT_S = 60.0
CONFLICT_SAMPLE_LIMIT = 20


def _log(event: str, **fields: object) -> None:
    print(json.dumps({"event": event, **fields}, default=str), file=sys.stderr)


class TableRestoreReport(BaseModel):
    table: str
    key_column: str
    run_filter: str | None = None
    candidates: int = 0
    restorable: int = 0
    identical: int = 0
    conflicts: int = 0
    conflict_key_samples: list[str] = Field(default_factory=list)
    columns_defaulted: list[str] = Field(default_factory=list)
    restored: int = 0
    drill_verified: bool | None = None


class GcRestoreReport(BaseModel):
    run_at: str
    db_path: str
    archive_path: str
    mode: Literal["dry-run", "apply", "drill"]
    tables: list[TableRestoreReport] = Field(default_factory=list[TableRestoreReport])


class PreservationArchiveReport(BaseModel):
    path: str
    size_bytes: int = Field(ge=0)
    sha256: str
    integrity_check: str
    quick_check: str
    foreign_key_violation_count: int = Field(ge=0)
    table_row_counts: dict[str, int] = Field(default_factory=dict[str, int])
    legacy_tables: list[str] = Field(default_factory=list[str])


def _preservation_only_error(tables: list[str]) -> db_gc.GcAbortedError:
    joined = ", ".join(tables)
    return db_gc.GcAbortedError(
        f"legacy archive table(s) {joined} predate run-keying and form a "
        "preservation-only forensic archive by owner decision; gc_restore will "
        "not mutate or bulk-reinsert them. Preserve the sidecar and use a sealed "
        "read-only archive generation for historical access"
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def inspect_preservation_archive(archive_path: Path) -> PreservationArchiveReport:
    """Inspect an archive without modifying it or attaching it to a writer."""
    resolved = archive_path.resolve(strict=True)
    sha256_before = _sha256(resolved)
    size_before = resolved.stat().st_size
    conn = connect_sqlite(resolved, role=SQLiteConnectionRole.READ_ONLY)
    try:
        tables = [
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' "
                "AND name != 'gc_manifest' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        table_row_counts = {
            table: int(
                conn.execute(
                    f'SELECT COUNT(*) FROM "{table}"'  # nosec B608 -- sqlite_master identifier
                ).fetchone()[0]
            )
            for table in tables
        }
        legacy_tables = [
            table for table in tables if db_gc.GC_RUN_ID_COL not in _columns(conn, "main", table)
        ]
        integrity_check = str(conn.execute("PRAGMA integrity_check").fetchone()[0])
        quick_check = str(conn.execute("PRAGMA quick_check").fetchone()[0])
        foreign_key_violation_count = len(conn.execute("PRAGMA foreign_key_check").fetchall())
    finally:
        conn.close()
    sha256_after = _sha256(resolved)
    size_after = resolved.stat().st_size
    if sha256_before != sha256_after or size_before != size_after:
        raise db_gc.GcAbortedError(
            "archive changed during preservation inspection; retry in a quiescent window"
        )
    return PreservationArchiveReport(
        path=str(resolved),
        size_bytes=size_after,
        sha256=sha256_after,
        integrity_check=integrity_check,
        quick_check=quick_check,
        foreign_key_violation_count=foreign_key_violation_count,
        table_row_counts=table_row_counts,
        legacy_tables=legacy_tables,
    )


def _archive_tables(conn: sqlite3.Connection) -> list[str]:
    return [
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM gcarc.sqlite_master WHERE type = 'table' "
            "AND name != 'gc_manifest' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
    ]


def _columns(conn: sqlite3.Connection, schema: str, table: str) -> list[str]:
    return [str(row[1]) for row in conn.execute(f'PRAGMA {schema}.table_info("{table}")')]  # nosec B608 -- schema is a validated internal alias


def _quoted(names: list[str]) -> str:
    return ", ".join(f'"{name}"' for name in names)


class _TablePlan:
    """Resolved identifiers + SQL fragments for one table's restore.

    ``live_schema`` is the schema alias holding the live table — ``main`` for
    dry-run/apply, the read-only ``livedb`` attach for drill mode.
    """

    def __init__(
        self,
        conn: sqlite3.Connection,
        table: str,
        run_filter: str | None,
        live_schema: str = "main",
    ) -> None:
        self.table = table
        self.live_schema = live_schema
        arc_cols = _columns(conn, "gcarc", table)
        if db_gc.GC_RUN_ID_COL not in arc_cols:
            raise _preservation_only_error([table])
        self.mirror = [c for c in arc_cols if c not in db_gc.ARCHIVE_META_COLUMNS]
        live_cols = _columns(conn, live_schema, table)
        dropped = sorted(set(self.mirror) - set(live_cols))
        if dropped:
            raise db_gc.GcAbortedError(
                f"{table}: live table no longer has archived column(s) {dropped}; "
                "cannot restore them faithfully — reconcile the schema first"
            )
        self.columns_defaulted = sorted(set(live_cols) - set(self.mirror))
        self.rowid_keyed = db_gc.GC_SOURCE_ROWID_COL in arc_cols
        self.key_col = db_gc.GC_SOURCE_ROWID_COL if self.rowid_keyed else "id"
        self.live_key = "rowid" if self.rowid_keyed else "id"
        run_col = db_gc.GC_RUN_ID_COL
        if run_filter is not None:
            self.variant_where = f'a."{run_col}" = :run'
            self.params: dict[str, str] = {"run": run_filter}
        else:
            self.variant_where = (
                f'a."{run_col}" = (SELECT MAX(b."{run_col}") '  # nosec B608 -- internal registry identifiers; no user input
                f'FROM gcarc."{table}" b WHERE b."{self.key_col}" = a."{self.key_col}")'
            )
            self.params = {}
        self.key_in_live = (
            f'SELECT 1 FROM {live_schema}."{table}" m '  # nosec B608 -- schema/table are validated internal identifiers; values bound
            f'WHERE m."{self.live_key}" = a."{self.key_col}"'
        )
        same_payload = " AND ".join(f'm."{c}" IS a."{c}"' for c in self.mirror)
        self.payload_in_live = f"{self.key_in_live} AND {same_payload}"

    def count(self, conn: sqlite3.Connection, extra_where: str = "") -> int:
        return int(
            conn.execute(
                f'SELECT COUNT(*) FROM gcarc."{self.table}" a '  # nosec B608 -- internal registry identifiers; values bound
                f"WHERE {self.variant_where}{extra_where}",
                self.params,
            ).fetchone()[0]
        )


def _classify(
    conn: sqlite3.Connection, plan: _TablePlan, run_filter: str | None
) -> TableRestoreReport:
    report = TableRestoreReport(
        table=plan.table,
        key_column=plan.key_col,
        run_filter=run_filter,
        columns_defaulted=plan.columns_defaulted,
    )
    report.candidates = plan.count(conn)
    report.restorable = plan.count(conn, f" AND NOT EXISTS ({plan.key_in_live})")
    report.identical = plan.count(conn, f" AND EXISTS ({plan.payload_in_live})")
    report.conflicts = plan.count(
        conn,
        f" AND EXISTS ({plan.key_in_live}) AND NOT EXISTS ({plan.payload_in_live})",
    )
    if report.conflicts:
        report.conflict_key_samples = [
            str(row[0])
            for row in conn.execute(
                f'SELECT a."{plan.key_col}" FROM gcarc."{plan.table}" a '  # nosec B608 -- internal registry identifiers; values bound
                f"WHERE {plan.variant_where} AND EXISTS ({plan.key_in_live}) "
                f"AND NOT EXISTS ({plan.payload_in_live}) "
                f'ORDER BY a."{plan.key_col}" LIMIT {CONFLICT_SAMPLE_LIMIT}',
                plan.params,
            )
        ]
    return report


def _apply_table(
    conn: sqlite3.Connection,
    plan: _TablePlan,
    report: TableRestoreReport,
    run_at: str,
) -> None:
    """Restore the missing rows in ONE deferred-FK immediate transaction."""
    if not report.restorable:
        return
    targets = ([plan.live_key] if plan.rowid_keyed else []) + plan.mirror
    sources = ([f'a."{db_gc.GC_SOURCE_ROWID_COL}"'] if plan.rowid_keyed else []) + [
        f'a."{c}"' for c in plan.mirror
    ]
    conn.execute("BEGIN IMMEDIATE")
    try:
        # Txn-scoped: FK checks (the financial_facts supersedes_id self-FK)
        # run at COMMIT, so a parent+child chain restores regardless of order;
        # key-ASC ordering keeps the common lower-id-parent case incremental.
        conn.execute("PRAGMA defer_foreign_keys = ON")
        cur = conn.execute(
            f'INSERT INTO main."{plan.table}" ({_quoted(targets)}) '  # nosec B608 -- internal registry identifiers; values bound
            f'SELECT {", ".join(sources)} FROM gcarc."{plan.table}" a '
            f"WHERE {plan.variant_where} AND NOT EXISTS ({plan.key_in_live}) "
            f'ORDER BY a."{plan.key_col}"',
            plan.params,
        )
        report.restored = cur.rowcount
        conn.execute(
            "INSERT INTO gcarc.gc_manifest (run_at, policy, source_table, rows_archived) "
            "VALUES (?, 'restore', ?, ?)",
            (run_at, plan.table, report.restored),
        )
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    _log("gc_restore_applied", table=plan.table, rows=report.restored)


def _drill_table(
    conn: sqlite3.Connection,
    plan: _TablePlan,
    report: TableRestoreReport,
) -> None:
    """Insert every candidate into a schema-clone and verify with two-way EXCEPT.

    ``conn``'s OWN main schema is the throwaway drill database; the live DB is
    attached read-only as ``livedb`` and only supplies the CREATE TABLE DDL.
    """
    ddl_row = conn.execute(
        "SELECT sql FROM livedb.sqlite_master WHERE type = 'table' AND name = ?",
        (plan.table,),
    ).fetchone()
    if ddl_row is None or ddl_row[0] is None:
        raise db_gc.GcAbortedError(f"{plan.table}: no CREATE TABLE DDL in the live DB")
    ddl = str(ddl_row[0])
    conn.execute(f'DROP TABLE IF EXISTS main."{plan.table}"')
    conn.execute(ddl)  # created in the drill db's own main schema
    mirror_q = _quoted(plan.mirror)
    a_cols = ", ".join(f'a."{c}"' for c in plan.mirror)
    conn.execute(
        f'INSERT INTO main."{plan.table}" ({mirror_q}) '  # nosec B608 -- internal registry identifiers; values bound
        f'SELECT {a_cols} FROM gcarc."{plan.table}" a WHERE {plan.variant_where}',
        plan.params,
    )
    diff_out = conn.execute(
        f'SELECT COUNT(*) FROM (SELECT {mirror_q} FROM main."{plan.table}" '  # nosec B608 -- internal registry identifiers; values bound
        f'EXCEPT SELECT {mirror_q} FROM gcarc."{plan.table}" a WHERE {plan.variant_where})',
        plan.params,
    ).fetchone()[0]
    diff_back = conn.execute(
        f'SELECT COUNT(*) FROM (SELECT {mirror_q} FROM gcarc."{plan.table}" a '  # nosec B608 -- internal registry identifiers; values bound
        f"WHERE {plan.variant_where} "
        f'EXCEPT SELECT {mirror_q} FROM main."{plan.table}")',
        plan.params,
    ).fetchone()[0]
    inserted = conn.execute(
        f'SELECT COUNT(*) FROM main."{plan.table}"'  # nosec B608 -- internal registry identifiers
    ).fetchone()[0]
    report.drill_verified = bool(diff_out == 0 and diff_back == 0 and inserted == report.candidates)
    _log(
        "gc_restore_drill",
        table=plan.table,
        inserted=inserted,
        candidates=report.candidates,
        verified=report.drill_verified,
    )


def _open_drill_connection(resolved_db: Path, archive: Path, drill_dir: Path) -> sqlite3.Connection:
    """Writable throwaway DB with the live DB + archive attached READ-ONLY."""
    conn = sqlite3.connect(f"{(drill_dir / 'drill.db').as_uri()}?mode=rwc", uri=True)
    conn.execute("ATTACH DATABASE ? AS livedb", (f"{resolved_db.as_uri()}?mode=ro",))
    conn.execute("ATTACH DATABASE ? AS gcarc", (f"{archive.as_uri()}?mode=ro",))
    return conn


def run_restore(
    db_path: Path,
    *,
    mode: Literal["dry-run", "apply", "drill"],
    tables: list[str] | None = None,
    run_filter: str | None = None,
    archive_path: Path | None = None,
    lock_timeout_s: float = DEFAULT_LOCK_TIMEOUT_S,
    enforce_protected_window: bool = False,
) -> GcRestoreReport:
    if mode == "apply" and enforce_protected_window and db_gc.in_protected_window():
        raise db_gc.GcAbortedError(
            "inside the 03:00-05:00 America/Los_Angeles protected window; "
            "--ignore-protected-window overrides for a supervised restore"
        )
    resolved = db_path.resolve(strict=True)
    archive = archive_path or (resolved.parent / "archive" / db_gc.ARCHIVE_NAME)
    if not archive.exists():
        raise db_gc.GcAbortedError(f"archive not found: {archive}")
    run_at = now_naive_utc().isoformat()
    report = GcRestoreReport(
        run_at=run_at,
        db_path=str(resolved),
        archive_path=str(archive),
        mode=mode,
    )
    lock: RunLock | None = None
    if mode == "apply":
        lock = acquire_run_lock(resolved, owner="gc_restore", timeout_s=lock_timeout_s)
    try:
        live_schema = "main"
        if mode == "drill":
            drill_ctx = tempfile.TemporaryDirectory(prefix="gc_restore_drill_")
            conn = _open_drill_connection(resolved, archive, Path(drill_ctx.name))
            live_schema = "livedb"
        else:
            drill_ctx = None
            role = (
                SQLiteConnectionRole.WRITER if mode == "apply" else SQLiteConnectionRole.READ_ONLY
            )
            conn = connect_sqlite(resolved, role=role)
            conn.isolation_level = None
            db_gc.attach_archive(conn, archive)
        try:
            selected = tables if tables else _archive_tables(conn)
            legacy_tables = sorted(
                table
                for table in selected
                if db_gc.GC_RUN_ID_COL not in _columns(conn, "gcarc", table)
            )
            if legacy_tables:
                raise _preservation_only_error(legacy_tables)
            for table in selected:
                plan = _TablePlan(conn, table, run_filter, live_schema)
                table_report = _classify(conn, plan, run_filter)
                if mode == "apply":
                    _apply_table(conn, plan, table_report, run_at)
                elif mode == "drill":
                    _drill_table(conn, plan, table_report)
                report.tables.append(table_report)
                _log(
                    "gc_restore_table",
                    table=table,
                    mode=mode,
                    candidates=table_report.candidates,
                    restorable=table_report.restorable,
                    identical=table_report.identical,
                    conflicts=table_report.conflicts,
                    restored=table_report.restored,
                )
        finally:
            conn.close()
            if drill_ctx is not None:
                drill_ctx.cleanup()
    finally:
        if lock is not None:
            lock.release()
    return report


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--archive", type=Path, default=None)
    parser.add_argument(
        "--table",
        action="append",
        default=None,
        help="restrict to this table (repeatable; default: every archived table)",
    )
    parser.add_argument("--run", default=None, help="restore exactly this gc_run_id's rows")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--apply",
        action="store_true",
        help="write rows from a run-keyed archive; legacy sidecars are refused",
    )
    mode.add_argument(
        "--drill",
        action="store_true",
        help="prove restorability into a throwaway schema-clone; never touches main",
    )
    parser.add_argument("--lock-timeout-s", type=float, default=DEFAULT_LOCK_TIMEOUT_S)
    parser.add_argument("--ignore-protected-window", action="store_true")
    args = parser.parse_args(argv)
    mode_name: Literal["dry-run", "apply", "drill"] = (
        "apply" if args.apply else "drill" if args.drill else "dry-run"
    )
    try:
        report = run_restore(
            args.db,
            mode=mode_name,
            tables=args.table,
            run_filter=args.run,
            archive_path=args.archive,
            lock_timeout_s=args.lock_timeout_s,
            enforce_protected_window=not args.ignore_protected_window,
        )
    except (db_gc.GcAbortedError, RunLockHeldError, sqlite3.Error, OSError) as exc:
        _log("gc_restore_aborted", error=str(exc))
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    print(report.model_dump_json(indent=2))
    if any(t.conflicts for t in report.tables):
        return 4
    if mode_name == "drill" and not all(t.drill_verified for t in report.tables):
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
