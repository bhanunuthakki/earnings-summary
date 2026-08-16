"""CLI entrypoint for three-regime semantic source-regime and historical as-of backtests.

Executes metrics, DCF valuation, citations, and plausibility evaluations across
predetermined strata (10-K, 20-F, 40-F, Semiannual) under Regime 0, Regime 1, and Regime 2.
Emits structured JSON receipts to .tmp/three_regime_backtest_receipt.json.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC = PROJECT_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from evals.regime_backtest import (  # noqa: E402
    ThreeRegimeBacktestRunner,
)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run three-regime semantic and historical as-of backtests.")
    parser.add_argument(
        "--as-of-date",
        type=str,
        default="2026-04-30",
        help="Historical as-of date (ISO format YYYY-MM-DD, default: 2026-04-30)",
    )
    parser.add_argument(
        "--output-receipt",
        type=Path,
        default=PROJECT_ROOT / ".tmp" / "three_regime_backtest_receipt.json",
        help="Output receipt path (default: .tmp/three_regime_backtest_receipt.json)",
    )
    parser.add_argument("--json", action="store_true", help="Print receipt JSON to stdout")

    args = parser.parse_args()
    output_receipt: Path = args.output_receipt
    as_of_dt = date.fromisoformat(args.as_of_date)

    runner = ThreeRegimeBacktestRunner()
    cohort = ["RBRK", "WIX", "NVO", "BN", "ASML", "BHP"]

    receipt = runner.evaluate_cohort(tickers=cohort, as_of_date=as_of_dt)

    output_receipt.parent.mkdir(parents=True, exist_ok=True)
    output_receipt.write_text(json.dumps(receipt.model_dump(mode="json"), indent=2), encoding="utf-8")

    if args.json:
        print(json.dumps(receipt.model_dump(mode="json"), indent=2))
    else:
        print(
            f"Three-regime backtest complete. Status: {receipt.status} "
            f"(Tickers: {receipt.total_tickers_evaluated}, Regimes: {receipt.total_regimes_evaluated}, "
            f"Combined Quality: {receipt.regime_quality_summary.get('REGIME_2_COMBINED', 'N/A')})"
        )
        print(f"Receipt written to: {output_receipt}")

    if receipt.status != "PASS":
        sys.exit(1)


if __name__ == "__main__":
    main()
