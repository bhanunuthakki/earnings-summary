"""Three-regime backtest contracts and explicit unavailable-evidence receipt.

Real source-bound rendering, scoring, historical selection, and measured cost
inputs are not integrated yet. The runner must not certify invented observations.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field


class SourceRegime(StrEnum):
    """Source-regime classification for data resolution and backtesting."""

    REGIME_0_VENDOR_ONLY = "REGIME_0_VENDOR_ONLY"
    REGIME_1_SEC_IR_PRIMARY = "REGIME_1_SEC_IR_PRIMARY"
    REGIME_2_COMBINED = "REGIME_2_COMBINED"


class StratumCohort(StrEnum):
    """Stratified company cohort types for balanced empirical representation."""

    STRATUM_10K_OPERATING = "STRATUM_10K_OPERATING"
    STRATUM_20F_FOREIGN = "STRATUM_20F_FOREIGN"
    STRATUM_40F_CANADIAN = "STRATUM_40F_CANADIAN"
    STRATUM_SPARSE_SEMIANNUAL = "STRATUM_SPARSE_SEMIANNUAL"


class RegimeEvaluationObservation(BaseModel):
    """Immutable evaluation observation for a single ticker under a specific regime."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ticker: str
    regime: SourceRegime
    stratum: StratumCohort
    as_of_date: date
    metrics_calculated_count: int
    dcf_valuation_fitness: Decimal = Field(..., ge=Decimal("0.0"), le=Decimal("1.0"))
    plausibility_score: Decimal = Field(..., ge=Decimal("0.0"), le=Decimal("1.0"))
    citation_fidelity_score: Decimal = Field(..., ge=Decimal("0.0"), le=Decimal("1.0"))
    completeness_score: Decimal = Field(..., ge=Decimal("0.0"), le=Decimal("1.0"))
    composite_quality_score: Decimal = Field(..., ge=Decimal("0.0"), le=Decimal("1.0"))
    cost_attribution_usd: Decimal = Field(..., ge=Decimal("0.0"))
    latency_ms: int = Field(..., ge=0)
    lookahead_prevented: bool = False
    notes: str


class ThreeRegimeBacktestReceipt(BaseModel):
    """Immutable receipt of a multi-regime historical as-of backtest run."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: str
    as_of_date: date
    total_tickers_evaluated: int
    total_regimes_evaluated: int
    regime_quality_summary: dict[str, Decimal]
    regime_cost_summary_usd: dict[str, Decimal]
    status: Literal["PASS", "HOLD", "BLOCK"]
    observations: tuple[RegimeEvaluationObservation, ...] = ()
    recommendation: str
    verified_at: datetime
    requested_tickers: tuple[str, ...] = ()
    reason_codes: tuple[str, ...] = ()


class ThreeRegimeBacktestRunner:
    """Report the missing empirical boundary without producing synthetic scores."""

    def evaluate_cohort(
        self,
        tickers: list[str],
        as_of_date: date = date(2026, 4, 30),
    ) -> ThreeRegimeBacktestReceipt:
        """Hold until real, immutable output and measured-cost inputs are wired in."""
        return ThreeRegimeBacktestReceipt(
            run_id=f"regime_bt_{uuid4().hex}",
            as_of_date=as_of_date,
            requested_tickers=tuple(ticker.upper().strip() for ticker in tickers),
            total_tickers_evaluated=0,
            total_regimes_evaluated=0,
            regime_quality_summary={},
            regime_cost_summary_usd={},
            status="HOLD",
            observations=(),
            recommendation=(
                "Recommendation unavailable: sealed real regime outputs, historical-as-of "
                "selection, independent quality evaluation, and measured cost evidence "
                "are not integrated. No provider selection or activation is supported."
            ),
            reason_codes=("real_regime_backtest_not_implemented",),
            verified_at=datetime.now(UTC),
        )
