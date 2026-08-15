"""Tests for Versioned Qualitative Common-Drawdown Regime Playbook (PRD §6.1 P1-A, BHA-45)."""

from __future__ import annotations

import pytest

from macro_regime_playbook import (
    INITIAL_REGIMES,
    REGIME_REGISTRY,
    REGIME_REGISTRY_VERSION,
    evaluate_action_regime_impact,
    evaluate_holding_regime,
    evaluate_portfolio_regime,
    score_to_rating,
    select_top_regimes_for_action,
)


def test_initial_regimes_registry_integrity() -> None:
    assert len(INITIAL_REGIMES) == 9
    assert len(REGIME_REGISTRY) == 9
    assert REGIME_REGISTRY_VERSION == "2026-08-15.1"

    expected_ids = {
        "demand_led_recession",
        "rates_inflation_shock",
        "sovereign_debt_funding_crisis",
        "currency_crisis",
        "oil_supply_shock",
        "saas_multiple_compression",
        "advertising_recession",
        "latam_credit_stress",
        "glp1_pricing_pressure",
    }
    assert set(REGIME_REGISTRY.keys()) == expected_ids

    for regime in INITIAL_REGIMES:
        assert regime.regime_id in expected_ids
        assert len(regime.display_name) > 0
        assert len(regime.description) > 0
        assert len(regime.factors) > 0
        for factor in regime.factors:
            assert -2 <= factor.effect_ordinal <= 2
            assert 0.0 <= factor.confidence <= 1.0
            assert len(factor.transmission_mechanism) > 0


@pytest.mark.parametrize(
    ("score", "expected_rating"),
    [
        (0.85, "benefits"),
        (0.50, "benefits"),
        (0.49, "resilient"),
        (0.15, "resilient"),
        (0.14, "mixed"),
        (0.00, "mixed"),
        (-0.14, "mixed"),
        (-0.15, "vulnerable"),
        (-0.49, "vulnerable"),
        (-0.50, "highly_vulnerable"),
        (-0.95, "highly_vulnerable"),
    ],
)
def test_score_to_rating_thresholds(score: float, expected_rating: str) -> None:
    assert score_to_rating(score) == expected_rating


def test_holding_evaluation_formula_and_incidental_floor() -> None:
    recession = REGIME_REGISTRY["demand_led_recession"]

    # Holding with 1.0 loading on travel (-2 ordinal, 0.95 confidence)
    # Impact = 1.0 * (-2/2) * 0.95 = -0.95
    # Denominator = max(1.0, 1.0) = 1.0 -> score = -0.95 -> highly_vulnerable
    score_full, rating_full, factors_full = evaluate_holding_regime(
        {"Global travel demand": 1.0},
        recession,
    )
    assert score_full == pytest.approx(-0.95, abs=1e-3)
    assert rating_full == "highly_vulnerable"
    assert factors_full == ["Global travel demand"]

    # Incidental 0.10 loading on travel
    # Impact = 0.10 * (-2/2) * 0.95 = -0.095
    # Denominator = max(1.0, 0.10) = 1.0 -> score = -0.095 -> mixed
    # Floor of 1.0 prevents 0.10 from scoring as -0.95
    score_incidental, rating_incidental, factors_incidental = evaluate_holding_regime(
        {"Global travel demand": 0.10},
        recession,
    )
    assert score_incidental == pytest.approx(-0.095, abs=1e-3)
    assert rating_incidental == "mixed"
    assert factors_incidental == ["Global travel demand"]


def test_holding_with_no_matching_factors_is_mixed_zero() -> None:
    recession = REGIME_REGISTRY["demand_led_recession"]
    score, rating, factors = evaluate_holding_regime(
        {"Unknown factor": 1.0},
        recession,
    )
    assert score == 0.0
    assert rating == "mixed"
    assert factors == []


def test_portfolio_evaluation_with_coverage_tiers() -> None:
    recession = REGIME_REGISTRY["demand_led_recession"]

    holdings_weights = {
        "BKNG": 10.0,
        "UBER": 10.0,
        "META": 10.0,
        "CASH": 20.0,
    }

    holdings_factors = {
        "BKNG": {"Global travel demand": 1.0},
        "UBER": {"US consumer mobility/delivery": 0.9},
        "META": {"Digital advertising demand": 1.0},
    }

    assessment = evaluate_portfolio_regime(
        holdings_weights,
        holdings_factors,
        recession,
    )

    assert assessment.total_non_cash_weight_pct == 30.0
    assert assessment.covered_weight_pct == 30.0
    assert assessment.coverage_pct == 100.0
    assert assessment.availability == "full"
    assert assessment.raw_score < -0.50
    assert assessment.rating == "highly_vulnerable"
    assert len(assessment.holdings) == 3
    assert len(assessment.excluded_tickers) == 0


def test_portfolio_partial_coverage_below_70_pct() -> None:
    recession = REGIME_REGISTRY["demand_led_recession"]

    holdings_weights = {
        "BKNG": 10.0,
        "UNKNOWN1": 20.0,
    }

    holdings_factors = {
        "BKNG": {"Global travel demand": 1.0},
    }

    assessment = evaluate_portfolio_regime(
        holdings_weights,
        holdings_factors,
        recession,
        min_coverage_pct=70.0,
    )

    # 10 / 30 = 33.33% coverage -> partial
    assert assessment.coverage_pct == pytest.approx(33.33, abs=0.1)
    assert assessment.availability == "partial"
    assert assessment.excluded_tickers == ["UNKNOWN1"]


def test_evaluate_action_regime_impact_materiality() -> None:
    saas_shock = REGIME_REGISTRY["saas_multiple_compression"]

    current_weights = {
        "NOW": 5.0,
        "BKNG": 10.0,
    }
    holdings_factors = {
        "NOW": {"US enterprise IT budgets": 1.0},
        "BKNG": {"Global travel demand": 1.0},
    }

    # Propose adding 5% to NOW (high SaaS exposure)
    proposed_deltas = {"NOW": 5.0}

    impact = evaluate_action_regime_impact(
        current_weights,
        proposed_deltas,
        holdings_factors,
        saas_shock,
    )

    assert impact.material_change is True
    assert impact.direction == "increased_vulnerability"
    assert impact.score_delta < -0.02


def test_select_top_regimes_for_action_ranking() -> None:
    current_weights = {
        "NU": 10.0,
        "MELI": 10.0,
        "NOW": 5.0,
    }
    holdings_factors = {
        "NU": {"Brazil consumer credit": 1.0, "LatAm consumer/FX": 0.8},
        "MELI": {"LatAm consumer/FX": 1.0, "SMB digital adoption": 0.5},
        "NOW": {"US enterprise IT budgets": 1.0},
    }

    # Propose trim of NU (-3%) and add of NOW (+3%)
    proposed_deltas = {"NU": -3.0, "NOW": 3.0}

    top_regimes = select_top_regimes_for_action(
        current_weights,
        proposed_deltas,
        holdings_factors,
        top_n=2,
    )

    assert len(top_regimes) == 2
    # LatAm / credit / FX / SaaS regimes should dominate the delta
    top_ids = [r.regime_id for r in top_regimes]
    assert any(
        reg in top_ids
        for reg in ("latam_credit_stress", "currency_crisis", "saas_multiple_compression", "sovereign_debt_funding_crisis")
    )
