"""filing_diff_narrative lens — YoY 10-K Item 1A risk-factor diff narration.

Reads the risk_factors table's diff markers and tells the analyst what
management is signaling with the additions / removals / rewordings.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from ._shared import (
    Lens,
    LensContext,
    sha8,
    thesis_block,
)

_PROMPT_FILING_DIFF = """You are narrating the year-over-year 10-K Item 1A risk-factor changes for
{ticker}. The schema's `risk_factors` table has new/removed/reworded
markers from automated diffing; your job is to read those and tell the
analyst what management is actually SIGNALING with the changes.

**Thesis:**
{thesis_block}

**Risk factor diffs (current 10-K vs prior 10-K):**
{risk_diffs}

Produce a 250-400 word memo with these sections:

## What was ADDED
Which new risks did management feel obligated to disclose this year? For
each meaningful addition, name the specific business mechanism it reveals.

## What was REMOVED
What risks did management feel safe dropping? Removals are sometimes more
informative than additions — they signal what management thinks has been
de-risked.

## What was REWORDED — and how
The wording delta matters. "Material adverse impact" → "could affect
results" is a softening; the reverse is a hardening. Call these out
explicitly with the before/after phrasing.

## Net read
ONE paragraph: what is management *implicitly saying* about how they see
the business evolving? Is the risk profile getting more concentrated? Are
new categories emerging (regulatory, technological, geographic)? Does the
language tone match management's commentary on calls?

If no risk_factors data exists yet, return a one-line "no Item 1A diff
data ingested yet — run `python execution/extract_risk_factors.py --ticker
{ticker}`."
"""


def _ctx_filing_diff(ticker: str | None, repo_root: Path) -> LensContext | None:
    if not ticker:
        return None
    ticker = ticker.upper()
    db = repo_root / "data" / "portfolio.db"
    if not db.exists():
        return None
    conn = sqlite3.connect(str(db))
    try:
        conn.row_factory = sqlite3.Row
        if (
            conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='risk_factors'"
            ).fetchone()
            is None
        ):
            return None
        rows = conn.execute(
            """
            SELECT fiscal_year, ordinal, heading, body_md, category, vs_prior_year, reword_diff_md
            FROM risk_factors WHERE ticker = ?
            ORDER BY fiscal_year DESC, ordinal ASC
            LIMIT 200
            """,
            (ticker,),
        ).fetchall()
    finally:
        conn.close()
    if not rows:
        return None
    diff_block = "\n".join(
        f"- [{r['fiscal_year']}#{r['ordinal']}] {r['vs_prior_year'] or 'unchanged'} · "
        f"{r['category'] or '?'} · {r['heading'] or '(no heading)'}: "
        f"{(r['reword_diff_md'] or r['body_md'] or '')[:200]}"
        for r in rows[:60]
    )
    return LensContext(
        ticker=ticker,
        template_kwargs={
            "ticker": ticker,
            "thesis_block": thesis_block(ticker, repo_root),
            "risk_diffs": diff_block,
        },
        cache_inputs=[ticker, sha8(diff_block)],
        source_doc_ids=[],
        parent_artifact_ids=[],
    )


LENS = Lens(
    name="filing_diff_narrative",
    model="claude-sonnet-4-6",
    scope="ticker",
    prompt_template=_PROMPT_FILING_DIFF,
    build_context=_ctx_filing_diff,
)
