"""Initialize a bounded semantic-review queue for unsupported captured bytes."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from provenance.semantic_disposition import (  # noqa: E402
    SemanticReviewInitializationRequest,
    initialize_semantic_review_queue,
)
from runtime.job_runtime import JobAlreadyRunningError, JobLock  # noqa: E402
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite  # noqa: E402


def _datetime_arg(value: str) -> datetime:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be an ISO-8601 datetime") from error


def _event(event: str, **fields: object) -> None:
    sys.stderr.write(json.dumps({"event": event, **fields}, default=str, sort_keys=True) + "\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--inventory-key", action="append", required=True)
    parser.add_argument("--recorded-at", type=_datetime_arg, required=True)
    parser.add_argument("--batch-size", type=int, default=500)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    request = SemanticReviewInitializationRequest(
        inventory_keys=tuple(args.inventory_key),
        recorded_at=args.recorded_at,
        batch_size=args.batch_size,
        apply=args.apply,
    )
    if request.apply:
        try:
            with JobLock(
                PROJECT_ROOT,
                "initialize-semantic-review",
                [
                    f"sqlite:{args.db.resolve()}",
                    *(f"semantic-review:{key}" for key in request.inventory_keys),
                ],
            ):
                return _run(args.db, request)
        except JobAlreadyRunningError as error:
            _event("semantic_review_locked", detail=str(error))
            return 75
    return _run(args.db, request)


def _run(db_path: Path, request: SemanticReviewInitializationRequest) -> int:
    role = SQLiteConnectionRole.WRITER if request.apply else SQLiteConnectionRole.READ_ONLY
    conn = connect_sqlite(db_path, role=role, schema_preflight=request.apply)
    try:
        result = initialize_semantic_review_queue(conn, request)
    finally:
        conn.close()
    sys.stdout.write(result.model_dump_json() + "\n")
    _event(
        "semantic_review_completed",
        mode=result.mode,
        assessments_planned=result.assessments_planned,
        assessments_created=result.assessments_created,
        has_more=result.has_more,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
