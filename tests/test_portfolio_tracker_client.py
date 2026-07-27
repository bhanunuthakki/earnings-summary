"""Tests for the portfolio-tracker REST client + the Portfolio theme renderer.

Network is mocked (monkeypatched ``requests.get``) so these are hermetic: the
client parses a recorded holdings/items/transactions payload and derives
percent_of_portfolio + tax_treatment, and degrades cleanly when the tracker is
unreachable. P2.1 adds the analytics families: parsing canned performance /
position-alpha / positioning / policy / beta payloads (Decimal-as-strings,
mirroring the tracker's pydantic JSON), per-endpoint fault isolation, the
ConnectionError short-circuit, and the page composition's single-offline-note
guarantee. Renderers are exercised against offline, partial, and populated
states.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest
import requests

from integrations import portfolio_tracker_client as ptc
from integrations.portfolio_tracker_client import (
    LivePortfolio,
    LivePosition,
    PerformancePoint,
    PortfolioAnalytics,
    PositionCorrelationRow,
    TaxLot,
    fetch_live_portfolio,
    fetch_portfolio_analytics,
    tax_treatment,
)
from pipeline.portfolio_panel import (
    WindowSelection,
    compose_portfolio_page,
    compose_risk_page,
    compose_synthesis_page,
    render_live_portfolio_section,
    render_next_dollar_panel,
    render_portfolio_analytics_sections,
    render_portfolio_risk_panel,
    validated_window,
)
from portfolio_risk import compute_drawdown, factor_exposure_rollup

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


# --- Analytics payloads (P2.1), shaped like the tracker's response models:
# PerformanceSeries / PositionAlphaResult / PositioningOut / PolicyOut /
# BetaResult. Numbers arrive as Decimal-strings; dates as ISO strings.
_PERFORMANCE = {
    "start_date": "2025-06-10",
    "end_date": "2026-06-10",
    "base_value": "100000.00",
    "points": [
        {
            "date": "2025-06-10",
            "portfolio_value": "100000.00",
            "portfolio_return_pct": "0.0",
            "spy_return_pct": "0.0",
            "qqq_return_pct": "0.0",
            "policy_return_pct": "0.0",
            "spy_equivalent_value": "100000.00",
            "qqq_equivalent_value": "100000.00",
            "policy_equivalent_value": "100000.00",
        },
        {
            "date": "2025-12-10",
            "portfolio_value": "118000.00",
            "portfolio_return_pct": "8.4",
            "spy_return_pct": "5.1",
            "qqq_return_pct": "6.0",
            "policy_return_pct": "4.2",
            "spy_equivalent_value": "115100.00",
            "qqq_equivalent_value": "116000.00",
            "policy_equivalent_value": "114200.00",
        },
        {
            # Final day has a policy-benchmark gap (None) — cards/legend must
            # fall back to the last valid value instead of dropping the series.
            "date": "2026-06-10",
            "portfolio_value": "133200.00",
            "portfolio_return_pct": "18.2",
            "spy_return_pct": "11.5",
            "qqq_return_pct": "14.1",
            "policy_return_pct": None,
            "spy_equivalent_value": "111500.00",
            "qqq_equivalent_value": "114100.00",
            "policy_equivalent_value": None,
        },
    ],
    "earliest_observed_date": "2025-06-12",
    "net_external_cashflow_in": "25000.00",
    "backfill_start_unreliable": False,
}
_POSITION_ALPHA = {
    "start_date": "2025-06-10",
    "end_date": "2026-06-10",
    "rows": [
        {
            "ticker": "NU",
            "name": "Nu Holdings",
            "value_at_start": "10000.00",
            "bought_in_window": "2000.00",
            "sold_in_window": "0.00",
            "value_at_end": "15500.00",
            "actual_pl": "3500.00",
            "spy_counterfactual_pl": "1300.00",
            "qqq_counterfactual_pl": "1500.00",
            "policy_counterfactual_pl": "1100.00",
            "alpha": "2200.00",
            "alpha_vs_qqq": "2000.00",
            "alpha_vs_policy": "2400.00",
            "incomplete": False,
        },
        {
            "ticker": "AAPL",
            "name": "Apple",
            "value_at_start": "20000.00",
            "bought_in_window": "0.00",
            "sold_in_window": "5000.00",
            "value_at_end": "14200.00",
            "actual_pl": "-800.00",
            "spy_counterfactual_pl": "0.00",
            "qqq_counterfactual_pl": "100.00",
            "policy_counterfactual_pl": "-100.00",
            "alpha": "-800.00",
            "alpha_vs_qqq": "-900.00",
            "alpha_vs_policy": "-700.00",
            "incomplete": True,
        },
    ],
    "total_actual_pl": "2700.00",
    "total_spy_pl": "1300.00",
    "total_qqq_pl": "1600.00",
    "total_policy_pl": "1000.00",
    "total_alpha": "1400.00",
    "total_alpha_vs_qqq": "1100.00",
    "total_alpha_vs_policy": "1700.00",
    "series": [],
    "v_start": "30000.00",
    "v_end": "29700.00",
    "has_policy": True,
}
_POSITIONING = {
    "snapshot_date": "2026-06-10",
    "start_date": "2025-06-10",
    "end_date": "2026-06-10",
    "total_value": "47500.00",
    "by_asset_type": [
        {"label": "Stock", "value": "40000.00", "weight_pct": "84.2", "count": 12},
        {"label": "ETF", "value": "7500.00", "weight_pct": "15.8", "count": 2},
    ],
    "by_sector": [
        {"label": "Technology", "value": "20000.00", "weight_pct": "42.1", "count": 6},
        {"label": "Financial Services", "value": "15000.00", "weight_pct": "31.6", "count": 4},
    ],
    "by_region": [
        {"label": "US", "value": "30000.00", "weight_pct": "63.2", "count": 9},
        {"label": "International", "value": "17500.00", "weight_pct": "36.8", "count": 5},
    ],
    "by_account_type": [
        {"label": "Taxable", "value": "28500.00", "weight_pct": "60.0", "count": 11},
        {
            "label": "Retirement / tax-advantaged",
            "value": "19000.00",
            "weight_pct": "40.0",
            "count": 8,
        },
    ],
    "concentration": {
        "num_positions": 14,
        "top1_weight_pct": "12.5",
        "top5_weight_pct": "45.0",
        "top10_weight_pct": "78.0",
        "hhi": 950.0,
        "effective_holdings": 10.5,
    },
    "correlations": [
        {
            "security_id": 1,
            "ticker": "NU",
            "name": "Nu Holdings",
            "value": "15500.00",
            "weight_pct": "12.5",
            "sample_size": 250,
            "correlation_spy": 0.61,
            "beta_spy": 1.4,
            "correlation_qqq": 0.66,
            "beta_qqq": 1.2,
            "correlation_policy": 0.58,
            "beta_policy": 1.3,
        }
    ],
    "weighted_avg_correlation_spy": 0.72,
    "has_policy": True,
    "notes": [],
}
_POLICY = {
    "weights": [
        {
            "ticker": "VOO",
            "weight_pct": "70.0",
            "notes": "Core US",
            "updated_at": "2026-01-15T10:30:00Z",
        },
        {
            "ticker": "QQQ",
            "weight_pct": "20.0",
            "notes": None,
            "updated_at": "2026-01-15T10:30:00Z",
        },
        {
            "ticker": "BND",
            "weight_pct": "10.0",
            "notes": "Ballast",
            "updated_at": "2026-01-15T10:30:00Z",
        },
    ],
    "total_pct": "100.0",
    "is_balanced": True,
}
_BETA = {
    "benchmark": "SPY",
    "start_date": "2025-06-10",
    "end_date": "2026-06-10",
    "sample_size": 250,
    "risk_free_annual": 0.04,
    "beta": 1.12,
    "alpha_annualized_pct": 2.5,
    "alpha_t_stat": 2.31,
    "alpha_std_error_annualized_pct": 1.08,
    "alpha_significant": True,
    "r_squared": 0.82,
    "correlation": 0.91,
    "sharpe": 0.85,
    "sortino": 1.21,
    "information_ratio": 0.45,
    "portfolio_volatility_annualized": 0.18,
    "benchmark_volatility_annualized": 0.15,
    "tracking_error_annualized": 0.08,
    "notes": ["Dropped 2 day(s) with implausible (>30%) reconstructed portfolio moves"],
}
_DRAWDOWN = {
    "start_date": "2025-06-10",
    "end_date": "2026-06-10",
    "max_drawdown_pct": "-14.6",
    "peak_date": "2025-11-02",
    "trough_date": "2026-01-18",
    "recovery_date": "2026-03-30",
    "days_to_recovery": 71,
    "current_drawdown_pct": "-2.1",
    "annualized_return_pct": "18.2",
    "calmar": "1.25",
    "underwater": [
        {"date": "2025-11-02", "drawdown_pct": "0.0"},
        {"date": "2026-01-18", "drawdown_pct": "-14.6"},
        {"date": "2026-06-10", "drawdown_pct": "-2.1"},
    ],
}
_EXIT_QUALITY = {
    "start_date": "2025-06-10",
    "end_date": "2026-06-10",
    "total_sold_proceeds": "30000.00",
    "total_value_if_held": "33500.00",
    "total_regret_vs_hold": "3500.00",
    "total_spy_value_if_reinvested": "31200.00",
    "total_exit_alpha_vs_spy": "-1200.00",
    "rows": [
        {
            "ticker": "TSLA",
            "name": "Tesla",
            "sold_shares": "100",
            "sold_proceeds": "20000.00",
            "avg_sell_price": "200.00",
            "price_now": "240.00",
            "value_if_held": "24000.00",
            "regret_vs_hold": "4000.00",
            "spy_value_if_reinvested": "21000.00",
            "exit_alpha_vs_spy": "-3000.00",
            "still_held": False,
        },
        {
            "ticker": "META",
            "name": "Meta",
            "sold_shares": "20",
            "sold_proceeds": "10000.00",
            "avg_sell_price": "500.00",
            "price_now": "475.00",
            "value_if_held": "9500.00",
            "regret_vs_hold": "-500.00",
            "spy_value_if_reinvested": "10200.00",
            "exit_alpha_vs_spy": "1800.00",
            "still_held": True,
        },
    ],
}
_AFTER_TAX = {
    "tax_year": 2026,
    "st_rate": "0.37",
    "lt_rate": "0.20",
    "realized_gain_pretax": "12000.00",
    "realized_gain_aftertax": "8740.00",
    "total_tax": "3260.00",
    "by_term": [
        {
            "term": "short",
            "realized_gain_pretax": "4000.00",
            "tax": "1480.00",
            "realized_gain_aftertax": "2520.00",
        },
        {
            "term": "long",
            "realized_gain_pretax": "8000.00",
            "tax": "1600.00",
            "realized_gain_aftertax": "6400.00",
        },
    ],
    "notes": ["Rates are the caller's assumptions; state taxes not modeled."],
}


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
    if "/api/portfolio/performance" in url:
        return _FakeResp(_PERFORMANCE)
    if "/api/portfolio/position-alpha" in url:
        return _FakeResp(_POSITION_ALPHA)
    if "/api/portfolio/positioning" in url:
        return _FakeResp(_POSITIONING)
    if "/api/policy" in url:
        return _FakeResp(_POLICY)
    if "/api/portfolio/beta" in url:
        return _FakeResp(_BETA)
    if "/api/portfolio/drawdown" in url:
        return _FakeResp(_DRAWDOWN)
    if "/api/portfolio/exit-quality" in url:
        return _FakeResp(_EXIT_QUALITY)
    if "/api/portfolio/after-tax" in url:
        return _FakeResp(_AFTER_TAX)
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


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        # Live prod cases the /plaid/items map misses (brokerage sub-accounts).
        ("BrokerageLink Roth", "tax_free"),
        ("Health Savings Account", "tax_free"),
        ("Fidelity HSA", "tax_free"),
        ("SoFi Self-directed", "taxable"),
        ("Robinhood Individual", "taxable"),
        ("BrokerageLink", "taxable"),
        ("Company 401k", "tax_deferred"),
        ("Traditional IRA", "tax_deferred"),
        # "ira" is word-ish: no false positive inside another word.
        ("Admiral Shares", "unknown"),
        ("", "unknown"),
        (None, "unknown"),
    ],
)
def test_tax_treatment_from_name(name: str | None, expected: str) -> None:
    assert ptc.tax_treatment_from_name(name) == expected


def test_positions_fall_back_to_name_when_items_map_misses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    holdings = [
        {
            "ticker": "NU",
            "total_quantity": "100",
            "total_value": "1000.00",
            "accounts": [
                {
                    # account_id 99 is NOT in /plaid/items -> classify by name.
                    "account_id": 99,
                    "account_name": "BrokerageLink Roth",
                    "quantity": "100",
                    "institution_value": "1000.00",
                }
            ],
        }
    ]

    def _get(url: str, timeout: float | None = None, params: object = None) -> _FakeResp:
        if "/holdings" in url:
            return _FakeResp(holdings)
        if "/plaid/items" in url:
            return _FakeResp(_ITEMS)
        return _FakeResp([])

    monkeypatch.setattr(ptc.requests, "get", _get)
    live = fetch_live_portfolio(api_url="http://tracker.test")
    assert live.positions[0].accounts[0].tax_treatment == "tax_free"
    assert live.by_tax_treatment["tax_free"] == pytest.approx(1000.0)


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


def test_fetch_live_parallelizes_independent_reads(monkeypatch: pytest.MonkeyPatch) -> None:
    active = 0
    max_active = 0
    lock = threading.Lock()

    def _get(url: str, timeout: float | None = None, params: object = None) -> _FakeResp:
        nonlocal active, max_active
        with lock:
            active += 1
            max_active = max(max_active, active)
        time.sleep(0.04)
        try:
            return _route(url)
        finally:
            with lock:
                active -= 1

    monkeypatch.setattr(ptc.requests, "get", _get)
    result = fetch_live_portfolio(api_url="http://tracker.test")
    assert result.available is True
    assert max_active >= 2


def test_fetch_degrades_when_unreachable(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(url: str, timeout: float | None = None, params: object = None) -> _FakeResp:
        raise requests.ConnectionError("connection refused")

    monkeypatch.setattr(ptc.requests, "get", _boom)
    live = fetch_live_portfolio(api_url="http://tracker.test")
    assert live.available is False
    assert live.error is not None
    assert "ConnectionError" in live.error
    assert live.positions == []


# ----- transaction history (tax-lot reconstruction feed) -----


def test_fetch_transaction_history_parses_lot_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []

    def _get(url: str, timeout: float | None = None, params: object = None) -> _FakeResp:
        seen.append(url)
        return _FakeResp(
            [
                {
                    "plaid_investment_transaction_id": "t9",
                    "account_id": 6,
                    "account_name": "Fidelity Brokerage",
                    "ticker": "NU",
                    "date": "2025-01-10",
                    "type": "buy",
                    "subtype": "buy",
                    "quantity": "100",
                    "amount": "-5000.00",
                    "price": "50.00",
                    "fees": "1.25",
                }
            ]
        )

    monkeypatch.setattr(ptc.requests, "get", _get)
    txns = ptc.fetch_transaction_history(api_url="http://tracker.test")
    assert txns is not None and len(txns) == 1
    t = txns[0]
    assert (t.account_id, t.price, t.fees) == (6, 50.0, 1.25)
    assert t.quantity == 100.0 and t.type == "buy"
    # The deep-history window + row cap ride the query string.
    assert "start_date=2000-01-01" in seen[0]
    assert f"limit={ptc.TRANSACTION_HISTORY_LIMIT}" in seen[0]


def test_fetch_transaction_history_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(url: str, timeout: float | None = None, params: object = None) -> _FakeResp:
        raise requests.ConnectionError("connection refused")

    monkeypatch.setattr(ptc.requests, "get", _boom)
    assert ptc.fetch_transaction_history(api_url="http://tracker.test") is None


# ----- renderer -----


def test_render_offline_shows_start_hint() -> None:
    html = render_live_portfolio_section(
        LivePortfolio(
            available=False, api_url="http://localhost:8000", error="ConnectionError: nope"
        )
    )
    assert "isn't running" in html  # humane lede, not a raw requests repr
    assert "uvicorn portfolio_tracker.api.main:app" in html  # the manual hint
    # PR6: a one-click start button wired to /actions/start-tracker. Class-
    # scoped (Phase-5 verifier fix 4): the banner renders in BOTH the Health
    # and Allocation composites, so ids would collide across the document.
    assert 'class="pf-start-tracker k-btn k-btn-primary"' in html
    assert "/actions/start-tracker" in html
    # The raw error survives, but only inside the collapsed technical details.
    assert "offline-tech" in html
    assert "ConnectionError: nope" in html
    assert "<!doctype" not in html.lower()


def test_two_offline_banners_coexist_without_duplicate_ids() -> None:
    """Phase-5 verifier fix 4: the tracker-offline banner is emitted by BOTH the
    Synthesis section (Health console) and the Performance section (Allocation
    console). With id-scoped hooks, getElementById always resolved the FIRST
    instance, leaving the second console's Start button dead. The hooks are
    classes now, and the wiring script wires EVERY unwired instance."""
    import re

    live = LivePortfolio(
        available=False, api_url="http://localhost:8000", error="ConnectionError: nope"
    )
    banner = render_live_portfolio_section(live)
    # No id-based hooks left on the banner itself.
    for legacy_id in ("pf-start-tracker", "pf-start-log", "pf-start-msg", "pf-live-offline"):
        assert f'id="{legacy_id}"' not in banner
    # Two instances in one document introduce NO duplicate ids at all.
    doc = banner + banner
    ids = re.findall(r'id="([^"]+)"', doc)
    assert len(ids) == len(set(ids)), f"duplicate ids across two banners: {ids}"
    # Both instances carry a wireable button, scoped per subtree: the script
    # walks every .pf-live-offline banner and wires its own .pf-start-tracker.
    assert doc.count('class="pf-start-tracker k-btn k-btn-primary"') == 2
    assert "querySelectorAll('.pf-live-offline')" in banner
    assert "banner.querySelector('.pf-start-tracker')" in banner
    assert "getElementById('pf-start-tracker')" not in banner
    # The per-button re-wire guard survives (dataset.wired), and the page-level
    # autostart guard still fires the start exactly once per page load.
    assert "btn.dataset.wired" in banner
    assert "window.__pfTrackerAutostart" in banner


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


# ----- analytics client (P2.1) -----


def test_fetch_analytics_parses_all_endpoints(mock_tracker: None) -> None:
    a = fetch_portfolio_analytics(api_url="http://tracker.test")
    assert a.available is True
    assert a.errors == {}

    perf = a.performance
    assert perf is not None
    assert perf.base_value == pytest.approx(100000.0)
    assert [p.date for p in perf.points] == ["2025-06-10", "2025-12-10", "2026-06-10"]
    assert perf.points[-1].portfolio_return_pct == pytest.approx(18.2)
    assert perf.points[-1].policy_return_pct is None  # final-day benchmark gap preserved
    assert perf.net_external_cashflow_in == pytest.approx(25000.0)
    assert perf.backfill_start_unreliable is False

    pa = a.position_alpha
    assert pa is not None
    assert pa.has_policy is True
    by_ticker = {r.ticker: r for r in pa.rows}
    assert by_ticker["NU"].alpha == pytest.approx(2200.0)
    assert by_ticker["NU"].spy_counterfactual_pl == pytest.approx(1300.0)
    assert by_ticker["AAPL"].incomplete is True
    assert pa.total_alpha == pytest.approx(1400.0)
    assert pa.total_alpha_vs_policy == pytest.approx(1700.0)

    pos = a.positioning
    assert pos is not None
    conc = pos.concentration
    assert conc is not None
    assert conc.num_positions == 14
    assert conc.top5_weight_pct == pytest.approx(45.0)
    assert conc.hhi == pytest.approx(950.0)
    assert conc.effective_holdings == pytest.approx(10.5)
    assert [b.label for b in pos.by_sector] == ["Technology", "Financial Services"]
    assert pos.by_account_type[0].weight_pct == pytest.approx(60.0)
    assert pos.by_asset_type[0].count == 12
    assert pos.weighted_avg_correlation_spy == pytest.approx(0.72)
    # L5: the per-ticker correlation/beta rows are no longer discarded.
    assert pos.has_policy is True
    assert len(pos.correlations) == 1
    corr = pos.correlations[0]
    assert corr.ticker == "NU"
    assert corr.beta_spy == pytest.approx(1.4)
    assert corr.beta_qqq == pytest.approx(1.2)
    assert corr.correlation_spy == pytest.approx(0.61)
    assert corr.weight_pct == pytest.approx(12.5)
    assert corr.sample_size == 250

    pol = a.policy
    assert pol is not None
    assert [w.ticker for w in pol.weights] == ["VOO", "QQQ", "BND"]
    assert pol.weights[0].weight_pct == pytest.approx(70.0)
    assert pol.is_balanced is True

    beta = a.beta
    assert beta is not None
    assert beta.benchmark == "SPY"
    assert beta.beta == pytest.approx(1.12)
    assert beta.alpha_annualized_pct == pytest.approx(2.5)
    assert beta.tracking_error_annualized == pytest.approx(0.08)
    assert beta.sample_size == 250
    assert beta.notes and "implausible" in beta.notes[0]
    # Skill-vs-luck trio parses 1:1.
    assert beta.alpha_t_stat == pytest.approx(2.31)
    assert beta.alpha_std_error_annualized_pct == pytest.approx(1.08)
    assert beta.alpha_significant is True


def test_beta_significance_absent_is_tristate_none() -> None:
    # An older tracker that predates the trio: missing key → None, not False.
    payload = {k: v for k, v in _BETA.items() if not k.startswith("alpha_t")}
    payload.pop("alpha_significant")
    payload.pop("alpha_std_error_annualized_pct")
    beta = ptc._parse_beta(payload)
    assert beta.alpha_significant is None
    assert beta.alpha_t_stat is None
    assert beta.alpha_std_error_annualized_pct is None
    # The pre-existing alpha number is untouched.
    assert beta.alpha_annualized_pct == pytest.approx(2.5)


def test_drawdown_and_exit_quality_are_opt_in(mock_tracker: None) -> None:
    # A default fetch does NOT request the two new sections (existing callers
    # keep their round-trip count) — they stay None with no errors entry.
    a = fetch_portfolio_analytics(api_url="http://tracker.test")
    assert a.drawdown is None
    assert a.exit_quality is None
    assert "drawdown" not in a.errors
    assert "exit_quality" not in a.errors


def test_fetch_analytics_only_loads_drawdown_and_exit_quality(mock_tracker: None) -> None:
    a = fetch_portfolio_analytics(api_url="http://tracker.test", only={"drawdown", "exit_quality"})
    assert a.available is True
    # Only the two requested sections loaded.
    assert a.performance is None and a.beta is None
    dd = a.drawdown
    assert dd is not None
    assert dd.max_drawdown_pct == pytest.approx(-14.6)
    assert dd.days_to_recovery == 71
    assert dd.calmar == pytest.approx(1.25)
    assert [p.date for p in dd.underwater] == ["2025-11-02", "2026-01-18", "2026-06-10"]
    assert dd.underwater[1].drawdown_pct == pytest.approx(-14.6)
    eq = a.exit_quality
    assert eq is not None
    assert eq.total_regret_vs_hold == pytest.approx(3500.0)
    assert eq.total_exit_alpha_vs_spy == pytest.approx(-1200.0)
    by_ticker = {r.ticker: r for r in eq.rows}
    assert by_ticker["TSLA"].regret_vs_hold == pytest.approx(4000.0)
    assert by_ticker["TSLA"].still_held is False
    assert by_ticker["META"].exit_alpha_vs_spy == pytest.approx(1800.0)
    assert by_ticker["META"].still_held is True


def test_fetch_drawdown_standalone(mock_tracker: None) -> None:
    dd = ptc.fetch_drawdown(api_url="http://tracker.test")
    assert dd is not None
    assert dd.max_drawdown_pct == pytest.approx(-14.6)
    assert dd.recovery_date == "2026-03-30"


def test_fetch_exit_quality_standalone(mock_tracker: None) -> None:
    eq = ptc.fetch_exit_quality(api_url="http://tracker.test")
    assert eq is not None
    assert len(eq.rows) == 2
    assert eq.total_sold_proceeds == pytest.approx(30000.0)


def test_fetch_exit_quality_totals_from_nested_object(monkeypatch: pytest.MonkeyPatch) -> None:
    # Robust to a tracker that nests totals under a ``totals`` key instead of
    # top-level ``total_*``.
    nested = {
        "start_date": "2025-06-10",
        "end_date": "2026-06-10",
        "rows": _EXIT_QUALITY["rows"],
        "totals": {"total_regret_vs_hold": "999.00", "total_exit_alpha_vs_spy": "111.00"},
    }

    def _get(url: str, timeout: float | None = None, params: object = None) -> _FakeResp:
        return _FakeResp(nested)

    monkeypatch.setattr(ptc.requests, "get", _get)
    eq = ptc.fetch_exit_quality(api_url="http://tracker.test")
    assert eq is not None
    assert eq.total_regret_vs_hold == pytest.approx(999.0)
    assert eq.total_exit_alpha_vs_spy == pytest.approx(111.0)


def test_fetch_after_tax_standalone(mock_tracker: None) -> None:
    at = ptc.fetch_after_tax(
        tax_year=2026, st_rate=0.37, lt_rate=0.20, api_url="http://tracker.test"
    )
    assert at is not None
    assert at.tax_year == 2026
    assert at.total_tax == pytest.approx(3260.0)
    assert at.realized_gain_aftertax == pytest.approx(8740.0)
    by_term = {t.term: t for t in at.by_term}
    assert by_term["short"].tax == pytest.approx(1480.0)
    assert by_term["long"].realized_gain_aftertax == pytest.approx(6400.0)
    assert at.notes and "assumptions" in at.notes[0]


def test_after_tax_passes_params(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, str] = {}

    def _get(url: str, timeout: float | None = None, params: object = None) -> _FakeResp:
        seen["url"] = url
        return _FakeResp(_AFTER_TAX)

    monkeypatch.setattr(ptc.requests, "get", _get)
    ptc.fetch_after_tax(tax_year=2026, st_rate=0.37, lt_rate=0.2, api_url="http://tracker.test")
    assert "tax_year=2026" in seen["url"]
    assert "st_rate=0.37" in seen["url"]
    assert "lt_rate=0.2" in seen["url"]


def test_new_fetchers_degrade_to_none_when_unreachable(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(url: str, timeout: float | None = None, params: object = None) -> _FakeResp:
        raise requests.ConnectionError("connection refused")

    monkeypatch.setattr(ptc.requests, "get", _boom)
    assert ptc.fetch_drawdown(api_url="http://tracker.test") is None
    assert ptc.fetch_exit_quality(api_url="http://tracker.test") is None
    assert ptc.fetch_after_tax(tax_year=2026, api_url="http://tracker.test") is None


def test_fetch_analytics_degrades_when_unreachable(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"n": 0}

    def _boom(url: str, timeout: float | None = None, params: object = None) -> _FakeResp:
        calls["n"] += 1
        raise requests.ConnectionError("connection refused")

    monkeypatch.setattr(ptc.requests, "get", _boom)
    a = fetch_portfolio_analytics(api_url="http://tracker.test")
    assert a.available is False
    assert set(a.errors) == {"performance", "position_alpha", "positioning", "policy", "beta"}
    assert all("ConnectionError" in e for e in a.errors.values())
    # Host-down short-circuits the remaining endpoints: ONE socket attempt.
    assert calls["n"] == 1
    assert a.performance is None and a.beta is None


def test_fetch_analytics_parallelizes_independent_sections(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    active = 0
    max_active = 0
    lock = threading.Lock()

    def _get(url: str, timeout: float | None = None, params: object = None) -> _FakeResp:
        nonlocal active, max_active
        if "/performance" in url:
            return _route(url)
        with lock:
            active += 1
            max_active = max(max_active, active)
        time.sleep(0.04)
        try:
            return _route(url)
        finally:
            with lock:
                active -= 1

    monkeypatch.setattr(ptc.requests, "get", _get)
    result = fetch_portfolio_analytics(api_url="http://tracker.test")
    assert result.available is True
    assert max_active >= 2


def test_fetch_analytics_partial_failure_isolates_the_failed_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _get(url: str, timeout: float | None = None, params: object = None) -> _FakeResp:
        if "/api/portfolio/positioning" in url:
            return _FakeResp({}, 500)
        return _route(url)

    monkeypatch.setattr(ptc.requests, "get", _get)
    a = fetch_portfolio_analytics(api_url="http://tracker.test")
    assert a.available is True  # the other four sections still loaded
    assert set(a.errors) == {"positioning"}
    assert a.positioning is None
    assert a.performance is not None
    assert a.position_alpha is not None
    assert a.policy is not None
    assert a.beta is not None


# ----- analytics renderer (P2.1) -----


def test_render_analytics_sections_populated(mock_tracker: None) -> None:
    a = fetch_portfolio_analytics(api_url="http://tracker.test")
    html = render_portfolio_analytics_sections(a)
    # Performance: signed TWR card, the excess-vs-SPY readout, legend + chart,
    # and the policy-mix context line.
    assert "Performance vs benchmarks" in html
    assert "+18.2%" in html
    assert "+6.7pp" in html  # 18.2 - 11.5, a display delta of two API values
    assert 'class="pf-chart"' in html
    assert "Policy mix:" in html and "VOO 70%" in html
    assert "+4.2%" in html  # policy falls back to its last valid point
    # Risk strip (fraction-united fields render as percent).
    assert "Risk &amp; efficiency" in html
    assert "Beta vs SPY" in html and "1.12" in html
    assert "18.0%" in html  # portfolio sigma from the 0.18 fraction
    # Positioning cuts + concentration cards.
    assert "Positioning &amp; concentration" in html
    assert "Top 5" in html and "45.0%" in html
    assert "Technology" in html and "By account type" in html
    assert "Effective holdings" in html
    # Alpha table: sorted by alpha (NU first), incomplete flag, totals, the
    # has_policy=True column.
    assert "Per-position alpha" in html
    assert html.index("research/NU/") < html.index("research/AAPL/")
    assert "pf-total" in html
    # The has_policy=True column renders, now as a sortable living-grid header.
    assert "vs policy" in html and "sortBy('policy','num')" in html
    assert 'class="pf-flag"' in html  # AAPL's incomplete-window marker
    assert "<!doctype" not in html.lower()


def test_render_analytics_partial_notes_missing_sections(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _get(url: str, timeout: float | None = None, params: object = None) -> _FakeResp:
        if "/api/portfolio/positioning" in url:
            return _FakeResp({}, 500)
        return _route(url)

    monkeypatch.setattr(ptc.requests, "get", _get)
    a = fetch_portfolio_analytics(api_url="http://tracker.test")
    html = render_portfolio_analytics_sections(a)
    assert "Positioning &amp; concentration" not in html
    assert "Unavailable from the tracker right now: Positioning." in html
    assert "Performance vs benchmarks" in html  # the others still render


# ----- page composition (P2.1) -----


def test_compose_page_offline_renders_single_note() -> None:
    analytics = PortfolioAnalytics(
        available=False,
        api_url="http://localhost:8000",
        errors=dict.fromkeys(
            ("performance", "position_alpha", "positioning", "policy", "beta"),
            "ConnectionError: refused",
        ),
    )
    live = LivePortfolio(
        available=False, api_url="http://localhost:8000", error="ConnectionError: nope"
    )
    html = compose_portfolio_page(analytics, live)
    # Tracker fully down → exactly ONE offline note (the live section's, which
    # carries the start hint) — not a second analytics offline panel.
    assert html.count("offline-tech") == 1
    assert "uvicorn portfolio_tracker.api.main:app" in html
    assert "Portfolio analytics" not in html
    # The synthesis layer moved to its own sub-tab — Performance carries none
    # of it (no insights grid, no next-dollar, no lens memo).
    assert 'class="pf-insights"' not in html
    assert "Where the next dollar goes" not in html
    assert "Portfolio synthesis" not in html


def test_compose_page_analytics_down_live_up_notes_quietly() -> None:
    analytics = PortfolioAnalytics(
        available=False, api_url="http://x", errors={"performance": "HTTPError: HTTP 404"}
    )
    live = LivePortfolio(
        available=True,
        api_url="http://x",
        total_market_value=12000.0,
        positions=[
            LivePosition(
                ticker="NU",
                name="Nu Holdings",
                quantity=1000.0,
                market_value=12000.0,
                cost_basis=9000.0,
                unrealized_pnl=3000.0,
                percent_of_portfolio=100.0,
                accounts=[TaxLot(5, "RH Roth IRA", 1000.0, 12000.0, "tax_free")],
            )
        ],
        by_tax_treatment={"taxable": 0.0, "tax_deferred": 0.0, "tax_free": 12000.0, "unknown": 0.0},
    )
    html = compose_portfolio_page(analytics, live)
    # An older tracker build (reachable, but no analytics routes) gets one
    # quiet note naming the failure, and the live book still renders.
    assert "analytics endpoints aren't" in html
    assert "HTTP 404" in html
    assert "Live portfolio" in html and "NU" in html


# ----- analytics window (editable from the Portfolio page) -----


def test_fetch_analytics_passes_window_params(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []

    def _get(url: str, timeout: float | None = None, params: object = None) -> _FakeResp:
        seen.append(url)
        return _route(url)

    monkeypatch.setattr(ptc.requests, "get", _get)
    a = fetch_portfolio_analytics(
        api_url="http://tracker.test",
        start_date="2026-01-01",
        end_date="2026-06-10",
        include_backfill=True,
    )
    assert a.available is True
    by_endpoint = {u.split("?")[0].rsplit("/", 1)[-1]: u for u in seen}
    # The four windowed endpoints get the dates; only /performance gets the
    # backfill flag; /api/policy is window-less.
    perf = by_endpoint["performance"]
    assert "start_date=2026-01-01" in perf and "end_date=2026-06-10" in perf
    assert "include_backfill=true" in perf
    for key in ("position-alpha", "positioning", "beta"):
        assert "start_date=2026-01-01" in by_endpoint[key]
        assert "end_date=2026-06-10" in by_endpoint[key]
        assert "include_backfill" not in by_endpoint[key]
    assert "?" not in by_endpoint["policy"]


def test_fetch_analytics_default_omits_window_params(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []

    def _get(url: str, timeout: float | None = None, params: object = None) -> _FakeResp:
        seen.append(url)
        return _route(url)

    monkeypatch.setattr(ptc.requests, "get", _get)
    fetch_portfolio_analytics(api_url="http://tracker.test")
    # No window chosen -> the tracker's own defaults, no query strings at all.
    assert seen and all("?" not in u for u in seen)


def test_validated_window_sanitizes() -> None:
    w = validated_window("2026-01-01", "2026-06-10", True)
    assert (w.start_date, w.end_date, w.include_backfill) == ("2026-01-01", "2026-06-10", True)
    # Garbage dates are dropped individually.
    partial = validated_window("junk", "2026-06-10", False)
    assert partial.start_date is None
    assert partial.end_date == "2026-06-10"
    # An inverted range falls back to the tracker defaults entirely.
    inverted = validated_window("2026-06-10", "2026-01-01", False)
    assert inverted.start_date is None and inverted.end_date is None
    # Absent input is the default window.
    assert validated_window(None, None, False) == WindowSelection()


def test_compose_page_embeds_window_bar_with_echoed_values(mock_tracker: None) -> None:
    # The window controls now ride in the Performance panel header (embedded
    # with the chart they drive), echoing the applied window into the inputs.
    analytics = fetch_portfolio_analytics(api_url="http://tracker.test")
    live = LivePortfolio(available=True, api_url="http://x", total_market_value=0.0)
    window = WindowSelection(start_date="2026-01-01", end_date="2026-06-10", include_backfill=True)
    html = compose_portfolio_page(analytics, live, window=window)
    assert 'class="pf-perf-head"' in html  # embedded in the panel header, not a top bar
    assert 'id="pf-window-bar"' in html
    assert 'value="2026-01-01"' in html and 'value="2026-06-10"' in html
    assert 'id="pf-backfill" checked' in html
    assert 'data-preset="ytd"' in html and 'data-preset="default"' in html
    assert "/api/panel/portfolio" in html  # the refetch script targets this panel
    # The book attribution narrative is no longer mounted on the page.
    assert "Attribution narratives" not in html


def test_compose_page_default_window_bar_is_unset(mock_tracker: None) -> None:
    analytics = fetch_portfolio_analytics(api_url="http://tracker.test")
    live = LivePortfolio(available=True, api_url="http://x", total_market_value=0.0)
    html = compose_portfolio_page(analytics, live)
    assert 'id="pf-window-bar"' in html
    assert 'id="pf-start" value=""' in html and 'id="pf-end" value=""' in html
    assert 'id="pf-backfill">' in html  # unchecked


def test_compose_page_offline_leads_with_start_banner() -> None:
    # Tracker down → the whole page is gated, so the start-tracker banner LEADS
    # the page (and auto-starts on open); there is no window bar without a chart.
    analytics = PortfolioAnalytics(
        available=False,
        api_url="http://localhost:8000",
        errors=dict.fromkeys(
            ("performance", "position_alpha", "positioning", "policy", "beta"),
            "ConnectionError: refused",
        ),
    )
    live = LivePortfolio(
        available=False, api_url="http://localhost:8000", error="ConnectionError: nope"
    )
    html = compose_portfolio_page(analytics, live)
    # Class-scoped hook (Phase-5 verifier fix 4 — the banner also renders in
    # the Health console, so an id would collide across the document).
    assert "pf-live-offline" in html and "Start tracker" in html
    assert "__pfTrackerAutostart" in html  # auto-starts when the page opens
    assert 'id="pf-window-bar"' not in html  # no chart, so no window controls
    assert html.count("<section") == 1  # just the banner — nothing buried below it


# ----- Portfolio → Synthesis tab: rollup / exposure / next-dollar / memo -----


def test_synthesis_page_empty_db_renders_only_the_memo(tmp_path: Path) -> None:
    """No substrate → no insight panels (hide-don't-stub); the lens-memo
    fragment passes through untouched."""
    live = LivePortfolio(available=False, api_url="http://x", error="down")
    html = compose_synthesis_page(tmp_path / "missing.db", live, "<div>MEMO</div>")
    assert "MEMO" in html
    assert 'class="pf-insights"' not in html
    assert "Thesis health" not in html
    assert "Where the next dollar goes" not in html


def test_synthesis_page_rollup_and_next_dollar(tmp_path: Path) -> None:
    import sqlite3

    db = tmp_path / "data" / "portfolio.db"
    db.parent.mkdir(parents=True)
    conn = sqlite3.connect(db)
    conn.executescript(
        "CREATE TABLE tracked_companies (ticker TEXT, list_type TEXT, archived_at TEXT);"
        "CREATE TABLE thesis_evaluations (ticker TEXT, overall_status TEXT, evaluated_at TEXT);"
        "CREATE TABLE advisor_memos (id INTEGER PRIMARY KEY, kind TEXT, title TEXT,"
        " body_md TEXT, created_at TEXT);"
    )
    conn.executemany(
        "INSERT INTO tracked_companies VALUES (?, 'portfolio', NULL)",
        [("NU",), ("MELI",), ("WIX",)],
    )
    conn.executemany(
        "INSERT INTO thesis_evaluations VALUES (?, ?, ?)",
        [
            ("NU", "ok", "2026-06-01"),
            ("NU", "warn", "2026-05-01"),  # older row must NOT win
            ("MELI", "ok", "2026-06-01"),
            ("WIX", "watch", "2026-06-01"),
        ],
    )
    conn.execute(
        "INSERT INTO advisor_memos (kind, title, body_md, created_at) VALUES"
        " ('next_dollar', 'Next-dollar memo', '## Where it works hardest **MELI**',"
        " '2026-06-10')"
    )
    conn.commit()
    conn.close()

    live = LivePortfolio(available=False, api_url="http://x", error="down")
    html = compose_synthesis_page(db, live, "")
    # Rollup: latest-eval-wins, flagged chip deep-links to the Holding tab.
    assert "Thesis health" in html
    assert "2 OK" in html and "1 flagged" in html
    assert 'href="#holding=WIX"' in html
    assert "WIX" in html
    # P0.4b: Health no longer renders the next-dollar distribution/memo at
    # all — it points to the governed Incremental Dollar Recommendation on
    # Portfolio -> Allocation instead (PRD §6/§7.4).
    assert "Where the next dollar goes" not in html
    assert 'class="pf-nd-row"' not in html
    assert "Incremental Dollar Recommendation" in html
    assert 'href="/#portfolio_allocation"' in html


def _next_dollar_fixture(tmp_path: Path) -> tuple[Path, Path]:
    """Repo root with the full next-dollar substrate: three portfolio names,
    dcf_runs rows, synthetic price charts (BBB tracks AAA; CCC independent),
    and a next-dollar advisor memo. Macro tables are absent on purpose — the
    factor must hide itself and renormalize the blend."""
    import json
    import sqlite3
    from datetime import date, timedelta

    import numpy as np

    repo_root = tmp_path
    db = repo_root / "data" / "portfolio.db"
    db.parent.mkdir(parents=True)
    conn = sqlite3.connect(db)
    conn.executescript(
        "CREATE TABLE tracked_companies (ticker TEXT, list_type TEXT, archived_at TEXT);"
        "CREATE TABLE advisor_memos (id INTEGER PRIMARY KEY, kind TEXT, title TEXT,"
        " body_md TEXT, created_at TEXT);"
        "CREATE TABLE dcf_runs (id INTEGER PRIMARY KEY AUTOINCREMENT, ticker TEXT,"
        " valuation_date TEXT, npv_per_share NUMERIC, live_price FLOAT, created_at TEXT);"
    )
    conn.executemany(
        "INSERT INTO tracked_companies VALUES (?, 'portfolio', NULL)",
        [("AAA",), ("BBB",), ("CCC",)],
    )
    conn.execute(
        "INSERT INTO advisor_memos (kind, title, body_md, created_at) VALUES"
        " ('next_dollar', 'Next-dollar memo', '## Narrative **layer**', '2026-06-10')"
    )
    conn.executemany(
        "INSERT INTO dcf_runs (ticker, valuation_date, npv_per_share, live_price, created_at)"
        " VALUES (?, '2026-06-08', ?, 100.0, '2026-06-08 00:00:00')",
        [("AAA", 150.0), ("BBB", 90.0), ("CCC", 120.0)],
    )
    conn.commit()
    conn.close()

    days: list[date] = []
    d = date.today() - timedelta(days=2)
    while len(days) < 200:
        if d.weekday() < 5:
            days.append(d)
        d -= timedelta(days=1)
    days.reverse()
    rng = np.random.default_rng(0)
    base = rng.normal(0.0005, 0.02, 200)
    noise = rng.normal(0.0, 0.005, 200)
    indep = rng.normal(0.0005, 0.02, 200)
    fmp = repo_root / "data" / "historical" / "fmp"
    fmp.mkdir(parents=True)
    for ticker, rets in (("AAA", base), ("BBB", 0.9 * base + noise), ("CCC", indep)):
        prices = 100.0 * np.exp(np.cumsum(rets))
        rows = [
            {"date": days[i].isoformat(), "adjClose": round(float(prices[i]), 6)}
            for i in range(200)
        ][::-1]  # newest first, like FMP
        (fmp / f"{ticker}_price_chart_10y_div_adj.json").write_text(
            json.dumps(rows), encoding="utf-8"
        )
    return repo_root, db


def _position(ticker: str, value: float) -> LivePosition:
    return LivePosition(
        ticker=ticker,
        name=ticker,
        quantity=1.0,
        market_value=value,
        cost_basis=None,
        unrealized_pnl=None,
        percent_of_portfolio=None,
    )


def test_next_dollar_distribution_with_tracker_weights(tmp_path: Path) -> None:
    """P0.4b: ``render_next_dollar_panel`` no longer renders inside
    ``compose_synthesis_page`` (Health), but the function itself — and this
    coverage of its quantitative distribution model — is unchanged; it is
    called directly here (as the peek/markup-contract tests already do)."""
    import re

    _repo_root, db = _next_dollar_fixture(tmp_path)
    live = LivePortfolio(
        available=True,
        api_url="http://x",
        positions=[_position("AAA", 5000.0), _position("BBB", 3000.0), _position("CCC", 2000.0)],
    )
    html = render_next_dollar_panel(db, live)

    # Distribution bars render, weighted by the tracker's live values.
    assert 'class="pf-nd-row"' in html
    assert "tracker-weighted" in html
    assert "now 50.0%" in html  # AAA = 5000 / 10000
    assert 'href="../research/AAA/"' in html
    # Macro tables absent -> factor hidden, blend renormalized and labelled.
    assert "expected return 62% / diversification 38%" in html
    assert "macro tilt hidden" in html
    # Provenance sub-line carries the covariance window + shrinkage.
    assert "daily returns through" in html
    assert "LW shrink" in html
    # Waterfall chips: signed contribution per factor, raw in the tooltip.
    assert "expected return +" in html
    assert "z +" in html and "raw +50.0%" in html
    assert "corr to book" in html
    # The softmax shares sum to ~100 across the three rows.
    allocs = [float(m) for m in re.findall(r'pf-nd-alloc">([0-9.]+)%', html)]
    assert len(allocs) == 3
    assert sum(allocs) == pytest.approx(100.0, abs=0.2)
    assert allocs == sorted(allocs, reverse=True)
    # The advisor memo keeps its excerpt below, under its own sub-heading.
    assert "Advisor memo" in html
    assert "Narrative" in html and "##" not in html.split("Advisor memo")[1][:200]
    assert 'href="#advisor_memos"' in html


def test_next_dollar_equal_weight_when_tracker_down(tmp_path: Path) -> None:
    """P0.4b: called directly (see the sibling test's note above)."""
    _repo_root, db = _next_dollar_fixture(tmp_path)
    live = LivePortfolio(available=False, api_url="http://x", error="down")
    html = render_next_dollar_panel(db, live)
    assert 'class="pf-nd-row"' in html
    assert "equal-weighted" in html
    assert "now 33.3%" in html


def test_synthesis_page_layout_order(tmp_path: Path) -> None:
    """The Synthesis tab's shape: the rollup/exposure insights grid first, the
    next-dollar POINTER (P0.4b — the full distribution moved to Portfolio ->
    Allocation) below it (NOT a grid cell — the grid wrapper closes before
    the section opens), the lens memo last."""
    _repo_root, db = _next_dollar_fixture(tmp_path)
    live = LivePortfolio(available=False, api_url="http://x", error="down")
    memo = '<section class="panel synthesis-panel">MEMO</section>'
    html = compose_synthesis_page(db, live, memo)
    grid = html.index('class="pf-insights"')  # exposure renders (equal-weight)
    nd = html.index("Incremental Dollar Recommendation")
    assert grid < nd < html.index("synthesis-panel")
    assert '</div><section class="panel"><h2>Next dollar</h2>' in html


# ----- Portfolio → Risk tab (L5): drawdown · factor exposure · macro stress -----


def test_render_risk_panel_populated(mock_tracker: None) -> None:
    """Live tracker → the benchmark-risk recap, drawdown, factor/style exposure
    (rolled up from the now-kept correlation rows), and the macro-stress picker."""
    html = render_portfolio_risk_panel(api_url="http://tracker.test", db_path=None)
    assert 'id="pfr-root"' in html
    # Benchmark-risk recap (reuses the Performance tab's risk strip).
    assert "Risk &amp; efficiency" in html and "Beta vs SPY" in html
    # Drawdown — the mock TWR series climbs monotonically, so no drawdown.
    assert "Drawdown" in html
    assert "no drawdown in window" in html and "none needed" in html
    # Factor exposure rolled up from the per-ticker rows (NU β_spy 1.4, single name).
    assert "Factor &amp; style exposure" in html
    assert "Market β (SPY)" in html and "1.40" in html
    assert "Growth β (QQQ)" in html and "Growth tilt" in html
    assert "Crowding" in html
    assert "names priced" in html
    assert "Value / size / momentum" in html  # honest coverage note
    assert "Most market-sensitive" in html
    # Macro-stress picker + the SSE wiring + every scenario option.
    assert "Whole-book macro stress" in html
    assert 'id="pfr-scenario"' in html and 'id="pfr-run-scenario"' in html
    assert "/actions/run-scenario" in html and "/api/panel/portfolio_risk" in html
    for sid in ("fed_cuts_50bps", "recession_2026", "oil_to_50"):
        assert f'value="{sid}"' in html
    # No digest cached (db_path=None) → the run-it hint, not a stale digest.
    assert "No stress digest cached yet" in html
    assert "<!doctype" not in html.lower()


def test_compose_risk_page_offline_keeps_macro_stress() -> None:
    """Tracker down → the tracker-fed sections degrade to one offline note, but
    the macro-stress section (local cache + picker) still renders."""
    analytics = PortfolioAnalytics(
        available=False,
        api_url="http://localhost:8000",
        errors={"performance": "ConnectionError: refused"},
    )
    html = compose_risk_page(
        analytics,
        drawdown=None,
        factor=None,
        scenarios=[("fed_cuts_50bps", "Fed cuts 50bps in one move")],
        digest="",
    )
    assert "Risk &amp; drawdown" in html  # the offline note
    assert "live portfolio tracker" in html
    assert "Drawdown</h2>" not in html  # no live drawdown section
    # Macro stress survives the tracker outage.
    assert "Whole-book macro stress" in html
    assert 'value="fed_cuts_50bps"' in html


def test_compose_risk_page_renders_drawdown_and_cached_digest() -> None:
    """A real drawdown: KPI cards (recovery), the underwater SVG, and a passed-in
    cached digest block render together."""
    points = [
        PerformancePoint("2026-01-01", 0.0, None, None, None),
        PerformancePoint("2026-02-01", 10.0, None, None, None),
        PerformancePoint("2026-03-01", -2.0, None, None, None),  # 0.98/1.10-1 underwater
        PerformancePoint("2026-04-01", 12.0, None, None, None),  # new high → recovered
    ]
    dd = compute_drawdown(points)
    factor = factor_exposure_rollup(
        [
            PositionCorrelationRow(
                None, "NU", "Nu", 100.0, 100.0, 250, 0.6, 1.4, 0.7, 1.2, None, None
            )
        ]
    )
    analytics = PortfolioAnalytics(available=True, api_url="http://x")
    html = compose_risk_page(
        analytics,
        drawdown=dd,
        factor=factor,
        scenarios=[("oil_to_50", "Brent crashes to ~$50/bbl")],
        digest='<h3 class="panel-h3">Stress digest</h3><p>DIGEST-BODY</p>',
    )
    assert "Max drawdown" in html and "-10.9%" in html
    assert "Time to recovery" in html
    assert 'class="pfr-uw"' in html  # the underwater curve SVG
    assert "Factor &amp; style exposure" in html and "1.40" in html
    # The cached digest is spliced into the macro-stress section verbatim.
    assert "DIGEST-BODY" in html
    assert "No stress digest cached yet" not in html
