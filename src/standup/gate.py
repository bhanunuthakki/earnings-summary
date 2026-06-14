"""The eval / relevance gate — silence is the default for a borderline brief.

An unprompted, miscalibrated advisor is worse than silence, so a composed brief
clears two bars before it is delivered:

  * the L3 ``ask_advisory_answer`` rubric (grounding-correctness, risk/reward
    balance, calibration-vs-evidence, follow-up usefulness), scored by the same
    ``evals.rubric_judge.judge_item`` that audits real Ask turns — the brief is
    audited exactly as a production answer would be; and
  * a standup ``min_score`` floor that sits at or above the rubric's own pass
    threshold, so borderline-passing briefs still stay quiet.

The judge call is injected (``caller``) so tests never spend; production leaves
the default ``call_llm``. A hard stop (budget / setup) propagates out of
``judge_item`` as ``EvalAbortError`` — the run aborts loudly rather than
silently suppressing every brief. A transient judge failure fails closed:
``judge_item`` returns a non-passing result, and the brief is suppressed (never
delivered ungraded).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import cast
from uuid import uuid4

from evals.corpora import AuditItem
from evals.rubric_judge import LlmCaller, Rubric, judge_item
from llm.cli import call_llm
from standup.compose import ComposedMessage
from standup.signals import StandupSignal


@dataclass(frozen=True, slots=True)
class GateOutcome:
    """The verdict on one composed brief."""

    passed: bool
    score: float
    rationale: str
    facet_scores: dict[str, float] = field(default_factory=dict[str, float])


def _format_citations(citations: list[dict[str, object]]) -> str:
    """Render the brief's citations as the numbered ground truth the grounding
    facet checks against — mirrors ``evals.corpora._format_ask_citations`` (the
    [n] label (conf XX%) shape) so the standup is judged on the same evidence
    rendering as a replayed ask turn."""
    if not citations:
        return "(no sources cited)"
    lines: list[str] = []
    for item in citations:
        n = item.get("n")
        label = str(item.get("label") or item.get("kind") or "source")
        conf = item.get("confidence")
        marker = f"[{n}]" if isinstance(n, int) else "-"
        conf_str = (
            f" (conf {round(float(conf) * 100)}%)"
            if isinstance(conf, (int, float)) and not isinstance(conf, bool)
            else ""
        )
        lines.append(f"{marker} {label}{conf_str}")
    return "\n".join(lines) if lines else "(no sources cited)"


def format_audit_content(composed: ComposedMessage) -> str:
    """Assemble the three-block content the rubric pins — the framing as the
    conversation context, the brief as the answer under audit, the citations as
    the evidence — identical in shape to ``corpora._format_ask_item_content``."""
    return (
        "Conversation scope: portfolio (proactive standup)\n\n"
        "=== CONVERSATION SO FAR (context only — do NOT grade) ===\n"
        f"[USER] {composed.question.strip()}\n\n"
        "=== ANSWER UNDER AUDIT (grade THIS) ===\n"
        f"{composed.answer.strip()}\n\n"
        "=== EVIDENCE THE ANSWER CITED (the only ground truth available) ===\n"
        f"{_format_citations(composed.citations)}"
    )


def gate_message(
    signal: StandupSignal,
    composed: ComposedMessage,
    *,
    rubric: Rubric,
    min_score: float,
    caller: LlmCaller = call_llm,
    run_id: str | None = None,
) -> GateOutcome:
    """Score the composed brief against the rubric and apply the standup floor.

    Delivered iff the judge passed it (overall >= rubric threshold) AND the
    overall clears ``min_score``. Raises ``EvalAbortError`` on a judge hard stop
    (propagated from ``judge_item``)."""
    item = AuditItem(
        item_id=f"standup:{signal.kind}:{signal.signature}",
        label=f"standup/{signal.kind}" + (f" ({signal.ticker})" if signal.ticker else " (book)"),
        ticker=signal.ticker,
        content=format_audit_content(composed),
    )
    result = judge_item(rubric, item, run_id=run_id or uuid4().hex, caller=caller)
    facet_scores: dict[str, float] = {}
    if result.actual_json:
        try:
            parsed: object = json.loads(result.actual_json)
            raw = (
                cast("dict[str, object]", parsed).get("facet_scores")
                if isinstance(parsed, dict)
                else None
            )
            if isinstance(raw, dict):
                facet_scores = {
                    str(k): float(cast("float", v))
                    for k, v in cast("dict[str, object]", raw).items()
                }
        except (ValueError, TypeError):
            facet_scores = {}
    passed = bool(result.passed) and result.score >= min_score
    return GateOutcome(
        passed=passed,
        score=result.score,
        rationale=result.judge_rationale or "",
        facet_scores=facet_scores,
    )


__all__ = ["GateOutcome", "format_audit_content", "gate_message"]
