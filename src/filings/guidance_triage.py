"""D2.1 guidance-withdrawal detector — Stage 2/3 LLM triage.

Mirrors ``filings.metric_triage``'s Stage 2 shape exactly (one batched call
per ticker, names/labels ONLY, never a document, Haiku-class model) but asks
a DIFFERENT first question, because the two lanes' raw candidates conflate
"reads as forward guidance" with things that merely CONTAIN guidance-shaped
words:

* Lane A (``management_commitments`` own-cadence) candidates are already
  built from a KPI-target feed, so the relevance question is closer to moot —
  but is still asked, because a stray commitment about an unrelated ad hoc
  metric management floated once is not the same as an established guidance
  PRACTICE going quiet.
* Lane B (MD&A "Outlook"/"Guidance" heading own-cadence) candidates are, per
  ``docs/design/disclosure_gap_scoping.md`` Gap 1 and the real corpus check
  logged in ``filings.guidance_lifecycle``'s module docstring, frequently
  generic industry commentary ("Market Outlook") or accounting-standard
  boilerplate that the deterministic ``_ACCOUNTING_GUIDANCE_RX`` filter
  cannot fully disambiguate from actual forward operating/financial guidance
  language — the LLM step is what the scoping doc calls "a small triage for
  what counts as a guidance statement".

``LifecyclePrior`` (concealment / maturity / unclear) is REUSED verbatim from
``filings.metric_triage`` rather than redefined — the concealment-vs-maturity
question is identical in spirit for both subject families (Chen, Matsumoto &
Rajgopal's -4.8% CAR for guidance stoppers vs. Call, Melessa & Volant's ~40%
COVID-era stoppers that never restarted and subsequently outperformed is the
SAME "do not encode stopped=bearish" tension P1's ``metric_triage`` already
navigates for XBRL tags).

Degrades honestly on any LLM failure, matching ``metric_triage``: a failed or
unparseable call returns ``GuidanceTriageOutcome(degraded=True)`` with an
EMPTY verdict map, never a fabricated verdict for every candidate. Hard stops
(budget cap / missing CLI) propagate untouched.
"""

from __future__ import annotations

import logging
from enum import StrEnum
from pathlib import Path
from typing import cast

from pydantic import BaseModel, Field, TypeAdapter

from filings.guidance_lifecycle import GuidanceCandidate
from filings.metric_triage import LifecyclePrior
from llm.cli import is_hard_stop
from llm.structured import call_llm_structured

log = logging.getLogger(__name__)

GUIDANCE_TRIAGE_PURPOSE = "guidance_lifecycle_triage"


class GuidanceRelevance(StrEnum):
    """Lane B's core disambiguator: does this heading actually read as
    forward operating/financial guidance, or is it industry commentary /
    accounting-standard boilerplate that merely contains a guidance-shaped
    word? Lane A candidates are pre-filtered by construction (they come from
    a KPI-target feed) but are still asked this question for consistency —
    a stray, non-recurring commitment about an ad hoc metric is
    ``NOT_GUIDANCE`` even on Lane A."""

    FORWARD_GUIDANCE = "forward_guidance"
    NOT_GUIDANCE = "not_guidance"


class GuidanceTriageVerdict(BaseModel):
    subject_key: str
    relevance: GuidanceRelevance
    prior: LifecyclePrior
    rationale: str = Field(max_length=500)


class _GuidanceVerdictWire(BaseModel):
    relevance: GuidanceRelevance
    prior: LifecyclePrior
    rationale: str = Field(max_length=500)


_GUIDANCE_TRIAGE_ADAPTER = TypeAdapter(dict[str, _GuidanceVerdictWire])


class GuidanceTriageOutcome(BaseModel):
    ticker: str
    verdicts: dict[str, GuidanceTriageVerdict] = Field(
        default_factory=dict["str", "GuidanceTriageVerdict"]
    )
    degraded: bool = False
    degrade_reason: str | None = None


def _build_prompt(ticker: str, candidates: list[GuidanceCandidate]) -> str:
    rows = [
        f"- key: {c.subject_key!r} | lane: {c.lane} | description: {c.subject_label!r} | "
        f"kind: {c.kind} | last present: {c.last_present_period} | silent for "
        f"{c.current_silence} period(s), beyond its own historical tolerance of "
        f"{c.historical_max_gap}"
        for c in candidates
    ]
    listing = "\n".join(rows)
    return f"""You are triaging candidates where {ticker} appears to have stopped (or, for \
"guidance_resumed", resumed) a recurring forward-looking disclosure practice, to separate \
genuine forward operating/financial guidance from things that merely LOOK like guidance. \
You are given ONLY a short description and a silence/resumption pattern — no filing text \
or transcript excerpts.

For EACH candidate below, decide:
1. relevance: "forward_guidance" if the description plausibly refers to management's OWN \
forward-looking operating or financial guidance (a revenue/margin/EPS target, a growth-rate \
commitment, a capex or unit target for a future period) vs "not_guidance" if it more plausibly \
refers to generic industry/market commentary (e.g. "Market Outlook" describing sector \
conditions, not a company target), or to an ACCOUNTING-STANDARDS footnote (e.g. "Recently \
Adopted Accounting Guidance" — this is NOT forward guidance, it is FASB/ASU/ASC pronouncement \
boilerplate).
2. prior: your best guess whether this change looks more like "concealment" (hiding a \
deteriorating outlook), "maturity" (the company outgrew this practice or folded it into \
something broader — some issuers deliberately stop point-guidance as their business matures \
and are fine afterward), or "unclear" (impossible to judge from this description alone). \
"unclear" is the honest default for almost every "not_guidance" candidate, and is often the \
honest answer for real forward_guidance candidates too -- you cannot see WHY from this data \
alone. Do not force a directional guess you cannot support. For "guidance_resumed" candidates, \
prior should almost always be "unclear" (a resumption is not itself concealment or maturity).
3. rationale: one sentence.

Candidates:
{listing}

Return ONLY a JSON object: {{"<key>": {{"relevance": "...", "prior": "...", "rationale": "..."}}, ...}}
Every candidate above MUST appear as a key exactly once, using its exact key value."""


def triage_guidance_candidates(
    ticker: str,
    candidates: list[GuidanceCandidate],
    *,
    db_path: Path | str | None = None,
) -> GuidanceTriageOutcome:
    """ONE batched call over subject key/description/silence-pattern for this
    ticker, across BOTH lanes together (never a document, never transcript
    text). Raises whatever ``call_llm_structured`` raises when it is a hard
    stop (budget cap / missing CLI, per ``is_hard_stop``) — those propagate
    so the caller fails the run loudly rather than mistaking a setup problem
    for a triage miss. Every other failure degrades to
    ``GuidanceTriageOutcome(degraded=True)``."""
    if not candidates:
        return GuidanceTriageOutcome(ticker=ticker)
    prompt = _build_prompt(ticker, candidates)
    try:
        decoded = call_llm_structured(
            prompt,
            purpose=GUIDANCE_TRIAGE_PURPOSE,
            ticker=ticker,
            expect="object",
            schema=_GUIDANCE_TRIAGE_ADAPTER,
            db_path=db_path,
        )
    except Exception as exc:
        if is_hard_stop(exc):
            raise
        log.error(
            {
                "event": "guidance_triage_failed_degrading",
                "ticker": ticker,
                "n_candidates": len(candidates),
                "error": f"{type(exc).__name__}: {exc}",
            }
        )
        return GuidanceTriageOutcome(
            ticker=ticker,
            degraded=True,
            degrade_reason=f"{type(exc).__name__}: {str(exc)[:300]}",
        )

    verdicts: dict[str, GuidanceTriageVerdict] = {}
    dropped = 0
    for key, raw in cast("dict[str, object]", decoded).items():
        try:
            row = _GuidanceVerdictWire.model_validate(raw)
            verdicts[key] = GuidanceTriageVerdict(
                subject_key=key,
                relevance=row.relevance,
                prior=row.prior,
                rationale=row.rationale,
            )
        except (ValueError, TypeError):
            dropped += 1
            continue
    if dropped:
        log.warning({"event": "guidance_triage_rows_dropped", "ticker": ticker, "count": dropped})
    missing = {c.subject_key for c in candidates} - verdicts.keys()
    if missing:
        log.warning(
            {
                "event": "guidance_triage_missing_verdicts",
                "ticker": ticker,
                "missing": sorted(missing),
            }
        )
    return GuidanceTriageOutcome(ticker=ticker, verdicts=verdicts)
