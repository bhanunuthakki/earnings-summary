"""Hermetic BHA-74/BHA-80 contracts for the portfolio foundation."""

from __future__ import annotations

import ctypes
import json
from collections.abc import Callable
from datetime import UTC, date, datetime
from decimal import Decimal
from io import StringIO
from pathlib import Path
from typing import Any, Literal, cast

import pytest

from integrations.portfolio_tracker_v1 import (
    HealthV1,
    PortfolioSnapshotV1,
    PositionsV1Result,
    V1Fetch,
)


def _health(*, stale: bool = False) -> HealthV1:
    return HealthV1.model_validate(
        {
            "status": "ok",
            "schema_version": "1.0.0",
            "generated_at": "2026-08-20T12:00:00Z",
            "database_ok": True,
            "migration_version": "0023",
            "providers": [],
            "active_account_count": 3,
            "latest_snapshot_date": "2026-08-20",
            "is_stale": stale,
            "links": {},
        }
    )


def _positions(*, ticker: str = "MELI", duplicate_account: bool = False) -> PositionsV1Result:
    accounts = [
        {
            "account_id": 1,
            "account_name": "Brokerage",
            "quantity": "55.256834",
            "market_value": "101871.698219",
            "cost_basis": "78911.161003",
            "cost_basis_source": "broker",
            "tax_treatment": "taxable",
        }
    ]
    if duplicate_account:
        accounts.append({**accounts[0], "account_name": "Duplicate"})
    return PositionsV1Result.model_validate(
        {
            "snapshot_date": "2026-08-20",
            "total_market_value": "101871.698219",
            "positions": [
                {
                    "security_id": 1,
                    "ticker": ticker,
                    "name": "MercadoLibre",
                    "quantity": "55.256834",
                    "market_value": "101871.698219",
                    "cost_basis": "78911.161003",
                    "unrealized_pnl": "22960.537216",
                    "percent_of_portfolio": "100",
                    "accounts": accounts,
                }
            ],
            "by_tax_treatment": {},
            "notes": [],
        }
    )


def _snapshot_from_positions(positions: PositionsV1Result) -> PortfolioSnapshotV1:
    """Build the full typed envelope used by the adapter test client."""
    payload = json.loads(
        (Path(__file__).parent / "fixtures/tracker_v1/portfolio-snapshot.json").read_text()
    )
    snapshot = PortfolioSnapshotV1.model_validate(payload)
    return snapshot.model_copy(
        update={
            "accounts": [
                account.model_copy(update={"holdings_as_of": positions.snapshot_date})
                for account in snapshot.accounts
            ],
            "meta": snapshot.meta.model_copy(update={"as_of": positions.snapshot_date}),
            "equity_fraction": snapshot.equity_fraction.model_copy(
                update={
                    "holdings_as_of": positions.snapshot_date,
                    "equity_value": positions.total_market_value,
                    "denominator_value": positions.total_market_value,
                    "equity_fraction": Decimal("1"),
                }
            ),
            "total_market_value": positions.total_market_value,
            "positions": positions.positions,
            "by_tax_treatment": positions.by_tax_treatment,
        }
    )


class _Client:
    def __init__(self, health: V1Fetch[HealthV1], positions: V1Fetch[PositionsV1Result]) -> None:
        self._health = health
        self._positions = positions
        self._snapshot = V1Fetch(
            available=positions.available,
            endpoint="/api/v1/portfolio-snapshot",
            data=_snapshot_from_positions(positions.data) if positions.data is not None else None,
            error=positions.error,
        )

    def probe_v1(self) -> V1Fetch[HealthV1]:
        return self._health

    def get_portfolio_snapshot(self) -> V1Fetch[PortfolioSnapshotV1]:
        return self._snapshot


def test_canonical_adapter_preserves_held_not_held_and_unavailable() -> None:
    from integrations.portfolio_position import PortfolioPositionAdapter

    healthy = V1Fetch(available=True, endpoint="/api/v1/health", data=_health())
    held = PortfolioPositionAdapter(
        _Client(
            healthy,
            V1Fetch(available=True, endpoint="/api/v1/portfolio/positions", data=_positions()),
        )
    ).resolve("meli")
    not_held = PortfolioPositionAdapter(
        _Client(
            healthy,
            V1Fetch(
                available=True, endpoint="/api/v1/portfolio/positions", data=_positions(ticker="NU")
            ),
        )
    ).resolve("MELI")
    unavailable = PortfolioPositionAdapter(
        _Client(
            V1Fetch(available=False, endpoint="/api/v1/health", error="connection_refused"),
            V1Fetch(
                available=False, endpoint="/api/v1/portfolio/positions", error="connection_refused"
            ),
        )
    ).resolve("MELI")

    assert held.state == "held"
    assert held.total_quantity == pytest.approx(55.256834)
    assert held.total_cost_basis == pytest.approx(78911.161003)
    assert held.provenance is not None
    assert held.provenance.snapshot_as_of == date(2026, 8, 20)
    assert held.provenance.account_coverage == 1
    assert not_held.state == "not_held"
    assert unavailable.state == "source_unavailable"
    assert held.provenance is not None
    assert held.provenance.schema_version == "1.0.0"
    assert held.provenance.currency == "USD"
    assert held.provenance.snapshot_account_coverage == 3
    assert held.provenance.excluded_account_ids == [4]
    assert any(warning.code == "NO_CANONICAL_LINK" for warning in held.provenance.warnings)


def test_canonical_adapter_rejects_official_partial_snapshot_fixture() -> None:
    from integrations.portfolio_position import PortfolioPositionAdapter

    partial = PortfolioSnapshotV1.model_validate_json(
        (Path(__file__).parent / "fixtures/tracker_v1/portfolio-snapshot.partial.json").read_bytes()
    )
    partial_health = _health().model_copy(update={"latest_snapshot_date": partial.meta.as_of})

    class _PartialClient:
        def probe_v1(self) -> V1Fetch[HealthV1]:
            return V1Fetch(available=True, endpoint="/health", data=partial_health)

        def get_portfolio_snapshot(self) -> V1Fetch[PortfolioSnapshotV1]:
            return V1Fetch(available=True, endpoint="/portfolio-snapshot", data=partial)

    result = PortfolioPositionAdapter(_PartialClient()).resolve("AAAA")

    assert result.state == "source_unavailable"
    assert result.error_code == "portfolio_snapshot_partial"
    assert result.provenance is not None
    assert result.provenance.is_partial is True


def test_canonical_adapter_fails_closed_on_untruthful_lagging_coverage() -> None:
    from integrations.portfolio_position import PortfolioPositionAdapter

    base = _snapshot_from_positions(_positions(ticker="NU"))
    lagging = base.model_copy(
        update={
            "meta": base.meta.model_copy(
                update={
                    "account_coverage": base.meta.account_coverage.model_copy(
                        update={"lagging_account_ids": [1]}
                    )
                }
            )
        }
    )

    class _LaggingClient:
        def probe_v1(self) -> V1Fetch[HealthV1]:
            return V1Fetch(available=True, endpoint="/health", data=_health())

        def get_portfolio_snapshot(self) -> V1Fetch[PortfolioSnapshotV1]:
            return V1Fetch(available=True, endpoint="/portfolio-snapshot", data=lagging)

    result = PortfolioPositionAdapter(_LaggingClient()).resolve("MELI")

    assert result.state == "source_unavailable"
    assert result.error_code == "portfolio_snapshot_account_coverage_lagging"
    assert result.provenance is not None
    assert result.provenance.lagging_account_ids == [1]
    assert result.provenance.is_partial is True

    unlisted = lagging.model_copy(
        update={
            "meta": lagging.meta.model_copy(
                update={
                    "account_coverage": lagging.meta.account_coverage.model_copy(
                        update={"lagging_account_ids": []}
                    )
                }
            ),
            "accounts": [
                lagging.accounts[0].model_copy(update={"holdings_as_of": date(2026, 8, 19)}),
                *lagging.accounts[1:],
            ],
        }
    )

    class _UnlistedLaggingClient(_LaggingClient):
        def get_portfolio_snapshot(self) -> V1Fetch[PortfolioSnapshotV1]:
            return V1Fetch(available=True, endpoint="/portfolio-snapshot", data=unlisted)

    unlisted_result = PortfolioPositionAdapter(_UnlistedLaggingClient()).resolve("MELI")
    assert unlisted_result.state == "source_unavailable"
    assert unlisted_result.error_code == "account_coverage_invalid"


def test_canonical_adapter_rejects_snapshot_with_no_observation_date() -> None:
    from integrations.portfolio_position import PortfolioPositionAdapter

    base = _snapshot_from_positions(_positions(ticker="NU"))
    snapshot = base.model_copy(
        update={
            "meta": base.meta.model_copy(update={"as_of": None}),
            "equity_fraction": base.equity_fraction.model_copy(update={"holdings_as_of": None}),
        }
    )
    health = _health().model_copy(update={"latest_snapshot_date": None})

    class _NoDateClient:
        def probe_v1(self) -> V1Fetch[HealthV1]:
            return V1Fetch(available=True, endpoint="/health", data=health)

        def get_portfolio_snapshot(self) -> V1Fetch[PortfolioSnapshotV1]:
            return V1Fetch(available=True, endpoint="/portfolio-snapshot", data=snapshot)

    result = PortfolioPositionAdapter(_NoDateClient()).resolve("MELI")
    assert result.state == "source_unavailable"
    assert result.error_code == "snapshot_date_missing"


def test_report_surfaces_lagging_account_ids_without_not_held_copy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from integrations.portfolio_position import PortfolioPositionResult, PositionProvenance
    from report.models import ReportSpec, SectionStatus
    from report.renderers.markdown import _portfolio_position  # pyright: ignore[reportPrivateUsage]
    from report.renderers.workspace_sections.position import _position_tab
    from report.sections import portfolio_position

    def lagging_position(_ticker: str) -> PortfolioPositionResult:
        return PortfolioPositionResult(
            state="source_unavailable",
            error_code="portfolio_snapshot_account_coverage_lagging",
            error_detail="current held/not-held status is unproven",
            provenance=PositionProvenance(
                source_identity="test-source",
                snapshot_as_of=date(2026, 8, 20),
                account_coverage=0,
                lagging_account_ids=[1],
                is_stale=False,
                is_partial=True,
            ),
        )

    monkeypatch.setattr(portfolio_position, "resolve_configured_position", lagging_position)
    section = portfolio_position.build("MELI", Path("detached-checkout"))
    assert section.status is SectionStatus.MISSING_DATA
    assert section.source_lagging_account_ids == [1]

    html = StringIO()
    _position_tab(html, section, ticker="MELI")
    rendered_html = html.getvalue()
    assert "LAGGING_ACCOUNT_COVERAGE" in rendered_html
    assert "account IDs 1" in rendered_html
    assert "shows no position in this name" not in rendered_html

    markdown = StringIO()
    _portfolio_position(
        markdown,
        ReportSpec.model_construct(
            ticker="MELI",
            generation_date=date(2026, 8, 20),
            repo_root=".",
            portfolio_position=section,
        ),
    )
    rendered_markdown = markdown.getvalue()
    assert "Lagging account coverage" in rendered_markdown
    assert "account IDs 1" in rendered_markdown
    assert "NOT HELD" not in rendered_markdown.upper()


def test_canonical_adapter_requires_explicit_consistent_currency() -> None:
    from integrations.portfolio_position import PortfolioPositionAdapter

    snapshot = _snapshot_from_positions(_positions())
    account = snapshot.accounts[0].model_copy(update={"value_currency": "EUR"})
    mismatched = snapshot.model_copy(update={"accounts": [account, *snapshot.accounts[1:]]})

    class _CurrencyClient:
        def probe_v1(self) -> V1Fetch[HealthV1]:
            return V1Fetch(available=True, endpoint="/health", data=_health())

        def get_portfolio_snapshot(self) -> V1Fetch[PortfolioSnapshotV1]:
            return V1Fetch(available=True, endpoint="/portfolio-snapshot", data=mismatched)

    result = PortfolioPositionAdapter(_CurrencyClient()).resolve("MELI")

    assert result.state == "source_unavailable"
    assert result.error_code == "currency_mismatch"


def test_canonical_adapter_fails_closed_on_official_stale_envelope_state() -> None:
    from integrations.portfolio_position import PortfolioPositionAdapter

    stale = PortfolioSnapshotV1.model_validate_json(
        (Path(__file__).parent / "fixtures/tracker_v1/portfolio-snapshot.stale.json").read_bytes()
    )
    stale_health = _health().model_copy(update={"latest_snapshot_date": stale.meta.as_of})

    class _StaleClient:
        def probe_v1(self) -> V1Fetch[HealthV1]:
            return V1Fetch(available=True, endpoint="/health", data=stale_health)

        def get_portfolio_snapshot(self) -> V1Fetch[PortfolioSnapshotV1]:
            return V1Fetch(available=True, endpoint="/portfolio-snapshot", data=stale)

    result = PortfolioPositionAdapter(_StaleClient()).resolve("AAAA")

    assert result.state == "source_unavailable"
    assert result.error_code == "portfolio_snapshot_stale"
    assert result.provenance is not None and result.provenance.is_stale is True


def test_canonical_adapter_rejects_invalid_equity_unit_and_date() -> None:
    from integrations.portfolio_position import PortfolioPositionAdapter

    snapshot = _snapshot_from_positions(_positions())
    invalid = snapshot.model_copy(
        update={
            "equity_fraction": snapshot.equity_fraction.model_copy(
                update={"unit": "ratio", "holdings_as_of": date(2026, 8, 19)}
            )
        }
    )

    class _InvalidEquityClient:
        def probe_v1(self) -> V1Fetch[HealthV1]:
            return V1Fetch(available=True, endpoint="/health", data=_health())

        def get_portfolio_snapshot(self) -> V1Fetch[PortfolioSnapshotV1]:
            return V1Fetch(available=True, endpoint="/portfolio-snapshot", data=invalid)

    result = PortfolioPositionAdapter(_InvalidEquityClient()).resolve("MELI")

    assert result.state == "source_unavailable"
    assert result.error_code == "equity_unit_invalid"

    invalid_date = snapshot.model_copy(
        update={
            "equity_fraction": snapshot.equity_fraction.model_copy(
                update={"holdings_as_of": date(2026, 8, 19)}
            )
        }
    )

    class _InvalidDateClient(_InvalidEquityClient):
        def get_portfolio_snapshot(self) -> V1Fetch[PortfolioSnapshotV1]:
            return V1Fetch(available=True, endpoint="/portfolio-snapshot", data=invalid_date)

    dated = PortfolioPositionAdapter(_InvalidDateClient()).resolve("MELI")
    assert dated.state == "source_unavailable"
    assert dated.error_code == "equity_snapshot_date_mismatch"


@pytest.mark.parametrize(
    ("field", "value", "expected"),
    (
        ("denominator_value", 0, "equity_denominator_invalid"),
        ("denominator_value", -1, "equity_denominator_invalid"),
        ("equity_fraction", None, "equity_fraction_missing"),
        ("equity_value", -1, "equity_value_invalid"),
    ),
)
def test_canonical_adapter_rejects_invalid_equity_denominator_and_values(
    field: str, value: object, expected: str
) -> None:
    from integrations.portfolio_position import PortfolioPositionAdapter

    snapshot = _snapshot_from_positions(_positions())
    invalid = snapshot.model_copy(
        update={"equity_fraction": snapshot.equity_fraction.model_copy(update={field: value})}
    )

    class _InvalidEquityClient:
        def probe_v1(self) -> V1Fetch[HealthV1]:
            return V1Fetch(available=True, endpoint="/health", data=_health())

        def get_portfolio_snapshot(self) -> V1Fetch[PortfolioSnapshotV1]:
            return V1Fetch(available=True, endpoint="/portfolio-snapshot", data=invalid)

    result = PortfolioPositionAdapter(_InvalidEquityClient()).resolve("MELI")
    assert result.state == "source_unavailable"
    assert result.error_code == expected


def test_snapshot_integrity_requires_total_equity_and_exact_account_coverage() -> None:
    from integrations.portfolio_position import (
        validate_equity_evidence,
        validate_snapshot_account_coverage,
    )

    base = _snapshot_from_positions(_positions())
    assert validate_snapshot_account_coverage(base, _health()) is None

    total_mismatch = base.model_copy(update={"total_market_value": 19_999})
    assert validate_equity_evidence(total_mismatch) == (
        "equity_denominator_mismatch",
        "equity denominator does not match the portfolio total market value",
    )
    sub_micro_mismatch = base.model_copy(
        update={"total_market_value": base.total_market_value + Decimal("0.0000005")}
    )
    assert validate_equity_evidence(sub_micro_mismatch) is not None

    duplicate_account = base.model_copy(
        update={"accounts": [base.accounts[0], base.accounts[0], *base.accounts[2:]]}
    )
    assert validate_snapshot_account_coverage(duplicate_account, _health()) == (
        "account_coverage_invalid",
        "portfolio snapshot repeats an account id",
    )

    equity_coverage_mismatch = base.model_copy(
        update={
            "equity_fraction": base.equity_fraction.model_copy(
                update={"included_account_ids": [1, 2]}
            )
        }
    )
    assert validate_snapshot_account_coverage(equity_coverage_mismatch, _health()) == (
        "equity_account_coverage_invalid",
        "equity included accounts must exactly match included active envelope accounts",
    )


def _snapshot_with_empty_account() -> PortfolioSnapshotV1:
    """Build a complete book with MELI held in account 10 and account 19 empty."""
    base = _snapshot_from_positions(_positions())
    account_10 = base.accounts[3].model_copy(update={"account_id": 10, "name": "MELI brokerage"})
    account_19 = base.accounts[1].model_copy(
        update={
            "account_id": 19,
            "name": "Empty 401(k)",
            "active": True,
            "included_in_totals": True,
            "exclusion_reason": None,
            "value": None,
            "holdings_as_of": None,
            "last_successful_sync_at": None,
            "warnings": [],
        }
    )
    position = base.positions[0]
    lot = position.accounts[0].model_copy(
        update={"account_id": 10, "account_name": "MELI brokerage"}
    )
    included_account_ids = [2, 3, 10, 19]
    return base.model_copy(
        update={
            "accounts": [base.accounts[0], base.accounts[2], account_10, account_19],
            "meta": base.meta.model_copy(
                update={
                    "account_coverage": base.meta.account_coverage.model_copy(
                        update={"included_account_ids": included_account_ids}
                    )
                }
            ),
            "equity_fraction": base.equity_fraction.model_copy(
                update={"included_account_ids": included_account_ids}
            ),
            "positions": [position.model_copy(update={"accounts": [lot]})],
        }
    )


def _snapshot_with_four_lot_meli_and_partial_unrelated_cost(
    *, meli_partial_cost: bool = False
) -> tuple[PortfolioSnapshotV1, HealthV1]:
    """Build a structurally valid book with partial cost only off the requested ticker."""
    base = _snapshot_from_positions(_positions())
    source_position = base.positions[0]
    lot_values = (
        (Decimal("10"), Decimal("25000"), Decimal("18000")),
        (Decimal("20"), Decimal("30000"), Decimal("22000")),
        (Decimal("15.256834"), Decimal("20000"), Decimal("15000")),
        (Decimal("10"), Decimal("26871.698219"), Decimal("23911.161003")),
    )
    meli_lots = [
        source_position.accounts[0].model_copy(
            update={
                "account_id": account_id,
                "account_name": f"Account {account_id}",
                "quantity": quantity,
                "market_value": market_value,
                "cost_basis": None if meli_partial_cost and account_id == 1 else cost_basis,
            }
        )
        for account_id, (quantity, market_value, cost_basis) in enumerate(lot_values, start=1)
    ]
    meli = source_position.model_copy(update={"accounts": meli_lots})
    unrelated = []
    for index in range(6):
        account_id = (index % 4) + 1
        lot = source_position.accounts[0].model_copy(
            update={
                "account_id": account_id,
                "account_name": f"Account {account_id}",
                "quantity": Decimal("1"),
                "market_value": Decimal("10"),
                "cost_basis": None,
            }
        )
        unrelated.append(
            source_position.model_copy(
                update={
                    "security_id": 100 + index,
                    "ticker": f"OTHER{index}",
                    "quantity": Decimal("1"),
                    "market_value": Decimal("10"),
                    "cost_basis": Decimal("5"),
                    "unrealized_pnl": Decimal("5"),
                    "percent_of_portfolio": Decimal("0"),
                    "accounts": [lot],
                }
            )
        )
    total_market_value = Decimal("101931.698219")
    active_accounts = [
        base.accounts[0],
        base.accounts[2],
        base.accounts[3],
        base.accounts[1].model_copy(
            update={
                "account_id": 4,
                "active": True,
                "included_in_totals": True,
                "exclusion_reason": None,
                "warnings": [],
            }
        ),
    ]
    included_account_ids = [1, 2, 3, 4]
    snapshot = base.model_copy(
        update={
            "accounts": active_accounts,
            "meta": base.meta.model_copy(
                update={
                    "account_coverage": base.meta.account_coverage.model_copy(
                        update={
                            "included_account_ids": included_account_ids,
                            "excluded_account_ids": [],
                        }
                    )
                }
            ),
            "equity_fraction": base.equity_fraction.model_copy(
                update={
                    "included_account_ids": included_account_ids,
                    "excluded_account_ids": [],
                    "equity_value": total_market_value,
                    "denominator_value": total_market_value,
                    "equity_fraction": Decimal("1"),
                }
            ),
            "total_market_value": total_market_value,
            "positions": [meli, *unrelated],
        }
    )
    return snapshot, _health().model_copy(update={"active_account_count": 4})


def _zero_meli_snapshot(
    *,
    aggregate_cost: Decimal | None = None,
    lot_cost: Decimal | None = None,
    aggregate_pnl: Decimal | None = None,
) -> tuple[PortfolioSnapshotV1, HealthV1]:
    snapshot, health = _snapshot_with_four_lot_meli_and_partial_unrelated_cost()
    meli = snapshot.positions[0]
    zero_lots = [
        lot.model_copy(
            update={
                "quantity": Decimal("0"),
                "market_value": Decimal("0"),
                "cost_basis": lot_cost if index == 0 else None,
            }
        )
        for index, lot in enumerate(meli.accounts)
    ]
    zero_meli = meli.model_copy(
        update={
            "quantity": Decimal("0"),
            "market_value": Decimal("0"),
            "cost_basis": aggregate_cost,
            "unrealized_pnl": aggregate_pnl,
            "percent_of_portfolio": Decimal("0"),
            "accounts": zero_lots,
        }
    )
    total_market_value = Decimal("60")
    return (
        snapshot.model_copy(
            update={
                "total_market_value": total_market_value,
                "positions": [zero_meli, *snapshot.positions[1:]],
                "equity_fraction": snapshot.equity_fraction.model_copy(
                    update={
                        "equity_value": total_market_value,
                        "denominator_value": total_market_value,
                    }
                ),
            }
        ),
        health,
    )


def test_complete_current_snapshot_accepts_empty_included_account_without_hiding_meli() -> None:
    from integrations.portfolio_position import (
        PortfolioPositionAdapter,
        validate_snapshot_account_coverage,
    )

    snapshot = _snapshot_with_empty_account()
    health = _health().model_copy(update={"active_account_count": 4})
    assert validate_snapshot_account_coverage(snapshot, health) is None

    class _EmptyAccountClient:
        def probe_v1(self) -> V1Fetch[HealthV1]:
            return V1Fetch(available=True, endpoint="/health", data=health)

        def get_portfolio_snapshot(self) -> V1Fetch[PortfolioSnapshotV1]:
            return V1Fetch(available=True, endpoint="/portfolio-snapshot", data=snapshot)

    result = PortfolioPositionAdapter(_EmptyAccountClient()).resolve("MELI")
    assert result.state == "held"
    assert result.total_quantity == pytest.approx(55.256834)
    assert result.accounts[0].account_name == "MELI brokerage"
    assert result.provenance is not None
    assert result.provenance.included_account_ids == [2, 3, 10, 19]


@pytest.mark.parametrize(
    ("snapshot_update", "expected_detail"),
    (
        (
            {"meta": {"is_partial": True}},
            "empty account 19 is not valid in a partial or stale envelope",
        ),
        (
            {"meta": {"is_stale": True}},
            "empty account 19 is not valid in a partial or stale envelope",
        ),
        (
            {"equity_fraction": {"is_partial": True}},
            "empty account 19 is not valid in a partial or stale envelope",
        ),
        (
            {"equity_fraction": {"is_stale": True}},
            "empty account 19 is not valid in a partial or stale envelope",
        ),
    ),
)
def test_empty_included_account_requires_complete_current_non_lagging_envelope(
    snapshot_update: dict[str, dict[str, bool]], expected_detail: str
) -> None:
    from integrations.portfolio_position import validate_snapshot_account_coverage

    base = _snapshot_with_empty_account()
    if "meta" in snapshot_update:
        snapshot = base.model_copy(
            update={"meta": base.meta.model_copy(update=snapshot_update["meta"])}
        )
    else:
        snapshot = base.model_copy(
            update={
                "equity_fraction": base.equity_fraction.model_copy(
                    update=snapshot_update["equity_fraction"]
                )
            }
        )
    error = validate_snapshot_account_coverage(
        snapshot, _health().model_copy(update={"active_account_count": 4})
    )
    assert error == ("account_coverage_invalid", expected_detail)


def test_empty_included_account_rejects_one_sided_nulls_and_lagging_state() -> None:
    from integrations.portfolio_position import validate_snapshot_account_coverage

    base = _snapshot_with_empty_account()
    health = _health().model_copy(update={"active_account_count": 4})
    dated_empty = base.model_copy(
        update={
            "accounts": [
                *base.accounts[:3],
                base.accounts[3].model_copy(update={"holdings_as_of": date(2026, 8, 19)}),
            ]
        }
    )
    assert validate_snapshot_account_coverage(dated_empty, health) == (
        "account_coverage_invalid",
        "active account 19 holdings date is not covered by the portfolio snapshot envelope",
    )

    valued_without_date = base.model_copy(
        update={
            "accounts": [
                *base.accounts[:3],
                base.accounts[3].model_copy(update={"value": Decimal("1")}),
            ]
        }
    )
    assert validate_snapshot_account_coverage(valued_without_date, health) == (
        "account_coverage_invalid",
        "active account 19 has value without a holdings date",
    )

    lagging = base.model_copy(
        update={
            "meta": base.meta.model_copy(
                update={
                    "account_coverage": base.meta.account_coverage.model_copy(
                        update={"lagging_account_ids": [19]}
                    )
                }
            )
        }
    )
    assert validate_snapshot_account_coverage(lagging, health) == (
        "account_coverage_invalid",
        "empty included account 19 cannot be marked lagging",
    )


def test_unrelated_partial_cost_does_not_suppress_meli_or_not_held_evidence() -> None:
    from integrations.portfolio_position import PortfolioPositionAdapter

    snapshot, health = _snapshot_with_four_lot_meli_and_partial_unrelated_cost()

    class _PartialUnrelatedCostClient:
        def probe_v1(self) -> V1Fetch[HealthV1]:
            return V1Fetch(available=True, endpoint="/health", data=health)

        def get_portfolio_snapshot(self) -> V1Fetch[PortfolioSnapshotV1]:
            return V1Fetch(available=True, endpoint="/portfolio-snapshot", data=snapshot)

    adapter = PortfolioPositionAdapter(_PartialUnrelatedCostClient())
    held = adapter.resolve("MELI")
    assert held.state == "held"
    assert held.total_quantity == pytest.approx(55.256834)
    assert held.total_cost_basis == pytest.approx(78911.161003)
    assert len(held.accounts) == 4

    not_held = adapter.resolve("NOT_HELD")
    assert not_held.state == "not_held"


def test_partial_cost_on_matching_meli_remains_unavailable() -> None:
    from integrations.portfolio_position import PortfolioPositionAdapter

    snapshot, health = _snapshot_with_four_lot_meli_and_partial_unrelated_cost(
        meli_partial_cost=True
    )

    class _PartialMeliCostClient:
        def probe_v1(self) -> V1Fetch[HealthV1]:
            return V1Fetch(available=True, endpoint="/health", data=health)

        def get_portfolio_snapshot(self) -> V1Fetch[PortfolioSnapshotV1]:
            return V1Fetch(available=True, endpoint="/portfolio-snapshot", data=snapshot)

    result = PortfolioPositionAdapter(_PartialMeliCostClient()).resolve("MELI")
    assert result.state == "source_unavailable"
    assert result.error_code == "position_lot_reconciliation_failed"


@pytest.mark.parametrize(
    ("aggregate_cost", "lot_cost", "aggregate_pnl"),
    (
        (Decimal("1"), None, None),
        (None, Decimal("1"), None),
        (Decimal("1"), Decimal("1"), Decimal("-1")),
    ),
)
def test_zero_quantity_meli_with_residual_cost_or_pnl_is_unavailable(
    aggregate_cost: Decimal | None, lot_cost: Decimal | None, aggregate_pnl: Decimal | None
) -> None:
    from integrations.portfolio_position import PortfolioPositionAdapter

    snapshot, health = _zero_meli_snapshot(
        aggregate_cost=aggregate_cost, lot_cost=lot_cost, aggregate_pnl=aggregate_pnl
    )

    class _ResidualZeroClient:
        def probe_v1(self) -> V1Fetch[HealthV1]:
            return V1Fetch(available=True, endpoint="/health", data=health)

        def get_portfolio_snapshot(self) -> V1Fetch[PortfolioSnapshotV1]:
            return V1Fetch(available=True, endpoint="/portfolio-snapshot", data=snapshot)

    result = PortfolioPositionAdapter(_ResidualZeroClient()).resolve("MELI")
    assert result.state == "source_unavailable"
    assert result.error_code == "position_lot_reconciliation_failed"


def test_clean_zero_quantity_meli_is_not_held() -> None:
    from integrations.portfolio_position import PortfolioPositionAdapter

    snapshot, health = _zero_meli_snapshot()

    class _CleanZeroClient:
        def probe_v1(self) -> V1Fetch[HealthV1]:
            return V1Fetch(available=True, endpoint="/health", data=health)

        def get_portfolio_snapshot(self) -> V1Fetch[PortfolioSnapshotV1]:
            return V1Fetch(available=True, endpoint="/portfolio-snapshot", data=snapshot)

    result = PortfolioPositionAdapter(_CleanZeroClient()).resolve("MELI")
    assert result.state == "not_held"


def test_canonical_adapter_rejects_position_lot_outside_active_envelope_accounts() -> None:
    from integrations.portfolio_position import PortfolioPositionAdapter

    snapshot = _snapshot_from_positions(_positions())
    position = snapshot.positions[0]
    lot = position.accounts[0].model_copy(update={"account_id": 4})
    invalid = snapshot.model_copy(
        update={
            "meta": snapshot.meta.model_copy(
                update={
                    "account_coverage": snapshot.meta.account_coverage.model_copy(
                        update={"included_account_ids": [1, 2, 3, 4], "excluded_account_ids": []}
                    )
                }
            ),
            "equity_fraction": snapshot.equity_fraction.model_copy(
                update={"included_account_ids": [1, 2, 3], "excluded_account_ids": []}
            ),
            "positions": [position.model_copy(update={"accounts": [lot]})],
        }
    )

    class _CoverageClient:
        def probe_v1(self) -> V1Fetch[HealthV1]:
            return V1Fetch(available=True, endpoint="/health", data=_health())

        def get_portfolio_snapshot(self) -> V1Fetch[PortfolioSnapshotV1]:
            return V1Fetch(available=True, endpoint="/portfolio-snapshot", data=invalid)

    result = PortfolioPositionAdapter(_CoverageClient()).resolve("MELI")

    assert result.state == "source_unavailable"
    assert result.error_code == "position_account_inactive_or_excluded"


def test_canonical_adapter_rejects_duplicate_account_evidence() -> None:
    from integrations.portfolio_position import PortfolioPositionAdapter

    result = PortfolioPositionAdapter(
        _Client(
            V1Fetch(available=True, endpoint="/api/v1/health", data=_health()),
            V1Fetch(
                available=True,
                endpoint="/api/v1/portfolio/positions",
                data=_positions(duplicate_account=True),
            ),
        )
    ).resolve("MELI")

    assert result.state == "source_unavailable"
    assert result.error_code == "duplicate_account_snapshot"


@pytest.mark.parametrize(
    ("health_date", "position_date", "expected"),
    (
        (None, "2026-08-20", "snapshot_date_incomplete"),
        ("2026-08-20", None, "snapshot_date_incomplete"),
    ),
)
def test_canonical_adapter_fails_closed_on_one_sided_snapshot_dates(
    health_date: str | None, position_date: str | None, expected: str
) -> None:
    from integrations.portfolio_position import PortfolioPositionAdapter

    health = _health().model_copy(update={"latest_snapshot_date": health_date})
    positions = _positions().model_copy(update={"snapshot_date": position_date})
    result = PortfolioPositionAdapter(
        _Client(
            V1Fetch(available=True, endpoint="/health", data=health),
            V1Fetch(available=True, endpoint="/positions", data=positions),
        )
    ).resolve("MELI")
    assert result.state == "source_unavailable"
    assert result.error_code == expected


def test_canonical_adapter_does_not_call_stale_absence_not_held() -> None:
    from integrations.portfolio_position import PortfolioPositionAdapter

    result = PortfolioPositionAdapter(
        _Client(
            V1Fetch(available=True, endpoint="/health", data=_health(stale=True)),
            V1Fetch(available=True, endpoint="/positions", data=_positions(ticker="NU")),
        )
    ).resolve("MELI")
    assert result.state == "source_unavailable"
    assert result.error_code == "health_invalid"


@pytest.mark.parametrize("field", ("unrealized_pnl", "total_market_value"))
def test_canonical_adapter_rejects_aggregate_reconciliation_drift(field: str) -> None:
    from integrations.portfolio_position import PortfolioPositionAdapter

    positions = _positions()
    if field == "unrealized_pnl":
        item = positions.positions[0].model_copy(update={"unrealized_pnl": 1})
        positions = positions.model_copy(update={"positions": [item]})
    else:
        positions = positions.model_copy(update={"total_market_value": Decimal("1")})
    result = PortfolioPositionAdapter(
        _Client(
            V1Fetch(available=True, endpoint="/health", data=_health()),
            V1Fetch(available=True, endpoint="/positions", data=positions),
        )
    ).resolve("MELI")
    assert result.state == "source_unavailable"
    assert result.error_code in {
        "position_lot_reconciliation_failed",
        "portfolio_total_reconciliation_failed",
    }


def test_canonical_adapter_rejects_zero_quantity_with_nonzero_aggregates() -> None:
    from integrations.portfolio_position import PortfolioPositionAdapter

    item = (
        _positions()
        .positions[0]
        .model_copy(
            update={
                "quantity": 0,
                "market_value": 10,
                "cost_basis": 0,
                "unrealized_pnl": 10,
                "accounts": [
                    item_lot.model_copy(update={"quantity": 0, "market_value": 10, "cost_basis": 0})
                    for item_lot in _positions().positions[0].accounts
                ],
            }
        )
    )
    positions = _positions().model_copy(
        update={"total_market_value": Decimal("10"), "positions": [item]}
    )
    result = PortfolioPositionAdapter(
        _Client(
            V1Fetch(available=True, endpoint="/health", data=_health()),
            V1Fetch(available=True, endpoint="/positions", data=positions),
        )
    ).resolve("MELI")
    assert result.state == "source_unavailable"
    assert result.error_code == "portfolio_total_reconciliation_failed"


def test_canonical_adapter_rejects_negative_lot_quantity() -> None:
    from integrations.portfolio_position import PortfolioPositionAdapter

    item = (
        _positions()
        .positions[0]
        .model_copy(
            update={
                "accounts": [
                    item_lot.model_copy(update={"quantity": -1})
                    for item_lot in _positions().positions[0].accounts
                ]
            }
        )
    )
    positions = _positions().model_copy(update={"positions": [item]})
    result = PortfolioPositionAdapter(
        _Client(
            V1Fetch(available=True, endpoint="/health", data=_health()),
            V1Fetch(available=True, endpoint="/positions", data=positions),
        )
    ).resolve("MELI")
    assert result.state == "source_unavailable"
    assert result.error_code == "negative_quantity_unsupported"


def test_configured_immutable_snapshot_is_detached_checkout_safe(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from integrations.portfolio_position import (
        ImmutableTrackerSnapshot,
        SnapshotDecision,
        SnapshotTransaction,
        resolve_configured_position,
    )

    snapshot_path = tmp_path / "tracker-snapshot.json"
    snapshot_path.write_text(
        ImmutableTrackerSnapshot(
            source_identity="test-immutable-snapshot",
            health=_health(),
            portfolio_snapshot=_snapshot_from_positions(_positions()),
            recent_transactions=[
                SnapshotTransaction(
                    ticker="MELI",
                    date=date(2026, 8, 19),
                    account_name="Brokerage",
                    type="buy",
                    quantity=1.0,
                    amount=100.0,
                ),
                SnapshotTransaction(
                    ticker="NU",
                    date=date(2026, 8, 19),
                    account_name="Brokerage",
                    type="sell",
                    quantity=2.0,
                    amount=200.0,
                ),
            ],
            open_decisions=[
                SnapshotDecision(
                    ticker="MELI",
                    decision_date=date(2026, 8, 18),
                    action="add",
                    thesis="Preserved canonical history",
                ),
                SnapshotDecision(
                    ticker="NU",
                    decision_date=date(2026, 8, 18),
                    action="trim",
                    thesis="Other ticker history",
                ),
            ],
            closed_decisions=[
                SnapshotDecision(
                    ticker="MELI",
                    decision_date=date(2026, 8, 17),
                    action="trim",
                    thesis="Preserved closed history",
                    outcome_status="validated",
                )
            ],
        ).model_dump_json(),
        encoding="utf-8",
    )
    monkeypatch.setenv("PORTFOLIO_TRACKER_SNAPSHOT_PATH", str(snapshot_path))
    monkeypatch.delenv("PORTFOLIO_TRACKER_API_URL", raising=False)

    result = resolve_configured_position("MELI")

    assert result.state == "held"
    assert result.provenance is not None
    assert result.provenance.source_identity.startswith("test-immutable-snapshot:sha256:")
    assert len(result.recent_transactions) == 1
    assert len(result.open_decisions) == 1
    assert len(result.closed_decisions) == 1
    assert result.recent_transactions[0].ticker == "MELI"
    assert result.open_decisions[0].ticker == "MELI"


def test_configured_snapshot_is_content_addressed_and_rejects_stale_date_mismatch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from integrations.portfolio_position import (
        ImmutableTrackerSnapshot,
        resolve_configured_position,
    )

    snapshot_path = tmp_path / "tracker-snapshot.json"
    snapshot_path.write_text(
        ImmutableTrackerSnapshot(
            source_identity="test-immutable-snapshot",
            health=_health(),
            portfolio_snapshot=_snapshot_from_positions(_positions()),
        ).model_dump_json(),
        encoding="utf-8",
    )
    monkeypatch.setenv("PORTFOLIO_TRACKER_SNAPSHOT_PATH", str(snapshot_path))
    monkeypatch.delenv("PORTFOLIO_TRACKER_API_URL", raising=False)

    result = resolve_configured_position("MELI")

    assert result.state == "held"
    assert result.provenance is not None
    assert "sha256:" in result.provenance.source_identity

    stale_snapshot = ImmutableTrackerSnapshot(
        source_identity="test-immutable-snapshot",
        health=_health(),
        portfolio_snapshot=_snapshot_from_positions(_positions()),
    ).model_copy(
        update={
            "portfolio_snapshot": _snapshot_from_positions(
                _positions().model_copy(update={"snapshot_date": date(2026, 8, 19)})
            )
        }
    )
    snapshot_path.write_text(stale_snapshot.model_dump_json(), encoding="utf-8")

    mismatch = resolve_configured_position("MELI")

    assert mismatch.state == "source_unavailable"
    assert mismatch.error_code == "snapshot_date_mismatch"


def test_configured_immutable_snapshot_rejects_unsupported_schema_major(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from integrations.portfolio_position import (
        ImmutableTrackerSnapshot,
        resolve_configured_position,
    )

    snapshot = _snapshot_from_positions(_positions()).model_copy(
        update={
            "meta": _snapshot_from_positions(_positions()).meta.model_copy(
                update={"schema_version": "2.0.0"}
            )
        }
    )
    path = tmp_path / "unsupported-major.json"
    path.write_text(
        ImmutableTrackerSnapshot(
            source_identity="test-immutable-snapshot",
            health=_health(),
            portfolio_snapshot=snapshot,
        ).model_dump_json(),
        encoding="utf-8",
    )
    monkeypatch.setenv("PORTFOLIO_TRACKER_SNAPSHOT_PATH", str(path))
    monkeypatch.delenv("PORTFOLIO_TRACKER_API_URL", raising=False)

    result = resolve_configured_position("MELI")

    assert result.state == "source_unavailable"
    assert result.error_code == "incompatible_schema_version"


def test_tracker_navigation_requires_an_explicit_ui_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pipeline.ticker_command_center import tracker_url

    monkeypatch.delenv("PORTFOLIO_TRACKER_UI_URL", raising=False)
    monkeypatch.delenv("PORTFOLIO_TRACKER_URL", raising=False)
    assert tracker_url("MELI") is None
    monkeypatch.setenv("PORTFOLIO_TRACKER_UI_URL", "https://tracker.example/")
    assert tracker_url("MELI") == "https://tracker.example/holdings?ticker=MELI"


@pytest.mark.parametrize(
    ("api_url", "expected"),
    (
        ("http://127.0.0.1:8123", ("127.0.0.1", 8123)),
        ("http://[::1]:8123", ("::1", 8123)),
        ("https://127.0.0.1:9443/", None),
        ("https://tracker.example:9443/", None),
        ("http://192.168.1.5:8123", None),
        ("http://0.0.0.0:8123", None),
        ("http://[::]:8123", None),
        ("ftp://127.0.0.1:8123", None),
        ("http://user@127.0.0.1:8123", None),
        ("http://127.0.0.1:8123/api", None),
        ("http://127.0.0.1:99999", None),
    ),
)
def test_tracker_bind_parser_is_explicit_and_fail_closed(
    api_url: str, expected: tuple[str, int] | None
) -> None:
    from runtime.portfolio_tracker import parse_tracker_bind_url

    assert parse_tracker_bind_url(api_url) == expected


def test_report_keeps_unavailable_distinct_from_not_held(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from integrations.portfolio_position import PortfolioPositionResult
    from report.models import MissingReason, PortfolioPositionSection, SectionStatus
    from report.renderers.workspace_sections.position import _position_tab
    from report.sections import portfolio_position

    def unavailable_position(_: str) -> PortfolioPositionResult:
        return PortfolioPositionResult(
            state="source_unavailable", error_detail="configured tracker source is unavailable"
        )

    monkeypatch.setattr(portfolio_position, "resolve_configured_position", unavailable_position)
    section = portfolio_position.build("MELI", Path("detached-checkout"))
    assert section.status is SectionStatus.MISSING_DATA
    assert section.missing is not None

    rendered = StringIO()
    _position_tab(
        rendered,
        PortfolioPositionSection(
            status=SectionStatus.MISSING_DATA,
            missing=MissingReason(
                stage="portfolio_tracker",
                fix_command="configure PORTFOLIO_TRACKER_API_URL",
                detail="configured tracker source is unavailable",
            ),
        ),
        ticker="MELI",
    )
    assert "source unavailable" in rendered.getvalue()
    assert "shows no position in this name" not in rendered.getvalue()


def test_report_exposes_partial_history_capability_for_exited_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from integrations.portfolio_position import PortfolioPositionResult
    from report.renderers.workspace_sections.position import _position_tab
    from report.sections import portfolio_position

    def partial_history(_ticker: str) -> PortfolioPositionResult:
        return PortfolioPositionResult(
            state="not_held",
            history_state="partial",
            history_error="open and closed decision history capability is unavailable",
        )

    monkeypatch.setattr(portfolio_position, "resolve_configured_position", partial_history)
    section = portfolio_position.build("MELI", Path("detached-checkout"))
    assert section.status.value == "partial"
    rendered = StringIO()
    _position_tab(rendered, section, ticker="MELI")
    html = rendered.getvalue()
    assert "canonical transaction/open/closed history is unavailable" in html
    assert "shows no position in this name" not in html


def test_report_source_unavailable_preserves_supplied_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from integrations.portfolio_position import (
        PortfolioPositionResult,
        PositionProvenance,
        SnapshotDecision,
        SnapshotTransaction,
    )
    from integrations.portfolio_tracker_v1 import V1Warning
    from report.models import SectionStatus
    from report.sections import portfolio_position

    def unavailable_with_history(_ticker: str) -> PortfolioPositionResult:
        return PortfolioPositionResult(
            state="source_unavailable",
            error_code="portfolio_snapshot_stale",
            error_detail="source snapshot is stale",
            provenance=PositionProvenance(
                source_identity="test-source",
                snapshot_as_of=date(2026, 8, 20),
                account_coverage=1,
                is_stale=True,
                warnings=[
                    V1Warning.model_validate(
                        {
                            "code": "HISTORY_STALE",
                            "message": "Current source unavailable.",
                            "scope": "portfolio",
                            "severity": 2,
                        }
                    )
                ],
            ),
            recent_transactions=[
                SnapshotTransaction(
                    ticker="MELI",
                    date=date(2026, 8, 19),
                    account_name="Brokerage",
                    type="buy",
                    quantity=1.0,
                    amount=100.0,
                )
            ],
            open_decisions=[
                SnapshotDecision(
                    ticker="MELI",
                    decision_date=date(2026, 8, 18),
                    action="add",
                    thesis="Historical evidence",
                )
            ],
            closed_decisions=[],
            history_state="partial",
            history_error="closed decisions unavailable",
        )

    monkeypatch.setattr(portfolio_position, "resolve_configured_position", unavailable_with_history)
    section = portfolio_position.build("MELI", Path("detached-checkout"))

    assert section.status is SectionStatus.MISSING_DATA
    assert len(section.recent_transactions) == 1
    assert len(section.open_decisions) == 1
    assert section.history_error == "closed decisions unavailable"

    from report.models import ReportSpec
    from report.renderers.markdown import _portfolio_position  # pyright: ignore[reportPrivateUsage]

    markdown = StringIO()
    _portfolio_position(
        markdown,
        ReportSpec.model_construct(
            ticker="MELI",
            generation_date=date(2026, 8, 20),
            repo_root=".",
            portfolio_position=section,
        ),
    )
    rendered = markdown.getvalue()
    assert "current source unavailable" in rendered
    assert rendered.count("HISTORY_STALE") == 1
    assert "Recent activity" in rendered
    assert "Your open thesis on this name" in rendered


def test_workspace_position_surfaces_structured_source_warning() -> None:
    from report.models import PortfolioPositionSection, SectionStatus
    from report.renderers.workspace_sections.position import _position_tab

    rendered = StringIO()
    _position_tab(
        rendered,
        PortfolioPositionSection(
            status=SectionStatus.OK,
            held=True,
            total_quantity=1,
            source_warnings=[
                {"code": "NO_CANONICAL_LINK", "message": "Account excluded from totals."}
            ],
        ),
        ticker="MELI",
    )

    html = rendered.getvalue()
    assert "NO_CANONICAL_LINK" in html
    assert "Account excluded from totals." in html


def test_workspace_position_surfaces_warnings_for_not_held_and_unavailable_history() -> None:
    from report.models import (
        MissingReason,
        PortfolioPositionSection,
        PortfolioPositionTransaction,
        SectionStatus,
    )
    from report.renderers.workspace_sections.position import _position_tab

    not_held = StringIO()
    _position_tab(
        not_held,
        PortfolioPositionSection(
            status=SectionStatus.NOT_APPLICABLE,
            held=False,
            source_warnings=[{"code": "EXCLUDED", "message": "Account excluded."}],
        ),
        ticker="MELI",
    )
    assert "EXCLUDED" in not_held.getvalue()

    unavailable = StringIO()
    _position_tab(
        unavailable,
        PortfolioPositionSection(
            status=SectionStatus.MISSING_DATA,
            held=False,
            missing=MissingReason(
                stage="portfolio_tracker",
                fix_command="configure API",
                detail="source unavailable",
            ),
            source_warnings=[{"code": "STALE", "message": "Snapshot stale."}],
            recent_transactions=[
                PortfolioPositionTransaction(
                    date=date(2026, 8, 19),
                    account_name="Brokerage",
                    type="buy",
                    quantity=1.0,
                    amount=100.0,
                )
            ],
            history_state="partial",
            history_error="decision history unavailable",
        ),
        ticker="MELI",
    )
    html = unavailable.getvalue()
    assert "STALE" in html
    assert "Source unavailable; preserving canonical history." in html
    assert "Recent transactions" in html


def test_report_persists_canonical_position_provenance(monkeypatch: pytest.MonkeyPatch) -> None:
    from integrations.portfolio_position import (
        PortfolioPositionAccount,
        PortfolioPositionResult,
        PositionProvenance,
    )
    from report.sections import portfolio_position

    def held_position(_: str) -> PortfolioPositionResult:
        return PortfolioPositionResult(
            state="held",
            accounts=[PortfolioPositionAccount(account_name="Brokerage", quantity=1.0)],
            total_quantity=1.0,
            provenance=PositionProvenance(
                source_identity="immutable-test-snapshot",
                snapshot_as_of=date(2026, 8, 20),
                account_coverage=2,
                is_stale=False,
            ),
        )

    monkeypatch.setattr(portfolio_position, "resolve_configured_position", held_position)

    section = portfolio_position.build("MELI", Path("detached-checkout"))

    assert section.source_identity == "immutable-test-snapshot"
    assert section.position_as_of == date(2026, 8, 20)
    assert section.source_account_coverage == 2
    assert section.source_is_stale is False


def test_runtime_manager_is_idempotent_when_health_is_already_attributed() -> None:
    from runtime.portfolio_tracker import (
        ListenerObservation,
        PortfolioTrackerRuntimeManager,
        RefreshEvidence,
        RuntimeConfig,
        RuntimeReceipt,
        SchedulerEvidence,
    )

    starts: list[RuntimeConfig] = []
    manager = PortfolioTrackerRuntimeManager(
        config=RuntimeConfig(
            listener_owner="portfolio-tracker-service",
            daily_refresh_owner="portfolio-tracker-service",
            idempotency_key="portfolio-tracker:2026-08-20",
        ),
        inspect_listener=lambda: ListenerObservation(
            healthy=True,
            owner="portfolio-tracker-service",
            pid=42,
            health=_health(),
        ),
        start_listener=lambda config: starts.append(config),
        now=lambda: datetime(2026, 8, 20, 12, 0, tzinfo=UTC),
    )

    first = manager.ensure_running()
    second = manager.ensure_running()

    assert starts == []
    assert first.lifecycle_state == "already_running"
    assert second.lifecycle_state == "already_running"
    assert first.listener.owner == "portfolio-tracker-service"
    assert first.scheduler is None and first.refresh is None

    evidence = RuntimeReceipt(
        idempotency_key="portfolio-tracker:2026-08-20",
        lifecycle_state="already_running",
        recorded_at=datetime(2026, 8, 20, 12, 0, tzinfo=UTC),
        listener=first.listener,
        scheduler=SchedulerEvidence(
            task_name="portfolio-refresh",
            terminal_result="success",
            observed_at=datetime(2026, 8, 20, 11, 0, tzinfo=UTC),
        ),
        refresh=RefreshEvidence(
            owner="portfolio-tracker-service",
            snapshot_as_of="2026-08-20",
            completed_at=datetime(2026, 8, 20, 11, 5, tzinfo=UTC),
            terminal_result="success",
        ),
    )
    assert evidence.listener.pid == 42
    assert evidence.scheduler is not None and evidence.scheduler.task_name == "portfolio-refresh"
    assert evidence.refresh is not None and evidence.refresh.snapshot_as_of == "2026-08-20"


def test_runtime_manager_fails_loudly_when_lease_release_deadline_expires() -> None:
    from runtime.portfolio_tracker import (
        LeaseReleaseError,
        ListenerObservation,
        PortfolioTrackerRuntimeManager,
        RuntimeConfig,
    )

    class _OrphanedLease:
        last_conflict_detail = "lease release deadline exceeded; retry required"

        def acquire(self) -> bool:
            return True

        def release(self) -> bool:
            return False

    manager = PortfolioTrackerRuntimeManager(
        config=RuntimeConfig(
            listener_owner="portfolio-tracker-service",
            daily_refresh_owner="portfolio-tracker-service",
            idempotency_key="portfolio-tracker:2026-08-20",
        ),
        inspect_listener=lambda: ListenerObservation(healthy=False),
        start_listener=lambda _: None,
        now=lambda: datetime(2026, 8, 20, 12, 0, tzinfo=UTC),
        lease=cast(Any, _OrphanedLease()),
    )

    with pytest.raises(LeaseReleaseError, match="lease release deadline"):
        manager.ensure_running()


def test_runtime_manager_never_reports_started_without_health_and_owner_proof() -> None:
    from runtime.portfolio_tracker import (
        ListenerObservation,
        PortfolioTrackerRuntimeManager,
        RuntimeConfig,
    )

    starts: list[RuntimeConfig] = []
    manager = PortfolioTrackerRuntimeManager(
        config=RuntimeConfig(
            listener_owner="portfolio-tracker-service",
            daily_refresh_owner="portfolio-tracker-service",
            idempotency_key="portfolio-tracker:2026-08-20",
        ),
        inspect_listener=lambda: ListenerObservation(healthy=True, owner=None),
        start_listener=lambda config: starts.append(config),
        now=lambda: datetime(2026, 8, 20, 12, 0, tzinfo=UTC),
        startup_timeout_seconds=0,
    )

    receipt = manager.ensure_running()

    assert starts == []
    assert receipt.lifecycle_state == "failed"
    assert receipt.listener.owner is None


def test_runtime_manager_started_requires_post_launch_health_and_expected_owner() -> None:
    from runtime.portfolio_tracker import (
        ListenerObservation,
        PortfolioTrackerRuntimeManager,
        RuntimeConfig,
    )

    now = datetime(2026, 8, 20, 12, 0, tzinfo=UTC)
    observations = iter(
        (
            ListenerObservation(healthy=False),
            ListenerObservation(
                healthy=True,
                owner="portfolio-tracker-service",
                health=_health(),
            ),
        )
    )
    starts: list[RuntimeConfig] = []
    receipt = PortfolioTrackerRuntimeManager(
        config=RuntimeConfig(
            listener_owner="portfolio-tracker-service",
            daily_refresh_owner="portfolio-tracker-service",
            idempotency_key="portfolio-tracker:2026-08-20",
        ),
        inspect_listener=lambda: next(observations),
        start_listener=lambda config: starts.append(config),
        now=lambda: now,
        sleep=lambda _seconds: None,
    ).ensure_running()

    assert len(starts) == 1
    assert receipt.lifecycle_state == "started"
    assert receipt.listener.owner == "portfolio-tracker-service"


def test_runtime_lease_failure_receipt_and_atomic_round_trip(tmp_path: Path) -> None:
    from runtime.portfolio_tracker import (
        AtomicFileLease,
        ListenerObservation,
        PortfolioTrackerRuntimeManager,
        RuntimeConfig,
        write_runtime_receipt,
    )

    def now() -> datetime:
        return datetime(2026, 8, 20, 12, 0, tzinfo=UTC)

    config = RuntimeConfig(
        listener_owner="portfolio-tracker-service",
        daily_refresh_owner="portfolio-tracker-service",
        idempotency_key="portfolio-tracker:2026-08-20",
    )
    first_lease = AtomicFileLease(tmp_path / "runtime.lock")
    second_lease = AtomicFileLease(tmp_path / "runtime.lock")
    assert first_lease.acquire() is True
    blocked = PortfolioTrackerRuntimeManager(
        config=config,
        inspect_listener=lambda: ListenerObservation(healthy=False),
        start_listener=lambda _: pytest.fail("blocked launch must not start"),
        now=now,
        lease=second_lease,
    ).ensure_running()
    first_lease.release()
    failed = PortfolioTrackerRuntimeManager(
        config=config,
        inspect_listener=lambda: ListenerObservation(healthy=False),
        start_listener=lambda _: (_ for _ in ()).throw(OSError("start failed")),
        now=now,
        lease=AtomicFileLease(tmp_path / "runtime.lock"),
    ).ensure_running()
    persisted = write_runtime_receipt(tmp_path / "runtime.receipt.json", failed)

    assert blocked.lifecycle_state == "ownership_conflict"
    assert failed.lifecycle_state == "failed"
    assert failed.failure_detail == "OSError"
    assert persisted.lifecycle_state == failed.lifecycle_state
    assert persisted.scheduler is not None
    assert persisted.scheduler.terminal_result == "activation_required"


def test_runtime_lease_recovers_only_dead_old_owner_with_evidence(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import os
    import time

    import runtime.portfolio_tracker as runtime

    path = tmp_path / "runtime.lock"
    path.write_text("999999|0|stale-token", encoding="ascii")
    old = time.time() - runtime.LEASE_STALE_AFTER_SECONDS - 1
    os.utime(path, (old, old))

    def stale_liveness(_pid: int) -> Literal["dead"]:
        return "dead"

    monkeypatch.setattr(runtime, "_pid_liveness", stale_liveness)

    lease = runtime.AtomicFileLease(path)

    assert lease.acquire() is True
    lease.release()
    assert not path.exists()


def test_runtime_lease_takeover_never_deletes_a_replacement_race(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import os
    import time

    import runtime.portfolio_tracker as runtime

    path = tmp_path / "runtime.lock"
    path.write_text("999999|0|stale-token", encoding="ascii")
    old = time.time() - runtime.LEASE_STALE_AFTER_SECONDS - 1
    os.utime(path, (old, old))
    real_rename = runtime.os.rename

    def rename_then_replace(source: Path, destination: Path) -> None:
        real_rename(source, destination)
        Path(source).write_text(
            f"{os.getpid()}|{time.time():.6f}|replacement-token", encoding="ascii"
        )

    monkeypatch.setattr(runtime.os, "rename", rename_then_replace)

    def replacement_pid_alive(pid: int) -> bool:
        return pid == os.getpid()

    def replacement_liveness(pid: int) -> Literal["alive", "dead"]:
        return "alive" if replacement_pid_alive(pid) else "dead"

    monkeypatch.setattr(
        runtime,
        "_pid_liveness",
        replacement_liveness,
    )

    assert runtime.AtomicFileLease(path).acquire() is False
    assert path.read_text(encoding="ascii").endswith("replacement-token")


def test_runtime_lease_takeover_inverse_replacement_before_rename_is_preserved(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import os
    import time

    import runtime.portfolio_tracker as runtime

    path = tmp_path / "runtime.lock"
    path.write_text("999999|0|stale-token", encoding="ascii")
    old = time.time() - runtime.LEASE_STALE_AFTER_SECONDS - 1
    os.utime(path, (old, old))
    real_rename = runtime.os.rename

    def replace_then_rename(source: Path, destination: Path) -> None:
        Path(source).write_text(
            f"{os.getpid()}|{time.time():.6f}|replacement-token", encoding="ascii"
        )
        real_rename(source, destination)

    def stale_liveness(_pid: int) -> Literal["dead"]:
        return "dead"

    monkeypatch.setattr(runtime.os, "rename", replace_then_rename)
    monkeypatch.setattr(runtime, "_pid_liveness", stale_liveness)

    assert runtime.AtomicFileLease(path).acquire() is False
    assert path.read_text(encoding="ascii").endswith("replacement-token")


def test_windows_pid_probe_uses_open_process_without_signal_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import runtime.portfolio_tracker as runtime

    class _Kernel:
        def OpenProcess(self, *_args: object) -> int:
            return 41

        def GetExitCodeProcess(self, _handle: int, result: object) -> bool:
            pointer = ctypes.cast(cast(Any, result), ctypes.POINTER(ctypes.c_ulong))
            pointer.contents.value = 259
            return True

        def CloseHandle(self, _handle: int) -> None:
            return None

    def fake_dll(*_args: object, **_kwargs: object) -> _Kernel:
        return _Kernel()

    def forbidden_kill(*_args: object) -> None:
        pytest.fail("signals are forbidden")

    monkeypatch.setattr(runtime.sys, "platform", "win32")
    monkeypatch.setattr(runtime.ctypes, "WinDLL", fake_dll, raising=False)
    monkeypatch.setattr(runtime.os, "kill", forbidden_kill)

    probe = cast("Callable[[int], bool]", getattr(runtime, "_pid_is_alive"))
    assert probe(1234) is True


def test_windows_endpoint_probe_rejects_foreign_healthy_responder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import runtime.portfolio_tracker as runtime

    class _Result:
        stdout = "  TCP    127.0.0.1:8123    0.0.0.0:0    LISTENING    7777\n"

    def netstat(*_args: object, **_kwargs: object) -> _Result:
        return _Result()

    monkeypatch.setattr(runtime.os, "name", "nt")
    monkeypatch.setattr(runtime.subprocess, "run", netstat)

    assert runtime.endpoint_owner_matches_pid("127.0.0.1", 8123, 8888) is False


def test_windows_endpoint_probe_matches_structured_exact_ipv4_and_ipv6_hosts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import runtime.portfolio_tracker as runtime

    class _Result:
        stdout = (
            "  TCP    127.0.0.10:8123    0.0.0.0:0    LISTENING    8888\n"
            "  TCP    [0:0:0:0:0:0:0:1]:8123    [::]:0    LISTENING    8888\n"
        )

    monkeypatch.setattr(runtime.os, "name", "nt")

    def netstat(*_args: object, **_kwargs: object) -> _Result:
        return _Result()

    monkeypatch.setattr(runtime.subprocess, "run", netstat)

    assert runtime.endpoint_owner_matches_pid("127.0.0.1", 8123, 8888) is False
    assert runtime.endpoint_owner_matches_pid("::1", 8123, 8888) is True


def test_runtime_lease_release_defers_while_takeover_guard_is_held(tmp_path: Path) -> None:
    import runtime.portfolio_tracker as runtime

    path = tmp_path / "runtime.lock"
    guard = path.with_name(f".{path.name}.takeover")
    owner = runtime.AtomicFileLease(path)
    assert owner.acquire() is True
    guard.write_text("contender", encoding="ascii")

    owner.release()

    assert path.exists()
    assert owner.last_conflict_detail == "lease release deadline exceeded; retry required"
    guard.unlink()
    owner.release()
    assert not path.exists()


def test_runtime_lease_release_retries_after_takeover_guard_clears(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import runtime.portfolio_tracker as runtime

    path = tmp_path / "runtime.lock"
    guard = path.with_name(f".{path.name}.takeover")
    owner = runtime.AtomicFileLease(path)
    assert owner.acquire() is True
    guard.write_text("contender", encoding="ascii")

    def clear_guard(_delay: float) -> None:
        guard.unlink(missing_ok=True)

    monkeypatch.setattr(runtime.time, "sleep", clear_guard)
    assert owner.release() is True
    assert not path.exists()


def test_windows_access_denied_pid_probe_is_unknown_not_dead(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import runtime.portfolio_tracker as runtime

    class _Kernel:
        def OpenProcess(self, *_args: object) -> int:
            return 0

    def fake_dll(*_args: object, **_kwargs: object) -> _Kernel:
        return _Kernel()

    def access_denied() -> int:
        return 5

    monkeypatch.setattr(runtime.sys, "platform", "win32")
    monkeypatch.setattr(runtime.ctypes, "WinDLL", fake_dll, raising=False)
    monkeypatch.setattr(runtime, "_windows_last_error", access_denied)

    probe = cast(
        "Callable[[int], Literal['alive', 'dead', 'unknown']]", getattr(runtime, "_pid_liveness")
    )
    assert probe(1234) == "unknown"


def test_runtime_lease_old_owner_cannot_release_new_owner_after_takeover(tmp_path: Path) -> None:
    import runtime.portfolio_tracker as runtime

    path = tmp_path / "runtime.lock"
    old_owner = runtime.AtomicFileLease(path)
    assert old_owner.acquire() is True
    path.unlink()

    new_owner = runtime.AtomicFileLease(path)
    assert new_owner.acquire() is True
    old_owner.release()

    assert path.exists()
    assert new_owner.acquire() is True
    new_owner.release()
    assert not path.exists()


def test_runtime_lease_contenders_have_one_owner_per_interleaving(tmp_path: Path) -> None:
    import runtime.portfolio_tracker as runtime

    path = tmp_path / "runtime.lock"
    first = runtime.AtomicFileLease(path)
    second = runtime.AtomicFileLease(path)

    assert first.acquire() is True
    assert second.acquire() is False
    first.release()
    assert second.acquire() is True
    second.release()


def test_daily_refresh_producer_derives_key_and_never_activates(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from integrations.portfolio_tracker_v1 import V1Fetch
    from runtime.portfolio_tracker import produce_daily_refresh_receipt

    class _ReadOnlyClient:
        def __init__(self, **_: object) -> None:
            self.started = False

        def get_health(self) -> V1Fetch[HealthV1]:
            return V1Fetch(available=True, endpoint="/health", data=_health())

        def get_portfolio_snapshot(self) -> V1Fetch[PortfolioSnapshotV1]:
            return V1Fetch(
                available=True,
                endpoint="/portfolio-snapshot",
                data=_snapshot_from_positions(_positions()),
            )

    import integrations.portfolio_tracker_v1 as v1

    monkeypatch.setattr(v1, "TrackerV1Client", _ReadOnlyClient)
    recorded = datetime(2026, 8, 20, 12, tzinfo=UTC)
    receipt = produce_daily_refresh_receipt(
        api_url="http://tracker.test",
        receipt_path=tmp_path / "receipt.json",
        now=recorded,
    )
    assert receipt.idempotency_key == "portfolio-tracker-refresh:2026-08-20"
    assert receipt.refresh is not None and receipt.refresh.snapshot_as_of == "2026-08-20"
    assert receipt.refresh.owner is None
    assert receipt.refresh.completed_at is None
    assert receipt.refresh.terminal_result == "activation_required"
    assert receipt.scheduler is not None
    assert receipt.scheduler.terminal_result == "activation_required"
    assert receipt.lifecycle_state == "already_running"


def test_daily_refresh_rejects_snapshot_with_no_observation_date(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from integrations.portfolio_tracker_v1 import V1Fetch
    from runtime.portfolio_tracker import produce_daily_refresh_receipt

    base = _snapshot_from_positions(_positions())
    snapshot = base.model_copy(
        update={
            "meta": base.meta.model_copy(update={"as_of": None}),
            "equity_fraction": base.equity_fraction.model_copy(update={"holdings_as_of": None}),
        }
    )

    class _NoDateClient:
        def __init__(self, **_: object) -> None:
            pass

        def get_health(self) -> V1Fetch[HealthV1]:
            return V1Fetch(
                available=True,
                endpoint="/health",
                data=_health().model_copy(update={"latest_snapshot_date": None}),
            )

        def get_portfolio_snapshot(self) -> V1Fetch[PortfolioSnapshotV1]:
            return V1Fetch(available=True, endpoint="/portfolio-snapshot", data=snapshot)

    import integrations.portfolio_tracker_v1 as v1

    monkeypatch.setattr(v1, "TrackerV1Client", _NoDateClient)
    receipt = produce_daily_refresh_receipt(
        api_url="http://tracker.test",
        receipt_path=tmp_path / "receipt.json",
        now=datetime(2026, 8, 20, 12, tzinfo=UTC),
    )
    assert receipt.lifecycle_state == "failed"
    assert receipt.refresh is None
    assert receipt.failure_detail == "portfolio snapshot has no observation date"


def test_daily_refresh_rejects_lagging_account_coverage(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from integrations.portfolio_tracker_v1 import V1Fetch
    from runtime.portfolio_tracker import produce_daily_refresh_receipt

    base = _snapshot_from_positions(_positions())
    snapshot = base.model_copy(
        update={
            "meta": base.meta.model_copy(
                update={
                    "account_coverage": base.meta.account_coverage.model_copy(
                        update={"lagging_account_ids": [1]}
                    )
                }
            )
        }
    )

    class _LaggingClient:
        def __init__(self, **_: object) -> None:
            pass

        def get_health(self) -> V1Fetch[HealthV1]:
            return V1Fetch(available=True, endpoint="/health", data=_health())

        def get_portfolio_snapshot(self) -> V1Fetch[PortfolioSnapshotV1]:
            return V1Fetch(available=True, endpoint="/portfolio-snapshot", data=snapshot)

    import integrations.portfolio_tracker_v1 as v1

    monkeypatch.setattr(v1, "TrackerV1Client", _LaggingClient)
    receipt = produce_daily_refresh_receipt(
        api_url="http://tracker.test",
        receipt_path=tmp_path / "receipt.json",
        now=datetime(2026, 8, 20, 12, tzinfo=UTC),
    )
    assert receipt.lifecycle_state == "failed"
    assert receipt.refresh is None
    assert receipt.failure_detail == (
        "portfolio snapshot account coverage is lagging for account ids: 1"
    )


def test_daily_refresh_rejects_stale_or_currency_inconsistent_snapshot(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from integrations.portfolio_tracker_v1 import V1Fetch
    from runtime.portfolio_tracker import produce_daily_refresh_receipt

    snapshot = PortfolioSnapshotV1.model_validate_json(
        (Path(__file__).parent / "fixtures/tracker_v1/portfolio-snapshot.stale.json").read_bytes()
    )
    snapshot = snapshot.model_copy(
        update={
            "accounts": [
                snapshot.accounts[0].model_copy(update={"value_currency": "EUR"}),
                *snapshot.accounts[1:],
            ]
        }
    )

    class _BadClient:
        def __init__(self, **_: object) -> None:
            pass

        def get_health(self) -> V1Fetch[HealthV1]:
            return V1Fetch(
                available=True,
                endpoint="/health",
                data=_health().model_copy(update={"latest_snapshot_date": snapshot.meta.as_of}),
            )

        def get_portfolio_snapshot(self) -> V1Fetch[PortfolioSnapshotV1]:
            return V1Fetch(available=True, endpoint="/portfolio-snapshot", data=snapshot)

    import integrations.portfolio_tracker_v1 as v1

    monkeypatch.setattr(v1, "TrackerV1Client", _BadClient)
    receipt = produce_daily_refresh_receipt(
        api_url="http://tracker.test",
        receipt_path=tmp_path / "receipt.json",
        now=datetime(2026, 8, 20, 12, tzinfo=UTC),
    )

    assert receipt.lifecycle_state == "failed"
    assert receipt.refresh is None
    assert (
        receipt.failure_detail
        == "portfolio snapshot account currency does not match envelope currency"
    )


def test_daily_refresh_rejects_position_lot_outside_active_envelope(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from integrations.portfolio_tracker_v1 import V1Fetch
    from runtime.portfolio_tracker import produce_daily_refresh_receipt

    base = _snapshot_from_positions(_positions())
    snapshot = base.model_copy(
        update={
            "meta": base.meta.model_copy(
                update={
                    "account_coverage": base.meta.account_coverage.model_copy(
                        update={"included_account_ids": [1, 2, 3, 4], "excluded_account_ids": []}
                    )
                }
            ),
            "equity_fraction": base.equity_fraction.model_copy(
                update={"included_account_ids": [1, 2, 3], "excluded_account_ids": []}
            ),
            "positions": [
                base.positions[0].model_copy(
                    update={
                        "accounts": [
                            base.positions[0].accounts[0].model_copy(update={"account_id": 4})
                        ]
                    }
                )
            ],
        }
    )

    class _BadCoverageClient:
        def __init__(self, **_: object) -> None:
            pass

        def get_health(self) -> V1Fetch[HealthV1]:
            return V1Fetch(available=True, endpoint="/health", data=_health())

        def get_portfolio_snapshot(self) -> V1Fetch[PortfolioSnapshotV1]:
            return V1Fetch(available=True, endpoint="/portfolio-snapshot", data=snapshot)

    import integrations.portfolio_tracker_v1 as v1

    monkeypatch.setattr(v1, "TrackerV1Client", _BadCoverageClient)
    receipt = produce_daily_refresh_receipt(
        api_url="http://tracker.test",
        receipt_path=tmp_path / "receipt.json",
        now=datetime(2026, 8, 20, 12, tzinfo=UTC),
    )

    assert receipt.lifecycle_state == "failed"
    assert receipt.refresh is None
    assert receipt.failure_detail == "position lot references an inactive or excluded account"


def test_runtime_receipt_writer_merges_typed_planes_under_shared_path(tmp_path: Path) -> None:
    from runtime.portfolio_tracker import (
        ListenerObservation,
        RuntimeReceipt,
        SchedulerEvidence,
        write_runtime_receipt,
    )

    path = tmp_path / "receipt.json"
    recorded = datetime(2026, 8, 20, 12, tzinfo=UTC)
    first = RuntimeReceipt(
        idempotency_key="portfolio-tracker-refresh:2026-08-20",
        lifecycle_state="failed",
        recorded_at=recorded,
        listener=ListenerObservation(healthy=False),
        scheduler=SchedulerEvidence(
            task_name="PortfolioTrackerDaily",
            terminal_result="activation_required",
            observed_at=recorded,
        ),
    )
    second = first.model_copy(update={"lifecycle_state": "already_running", "scheduler": None})

    write_runtime_receipt(path, first)
    merged = write_runtime_receipt(path, second)

    assert merged.scheduler is not None
    assert merged.scheduler.terminal_result == "activation_required"


def test_runtime_receipt_merge_keeps_missing_listener_attribution_unproven(
    tmp_path: Path,
) -> None:
    from runtime.portfolio_tracker import (
        ListenerObservation,
        RefreshEvidence,
        RuntimeReceipt,
        SchedulerEvidence,
        write_runtime_receipt,
    )

    path = tmp_path / "receipt.json"
    recorded = datetime(2026, 8, 20, 12, tzinfo=UTC)
    first = RuntimeReceipt(
        idempotency_key="portfolio-tracker-refresh:2026-08-20",
        lifecycle_state="started",
        recorded_at=recorded,
        listener=ListenerObservation(
            healthy=True,
            owner="portfolio-tracker-service",
            pid=123,
            job_id="job-123",
            health_checked_at=recorded,
            health=_health(),
        ),
        refresh=RefreshEvidence(owner="daily-owner", completed_at=recorded),
        scheduler=SchedulerEvidence(
            task_name="PortfolioTrackerDaily",
            terminal_result="success",
            observed_at=recorded,
        ),
    )
    daily = RuntimeReceipt(
        idempotency_key=first.idempotency_key,
        lifecycle_state="failed",
        recorded_at=recorded.replace(hour=13),
        listener=ListenerObservation(
            healthy=False,
            health_checked_at=recorded.replace(hour=13),
            health=_health(stale=True),
        ),
        refresh=None,
        scheduler=SchedulerEvidence(
            task_name="PortfolioTrackerDaily",
            terminal_result="activation_required",
            observed_at=recorded.replace(hour=13),
        ),
    )

    write_runtime_receipt(path, first)
    merged = write_runtime_receipt(path, daily)

    assert merged.listener.owner is None
    assert merged.listener.pid is None
    assert merged.listener.job_id is None
    assert merged.listener.health is not None
    assert merged.refresh is not None and merged.refresh.owner == "daily-owner"
    assert merged.scheduler is not None
    assert merged.scheduler.terminal_result == "activation_required"

    older = RuntimeReceipt(
        idempotency_key="portfolio-tracker-refresh:2026-08-19",
        lifecycle_state="failed",
        recorded_at=recorded.replace(hour=11),
        listener=ListenerObservation(healthy=False),
        failure_detail="out-of-order write",
    )
    out_of_order = write_runtime_receipt(path, older)
    assert out_of_order.idempotency_key == merged.idempotency_key
    assert out_of_order.recorded_at == merged.recorded_at
    assert out_of_order.lifecycle_state == merged.lifecycle_state


def test_operations_snapshot_projects_portfolio_tracker_receipt_states(tmp_path: Path) -> None:
    import sqlite3

    from operations.paths import portfolio_tracker_receipt_path
    from operations.registry import build_operations_registry
    from operations.snapshot import collect_operations_snapshot
    from runtime.portfolio_tracker import ListenerObservation, RuntimeReceipt

    observed = datetime(2026, 8, 20, 12, tzinfo=UTC)
    receipt = RuntimeReceipt(
        idempotency_key="portfolio-tracker-refresh:2026-08-20",
        lifecycle_state="already_running",
        recorded_at=observed,
        listener=ListenerObservation(healthy=True, owner="portfolio-tracker-service"),
    )
    path = portfolio_tracker_receipt_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text(receipt.model_dump_json(), encoding="utf-8")
    snapshot = collect_operations_snapshot(
        build_operations_registry(Path(__file__).resolve().parents[1]),
        repo_root=tmp_path,
        conn=sqlite3.connect(":memory:"),
        observed_at=observed,
    )
    assert snapshot.portfolio_tracker_runtime.state == "current"
    assert snapshot.portfolio_tracker_runtime.receipt is not None


@pytest.mark.parametrize("stale_plane", ["listener", "scheduler", "refresh"])
def test_operations_panel_does_not_greenwash_stale_runtime_planes(stale_plane: str) -> None:
    from operations.models import PortfolioTrackerRuntimeObservation
    from pipeline.operations_panel import (  # pyright: ignore[reportPrivateUsage]
        _portfolio_tracker_evidence,  # pyright: ignore[reportPrivateUsage]
    )
    from runtime.portfolio_tracker import (
        ListenerObservation,
        RefreshEvidence,
        RuntimeReceipt,
        SchedulerEvidence,
    )

    observed = datetime(2026, 8, 20, 12, 20, tzinfo=UTC)
    old = datetime(2026, 8, 20, 11, tzinfo=UTC)
    receipt = RuntimeReceipt(
        idempotency_key="portfolio-tracker-refresh:2026-08-20",
        lifecycle_state="started",
        recorded_at=observed,
        listener=ListenerObservation(
            healthy=True,
            owner="portfolio-tracker-service",
            health_checked_at=old if stale_plane == "listener" else observed,
            health=_health(),
        ),
        scheduler=SchedulerEvidence(
            task_name="PortfolioTrackerDailyRefresh",
            terminal_result="success",
            observed_at=old if stale_plane == "scheduler" else observed,
        ),
        refresh=RefreshEvidence(
            owner="portfolio-tracker-service",
            terminal_result="success",
            completed_at=old if stale_plane == "refresh" else observed,
        ),
    )
    observation = PortfolioTrackerRuntimeObservation(
        state="current",
        observed_at=observed,
        evidence_source="test:runtime-receipt",
        evidence_recorded_at=observed,
        receipt=receipt,
    )

    evidence = _portfolio_tracker_evidence(observation)  # pyright: ignore[reportPrivateUsage]

    assert evidence.tone == "warn"
    assert "stale planes" in evidence.state
    assert stale_plane.replace("refresh", "daily refresh") in evidence.detail


def test_operations_panel_does_not_greenwash_read_only_refresh_probe() -> None:
    from operations.models import PortfolioTrackerRuntimeObservation
    from pipeline.operations_panel import (  # pyright: ignore[reportPrivateUsage]
        _portfolio_tracker_evidence,  # pyright: ignore[reportPrivateUsage]
    )
    from runtime.portfolio_tracker import (
        ListenerObservation,
        RefreshEvidence,
        RuntimeReceipt,
    )

    observed = datetime(2026, 8, 20, 12, tzinfo=UTC)
    receipt = RuntimeReceipt(
        idempotency_key="portfolio-tracker-refresh:2026-08-20",
        lifecycle_state="already_running",
        recorded_at=observed,
        listener=ListenerObservation(healthy=True, health=_health()),
        refresh=RefreshEvidence(
            owner=None,
            snapshot_as_of="2026-08-20",
            completed_at=None,
            terminal_result="activation_required",
        ),
    )
    evidence = _portfolio_tracker_evidence(
        PortfolioTrackerRuntimeObservation(
            state="current",
            observed_at=observed,
            evidence_source="test:runtime-receipt",
            receipt=receipt,
        )
    )
    assert evidence.tone == "bad"
    assert evidence.detail is not None and "refresh activation_required" in evidence.detail


def test_operations_snapshot_marks_stale_runtime_plane_even_with_fresh_envelope(
    tmp_path: Path,
) -> None:
    import sqlite3

    from operations.paths import portfolio_tracker_receipt_path
    from operations.registry import build_operations_registry
    from operations.snapshot import collect_operations_snapshot
    from runtime.portfolio_tracker import ListenerObservation, RuntimeReceipt

    observed = datetime(2026, 8, 20, 12, 20, tzinfo=UTC)
    receipt = RuntimeReceipt(
        idempotency_key="portfolio-tracker-refresh:2026-08-20",
        lifecycle_state="started",
        recorded_at=observed,
        listener=ListenerObservation(
            healthy=True,
            owner="portfolio-tracker-service",
            health_checked_at=datetime(2026, 8, 20, 11, tzinfo=UTC),
            health=_health(),
        ),
    )
    path = portfolio_tracker_receipt_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text(receipt.model_dump_json(), encoding="utf-8")
    snapshot = collect_operations_snapshot(
        build_operations_registry(Path(__file__).resolve().parents[1]),
        repo_root=tmp_path,
        conn=sqlite3.connect(":memory:"),
        observed_at=observed,
    )

    assert snapshot.portfolio_tracker_runtime.state == "stale"
    assert snapshot.portfolio_tracker_runtime.detail is not None
    assert "listener" in snapshot.portfolio_tracker_runtime.detail
