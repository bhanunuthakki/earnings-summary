"""Backfill exact PDF-table evidence for explicitly selected document versions.

Dry run is the default.  ``--apply`` persists one bounded batch and advances
its keyset checkpoint only after the SQLite transaction commits.  Stdout is
one data-only JSON object; operational events are JSON lines on stderr.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from provenance.pdf_table_backfill import (  # noqa: E402
    PdfTableBackfillRequest,
    backfill_pdf_table_evidence,
    emit_pdf_table_backfill_event,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True, help="Explicit SQLite path")
    parser.add_argument(
        "--document-version-id",
        action="append",
        required=True,
        help="Immutable evidence document-version ID; repeat for multiple PDFs",
    )
    parser.add_argument(
        "--content-root",
        type=Path,
        action="append",
        required=True,
        help="Allowed root for evidence_content_blobs file storage; repeat as needed",
    )
    parser.add_argument(
        "--recorded-at",
        type=datetime.fromisoformat,
        required=True,
        help="Timezone-aware operational timestamp, reused for exact replay",
    )
    parser.add_argument("--repo-root", type=Path, default=PROJECT_ROOT)
    parser.add_argument("--batch-size", type=int, default=25)
    parser.add_argument("--maximum-pdf-bytes", type=int, default=100 * 1024 * 1024)
    parser.add_argument("--task-id", default="pdf-table-evidence-backfill")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    try:
        request = PdfTableBackfillRequest(
            db_path=args.db,
            repo_root=args.repo_root,
            content_roots=tuple(args.content_root),
            document_version_ids=tuple(sorted(args.document_version_id)),
            recorded_at=args.recorded_at,
            apply=args.apply,
            batch_size=args.batch_size,
            maximum_pdf_bytes=args.maximum_pdf_bytes,
            task_id=args.task_id,
        )
        result = backfill_pdf_table_evidence(request)
    except Exception as error:
        emit_pdf_table_backfill_event(
            "pdf_table_evidence_backfill_failed",
            task_id=args.task_id,
            error_type=type(error).__name__,
            detail=str(error),
        )
        return 2
    sys.stdout.write(
        json.dumps(
            result.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
