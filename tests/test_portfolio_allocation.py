"""Contracts for the read-only BHA-79 portfolio allocation projection."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from integrations.portfolio_tracker_v1 import (
    HealthV1,
    OptionContractV1,
    PositionLotV1,
    PositionsV1Result,
    PositionV1,
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


def _covered_call_book(
    *, account_id: int = 1, units: Decimal = Decimal(-100)
) -> tuple[PositionsV1Result, SecuritiesV1Result]:
    contract = OptionContractV1(
        underlying_ticker="AAAA",
        contract_type="call",
        expiration_date=date(2026, 11, 20),
        strike_price=Decimal(150),
        multiplier=Decimal(100),
        metadata_source="snaptrade.option_symbol",
        multiplier_source="snaptrade.option_symbol",
    )
    call = PositionV1(
        security_id=10,
        ticker="AAAA261120C00150000",
        name="Alpha call",
        quantity=units,
        market_value=Decimal(-200),
        cost_basis=None,
        unrealized_pnl=None,
        percent_of_portfolio=Decimal(-200) / Decimal(19800) * 100,
        option_contract=contract,
        quantity_unit="underlying_units",
        contract_quantity=units / 100,
        accounts=[
            PositionLotV1(
                account_id=account_id,
                account_name="Synthetic account",
                quantity=units,
                quantity_unit="underlying_units",
                contract_quantity=units / 100,
                market_value=Decimal(-200),
                cost_basis=None,
                cost_basis_source=None,
                tax_treatment="roth",
            )
        ],
    )
    base = _positions()
    rows = [
        row.model_copy(
            update={
                "percent_of_portfolio": row.market_value / Decimal(19800) * 100,
            }
        )
        for row in base.positions
        if row.market_value is not None
    ]
    positions = base.model_copy(
        update={"positions": [*rows, call], "total_market_value": Decimal(19800)}
    )
    securities = _classified_securities()
    option = securities.securities[0].model_copy(
        update={
            "security_id": 10,
            "ticker": call.ticker,
            "asset_type": "Other",
            "type": "option",
            "option_contract": contract,
        }
    )
    return positions, securities.model_copy(update={"securities": [*securities.securities, option]})


def test_signed_covered_call_preserves_net_allocation_and_stock_capital() -> None:
    from integrations.portfolio_allocation import read_portfolio_allocation
    from integrations.portfolio_position import validate_positions_snapshot

    positions, securities = _covered_call_book()
    rejected = validate_positions_snapshot(positions)
    assert rejected is not None and rejected[0] == "negative_quantity_unsupported"
    assert (
        validate_positions_snapshot(positions, allowed_short_security_ids=frozenset({10})) is None
    )
    result = read_portfolio_allocation(_reader(positions=positions, securities=securities))
    assert result.state == "available"
    assert result.buckets.us_equity.value == Decimal(13000)
    assert result.reconciliation.bucket_total == Decimal(19800)
    exposure = result.covered_calls[0]
    assert exposure.gross_stock_capital == Decimal(13200)
    assert exposure.option_market_value == Decimal(-200)
    assert exposure.net_market_value == Decimal(13000)
    assert exposure.legs[0].covered_shares == Decimal(100)
    assert exposure.legs[0].uncovered_shares == 0
    assert exposure.legs[0].contracts == 1


@pytest.mark.parametrize(
    "account_id,units,covered,uncovered",
    [
        (3, Decimal(-100), Decimal(0), Decimal(100)),
        (1, Decimal(-200), Decimal(100), Decimal(100)),
    ],
)
def test_call_coverage_cannot_borrow_another_account_or_double_available_shares(
    account_id: int, units: Decimal, covered: Decimal, uncovered: Decimal
) -> None:
    from integrations.portfolio_allocation import read_portfolio_allocation

    positions, securities = _covered_call_book(account_id=account_id, units=units)
    result = read_portfolio_allocation(_reader(positions=positions, securities=securities))
    assert result.state == "incomplete"
    assert "option_call_not_fully_covered" in result.reason_codes
    assert result.covered_calls[0].legs[0].covered_shares == covered
    assert result.covered_calls[0].legs[0].uncovered_shares == uncovered
    assert result.reconciliation.is_reconciled


@pytest.mark.parametrize(
    "field,value,code",
    [
        ("option_contract", None, "option_metadata_incomplete"),
        ("quantity_unit", "unknown", "option_quantity_or_metadata_unproven"),
    ],
)
def test_unsupported_option_evidence_is_signed_unclassified_not_zero_or_rejected(
    field: str, value: object, code: str
) -> None:
    from integrations.portfolio_allocation import read_portfolio_allocation

    positions, securities = _covered_call_book()
    positions.positions[-1] = positions.positions[-1].model_copy(update={field: value})
    result = read_portfolio_allocation(_reader(positions=positions, securities=securities))
    assert result.state == "incomplete"
    assert code in result.reason_codes
    assert result.buckets.unclassified.value == Decimal(-200)
    assert result.reconciliation.bucket_total == Decimal(19800)
    assert result.covered_calls == ()


def test_multiple_calls_share_one_same_account_coverage_budget() -> None:
    from integrations.portfolio_allocation import read_portfolio_allocation

    positions, securities = _covered_call_book()
    second = positions.positions[-1].model_copy(update={"security_id": 11})
    total = Decimal(19600)
    rows = [*positions.positions, second]
    rows = [
        row.model_copy(update={"percent_of_portfolio": row.market_value / total * 100})
        for row in rows
        if row.market_value is not None
    ]
    positions = positions.model_copy(update={"positions": rows, "total_market_value": total})
    option = securities.securities[-1].model_copy(update={"security_id": 11})
    securities = securities.model_copy(update={"securities": [*securities.securities, option]})
    result = read_portfolio_allocation(_reader(positions=positions, securities=securities))
    assert sum(leg.covered_shares for leg in result.covered_calls[0].legs) == 100
    assert "option_call_not_fully_covered" in result.reason_codes


def test_known_underlying_units_do_not_require_an_invented_multiplier() -> None:
    from integrations.portfolio_allocation import read_portfolio_allocation

    positions, securities = _covered_call_book()
    contract = securities.securities[-1].option_contract
    assert contract is not None
    contract = contract.model_copy(update={"multiplier": None, "multiplier_source": None})
    securities.securities[-1] = securities.securities[-1].model_copy(
        update={"option_contract": contract}
    )
    positions.positions[-1] = positions.positions[-1].model_copy(
        update={"option_contract": contract}
    )
    result = read_portfolio_allocation(_reader(positions=positions, securities=securities))
    assert result.state == "available"
    assert result.covered_calls[0].legs[0].covered_shares == 100
    assert result.covered_calls[0].legs[0].contracts is None


def test_put_and_unmapped_call_remain_incomplete_signed_values() -> None:
    from integrations.portfolio_allocation import read_portfolio_allocation

    for update, reason in [
        ({"contract_type": "put"}, "option_strategy_unsupported"),
        ({"underlying_ticker": "MISSING"}, "option_underlying_unmapped"),
    ]:
        positions, securities = _covered_call_book()
        contract = securities.securities[-1].option_contract
        assert contract is not None
        changed = contract.model_copy(update=update)
        securities.securities[-1] = securities.securities[-1].model_copy(
            update={"option_contract": changed}
        )
        positions.positions[-1] = positions.positions[-1].model_copy(
            update={"option_contract": changed}
        )
        result = read_portfolio_allocation(_reader(positions=positions, securities=securities))
        assert result.state == "incomplete"
        assert reason in result.reason_codes
        assert result.buckets.unclassified.value == -200
        assert result.reconciliation.is_reconciled


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


@pytest.mark.parametrize("account_id", [4, 99])
def test_rejects_position_lot_outside_envelope_account_coverage(account_id: int) -> None:
    from integrations.portfolio_allocation import read_portfolio_allocation

    base = _positions()
    altered_lot = base.positions[0].accounts[0].model_copy(update={"account_id": account_id})
    altered_position = base.positions[0].model_copy(
        update={"accounts": [altered_lot, *base.positions[0].accounts[1:]]}
    )
    positions = base.model_copy(update={"positions": [altered_position, *base.positions[1:]]})

    result = read_portfolio_allocation(_reader(positions=positions))

    assert result.state == "unavailable"
    assert result.reason_codes == ("account_coverage_invalid",)


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
