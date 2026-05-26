"""catalyst_calendar lens — upcoming events + bingo card.

Builds per-catalyst pre-event briefs (next 90d) with what-to-watch
numbers, named-failure-mode engagement, and Q&A bingo.
"""

from __future__ import annotations

from pathlib import Path

from ._shared import (
    Lens,
    LensContext,
    load_predictions,
    load_recent_summaries,
    sha8,
    summarize_predictions,
    thesis_block,
)

_PROMPT_CATALYST_CALENDAR = """You are building the catalyst calendar for {ticker} for the next 90 days.
For each upcoming catalyst, produce a one-paragraph pre-event brief.

**Thesis:**
{thesis_block}

**Latest earnings summary:**
{latest_summary}

**Recent predictions (mgmt commitments + bear-case hypotheses + risks):**
{predictions}

Produce a memo:

## Upcoming catalysts (next 90d)
For each catalyst you can infer from the data (next earnings call, scheduled
investor days, regulatory rulings, drug readouts, contract decisions, etc.):

### [Date estimate] · [Catalyst type]
- **What to watch:** 2-3 SPECIFIC numbers / disclosures / language signals
  that would meaningfully update the thesis.
- **Named failure modes engaged:** which prior bear-case hypotheses will
  this catalyst test? Cite by name.
- **Bingo card:** 2-3 things a credible analyst would ask in Q&A but
  consensus probably won't.

If there are no clear catalysts in the data, say so plainly and identify
the EARLIEST inferable signal (e.g. monthly subscriber disclosures, weekly
flight data, etc.).
"""


def _ctx_catalyst_calendar(ticker: str | None, repo_root: Path) -> LensContext | None:
    if not ticker:
        return None
    ticker = ticker.upper()
    summaries = load_recent_summaries(ticker, repo_root, n=1)
    predictions = load_predictions(ticker, repo_root)
    if not summaries:
        return None
    return LensContext(
        ticker=ticker,
        template_kwargs={
            "ticker": ticker,
            "thesis_block": thesis_block(ticker, repo_root),
            "latest_summary": summaries[0][1][:3500],
            "predictions": summarize_predictions(predictions),
        },
        cache_inputs=[ticker, sha8(summaries[0][1])],
        source_doc_ids=[],
        parent_artifact_ids=[],
    )


LENS = Lens(
    name="catalyst_calendar",
    model="claude-sonnet-4-6",
    scope="ticker",
    prompt_template=_PROMPT_CATALYST_CALENDAR,
    build_context=_ctx_catalyst_calendar,
)
