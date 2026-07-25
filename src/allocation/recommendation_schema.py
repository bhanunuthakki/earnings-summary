"""``IncrementalDollarRecommendation`` — the P0.4a governed-selection output
shape (PRD ``docs/design/personal_investment_partner_prd.md`` §7.4 structured
output).

This is the Pydantic BOUNDARY model between the LLM's raw JSON and everything
downstream (persistence, routes, decision-linkage): ``model_validate`` catches
a structurally-malformed payload; :meth:`IncrementalDollarRecommendation.
validate_against_frontier` catches a structurally-VALID payload whose numbers
or claims don't actually follow from the deterministic frontier it was shown
(PRD §7.4's governed-judgment gate) — an invented ticker, a probability the
model wasn't entitled to state, allocations that don't sum, etc. Both gates
must pass before ``allocation.recommendation_artifact.generate_recommendation``
will persist an ``selection_mode="llm"`` artifact; either failing routes to
the deterministic fallback (never a half-validated artifact).

No LLM call lives in this module — it is pure schema + validation, exercised
directly by tests with hand-built payloads.
"""

from __future__ import annotations

import math
import re
from typing import Literal

from pydantic import BaseModel, Field

from allocation.recommendation import DeterministicFrontier, format_allocation_citation

__all__ = [
    "IncrementalDollarRecommendation",
    "PlannedAllocation",
    "RecommendationPlan",
]

# A bare "N% ... probability|chance|odds" (either order) reads as a fabricated
# numeric probability unless "base rate" appears nearby — PRD §7.4's humility
# gate ("we do not confirm numeric probabilities" outside a cited base rate).
_PCT_RX = re.compile(r"\d+(?:\.\d+)?\s*%")
_PROBABILITY_WORD_RX = re.compile(r"\b(probability|chance|odds)\b", re.IGNORECASE)
_BASE_RATE_RX = re.compile(r"\bbase rate\b", re.IGNORECASE)

# A dollar-rounding tolerance for the allocated+retained == cash check.
_CASH_EPS = 0.01
# Tolerance for re-derived resulting-weight math (percentage points).
_WEIGHT_EPS_PP = 0.1
# Any allocation reaching this weight must carry a zone (mirrors
# allocation.concentration's exceptional-zone floor; see concentration.py).
_ZONE_REQUIRED_PCT = 12.0


class PlannedAllocation(BaseModel):
    """One name's slice of a proposed plan (mirrors
    ``allocation.recommendation.PlanAllocation`` but is the LLM-facing,
    independently-validated shape — never trust-passed from the frontier)."""

    ticker: str = Field(min_length=1)
    dollars: float = Field(ge=0.0)
    pct_of_cash: float = Field(ge=0.0, le=100.0)
    resulting_weight_pct: float = Field(ge=0.0, le=100.0)
    zone: str | None = None


class RecommendationPlan(BaseModel):
    """One named plan (preferred / best_alternative / best_diversifier)."""

    allocations: list[PlannedAllocation] = Field(default_factory=list[PlannedAllocation])
    cash_retained_usd: float = Field(ge=0.0)


class IncrementalDollarRecommendation(BaseModel):
    """The full §7.4 structured output. See module docstring for the two-gate
    validation contract."""

    as_of_date: str = Field(min_length=1)
    input_sha: str = Field(min_length=1)
    status: Literal["deploy_all", "deploy_partial", "retain_all", "defer"]
    preferred_plan: RecommendationPlan
    best_alternative: RecommendationPlan | None = None
    best_diversifier: RecommendationPlan | None = None
    central_hypothesis: str = Field(min_length=1)
    personalization_why: str = Field(min_length=1)
    supporting_evidence: list[str] = Field(default_factory=list[str])
    main_unknowns: list[str] = Field(default_factory=list[str])
    disconfirming_evidence: list[str] = Field(default_factory=list[str])
    scenario_reasoning: str = ""
    confidence_verbal: Literal["low", "moderate", "high"]
    confidence_basis: str = Field(min_length=1)
    followup_research: list[str] = Field(default_factory=list[str])
    frontier_plan_ids: list[str] = Field(default_factory=list[str])
    source_refs: list[str] = Field(default_factory=list[str])
    risk_snapshot_ref: str | None = None
    engine_version: str = "v1"
    prompt_version: str = "v1"
    selection_mode: Literal["llm", "deterministic_fallback"]

    # ------------------------------------------------------------------
    # §7.4 governed-judgment gate — checked against the DeterministicFrontier
    # this recommendation was generated FROM. Returns a list of human-
    # readable reasons; empty means "passes the gate". Never raises — the
    # caller feeds a non-empty result back into ONE corrective retry, then
    # falls back deterministically.
    # ------------------------------------------------------------------

    def validate_against_frontier(
        self, frontier: DeterministicFrontier, cash_usd: float
    ) -> list[str]:
        reasons: list[str] = []
        eligible = set(frontier.eligible_tickers)
        held_names = {
            a.ticker for plan in frontier.plans for a in plan.allocations
        }  # names the frontier itself already sized (covers a currently-held
        # name the eligibility gate scored elsewhere in the same run)
        allowed_tickers = eligible | held_names

        # ticker -> [(dollars, resulting_weight_pct), ...] across every frontier
        # plan — reused (not re-derived) to check a claimed resulting_weight_pct
        # against the frontier's own math for the SAME dollar amount at that
        # ticker (§7.4: "resulting weights recompute correctly against the
        # frontier's math"). A different dollar amount naturally implies a
        # different weight, so only an exact-dollar match is checked here —
        # this deliberately reuses the frontier's numbers rather than
        # duplicating its cash-funded-weight formula.
        frontier_by_ticker: dict[str, list[tuple[float, float]]] = {}
        for fplan in frontier.plans:
            for a in fplan.allocations:
                frontier_by_ticker.setdefault(a.ticker, []).append(
                    (a.dollars, a.resulting_weight_pct)
                )

        for label, plan in (
            ("preferred_plan", self.preferred_plan),
            ("best_alternative", self.best_alternative),
            ("best_diversifier", self.best_diversifier),
        ):
            if plan is None:
                continue
            reasons.extend(
                self._validate_plan(label, plan, cash_usd, allowed_tickers, frontier_by_ticker)
            )

        if not self.main_unknowns:
            reasons.append("main_unknowns is empty — the humility fields must never be empty")
        if not self.disconfirming_evidence:
            reasons.append(
                "disconfirming_evidence is empty — the humility fields must never be empty"
            )
        if not self.confidence_basis.strip():
            reasons.append("confidence_basis is empty — the humility fields must never be empty")

        reasons.extend(
            self._check_numeric_probability("central_hypothesis", self.central_hypothesis)
        )
        reasons.extend(self._check_numeric_probability("confidence_basis", self.confidence_basis))

        allowed_refs = (
            set(frontier.source_freshness)
            | {fact for plan in frontier.plans for fact in plan.rationale_facts}
            | {format_allocation_citation(a) for plan in frontier.plans for a in plan.allocations}
        )
        for ref in self.source_refs:
            # A source_ref is either a literal source_freshness key (e.g.
            # "dcf", "weights"), one of the frontier's own rationale facts, or
            # the exact per-ticker allocation line the prompt showed the LLM
            # (``format_allocation_citation`` — the single source of truth
            # shared with the prompt renderer, so a verbatim citation of what
            # was actually displayed can never be rejected as invented).
            if ref in allowed_refs:
                continue
            if any(ref in fact for fact in allowed_refs):
                continue
            reasons.append(f"source_refs cites {ref!r}, not present in the frontier's own facts")

        return reasons

    def _validate_plan(
        self,
        label: str,
        plan: RecommendationPlan,
        cash_usd: float,
        allowed_tickers: set[str],
        frontier_by_ticker: dict[str, list[tuple[float, float]]],
    ) -> list[str]:
        reasons: list[str] = []
        total_allocated = 0.0
        for alloc in plan.allocations:
            if alloc.dollars < 0 or not _is_finite(alloc.dollars):
                reasons.append(f"{label}: {alloc.ticker} dollars is not finite/non-negative")
            if not _is_finite(alloc.resulting_weight_pct):
                reasons.append(f"{label}: {alloc.ticker} resulting_weight_pct is not finite")
            if alloc.ticker.upper() not in allowed_tickers:
                reasons.append(
                    f"{label}: {alloc.ticker} is not an eligible or already-sized frontier ticker"
                )
            if alloc.resulting_weight_pct >= _ZONE_REQUIRED_PCT and not alloc.zone:
                reasons.append(
                    f"{label}: {alloc.ticker} reaches {alloc.resulting_weight_pct:.1f}% "
                    f"(>= {_ZONE_REQUIRED_PCT:g}%) but carries no concentration zone"
                )
            for f_dollars, f_weight in frontier_by_ticker.get(alloc.ticker, []):
                if abs(f_dollars - alloc.dollars) <= _CASH_EPS:
                    if abs(f_weight - alloc.resulting_weight_pct) > _WEIGHT_EPS_PP:
                        reasons.append(
                            f"{label}: {alloc.ticker} claims resulting_weight_pct="
                            f"{alloc.resulting_weight_pct:.2f}% for ${alloc.dollars:,.2f}, but "
                            f"the frontier computed {f_weight:.2f}% for the same dollar amount"
                        )
                    break
            total_allocated += alloc.dollars

        combined = round(total_allocated + plan.cash_retained_usd, 2)
        if abs(combined - round(cash_usd, 2)) > _CASH_EPS:
            reasons.append(
                f"{label}: allocated (${total_allocated:,.2f}) + retained "
                f"(${plan.cash_retained_usd:,.2f}) = ${combined:,.2f} != cash ${cash_usd:,.2f}"
            )
        return reasons

    @staticmethod
    def _check_numeric_probability(field_name: str, text: str) -> list[str]:
        if not text:
            return []
        if _BASE_RATE_RX.search(text):
            return []
        if _PCT_RX.search(text) and _PROBABILITY_WORD_RX.search(text):
            return [
                f"{field_name} states a numeric probability without citing a base rate "
                "(PRD §7.4: we do not confirm numeric probabilities)"
            ]
        return []


def _is_finite(value: float) -> bool:
    return math.isfinite(value)
