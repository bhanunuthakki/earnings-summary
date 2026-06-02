"""Run the validation rule engine across all tracked tickers.

Inserts validation_issues rows for any value that fails range, magnitude-jump,
or source-disagreement checks. Wraps the run in start_run / end_run for audit.

The engine's checks are population-level (magnitude jumps compare consecutive
periods; source disagreement compares sources), so they cannot be moved to a
per-row insert-time gate — the surrounding data they compare against only exists
after the write. ``--gate`` makes the post-hoc engine *enforceable* instead:
run it from a cron / pipeline / CI-of-data step and it exits non-zero when this
run produced any HALT-severity issue, so egregious data (a 10x unit error, a
>1000x range violation) fails a job rather than only being recorded.

Usage:
    python execution/run_validation_engine.py            # all tickers (report only)
    python execution/run_validation_engine.py --ticker MELI
    python execution/run_validation_engine.py --gate     # exit 2 on any HALT issue
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from models.runs import StageStatus  # noqa: E402
from models.validation import Severity  # noqa: E402
from pipeline.queries import open_db  # noqa: E402
from pipeline.run_accounting import end_run, start_run  # noqa: E402
from pipeline.validation_engine import run_all_checks  # noqa: E402

# Exit code when --gate is set and the run produced HALT-severity issues. A
# distinct non-zero (not 1) so a scheduler can tell "data failed validation"
# apart from "the script itself crashed".
GATE_FAILURE_EXIT = 2


def halt_issue_count(conn: sqlite3.Connection, run_id: str) -> int:
    """Number of HALT-severity validation_issues recorded under ``run_id``."""
    row = conn.execute(
        "SELECT COUNT(*) FROM validation_issues WHERE run_id = ? AND severity = ?",
        (run_id, Severity.HALT.value),
    ).fetchone()
    return int(row[0]) if row else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ticker", help="Restrict to one ticker (default: all)")
    parser.add_argument("--db", default=str(PROJECT_ROOT / "data" / "portfolio.db"))
    parser.add_argument(
        "--gate",
        action="store_true",
        help=(
            f"Exit {GATE_FAILURE_EXIT} if this run produced any HALT-severity issue, "
            "so a scheduled/CI step can fail on egregious data."
        ),
    )
    args = parser.parse_args()

    conn = open_db(args.db)
    try:
        scope = [args.ticker.upper()] if args.ticker else ["ALL"]
        run_id = start_run(conn, directive="run_validation_engine", ticker_scope=scope)
        report = run_all_checks(conn, run_id=run_id, ticker=args.ticker)
        end_run(conn, run_id, StageStatus.OK, error_summary=None)

        total_inserted = sum(o.issues_inserted for o in report.outcomes)
        halt_count = halt_issue_count(conn, run_id)
        print(
            json.dumps(
                {
                    "run_id": run_id,
                    "ticker": args.ticker,
                    "started_at": report.started_at.isoformat(),
                    "ended_at": report.ended_at.isoformat(),
                    "total_issues_inserted": total_inserted,
                    "halt_issues": halt_count,
                    "by_rule": [
                        {
                            "rule": o.rule.value,
                            "rows_examined": o.rows_examined,
                            "issues_inserted": o.issues_inserted,
                        }
                        for o in report.outcomes
                    ],
                },
                indent=2,
            )
        )
        if args.gate and halt_count > 0:
            print(
                f"GATE FAILED: {halt_count} HALT-severity validation issue(s) this run.",
                file=sys.stderr,
            )
            return GATE_FAILURE_EXIT
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
