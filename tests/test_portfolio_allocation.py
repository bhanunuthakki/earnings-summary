"""Contracts for the read-only BHA-79 portfolio allocation projection."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from integrations.portfolio_tracker_v1 import (
    HealthV1,
    PositionsV1Result,
    SecuritiesV1Result,
    V1Fetch,
)

_FIXTURES = Path(__file__).parent / "fixtures" / "tracker_v1"


def _health() -> HealthV1:
    return HealthV1.model_validate_json((_FIXTURES / "health.json").read_bytes())


def _positions() -> PositionsV1Result:
    return PositionsV1Result.model_validate_json((_FIXTURES / "positions.json").read_bytes())


def _securities() -> SecuritiesV1Result:
    return SecuritiesV1Result.model_validate_json((_FIXTURES / "securities.json").read_bytes())


class _Reader:
    def __init__(
        self,
        health: V1Fetch[HealthV1],
        positions: V1Fetch[PositionsV1Result],
        securities: V1Fetch[SecuritiesV1Result],
    ) -> None:
        self.health = health
        self.positions = positions
        self.securities = securities

    def probe_v1(self) -> V1Fetch[HealthV1]:
        return self.health

    def get_positions(self) -> V1Fetch[PositionsV1Result]:
        return self.positions

    def get_securities(self) -> V1Fetch[SecuritiesV1Result]:
        return self.securities


def _reader(
    *,
    health: HealthV1 | None = None,
    positions: PositionsV1Result | None = None,
    securities: SecuritiesV1Result | None = None,
) -> _Reader:
    return _Reader(
        V1Fetch(available=True, endpoint="/api/v1/health", data=health or _health()),
        V1Fetch(
            available=True,
            endpoint="/api/v1/portfolio/positions",
            data=positions or _positions(),
        ),
        V1Fetch(available=True, endpoint="/api/v1/securities", data=securities or _securities()),
    )


def _classified_securities() -> SecuritiesV1Result:
    base = _securities()
    securities = [
        security.model_copy(update={"asset_type": "Stock", "region": "US"})
        for security in base.securities[:1]
    ]
    securities.extend(
        [
            base.securities[1].model_copy(update={"asset_type": "ETF", "region": "International"}),
            base.securities[2],
        ]
    )
    return base.model_copy(update={"securities": securities})


def test_projects_typed_current_allocation_with_decimal_buckets() -> None:
    from integrations.portfolio_allocation import read_portfolio_allocation

    result = read_portfolio_allocation(_reader(securities=_classified_securities()))

    assert result.state == "available"
    assert result.as_of == date(2026, 7, 22)
    assert result.currency == "USD"
    assert result.reason_codes == ()
    assert result.buckets.us_equity.value == Decimal("13200")
    assert result.buckets.international_etf.value == Decimal("5800")
    assert result.buckets.cash.value == Decimal("1000")
    assert result.buckets.us_equity.weight_pct == Decimal("66")
    assert result.buckets.international_etf.weight_pct == Decimal("29")
    assert result.buckets.cash.weight_pct == Decimal("5")
    assert result.buckets.international_equity.value == Decimal("0")
    assert result.buckets.us_etf.value == Decimal("0")
    assert result.buckets.unclassified.value == Decimal("0")
    assert result.reconciliation.position_total == Decimal("20000")
    assert result.reconciliation.bucket_total == Decimal("20000")
    assert result.reconciliation.is_reconciled is True


def test_unknown_etf_geography_is_incomplete_and_kept_in_denominator() -> None:
    from integrations.portfolio_allocation import read_portfolio_allocation

    securities = _classified_securities()
    unknown_geography = securities.securities[1].model_copy(update={"region": None})
    result = read_portfolio_allocation(
        _reader(
            securities=securities.model_copy(
                update={
                    "securities": [
                        securities.securities[0],
                        unknown_geography,
                        securities.securities[2],
                    ]
                }
            )
        )
    )

    assert result.state == "incomplete"
    assert result.reason_codes == ("portfolio_allocation_incomplete",)
    assert result.buckets.international_etf.value == Decimal("0")
    assert result.buckets.unclassified.value == Decimal("5800")
    assert result.buckets.unclassified.weight_pct == Decimal("29")
    assert result.reconciliation.bucket_total == Decimal("20000")


@pytest.mark.parametrize(
    ("update", "code"),
    [
        ({"is_stale": True}, "health_invalid"),
        ({"latest_snapshot_date": date(2026, 7, 21)}, "snapshot_date_mismatch"),
        ({"active_account_count": 2}, "account_coverage_invalid"),
    ],
)
def test_rejects_untruthful_health_or_account_coverage(
    update: dict[str, object], code: str
) -> None:
    from integrations.portfolio_allocation import read_portfolio_allocation

    result = read_portfolio_allocation(_reader(health=_health().model_copy(update=update)))

    assert result.state == "unavailable"
    assert result.reason_codes == (code,)
    assert result.buckets.us_equity.value is None
    assert result.buckets.unclassified.weight_pct is None
    assert result.reconciliation.position_total is None


def test_rejects_missing_security_join_and_provider_percent_mismatch() -> None:
    from integrations.portfolio_allocation import read_portfolio_allocation

    base = _positions()
    mismatched = base.model_copy(
        update={
            "positions": [
                base.positions[0].model_copy(update={"percent_of_portfolio": Decimal("65")}),
                *base.positions[1:],
            ]
        }
    )
    mismatch = read_portfolio_allocation(_reader(positions=mismatched))
    missing_join = read_portfolio_allocation(
        _reader(
            securities=_securities().model_copy(update={"securities": _securities().securities[:2]})
        )
    )

    assert mismatch.state == "unavailable"
    assert mismatch.reason_codes == ("position_percent_reconciliation_failed",)
    assert missing_join.state == "unavailable"
    assert missing_join.reason_codes == ("security_join_missing",)


def test_fetch_failure_never_surfaces_transport_error_text() -> None:
    from integrations.portfolio_allocation import read_portfolio_allocation

    result = read_portfolio_allocation(
        _Reader(
            V1Fetch(
                available=False,
                endpoint="/api/v1/health",
                error="https://tracker.test?api_key=not-for-output",
            ),
            V1Fetch(available=True, endpoint="/api/v1/portfolio/positions", data=_positions()),
            V1Fetch(available=True, endpoint="/api/v1/securities", data=_securities()),
        )
    )

    rendered = result.model_dump_json()
    assert result.state == "unavailable"
    assert result.reason_codes == ("health_unavailable",)
    assert "not-for-output" not in rendered


def test_models_are_frozen_and_reject_unknown_fields() -> None:
    from integrations.portfolio_allocation import PortfolioAllocationBucket

    bucket = PortfolioAllocationBucket(value=Decimal("1"), weight_pct=Decimal("1"))
    with pytest.raises(Exception):
        setattr(bucket, "value", Decimal("2"))
    with pytest.raises(Exception):
        PortfolioAllocationBucket.model_validate(
            {"value": Decimal("1"), "weight_pct": Decimal("1"), "extra": "nope"}
        )
