"""Hermetic unit tests for three-regime semantic and historical as-of backtests."""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

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
        setattr(obs, "composite_quality_score", Decimal("0.50"))

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
        setattr(receipt, "status", "HOLD")


@pytest.mark.parametrize("tickers", [["RBRK", "WIX"], ["NO_SUCH_ISSUER"], []])
def test_missing_real_artifacts_cannot_certify_backtest(tickers: list[str]) -> None:
    receipt = ThreeRegimeBacktestRunner().evaluate_cohort(tickers, date(1900, 1, 1))
    assert receipt.status == "HOLD"
    assert receipt.observations == ()
    assert receipt.total_tickers_evaluated == 0
    assert receipt.total_regimes_evaluated == 0
    assert receipt.regime_quality_summary == {}
    assert receipt.regime_cost_summary_usd == {}
    assert "unavailable" in receipt.recommendation.lower()


def test_operational_cli_exits_nonzero_with_hold_receipt(tmp_path: Path) -> None:
    receipt_path = tmp_path / "receipt.json"
    result = subprocess.run(
        [sys.executable, "execution/run_regime_backtest.py", "--output-receipt", str(receipt_path)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
    receipt = json.loads(receipt_path.read_text())
    assert receipt["status"] == "HOLD"
    assert receipt["reason_codes"]
