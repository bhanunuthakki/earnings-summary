"""Populate exact document-processing evidence, dispositions, and snapshots."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from log_redact import redact  # noqa: E402
from provenance.population_document_processing import (  # noqa: E402
    DocumentProcessingPopulationRequest,
    populate_document_processing,
)
from runtime.job_runtime import JobAlreadyRunningError, JobLock  # noqa: E402
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite  # noqa: E402


def _datetime(value: str) -> datetime:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an ISO-8601 datetime") from exc


def _run(args: argparse.Namespace) -> int:
    role = SQLiteConnectionRole.WRITER if args.apply else SQLiteConnectionRole.READ_ONLY
    conn = connect_sqlite(args.db, role=role, schema_preflight=args.apply)
    try:
        result = populate_document_processing(
            conn,
            DocumentProcessingPopulationRequest(
                cutoff_at=args.cutoff_at,
                operation_recorded_at=args.operation_recorded_at,
                apply=args.apply,
                phase=args.phase,
                after_processing_obligation_revision_id=args.after_obligation_id,
                max_obligations=args.max_obligations,
                input_commitment_sha256=args.input_commitment_sha256,
                plan_commitment_sha256=args.plan_commitment_sha256,
            ),
        )
    finally:
        conn.close()
    sys.stdout.write(result.model_dump_json() + "\n")
    return int(
        result.failed_obligation_count > 0
        or result.missing_document_count > 0
        or result.unresolved_document_count > 0
        or result.incomplete_inventory_count > 0
        or result.binding_failure_count > 0
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--cutoff-at", type=_datetime, required=True)
    parser.add_argument(
        "--operation-recorded-at",
        "--recorded-at",
        dest="operation_recorded_at",
        type=_datetime,
        required=True,
    )
    parser.add_argument(
        "--phase",
        choices=("obligations", "dispositions", "snapshots", "all"),
        default="all",
    )
    parser.add_argument("--after-obligation-id")
    parser.add_argument("--max-obligations", type=int)
    parser.add_argument("--input-commitment-sha256")
    parser.add_argument("--plan-commitment-sha256")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    try:
        if not args.apply:
            return _run(args)
        with JobLock(
            PROJECT_ROOT,
            "populate-document-processing",
            ["portfolio-db", f"sqlite:{args.db.resolve()}"],
        ):
            return _run(args)
    except JobAlreadyRunningError as exc:
        sys.stderr.write(
            json.dumps(
                {"event": "document_processing_population_deferred", "reason": str(exc)},
                sort_keys=True,
            )
            + "\n"
        )
        return 75
    except Exception as exc:
        sys.stderr.write(
            json.dumps(
                {
                    "event": "document_processing_population_failed",
                    "error_type": type(exc).__name__,
                    "detail": redact(exc),
                },
                sort_keys=True,
            )
            + "\n"
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
