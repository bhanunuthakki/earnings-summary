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

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from provenance.financial_fact_resolution import (  # noqa: E402
    FactCutoverRequest,
    execute_fact_cutover,
)
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
    args = parser.parse_args(argv)
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
