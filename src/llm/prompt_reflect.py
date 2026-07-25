"""Reflective prompt mutation — P2 of the LLM Quality Program
(directives/llm_quality_program_2026_07.md), the GEPA-shaped successor to the
§4.7 edit-splice proposer.

Why this replaces edit-splicing. The July loop proposed 1-4 exact-substring
edits drawn from a hand-written 11-strategy menu, against a scaffold
reverse-engineered from captured renders. Two ceilings: the mutation space is
whatever the menu enumerates, and every candidate is anchor-fragile. GEPA
(ICLR 2026 oral, arXiv:2507.19457) shows the stronger operator is *reflective*
— read the judged failures, diagnose them in natural language, and rewrite the
whole instruction — reporting ~10%+ over MIPROv2 at 35x fewer rollouts than
RL. It needs exactly two things this platform already has: a per-case judged
metric (the brand-blind pairwise judge + rubric facets) and versioned prompts
(P0's registry).

What this module owns:

* ``reflect_and_rewrite`` — one mutation: (template body + judged failure
  evidence + optional direction) -> (diagnosis, revised body). The revision is
  a WHOLE template body, so there are no anchors to drift and no menu to be
  limited by. The old strategy taxonomy survives only as optional steering
  vocabulary passed in ``direction``.
* ``ParetoFrontier`` — GEPA's second half. Candidates are scored on (quality,
  cost) and the frontier keeps every non-dominated one, so the loop explores
  from a diverse set of good parents rather than hill-climbing a single line.
  A cheaper-and-equal candidate belongs on the frontier even when a pricier
  one scores higher; that is the whole point of Pareto rather than argmax.

Safety contracts kept from §4:
* Candidates are REGISTRY VERSIONS (``PromptTemplate`` bodies), so a promoted
  candidate is pinned by version — byte-stable, trivially reconciled to git.
* A rewrite that changes the template's VARIABLE SET is rejected: the call
  site passes a fixed set of variables, so a body that invents or drops a slot
  cannot render and would fail at spend time. Validated before any spend.
* Proposing is steering, never load-bearing: every failure path returns None.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import cast

from llm.prompt_registry import PromptTemplate, body_slots

log = logging.getLogger(__name__)

REFLECT_PURPOSE = "prompt_reflect_rewrite"

# Guardrails on the rewritten body. A revision that collapses the prompt to a
# stub or balloons it 3x is far more likely a model failure than an insight;
# both are rejected pre-spend rather than measured expensively.
MIN_BODY_RATIO = 0.5
MAX_BODY_RATIO = 2.0

REFLECT_PROMPT = """\
You are improving one production LLM prompt by REWRITING it, based on evidence
of how its current version actually failed.

Purpose: "{purpose}".

Below are (1) the CURRENT prompt template, (2) JUDGED FAILURE EVIDENCE — real
cases where this prompt's output lost to an alternative or missed a rubric
facet, with the judge's own reasoning, and (3) the direction to explore.

Your job: diagnose WHY the current template produced those failures, then emit
a revised template that fixes the diagnosis.

HARD RULES — a violation makes your rewrite unusable:
- The revised template MUST contain exactly these variable slots, spelled
  identically, each at least once: {slot_list}
  These are filled with per-request data at call time. Do not invent new
  slots, do not drop any, do not rename any.
- Any literal brace that is NOT one of those slots must be DOUBLED ({{ and }}),
  because the template is rendered with str.format. JSON examples in the
  prompt therefore keep their doubled braces.
- Preserve WHAT is asked: same task, same output contract, same consumer. You
  are changing HOW the instruction gets there, not what it produces.
- Keep the revision within roughly half to twice the current length.

Respond with ONLY a JSON object:
{{"diagnosis": "<2-3 sentences: the specific mechanism by which the current
wording produced the observed failures>",
 "revised_template": "<the complete revised template body>",
 "expected_effect": "<which facet/criterion should move, and why>"}}

=== CURRENT TEMPLATE ===
{body}

=== JUDGED FAILURE EVIDENCE ===
{evidence}

=== DIRECTION FOR THIS REWRITE ===
{direction}
"""

StructCall = Callable[..., object]


@dataclass(frozen=True, slots=True)
class Rewrite:
    """One validated candidate template body plus the reasoning that produced
    it. ``template`` is constructed (and therefore slot-validated) already."""

    diagnosis: str
    template: PromptTemplate
    expected_effect: str
    parent_version: str


def _validate_body(base: PromptTemplate, body: str) -> tuple[bool, str]:
    """Cheap pre-spend checks. Returns (ok, reason-if-not)."""
    if not body.strip():
        return False, "empty body"
    ratio = len(body) / max(1, len(base.body))
    if not (MIN_BODY_RATIO <= ratio <= MAX_BODY_RATIO):
        return False, f"length ratio {ratio:.2f} outside [{MIN_BODY_RATIO}, {MAX_BODY_RATIO}]"
    try:
        slots = body_slots(body)
    except Exception as exc:  # malformed braces surface as a parse error
        return False, f"unparseable braces ({type(exc).__name__})"
    declared = set(base.variables)
    if slots != declared:
        return False, (
            f"variable set changed (missing {sorted(declared - slots)}, "
            f"added {sorted(slots - declared)})"
        )
    return True, ""


def reflect_and_rewrite(
    base: PromptTemplate,
    *,
    purpose: str,
    evidence: str,
    direction: str = "No fixed direction — attack the strongest failure in the evidence.",
    struct: StructCall | None = None,
) -> Rewrite | None:
    """One reflective mutation. Returns None on ANY failure — a mutation that
    cannot be validated is not worth spending judged cases on, and proposing is
    steering, never load-bearing."""
    struct_fn: StructCall
    if struct is None:
        from llm.structured import call_llm_structured

        struct_fn = call_llm_structured
    else:
        struct_fn = struct
    try:
        payload = struct_fn(
            REFLECT_PROMPT.format(
                purpose=purpose,
                slot_list=", ".join(sorted(base.variables)) or "(none)",
                body=base.body[:14000],
                evidence=evidence[:8000],
                direction=direction[:1200],
            ),
            purpose=REFLECT_PURPOSE,
            scope="meta_eval",
            expect="object",
            required_keys=("diagnosis", "revised_template"),
        )
    except Exception as exc:
        log.warning("reflective rewrite failed (%s: %s)", type(exc).__name__, str(exc)[:200])
        return None
    if not isinstance(payload, dict):
        return None
    obj = cast("dict[str, object]", payload)
    diagnosis = obj.get("diagnosis")
    revised = obj.get("revised_template")
    expected = obj.get("expected_effect")
    if not isinstance(diagnosis, str) or not diagnosis.strip():
        return None
    if not isinstance(revised, str) or not revised.strip():
        return None

    ok, why = _validate_body(base, revised)
    if not ok:
        log.warning(
            {
                "event": "reflective_rewrite_rejected",
                "purpose": purpose,
                "reason": why,
                "note": "rejected BEFORE any judged spend",
            }
        )
        return None
    try:
        candidate = PromptTemplate(
            template_id=base.template_id,
            body=revised,
            variables=base.variables,
            description=base.description,
        )
    except ValueError as exc:  # slot/declaration mismatch the checks missed
        log.warning({"event": "reflective_rewrite_invalid_template", "error": str(exc)[:200]})
        return None
    if candidate.version == base.version:
        log.info({"event": "reflective_rewrite_noop", "purpose": purpose})
        return None
    return Rewrite(
        diagnosis=diagnosis.strip()[:600],
        template=candidate,
        expected_effect=(expected.strip()[:300] if isinstance(expected, str) else ""),
        parent_version=base.version,
    )


# ---------------------------------------------------------------------------
# Pareto frontier over (quality, cost)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Candidate:
    """One evaluated template version. ``quality`` is the judged win rate or
    rubric score in [0,1]; ``cost`` is mean output tokens (the lever a prompt
    actually controls — input size is dominated by the data region)."""

    version: str
    quality: float
    cost: float
    n_cases: int = 0

    def dominates(self, other: Candidate) -> bool:
        """Pareto domination: at least as good on BOTH axes and strictly
        better on one. (cost is minimised, quality maximised.)"""
        at_least_as_good = self.quality >= other.quality and self.cost <= other.cost
        strictly_better = self.quality > other.quality or self.cost < other.cost
        return at_least_as_good and strictly_better


class ParetoFrontier:
    """The non-dominated candidate set for one purpose.

    Why a frontier instead of "keep the best": a cheaper candidate at equal
    quality is a real win the argmax would discard, and keeping diverse good
    parents is what lets reflective evolution escape the local optimum a
    single hill-climb converges to (the GEPA result). Ties on both axes keep
    the INCUMBENT — churn has cost, so a new candidate must actually beat
    something to earn a slot.
    """

    def __init__(self, candidates: Sequence[Candidate] = ()) -> None:
        self._items: list[Candidate] = []
        for c in candidates:
            self.add(c)

    @property
    def candidates(self) -> tuple[Candidate, ...]:
        return tuple(self._items)

    def add(self, candidate: Candidate) -> bool:
        """Insert if non-dominated, evicting anything it dominates.
        Returns True when the frontier changed."""
        for existing in self._items:
            if existing.dominates(candidate) or (
                existing.quality == candidate.quality and existing.cost == candidate.cost
            ):
                return False
        survivors = [e for e in self._items if not candidate.dominates(e)]
        changed = len(survivors) != len(self._items) or candidate not in self._items
        survivors.append(candidate)
        self._items = sorted(survivors, key=lambda c: (-c.quality, c.cost))
        return changed

    def best(self) -> Candidate | None:
        """Highest quality, cheapest among ties — the promotion candidate."""
        return self._items[0] if self._items else None

    def draw_parent(self, rand: float) -> Candidate | None:
        """Pick a parent to mutate next, quality-weighted over the frontier.

        ``rand`` is a float in [0,1) supplied by the caller's SEEDED rng, so a
        cycle stays reproducible (the §4.7 replay contract).
        """
        if not self._items:
            return None
        weights = [max(c.quality, 0.01) for c in self._items]
        total = sum(weights)
        threshold = rand * total
        running = 0.0
        for cand, w in zip(self._items, weights, strict=True):
            running += w
            if running >= threshold:
                return cand
        return self._items[-1]
