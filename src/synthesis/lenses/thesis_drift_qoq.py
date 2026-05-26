"""thesis_drift_qoq lens — quarter-over-quarter thesis evolution memo.

Reads the prior bear case + recent earnings summaries + predictions
outcomes and narrates how the named failure modes held up to evidence
across the latest quarter. Not a fresh bear case — the *delta*.
"""

from __future__ import annotations

import json
from pathlib import Path

from ._shared import (
    Lens,
    LensContext,
    load_predictions,
    load_prior_bear_case,
    load_recent_summaries,
    sha8,
    summarize_predictions,
    thesis_block,
)

_PROMPT_THESIS_DRIFT = """You are a senior buy-side analyst writing the quarter-over-quarter thesis
evolution memo for {ticker}. This is NOT a fresh bear case; it is the
*update* between two points in time, narrating how the named failure modes
held up to evidence.

**The thesis (anchor):**
{thesis_block}

**Prior bear case (failure modes the analyst was tracking):**
{prior_bear_case}

**This quarter's earnings summary (newest first):**
{recent_summaries}

**Predictions outcomes since the prior bear case (mgmt commitments,
risk-factor materialization, prior bear-case hypotheses):**
{predictions}

Produce a 350-500 word memo with three sections:

## 1. Failure modes that got MORE probable
List 1-3 prior failure modes whose probability went UP this quarter. For
each: NAME the specific number / disclosure / data point that drove the
shift. No hand-waving.

## 2. Failure modes that got LESS probable (or refuted)
Same shape. NAME the refuting evidence. If a hypothesis was outright
refuted, say so plainly — it's data even when it's negative.

## 3. NEW failure mode this quarter surfaced
What's the new risk that wasn't on the radar last quarter? Be specific: a
mechanism, a leading indicator, a refutation criterion. If nothing new
surfaced, say "no new failure modes — thesis is converging."

Voice: senior analyst writing for themselves. Opinion-bearing, terse,
non-consensus where the data supports it. No restating the thesis verbatim;
the reader already knows it. NO restating the prior failure modes verbatim;
they're the input. Focus on the DELTA.
"""


def _ctx_thesis_drift(ticker: str | None, repo_root: Path) -> LensContext | None:
    if not ticker:
        return None
    ticker = ticker.upper()
    prior = load_prior_bear_case(ticker, repo_root)
    if prior is None or not (prior.content_md or prior.content_json):
        return None  # no prior bear case to drift against
    summaries = load_recent_summaries(ticker, repo_root, n=4)
    if not summaries:
        return None
    predictions = load_predictions(ticker, repo_root)
    return LensContext(
        ticker=ticker,
        template_kwargs={
            "ticker": ticker,
            "thesis_block": thesis_block(ticker, repo_root),
            "prior_bear_case": (
                (prior.content_md or json.dumps(prior.content_json or {}, indent=2))[:6000]
            ),
            "recent_summaries": "\n\n---\n\n".join(
                f"### {q}\n{txt[:3000]}" for q, txt in summaries
            ),
            "predictions": summarize_predictions(predictions),
        },
        cache_inputs=[
            ticker,
            prior.input_sha256,
            *(f"{q}:{sha8(txt)}" for q, txt in summaries),
            sha8(json.dumps(predictions, default=str, sort_keys=True)),
        ],
        source_doc_ids=[],
        parent_artifact_ids=[prior.id],
    )


LENS = Lens(
    name="thesis_drift_qoq",
    model="claude-sonnet-4-6",
    scope="ticker",
    prompt_template=_PROMPT_THESIS_DRIFT,
    build_context=_ctx_thesis_drift,
)
