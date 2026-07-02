"""Print the deterministic position-review pre-analysis for one holding.

Slice 1 of the position-review service: the LLM-free grounded facts (weight,
dollars, break-rule status, DCF ladder verdict, conviction-encoded flag) that
answer "should I trim <TICKER>?". Run from the MAIN checkout (data/ + the
holdings JSON live there, not in a worktree):

    python execution/review_position.py RBRK
    python execution/review_position.py FLKR --at-price 70
    python execution/review_position.py RBRK --json

``--at-price`` recomputes the valuation gap at that level (answers "above $70").
Degrades gracefully when the portfolio tracker is offline (falls back to the
materialized weight cache) and when a name has no encoded thesis (FLKR).
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from advisor.position_review import (  # noqa: E402
    CONCENTRATION_PCT,
    PreAnalysis,
    build_pre_analysis,
    render_tax_lines,
)


def _fmt_pct(v: float | None) -> str:
    return "—" if v is None else f"{v:+.1f}%"


def _fmt_usd(v: float | None) -> str:
    return "—" if v is None else f"${v:,.0f}"


def _render(pre: PreAnalysis) -> str:
    band = (
        f"{pre.target_band[0]:.1f}-{pre.target_band[1]:.1f}%"
        if pre.target_band is not None
        else "no target recorded"
    )
    tripped = "\n".join(f"      • {r}" for r in pre.tripped_rules) or "      (none)"
    conc = f"YES (>= {CONCENTRATION_PCT:.0f}% single name)" if pre.concentration_flag else "no"
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
        f"  conviction encoded?  {'yes' if pre.conviction_encoded else 'NO — degrade / encode-first'}",
        f"  has stance / note    {pre.has_stance} / {pre.has_decision_note}",
        f"  index instrument?    {pre.is_index_instrument}",
        "TAX (deterministic, FIFO lots from the tracker)",
    ]
    tax_lines = render_tax_lines(pre.tax) or ["- (no tax view on this pre-analysis)"]
    lines.extend(f"  {ln[2:] if ln.startswith('- ') else ln}" for ln in tax_lines)
    return "\n".join(lines)


def main() -> int:
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
    ap.add_argument(
        "--db", default=str(PROJECT_ROOT / "data" / "portfolio.db"), help="portfolio.db path"
    )
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
