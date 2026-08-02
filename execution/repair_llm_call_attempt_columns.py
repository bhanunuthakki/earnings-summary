"""Repair the five ``llm_calls`` attempt-attribution columns prod never received.

Why this is a script and NOT a migration
----------------------------------------
``0212_llm_call_attempt_attribution`` adds ``auth_class``, ``attempt_count``,
``retry_count``, ``fallback_from_provider`` and ``fallback_from_transport``.
Its code is correct and idempotent, and its neighbours applied cleanly (0206
3/3, 0210 4/4) — but on the production database all five are ABSENT while
``alembic_version`` reads far past 0212. The version pointer was advanced
without the migration executing (a stamp or a restore), so alembic believes
0212 is applied and **``alembic upgrade head`` can never repair it: there is
nothing left to run.**

A *new* migration would work, but it is the wrong shape for the problem:

* Every fresh clone and every test database already gets these columns from
  0212, which runs correctly there. **Exactly one database has the gap.**
* Adding any migration moves the chain head, and the head is pinned as a
  constant in 23 places across 10 test files. That is a large, unrelated diff
  in the active governed-state program's tests — a lot of blast radius to fix
  one database.
* A migration also has no honest ``downgrade``. Dropping the columns is wrong
  wherever 0212 *did* run (it would destroy columns 0212 legitimately owns and
  leave that database at the previous revision with 0212 still marked applied
  — the same inconsistency class, inverted), and a no-op downgrade leaves
  schema drift behind. There is no correct answer because the operation is not
  a schema *evolution* at all.

This is a one-time repair of one database's divergence from its recorded
revision. That is deterministic, single-purpose, prod-facing work — Layer 3.

Contract
--------
* **Idempotent.** Adds only the columns actually missing; re-running is a
  logged no-op. Safe to run against a database where 0212 landed normally.
* **Never touches ``alembic_version``.** The revision pointer is already
  correct in claiming 0212 applied; this makes the schema match the claim.
  Rewriting the pointer would corrupt a correct value to fix an incorrect one.
* **Additive only.** Every column is nullable with no default, so no existing
  row is rewritten and no reader can break. There is no delete path and no
  ``--undo``: dropping these columns is what a downgrade would get wrong, and
  a repair that can un-repair into a broken state is not a repair.
* **Backfills what is recoverable, and only that.** ``attempt_count`` /
  ``retry_count`` are count-shaped renames of 0210's ``attempts`` / ``retries``
  and are recovered from them. ``auth_class`` and the two ``fallback_from_*``
  columns stay NULL on historical rows — those facts were never recorded and
  cannot be reconstructed. Inventing them would be worse than their absence.
* **Holds the portfolio write-set run lock under ``--apply``**, per AGENTS.md's
  one-writer-owns-the-write-set rule and the db_gc precedent. A held lock is a
  loud abort, never a queue.
* Dry run by default; ``--apply`` writes. Structured JSON events to stderr, the
  run summary as JSON on stdout.

Usage::

    python execution/repair_llm_call_attempt_columns.py            # inspect
    python execution/repair_llm_call_attempt_columns.py --apply    # repair
"""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
from pathlib import Path
from typing import cast

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from run_lock import RunLockHeldError, hold_run_lock  # noqa: E402

_EXIT_FAILED = 1
_EXIT_BAD_ARGS = 2
_EXIT_LOCK_HELD = 3

_TABLE = "llm_calls"

#: The 0212 columns, with the SQLite type each is declared with. Nullable, no
#: default — purely additive, so no existing row is rewritten.
_COLUMNS: tuple[tuple[str, str], ...] = (
    ("auth_class", "TEXT"),
    ("attempt_count", "INTEGER"),
    ("retry_count", "INTEGER"),
    ("fallback_from_provider", "TEXT"),
    ("fallback_from_transport", "TEXT"),
)

#: 0212's own recovery step: the count-shaped columns supersede 0210's
#: ``attempts`` / ``retries``, which remain as compatibility projections. Only
#: applied where the source column exists and the target is still NULL.
#:
#: The statement is carried as a LITERAL per pair rather than composed from the
#: names. There are exactly two, both known when this file is written, so
#: building them dynamically bought nothing and put interpolated identifiers in
#: a SQL string — which is a real SQL-injection shape even when today's inputs
#: happen to be constants. Static SQL removes the question instead of arguing
#: about it, and needs no scanner suppression.
_BACKFILL: tuple[tuple[str, str, str], ...] = (
    (
        "attempt_count",
        "attempts",
        'UPDATE "llm_calls" SET "attempt_count" = "attempts" '
        'WHERE "attempt_count" IS NULL AND "attempts" IS NOT NULL',
    ),
    (
        "retry_count",
        "retries",
        'UPDATE "llm_calls" SET "retry_count" = "retries" '
        'WHERE "retry_count" IS NULL AND "retries" IS NOT NULL',
    ),
)

log = logging.getLogger("repair_llm_call_attempt_columns")


class _JsonLineFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = (
            cast("dict[str, object]", record.msg)
            if isinstance(record.msg, dict)
            else {"message": record.getMessage()}
        )
        return json.dumps({"level": record.levelname, **payload}, default=str)


def _configure_logging(verbose: bool) -> None:
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(_JsonLineFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(logging.DEBUG if verbose else logging.INFO)


#: Every identifier this module is ever allowed to interpolate into SQL: the
#: one table plus the columns named in _COLUMNS / _BACKFILL. Nothing here comes
#: from argv, the database, or any caller.
_KNOWN_IDENTIFIERS: frozenset[str] = frozenset(
    {_TABLE}
    | {name for name, _type in _COLUMNS}
    | {t for t, _s, _sql in _BACKFILL}
    | {s for _t, s, _sql in _BACKFILL}
)


def _assert_known_identifier(*names: str) -> None:
    """Fail loudly if a SQL identifier is not one of this module's constants.

    SQLite cannot bind an identifier, so table and column names must be
    interpolated. This makes that safe by construction rather than by comment:
    the allowlist is derived from the same constants the statements are built
    from, so it cannot drift from them, and any future edit that routed an
    external value here would raise instead of producing a query.
    """
    unknown = [n for n in names if n not in _KNOWN_IDENTIFIERS]
    if unknown:
        raise ValueError(f"refusing to build SQL with unknown identifier(s): {unknown!r}")


def _columns_of(conn: sqlite3.Connection, table: str) -> set[str]:
    # PRAGMA cannot be parameterised either; same allowlist check.
    _assert_known_identifier(table)
    return {str(row[1]) for row in conn.execute(f'PRAGMA table_info("{table}")')}


def inspect(conn: sqlite3.Connection) -> dict[str, object]:
    """What the repair would do. Pure — no writes."""
    tables = {str(r[0]) for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if _TABLE not in tables:
        return {"table_present": False, "missing": [], "backfillable": []}
    existing = _columns_of(conn, _TABLE)
    missing = [name for name, _type in _COLUMNS if name not in existing]
    # A backfill is possible only where the SOURCE column exists. On a database
    # missing the 0210 columns too, the counts stay NULL rather than erroring —
    # absence is reported, never silently treated as zero.
    backfillable = [
        (target, source)
        for target, source, _sql in _BACKFILL
        if source in existing and (target in missing or target in existing)
    ]
    return {
        "table_present": True,
        "existing_columns": sorted(existing),
        "missing": missing,
        "backfillable": [f"{t}<-{s}" for t, s in backfillable],
    }


def repair(conn: sqlite3.Connection) -> dict[str, object]:
    """Add the missing columns and backfill the recoverable counts.

    Idempotent: adds only what is absent, and backfills only rows whose target
    is still NULL, so a second run reports zeros and changes nothing.
    """
    existing = _columns_of(conn, _TABLE)
    added: list[str] = []
    for name, sql_type in _COLUMNS:
        if name in existing:
            continue
        _assert_known_identifier(_TABLE, name)
        conn.execute(f'ALTER TABLE "{_TABLE}" ADD COLUMN "{name}" {sql_type}')
        added.append(name)
        log.info({"event": "column_added", "table": _TABLE, "column": name, "type": sql_type})

    now_present = _columns_of(conn, _TABLE)
    backfilled: dict[str, int] = {}
    for target, source, statement in _BACKFILL:
        if target not in now_present or source not in now_present:
            log.warning(
                {
                    "event": "backfill_skipped",
                    "target": target,
                    "source": source,
                    "reason": "source or target column absent",
                    "note": "counts stay NULL — never defaulted to 0",
                }
            )
            continue
        cur = conn.execute(statement)
        backfilled[target] = cur.rowcount
        log.info({"event": "backfilled", "target": target, "source": source, "rows": cur.rowcount})

    conn.commit()
    return {"added": added, "backfilled": backfilled}


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Repair the five llm_calls attempt-attribution columns that migration "
            "0212 never applied to this database. Idempotent; dry run by default."
        )
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write the changes. Without this the run only reports what it would do.",
    )
    parser.add_argument(
        "--db-path",
        type=Path,
        default=None,
        help="Database to repair. Defaults to data/portfolio.db under the repo root.",
    )
    parser.add_argument(
        "--lock-timeout-s",
        type=float,
        default=30.0,
        help="Seconds to wait for the portfolio run lock under --apply (default 30).",
    )
    parser.add_argument("--verbose", action="store_true", help="Debug-level logging.")
    args = parser.parse_args()

    _configure_logging(args.verbose)

    db_path: Path = (
        args.db_path if args.db_path is not None else PROJECT_ROOT / "data" / "portfolio.db"
    )
    if not db_path.exists():
        log.error({"event": "db_missing", "path": str(db_path)})
        return _EXIT_BAD_ARGS

    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA busy_timeout = 30000")
    try:
        plan = inspect(conn)
        if not plan["table_present"]:
            log.error(
                {
                    "event": "table_missing",
                    "table": _TABLE,
                    "path": str(db_path),
                    "hint": "this database predates llm_calls entirely; nothing to repair",
                }
            )
            return _EXIT_BAD_ARGS

        missing = cast("list[str]", plan["missing"])
        log.info(
            {
                "event": "inspected",
                "path": str(db_path),
                "missing_count": len(missing),
                "missing": missing,
            }
        )

        if not missing:
            summary: dict[str, object] = {
                "db_path": str(db_path),
                "applied": False,
                "already_repaired": True,
                "added": [],
                "backfilled": {},
                "note": "all five 0212 columns already present — nothing to do",
            }
            json.dump(summary, sys.stdout, indent=2, default=str)
            sys.stdout.write("\n")
            return 0

        if not args.apply:
            summary = {
                "db_path": str(db_path),
                "applied": False,
                "would_add": missing,
                "would_backfill": plan["backfillable"],
                "hint": "re-run with --apply to write",
            }
            json.dump(summary, sys.stdout, indent=2, default=str)
            sys.stdout.write("\n")
            return 0

        # One writer owns the write set (AGENTS.md §Concurrency). A held lock
        # aborts loudly rather than queueing behind an unknown holder.
        try:
            with hold_run_lock(
                db_path,
                owner="repair_llm_call_attempt_columns",
                timeout_s=args.lock_timeout_s,
            ):
                result = repair(conn)
        except RunLockHeldError as exc:
            log.error(
                {
                    "event": "run_lock_held",
                    "error": str(exc),
                    "hint": "another writer owns portfolio.db; retry when it finishes",
                }
            )
            return _EXIT_LOCK_HELD

        # Verify against the database rather than trusting the write path.
        remaining = [n for n, _t in _COLUMNS if n not in _columns_of(conn, _TABLE)]
        if remaining:
            log.error({"event": "repair_incomplete", "still_missing": remaining})
            return _EXIT_FAILED

        summary = {
            "db_path": str(db_path),
            "applied": True,
            "added": result["added"],
            "backfilled": result["backfilled"],
            "verified_all_present": True,
        }
        json.dump(summary, sys.stdout, indent=2, default=str)
        sys.stdout.write("\n")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
