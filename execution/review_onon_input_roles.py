"""Plan or apply an exact analyst review of ONON valuation roles."""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path

from dcf.reviewed_input_roles import ReviewedInputRoleReceipt, review_input_roles
from runtime.job_runtime import JobAlreadyRunningError, JobLock
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _run(args: argparse.Namespace) -> ReviewedInputRoleReceipt:
    if args.apply and args.expected_plan_sha256 is None:
        raise ValueError("exact_dry_run_plan_digest_required")
    if not args.apply and args.expected_plan_sha256 is not None:
        raise ValueError("plan_digest_requires_apply")
    raw = args.request.read_bytes()
    role = SQLiteConnectionRole.WRITER if args.apply else SQLiteConnectionRole.READ_ONLY
    with connect_sqlite(args.db, role=role, schema_preflight=bool(args.apply)) as conn:
        if args.apply:
            conn.execute("BEGIN IMMEDIATE")
        try:
            result = review_input_roles(
                conn,
                raw,
                as_of=datetime.now(UTC),
                apply=args.apply,
                expected_plan_sha256=args.expected_plan_sha256,
            )
            if args.apply:
                conn.commit()
            return result
        except Exception:
            if args.apply:
                conn.rollback()
            raise


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
                "reviewed-onon-input-roles",
                [f"sqlite:{args.db.resolve()}", "portfolio-db"],
            ):
                result = _run(args)
        else:
            result = _run(args)
    except JobAlreadyRunningError:
        print(json.dumps({"outcome": "blocked", "reason_code": "database_writer_busy"}))
        return 75
    except (ValueError, OSError) as exc:
        print(
            json.dumps(
                {
                    "outcome": "blocked",
                    "reason_code": "reviewed_onon_input_roles_invalid",
                    "error_class": type(exc).__name__,
                }
            )
        )
        return 2
    print(result.model_dump_json())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
