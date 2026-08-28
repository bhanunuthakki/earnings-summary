"""Extract verified full text into separate immutable evidence runs.

The default is a read-only dry run.  ``--apply`` persists one bounded batch,
then advances a checkpoint in ``.tmp/<task-id>/state.json``.  Stdout is one
JSON summary and stderr is JSONL operational events.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from provenance.fulltext_backfill import (  # noqa: E402
    FullTextBackfillRequest,
    backfill_fulltext_evidence,
)
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True, help="Portfolio SQLite path")
    parser.add_argument("--repo-root", type=Path, default=PROJECT_ROOT, help="Artifact root")
    parser.add_argument(
        "--content-root",
        type=Path,
        action="append",
        dest="content_roots",
        help="Additional allowed root for local evidence blobs; repeat as needed",
    )
    parser.add_argument(
        "--batch-size", type=int, default=100, help="Document ids per bounded batch"
    )
    parser.add_argument(
        "--document-id",
        type=int,
        help="Extract exactly one legacy document without reading or advancing a checkpoint",
    )
    parser.add_argument(
        "--max-records",
        type=int,
        default=50_000,
        help="Maximum planned ledger records per transaction (one oversized document allowed)",
    )
    parser.add_argument(
        "--max-nodes",
        type=int,
        default=50_000,
        help="Maximum substantive nodes per transaction (one oversized document allowed)",
    )
    parser.add_argument(
        "--task-id", default="fulltext-evidence-backfill", help="Checkpoint namespace"
    )
    parser.add_argument(
        "--source-lane",
        choices=("legacy", "evidence_native"),
        default="legacy",
        help="Select legacy documents or legacy-free evidence document versions",
    )
    parser.add_argument(
        "--format-scope",
        choices=("all", "office"),
        default="all",
        help="Process every supported format or only native/legacy Office candidates",
    )
    parser.add_argument("--apply", action="store_true", help="Persist this bounded batch")
    args = parser.parse_args(argv)
    request = FullTextBackfillRequest(
        repo_root=args.repo_root,
        content_roots=tuple(args.content_roots or ()),
        apply=args.apply,
        document_id=args.document_id,
        batch_size=args.batch_size,
        max_records_per_batch=args.max_records,
        max_nodes_per_batch=args.max_nodes,
        task_id=args.task_id,
        source_lane=args.source_lane,
        format_scope=args.format_scope,
    )
    role = SQLiteConnectionRole.WRITER if request.apply else SQLiteConnectionRole.READ_ONLY
    conn = connect_sqlite(args.db, role=role, schema_preflight=request.apply)
    try:
        result = backfill_fulltext_evidence(conn, request)
    finally:
        conn.close()
    sys.stdout.write(result.model_dump_json() + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
