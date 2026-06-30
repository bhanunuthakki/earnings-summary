"""Show (or apply) the real stored competitive KPI values for a holding.

Reads each competitive KPI's value back from its source (kpi_facts for the
annual category + per-quarter mention counts; the news table for the S-1 watch)
and renders a ``current`` string. ``--show`` (default) prints them — proof the
KPIs read real values; ``--apply`` writes them into the competitive tier-2 KPIs'
``current`` field in ``RBRK.json``.

Usage:
    python execution/sync_competitive_kpis.py --ticker RBRK              # show
    python execution/sync_competitive_kpis.py --ticker RBRK --apply      # write JSON
    python execution/sync_competitive_kpis.py --ticker RBRK --db /tmp/x.db
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from competitive.holdings_sync import sync_holdings  # noqa: E402
from pipeline.queries import open_db  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db", default=str(PROJECT_ROOT / "data" / "portfolio.db"), help="Path to portfolio.db"
    )
    parser.add_argument("--ticker", default="RBRK", help="Ticker (uppercase); default RBRK")
    parser.add_argument("--repo-root", type=Path, default=PROJECT_ROOT, help="Repo root")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write resolved values into the holdings JSON (default: show only).",
    )
    args = parser.parse_args()

    conn = open_db(args.db)
    try:
        result = sync_holdings(
            conn, args.repo_root.resolve(), args.ticker.upper(), apply=args.apply
        )
    finally:
        conn.close()

    print(
        json.dumps(
            {
                "ticker": result.ticker,
                "applied": result.applied,
                "kpis": [
                    {"name": r.name, "current": r.current, "has_value": r.has_value}
                    for r in result.resolved
                ],
            },
            indent=2,
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
