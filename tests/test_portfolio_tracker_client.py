"""Tests for the portfolio-tracker REST client + the Portfolio tab renderer.

Network is mocked (monkeypatched ``requests.get``) so these are hermetic: the
client parses a recorded holdings/items/transactions payload and derives
percent_of_portfolio + tax_treatment, and degrades cleanly when the tracker is
unreachable. The renderer is exercised against both an offline and a populated
LivePortfolio.
"""

from __future__ import annotations

import pytest
import requests

from integrations import portfolio_tracker_client as ptc
from integrations.portfolio_tracker_client import (
    LivePortfolio,
    LivePosition,
    TaxLot,
    fetch_live_portfolio,
    tax_treatment,
)
from pipeline.portfolio_panel import render_live_portfolio_section

# Recorded payloads (Decimal-as-strings, mirroring the real FastAPI responses).
_HOLDINGS = [
    {
        "ticker": "NU",
        "name": "Nu Holdings",
        "security_id": 1,
        "total_quantity": "1000",
        "total_value": "12000.00",
        "total_cost_basis": "9000.00",
        "unrealized_pnl": "3000.00",
        "has_unreliable_cost_basis": False,
        "currency": "USD",
        "accounts": [
            {
                "account_id": 5,
                "account_name": "RH Roth IRA",
                "quantity": "1000",
                "institution_value": "12000.00",
                "cost_basis": "9000.00",
            }
        ],
    },
    {
        "ticker": "AAPL",
        "name": "Apple",
        "security_id": 2,
        "total_quantity": "100",
        "total_value": "20000.00",
        "total_cost_basis": "15000.00",
        "unrealized_pnl": "5000.00",
        "has_unreliable_cost_basis": False,
        "currency": "USD",
        "accounts": [
            {
                "account_id": 6,
                "account_name": "Fidelity Brokerage",
                "quantity": "60",
                "institution_value": "12000.00",
                "cost_basis": "9000.00",
            },
            {
                "account_id": 5,
                "account_name": "RH Roth IRA",
                "quantity": "40",
                "institution_value": "8000.00",
                "cost_basis": "6000.00",
            },
        ],
    },
]
_ITEMS = [
    {
        "item_id": 1,
        "institution_name": "Robinhood",
        "accounts": [
            {"account_id": 5, "name": "RH Roth IRA", "type": "investment", "subtype": "roth"}
        ],
    },
    {
        "item_id": 2,
        "institution_name": "Fidelity",
        "accounts": [
            {
                "account_id": 6,
                "name": "Fidelity Brokerage",
                "type": "investment",
                "subtype": "brokerage",
            },
            {"account_id": 7, "name": "Fidelity 401k", "type": "investment", "subtype": "401k"},
        ],
    },
]
_TXNS = [
    {
        "plaid_investment_transaction_id": "t1",
        "account_id": 5,
        "account_name": "RH Roth IRA",
        "security_id": 1,
        "ticker": "NU",
        "date": "2026-05-30",
        "name": "BUY NU",
        "quantity": "100",
        "amount": "-1200.00",
        "type": "buy",
        "subtype": "buy",
        "currency": "USD",
    }
]


class _FakeResp:
    def __init__(self, payload: object, status: int = 200) -> None:
        self._payload = payload
        self.status_code = status

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")

    def json(self) -> object:
        return self._payload


def _route(url: str) -> _FakeResp:
    if "/holdings" in url:
        return _FakeResp(_HOLDINGS)
    if "/plaid/items" in url:
        return _FakeResp(_ITEMS)
    if "/transactions" in url:
        return _FakeResp(_TXNS)
    return _FakeResp([], 404)


@pytest.fixture
def mock_tracker(monkeypatch: pytest.MonkeyPatch) -> None:
    def _get(url: str, timeout: float | None = None, params: object = None) -> _FakeResp:
        return _route(url)

    monkeypatch.setattr(ptc.requests, "get", _get)


# ----- tax_treatment classifier -----


@pytest.mark.parametrize(
    ("acct_type", "subtype", "expected"),
    [
        ("investment", "roth", "tax_free"),
        ("investment", "Roth IRA", "tax_free"),
        ("investment", "roth 401k", "tax_free"),
        ("investment", "hsa", "tax_free"),
        ("investment", "401k", "tax_deferred"),
        ("investment", "ira", "tax_deferred"),
        ("investment", "sep ira", "tax_deferred"),
        ("investment", "brokerage", "taxable"),
        ("brokerage", None, "taxable"),
        ("investment", "crypto exchange", "unknown"),
        (None, None, "unknown"),
    ],
)
def test_tax_treatment(acct_type: str | None, subtype: str | None, expected: str) -> None:
    assert tax_treatment(acct_type, subtype) == expected


# ----- client parsing + derivation -----


def test_fetch_parses_and_derives(mock_tracker: None) -> None:
    live = fetch_live_portfolio(api_url="http://tracker.test")
    assert live.available is True
    assert live.total_market_value == pytest.approx(32000.0)
    by_ticker = {p.ticker: p for p in live.positions}
    # percent_of_portfolio: 12000/32000 = 37.5%, 20000/32000 = 62.5%
    assert by_ticker["NU"].percent_of_portfolio == pytest.approx(37.5)
    assert by_ticker["AAPL"].percent_of_portfolio == pytest.approx(62.5)
    # Per-account tax treatment joined from /plaid/items.
    aapl_lots = {lot.account_id: lot.tax_treatment for lot in by_ticker["AAPL"].accounts}
    assert aapl_lots[6] == "taxable"  # Fidelity brokerage
    assert aapl_lots[5] == "tax_free"  # RH Roth IRA
    # Taxable breakdown bucketed at the lot level: tax_free = 12000 (NU) + 8000 (AAPL).
    assert live.by_tax_treatment["tax_free"] == pytest.approx(20000.0)
    assert live.by_tax_treatment["taxable"] == pytest.approx(12000.0)
    assert live.by_tax_treatment["tax_deferred"] == pytest.approx(0.0)
    # Transactions parsed.
    assert [t.ticker for t in live.transactions] == ["NU"]


def test_fetch_degrades_when_unreachable(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(url: str, timeout: float | None = None, params: object = None) -> _FakeResp:
        raise requests.ConnectionError("connection refused")

    monkeypatch.setattr(ptc.requests, "get", _boom)
    live = fetch_live_portfolio(api_url="http://tracker.test")
    assert live.available is False
    assert live.error is not None
    assert "ConnectionError" in live.error
    assert live.positions == []


# ----- renderer -----


def test_render_offline_shows_start_hint() -> None:
    html = render_live_portfolio_section(
        LivePortfolio(
            available=False, api_url="http://localhost:8000", error="ConnectionError: nope"
        )
    )
    assert "not reachable" in html
    assert "uvicorn portfolio_tracker.api.main:app" in html  # the start hint
    assert "<!doctype" not in html.lower()


def test_render_populated_positions_and_taxable() -> None:
    live = LivePortfolio(
        available=True,
        api_url="http://localhost:8000",
        total_market_value=32000.0,
        positions=[
            LivePosition(
                ticker="NU",
                name="Nu Holdings",
                quantity=1000.0,
                market_value=12000.0,
                cost_basis=9000.0,
                unrealized_pnl=3000.0,
                percent_of_portfolio=37.5,
                accounts=[TaxLot(5, "RH Roth IRA", 1000.0, 12000.0, "tax_free")],
            )
        ],
        by_tax_treatment={"taxable": 0.0, "tax_deferred": 0.0, "tax_free": 12000.0, "unknown": 0.0},
    )
    html = render_live_portfolio_section(live)
    assert "Live portfolio" in html
    assert "NU" in html
    assert "% of book" in html
    assert "Tax-free" in html
    assert "37.5%" in html
