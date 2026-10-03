from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

from integrations.portfolio_offline_snapshot import read_configured_offline_portfolio_snapshot
from integrations.portfolio_position import ImmutableTrackerSnapshot, PortfolioPositionAdapter
from integrations.portfolio_tracker_v1 import (
    HealthV1,
    OptionContractV1,
    PortfolioSnapshotV1,
    V1Fetch,
)

FIXTURES = Path(__file__).parent / "fixtures" / "tracker_v1"


class SnapshotReader:
    def __init__(self, health: HealthV1, snapshot: PortfolioSnapshotV1) -> None:
        self.health = health
        self.snapshot = snapshot

    def probe_v1(self) -> V1Fetch[HealthV1]:
        return V1Fetch(available=True, endpoint="/health", data=self.health)

    def get_portfolio_snapshot(self) -> V1Fetch[PortfolioSnapshotV1]:
        return V1Fetch(available=True, endpoint="/snapshot", data=self.snapshot)


def test_typed_call_does_not_hide_held_stock_or_reject_stock_percent_above_100(
    tmp_path: Path,
) -> None:
    health = HealthV1.model_validate_json((FIXTURES / "health.json").read_bytes())
    snapshot = PortfolioSnapshotV1.model_validate_json(
        (FIXTURES / "portfolio-snapshot.json").read_bytes()
    )
    stock = snapshot.positions[0]
    contract = OptionContractV1(
        underlying_ticker="AAAA",
        contract_type="call",
        expiration_date=date(2026, 11, 20),
        strike_price=Decimal(150),
        multiplier=None,
        metadata_source="plaid.option_contract",
    )
    option = stock.model_copy(
        update={
            "security_id": 99,
            "ticker": "AAAA261120C00150000",
            "quantity": Decimal(-100),
            "market_value": Decimal(-2000),
            "cost_basis": None,
            "unrealized_pnl": None,
            "option_contract": contract,
            "quantity_unit": "underlying_units",
            "accounts": [
                stock.accounts[0].model_copy(
                    update={
                        "quantity": Decimal(-100),
                        "market_value": Decimal(-2000),
                        "cost_basis": None,
                        "quantity_unit": "underlying_units",
                    }
                )
            ],
        }
    )
    assert stock.market_value is not None
    total = stock.market_value - 2000
    rows = [
        row.model_copy(update={"percent_of_portfolio": row.market_value / total * 100})
        for row in [stock, option]
        if row.market_value is not None
    ]
    snapshot = snapshot.model_copy(
        update={
            "positions": rows,
            "total_market_value": total,
            "equity_fraction": snapshot.equity_fraction.model_copy(
                update={
                    "equity_value": total,
                    "denominator_value": total,
                    "equity_fraction": Decimal(1),
                }
            ),
        }
    )
    assert (
        PortfolioPositionAdapter(SnapshotReader(health, snapshot)).resolve("AAAA").state == "held"
    )
    path = tmp_path / "synthetic-snapshot.json"
    path.write_text(
        ImmutableTrackerSnapshot(
            source_identity="synthetic",
            health=health,
            portfolio_snapshot=snapshot,
        ).model_dump_json()
    )
    assert read_configured_offline_portfolio_snapshot(path) is not None
