"""Versioned qualitative common-drawdown regime playbook (PRD §6.1 P1-A, BHA-45).

Deterministic, typed, pure mathematical valuation and qualitative stress testing
across portfolio holdings and proposed allocation actions.

Outputs qualitative ratings: benefits, resilient, mixed, vulnerable, highly_vulnerable.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

REGIME_REGISTRY_VERSION = "2026-08-15.1"

QualitativeRating = Literal[
    "benefits",
    "resilient",
    "mixed",
    "vulnerable",
    "highly_vulnerable",
]

AvailabilityStatus = Literal["full", "partial", "unavailable"]
DirectionStatus = Literal[
    "increased_vulnerability",
    "decreased_vulnerability",
    "no_material_change",
]


class FactorImpact(BaseModel):
    """Impact mapping of a specific business factor under a macro regime."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    factor: str
    effect_ordinal: int = Field(
        ge=-2,
        le=2,
        description="-2 (highly vulnerable), -1 (vulnerable), 0 (mixed), +1 (resilient), +2 (benefits)",
    )
    confidence: float = Field(
        ge=0.0,
        le=1.0,
        description="Confidence multiplier fixed in the registry (0.0 to 1.0)",
    )
    transmission_mechanism: str


class RegimeDefinition(BaseModel):
    """Versioned specification of a macro/qualitative common-drawdown scenario."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    regime_id: str
    display_name: str
    description: str
    factors: list[FactorImpact]
    named_scenario_ids: list[str] = Field(default_factory=list)
    historical_analogs: list[str] = Field(default_factory=list)
    evidence_citations: list[str] = Field(default_factory=list)
    registry_version: str = REGIME_REGISTRY_VERSION
    priority: int = 100


class HoldingRegimeAssessment(BaseModel):
    """Assessment of an individual holding under a specific regime."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    ticker: str
    weight_pct: float
    raw_score: float
    rating: QualitativeRating
    applicable_factors: list[str]


class PortfolioRegimeAssessment(BaseModel):
    """Portfolio-wide assessment under a macro regime."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    regime_id: str
    regime_name: str
    raw_score: float
    rating: QualitativeRating
    coverage_pct: float
    availability: AvailabilityStatus
    covered_weight_pct: float
    total_non_cash_weight_pct: float
    excluded_tickers: list[str]
    holdings: list[HoldingRegimeAssessment]
    registry_version: str = REGIME_REGISTRY_VERSION


class ActionRegimeImpact(BaseModel):
    """Before-and-after assessment of a proposed portfolio allocation trade."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    regime_id: str
    regime_name: str
    before_score: float
    after_score: float
    score_delta: float
    before_rating: QualitativeRating
    after_rating: QualitativeRating
    direction: DirectionStatus
    material_change: bool
    registry_version: str = REGIME_REGISTRY_VERSION


# ---------------------------------------------------------------------------
# Initial 9 Curated Regimes (PRD §6.1 P1-A)
# ---------------------------------------------------------------------------

INITIAL_REGIMES: list[RegimeDefinition] = [
    RegimeDefinition(
        regime_id="demand_led_recession",
        display_name="Demand-Led Recession",
        description="Broad cyclical contraction in global consumer spending, advertising, and discretionary corporate budgets.",
        priority=1,
        named_scenario_ids=["recession", "cyclical_downturn"],
        historical_analogs=["2008 GFC", "2001 Tech Recession"],
        factors=[
            FactorImpact(
                factor="US consumer mobility/delivery",
                effect_ordinal=-2,
                confidence=0.9,
                transmission_mechanism="Discretionary ride-hailing and food delivery volumes contract with household belt-tightening.",
            ),
            FactorImpact(
                factor="Global travel demand",
                effect_ordinal=-2,
                confidence=0.95,
                transmission_mechanism="Leisure and business travel budgets face immediate cancellations and down-trading.",
            ),
            FactorImpact(
                factor="Digital advertising demand",
                effect_ordinal=-2,
                confidence=0.85,
                transmission_mechanism="Brand marketing budgets slashed rapidly by enterprises seeking immediate cash preservation.",
            ),
            FactorImpact(
                factor="US enterprise IT budgets",
                effect_ordinal=-1,
                confidence=0.8,
                transmission_mechanism="Software seat additions stall and procurement cycles extend.",
            ),
            FactorImpact(
                factor="SMB digital adoption",
                effect_ordinal=-2,
                confidence=0.85,
                transmission_mechanism="Higher SMB churn and business closures reduce subscription base.",
            ),
            FactorImpact(
                factor="Alternative-asset fee growth",
                effect_ordinal=-1,
                confidence=0.75,
                transmission_mechanism="M&A realizations slow, delaying performance fees despite resilient management fees.",
            ),
        ],
    ),
    RegimeDefinition(
        regime_id="rates_inflation_shock",
        display_name="Inflation & Long-Duration Rates Shock",
        description="Persistent inflation driving sustained central bank tightening and terminal rate spikes.",
        priority=2,
        named_scenario_ids=["rates_spike", "stagflation"],
        historical_analogs=["2022 Fed Rate Hike Cycle", "1970s Stagflation"],
        factors=[
            FactorImpact(
                factor="AI capex/data-volume",
                effect_ordinal=-1,
                confidence=0.8,
                transmission_mechanism="Long-duration DCF multiples compress as the cost of capital rises.",
            ),
            FactorImpact(
                factor="US enterprise IT budgets",
                effect_ordinal=-1,
                confidence=0.75,
                transmission_mechanism="High multiple cloud/SaaS multiples de-rate across equity markets.",
            ),
            FactorImpact(
                factor="SMB digital adoption",
                effect_ordinal=-1,
                confidence=0.8,
                transmission_mechanism="Higher borrowing costs squeeze SMB operating margins and capital spending.",
            ),
            FactorImpact(
                factor="Alternative-asset fee growth",
                effect_ordinal=1,
                confidence=0.7,
                transmission_mechanism="Private credit, real estate, and floating-rate infrastructure strategies benefit from higher yields.",
            ),
        ],
    ),
    RegimeDefinition(
        regime_id="sovereign_debt_funding_crisis",
        display_name="Sovereign-Debt or Funding Crisis",
        description="Spike in global sovereign bond yields and sharp widening in sovereign/corporate credit spreads.",
        priority=3,
        historical_analogs=["2011 Eurozone Debt Crisis"],
        factors=[
            FactorImpact(
                factor="Brazil consumer credit",
                effect_ordinal=-2,
                confidence=0.9,
                transmission_mechanism="Sovereign yield spikes force local central bank rate hikes and squeeze retail net interest margins.",
            ),
            FactorImpact(
                factor="LatAm consumer/FX",
                effect_ordinal=-2,
                confidence=0.9,
                transmission_mechanism="Capital flight sparks emerging market currency depreciation and domestic demand slump.",
            ),
            FactorImpact(
                factor="Alternative-asset fee growth",
                effect_ordinal=-2,
                confidence=0.85,
                transmission_mechanism="Debt capital markets freeze, hindering leveraged buyout originations and asset refinancings.",
            ),
        ],
    ),
    RegimeDefinition(
        regime_id="currency_crisis",
        display_name="Currency Crisis (LatAm Devaluation)",
        description="Accelerated emerging market currency depreciation relative to the USD, driving imported inflation.",
        priority=4,
        historical_analogs=["2015 BRL Devaluation", "2018 LatAm Currency Slump"],
        factors=[
            FactorImpact(
                factor="LatAm consumer/FX",
                effect_ordinal=-2,
                confidence=0.95,
                transmission_mechanism="USD-denominated earnings translation collapses while local purchasing power erodes.",
            ),
            FactorImpact(
                factor="Brazil consumer credit",
                effect_ordinal=-2,
                confidence=0.95,
                transmission_mechanism="Selic hikes to defend FX trigger higher default rates among leveraged consumer borrowers.",
            ),
            FactorImpact(
                factor="Mexico expansion",
                effect_ordinal=-1,
                confidence=0.85,
                transmission_mechanism="Cross-border infrastructure investments face FX drag and currency translation headwinds.",
            ),
        ],
    ),
    RegimeDefinition(
        regime_id="oil_supply_shock",
        display_name="Oil & Energy Supply Shock",
        description="Geopolitical disruption triggering crude oil and refined energy price surges.",
        priority=5,
        historical_analogs=["1973 OPEC Embargo", "2022 Energy Shock"],
        factors=[
            FactorImpact(
                factor="US consumer mobility/delivery",
                effect_ordinal=-2,
                confidence=0.9,
                transmission_mechanism="Fuel pump price surges squeeze gig-worker driver unit economics and consumer surcharges.",
            ),
            FactorImpact(
                factor="Global travel demand",
                effect_ordinal=-1,
                confidence=0.85,
                transmission_mechanism="Jet fuel price hikes pass through into airline ticket pricing, cooling leisure travel volume.",
            ),
        ],
    ),
    RegimeDefinition(
        regime_id="saas_multiple_compression",
        display_name="SaaS & Software Multiple Compression",
        description="Structural re-rating of enterprise software valuation multiples from peak historical sales multiples.",
        priority=6,
        named_scenario_ids=["software_rerating", "cloud_multiple_compression"],
        historical_analogs=["2022 Software Multiple Drawdown"],
        factors=[
            FactorImpact(
                factor="US enterprise IT budgets",
                effect_ordinal=-2,
                confidence=0.95,
                transmission_mechanism="Enterprise software enterprise value to NTM sales multiples compress severely.",
            ),
            FactorImpact(
                factor="SMB digital adoption",
                effect_ordinal=-2,
                confidence=0.9,
                transmission_mechanism="High-beta SMB web software platforms face acute valuation multiples deflation.",
            ),
            FactorImpact(
                factor="Life-sciences R&D/cloud",
                effect_ordinal=-1,
                confidence=0.85,
                transmission_mechanism="Vertical SaaS multiples pull back in sympathy with broader enterprise software peer group.",
            ),
            FactorImpact(
                factor="AI capex/data-volume",
                effect_ordinal=-1,
                confidence=0.75,
                transmission_mechanism="Data infrastructure multiples de-rate unless backed by direct GAAP cash flow conversion.",
            ),
        ],
    ),
    RegimeDefinition(
        regime_id="advertising_recession",
        display_name="Digital Advertising Recession",
        description="Severe pullback in performance marketing and digital brand advertising expenditure.",
        priority=7,
        historical_analogs=["2022 Digital Ad Spending Reset"],
        factors=[
            FactorImpact(
                factor="Digital advertising demand",
                effect_ordinal=-2,
                confidence=0.95,
                transmission_mechanism="Direct drop in blended CPMs, ad impressions monetization, and merchant ad budgets.",
            ),
            FactorImpact(
                factor="SMB digital adoption",
                effect_ordinal=-1,
                confidence=0.8,
                transmission_mechanism="E-commerce merchants cut acquisition spend, slowing merchant website additions.",
            ),
        ],
    ),
    RegimeDefinition(
        regime_id="latam_credit_stress",
        display_name="LatAm Consumer Credit Stress",
        description="Surge in non-performing loans (NPLs) and household credit delinquency across Brazil and Mexico.",
        priority=8,
        historical_analogs=["2015-2016 Brazil Recession"],
        factors=[
            FactorImpact(
                factor="Brazil consumer credit",
                effect_ordinal=-2,
                confidence=0.95,
                transmission_mechanism="Surging 90-day delinquency rates require aggressive loan-loss provisioning, eroding NIM.",
            ),
            FactorImpact(
                factor="LatAm consumer/FX",
                effect_ordinal=-2,
                confidence=0.9,
                transmission_mechanism="E-commerce credit default rates rise, forcing tighter merchant underwriting.",
            ),
            FactorImpact(
                factor="Mexico expansion",
                effect_ordinal=-1,
                confidence=0.8,
                transmission_mechanism="Unseasoned cohort credit losses force slowdown in customer acquisition pace.",
            ),
        ],
    ),
    RegimeDefinition(
        regime_id="glp1_pricing_pressure",
        display_name="GLP-1 Reimbursement & Pricing Reset",
        description="Government price negotiation, mandatory rebates, and supply saturation in metabolic therapeutics.",
        priority=9,
        historical_analogs=["2024 Medicare Price Negotiations"],
        factors=[
            FactorImpact(
                factor="GLP-1 reimbursement/supply",
                effect_ordinal=-2,
                confidence=0.95,
                transmission_mechanism="Net realized price per prescription declines under aggressive PBM rebate demands and statutory caps.",
            ),
        ],
    ),
]

REGIME_REGISTRY: dict[str, RegimeDefinition] = {r.regime_id: r for r in INITIAL_REGIMES}


# ---------------------------------------------------------------------------
# Core Pure Computation Functions (PRD §6.1)
# ---------------------------------------------------------------------------


def score_to_rating(score: float) -> QualitativeRating:
    """Convert a continuous regime score in [-1.0, 1.0] to a qualitative rating."""
    if score >= 0.50:
        return "benefits"
    if score >= 0.15:
        return "resilient"
    if score > -0.15:
        return "mixed"
    if score > -0.50:
        return "vulnerable"
    return "highly_vulnerable"


def evaluate_holding_regime(
    factor_loadings: dict[str, float],
    regime: RegimeDefinition,
) -> tuple[float, QualitativeRating, list[str]]:
    """Evaluate an individual holding against a regime definition.

    Formula (PRD §6.1):
      sum(loading * (effect_ordinal / 2.0) * confidence) / max(1.0, total_applicable_loading)
      capped strictly to [-1.0, 1.0].
    """
    total_weighted_impact = 0.0
    total_applicable_loading = 0.0
    applicable_factors: list[str] = []

    for impact in regime.factors:
        loading = factor_loadings.get(impact.factor, 0.0)
        if loading > 0.0:
            factor_contribution = loading * (impact.effect_ordinal / 2.0) * impact.confidence
            total_weighted_impact += factor_contribution
            total_applicable_loading += loading
            applicable_factors.append(impact.factor)

    if total_applicable_loading <= 0.0:
        return 0.0, "mixed", []

    denominator = max(1.0, total_applicable_loading)
    raw_score = total_weighted_impact / denominator
    capped_score = max(-1.0, min(1.0, raw_score))
    rating = score_to_rating(capped_score)

    return capped_score, rating, applicable_factors


def evaluate_portfolio_regime(
    holdings_weights: dict[str, float],
    holdings_factors: dict[str, dict[str, float]],
    regime: RegimeDefinition,
    min_coverage_pct: float = 70.0,
) -> PortfolioRegimeAssessment:
    """Compute portfolio-wide regime vulnerability and availability."""
    total_non_cash_weight = 0.0
    covered_non_cash_weight = 0.0
    weighted_score_sum = 0.0
    holding_assessments: list[HoldingRegimeAssessment] = []
    excluded_tickers: list[str] = []

    for ticker, weight in holdings_weights.items():
        if ticker.upper() in ("USD", "CASH", "CURRENCY"):
            continue
        if weight <= 0.0:
            continue

        total_non_cash_weight += weight
        factors = holdings_factors.get(ticker)

        if factors is not None and len(factors) > 0:
            score, rating, app_factors = evaluate_holding_regime(factors, regime)
            covered_non_cash_weight += weight
            weighted_score_sum += score * weight
            holding_assessments.append(
                HoldingRegimeAssessment(
                    ticker=ticker,
                    weight_pct=weight,
                    raw_score=score,
                    rating=rating,
                    applicable_factors=app_factors,
                )
            )
        else:
            excluded_tickers.append(ticker)

    if total_non_cash_weight > 0.0:
        coverage_pct = (covered_non_cash_weight / total_non_cash_weight) * 100.0
    else:
        coverage_pct = 0.0

    if covered_non_cash_weight > 0.0:
        portfolio_raw_score = weighted_score_sum / covered_non_cash_weight
    else:
        portfolio_raw_score = 0.0

    portfolio_raw_score = max(-1.0, min(1.0, portfolio_raw_score))
    portfolio_rating = score_to_rating(portfolio_raw_score)

    if coverage_pct >= min_coverage_pct:
        availability: AvailabilityStatus = "full"
    elif coverage_pct > 0.0:
        availability = "partial"
    else:
        availability = "unavailable"

    return PortfolioRegimeAssessment(
        regime_id=regime.regime_id,
        regime_name=regime.display_name,
        raw_score=round(portfolio_raw_score, 4),
        rating=portfolio_rating,
        coverage_pct=round(coverage_pct, 2),
        availability=availability,
        covered_weight_pct=round(covered_non_cash_weight, 4),
        total_non_cash_weight_pct=round(total_non_cash_weight, 4),
        excluded_tickers=sorted(excluded_tickers),
        holdings=sorted(holding_assessments, key=lambda h: -h.weight_pct),
        registry_version=regime.registry_version,
    )


def evaluate_action_regime_impact(
    current_weights: dict[str, float],
    proposed_deltas: dict[str, float],
    holdings_factors: dict[str, dict[str, float]],
    regime: RegimeDefinition,
) -> ActionRegimeImpact:
    """Evaluate before-and-after change in regime vulnerability for a proposed action."""
    before_eval = evaluate_portfolio_regime(current_weights, holdings_factors, regime)

    # Compute after weights
    after_weights = dict(current_weights)
    for ticker, delta in proposed_deltas.items():
        after_weights[ticker] = max(0.0, after_weights.get(ticker, 0.0) + delta)

    after_eval = evaluate_portfolio_regime(after_weights, holdings_factors, regime)

    score_delta = round(after_eval.raw_score - before_eval.raw_score, 4)

    # Material change policy (PRD §6.1):
    # Moves by >= 0.02 OR crosses category boundary
    rating_crossed = before_eval.rating != after_eval.rating
    delta_significant = abs(score_delta) >= 0.02
    material_change = rating_crossed or delta_significant

    if not material_change:
        direction: DirectionStatus = "no_material_change"
    elif score_delta < 0.0:
        # Negative score means more vulnerable
        direction = "increased_vulnerability"
    else:
        # Positive score means more resilient / beneficial
        direction = "decreased_vulnerability"

    return ActionRegimeImpact(
        regime_id=regime.regime_id,
        regime_name=regime.display_name,
        before_score=before_eval.raw_score,
        after_score=after_eval.raw_score,
        score_delta=score_delta,
        before_rating=before_eval.rating,
        after_rating=after_eval.rating,
        direction=direction,
        material_change=material_change,
        registry_version=regime.registry_version,
    )


def select_top_regimes_for_action(
    current_weights: dict[str, float],
    proposed_deltas: dict[str, float],
    holdings_factors: dict[str, dict[str, float]],
    top_n: int = 2,
) -> list[ActionRegimeImpact]:
    """Select the top N most relevant regimes for an add/trim action (PRD §6.1).

    Selection order:
    1. Absolute material score delta
    2. Baseline vulnerability (lowest before_score)
    3. Registry priority order
    """
    impacts: list[tuple[ActionRegimeImpact, RegimeDefinition]] = []

    for regime in INITIAL_REGIMES:
        impact = evaluate_action_regime_impact(
            current_weights,
            proposed_deltas,
            holdings_factors,
            regime,
        )
        impacts.append((impact, regime))

    # Sort key:
    # 1. -abs(score_delta) (highest delta first)
    # 2. before_score (lowest/most vulnerable first)
    # 3. regime.priority (lowest priority int first)
    impacts.sort(
        key=lambda item: (
            -abs(item[0].score_delta),
            item[0].before_score,
            item[1].priority,
        )
    )

    return [item[0] for item in impacts[:top_n]]


__all__ = [
    "INITIAL_REGIMES",
    "REGIME_REGISTRY",
    "REGIME_REGISTRY_VERSION",
    "ActionRegimeImpact",
    "AvailabilityStatus",
    "DirectionStatus",
    "FactorImpact",
    "HoldingRegimeAssessment",
    "PortfolioRegimeAssessment",
    "QualitativeRating",
    "RegimeDefinition",
    "evaluate_action_regime_impact",
    "evaluate_holding_regime",
    "evaluate_portfolio_regime",
    "score_to_rating",
    "select_top_regimes_for_action",
]
