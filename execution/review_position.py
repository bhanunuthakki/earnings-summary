"""Print deterministic position facts from the configured research database.

Use --json for structured output. Use --at-price to calculate the valuation gap
at a specified price. This command makes no model call and writes no memo.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from advisor.position_review import PreAnalysis

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _fmt_pct(v: float | None) -> str:
    return "—" if v is None else f"{v:+.1f}%"


def _fmt_usd(v: float | None) -> str:
    return "—" if v is None else f"${v:,.0f}"


def _render(pre: PreAnalysis) -> str:
    # The CLI establishes the source path before importing product modules.
    from advisor.position_review import render_tax_lines

    band = (
        f"{pre.target_band[0]:.1f}-{pre.target_band[1]:.1f}%"
        if pre.target_band is not None
        else "no target recorded"
    )
    tripped = "\n".join(f"      • {r}" for r in pre.tripped_rules) or "      (none)"
    conc = (
        f"{pre.concentration_zone} ({pre.weight_pct:.1f}%)"
        if pre.concentration_zone and pre.weight_pct is not None
        else (pre.concentration_zone or "n/a")
    )
    mos = f"{pre.mos_bar * 100:.0f}%" if pre.mos_bar is not None else "—"
    lines = [
        f"Position review - {pre.ticker}",
        "-" * 48,
        "SIZING",
        f"  weight            {_fmt_pct(pre.weight_pct)}  (source: {pre.weight_source})",
        f"  market value      {_fmt_usd(pre.market_value_usd)}",
        f"  unrealized P&L    {_fmt_usd(pre.unrealized_pnl_usd)}",
        f"  target band       {band}  → {pre.weight_vs_band}",
        f"  conviction (1-5)  {pre.conviction_1_5 if pre.conviction_1_5 is not None else '—'}",
        f"  concentrated?     {conc}",
        "FUNDAMENTALS",
        f"  thesis encoded?   {'yes' if pre.thesis_present else 'NO'}",
        f"  verdict           {pre.verdict_label or '—'}",
        f"  key driver        {pre.key_driver or '—'}",
        f"  break-rules       {pre.break_rule_status}",
        "  tripped/watch:",
        tripped,
        "VALUATION",
        f"  DCF fair value    {_fmt_usd(pre.npv_per_share)}  (as of {pre.dcf_date or '—'})",
        f"  asked-at price    {_fmt_usd(pre.at_price)}",
        f"  over/under        {_fmt_pct(pre.dcf_gap_pct)}  (+ = over-valued)",
        f"  mos bar           {mos}",
        f"  ladder verdict    {pre.valuation_verdict}",
        "CONVICTION / INSTRUMENT",
        f"  has decision note    {pre.has_decision_note}",
        f"  index instrument?    {pre.is_index_instrument}",
        "TAX (deterministic, FIFO lots from the tracker)",
    ]
    tax_lines = render_tax_lines(pre.tax) or ["- (no tax view on this pre-analysis)"]
    lines.extend(f"  {ln[2:] if ln.startswith('- ') else ln}" for ln in tax_lines)
    return "\n".join(lines)


def main() -> int:
    sys.path.insert(0, str(PROJECT_ROOT / "src"))
    from advisor.position_review import build_pre_analysis

    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("ticker", help="ticker to review, e.g. RBRK")
    ap.add_argument(
        "--at-price",
        type=float,
        default=None,
        help="recompute the valuation gap at this price level (answers 'above $X')",
    )
    ap.add_argument("--db", type=Path, default=None, help="explicit research database path")
    ap.add_argument(
        "--api-url", default=None, help="portfolio-tracker base URL (default: env / 127.0.0.1:8000)"
    )
    ap.add_argument("--json", action="store_true", help="emit JSON instead of the human summary")
    args = ap.parse_args()
    pre = build_pre_analysis(
        PROJECT_ROOT,
        args.ticker,
        at_price=args.at_price,
        api_url=args.api_url,
        db_path=args.db,
    )
    if args.json:
        print(json.dumps(dataclasses.asdict(pre), indent=2, default=str))
    else:
        print(_render(pre))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
