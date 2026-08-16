"""Hermetic unit tests for three-regime semantic and historical as-of backtests."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from evals.regime_backtest import (
    RegimeEvaluationObservation,
    SourceRegime,
    StratumCohort,
    ThreeRegimeBacktestReceipt,
    ThreeRegimeBacktestRunner,
)


def test_regime_models_frozen_immutability() -> None:
    """Assert regime observation and receipt models reject mutations and extra fields."""
    obs = RegimeEvaluationObservation(
        ticker="RBRK",
        regime=SourceRegime.REGIME_2_COMBINED,
        stratum=StratumCohort.STRATUM_10K_OPERATING,
        as_of_date=date(2026, 4, 30),
        metrics_calculated_count=24,
        dcf_valuation_fitness=Decimal("0.98"),
        plausibility_score=Decimal("0.99"),
        citation_fidelity_score=Decimal("0.98"),
        completeness_score=Decimal("0.97"),
        composite_quality_score=Decimal("0.98"),
        cost_attribution_usd=Decimal("0.005"),
        latency_ms=150,
        lookahead_prevented=True,
        notes="OK",
    )
    with pytest.raises(ValidationError):
        obs.composite_quality_score = Decimal("0.50")  # type: ignore[misc]

    receipt = ThreeRegimeBacktestReceipt(
        run_id="run_1",
        as_of_date=date(2026, 4, 30),
        total_tickers_evaluated=1,
        total_regimes_evaluated=3,
        regime_quality_summary={"REGIME_2_COMBINED": Decimal("0.98")},
        regime_cost_summary_usd={"REGIME_2_COMBINED": Decimal("0.005")},
        status="PASS",
        observations=(obs,),
        recommendation="Recommend Combined",
        verified_at=datetime.now(UTC),
    )
    with pytest.raises(ValidationError):
        receipt.status = "HOLD"  # type: ignore[misc]


def test_three_regime_backtest_runner_evaluation() -> None:
    """Assert runner evaluates stratified cohort across all 3 regimes without look-ahead."""
    runner = ThreeRegimeBacktestRunner()
    cohort = ["RBRK", "WIX", "NVO", "BN", "ASML", "BHP"]
    as_of = date(2026, 4, 30)

    receipt = runner.evaluate_cohort(tickers=cohort, as_of_date=as_of)

    assert receipt.status == "PASS"
    assert receipt.total_tickers_evaluated == 6
    assert receipt.total_regimes_evaluated == 3
    assert len(receipt.observations) == 18  # 6 tickers * 3 regimes

    # Check lookahead prevention invariant
    for obs in receipt.observations:
        assert obs.lookahead_prevented is True
        assert obs.as_of_date == as_of

    # Regime 2 (Combined) should achieve highest composite quality
    q_regime0 = receipt.regime_quality_summary[SourceRegime.REGIME_0_VENDOR_ONLY.value]
    q_regime1 = receipt.regime_quality_summary[SourceRegime.REGIME_1_SEC_IR_PRIMARY.value]
    q_regime2 = receipt.regime_quality_summary[SourceRegime.REGIME_2_COMBINED.value]

    assert q_regime2 > q_regime0
    assert q_regime2 > q_regime1
    assert q_regime2 >= Decimal("0.95")

    # Regime 2 (Combined) has substantially lower cost than Regime 0 (Vendor-Only)
    c_regime0 = receipt.regime_cost_summary_usd[SourceRegime.REGIME_0_VENDOR_ONLY.value]
    c_regime2 = receipt.regime_cost_summary_usd[SourceRegime.REGIME_2_COMBINED.value]
    assert c_regime2 < c_regime0


def test_strata_specific_quality_adjustments() -> None:
    """Assert semiannual and foreign filer strata receive specific empirical adjustments."""
    runner = ThreeRegimeBacktestRunner()
    receipt = runner.evaluate_cohort(tickers=["WIX", "BHP"])

    # BHP (Semiannual) completeness adjusted
    bhp_obs_regime2 = next(o for o in receipt.observations if o.ticker == "BHP" and o.regime == SourceRegime.REGIME_2_COMBINED)
    wix_obs_regime2 = next(o for o in receipt.observations if o.ticker == "WIX" and o.regime == SourceRegime.REGIME_2_COMBINED)
    assert bhp_obs_regime2.completeness_score < wix_obs_regime2.completeness_score

    # WIX (20-F) under vendor-only has reduced citation fidelity
    wix_vendor = next(o for o in receipt.observations if o.ticker == "WIX" and o.regime == SourceRegime.REGIME_0_VENDOR_ONLY)
    wix_sec = next(o for o in receipt.observations if o.ticker == "WIX" and o.regime == SourceRegime.REGIME_1_SEC_IR_PRIMARY)
    assert wix_vendor.citation_fidelity_score < wix_sec.citation_fidelity_score
