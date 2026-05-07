"""Print thesis-evaluation time-series rollups.

Shows current status, streak length, when the streak started, and any
status transitions across the recorded evaluations.

Usage:
    python execution/show_thesis_history.py                # portfolio summary
    python execution/show_thesis_history.py --ticker NOW   # single-ticker detail (transitions + history)
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from compute.thesis_history import (  # noqa: E402
    fetch_history,
    portfolio_summary,
    transitions_for,
)
from pipeline.queries import open_db  # noqa: E402


def _portfolio(conn) -> dict[str, object]:
    summaries = portfolio_summary(conn)
    return {
        "tickers": len(summaries),
        "rows": [
            {
                "ticker": s.ticker,
                "current_status": s.current_status.value,
                "streak_length": s.streak_length,
                "streak_started_at": s.streak_started_at.isoformat(),
                "last_evaluated_at": s.last_evaluated_at.isoformat(),
                "total_evaluations": s.total_evaluations,
            }
            for s in summaries
        ],
    }


def _ticker_detail(conn, ticker: str) -> dict[str, object]:
    history = fetch_history(conn, ticker)
    transitions = transitions_for(conn, ticker)
    return {
        "ticker": ticker.upper(),
        "history": [
            {
                "evaluated_at": s.evaluated_at.isoformat(),
                "status": s.status.value,
                "run_id": s.run_id,
            }
            for s in history
        ],
        "transitions": [
            {
                "from_status": t.from_status.value,
                "to_status": t.to_status.value,
                "transitioned_at": t.transitioned_at.isoformat(),
            }
            for t in transitions
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ticker", help="Single-ticker detail (history + transitions)")
    parser.add_argument("--db", default=str(PROJECT_ROOT / "data" / "portfolio.db"))
    args = parser.parse_args()

    conn = open_db(args.db)
    try:
        if args.ticker:
            print(json.dumps(_ticker_detail(conn, args.ticker), indent=2))
        else:
            print(json.dumps(_portfolio(conn), indent=2))
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
