"""Promote one evaluated evidence embedding model with explicit owner approval."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from runtime.job_runtime import JobAlreadyRunningError, JobLock  # noqa: E402
from search.embedding_promotion import (  # noqa: E402
    EmbeddingApprovalReceipt,
    load_evaluation_artifact,
    persist_promotion,
    promotion_from_evaluation,
)
from search.embedding_runtime_registration import load_runtime_registration  # noqa: E402
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite  # noqa: E402


def _approved_at(raw: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an ISO-8601 datetime") from exc
    if parsed.tzinfo is None:
        raise argparse.ArgumentTypeError("must include an explicit timezone")
    return parsed.astimezone(UTC)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--evaluation-artifact", type=Path, required=True)
    parser.add_argument("--revision", type=int, required=True)
    parser.add_argument("--runtime-registration-id", required=True)
    parser.add_argument("--approval-spec", type=Path, required=True)
    parser.add_argument("--supersedes-promotion-id")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    artifact, _artifact_sha = load_evaluation_artifact(args.evaluation_artifact)
    approval_raw = args.approval_spec.read_text(encoding="utf-8")
    approval = EmbeddingApprovalReceipt.model_validate_json(approval_raw)
    if approval_raw.rstrip("\r\n") != approval.canonical_json():
        raise ValueError("embedding approval receipt file is not canonical JSON")
    conn = connect_sqlite(args.db, role=SQLiteConnectionRole.READ_ONLY)
    try:
        runtime_registration = load_runtime_registration(conn, args.runtime_registration_id)
    finally:
        conn.close()
    if runtime_registration is None:
        raise ValueError("embedding runtime registration is absent")
    promotion = promotion_from_evaluation(
        artifact,
        revision=args.revision,
        runtime_registration=runtime_registration,
        approval=approval,
        supersedes_promotion_id=args.supersedes_promotion_id,
    )
    if not args.apply:
        sys.stdout.write(
            json.dumps(
                {
                    "mode": "dry_run",
                    "promotion_id": promotion.promotion_id,
                    "model": promotion.model,
                    "golden_sha256": promotion.golden_sha256,
                },
                sort_keys=True,
            )
            + "\n"
        )
        return 0
    try:
        with JobLock(PROJECT_ROOT, "promote-embedding-model", ["portfolio-db"]):
            conn = connect_sqlite(args.db, role=SQLiteConnectionRole.WRITER, schema_preflight=True)
            try:
                conn.execute("BEGIN IMMEDIATE")
                result = persist_promotion(conn, promotion)
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()
    except JobAlreadyRunningError as exc:
        sys.stderr.write(
            json.dumps(
                {"event": "embedding_promotion_deferred", "reason": str(exc)},
                sort_keys=True,
            )
            + "\n"
        )
        return 75
    sys.stdout.write(
        json.dumps(
            {
                "mode": "apply",
                "promotion_id": result.promotion_id,
                "created": result.created,
                "model": promotion.model,
            },
            sort_keys=True,
        )
        + "\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
