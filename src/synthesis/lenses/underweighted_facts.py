"""underweighted_facts lens — 5 things consensus is missing.

Surfaces specific data points from the latest quarter that consensus is
mispricing or ignoring, ranked by conviction.
"""

from __future__ import annotations

from pathlib import Path

from ._shared import (
    Lens,
    LensContext,
    load_latest_financials_snapshot,
    load_recent_summaries,
    sha8,
    thesis_block,
)

_PROMPT_UNDERWEIGHTED = """You are writing the "5 underweighted facts" memo for {ticker} this quarter.
Sell-side reports the headline numbers and management's narrative. Your job:
surface 5 specific data points from this quarter that the consensus is
mispricing or ignoring.

**Thesis:**
{thesis_block}

**Latest earnings summary:**
{latest_summary}

**Latest financials snapshot (last 8Q line items):**
{financials_snapshot}

Produce a numbered list of 5 underweighted facts. For each:
- **The fact** (one sentence, with the specific number / disclosure)
- **Why consensus is missing it** (one sentence — disclosure pattern, framework
  mismatch, sell-side coverage gap, timing)
- **Why it matters for the thesis** (one sentence linking to the tier-1
  KPIs or a named failure mode)

Format: tight, scannable bullets. No more than 80 words per item.
The 5 items together should add up to a thesis the consensus does not yet
hold. The ranking matters — #1 is the highest-conviction underweighted
read.
"""


def _ctx_underweighted(ticker: str | None, repo_root: Path) -> LensContext | None:
    if not ticker:
        return None
    ticker = ticker.upper()
    summaries = load_recent_summaries(ticker, repo_root, n=1)
    if not summaries:
        return None
    financials = load_latest_financials_snapshot(ticker, repo_root)
    fin_summary = (
        "\n".join(
            f"- {r['period_end'][:10]} · {r['line_item']}: {float(r['value'] or 0):,.0f}"
            for r in financials[:40]
        )
        if financials
        else "(no financial facts available)"
    )
    return LensContext(
        ticker=ticker,
        template_kwargs={
            "ticker": ticker,
            "thesis_block": thesis_block(ticker, repo_root),
            "latest_summary": summaries[0][1][:4000],
            "financials_snapshot": fin_summary,
        },
        cache_inputs=[
            ticker,
            sha8(summaries[0][1]),
            sha8(fin_summary),
        ],
        source_doc_ids=[],
        parent_artifact_ids=[],
    )


LENS = Lens(
    name="underweighted_facts",
    model="claude-sonnet-4-6",
    scope="ticker",
    prompt_template=_PROMPT_UNDERWEIGHTED,
    build_context=_ctx_underweighted,
)
