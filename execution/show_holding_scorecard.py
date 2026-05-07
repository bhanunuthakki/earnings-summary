"""Print per-holding scorecard: thesis status streak + commitment hit rate + recent KPIs.

Usage:
    python execution/show_holding_scorecard.py                # all 11 thesis_state holdings
    python execution/show_holding_scorecard.py --ticker MELI  # single holding
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from compute.holding_scorecard import (  # noqa: E402
    HoldingScorecard,
    portfolio_scorecards,
    scorecard_for,
)
from pipeline.queries import open_db  # noqa: E402


def _scorecard_to_dict(s: HoldingScorecard) -> dict[str, object]:
    return {
        "ticker": s.ticker,
        "breach_status": s.breach_status.value if s.breach_status is not None else None,
        "streak": (
            None if s.streak is None
            else {
                "current_status": s.streak.current_status.value,
                "streak_length": s.streak.streak_length,
                "streak_started_at": s.streak.streak_started_at.isoformat(),
                "total_evaluations": s.streak.total_evaluations,
            }
        ),
        "commitments": {
            "total": s.commitments.total,
            "hit": s.commitments.hit,
            "beat": s.commitments.beat,
            "miss": s.commitments.miss,
            "no_data": s.commitments.no_data,
            "hit_rate_pct": (
                str(s.commitments.hit_rate_pct)
                if s.commitments.hit_rate_pct is not None
                else None
            ),
        },
        "recent_kpis": [
            {
                "name": k.name,
                "value": str(k.value),
                "period_end": k.period_end.date().isoformat(),
                "fiscal_period_type": k.fiscal_period_type,
            }
            for k in s.recent_kpis
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ticker", help="Single ticker (default: all in thesis_state)")
    parser.add_argument("--db", default=str(PROJECT_ROOT / "data" / "portfolio.db"))
    parser.add_argument(
        "--recent-kpi-limit", type=int, default=8,
        help="Max recent KPIs per holding to display (default 8).",
    )
    args = parser.parse_args()

    conn = open_db(args.db)
    try:
        if args.ticker:
            sc = scorecard_for(conn, args.ticker, recent_kpi_limit=args.recent_kpi_limit)
            print(json.dumps(_scorecard_to_dict(sc), indent=2))
        else:
            cards = portfolio_scorecards(conn, recent_kpi_limit=args.recent_kpi_limit)
            print(json.dumps(
                {"holdings": [_scorecard_to_dict(c) for c in cards]},
                indent=2,
            ))
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
