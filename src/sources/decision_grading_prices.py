"""Admit comparable, dated adjusted prices for the existing price-only grader."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict

from sources.adapters import (
    AdjustedPricePoint,
    CorporateActionAdjustment,
    CurrencyBinding,
    CurrencyBindingBasis,
)
from sources.market_price_policy import PRICE_STALE_DAYS
from sources.readers import ProviderNeutralDataReader, ReaderUnavailableStatus


class DecisionPriceEvidence(BaseModel):
    """Exact input manifest stored with the resulting decision outcome."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    schema_version: Literal["decision-price-evidence/v1"] = "decision-price-evidence/v1"
    ticker: str
    provider: str
    currency_binding: CurrencyBinding
    adjustment_method: CorporateActionAdjustment
    source_payload_hash: str
    reference: AdjustedPricePoint
    outcome: AdjustedPricePoint
    graded_as_of: datetime
    market_price_max_age_days: int = PRICE_STALE_DAYS
    # The reader exposes observation dates and packet hashes, not capture time.
    capture_freshness: Literal["unverified"] = "unverified"

    @property
    def pct_change(self) -> float:
        return float((self.outcome.close - self.reference.close) / self.reference.close)


def decision_price_evidence(
    reader: ProviderNeutralDataReader, *, ticker: str, made_at: datetime, now: datetime
) -> DecisionPriceEvidence | ReaderUnavailableStatus:
    """Preserve the shared seven-day market-price validity rule at both endpoints."""
    series = reader.get_adjusted_price_series(ticker)
    if isinstance(series, ReaderUnavailableStatus):
        return series

    def unavailable(reason: str) -> ReaderUnavailableStatus:
        return ReaderUnavailableStatus(
            ticker=ticker,
            provider=series.provider,
            data_type="decision_grading_prices",
            reason=reason,
            as_of=now,
        )

    if (
        series.ticker != ticker.upper()
        or series.currency_binding.ticker != series.ticker
        or series.currency_binding.currency != series.currency
        or series.currency_binding.basis != CurrencyBindingBasis.QUOTE
        or series.adjustment_method != CorporateActionAdjustment.SPLIT_AND_DIVIDEND
    ):
        return unavailable("price_identity_currency_or_adjustment_mismatch")
    dates = [point.as_of_date.date() for point in series.points]
    if len(set(dates)) != len(dates) or any(
        point.close <= 0 or point.as_of_date.date() > now.date() for point in series.points
    ):
        return unavailable("duplicate_future_or_nonpositive_price_observation")
    reference = max(
        (point for point in series.points if point.as_of_date.date() <= made_at.date()),
        key=lambda point: point.as_of_date,
        default=None,
    )
    outcome = max(series.points, key=lambda point: point.as_of_date, default=None)
    if reference is None or outcome is None:
        return unavailable("price_observation_unavailable")
    if (made_at.date() - reference.as_of_date.date()).days > PRICE_STALE_DAYS or (
        now.date() - outcome.as_of_date.date()
    ).days > PRICE_STALE_DAYS:
        return unavailable("price_observation_stale")
    if outcome.as_of_date <= reference.as_of_date or made_at.date() == now.date():
        return unavailable("subsequent_price_observation_unavailable")
    return DecisionPriceEvidence(
        ticker=series.ticker,
        provider=series.provider,
        currency_binding=series.currency_binding,
        adjustment_method=series.adjustment_method,
        source_payload_hash=series.source_payload_hash,
        reference=reference,
        outcome=outcome,
        graded_as_of=now,
    )
