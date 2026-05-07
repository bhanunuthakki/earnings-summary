"""Match management commitments against realized kpi_facts.

Walks management_commitments where outcome IS NULL, looks up the
realized value from kpi_facts (matching ticker, kpi_name, period_target),
classifies HIT / MISS / BEAT / NO_DATA, and writes the verdict back to
the row.

Usage:
    python execution/match_commitments.py
    python execution/match_commitments.py --ticker MELI
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from compute.say_do import match_pending  # noqa: E402
from pipeline.queries import open_db  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ticker", help="Restrict to one ticker")
    parser.add_argument("--db", default=str(PROJECT_ROOT / "data" / "portfolio.db"))
    args = parser.parse_args()

    conn = open_db(args.db)
    try:
        results = match_pending(conn, ticker=args.ticker)
        rows = [
            {
                "commitment_id": r.commitment_id,
                "ticker": r.ticker,
                "kpi_name": r.kpi_name,
                "period_target": r.period_target.date().isoformat(),
                "target_value": str(r.target_value),
                "realized_value": str(r.realized_value) if r.realized_value is not None else None,
                "outcome": r.outcome.value,
            }
            for r in results
        ]
        outcome_counts: dict[str, int] = {}
        for r in rows:
            outcome_counts[r["outcome"]] = outcome_counts.get(r["outcome"], 0) + 1

        print(json.dumps({
            "matched": len(rows),
            "by_outcome": outcome_counts,
            "results": rows,
        }, indent=2))
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
