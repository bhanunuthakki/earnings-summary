"""five_min_reread lens — what-changed + recommended action brief.

Five-minute decision-grade reread: what changed since last look, what
size adjustment is warranted, and what data would flip the call.
"""

from __future__ import annotations

from pathlib import Path

from ._shared import (
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
List 2-4 specific changes since the analyst last looked. Each must name a
concrete number, disclosure, or insider event. Sort by analytical
importance, NOT chronological.

## 2. Recommended action
ONE of: ADD <N%> / HOLD / TRIM <N%> / SELL. Pick a percentage size (of
current position) when ADD or TRIM. Justify the size in one sentence using
the DCF over/under, the trigger ladder thresholds, and what the recent
data tells you. Be opinionated — vague "watch for now" is not allowed
unless genuinely no thesis-relevant data has moved.

## 3. What would change my mind
2-3 specific data points that, if disclosed in the next 1-2 quarters,
would flip the recommendation. Concrete and falsifiable: "Cloud revenue
growth dropping below 25% YoY for 2 consecutive quarters" not "Cloud
disappointing."

Voice: terse, opinion-bearing, decision-oriented. The reader is making a
capital allocation choice in the next 5 minutes. Help them.
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
    if dcf:
        ou = float(dcf.get("over_under_pct") or 0) * 100 if dcf.get("over_under_pct") else 0.0
        npv = dcf.get("npv_per_share")
        live = dcf.get("live_price")
        mos = dcf.get("mos_bar_used")
        dcf_summary = (
            f"NPV/share: ${float(npv or 0):.0f} · Live: ${float(live or 0):.0f} · "
            f"Over/Under: {ou:+.1f}% · MoS bar: {float(mos or 0) * 100:.0f}% · "
            f"As of: {dcf.get('valuation_date')}"
        )
    latest_summary = summaries[0][1][:4000] if summaries else "(no recent earnings summary)"

    return LensContext(
        ticker=ticker,
        template_kwargs={
            "ticker": ticker,
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
