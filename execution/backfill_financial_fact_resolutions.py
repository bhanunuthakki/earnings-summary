"""Backfill immutable financial/KPI observations and canonical resolutions.

Dry-run is the default.  ``--apply`` takes one bounded, transactionally
committed batch and advances the explicit checkpoint only after the database
commit succeeds.  Unresolved material conflicts are preserved and reported;
they are intentionally absent from canonical fact views.  Each apply batch
also converges one bounded window of ``current_fact_selection`` toward the
resolution ledger, so repeated runs keep the materialized selection exactly
equal to the resolved views without unbounded scans.

Registered as the daily ``backfill-financial-fact-resolutions`` scheduler task
(``cron/backfill_financial_fact_resolutions.task.xml``).  The scheduled run
omits ``--db`` so the configured runtime database authority is used, exactly
like every other portfolio-writer job.
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from db_paths import require_db_path
from provenance.financial_fact_resolution import (
    FactCutoverRequest,
    FactTable,
    execute_fact_cutover,
    require_exact_canonical_fact_row,
)
from run_lock import hold_run_lock
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db",
        type=Path,
        default=None,
        help="Portfolio SQLite path; omit to use the configured runtime authority",
    )
    parser.add_argument(
        "--batch-size", type=int, default=500, help="Legacy fact rows per bounded batch"
    )
    parser.add_argument(
        "--task-id",
        default="financial-fact-resolution-cutover",
        help="Checkpoint namespace beneath .tmp/",
    )
    parser.add_argument(
        "--knowledge-cutoff",
        type=datetime.fromisoformat,
        default=None,
        help="Explicit ISO cutoff; defaults to current UTC time",
    )
    parser.add_argument(
        "--apply", action="store_true", help="Persist this batch and its checkpoint"
    )
    parser.add_argument(
        "--fact-table",
        choices=("financial_facts", "kpi_facts"),
        help="Reconcile exactly one already-captured fact row instead of the checkpoint batch",
    )
    parser.add_argument(
        "--fact-row-id",
        type=int,
        help="Positive row id paired with --fact-table",
    )
    args = parser.parse_args(argv)
    db_path = require_db_path(args.db)
    if (args.fact_table is None) != (args.fact_row_id is None):
        parser.error("--fact-table and --fact-row-id must be provided together")
    if args.fact_row_id is not None and args.fact_row_id <= 0:
        parser.error("--fact-row-id must be positive")
    if args.fact_table is not None:
        assert args.fact_row_id is not None
        fact_table = cast("FactTable", args.fact_table)
        fact_row_id = int(args.fact_row_id)
        cutoff = args.knowledge_cutoff or datetime.now(UTC)
        if args.apply:
            with hold_run_lock(
                db_path,
                owner="targeted-fact-resolution",
            ):
                conn = connect_sqlite(
                    db_path,
                    role=SQLiteConnectionRole.WRITER,
                    schema_preflight=True,
                )
                try:
                    conn.execute("BEGIN IMMEDIATE")
                    result = require_exact_canonical_fact_row(
                        conn,
                        fact_table=fact_table,
                        fact_row_id=fact_row_id,
                        knowledge_cutoff=cutoff,
                    )
                    conn.commit()
                except Exception:
                    if conn.in_transaction:
                        conn.rollback()
                    raise
                finally:
                    conn.close()
        else:
            conn = connect_sqlite(db_path, role=SQLiteConnectionRole.READ_ONLY)
            try:
                result = require_exact_canonical_fact_row(
                    conn,
                    fact_table=fact_table,
                    fact_row_id=fact_row_id,
                    knowledge_cutoff=cutoff,
                    persist=False,
                )
            finally:
                conn.close()
        sys.stdout.write(result.model_dump_json() + "\n")
        return 0 if result.resolution_status == "resolved" else 2
    checkpoint = PROJECT_ROOT / ".tmp" / args.task_id / "state.json"
    request = FactCutoverRequest(
        apply=args.apply,
        batch_size=args.batch_size,
        checkpoint_path=checkpoint,
        knowledge_cutoff=args.knowledge_cutoff or datetime.now(UTC),
    )
    role = SQLiteConnectionRole.WRITER if request.apply else SQLiteConnectionRole.READ_ONLY
    conn = connect_sqlite(db_path, role=role, schema_preflight=request.apply)
    try:
        result = execute_fact_cutover(conn, request)
    finally:
        conn.close()
    sys.stdout.write(result.model_dump_json() + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
