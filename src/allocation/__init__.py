"""Next-dollar allocation model — quantitative "where the next dollar goes".

Blends DCF fair-value upside, marginal-volatility diversification off a
Ledoit–Wolf shrunk covariance of daily returns, and a macro sentiment tilt
(betas × series momentum) into a softmax allocation distribution across the
portfolio holdings. Inspectable by construction: visible blend weights, a
per-holding factor waterfall, hide-don't-stub for factors without data.

Model documentation: directives/next_dollar_model.md.

Public surface:
  build_next_dollar_model — score the holdings, return the distribution.
  NextDollarModel / HoldingScore / FactorReading — the result shape.
  BLEND_WEIGHTS / FACTOR_LABELS — the visible blend.

Also re-exports ``allocation.concentration`` — the PRD §7.2 (P0.2) soft
concentration-zone policy shared by the position-review service — and, for
P0.3 (PRD §7.3/§7.4), ``allocation.eligibility`` (the deterministic
decision-ready gate) and ``allocation.recommendation`` (the deterministic
Incremental Dollar frontier composed over it). Both P0.3 modules are LLM-free;
the governed selection over the frontier is P0.4.
"""

from __future__ import annotations

from allocation.concentration import (
    ENTRY_APPRECIATION,
    ENTRY_INTENTIONAL,
    TRIM_ASSESSMENT_THRESHOLD_PCT,
    ZONE_BOUNDS,
    Zone,
    ZoneAssessment,
    classify_entry_method,
    classify_zone,
    zone_at_least,
)
from allocation.eligibility import (
    CHECK_CANDIDATE_FIT,
    CHECK_DIRECTIONAL_HYPOTHESIS,
    CHECK_DISCONFIRMERS,
    CHECK_KPI_COVERAGE,
    CHECK_PORTFOLIO_CONTEXT,
    CHECK_PRICE_FRESHNESS,
    CHECK_SOURCE_PROVENANCE,
    CHECK_USABLE_DCF,
    DecisionReadyAssessment,
    EligibilityCheck,
    assess_eligibility,
    assess_universe,
    cash_assessment,
)
from allocation.model import (
    BLEND_WEIGHTS,
    FACTOR_LABELS,
    FactorReading,
    HoldingScore,
    NextDollarModel,
    build_next_dollar_model,
)
from allocation.recommendation import (
    DeterministicFrontier,
    FrontierPlan,
    PlanAllocation,
    build_frontier,
)

__all__ = [
    "BLEND_WEIGHTS",
    "CHECK_CANDIDATE_FIT",
    "CHECK_DIRECTIONAL_HYPOTHESIS",
    "CHECK_DISCONFIRMERS",
    "CHECK_KPI_COVERAGE",
    "CHECK_PORTFOLIO_CONTEXT",
    "CHECK_PRICE_FRESHNESS",
    "CHECK_SOURCE_PROVENANCE",
    "CHECK_USABLE_DCF",
    "ENTRY_APPRECIATION",
    "ENTRY_INTENTIONAL",
    "FACTOR_LABELS",
    "TRIM_ASSESSMENT_THRESHOLD_PCT",
    "ZONE_BOUNDS",
    "DecisionReadyAssessment",
    "DeterministicFrontier",
    "EligibilityCheck",
    "FactorReading",
    "FrontierPlan",
    "HoldingScore",
    "NextDollarModel",
    "PlanAllocation",
    "Zone",
    "ZoneAssessment",
    "assess_eligibility",
    "assess_universe",
    "build_frontier",
    "build_next_dollar_model",
    "cash_assessment",
    "classify_entry_method",
    "classify_zone",
    "zone_at_least",
]
