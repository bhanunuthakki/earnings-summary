"""Seed one-row-per-metric kpi_definitions for tracked names with concrete tier-1 KPIs.

The original kpi_definitions rows used `;`-delimited blobs in `name`; this script
replaces those with one row per discrete metric so kpi_facts can join on a
specific KPI. Idempotent: re-runnable, INSERT OR IGNORE on (ticker, name).

Each entry below comes from `micro_thesis/holdings/<TICKER>.json`'s tier_1_kpis
list. Adding a ticker = adding one block.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from models.documents import SourceType  # noqa: E402
from models.facts import Unit  # noqa: E402
from models.kpis import ThesisTier  # noqa: E402
from pipeline.queries import open_db  # noqa: E402

# Per-ticker normalized KPI specs. Each tuple: (name, unit, primary_source, tier).
# Unit semantics:
#   - PERCENT for growth rates / margins (e.g. 24.5 means 24.5%)
#   - RATIO for fractions (e.g. 0.245)
#   - ACTUAL for currency-denominated absolute values
#   - COUNT for headcount / customer counts
#   - BPS for basis-point deltas
# Pick the unit that matches how the issuer actually reports it.
_SEEDS: dict[str, list[tuple[str, Unit, SourceType, ThesisTier]]] = {
    "MELI": [
        ("Revenue Growth (FXN)", Unit.PERCENT, SourceType.IR_DOC, ThesisTier.TIER_1_BREAK),
        ("GMV Growth (FXN)", Unit.PERCENT, SourceType.IR_DOC, ThesisTier.TIER_1_BREAK),
        ("TPV Growth (FXN)", Unit.PERCENT, SourceType.IR_DOC, ThesisTier.TIER_1_BREAK),
        ("Operating Margin", Unit.PERCENT, SourceType.IR_DOC, ThesisTier.TIER_1_BREAK),
        ("Items Sold", Unit.COUNT, SourceType.IR_DOC, ThesisTier.TIER_2_MONITOR),
        ("Fintech MAU", Unit.COUNT, SourceType.IR_DOC, ThesisTier.TIER_2_MONITOR),
        ("Credit NPL 90d+", Unit.PERCENT, SourceType.IR_DOC, ThesisTier.TIER_1_BREAK),
        ("Take Rate", Unit.PERCENT, SourceType.IR_DOC, ThesisTier.TIER_2_MONITOR),
    ],
    "NU": [
        ("Revenue Growth (FXN)", Unit.PERCENT, SourceType.IR_DOC, ThesisTier.TIER_1_BREAK),
        ("Customer Count", Unit.COUNT, SourceType.IR_DOC, ThesisTier.TIER_1_BREAK),
        ("Monthly ARPAC", Unit.ACTUAL, SourceType.IR_DOC, ThesisTier.TIER_1_BREAK),
        ("NPL 90d+", Unit.PERCENT, SourceType.IR_DOC, ThesisTier.TIER_1_BREAK),
        ("NPL 15-90d", Unit.PERCENT, SourceType.IR_DOC, ThesisTier.TIER_2_MONITOR),
        ("Cost-to-Serve per Active Customer", Unit.ACTUAL, SourceType.IR_DOC, ThesisTier.TIER_1_BREAK),
        ("NIM", Unit.PERCENT, SourceType.IR_DOC, ThesisTier.TIER_2_MONITOR),
        ("Activity Rate", Unit.PERCENT, SourceType.IR_DOC, ThesisTier.TIER_2_MONITOR),
        ("Efficiency Ratio", Unit.PERCENT, SourceType.IR_DOC, ThesisTier.TIER_2_MONITOR),
    ],
    "NOW": [
        ("Subscription Revenue Growth (CC)", Unit.PERCENT, SourceType.IR_DOC, ThesisTier.TIER_1_BREAK),
        ("cRPO Growth", Unit.PERCENT, SourceType.IR_DOC, ThesisTier.TIER_1_BREAK),
        ("RPO Growth", Unit.PERCENT, SourceType.IR_DOC, ThesisTier.TIER_2_MONITOR),
        ("Net Revenue Retention", Unit.PERCENT, SourceType.IR_DOC, ThesisTier.TIER_1_BREAK),
        ("Customers >$1M ACV", Unit.COUNT, SourceType.IR_DOC, ThesisTier.TIER_2_MONITOR),
        ("Subscription Gross Margin", Unit.PERCENT, SourceType.IR_DOC, ThesisTier.TIER_2_MONITOR),
        ("Pro+ SKU Adoption Rate", Unit.PERCENT, SourceType.IR_DOC, ThesisTier.TIER_1_BREAK),
        ("Renewal Rate", Unit.PERCENT, SourceType.IR_DOC, ThesisTier.TIER_2_MONITOR),
    ],
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db", default=str(PROJECT_ROOT / "data" / "portfolio.db"), help="Path to portfolio.db"
    )
    parser.add_argument(
        "--ticker",
        help="Seed only one ticker (uppercase). Default: all tickers in _SEEDS.",
    )
    args = parser.parse_args()

    conn = open_db(args.db)
    try:
        scope = (
            {args.ticker.upper(): _SEEDS[args.ticker.upper()]}
            if args.ticker
            else _SEEDS
        )

        inserted = 0
        skipped = 0
        for ticker, specs in scope.items():
            for name, unit, src, tier in specs:
                cur = conn.execute(
                    "INSERT OR IGNORE INTO kpi_definitions "
                    "(ticker, name, unit, primary_source, fallback_source, ir_url, "
                    " threshold_tier, threshold_low, threshold_high, notes) "
                    "VALUES (?, ?, ?, ?, NULL, NULL, ?, NULL, NULL, NULL)",
                    (ticker, name, unit.value, src.value, tier.value),
                )
                if cur.rowcount > 0:
                    inserted += 1
                else:
                    skipped += 1
        conn.commit()

        print(json.dumps({
            "tickers": list(scope.keys()),
            "inserted": inserted,
            "skipped_existing": skipped,
        }, indent=2))
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
