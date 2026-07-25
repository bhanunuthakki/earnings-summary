"""P3 boilerplate-vs-substance classifier — Stage 2 LLM triage.

The ONLY LLM step in the P3 build (``docs/design/disclosure_change_build_stack.md``
§P3). Mirrors ``filings.metric_triage``'s shape (P1) deliberately — same
call → parse → degrade-honestly structure, same batching discipline:

* ONE batched call per ticker over every item that survives deterministic
  specificity scoring (``filings.specificity``) — the confidently-boilerplate
  and confidently-substantive extremes never reach here.
* The prompt carries event id + event type + heading + a DIFF HUNK ONLY —
  never a whole item body, section, or document. For ``item_reworded``
  events the hunk is already reduced to just the changed spans
  (``filings.specificity.extract_diff_hunk``); for ``item_added``/
  ``item_removed`` it is the (already <=500-char) stored excerpt, since
  there is nothing to diff against.
* Haiku-class model (``disclosure_item_specificity_triage`` purpose in
  ``llm.cli.LLM_MODELS``) — narrow, closed-vocabulary classification, not
  open-ended judgment.

Degrades honestly on any LLM failure (repo's silent-degradation-class rule):
a failed or unparseable call returns ``TriageOutcome(degraded=True)`` with an
EMPTY verdict map, never a fabricated ``boilerplate_update`` for every
candidate — the caller (``filings.boilerplate_classify``) must leave those
rows ``unclassified`` rather than guess a direction. Hard stops (budget cap /
missing CLI, per ``llm.cli.is_hard_stop``) propagate untouched so the caller
can fail the whole run loudly instead of mistaking a setup problem for a
triage miss.
"""

from __future__ import annotations

import logging
from enum import StrEnum
from pathlib import Path
from typing import cast

from pydantic import BaseModel, Field

from llm.cli import is_hard_stop
from llm.structured import call_llm_structured

log = logging.getLogger(__name__)

TRIAGE_PURPOSE = "disclosure_item_specificity_triage"


class BoilerplateVerdict(StrEnum):
    BOILERPLATE_UPDATE = "boilerplate_update"
    SUBSTANTIVE = "substantive"


class ItemVerdict(BaseModel):
    event_id: int
    verdict: BoilerplateVerdict
    confidence: float = Field(ge=0.0, le=1.0)
    rationale: str = Field(max_length=300)


class TriageOutcome(BaseModel):
    """Per-ticker triage result.

    ``degraded=True`` means the LLM call/parse failed and NO candidate was
    silently marked ``boilerplate_update`` — callers must treat a degraded
    outcome as "still unclassified" for every id in the batch.
    """

    ticker: str
    verdicts: dict[int, ItemVerdict] = Field(default_factory=dict[int, ItemVerdict])
    degraded: bool = False
    degrade_reason: str | None = None


#: (event_id, event_type, heading-or-subject, diff hunk / excerpt).
TriageCandidate = tuple[int, str, str, str]


def _build_prompt(ticker: str, candidates: list[TriageCandidate]) -> str:
    rows = [
        f"- id={event_id} | type: {event_type} | heading: {label!r}\n  hunk: {hunk}"
        for event_id, event_type, label, hunk in candidates
    ]
    listing = "\n".join(rows)
    return f"""You are classifying changes to {ticker}'s SEC filing disclosures (risk factors \
and/or MD&A) as either GENERIC BOILERPLATE or FIRM-SPECIFIC SUBSTANCE. You are given ONLY the \
changed text for each item (a diff hunk marked [WAS: ...] [NOW: ...], or the full new/removed \
item text when there is nothing to diff against) — never the whole filing.

For EACH item below, decide:
1. verdict: "boilerplate_update" if the change is generic legal/risk phrasing that could appear \
in almost any company's filing (rewording, reordering, standard risk-factor caveats, \
cross-reference housekeeping) with no new firm-specific information; "substantive" if it \
introduces or removes information specific to THIS company (a new risk driver, a named \
product/geography/customer/competitor, a quantified exposure, a change in circumstances).
2. confidence: your confidence in that verdict, 0.0-1.0.
3. rationale: one sentence.

Items:
{listing}

Return ONLY a JSON object: {{"<event_id>": {{"verdict": "...", "confidence": 0.0, "rationale": "..."}}, ...}}
Every id listed above MUST appear as a key exactly once, using its exact integer id as a string."""


def triage_events(
    ticker: str,
    candidates: list[TriageCandidate],
    *,
    db_path: Path | str | None = None,
) -> TriageOutcome:
    """ONE batched call over diff hunks for this ticker's ambiguous survivors.

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
                "event": "boilerplate_triage_failed_degrading",
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

    verdicts: dict[int, ItemVerdict] = {}
    dropped = 0
    for key, raw in cast("dict[str, object]", decoded).items():
        try:
            event_id = int(key)
        except (TypeError, ValueError):
            dropped += 1
            continue
        if not isinstance(raw, dict):
            dropped += 1
            continue
        row = cast("dict[str, object]", raw)
        try:
            verdicts[event_id] = ItemVerdict(
                event_id=event_id,
                verdict=BoilerplateVerdict(str(row.get("verdict"))),
                confidence=float(cast("float", row.get("confidence", 0.0))),
                rationale=str(row.get("rationale") or "")[:300],
            )
        except (ValueError, TypeError):
            dropped += 1
            continue
    if dropped:
        log.warning(
            {"event": "boilerplate_triage_rows_dropped", "ticker": ticker, "count": dropped}
        )
    expected_ids = {c[0] for c in candidates}
    missing = expected_ids - verdicts.keys()
    if missing:
        log.warning(
            {
                "event": "boilerplate_triage_missing_verdicts",
                "ticker": ticker,
                "missing": sorted(missing),
            }
        )
    return TriageOutcome(ticker=ticker, verdicts=verdicts)
