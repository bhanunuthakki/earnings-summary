"""Read-only, fail-closed portfolio allocation projection for BHA-79.

This module deliberately consumes the v1 positions endpoint instead of the
legacy live-portfolio payload: v1 positions preserve the ``security_id`` used
to join the typed securities master.  The projection never infers geography
from a ticker, fund name, domicile, or other free text.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Literal, Protocol, TypeGuard

from pydantic import BaseModel, ConfigDict

from integrations.portfolio_position import supported_schema_major, validate_positions_snapshot
from integrations.portfolio_tracker_v1 import (
    HealthV1,
    PositionsV1Result,
    PositionV1,
    SecuritiesV1Result,
    SecurityV1,
    TrackerV1Client,
    V1Fetch,
)

_BUCKET_NAMES = (
    "us_equity",
    "international_equity",
    "us_etf",
    "international_etf",
    "cash",
    "unclassified",
)
_POSITION_PERCENT_TOLERANCE = Decimal("0.0001")
_ZERO = Decimal(0)


class PortfolioAllocationBucket(BaseModel):
    """One allocation bucket, with percent expressed as 0-100 points."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    value: Decimal | None
    weight_pct: Decimal | None


class PortfolioAllocationBuckets(BaseModel):
    """The six mutually exclusive buckets used by the allocation surface."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    us_equity: PortfolioAllocationBucket
    international_equity: PortfolioAllocationBucket
    us_etf: PortfolioAllocationBucket
    international_etf: PortfolioAllocationBucket
    cash: PortfolioAllocationBucket
    unclassified: PortfolioAllocationBucket


class PortfolioAllocationReconciliation(BaseModel):
    """Values used to prove the allocation preserves the raw portfolio total."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    position_total: Decimal | None
    bucket_total: Decimal | None
    difference: Decimal | None
    is_reconciled: bool


class PortfolioAllocationProjection(BaseModel):
    """Typed, source-truthful allocation projection for downstream renderers."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    state: Literal["available", "incomplete", "unavailable"]
    source_identity: str
    as_of: date | None = None
    currency: str | None = None
    buckets: PortfolioAllocationBuckets
    reconciliation: PortfolioAllocationReconciliation
    reason_codes: tuple[str, ...] = ()


class PortfolioAllocationReader(Protocol):
    """The smallest v1 reader contract required for allocation projection."""

    def probe_v1(self) -> V1Fetch[HealthV1]: ...

    def get_positions(self) -> V1Fetch[PositionsV1Result]: ...

    def get_securities(self) -> V1Fetch[SecuritiesV1Result]: ...


def _null_buckets() -> PortfolioAllocationBuckets:
    bucket = PortfolioAllocationBucket(value=None, weight_pct=None)
    return PortfolioAllocationBuckets(
        us_equity=bucket,
        international_equity=bucket,
        us_etf=bucket,
        international_etf=bucket,
        cash=bucket,
        unclassified=bucket,
    )


def _unavailable(code: str) -> PortfolioAllocationProjection:
    return PortfolioAllocationProjection(
        state="unavailable",
        source_identity="portfolio_tracker_api_v1",
        buckets=_null_buckets(),
        reconciliation=PortfolioAllocationReconciliation(
            position_total=None,
            bucket_total=None,
            difference=None,
            is_reconciled=False,
        ),
        reason_codes=(code,),
    )


def _finite_decimal(value: object) -> TypeGuard[Decimal]:
    return isinstance(value, Decimal) and value.is_finite()


def _account_coverage_error(
    health: HealthV1,
    positions: PositionsV1Result,
    securities: SecuritiesV1Result,
) -> str | None:
    coverage = securities.meta.account_coverage
    included = coverage.included_account_ids
    excluded = coverage.excluded_account_ids
    if (
        not included
        or len(included) != len(set(included))
        or len(excluded) != len(set(excluded))
        or bool(set(included).intersection(excluded))
        or len(included) != health.active_account_count
    ):
        return "account_coverage_invalid"
    lagging = coverage.lagging_account_ids
    if len(lagging) != len(set(lagging)) or not set(lagging).issubset(included):
        return "account_coverage_invalid"
    if lagging:
        return "portfolio_snapshot_account_coverage_lagging"
    referenced_account_ids = {
        lot.account_id for position in positions.positions for lot in position.accounts
    }
    if not referenced_account_ids.issubset(included) or bool(
        referenced_account_ids.intersection(excluded)
    ):
        return "account_coverage_invalid"
    return None


def _structural_error(
    health: HealthV1,
    positions: PositionsV1Result,
    securities: SecuritiesV1Result,
) -> str | None:
    meta = securities.meta
    if (
        health.status != "ok"
        or not health.database_ok
        or health.active_account_count < 1
        or health.is_stale
    ):
        return "health_invalid"
    if not supported_schema_major(health.schema_version) or not supported_schema_major(
        meta.schema_version
    ):
        return "incompatible_schema_version"
    if health.generated_at.tzinfo is None or health.generated_at > datetime.now(UTC):
        return "health_clock_invalid"
    if positions.snapshot_date is None or health.latest_snapshot_date is None or meta.as_of is None:
        return "snapshot_date_missing"
    if (
        positions.snapshot_date != health.latest_snapshot_date
        or positions.snapshot_date != meta.as_of
    ):
        return "snapshot_date_mismatch"
    if not meta.currency.strip():
        return "currency_missing"
    if meta.methodology != "securities.master" or not meta.methodology_version:
        return "securities_methodology_unsupported"
    if meta.is_partial:
        return "portfolio_snapshot_partial"
    if meta.is_stale:
        return "portfolio_snapshot_stale"
    coverage_error = _account_coverage_error(health, positions, securities)
    if coverage_error is not None:
        return coverage_error
    if not _finite_decimal(positions.total_market_value) or positions.total_market_value <= _ZERO:
        return "portfolio_total_invalid"
    reconciliation_error = validate_positions_snapshot(positions)
    if reconciliation_error is not None:
        return reconciliation_error[0]
    position_ids = [position.security_id for position in positions.positions]
    security_ids = [security.security_id for security in securities.securities]
    if len(position_ids) != len(set(position_ids)):
        return "duplicate_position_security_id"
    if len(security_ids) != len(set(security_ids)):
        return "duplicate_security_id"
    security_id_set = set(security_ids)
    for position in positions.positions:
        if position.security_id not in security_id_set:
            return "security_join_missing"
        if not _valid_position_measure(position, positions.total_market_value):
            return "position_percent_reconciliation_failed"
    return None


def _valid_position_measure(position: PositionV1, total: Decimal) -> bool:
    if (
        not _finite_decimal(position.market_value)
        or position.market_value < _ZERO
        or not _finite_decimal(position.percent_of_portfolio)
    ):
        return False
    recomputed_percent = position.market_value / total * Decimal(100)
    return abs(recomputed_percent - position.percent_of_portfolio) <= _POSITION_PERCENT_TOLERANCE


def _bucket_name(security: SecurityV1) -> str:
    """Classify only fields defined by the securities-master contract."""
    asset_type = security.asset_type.strip().casefold()
    region = security.region.strip().casefold() if security.region is not None else ""
    if security.is_cash_equivalent or asset_type == "cash":
        return "cash"
    if asset_type == "stock":
        if region == "us":
            return "us_equity"
        if region == "international":
            return "international_equity"
    if asset_type == "etf":
        if region == "us":
            return "us_etf"
        if region == "international":
            return "international_etf"
    return "unclassified"


def project_portfolio_allocation(
    health: HealthV1,
    positions: PositionsV1Result,
    securities: SecuritiesV1Result,
) -> PortfolioAllocationProjection:
    """Project validated v1 source data into six raw-total-preserving buckets."""
    error = _structural_error(health, positions, securities)
    if error is not None:
        return _unavailable(error)

    totals = {name: _ZERO for name in _BUCKET_NAMES}
    by_security_id = {security.security_id: security for security in securities.securities}
    for position in positions.positions:
        security = by_security_id[position.security_id]
        # _structural_error() has proved this field is a finite Decimal.
        market_value = position.market_value
        assert isinstance(market_value, Decimal)
        totals[_bucket_name(security)] += market_value

    total = positions.total_market_value
    bucket_total = sum(totals.values(), _ZERO)
    difference = bucket_total - total
    if difference != _ZERO:
        return _unavailable("portfolio_allocation_reconciliation_failed")

    def bucket(name: str) -> PortfolioAllocationBucket:
        value = totals[name]
        return PortfolioAllocationBucket(value=value, weight_pct=value / total * Decimal(100))

    allocation_buckets = PortfolioAllocationBuckets(
        us_equity=bucket("us_equity"),
        international_equity=bucket("international_equity"),
        us_etf=bucket("us_etf"),
        international_etf=bucket("international_etf"),
        cash=bucket("cash"),
        unclassified=bucket("unclassified"),
    )
    incomplete = totals["unclassified"] != _ZERO
    return PortfolioAllocationProjection(
        state="incomplete" if incomplete else "available",
        source_identity="portfolio_tracker_api_v1",
        as_of=securities.meta.as_of,
        currency=securities.meta.currency,
        buckets=allocation_buckets,
        reconciliation=PortfolioAllocationReconciliation(
            position_total=total,
            bucket_total=bucket_total,
            difference=difference,
            is_reconciled=True,
        ),
        reason_codes=("portfolio_allocation_incomplete",) if incomplete else (),
    )


def read_portfolio_allocation(reader: PortfolioAllocationReader) -> PortfolioAllocationProjection:
    """Read the three required v1 endpoints without leaking transport details."""
    try:
        health_fetch = reader.probe_v1()
    except Exception:
        return _unavailable("health_unavailable")
    if not health_fetch.available or health_fetch.data is None:
        return _unavailable("health_unavailable")
    try:
        positions_fetch = reader.get_positions()
    except Exception:
        return _unavailable("positions_unavailable")
    if not positions_fetch.available or positions_fetch.data is None:
        return _unavailable("positions_unavailable")
    try:
        securities_fetch = reader.get_securities()
    except Exception:
        return _unavailable("securities_unavailable")
    if not securities_fetch.available or securities_fetch.data is None:
        return _unavailable("securities_unavailable")
    return project_portfolio_allocation(
        health_fetch.data,
        positions_fetch.data,
        securities_fetch.data,
    )


def fetch_portfolio_allocation(api_url: str | None = None) -> PortfolioAllocationProjection:
    """Fetch the projection through the canonical typed v1 client."""
    return read_portfolio_allocation(TrackerV1Client(base_url=api_url))


__all__ = [
    "PortfolioAllocationBucket",
    "PortfolioAllocationBuckets",
    "PortfolioAllocationProjection",
    "PortfolioAllocationReader",
    "PortfolioAllocationReconciliation",
    "fetch_portfolio_allocation",
    "project_portfolio_allocation",
    "read_portfolio_allocation",
]
