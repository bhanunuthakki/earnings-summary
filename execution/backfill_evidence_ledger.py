"""Backfill verified legacy evidence into the append-only 0213 ledger.

The default is a read-only dry run.  ``--apply`` writes one bounded batch and
only then advances ``.tmp/<task-id>/state.json``.  stdout is one JSON summary;
structured progress and quarantine events are emitted to stderr.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from provenance.evidence_backfill import BackfillRequest, backfill_legacy_evidence  # noqa: E402
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True, help="Portfolio SQLite path")
    parser.add_argument(
        "--repo-root", type=Path, default=PROJECT_ROOT, help="Explicit artifact root"
    )
    parser.add_argument("--batch-size", type=int, default=100, help="Documents per bounded batch")
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
        batch_size=args.batch_size,
        task_id=args.task_id,
    )
    role = SQLiteConnectionRole.WRITER if request.apply else SQLiteConnectionRole.READ_ONLY
    conn = connect_sqlite(args.db, role=role, schema_preflight=request.apply)
    try:
        result = backfill_legacy_evidence(conn, request)
    finally:
        conn.close()
    sys.stdout.write(result.model_dump_json() + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
