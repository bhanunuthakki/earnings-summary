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
    load_evaluation_artifact,
    persist_promotion,
    promotion_from_evaluation,
)
from search.embedding_runtime_artifact import load_runtime_artifact  # noqa: E402
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
    parser.add_argument("--provider", default="fastembed")
    parser.add_argument("--dimensions", type=int, required=True)
    parser.add_argument("--runtime-artifact", type=Path, required=True)
    parser.add_argument("--approved-by", required=True)
    parser.add_argument(
        "--approved-at",
        type=_approved_at,
        required=True,
        help="Owner approval timestamp with an explicit timezone.",
    )
    parser.add_argument("--supersedes-promotion-id")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    artifact, artifact_sha = load_evaluation_artifact(args.evaluation_artifact)
    runtime_artifact = load_runtime_artifact(args.runtime_artifact)
    promotion = promotion_from_evaluation(
        artifact,
        evaluation_artifact_sha256=artifact_sha,
        revision=args.revision,
        provider=args.provider,
        dimensions=args.dimensions,
        approved_by=args.approved_by,
        approved_at=args.approved_at,
        runtime_artifact=runtime_artifact,
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
