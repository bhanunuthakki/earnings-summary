"""Print the deterministic position-review pre-analysis for one holding, or
(``--verdict``) run the full governed review end-to-end.

Slice 1 (default): the LLM-free grounded facts (weight, dollars, break-rule
status, DCF ladder verdict, conviction-encoded flag) that answer "should I
trim <TICKER>?". Slice 2 (``--verdict``): the governed LLM verdict + the
deterministic behavioral guard + a persisted ``advisor_memos`` row (kind
``position_review``) — the same path ``review_position()`` runs, so a CLI
call and a chat ``/review`` full-verdict run land the exact same memo shape.

Run from the MAIN checkout (data/ + the holdings JSON live there, not in a
worktree):

    python execution/review_position.py RBRK
    python execution/review_position.py FLKR --at-price 70
    python execution/review_position.py RBRK --json
    python execution/review_position.py RBRK --verdict
    python execution/review_position.py RBRK --verdict --no-persist

``--at-price`` recomputes the valuation gap at that level (answers "above $70").
Degrades gracefully when the portfolio tracker is offline (falls back to the
materialized weight cache) and when a name has no encoded thesis (FLKR).

``--verdict`` calls the ``position_review`` LLM purpose through
``llm.structured.call_llm_structured`` -> ``llm.cli``, which already runs the
nested ``claude -p`` subprocess from a neutral temp cwd
(``llm.cli._neutral_subprocess_cwd``) specifically to dodge this project's
``.mcp.json`` — a project-cwd invocation otherwise hangs ~5 minutes trying to
boot every configured MCP server before being killed. That guard lives in the
shared transport, so it applies here automatically; no extra handling needed
in this script, and no reason to invoke ``claude`` directly from project cwd.
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
    AGENT_SOURCE,
    REVIEW_SOURCES,
    PositionReview,
    PreAnalysis,
    build_pre_analysis,
    render_tax_lines,
    review_position,
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
        f"  conviction encoded?  {'yes' if pre.conviction_encoded else 'NO — degrade / encode-first'}",
        f"  has stance / note    {pre.has_stance} / {pre.has_decision_note}",
        f"  index instrument?    {pre.is_index_instrument}",
        "TAX (deterministic, FIFO lots from the tracker)",
    ]
    tax_lines = render_tax_lines(pre.tax) or ["- (no tax view on this pre-analysis)"]
    lines.extend(f"  {ln[2:] if ln.startswith('- ') else ln}" for ln in tax_lines)
    return "\n".join(lines)


def _render_verdict(review: PositionReview) -> str:
    out = review.output
    lines = [
        _render(review.pre),
        "-" * 48,
        "GOVERNED VERDICT (LLM + deterministic behavioral guard)",
        f"  verdict           {out.verdict}  ({out.confidence} confidence)",
        f"  verdict source    {out.verdict_source}",
        f"  size              {out.size}",
        f"  reason            {out.reason}",
        f"  behavioral check  {out.behavioral_check}",
        f"  suggested expr.   {out.suggested_expression}",
        (
            f"  persisted memo    #{review.memo_id} (advisor_memos, kind=position_review)"
            if review.memo_id is not None
            else "  persisted memo    NOT persisted (--no-persist or no DB configured)"
        ),
    ]
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
    ap.add_argument(
        "--verdict",
        action="store_true",
        help=(
            "run the full governed review (LLM verdict + behavioral guard) instead of just "
            "the deterministic pre-analysis; persists an advisor_memos row by default"
        ),
    )
    ap.add_argument(
        "--no-persist",
        action="store_true",
        help="with --verdict, run the LLM verdict but skip persisting the advisor_memos row",
    )
    ap.add_argument(
        "--source",
        choices=REVIEW_SOURCES,
        default=AGENT_SOURCE,
        help=(
            "provenance tag written to the persisted memo (default: agent). Owner-driven "
            "surfaces pass their own; the DEFAULT 'agent' keeps automated/verification runs "
            "OUT of the owner-facing Coach P&L (a hand-run owner CLI should pass --source cli)"
        ),
    )
    args = ap.parse_args()

    if args.verdict:
        review = review_position(
            PROJECT_ROOT,
            args.ticker,
            at_price=args.at_price,
            api_url=args.api_url,
            db_path=args.db,
            persist=not args.no_persist,
            source=args.source,
        )
        if args.json:
            print(
                json.dumps(
                    {
                        "pre": dataclasses.asdict(review.pre),
                        "output": dataclasses.asdict(review.output),
                        "memo_id": review.memo_id,
                    },
                    indent=2,
                    default=str,
                )
            )
        else:
            print(_render_verdict(review))
        return 0

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
