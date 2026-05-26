"""bull_case lens — structural mirror of bear_case.

Names the specific mechanism + conditions for a 3-5x outcome, and why
the market is currently missing it.
"""

from __future__ import annotations

from pathlib import Path

from ._shared import (
    Lens,
    LensContext,
    load_dcf,
    load_recent_summaries,
    sha8,
    thesis_block,
)

_PROMPT_BULL_CASE = """You are writing a structural BULL case for {ticker}. The system already has
a strong bear case. Your job here is the inverse: what WOULD have to happen
for this to work spectacularly — say, 3-5x over 5 years?

**Thesis:**
{thesis_block}

**DCF snapshot:**
{dcf_summary}

**Recent earnings summary:**
{latest_summary}

Produce a 300-450 word memo with three sections:

## 1. The asymmetric upside thesis
ONE paragraph naming the specific mechanism that, if it plays out, produces
a 3-5x return. NOT "AI is big" — concrete: which segment, which margin
inflection, which TAM expansion, which competitive moat that compounds.
Quantify the upside path.

## 2. The conditions that have to hold
List 3-5 specific conditions that have to be true for #1 to play out. Each
should be falsifiable and trackable. Avoid generic "execution" — name the
KPI that would confirm progress on each condition.

## 3. Why the market is missing it
ONE paragraph: WHY isn't this priced in today? Is it disclosure-driven
(genuinely hidden in filings)? Is it analytical bias (legacy framework
mismatch)? Is it patience-driven (timeline mismatch with sell-side cycles)?
Be specific about the source of the mispricing.

Voice: contrarian-credible. Not "buy this stock" promotional — analyst who
has examined the bear case and is naming what would falsify it on the
upside. Most underweighted by consensus.
"""


def _ctx_bull_case(ticker: str | None, repo_root: Path) -> LensContext | None:
    if not ticker:
        return None
    ticker = ticker.upper()
    summaries = load_recent_summaries(ticker, repo_root, n=1)
    dcf = load_dcf(ticker, repo_root)
    dcf_summary = "(no DCF run)"
    if dcf:
        npv = dcf.get("npv_per_share")
        live = dcf.get("live_price")
        ou = (float(dcf.get("over_under_pct") or 0)) * 100 if dcf.get("over_under_pct") else 0.0
        dcf_summary = (
            f"NPV/share: ${float(npv or 0):.0f} · Live: ${float(live or 0):.0f} · "
            f"Over/Under: {ou:+.1f}%"
        )
    if not summaries and not dcf:
        return None
    latest = summaries[0][1][:3000] if summaries else "(no recent earnings summary)"
    return LensContext(
        ticker=ticker,
        template_kwargs={
            "ticker": ticker,
            "thesis_block": thesis_block(ticker, repo_root),
            "dcf_summary": dcf_summary,
            "latest_summary": latest,
        },
        cache_inputs=[ticker, sha8(latest), sha8(dcf_summary)],
        source_doc_ids=[],
        parent_artifact_ids=[],
    )


LENS = Lens(
    name="bull_case",
    model="claude-sonnet-4-6",
    scope="ticker",
    prompt_template=_PROMPT_BULL_CASE,
    build_context=_ctx_bull_case,
)
