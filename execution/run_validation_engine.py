"""Run the validation rule engine across all tracked tickers.

Inserts validation_issues rows for any value that fails range, magnitude-jump,
or source-disagreement checks. Wraps the run in start_run / end_run for audit.

Usage:
    python execution/run_validation_engine.py            # all tickers
    python execution/run_validation_engine.py --ticker MELI
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from models.runs import StageStatus  # noqa: E402
from pipeline.queries import open_db  # noqa: E402
from pipeline.run_accounting import end_run, start_run  # noqa: E402
from pipeline.validation_engine import run_all_checks  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ticker", help="Restrict to one ticker (default: all)")
    parser.add_argument("--db", default=str(PROJECT_ROOT / "data" / "portfolio.db"))
    args = parser.parse_args()

    conn = open_db(args.db)
    try:
        scope = [args.ticker.upper()] if args.ticker else ["ALL"]
        run_id = start_run(conn, directive="run_validation_engine", ticker_scope=scope)
        report = run_all_checks(conn, run_id=run_id, ticker=args.ticker)
        end_run(conn, run_id, StageStatus.OK, error_summary=None)

        total_inserted = sum(o.issues_inserted for o in report.outcomes)
        print(json.dumps({
            "run_id": run_id,
            "ticker": args.ticker,
            "started_at": report.started_at.isoformat(),
            "ended_at": report.ended_at.isoformat(),
            "total_issues_inserted": total_inserted,
            "by_rule": [
                {
                    "rule": o.rule.value,
                    "rows_examined": o.rows_examined,
                    "issues_inserted": o.issues_inserted,
                }
                for o in report.outcomes
            ],
        }, indent=2))
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
