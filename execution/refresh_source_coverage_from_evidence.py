"""Promote current source coverage from exact extraction lineage.

The command never crawls an authority surface.  Dry run is the default; apply
appends a bounded batch of immutable coverage revisions.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from provenance.source_coverage_refresh import (  # noqa: E402
    CoverageRefreshRequest,
    refresh_source_coverage,
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
    parser.add_argument(
        "--extractor-name",
        action="append",
        dest="extractor_names",
        help="Approved extractor; repeat to allow multiple",
    )
    parser.add_argument(
        "--index-kind",
        action="append",
        choices=("lexical", "vector"),
        dest="index_kinds",
        help="Approved successful index representation; repeat to allow multiple",
    )
    parser.add_argument("--batch-size", type=int, default=500)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    request = CoverageRefreshRequest(
        inventory_keys=tuple(args.inventory_key),
        recorded_at=args.recorded_at,
        extractor_names=tuple(
            args.extractor_names
            or (
                "fulltext-evidence-backfill",
                "governed-pdf-ocr",
                "governed-image-ocr",
            )
        ),
        index_kinds=tuple(args.index_kinds or ("lexical", "vector")),
        batch_size=args.batch_size,
        apply=args.apply,
    )
    if request.apply:
        try:
            with JobLock(
                PROJECT_ROOT,
                "refresh-source-coverage",
                [
                    f"sqlite:{args.db.resolve()}",
                    *(f"source-coverage:{key}" for key in request.inventory_keys),
                ],
            ):
                return _run(args.db, request)
        except JobAlreadyRunningError as error:
            _event("source_coverage_refresh_locked", detail=str(error))
            return 75
    return _run(args.db, request)


def _run(db_path: Path, request: CoverageRefreshRequest) -> int:
    role = SQLiteConnectionRole.WRITER if request.apply else SQLiteConnectionRole.READ_ONLY
    conn = connect_sqlite(db_path, role=role, schema_preflight=request.apply)
    try:
        result = refresh_source_coverage(conn, request)
    finally:
        conn.close()
    sys.stdout.write(result.model_dump_json() + "\n")
    _event(
        "source_coverage_refresh_completed",
        mode=result.mode,
        assessments_planned=result.assessments_planned,
        assessments_created=result.assessments_created,
        has_more=result.has_more,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
