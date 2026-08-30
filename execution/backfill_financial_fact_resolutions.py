"""Backfill immutable financial/KPI observations and canonical resolutions.

Dry-run is the default.  ``--apply`` takes one bounded, transactionally
committed batch and advances the explicit checkpoint only after the database
commit succeeds.  Unresolved material conflicts are preserved and reported;
they are intentionally absent from canonical fact views.
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from provenance.financial_fact_resolution import (  # noqa: E402
    FactCutoverRequest,
    FactTable,
    execute_fact_cutover,
    require_exact_canonical_fact_row,
)
from run_lock import hold_run_lock  # noqa: E402
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True, help="Portfolio SQLite path")
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
                args.db,
                owner="targeted-fact-resolution",
            ):
                conn = connect_sqlite(
                    args.db,
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
            conn = connect_sqlite(args.db, role=SQLiteConnectionRole.READ_ONLY)
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
    conn = connect_sqlite(args.db, role=role, schema_preflight=request.apply)
    try:
        result = execute_fact_cutover(conn, request)
    finally:
        conn.close()
    sys.stdout.write(result.model_dump_json() + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
