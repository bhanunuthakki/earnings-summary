"""Plan a governed SEC delta refresh without network or database writes."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import date
from pathlib import Path

from pydantic import ValidationError

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from pipeline.sec_delta_planner import (  # noqa: E402
    EvaluationAuthorization,
    SecDeltaPlannerRequest,
    SnapshotSafetyError,
    build_sec_delta_plan,
    write_sec_delta_plan,
)


def _event(event: str, **fields: object) -> None:
    sys.stderr.write(
        json.dumps({"event": event, **fields}, sort_keys=True, separators=(",", ":")) + "\n"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument(
        "--as-of",
        required=True,
        help="Required YYYY-MM-DD knowledge cutoff and task-identity date",
    )
    parser.add_argument(
        "--evaluation-request",
        action="append",
        default=[],
        metavar="TICKER:OWNER_REQUEST_ID",
        help="Explicit active-evaluation authorization; repeat for additional tickers",
    )
    parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="Governed durable destination for the full sealed plan; .tmp is forbidden",
    )
    args = parser.parse_args(argv)
    try:
        request = SecDeltaPlannerRequest(
            database_path=args.db,
            as_of=date.fromisoformat(str(args.as_of)),
            evaluation_requests=tuple(
                EvaluationAuthorization.parse(str(value)) for value in args.evaluation_request
            ),
        )
        plan = build_sec_delta_plan(request)
        terminal = write_sec_delta_plan(plan, args.output)
    except SnapshotSafetyError:
        _event(
            "sec_delta_plan_failed",
            error_code="unsafe_or_invalid_snapshot",
            error_type="SnapshotSafetyError",
        )
        return 2
    except (OSError, RuntimeError, sqlite3.Error, ValueError, ValidationError) as exc:
        _event(
            "sec_delta_plan_failed",
            error_code="invalid_request_or_plan_artifact",
            error_type=type(exc).__name__,
        )
        return 2
    sys.stdout.write(terminal.model_dump_json() + "\n")
    _event(
        "sec_delta_plan_completed",
        status=terminal.status,
        terminal_receipt_sha256=terminal.receipt_sha256,
        plan_sha256=terminal.plan_sha256,
        ticker_count=terminal.ticker_count,
        blocked_task_count=terminal.blocked_task_count,
    )
    return 2 if terminal.status == "BLOCKED" else 0


if __name__ == "__main__":
    raise SystemExit(main())
