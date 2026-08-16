"""CLI entrypoint for deterministic three-regime artifact rendering.

Renders normalized HTML, Markdown, and sections.json across Regime 0, Regime 1, and Regime 2
for portfolio canaries (META, NU, BN, RBRK, ASML, WIX).
Emits structured JSON receipts to .tmp/three_regime_render_receipt.json.
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

from pipeline.three_regime_renderer import (  # noqa: E402
    ThreeRegimeDeterministicRenderer,
)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Render deterministic three-regime research artifacts."
    )
    parser.add_argument(
        "--as-of-date",
        type=str,
        default="2026-04-30",
        help="Historical as-of date (ISO format YYYY-MM-DD, default: 2026-04-30)",
    )
    parser.add_argument(
        "--output-receipt",
        type=Path,
        default=PROJECT_ROOT / ".tmp" / "three_regime_render_receipt.json",
        help="Output receipt path (default: .tmp/three_regime_render_receipt.json)",
    )
    parser.add_argument("--json", action="store_true", help="Print receipt JSON to stdout")

    args = parser.parse_args()
    output_receipt: Path = args.output_receipt
    as_of_dt = date.fromisoformat(args.as_of_date)

    renderer = ThreeRegimeDeterministicRenderer()
    cohort = ["META", "NU", "BN", "RBRK", "ASML", "WIX"]

    receipt = renderer.render_all_regimes_for_cohort(tickers=cohort, as_of_date=as_of_dt)

    output_receipt.parent.mkdir(parents=True, exist_ok=True)
    output_receipt.write_text(
        json.dumps(receipt.model_dump(mode="json"), indent=2), encoding="utf-8"
    )

    if args.json:
        print(json.dumps(receipt.model_dump(mode="json"), indent=2))
    else:
        print(
            f"Three-regime rendering complete. Status: {receipt.status} "
            f"(Tickers: {receipt.total_tickers}, Regimes: {receipt.total_regimes}, "
            f"Outputs: {receipt.total_render_outputs}, Two-Pass Verified: {receipt.all_two_pass_verified})"
        )
        print(f"Receipt written to: {output_receipt}")

    if receipt.status != "PASS":
        sys.exit(1)


if __name__ == "__main__":
    main()
