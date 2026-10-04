"""Verify exact retained memo evidence; append a readiness receipt when requested."""

from __future__ import annotations

import argparse
import hashlib
import os
from datetime import UTC, datetime
from pathlib import Path

from db_paths import require_db_path
from report.artifacts import ReportArtifactRef, validate_report_artifact_path
from research.decision_brief import (
    MemoContextReview,
    assess_decision_brief,
    memo_reader_blocks,
    persist_decision_brief_readiness,
)
from runtime.secrets import load_project_env
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path)
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--artifact-manifest", type=Path, required=True)
    parser.add_argument("--context-review", type=Path)
    parser.add_argument("--persist", action="store_true")
    parser.add_argument(
        "--inspect-body",
        action="store_true",
        help="Return the exact claim population without database access",
    )
    args = parser.parse_args(argv)
    artifact = ReportArtifactRef.model_validate_json(args.artifact_manifest.read_bytes())
    if args.inspect_body:
        import json

        if artifact.body_path is None:
            raise ValueError("retained memo body missing")
        body = args.repo_root / artifact.body_path
        validate_report_artifact_path(args.repo_root, body)
        raw = body.read_bytes()
        if hashlib.sha256(raw).hexdigest() != artifact.body_sha256:
            raise ValueError("retained memo body changed")
        blocks = memo_reader_blocks(raw.decode("utf-8"))
        print(
            json.dumps(
                {
                    "artifact_id": artifact.artifact_id,
                    "body_sha256": artifact.body_sha256,
                    "blocks": [item.model_dump() for item in blocks],
                },
                indent=2,
            )
        )
        return 0
    load_project_env(args.repo_root)
    configured = args.db or os.environ.get("EARNINGS_SUMMARY_DB_PATH", "").strip()
    if not configured:
        raise RuntimeError("An explicit configured portfolio database is required")
    database = require_db_path(configured)
    review = (
        MemoContextReview.model_validate_json(args.context_review.read_bytes())
        if args.context_review
        else None
    )
    conn = connect_sqlite(database, role=SQLiteConnectionRole.READ_ONLY)
    try:
        receipt = assess_decision_brief(
            conn,
            repo_root=args.repo_root,
            artifact=artifact,
            as_of=datetime.now(UTC),
            context_review=review,
        )
    finally:
        conn.close()
    if args.persist:
        persist_decision_brief_readiness(args.repo_root, receipt)
    print(receipt.model_dump_json(indent=2))
    return 0 if receipt.decision_grade else 3


if __name__ == "__main__":
    raise SystemExit(main())
