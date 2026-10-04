"""Plan or publish an exact reviewed SEC financial fact population.

Dry-run uses a read-only database. Apply requires its exact plan digest and the
existing database writer lock. Publication does not grant canonical admission.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from provenance.reviewed_sec_financial_tables import (
    ReviewedSecFinancialRequest,
    ReviewedSecFinancialResult,
    publish_reviewed_sec_financial_tables,
)
from runtime.job_runtime import JobAlreadyRunningError, JobLock
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _run(args: argparse.Namespace) -> ReviewedSecFinancialResult:
    request = ReviewedSecFinancialRequest.model_validate_json(args.request.read_bytes())
    if request.apply or request.expected_plan_sha256 is not None:
        raise ValueError("request_file_must_contain_reviewed_dry_run_candidate")
    if args.apply:
        if args.expected_plan_sha256 is None:
            raise ValueError("exact_dry_run_plan_digest_required")
        request = ReviewedSecFinancialRequest.model_validate(
            {
                **request.model_dump(),
                "apply": True,
                "expected_plan_sha256": args.expected_plan_sha256,
            }
        )
    elif args.expected_plan_sha256 is not None:
        raise ValueError("plan_digest_requires_apply")
    role = SQLiteConnectionRole.WRITER if args.apply else SQLiteConnectionRole.READ_ONLY
    with connect_sqlite(args.db, role=role, schema_preflight=bool(args.apply)) as conn:
        return publish_reviewed_sec_financial_tables(conn, request)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--expected-plan-sha256")
    args = parser.parse_args(argv)
    try:
        if args.apply:
            with JobLock(
                PROJECT_ROOT,
                "reviewed-sec-financial-publication",
                [f"sqlite:{args.db.resolve()}", "portfolio-db"],
            ):
                result = _run(args)
        else:
            result = _run(args)
    except JobAlreadyRunningError:
        print(json.dumps({"outcome": "blocked", "reason_code": "database_writer_busy"}))
        return 75
    except (ValueError, OSError) as exc:
        # Validation exceptions can embed supplied source payloads. Keep them
        # out of operational output; retain the reviewed request for diagnosis.
        print(
            json.dumps(
                {
                    "outcome": "blocked",
                    "reason_code": "reviewed_sec_publication_invalid",
                    "error_class": type(exc).__name__,
                }
            )
        )
        return 2
    print(result.model_dump_json())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
