"""five_min_reread lens — what-changed + recommended action brief.

Five-minute decision-grade reread: what changed since last look, what
size adjustment is warranted, and what data would flip the call.
"""

from __future__ import annotations

from pathlib import Path

from llm_client import research_method_block

from ._shared import (
    DCF_FLAGGED_NOTE,
    Lens,
    LensContext,
    load_dcf,
    load_predictions,
    load_recent_insider_transactions,
    load_recent_summaries,
    sha8,
    summarize_insiders,
    summarize_predictions,
    thesis_block,
)

_PROMPT_FIVE_MIN_REREAD = """You are an analyst writing a 5-minute reread brief for {ticker}. The user
already knows the thesis. They want THREE things in 250-400 words:

**Thesis anchor:**
{thesis_block}

**DCF snapshot:**
{dcf_summary}

**Latest earnings summary (most recent quarter):**
{latest_summary}

**Recent insider activity (last 90d):**
{insider_activity}

**Predictions outcomes (last 12mo):**
{predictions}

Produce a memo with EXACTLY these three sections:

## 1. What changed
The prior review is not supplied. Do not claim changes since the last look or no change. State the latest supplied observations with their dates and comparison limits. Sort material observations by analytical importance.

## 2. Recommended action
Give a supported research stance: thesis intact, review needed, under pressure, or insufficient evidence. Discuss add/hold/trim/sell only when supplied evidence and accepted owner rules support it. Percentage sizing requires full portfolio, tax, liquidity and risk inputs; these are not supplied here. Do not invent a size or holding commitment. A missing source or unreviewed DCF is a research gap, not an automatic sell.

## 3. What would change my mind
2-3 specific data points that, if disclosed in the next 1-2 quarters,
would flip the recommendation. Use feasible public checks and preserve supplied accepted thresholds. Label new tests as proposed; do not invent a numeric threshold.

Voice: terse, opinion-bearing, decision-oriented. The reader is making a
research review. Give its practical next public check.

## Research method
{research_method}
"""


def _ctx_five_min_reread(ticker: str | None, repo_root: Path) -> LensContext | None:
    if not ticker:
        return None
    ticker = ticker.upper()
    dcf = load_dcf(ticker, repo_root)
    summaries = load_recent_summaries(ticker, repo_root, n=1)
    insiders = load_recent_insider_transactions(ticker, repo_root, days=90)
    predictions = load_predictions(ticker, repo_root)
    if not summaries and not dcf and not insiders:
        return None

    dcf_summary = "(no DCF run)"
    if dcf and dcf.get("sanity_flag"):
        dcf_summary = DCF_FLAGGED_NOTE
    elif dcf:

        def display(value: object, *, percent: bool = False) -> str:
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                return "unavailable"
            return f"{value * 100:+.1f}%" if percent else f"${value:.0f}"

        dcf_summary = (
            f"NPV/share: {display(dcf.get('npv_per_share'))} · "
            f"Live: {display(dcf.get('live_price'))} · "
            f"Over/Under: {display(dcf.get('over_under_pct'), percent=True)} · "
            f"MoS bar: {display(dcf.get('mos_bar_used'), percent=True)} · "
            f"As of: {dcf.get('valuation_date') or 'unavailable'}"
        )
    latest_summary = summaries[0][1][:4000] if summaries else "(no recent earnings summary)"

    return LensContext(
        ticker=ticker,
        template_kwargs={
            "ticker": ticker,
            "research_method": research_method_block("thesis"),
            "thesis_block": thesis_block(ticker, repo_root),
            "dcf_summary": dcf_summary,
            "latest_summary": latest_summary,
            "insider_activity": summarize_insiders(insiders),
            "predictions": summarize_predictions(predictions, max_items=10),
        },
        cache_inputs=[
            ticker,
            sha8(dcf_summary),
            sha8(latest_summary),
            sha8(summarize_insiders(insiders)),
            sha8(summarize_predictions(predictions)),
        ],
        source_doc_ids=[],
        parent_artifact_ids=[],
    )


LENS = Lens(
    name="five_min_reread",
    model="claude-sonnet-4-6",
    scope="ticker",
    prompt_template=_PROMPT_FIVE_MIN_REREAD,
    build_context=_ctx_five_min_reread,
)
