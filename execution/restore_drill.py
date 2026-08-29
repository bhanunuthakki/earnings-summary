"""Monthly DB restore drill — prove the daily backups actually restore.

A backup you have never restored is not a backup. ``cron/backup_db.py`` writes an
authenticated encrypted snapshot of ``data/portfolio.db`` daily (02:45); this drill periodically
restores the LATEST snapshot to a throwaway temp file and verifies it, so
real-world rot — Drive-sync truncation, gzip corruption, a stale/old snapshot,
or schema drift — surfaces on a schedule instead of during a real outage. It is
the scheduled counterpart to ``cron/restore_db.py``'s on-demand restore, and the
verification ``tests/test_backup_restore.py`` only ever exercises against
synthetic tmp DBs, never the real ``.gz.enc`` snapshots on Drive.

The drill NEVER touches the live DB's data — it restores to a temp path that is
deleted afterward. It writes exactly one ``ingestion_runs`` bookkeeping row
(directive=``restore_drill``) so the cron-health panel shows the drill verdict +
clean-streak alongside ``backup_db`` (best-effort: skipped if the live DB is
absent, which is itself the disaster case the drill is rehearsing for).

Checks (all on the temp copy):
  1. restore — authenticate, decrypt, gunzip the newest snapshot, then run
     ``PRAGMA integrity_check`` (reuses ``cron/restore_db.py::restore_snapshot``;
     a corrupt restore is discarded).
  2. row-count sanity — core tables (``tracked_companies``, ``financial_facts``)
     are non-empty, catching a truncated/empty snapshot that still passes the
     integrity check.
  3. schema match — fail closed unless the restored snapshot's
     ``alembic_version`` matches the live DB's exactly.

Exit code:
  0  drill passed (restore + integrity + non-empty core tables)
  1  a hard check failed (restore/integrity failed, or a core table was empty)
  2  no snapshot found (nothing to drill) / setup error

Usage:
    python execution/restore_drill.py
    python execution/restore_drill.py --backup-dir D:\\backups --keep
"""

from __future__ import annotations

import argparse
import contextlib
import json
import sqlite3
import sys
import tempfile
from hashlib import sha256
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.path.insert(0, str(PROJECT_ROOT / "cron"))
sys.path.insert(0, str(PROJECT_ROOT / "execution"))

import db_gc  # noqa: E402  (execution/db_gc.py — ARCHIVE_NAME + archive attach)
import gc_restore  # noqa: E402  (execution/gc_restore.py — archive drill)
import restore_db  # noqa: E402  (cron/restore_db.py — gunzip + integrity + list)

from models.runs import StageStatus  # noqa: E402
from pipeline.queries import open_db  # noqa: E402
from pipeline.run_accounting import (  # noqa: E402
    PipelineRunSuppressedError,
    end_run,
    start_run,
    suppression_payload,
)
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite  # noqa: E402

# A healthy snapshot has rows here; an empty one signals truncation/corruption
# that still slips past PRAGMA integrity_check (a valid-but-empty DB is "ok").
_CORE_TABLES: tuple[str, ...] = ("tracked_companies", "financial_facts")


class _AutoSnapshot:
    """Sentinel type preserving run_drill's direct-call latest-snapshot API."""


_AUTO_SNAPSHOT = _AutoSnapshot()


class SchemaRevisionError(RuntimeError):
    """The live database cannot provide one unambiguous Alembic revision."""


def _alembic_version(db_path: Path) -> str | None:
    """Return the one valid live Alembic revision, or ``None`` for no rows.

    A readable ``alembic_version`` table with zero rows uses the VERSIONED
    policy because there is no live revision to compare against. A missing or
    malformed table, multiple rows, and invalid values are different: accepting
    a snapshot when the live database has unreadable, ambiguous, or malformed
    schema state would bypass the exact-schema gate, so those conditions fail
    closed.
    """
    try:
        conn = connect_sqlite(str(db_path), role=SQLiteConnectionRole.READ_ONLY)
    except sqlite3.Error as exc:
        raise SchemaRevisionError(f"live database is unreadable: {exc}") from None
    try:
        try:
            rows = conn.execute("SELECT version_num FROM alembic_version").fetchall()
        except sqlite3.DatabaseError as exc:
            raise SchemaRevisionError(f"live Alembic revision is unreadable: {exc}") from None
    finally:
        conn.close()
    if not rows:
        return None
    if len(rows) != 1:
        raise SchemaRevisionError(
            f"live Alembic revision is ambiguous: expected exactly one row, found {len(rows)}"
        )
    value: object = rows[0][0]
    if not isinstance(value, str) or not value.strip():
        raise SchemaRevisionError("live Alembic revision is invalid: expected one non-empty string")
    return value.strip()


def _row_counts(db_path: Path, tables: tuple[str, ...]) -> dict[str, int]:
    """Row count per table; -1 if the table is missing/unreadable. Table names
    are module-level literals, never user input (so the f-string is safe)."""
    out: dict[str, int] = {}
    conn = connect_sqlite(str(db_path), role=SQLiteConnectionRole.READ_ONLY)
    try:
        for tbl in tables:
            try:
                out[tbl] = int(conn.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0])
            except sqlite3.DatabaseError:
                out[tbl] = -1
    finally:
        conn.close()
    return out


def _latest_snapshot(backup_dir: Path) -> Path | None:
    snapshots = restore_db.list_snapshots(backup_dir)
    return snapshots[-1] if snapshots else None


def _snapshot_sha256(snapshot: Path) -> str:
    digest = sha256()
    with snapshot.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def run_drill(
    backup_dir: Path,
    live_db: Path,
    *,
    keep: bool = False,
    snapshot: Path | _AutoSnapshot | None = _AUTO_SNAPSHOT,
) -> tuple[bool, dict[str, object]]:
    """Restore the newest snapshot to a temp file and verify it.

    Returns ``(ok, summary)``. Never mutates *live_db*. ``ok`` is False on a
    missing snapshot, a failed restore/integrity check, an empty core table,
    or a schema-version mismatch.
    """
    if isinstance(snapshot, _AutoSnapshot):
        snapshot = _latest_snapshot(backup_dir)
    if snapshot is None:
        return False, {"status": "no_snapshot", "error": f"no snapshots in {backup_dir}"}

    tmpdir = Path(tempfile.mkdtemp(prefix="restore_drill."))
    target = tmpdir / "drill.db"
    try:
        try:
            live_ver = _alembic_version(live_db) if live_db.exists() else None
        except SchemaRevisionError as exc:
            return False, {
                "status": "schema_unreadable",
                "error": str(exc),
                "live_schema": None,
                "schema_match": False,
                "schema_policy": restore_db.SchemaCompatibilityPolicy.EXACT.value,
            }
        schema_policy = (
            restore_db.SchemaCompatibilityPolicy.EXACT
            if live_ver is not None
            else restore_db.SchemaCompatibilityPolicy.VERSIONED
        )
        try:
            validation = restore_db.restore_snapshot(
                snapshot,
                target,
                force=False,
                schema_policy=schema_policy,
                expected_schema=live_ver,
            )
        except (OSError, RuntimeError) as exc:
            failed = exc.validation if isinstance(exc, restore_db.RestoreValidationError) else None
            schema_failed = (
                failed is not None
                and failed.schema_policy is restore_db.SchemaCompatibilityPolicy.EXACT
                and failed.schema_revision != failed.expected_schema
            )
            return False, {
                "status": "schema_incompatible" if schema_failed else "restore_failed",
                "snapshot": snapshot.name,
                "error": str(exc),
                "integrity_check": failed.integrity_check if failed else None,
                "quick_check": failed.quick_check if failed else None,
                "foreign_key_violation_count": (
                    failed.foreign_key_violation_count if failed else None
                ),
                "snapshot_schema": failed.schema_revision if failed else None,
                "live_schema": live_ver,
                "schema_match": False,
                "schema_policy": schema_policy.value,
            }

        counts = _row_counts(target, _CORE_TABLES)
        empty = [t for t, n in counts.items() if n <= 0]

        schema_match = live_ver is None or validation.schema_revision == live_ver

        ok = not empty
        summary: dict[str, object] = {
            "status": "ok" if ok else "empty_core_tables",
            "snapshot": snapshot.name,
            "snapshot_bytes": target.stat().st_size,
            "row_counts": counts,
            "integrity_check": validation.integrity_check,
            "quick_check": validation.quick_check,
            "foreign_key_violation_count": validation.foreign_key_violation_count,
            "snapshot_schema": validation.schema_revision,
            "live_schema": live_ver,
            "schema_match": schema_match,
            "schema_policy": validation.schema_policy.value,
        }
        return ok, summary
    finally:
        if not keep:
            target.unlink(missing_ok=True)
            with contextlib.suppress(OSError):
                tmpdir.rmdir()


def _start_accounting(
    live_db: Path,
    *,
    backup_dir: Path,
    keep: bool,
    snapshot_name: str | None,
    snapshot_sha256: str | None,
    live_schema: str | None,
) -> tuple[sqlite3.Connection, str] | None:
    """Start single-flight accounting before the restore/decrypt work begins."""
    if not live_db.exists():
        print("(live DB absent — skipping ingestion_runs bookkeeping)", file=sys.stderr)
        return None
    try:
        conn = open_db(live_db)
    except Exception as exc:  # bookkeeping must never fail the drill
        print(f"WARN: could not open ingestion_runs ledger: {exc}", file=sys.stderr)
        return None
    try:
        run_id = start_run(
            conn,
            directive="restore_drill",
            ticker_scope=["ALL"],
            invocation_inputs={
                "backup_dir": str(backup_dir.resolve()),
                "keep": keep,
                "live_db": str(live_db.resolve()),
                "live_schema": live_schema,
                "snapshot_name": snapshot_name,
                "snapshot_sha256": snapshot_sha256,
            },
            deduplicate_completed=True,
        )
    except PipelineRunSuppressedError:
        conn.close()
        raise
    except Exception as exc:  # bookkeeping must never fail the drill
        conn.close()
        print(f"WARN: could not record ingestion_runs start: {exc}", file=sys.stderr)
        return None
    return conn, run_id


def _finish_accounting(
    accounting: tuple[sqlite3.Connection, str] | None,
    ok: bool,
    summary: dict[str, object],
) -> None:
    """Best-effort terminal accounting after the drill finishes."""
    if accounting is None:
        return
    conn, run_id = accounting
    try:
        end_run(
            conn,
            run_id,
            StageStatus.OK if ok else StageStatus.FAILED,
            error_summary=None if ok else json.dumps(summary)[:500],
        )
    except Exception as exc:  # bookkeeping must never fail the drill
        print(f"WARN: could not record ingestion_runs finish: {exc}", file=sys.stderr)
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    from runtime.job_runtime import portfolio_db_path
    from runtime.secrets import load_project_env

    load_project_env(PROJECT_ROOT)
    configured_db = portfolio_db_path(PROJECT_ROOT)
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument(
        "--backup-dir",
        type=Path,
        default=restore_db.configured_backup_dir(),
        help="snapshot directory",
    )
    p.add_argument(
        "--db", type=Path, default=configured_db, help="live DB (read-only, for schema cmp)"
    )
    p.add_argument("--keep", action="store_true", help="keep the temp restore (debugging)")
    args = p.parse_args(argv)

    snapshot = _latest_snapshot(args.backup_dir)
    snapshot_digest = _snapshot_sha256(snapshot) if snapshot is not None else None
    try:
        live_schema = _alembic_version(args.db) if args.db.exists() else None
    except SchemaRevisionError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    try:
        accounting = _start_accounting(
            args.db,
            backup_dir=args.backup_dir,
            keep=args.keep,
            snapshot_name=snapshot.name if snapshot is not None else None,
            snapshot_sha256=snapshot_digest,
            live_schema=live_schema,
        )
    except PipelineRunSuppressedError as exc:
        print(json.dumps(suppression_payload(exc)))
        return 0

    try:
        ok, summary = run_drill(
            args.backup_dir,
            args.db,
            keep=args.keep,
            snapshot=snapshot,
        )
    except Exception as exc:
        _finish_accounting(
            accounting,
            False,
            {
                "status": "drill_error",
                "error": f"{type(exc).__name__}: {exc}"[:500],
            },
        )
        raise

    archive_ok, archive_summary = _drill_archive(args.db)
    summary["archive_drill"] = archive_summary
    overall_ok = ok and archive_ok
    _finish_accounting(accounting, overall_ok, summary)

    print(json.dumps(summary, indent=2))

    if summary.get("status") == "no_snapshot":
        return 2
    return 0 if overall_ok else 1


def _drill_archive(live_db: Path) -> tuple[bool, dict[str, object]]:
    """Verify archive recovery evidence without touching the live DB.

    The snapshot drill above covers portfolio.db; this covers its archive
    sidecar. Run-keyed archives get a restore drill. Legacy archives are
    preservation-only and get a read-only integrity/hash/inventory check.
    Absent archive = nothing pruned yet = vacuously ok. Failure here fails the
    drill (unlike the archive BACKUP leg, which is best-effort).
    """
    archive = live_db.parent / "archive" / db_gc.ARCHIVE_NAME
    if not archive.exists():
        return True, {"status": "no_archive"}
    try:
        preservation = gc_restore.inspect_preservation_archive(archive)
        if preservation.legacy_tables:
            ok = (
                preservation.integrity_check == "ok"
                and preservation.quick_check == "ok"
                and preservation.foreign_key_violation_count == 0
            )
            return ok, {
                "status": "preservation_only" if ok else "archive_unverified",
                "size_bytes": preservation.size_bytes,
                "sha256": preservation.sha256,
                "integrity_check": preservation.integrity_check,
                "quick_check": preservation.quick_check,
                "foreign_key_violation_count": preservation.foreign_key_violation_count,
                "table_row_counts": preservation.table_row_counts,
                "legacy_tables": preservation.legacy_tables,
            }
        report = gc_restore.run_restore(live_db, mode="drill")
    except Exception as exc:  # a drill that errors is a failed drill
        return False, {
            "status": "archive_drill_error",
            "error": f"{type(exc).__name__}: {exc}"[:500],
        }
    unverified = [t.table for t in report.tables if not t.drill_verified]
    conflicts = {t.table: t.conflicts for t in report.tables if t.conflicts}
    ok = not unverified
    return ok, {
        "status": "ok" if ok else "archive_unverified",
        "tables_verified": len(report.tables) - len(unverified),
        "tables_total": len(report.tables),
        "unverified_tables": unverified,
        # Conflicts are surfaced for visibility but do NOT fail the drill: they
        # mean live rows diverged from the archive (re-ingestion/recycled id),
        # which is a restore-time human decision, not archive rot.
        "conflict_tables": conflicts,
    }


if __name__ == "__main__":
    raise SystemExit(main())
