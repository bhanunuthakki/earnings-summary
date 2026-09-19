"""Reports preserve dates from the canonical tracker result without local SQL."""

from datetime import date
from pathlib import Path

import pytest

from integrations.portfolio_position import (
    PortfolioPositionAccount,
    PortfolioPositionResult,
    PositionProvenance,
)
from report.sections import portfolio_position


def test_report_preserves_canonical_account_and_position_dates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    observed_at = date(2026, 6, 1)
    result = PortfolioPositionResult(
        state="held",
        accounts=[
            PortfolioPositionAccount(
                account_name="Taxable", quantity=110, snapshot_date=observed_at
            )
        ],
        total_quantity=110,
        position_as_of=observed_at,
    )

    def resolve(ticker: str) -> PortfolioPositionResult:
        assert ticker == "NU"
        return result

    monkeypatch.setattr(portfolio_position, "resolve_configured_position", resolve)
    section = portfolio_position.build("NU", tmp_path)
    assert section.position_as_of == observed_at
    assert section.accounts[0].snapshot_date == observed_at
    assert section.accounts[0].quantity == 110
    assert not (tmp_path / "data" / "portfolio.db").exists()


def test_report_uses_provenance_date_when_position_date_is_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    observed_at = date(2026, 6, 1)

    def resolve(_ticker: str) -> PortfolioPositionResult:
        return PortfolioPositionResult(
            state="source_unavailable",
            provenance=PositionProvenance(
                source_identity="synthetic-tracker",
                snapshot_as_of=observed_at,
                account_coverage=1,
                is_stale=True,
            ),
        )

    monkeypatch.setattr(portfolio_position, "resolve_configured_position", resolve)
    section = portfolio_position.build("NU", tmp_path)
    assert section.position_as_of == observed_at
    assert section.source_is_stale is True
    assert section.held is False
