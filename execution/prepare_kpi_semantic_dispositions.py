"""Prepare one exact, read-only portfolio KPI disposition manifest."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.path.insert(0, str(PROJECT_ROOT / "execution"))

from backup_restore_readiness_receipt import BackupRestoreReadinessReceipt  # noqa: E402

from operations.review_bundle import OperationsReviewBundle  # noqa: E402
from pipeline.kpi_semantic_dispositions import (  # noqa: E402
    prepare_kpi_semantic_disposition_manifest,
)
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite  # noqa: E402

OPERATIONS_GOVERNANCE_DISPOSITION = "no_surface_change_internal_kpi_disposition_preparation"
OPERATIONS_GOVERNANCE_PRESERVED_CONTRACT = (
    "src/operations/registry.py:OperationsRegistry",
    "src/pipeline/operations_panel.py:visible_surface_dispositions",
    "src/operations/review_bundle.py:ReviewKpiCensus",
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--user-id", required=True)
    parser.add_argument("--reviewer", required=True)
    parser.add_argument("--logical-idempotency-key", required=True)
    parser.add_argument("--review-bundle", type=Path, required=True)
    parser.add_argument("--backup-restore-receipt", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    output = args.output.resolve()
    if output == args.db.resolve():
        raise ValueError("disposition manifest must not overwrite the database")
    if args.repo_root.resolve() != PROJECT_ROOT.resolve():
        raise ValueError("report configuration root must be the exact running code root")
    review_bundle = OperationsReviewBundle.model_validate_json(
        args.review_bundle.read_text(encoding="utf-8")
    )
    backup = BackupRestoreReadinessReceipt.model_validate_json(
        args.backup_restore_receipt.read_text(encoding="utf-8")
    )
    if review_bundle.schema_revision.matches is not True:
        raise ValueError("review bundle schema state is not healthy")
    if len(review_bundle.schema_revision.actual_heads) != 1:
        raise ValueError("review bundle schema revision is not singular")
    expected_revision = review_bundle.schema_revision.actual_heads[0]
    if backup.source_db_revision != expected_revision:
        raise ValueError("backup and review bundle schema revisions do not match")
    conn = connect_sqlite(args.db, role=SQLiteConnectionRole.READ_ONLY)
    try:
        manifest = prepare_kpi_semantic_disposition_manifest(
            conn,
            repo_root=args.repo_root,
            user_id=args.user_id,
            reviewer=args.reviewer,
            logical_idempotency_key=args.logical_idempotency_key,
            expected_schema_revision=expected_revision,
            review_bundle_sha256=review_bundle.content_sha256,
            backup_restore_evidence_id=backup.evidence_id,
            knowledge_at=datetime.now(UTC),
        )
    finally:
        conn.close()
    if (
        manifest.expected_database_instance_sha256
        != review_bundle.identity.database_instance_sha256
    ):
        raise ValueError("review bundle database identity does not match the source database")
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + f".{uuid4().hex}.tmp")
    temporary.write_text(manifest.model_dump_json(indent=2) + "\n", encoding="utf-8")
    temporary.replace(output)
    summary = {
        "event": "kpi_semantic_dispositions_prepared",
        "manifest_sha256": manifest.content_sha256(),
        "fact_dispositions": len(manifest.fact_dispositions),
        "report_reference_dispositions": len(manifest.report_reference_dispositions),
        "output": str(output),
    }
    print(json.dumps(summary, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
