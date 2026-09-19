"""Backfill verified legacy evidence into the append-only 0213 ledger.

The default is a read-only dry run.  ``--apply`` writes one bounded batch and
only then advances ``.tmp/<task-id>/state.json``.  stdout is one JSON summary;
structured progress and quarantine events are emitted to stderr.
Exit 2 means this batch contains quarantined documents; exit 75 means another
job owns a required write set.
"""

from __future__ import annotations

import argparse
import sys
from contextlib import ExitStack
from pathlib import Path

try:
    from _lib import PROJECT_ROOT
except ImportError:
    from execution._lib import PROJECT_ROOT

from provenance.evidence_backfill import (
    BackfillRequest,
    backfill_legacy_evidence,
    emit_structured_event,
)
from run_lock import RunLockHeldError, hold_run_lock
from runtime.job_runtime import (
    JobAlreadyRunningError,
    JobLock,
    inherited_lock_is_valid,
    portfolio_db_path,
)
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True, help="Portfolio SQLite path")
    parser.add_argument(
        "--repo-root", type=Path, default=PROJECT_ROOT, help="Explicit artifact root"
    )
    parser.add_argument("--batch-size", type=int, default=100, help="Documents per bounded batch")
    parser.add_argument(
        "--document-id",
        type=int,
        help="Prepare exactly one legacy source document without reading or advancing a checkpoint",
    )
    parser.add_argument(
        "--task-id", default="evidence-ledger-backfill", help="Checkpoint namespace"
    )
    parser.add_argument(
        "--apply", action="store_true", help="Persist this batch and its checkpoint"
    )
    args = parser.parse_args(argv)
    request = BackfillRequest(
        repo_root=args.repo_root,
        apply=args.apply,
        document_id=args.document_id,
        batch_size=args.batch_size,
        task_id=args.task_id,
    )
    if request.apply:
        target_database = args.db.resolve()
        try:
            with ExitStack() as locks:
                if not _inherited_database_lock(target_database):
                    locks.enter_context(
                        hold_run_lock(
                            target_database, owner="evidence-ledger-backfill", timeout_s=0
                        )
                    )
                if request.document_id is None:
                    checkpoint = (
                        request.repo_root.resolve() / ".tmp" / request.task_id / "state.json"
                    )
                    locks.enter_context(
                        JobLock(
                            request.repo_root,
                            "evidence-ledger-backfill",
                            [f"artifact:{checkpoint}"],
                            wait_s=0,
                        )
                    )
                return _run(target_database, request)
        except (JobAlreadyRunningError, RunLockHeldError) as error:
            emit_structured_event("evidence_ledger_backfill_locked", detail=str(error))
            return 75
    return _run(args.db, request)


def _inherited_database_lock(target_database: Path) -> bool:
    """Reuse parent ownership only when its configured lock is for this exact --db."""
    try:
        return portfolio_db_path(
            PROJECT_ROOT
        ).resolve() == target_database and inherited_lock_is_valid(PROJECT_ROOT, "portfolio-db")
    except (OSError, RuntimeError, ValueError):
        return False


def _run(db_path: Path, request: BackfillRequest) -> int:
    role = SQLiteConnectionRole.WRITER if request.apply else SQLiteConnectionRole.READ_ONLY
    conn = connect_sqlite(db_path, role=role, schema_preflight=request.apply)
    try:
        result = backfill_legacy_evidence(conn, request)
    finally:
        conn.close()
    sys.stdout.write(result.model_dump_json() + "\n")
    return 2 if result.documents_quarantined else 0


if __name__ == "__main__":
    raise SystemExit(main())
