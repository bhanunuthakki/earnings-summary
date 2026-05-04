"""Run a DCF valuation for a ticker and persist to dcf_runs.

Reads base revenue + diluted-shares from the most recent FY in financial_facts,
projects FCF over the horizon under user-supplied growth + margin assumptions,
discounts at WACC, and adds a Gordon-Growth terminal value.

Usage:
    python execution/run_dcf.py --ticker GOOG \\
        --growth 0.10 0.08 0.06 0.05 0.04 \\
        --fcf-margin 0.20 \\
        --wacc 0.09 \\
        --terminal-growth 0.025
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from compute.dcf import run_dcf_for_ticker  # noqa: E402
from pipeline.queries import open_db  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ticker", required=True, help="Ticker (uppercased)")
    parser.add_argument(
        "--growth",
        nargs="+",
        type=float,
        required=True,
        help="Per-year revenue growth rates (e.g. 0.10 0.08 0.06)",
    )
    parser.add_argument(
        "--fcf-margin", type=float, required=True, help="Constant FCF margin (e.g. 0.20)"
    )
    parser.add_argument("--wacc", type=float, required=True, help="WACC (e.g. 0.09)")
    parser.add_argument(
        "--terminal-growth",
        type=float,
        required=True,
        help="Terminal growth rate (e.g. 0.025); must be < wacc",
    )
    parser.add_argument("--notes", help="Optional analyst notes recorded with the run")
    parser.add_argument(
        "--db", default=str(PROJECT_ROOT / "data" / "portfolio.db"), help="Path to portfolio.db"
    )
    args = parser.parse_args()

    conn = open_db(args.db)
    try:
        inputs, result, row_id = run_dcf_for_ticker(
            conn,
            ticker=args.ticker,
            revenue_growths=args.growth,
            fcf_margin=args.fcf_margin,
            wacc=args.wacc,
            terminal_growth=args.terminal_growth,
            notes=args.notes,
        )
    finally:
        conn.close()

    output = {
        "ticker": args.ticker.upper(),
        "dcf_run_id": row_id,
        "inputs": {
            "base_revenue_billions": inputs.base_revenue / 1e9,
            "horizon_years": len(inputs.revenue_growths),
            "revenue_growths": inputs.revenue_growths,
            "fcf_margin": inputs.fcf_margin,
            "wacc": inputs.wacc,
            "terminal_growth": inputs.terminal_growth,
            "shares_outstanding_millions": (
                inputs.shares_outstanding / 1e6 if inputs.shares_outstanding else None
            ),
        },
        "outputs": {
            "projected_fcfs_billions": [f / 1e9 for f in result.projected_fcfs],
            "sum_dcf_billions": result.sum_of_discounted_fcfs / 1e9,
            "terminal_value_billions": result.terminal_value / 1e9,
            "discounted_terminal_billions": result.discounted_terminal / 1e9,
            "npv_billions": result.npv / 1e9,
            "npv_per_share": result.npv_per_share,
        },
    }
    print(json.dumps(output, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
