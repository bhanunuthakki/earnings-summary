"""P1 discontinued-metric detector — Stage 2 LLM triage.

The ONLY LLM step in the P1 build (``docs/design/disclosure_change_signals.md``
§3 Stage 2). Token efficiency is a hard requirement here, enforced structurally:

* ONE batched call per ticker over every surviving candidate.
* The prompt carries tag name + label + last value + last period ONLY —
  NEVER a document, NEVER filing text. ~40 candidates is roughly 600 input
  tokens.
* Haiku-class model (``FAST_CLASSIFIER_MODEL`` via the ``metric_lifecycle_triage``
  purpose in ``llm.cli.LLM_MODELS``) — this is a closed, narrow classification,
  not open-ended judgment.

It answers exactly two questions per candidate:

1. **Relevance** — is this a business-meaningful metric an investor would
   notice losing (users, ARR, backlog, segment revenue, churn, take rate),
   or accounting plumbing (``AccruedVacationCurrent``)?
2. **Prior** — from the tag name/label/value ALONE (no filing text is
   available at this stage), does stopping this disclosure look more like
   *concealment* (hiding deterioration) or *maturity/reframing* (the metric
   stopped being the right KPI — Apple dropping unit sales is the canonical
   case, and it outperformed after)? Per
   ``docs/design/disclosure_change_signals.md`` §1.5, this repo does NOT
   encode "stopped reporting = bearish": Chen/Matsumoto/Rajgopal found -4.8%
   CAR for guidance stoppers, but Call/Melessa/Volant found ~40% of
   COVID-era stoppers never restarted and subsequently performed well. Most
   answers here should honestly be "unclear" — a name/label/value triple
   rarely settles the direction; that determination is Stage 3's job (full
   filing-text context), not this one's.

Degrades honestly on any LLM failure (repo's silent-degradation-class rule):
a failed or unparseable call returns ``TriageOutcome(degraded=True)`` with an
EMPTY verdict map, never a fabricated "accounting_plumbing" verdict for every
candidate. Hard stops (budget cap / missing CLI, per ``llm.cli.is_hard_stop``)
propagate untouched so the caller can fail the whole run loudly instead of
mistaking a setup problem for a triage miss.
"""

from __future__ import annotations

import logging
from enum import StrEnum
from pathlib import Path
from typing import cast

from pydantic import BaseModel, Field

from filings.metric_lifecycle import MetricCandidate
from llm.cli import is_hard_stop
from llm.structured import call_llm_structured

log = logging.getLogger(__name__)

TRIAGE_PURPOSE = "metric_lifecycle_triage"


class Relevance(StrEnum):
    BUSINESS_METRIC = "business_metric"
    ACCOUNTING_PLUMBING = "accounting_plumbing"


class LifecyclePrior(StrEnum):
    CONCEALMENT = "concealment"
    MATURITY = "maturity"
    UNCLEAR = "unclear"


class TriageVerdict(BaseModel):
    qualified_name: str
    relevance: Relevance
    prior: LifecyclePrior
    rationale: str = Field(max_length=500)


class TriageOutcome(BaseModel):
    """Per-ticker triage result.

    ``degraded=True`` means the LLM call/parse failed and NO candidate was
    silently marked plumbing — callers must treat a degraded outcome as
    "still unclassified", never as a pass/fail verdict on any candidate.
    """

    ticker: str
    verdicts: dict[str, TriageVerdict] = Field(default_factory=dict["str", "TriageVerdict"])
    degraded: bool = False
    degrade_reason: str | None = None


def _build_prompt(ticker: str, candidates: list[MetricCandidate]) -> str:
    rows = [
        f"- {c.qualified_name} | label: {c.label!r} | last value: {c.last_value:,.0f} "
        f"| last period: {c.last_period_label} | silent for {c.current_silence} "
        f"{c.axis.value} period(s), beyond its own historical tolerance of {c.historical_max_gap} "
        f"| materiality: {'unknown' if c.materiality is None else f'{c.materiality:.3f}'} "
        "(fraction of revenue/total assets, higher = bigger line item)"
        for c in candidates
    ]
    listing = "\n".join(rows)
    return f"""You are triaging XBRL disclosure tags that {ticker} has stopped reporting, \
to separate business-meaningful metrics from accounting plumbing. You are given ONLY the \
tag name, its label, its last reported value, its last period, and a materiality score \
(the last value as a fraction of revenue or total assets) — no filing text.

For EACH tag below, decide:
1. relevance: "business_metric" if an investor would notice losing this disclosure \
(examples: user/subscriber counts, ARR, backlog, segment revenue, churn, take rate, \
volume/GMV metrics) vs "accounting_plumbing" if it is a routine accounting line item \
nobody tracks as a KPI (examples: AccruedVacationCurrent, \
DeferredTaxLiabilitiesPropertyPlantAndEquipment).
2. prior: your best guess whether stopping this disclosure looks more like "concealment" \
(hiding a deteriorating metric), "maturity" (the company outgrew this KPI or folded it \
into a broader one -- the classic case is Apple dropping unit sales), or "unclear" \
(impossible to judge from name/label/value alone). "unclear" is the honest default for \
almost every accounting_plumbing tag, and is often the honest answer for business_metric \
tags too -- you cannot see WHY it stopped from this data alone. Do not force a directional \
guess you cannot support. Materiality does NOT by itself imply concealment or maturity -- a \
high-materiality tag going silent deserves a genuinely considered answer, not a reflexive \
"unclear" you would also give a trivial one; still answer "unclear" if the name/label/value \
alone truly cannot settle it, high materiality or not.
3. rationale: one sentence.

Tags (each is a candidate the company has stopped reporting beyond its own normal filing cadence):
{listing}

Return ONLY a JSON object: {{"<qualified_name>": {{"relevance": "...", "prior": "...", "rationale": "..."}}, ...}}
Every tag listed above MUST appear as a key exactly once, using its exact qualified_name."""


def triage_candidates(
    ticker: str,
    candidates: list[MetricCandidate],
    *,
    db_path: Path | str | None = None,
) -> TriageOutcome:
    """ONE batched call over tag names/labels/last-values for this ticker.

    Raises whatever ``call_llm``/``call_llm_structured`` raises when it is a
    hard stop (budget cap / missing CLI, per ``is_hard_stop``) — those must
    propagate so the caller fails the run loudly rather than mistaking a
    setup problem for a triage miss. Every other failure degrades to
    ``TriageOutcome(degraded=True)``.
    """
    if not candidates:
        return TriageOutcome(ticker=ticker)
    prompt = _build_prompt(ticker, candidates)
    try:
        decoded = call_llm_structured(
            prompt,
            purpose=TRIAGE_PURPOSE,
            ticker=ticker,
            expect="object",
            db_path=db_path,
        )
    except Exception as exc:
        if is_hard_stop(exc):
            raise
        log.error(
            {
                "event": "metric_triage_failed_degrading",
                "ticker": ticker,
                "n_candidates": len(candidates),
                "error": f"{type(exc).__name__}: {exc}",
            }
        )
        return TriageOutcome(
            ticker=ticker,
            degraded=True,
            degrade_reason=f"{type(exc).__name__}: {str(exc)[:300]}",
        )

    verdicts: dict[str, TriageVerdict] = {}
    dropped = 0
    for qname, raw in cast("dict[str, object]", decoded).items():
        if not isinstance(raw, dict):
            dropped += 1
            continue
        row = cast("dict[str, object]", raw)
        try:
            verdicts[qname] = TriageVerdict(
                qualified_name=qname,
                relevance=Relevance(str(row.get("relevance"))),
                prior=LifecyclePrior(str(row.get("prior"))),
                rationale=str(row.get("rationale") or "")[:500],
            )
        except (ValueError, TypeError):
            dropped += 1
            continue
    if dropped:
        log.warning({"event": "metric_triage_rows_dropped", "ticker": ticker, "count": dropped})
    missing = {c.qualified_name for c in candidates} - verdicts.keys()
    if missing:
        log.warning(
            {
                "event": "metric_triage_missing_verdicts",
                "ticker": ticker,
                "missing": sorted(missing),
            }
        )
    return TriageOutcome(ticker=ticker, verdicts=verdicts)
