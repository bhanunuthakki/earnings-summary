"""Print thesis-evaluation time-series rollups.

Shows current status, streak length, when the streak started, and any
status transitions across the recorded evaluations.

Usage:
    python execution/show_thesis_history.py                # portfolio summary
    python execution/show_thesis_history.py --ticker NOW   # single-ticker detail (transitions + history)
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

try:  # direct script invocation
    from _lib import add_database_argument, command_parser
except ImportError:  # pragma: no cover - package import fallback
    from execution._lib import add_database_argument, command_parser

from compute.thesis_history import (
    fetch_history,
    portfolio_summary,
    transitions_for,
)
from pipeline.queries import open_db


def _portfolio(conn: sqlite3.Connection) -> dict[str, object]:
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


def _ticker_detail(conn: sqlite3.Connection, ticker: str) -> dict[str, object]:
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


def main(argv: list[str] | None = None) -> int:
    parser = command_parser(__doc__)
    add_database_argument(
        parser,
        flag="--db",
        default=Path(__file__).resolve().parents[1] / "data" / "portfolio.db",
    )
    parser.add_argument("--ticker", help="Single-ticker detail (history + transitions)")
    args = parser.parse_args(argv)

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
