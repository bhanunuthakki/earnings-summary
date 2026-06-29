"""reverse_dcf lens — market-implied narrative.

At today's price, what (growth × margin × multiple) combination is the
market betting on, and where does that diverge from the analyst's DCF?
"""

from __future__ import annotations

import json
from pathlib import Path

from ._shared import (
    Lens,
    LensContext,
    load_dcf,
    load_recent_summaries,
    sha8,
    thesis_block,
)

_PROMPT_REVERSE_DCF = """You are reverse-engineering the market-implied narrative for {ticker}.
The analyst's DCF says fair value is one number; the market is trading at
another. Your job: tell the analyst what the market is *implicitly
betting* and where that diverges from the thesis.

**Analyst's DCF assumptions:**
{dcf_assumptions}

**Current price + DCF over/under:**
{dcf_summary}

**Analyst's thesis:**
{thesis_block}

**Latest earnings summary:**
{latest_summary}

Produce a 250-400 word memo with three sections:

## 1. What the market is implying
At the current price, what combination of (revenue growth × FCF margin ×
terminal multiple) is the market pricing in? Solve approximately — name
the implied 5-year revenue CAGR, the implied steady-state FCF margin, and
the implied exit multiple. Compare each to the analyst's DCF.

## 2. The market's implicit thesis
What is the market *betting* that the analyst's DCF doesn't capture (or
that the analyst rejects)? Examples: "Market is pricing in faster Cloud
margin expansion than the DCF assumes" or "Market is pricing in a Search
TAM that contracts faster than the DCF allows for." Be SPECIFIC about
which assumption the market is more aggressive on.

## 3. Where the edge is
If the analyst's thesis is right, what specific data point in the next
1-4 quarters will start to close the gap? OR if the market is right and
the analyst is wrong, what would the analyst have to update?

Voice: analytical, quantitative-where-possible, edge-naming. The output
should help the analyst decide: is my edge real, or am I pricing the same
thing differently than the market?
"""


def _ctx_reverse_dcf(ticker: str | None, repo_root: Path) -> LensContext | None:
    if not ticker:
        return None
    ticker = ticker.upper()
    dcf = load_dcf(ticker, repo_root)
    if not dcf:
        return None
    summaries = load_recent_summaries(ticker, repo_root, n=1)
    assumptions = json.dumps(
        {
            "npv_per_share": dcf.get("npv_per_share"),
            "wacc": dcf.get("wacc"),
            "terminal_growth": dcf.get("terminal_growth"),
            "fcf_margin": dcf.get("fcf_margin"),
            "base_revenue": dcf.get("base_revenue"),
            "revenue_growths": dcf.get("revenue_growths_json"),
            "as_of": dcf.get("valuation_date"),
        },
        default=str,
        indent=2,
    )
    ou = (float(dcf.get("over_under_pct") or 0)) * 100 if dcf.get("over_under_pct") else 0.0
    dcf_summary = (
        f"NPV/share: ${float(dcf.get('npv_per_share') or 0):.0f} · "
        f"Live: ${float(dcf.get('live_price') or 0):.0f} · "
        f"Over/Under: {ou:+.1f}%"
    )
    latest = summaries[0][1][:2500] if summaries else "(none)"
    return LensContext(
        ticker=ticker,
        template_kwargs={
            "ticker": ticker,
            "dcf_assumptions": assumptions,
            "dcf_summary": dcf_summary,
            "thesis_block": thesis_block(ticker, repo_root),
            "latest_summary": latest,
        },
        cache_inputs=[ticker, sha8(assumptions), sha8(latest)],
        source_doc_ids=[],
        parent_artifact_ids=[],
    )


LENS = Lens(
    name="reverse_dcf",
    model="claude-opus-4-8",  # quantitative reverse-engineering benefits from Opus
    scope="ticker",
    prompt_template=_PROMPT_REVERSE_DCF,
    build_context=_ctx_reverse_dcf,
)
