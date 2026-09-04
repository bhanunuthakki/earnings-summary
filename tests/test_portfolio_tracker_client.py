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

import json
import threading
import time
from collections.abc import Callable
from copy import deepcopy
from pathlib import Path
from typing import cast

import pytest
import requests

from integrations import portfolio_tracker_client as ptc
from integrations.portfolio_tracker_client import (
    LivePortfolio,
    LivePosition,
    PerformancePoint,
    PerformanceSeries,
    PortfolioAnalytics,
    PositionCorrelationRow,
    TaxLot,
    fetch_live_portfolio,
    fetch_portfolio_analytics,
    tax_treatment,
)
from pipeline import portfolio_panel as portfolio_panel_module
from pipeline.portfolio_panel import (
    WindowSelection,
    backfill_warning,
    compose_portfolio_page,
    compose_risk_page,
    compose_synthesis_page,
    render_live_portfolio_section,
    render_next_dollar_panel,
    render_portfolio_analytics_sections,
    render_portfolio_panel,
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
def _benchmark_price_inputs(ticker: str) -> list[dict[str, str]]:
    return [
        {
            "ticker": ticker,
            "target_date": target_date,
            "source_date": target_date,
            "close": "100.00",
            "resolution": "same_day_close",
        }
        for target_date in ("2025-06-10", "2025-12-10", "2026-06-10")
    ]


_PERFORMANCE: dict[str, object] = {
    "methodology": "performance.modified_dietz",
    "methodology_version": "2",
    "calculation_status": "available",
    "calculation_reason_codes": [],
    "source_coverage": {
        "status": "complete",
        "is_complete": True,
        "requested_start_date": "2025-06-10",
        "requested_end_date": "2026-06-10",
        "required_start_date": "2025-06-11",
        "required_end_date": "2026-06-10",
        "accounts": [],
        "attestations": [],
    },
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
            # Final day is fully covered; available responses may not carry
            # partial required benchmark legs.
            "date": "2026-06-10",
            "portfolio_value": "143200.00",
            "portfolio_return_pct": "18.2",
            "spy_return_pct": "11.5",
            "qqq_return_pct": "14.1",
            "policy_return_pct": "13.0",
            "spy_equivalent_value": "136500.00",
            "qqq_equivalent_value": "139100.00",
            "policy_equivalent_value": "138000.00",
        },
    ],
    "earliest_observed_date": "2025-06-12",
    "net_external_cashflow_in": "25000.00",
    "backfill_start_unreliable": False,
    "opening_value_provenance": "modeled_transaction_walkback",
    "ending_value_provenance": "observed_complete_snapshot",
    "valuation_account_ids": [1],
    "equation_receipt": {
        "calculation_id": "calc-1",
        "external_flow_ledger_id": "ledger-1",
        "portfolio_valuation_input_id": "valuation-1",
        "included_account_ids": [1],
        "requested_start_date": "2025-06-10",
        "requested_end_date": "2026-06-10",
        "benchmark_price_resolution_policy": "same_day_or_previous_us_market_close",
        "opening_value": "100000.00",
        "dated_external_cashflows": [{"date": "2026-06-10", "amount": "25000.00"}],
        "net_external_cashflow_in": "25000.00",
        "ending_value": "143200.00",
        "investment_gain": "18200.00",
        "modified_dietz_denominator": "100000.00",
        "portfolio_return_pct": "18.2",
        "portfolio_equation_residual": "0",
        "spy": {
            "benchmark": "SPY",
            "ending_value": "136500.00",
            "investment_gain": "11500.00",
            "return_pct": "11.5",
            "dollar_alpha": "6700.00",
            "percentage_point_alpha": "6.7",
            "equation_residual": "0",
            "price_input_id": "sha256:4ca7ea8a7360cd1c1f5e787e4d34b34d6cf0dfa8aa29c0da0eef3224e7b32b37",
            "price_inputs": _benchmark_price_inputs("SPY"),
        },
        "qqq": {
            "benchmark": "QQQ",
            "ending_value": "139100.00",
            "investment_gain": "14100.00",
            "return_pct": "14.1",
            "dollar_alpha": "4100.00",
            "percentage_point_alpha": "4.1",
            "equation_residual": "0",
            "price_input_id": "sha256:e346d2f71d6f2ae21b22772bd08e9533b6de05a4e3cc9b52b8cd930047010dca",
            "price_inputs": _benchmark_price_inputs("QQQ"),
        },
        "policy": {
            "benchmark": "policy",
            "ending_value": "138000.00",
            "investment_gain": "13000.00",
            "return_pct": "13.0",
            "dollar_alpha": "5200.00",
            "percentage_point_alpha": "5.2",
            "equation_residual": "0",
            "price_input_id": "sha256:6e7ddb8328ce5b1d36b23332cd087d19a78855175f85ac9a5c00dbf345b2c113",
            "price_inputs": _benchmark_price_inputs("SPY"),
        },
    },
}
_POSITION_ALPHA = {
    "methodology": "position_alpha.split_normalized_price_trade_modified_dietz",
    "methodology_version": "3",
    "start_date": "2025-06-10",
    "end_date": "2026-06-10",
    "calculation_status": "available",
    "calculation_reason_codes": [],
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
            "incomplete": False,
        },
    ],
    "total_actual_pl": "2700.00",
    "total_spy_pl": "1300.00",
    "total_qqq_pl": "1600.00",
    "total_policy_pl": "1000.00",
    "total_alpha": "1400.00",
    "total_alpha_vs_qqq": "1100.00",
    "total_alpha_vs_policy": "1700.00",
    "series": [
        {
            "date": "2025-06-10",
            "portfolio_value": "30000.00",
            "spy_counterfactual_value": "30000.00",
            "qqq_counterfactual_value": "30000.00",
            "policy_counterfactual_value": "30000.00",
            "position_cashflow": "0.00",
            "portfolio_return_pct": "0.0000",
            "spy_return_pct": "0.0000",
            "qqq_return_pct": "0.0000",
            "policy_return_pct": "0.0000",
        },
        {
            "date": "2026-06-10",
            "portfolio_value": "32700.00",
            "spy_counterfactual_value": "31300.00",
            "qqq_counterfactual_value": "31600.00",
            "policy_counterfactual_value": "31000.00",
            "position_cashflow": "0.00",
            "portfolio_return_pct": "9.0000",
            "spy_return_pct": "4.3333",
            "qqq_return_pct": "5.3333",
            "policy_return_pct": "3.3333",
        },
    ],
    "v_start": "30000.00",
    "v_end": "29700.00",
    "has_policy": True,
    "matched_returns": {
        "dietz_denominator": "30000.00",
        "portfolio_return_pct": "9.0000",
        "spy_return_pct": "4.3333",
        "qqq_return_pct": "5.3333",
        "policy_return_pct": "3.3333",
        "alpha_vs_spy_pct": "4.6667",
        "alpha_vs_qqq_pct": "3.6667",
        "alpha_vs_policy_pct": "5.6667",
    },
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
    "methodology": "risk.beta_drawdown",
    "methodology_version": "2",
    "calculation_status": "available",
    "calculation_reason_codes": [],
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
    "methodology": "risk.beta_drawdown",
    "methodology_version": "2",
    "calculation_status": "available",
    "calculation_reason_codes": [],
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
    assert "Windows Task Scheduler" in html
    assert r"\earnings-summary\portfolio_tracker_api" in html
    assert "uvicorn portfolio_tracker.api.main:app" not in html
    assert "check the log below" not in html
    assert "inspect the Scheduler task details below" in html
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
    assert perf.points[-1].policy_return_pct == pytest.approx(13.0)
    assert perf.net_external_cashflow_in == pytest.approx(25000.0)
    assert perf.backfill_start_unreliable is False

    pa = a.position_alpha
    assert pa is not None
    assert pa.has_policy is True
    by_ticker = {r.ticker: r for r in pa.rows}
    assert by_ticker["NU"].alpha == pytest.approx(2200.0)
    assert by_ticker["NU"].spy_counterfactual_pl == pytest.approx(1300.0)
    assert by_ticker["AAPL"].incomplete is False
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


def test_legacy_position_alpha_uses_effective_performance_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def _get(url: str, timeout: float | None = None, params: object = None) -> _FakeResp:
        calls.append(url)
        return _route(url)

    monkeypatch.setattr(ptc.requests, "get", _get)
    analytics = fetch_portfolio_analytics(
        api_url="http://tracker.test", only={"performance", "position_alpha"}
    )

    assert analytics.performance is not None
    assert analytics.position_alpha is not None
    alpha_url = next(url for url in calls if "/api/portfolio/position-alpha" in url)
    assert "start_date=2025-06-10" in alpha_url
    assert "end_date=2026-06-10" in alpha_url


def test_v1_position_alpha_defaults_missing_matched_metrics_to_null() -> None:
    from integrations.portfolio_tracker_v1 import PositionAlphaResult

    legacy_payload = deepcopy(_POSITION_ALPHA)
    legacy_payload.pop("matched_returns")

    parsed = PositionAlphaResult.model_validate(legacy_payload)

    assert parsed.matched_returns.portfolio_return_pct is None
    assert parsed.matched_returns.alpha_vs_spy_pct is None


def test_v1_position_alpha_uses_rebased_performance_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture_dir = Path(__file__).parent / "fixtures" / "tracker_v1"
    performance_payload = cast(
        "dict[str, object]", json.loads((fixture_dir / "performance.json").read_text())
    )
    alpha_payload = cast(
        "dict[str, object]",
        json.loads((fixture_dir / "position-performance.json").read_text()),
    )
    alpha_params: dict[str, object] = {}

    def _get(
        self: requests.Session,
        url: str,
        params: object = None,
        timeout: object = None,
    ) -> _FakeResp:
        if "/api/v1/analytics/performance" in url:
            payload = deepcopy(performance_payload)
            assert isinstance(payload, dict)
            series = cast("dict[str, object]", payload["series"])
            if isinstance(params, dict) and params.get("start_date"):
                series["start_date"] = params["start_date"]
                series["earliest_observed_date"] = params["start_date"]
            return _FakeResp(payload)
        if "/api/v1/analytics/position-performance" in url:
            assert isinstance(params, dict)
            alpha_params.update(cast("dict[str, object]", params))
            return _FakeResp(alpha_payload)
        raise requests.ConnectionError(f"unexpected route {url}")

    monkeypatch.setenv("PORTFOLIO_TRACKER_V1_READS", "1")
    monkeypatch.setattr(requests.Session, "get", _get)
    analytics = fetch_portfolio_analytics(only={"performance", "position_alpha"})

    assert analytics.performance is not None
    assert analytics.performance.start_date == "2026-07-22"
    assert alpha_params["start_date"] == "2026-07-22"
    assert alpha_params["end_date"] == "2026-07-22"


def test_beta_significance_absent_is_tristate_none() -> None:
    # An older tracker that predates the trio: missing key → None, not False.
    payload: dict[str, object] = {k: v for k, v in _BETA.items() if not k.startswith("alpha_t")}
    payload.pop("alpha_significant")
    payload.pop("alpha_std_error_annualized_pct")
    beta = ptc.parse_beta(payload)
    assert beta.alpha_significant is None
    assert beta.alpha_t_stat is None
    assert beta.alpha_std_error_annualized_pct is None
    # The pre-existing alpha number is untouched.
    assert beta.alpha_annualized_pct == pytest.approx(2.5)


def test_unavailable_beta_suppresses_risk_metrics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = deepcopy(_BETA)
    payload["calculation_status"] = "unavailable"
    payload["calculation_reason_codes"] = ["insufficient_return_observations"]

    def _get(url: str, timeout: float | None = None, params: object = None) -> _FakeResp:
        if "/api/portfolio/beta" in url:
            return _FakeResp(payload)
        return _route(url)

    monkeypatch.setattr(ptc.requests, "get", _get)
    analytics = fetch_portfolio_analytics(api_url="http://tracker.test")
    html = render_portfolio_analytics_sections(analytics)

    assert analytics.beta is not None
    assert analytics.beta.beta is None
    assert analytics.beta.alpha_annualized_pct is None
    assert "insufficient return observations" in html
    assert "Beta vs SPY" not in html


def test_unavailable_drawdown_suppresses_derived_metrics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = cast("dict[str, object]", deepcopy(_DRAWDOWN))
    payload["calculation_status"] = "unavailable"
    payload["calculation_reason_codes"] = ["external_share_movement_price_unavailable"]

    def _get(url: str, timeout: float | None = None, params: object = None) -> _FakeResp:
        if "/api/portfolio/drawdown" in url:
            return _FakeResp(payload)
        return _route(url)

    monkeypatch.setattr(ptc.requests, "get", _get)
    drawdown = ptc.fetch_drawdown(api_url="http://tracker.test")

    assert drawdown is not None
    assert drawdown.calculation_status == "unavailable"
    assert drawdown.max_drawdown_pct is None
    assert drawdown.annualized_return_pct is None
    assert drawdown.underwater == []


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


def test_risk_bundle_mismatched_bounds_scrubs_beta_and_drawdown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mismatched_drawdown = deepcopy(_DRAWDOWN)
    mismatched_drawdown["start_date"] = "2025-06-11"
    underwater = cast("list[dict[str, object]]", mismatched_drawdown["underwater"])
    underwater[0]["date"] = "2025-06-11"

    def _get(url: str, timeout: float | None = None, params: object = None) -> _FakeResp:
        if "/api/portfolio/drawdown" in url:
            return _FakeResp(mismatched_drawdown)
        return _route(url)

    monkeypatch.setattr(ptc.requests, "get", _get)
    analytics = fetch_portfolio_analytics(api_url="http://tracker.test", only={"beta", "drawdown"})

    assert analytics.beta is not None
    assert analytics.drawdown is not None
    assert analytics.beta.calculation_status == "unavailable"
    assert analytics.beta.compatibility_issue == "returned_window_mismatch"
    assert analytics.beta.beta is None
    assert analytics.drawdown.calculation_status == "unavailable"
    assert analytics.drawdown.compatibility_issue == "returned_window_mismatch"
    assert analytics.drawdown.underwater == []


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


def test_compact_performance_surface_skips_position_fetch_when_drivers_hidden(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requested: set[str] = set()

    def _probe(_api_url: str | None) -> tuple[bool, str]:
        return True, "http://tracker.test"

    monkeypatch.setattr(portfolio_panel_module, "probe_tracker", _probe)

    def _fetch(**kwargs: object) -> PortfolioAnalytics:
        requested.update(cast("set[str]", kwargs["only"]))
        return PortfolioAnalytics(available=True, api_url="http://tracker.test")

    monkeypatch.setattr(portfolio_panel_module, "fetch_portfolio_analytics", _fetch)

    render_portfolio_panel(include_position_drivers=False)

    assert requested == {"performance", "policy"}


def test_render_analytics_sections_populated(mock_tracker: None) -> None:
    a = fetch_portfolio_analytics(api_url="http://tracker.test")
    html = render_portfolio_analytics_sections(a)
    # Performance uses one cashflow basis for both percentages and dollars.
    assert "Performance vs benchmarks" in html
    assert "Whole-portfolio cash-flow-matched return" in html
    assert "+18.2%" in html
    assert "+11.5%" in html
    assert "+14.1%" in html
    assert "+13.0%" in html
    # Position price/trade results remain available only inside the secondary
    # Position drivers disclosure and never replace the whole-account chart.
    assert "+9.0%" in html
    assert "+4.3%" in html
    assert "+4.7pp" in html
    assert "Invested-position price/trade return" in html
    assert "Matched SPY price/trade return" in html
    assert "Price/trade alpha vs SPY" in html
    assert "$2,700" in html
    assert "$1,300" in html
    assert html.index("Whole-portfolio cash-flow-matched return") < html.index("Position drivers")
    assert "same dated external cash flows" in html
    assert "not a total portfolio return" in html
    assert "excludes cash equivalents" in html
    assert "cash dividends or interest paid to cash" in html
    assert "account fees" in html
    assert "in-kind transfers are not normalized" in html
    assert 'class="pf-chart"' in html
    assert "Policy mix:" in html and "VOO 70%" in html
    assert "+3.3%" not in html  # secondary position series cannot replace the chart
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
    assert "<summary>Position drivers (2)</summary>" in html
    assert html.index("research/NU/") < html.index("research/AAPL/")
    assert "pf-total" in html
    # The has_policy=True column renders, now as a sortable living-grid header.
    assert "vs policy" in html and "sortBy('policy','num')" in html
    assert 'class="pf-flag"' not in html
    assert "<!doctype" not in html.lower()


def test_incomplete_matched_returns_preserve_legacy_performance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    incomplete = deepcopy(_POSITION_ALPHA)
    matched = cast("dict[str, object]", incomplete["matched_returns"])
    matched["spy_return_pct"] = None

    def _get(url: str, timeout: float | None = None, params: object = None) -> _FakeResp:
        if "/api/portfolio/position-alpha" in url:
            return _FakeResp(incomplete)
        return _route(url)

    monkeypatch.setattr(ptc.requests, "get", _get)
    analytics = fetch_portfolio_analytics(api_url="http://tracker.test")
    html = render_portfolio_analytics_sections(analytics)

    assert analytics.position_alpha is not None
    assert analytics.position_alpha.matched_returns is None
    assert "Modified Dietz" in html
    assert "+18.2%" in html
    assert 'class="kpi-label">Invested-position price/trade return' not in html
    assert "Position drivers" in html
    assert "$2,700" not in html
    assert "$1,400" not in html
    assert "provider response did not satisfy the supported calculation contract" in html


@pytest.mark.parametrize("status", [None, "ready"])
def test_missing_or_unknown_position_status_fails_closed(
    monkeypatch: pytest.MonkeyPatch, status: str | None
) -> None:
    payload = cast("dict[str, object]", deepcopy(_POSITION_ALPHA))
    if status is None:
        payload.pop("calculation_status")
    else:
        payload["calculation_status"] = status

    def _get(url: str, timeout: float | None = None, params: object = None) -> _FakeResp:
        if "/api/portfolio/position-alpha" in url:
            return _FakeResp(payload)
        return _route(url)

    monkeypatch.setattr(ptc.requests, "get", _get)
    analytics = fetch_portfolio_analytics(api_url="http://tracker.test")
    html = render_portfolio_analytics_sections(analytics)

    assert analytics.position_alpha is not None
    assert analytics.position_alpha.calculation_status == "unavailable"
    assert analytics.position_alpha.rows == []
    assert analytics.position_alpha.total_actual_pl is None
    assert "+18.2%" in html
    assert "Position drivers" in html
    assert "$2,700" not in html


@pytest.mark.parametrize(
    ("reason_code", "expected_label"),
    [
        ("share_movement_unmatched", "share movement could not be matched"),
        ("no_invested_position_capital", "no invested position capital"),
    ],
)
def test_position_unavailable_reason_does_not_affect_authoritative_performance(
    monkeypatch: pytest.MonkeyPatch,
    reason_code: str,
    expected_label: str,
) -> None:
    payload = cast("dict[str, object]", deepcopy(_POSITION_ALPHA))
    payload["calculation_status"] = "unavailable"
    payload["calculation_reason_codes"] = [reason_code]

    def _get(url: str, timeout: float | None = None, params: object = None) -> _FakeResp:
        if "/api/portfolio/position-alpha" in url:
            return _FakeResp(payload)
        return _route(url)

    monkeypatch.setattr(ptc.requests, "get", _get)
    analytics = fetch_portfolio_analytics(api_url="http://tracker.test")
    html = render_portfolio_analytics_sections(analytics)

    assert "+18.2%" in html
    assert expected_label in html
    assert "Position drivers" in html
    assert "$2,700" not in html


def test_position_no_invested_capital_reason_is_a_supported_unavailable_state() -> None:
    payload = cast("dict[str, object]", deepcopy(_POSITION_ALPHA))
    payload["calculation_status"] = "unavailable"
    payload["calculation_reason_codes"] = ["no_invested_position_capital"]

    parsed = ptc._parse_position_alpha(payload)  # pyright: ignore[reportPrivateUsage]

    assert parsed.calculation_status == "unavailable"
    assert parsed.calculation_reason_codes == ["no_invested_position_capital"]
    assert parsed.compatibility_issue is None
    assert parsed.rows == []
    assert parsed.matched_returns is None


def test_position_nonpositive_dietz_reason_is_a_supported_unavailable_state() -> None:
    payload = cast("dict[str, object]", deepcopy(_POSITION_ALPHA))
    payload["calculation_status"] = "unavailable"
    payload["calculation_reason_codes"] = ["nonpositive_dietz_denominator"]

    parsed = ptc._parse_position_alpha(payload)  # pyright: ignore[reportPrivateUsage]

    assert parsed.calculation_status == "unavailable"
    assert parsed.calculation_reason_codes == ["nonpositive_dietz_denominator"]
    assert parsed.compatibility_issue is None
    assert parsed.rows == []
    assert parsed.matched_returns is None


def test_nonpositive_dietz_reason_is_a_supported_unavailable_state() -> None:
    payload = deepcopy(_PERFORMANCE)
    payload["calculation_status"] = "unavailable"
    payload["calculation_reason_codes"] = ["nonpositive_dietz_denominator"]

    parsed = ptc._parse_performance(payload)  # pyright: ignore[reportPrivateUsage]
    html = render_portfolio_analytics_sections(
        PortfolioAnalytics(available=True, api_url="http://tracker.test", performance=parsed)
    )

    assert parsed.calculation_status == "unavailable"
    assert parsed.calculation_reason_codes == ["nonpositive_dietz_denominator"]
    assert parsed.compatibility_issue is None
    assert parsed.points == []
    assert "nonpositive Modified Dietz denominator" in html


def test_available_position_data_never_substitutes_for_unavailable_performance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    performance = deepcopy(_PERFORMANCE)
    performance["calculation_status"] = "unavailable"
    performance["calculation_reason_codes"] = ["external_share_movement_price_unavailable"]
    position = cast("dict[str, object]", deepcopy(_POSITION_ALPHA))

    def _get(url: str, timeout: float | None = None, params: object = None) -> _FakeResp:
        if "/api/portfolio/performance" in url:
            return _FakeResp(performance)
        if "/api/portfolio/position-alpha" in url:
            return _FakeResp(position)
        return _route(url)

    monkeypatch.setattr(ptc.requests, "get", _get)
    analytics = fetch_portfolio_analytics(api_url="http://tracker.test")
    html = render_portfolio_analytics_sections(analytics)

    assert analytics.performance is not None
    assert analytics.performance.calculation_status == "unavailable"
    assert analytics.performance.points == []
    assert "+18.2%" not in html
    assert 'class="pf-chart"' not in html
    assert "Whole-portfolio cash-flow-matched return" not in html
    assert "Invested-position price/trade return" in html
    assert "+9.0%" in html
    assert "Whole-account Modified Dietz return unavailable" in html
    assert "external share-movement price unavailable" in html
    assert "Position drivers" in html


def test_missing_performance_status_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    performance = deepcopy(_PERFORMANCE)
    performance.pop("calculation_status")
    position = cast("dict[str, object]", deepcopy(_POSITION_ALPHA))
    position["calculation_status"] = "unavailable"
    position["calculation_reason_codes"] = ["share_movement_unmatched"]

    def _get(url: str, timeout: float | None = None, params: object = None) -> _FakeResp:
        if "/api/portfolio/performance" in url:
            return _FakeResp(performance)
        if "/api/portfolio/position-alpha" in url:
            return _FakeResp(position)
        return _route(url)

    monkeypatch.setattr(ptc.requests, "get", _get)
    analytics = fetch_portfolio_analytics(api_url="http://tracker.test")
    html = render_portfolio_analytics_sections(analytics)

    assert analytics.performance is not None
    assert analytics.performance.compatibility_issue == "missing_calculation_status"
    assert analytics.performance.points == []
    assert "+18.2%" not in html
    assert "provider response did not satisfy the supported calculation contract" in html


def test_position_unsupported_methodology_fails_closed() -> None:
    from integrations.portfolio_tracker_v1 import V1Meta

    fixture = json.loads(
        (
            Path(__file__).parent / "fixtures" / "tracker_v1" / "position-performance.json"
        ).read_text()
    )
    meta = V1Meta.model_validate(fixture["meta"]).model_copy(update={"methodology_version": "2"})

    parsed = ptc._parse_position_alpha(  # pyright: ignore[reportPrivateUsage]
        cast("dict[str, object]", deepcopy(_POSITION_ALPHA)), meta=meta
    )

    assert parsed.calculation_status == "unavailable"
    assert parsed.compatibility_issue == "unsupported_methodology"
    assert parsed.rows == []
    assert parsed.matched_returns is None


@pytest.mark.parametrize(
    ("payload", "parser"),
    [
        (_PERFORMANCE, ptc._parse_performance),  # pyright: ignore[reportPrivateUsage]
        (_POSITION_ALPHA, ptc._parse_position_alpha),  # pyright: ignore[reportPrivateUsage]
        (_BETA, ptc.parse_beta),
        (_DRAWDOWN, ptc._parse_drawdown),  # pyright: ignore[reportPrivateUsage]
    ],
)
def test_legacy_analytics_missing_embedded_methodology_fails_closed(
    payload: dict[str, object], parser: Callable[[dict[str, object]], object]
) -> None:
    missing = deepcopy(payload)
    missing.pop("methodology")

    parsed = parser(missing)

    assert getattr(parsed, "calculation_status") == "unavailable"
    assert getattr(parsed, "compatibility_issue") == "unsupported_methodology"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("base_value", None),
        ("base_value", 0),
        ("net_external_cashflow_in", None),
        ("backfill_start_unreliable", "false"),
    ],
)
def test_available_performance_core_contradictions_fail_closed(field: str, value: object) -> None:
    payload = deepcopy(_PERFORMANCE)
    payload[field] = value

    parsed = ptc._parse_performance(payload)  # pyright: ignore[reportPrivateUsage]

    assert parsed.calculation_status == "unavailable"
    assert parsed.compatibility_issue == "contradictory_available_payload"
    assert parsed.points == []


@pytest.mark.parametrize("mutation", ["nan", "duplicate_date", "wrong_endpoint"])
def test_available_performance_series_contradictions_fail_closed(mutation: str) -> None:
    payload = deepcopy(_PERFORMANCE)
    points = cast("list[dict[str, object]]", payload["points"])
    if mutation == "nan":
        points[-1]["portfolio_return_pct"] = "NaN"
    elif mutation == "duplicate_date":
        points[-1]["date"] = points[-2]["date"]
    else:
        points[-1]["date"] = "2026-06-11"

    parsed = ptc._parse_performance(payload)  # pyright: ignore[reportPrivateUsage]

    assert parsed.calculation_status == "unavailable"
    assert parsed.compatibility_issue == "contradictory_available_payload"
    assert parsed.points == []


@pytest.mark.parametrize("mutation", ["missing", "tampered_alpha"])
def test_available_performance_requires_a_reconciling_equation_receipt(mutation: str) -> None:
    payload = deepcopy(_PERFORMANCE)
    if mutation == "missing":
        payload["equation_receipt"] = None
    else:
        receipt = cast("dict[str, object]", payload["equation_receipt"])
        spy = cast("dict[str, object]", receipt["spy"])
        spy["dollar_alpha"] = "6700.01"

    parsed = ptc._parse_performance(payload)  # pyright: ignore[reportPrivateUsage]

    assert parsed.calculation_status == "unavailable"
    assert parsed.compatibility_issue == "contradictory_available_payload"
    assert parsed.equation_receipt is None


@pytest.mark.parametrize(
    "mutation",
    [
        "missing_policy",
        "unknown_policy",
        "missing_inputs",
        "empty_inputs",
        "malformed_input",
        "wrong_ticker",
        "invalid_date",
        "duplicate_target",
        "missing_target",
        "extra_target",
        "nonpositive_close",
        "nonfinite_close",
        "wrong_resolution",
        "source_after_target",
        "opaque_id_missing",
        "final_point_mismatch",
        "policy_receipt_mismatch",
        "duplicate_flow_date",
        "out_of_window_flow",
    ],
)
def test_available_performance_requires_complete_market_session_lineage(mutation: str) -> None:
    payload = deepcopy(_PERFORMANCE)
    receipt = cast("dict[str, object]", payload["equation_receipt"])
    spy = cast("dict[str, object]", receipt["spy"])
    inputs = cast("list[object]", spy["price_inputs"])
    first_input = cast("dict[str, object]", inputs[0])
    if mutation == "missing_policy":
        receipt.pop("benchmark_price_resolution_policy")
    elif mutation == "unknown_policy":
        receipt["benchmark_price_resolution_policy"] = "nearest_close"
    elif mutation == "missing_inputs":
        spy.pop("price_inputs")
    elif mutation == "empty_inputs":
        spy["price_inputs"] = []
    elif mutation == "malformed_input":
        inputs.append("not-an-input")
    elif mutation == "wrong_ticker":
        first_input["ticker"] = "QQQ"
    elif mutation == "invalid_date":
        first_input["target_date"] = "not-a-date"
    elif mutation == "duplicate_target":
        inputs.append(deepcopy(inputs[0]))
    elif mutation == "missing_target":
        inputs.pop()
    elif mutation == "extra_target":
        extra = deepcopy(first_input)
        extra["target_date"] = "2026-01-15"
        extra["source_date"] = "2026-01-15"
        inputs.append(extra)
    elif mutation == "nonpositive_close":
        first_input["close"] = "0"
    elif mutation == "nonfinite_close":
        first_input["close"] = "NaN"
    elif mutation == "wrong_resolution":
        first_input["resolution"] = "previous_market_close"
    elif mutation == "source_after_target":
        first_input["source_date"] = "2025-06-11"
    elif mutation == "opaque_id_missing":
        spy["price_input_id"] = ""
    elif mutation == "final_point_mismatch":
        spy["ending_value"] = "136501.00"
    elif mutation == "policy_receipt_mismatch":
        receipt["policy"] = None
    else:
        flows = cast("list[object]", receipt["dated_external_cashflows"])
        extra_flow = {"date": "2026-06-10", "amount": "0"}
        if mutation == "out_of_window_flow":
            extra_flow["date"] = "2025-06-10"
        flows.append(extra_flow)

    parsed = ptc._parse_performance(payload)  # pyright: ignore[reportPrivateUsage]

    assert parsed.calculation_status == "unavailable"
    assert parsed.compatibility_issue == "contradictory_available_payload"
    assert parsed.equation_receipt is None


def test_available_performance_rejects_unverified_source_cashflows() -> None:
    payload = deepcopy(_PERFORMANCE)
    source_coverage = cast("dict[str, object]", payload["source_coverage"])
    source_coverage["status"] = "incomplete"
    source_coverage["is_complete"] = False

    parsed = ptc._parse_performance(payload)  # pyright: ignore[reportPrivateUsage]

    assert parsed.calculation_status == "unavailable"
    assert parsed.compatibility_issue == "source_cashflow_coverage_incomplete"


def test_available_position_series_rejects_nonpositive_intermediate_dietz_denominator() -> None:
    payload = cast("dict[str, object]", deepcopy(_POSITION_ALPHA))
    series = cast("list[dict[str, object]]", payload["series"])
    first = deepcopy(series[0])
    first["date"] = "2025-06-11"
    first["position_cashflow"] = "-60000.00"
    second = deepcopy(series[0])
    second["date"] = "2025-06-12"
    second["position_cashflow"] = "120000.00"
    series[1:1] = [first, second]

    parsed = ptc._parse_position_alpha(payload)  # pyright: ignore[reportPrivateUsage]

    assert parsed.calculation_status == "unavailable"
    assert parsed.compatibility_issue == "contradictory_available_payload"
    assert parsed.rows == []
    assert parsed.matched_returns is None


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("total_alpha",), "1400.02"),
        (("matched_returns", "alpha_vs_spy_pct"), "4.6668"),
        (("rows", 0, "alpha"), "2200.02"),
        (("rows", 0, "value_at_start"), None),
        (("rows", 0, "incomplete"), True),
        (("series", 1, "portfolio_return_pct"), "999"),
        (("total_actual_pl",), "NaN"),
    ],
)
def test_position_arithmetic_or_nonfinite_contradiction_fails_closed(
    path: tuple[str | int, ...], value: object
) -> None:
    payload: object = deepcopy(_POSITION_ALPHA)
    cursor = payload
    for key in path[:-1]:
        cursor = (
            cast("dict[str, object]", cursor)[key]
            if isinstance(key, str)
            else cast("list[object]", cursor)[key]
        )
    last = path[-1]
    if isinstance(last, str):
        cast("dict[str, object]", cursor)[last] = value
    else:
        cast("list[object]", cursor)[last] = value

    parsed = ptc._parse_position_alpha(  # pyright: ignore[reportPrivateUsage]
        cast("dict[str, object]", payload)
    )

    assert parsed.calculation_status == "unavailable"
    assert parsed.compatibility_issue == "contradictory_available_payload"
    assert parsed.matched_returns is None
    assert parsed.rows == []


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("sample_size", 0),
        ("risk_free_annual", None),
        ("beta", "NaN"),
        ("r_squared", 1.01),
        ("correlation", -1.01),
        ("alpha_significant", "false"),
    ],
)
def test_available_beta_core_contradictions_fail_closed(field: str, value: object) -> None:
    payload = cast("dict[str, object]", deepcopy(_BETA))
    payload[field] = value

    parsed = ptc.parse_beta(payload)

    assert parsed.calculation_status == "unavailable"
    assert parsed.compatibility_issue == "contradictory_available_payload"
    assert parsed.beta is None


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("max_drawdown_pct", "NaN"),
        ("annualized_return_pct", None),
    ],
)
def test_available_drawdown_core_contradictions_fail_closed(field: str, value: object) -> None:
    payload = cast("dict[str, object]", deepcopy(_DRAWDOWN))
    payload[field] = value

    parsed = ptc._parse_drawdown(payload)  # pyright: ignore[reportPrivateUsage]

    assert parsed.calculation_status == "unavailable"
    assert parsed.compatibility_issue == "contradictory_available_payload"
    assert parsed.max_drawdown_pct is None
    assert parsed.underwater == []


def test_available_drawdown_invalid_underwater_point_fails_closed() -> None:
    payload = cast("dict[str, object]", deepcopy(_DRAWDOWN))
    points = cast("list[dict[str, object]]", payload["underwater"])
    points[1]["drawdown_pct"] = None

    parsed = ptc._parse_drawdown(payload)  # pyright: ignore[reportPrivateUsage]

    assert parsed.calculation_status == "unavailable"
    assert parsed.compatibility_issue == "contradictory_available_payload"


def test_available_drawdown_curve_must_reconcile_to_summary() -> None:
    payload = cast("dict[str, object]", deepcopy(_DRAWDOWN))
    points = cast("list[dict[str, object]]", payload["underwater"])
    points[1]["drawdown_pct"] = "-99"

    parsed = ptc._parse_drawdown(payload)  # pyright: ignore[reportPrivateUsage]

    assert parsed.calculation_status == "unavailable"
    assert parsed.compatibility_issue == "contradictory_available_payload"


def test_drawdown_insufficient_observations_is_supported_unavailability() -> None:
    payload = cast("dict[str, object]", deepcopy(_DRAWDOWN))
    payload["calculation_status"] = "unavailable"
    payload["calculation_reason_codes"] = ["insufficient_return_observations"]

    parsed = ptc._parse_drawdown(payload)  # pyright: ignore[reportPrivateUsage]

    assert parsed.calculation_status == "unavailable"
    assert parsed.calculation_reason_codes == ["insufficient_return_observations"]
    assert parsed.compatibility_issue is None


def test_mismatched_matched_window_preserves_performance_warning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mismatched = deepcopy(_POSITION_ALPHA)
    mismatched["start_date"] = "2025-05-01"
    modeled_perf = deepcopy(_PERFORMANCE)
    modeled_perf["backfill_start_unreliable"] = True

    def _get(url: str, timeout: float | None = None, params: object = None) -> _FakeResp:
        if "/api/portfolio/performance" in url:
            return _FakeResp(modeled_perf)
        if "/api/portfolio/position-alpha" in url:
            return _FakeResp(mismatched)
        return _route(url)

    monkeypatch.setattr(ptc.requests, "get", _get)
    analytics = fetch_portfolio_analytics(api_url="http://tracker.test")
    html = render_portfolio_analytics_sections(analytics)

    assert "+18.2%" in html
    assert "+9.0%" not in html
    assert 'class="kpi-label">Invested-position price/trade return' not in html
    assert "This window is modeled, not measured" in html
    assert "2025-06-12" in html


def test_unpriced_position_benchmarks_do_not_suppress_whole_account_lines(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    unavailable = cast("dict[str, object]", deepcopy(_POSITION_ALPHA))
    matched = cast("dict[str, object]", unavailable["matched_returns"])
    matched["qqq_return_pct"] = None
    matched["alpha_vs_qqq_pct"] = None
    matched["policy_return_pct"] = None
    matched["alpha_vs_policy_pct"] = None
    unavailable["total_qqq_pl"] = None
    unavailable["total_alpha_vs_qqq"] = None
    unavailable["total_policy_pl"] = None
    unavailable["total_alpha_vs_policy"] = None
    for row in cast("list[dict[str, object]]", unavailable["rows"]):
        row["qqq_counterfactual_pl"] = None
        row["alpha_vs_qqq"] = None
        row["policy_counterfactual_pl"] = None
        row["alpha_vs_policy"] = None
    series = cast("list[dict[str, object]]", unavailable["series"])
    for point in series:
        point["qqq_counterfactual_value"] = "0.00"
        point["qqq_return_pct"] = None
        point["policy_counterfactual_value"] = None
        point["policy_return_pct"] = None

    def _get(url: str, timeout: float | None = None, params: object = None) -> _FakeResp:
        if "/api/portfolio/position-alpha" in url:
            return _FakeResp(unavailable)
        return _route(url)

    monkeypatch.setattr(ptc.requests, "get", _get)
    analytics = fetch_portfolio_analytics(api_url="http://tracker.test")
    html = render_portfolio_analytics_sections(analytics)

    assert "Matched SPY price/trade return" in html
    assert "Matched QQQ price/trade return" not in html
    assert "Price/trade alpha vs QQQ" not in html
    assert "pf-swatch-qqq" in html
    assert "pf-swatch-policy" in html
    assert "$0 P&amp;L" not in html
    assert "sortBy('qqq','num')" not in html
    assert "sortBy('policy','num')" not in html


def test_performance_renders_public_analytics_freshness_and_coverage(
    mock_tracker: None,
) -> None:
    analytics = fetch_portfolio_analytics(api_url="http://tracker.test")
    assert analytics.performance is not None
    assert analytics.position_alpha is not None
    analytics.performance.provenance = ptc.AnalyticsProvenance(
        as_of="2026-06-10",
        is_stale=False,
        is_partial=False,
        warning_codes=(),
        source_providers=("plaid", "snaptrade"),
        included_account_count=2,
        excluded_account_count=1,
        lagging_account_count=0,
        methodology="performance.modified_dietz",
        methodology_version="2",
    )
    analytics.position_alpha.provenance = ptc.AnalyticsProvenance(
        as_of="2026-06-09",
        is_stale=True,
        is_partial=True,
        warning_codes=(
            "position_price_coverage_partial",
            "unsafe warning with account detail",
        ),
        source_providers=("plaid", "snaptrade"),
        included_account_count=3,
        excluded_account_count=1,
        lagging_account_count=1,
        methodology="position_alpha.split_normalized_price_trade_modified_dietz",
        methodology_version="3",
    )

    html = render_portfolio_analytics_sections(analytics)

    assert "Analytics as of 2026-06-10" in html
    assert "performance.modified_dietz v2" in html
    assert "Analytics as of 2026-06-09" in html
    assert "stale" in html
    assert "partial coverage" in html
    assert "position_price_coverage_partial" in html
    assert "unsafe warning with account detail" not in html
    assert "providers: plaid, snaptrade" in html
    assert "accounts: 3 included, 1 excluded, 1 lagging" in html
    assert "position_alpha.split_normalized_price_trade_modified_dietz v3" in html
    assert html.index("performance.modified_dietz v2") < html.index("Position drivers")
    assert html.index("Position drivers") < html.index(
        "position_alpha.split_normalized_price_trade_modified_dietz v3"
    )


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
    assert "Windows Task Scheduler" in html
    assert "uvicorn portfolio_tracker.api.main:app" not in html
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
    assert a.performance is not None
    assert a.performance.compatibility_issue == "returned_window_mismatch"
    assert a.position_alpha is not None
    assert a.position_alpha.compatibility_issue == "returned_window_mismatch"
    assert a.beta is not None
    assert a.beta.compatibility_issue == "returned_window_mismatch"


def test_fetch_analytics_default_aligns_only_position_alpha_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[str] = []

    def _get(url: str, timeout: float | None = None, params: object = None) -> _FakeResp:
        seen.append(url)
        return _route(url)

    monkeypatch.setattr(ptc.requests, "get", _get)
    fetch_portfolio_analytics(api_url="http://tracker.test")
    # Performance establishes the effective default window. Position alpha is
    # then requested against those exact bounds; unrelated endpoints keep their
    # own default requests.
    alpha_url = next(url for url in seen if "/position-alpha" in url)
    assert "start_date=2025-06-10" in alpha_url
    assert "end_date=2026-06-10" in alpha_url
    assert all("?" not in url for url in seen if "/position-alpha" not in url)


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
    assert 'data-refresh-endpoint="/api/panel/portfolio"' in html
    # The book attribution narrative is no longer mounted on the page.
    assert "Attribution narratives" not in html


def test_compose_page_can_refresh_an_owning_composite(mock_tracker: None) -> None:
    analytics = fetch_portfolio_analytics(api_url="http://tracker.test")
    live = LivePortfolio(available=True, api_url="http://x", total_market_value=0.0)

    html = compose_portfolio_page(
        analytics,
        live,
        refresh_endpoint="/api/panel/performance_risk",
        refresh_target_selector="#workOsPerformanceMount",
    )

    assert 'data-refresh-endpoint="/api/panel/performance_risk"' in html
    assert 'data-refresh-target="#workOsPerformanceMount"' in html
    assert "bar.getAttribute('data-refresh-endpoint')" in html
    assert "bar.getAttribute('data-refresh-target')" in html


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
    assert 'data-refresh-endpoint="/api/panel/portfolio"' in html
    assert "banner.closest('.console-sec')" in html
    assert 'id="pf-window-bar"' not in html  # no chart, so no window controls
    assert html.count("<section") == 1  # just the banner — nothing buried below it


def test_compose_page_offline_recovery_refreshes_the_owning_composite() -> None:
    analytics = PortfolioAnalytics(available=False, api_url="http://localhost:8000")
    live = LivePortfolio(available=False, api_url="http://localhost:8000", error="offline")

    html = compose_portfolio_page(
        analytics,
        live,
        refresh_endpoint="/api/panel/performance_risk",
        refresh_target_selector="#workOsPerformanceMount",
    )

    assert 'data-refresh-endpoint="/api/panel/performance_risk"' in html
    assert 'data-refresh-target="#workOsPerformanceMount"' in html
    assert "banner.getAttribute('data-refresh-target')" in html


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
    assert "method: risk.beta_drawdown v2" in html
    assert "provider/account coverage/stale-partial status not supplied" in html
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


# --- modeled-window warning -------------------------------------------------
# The tracker's `backfill_start_unreliable` used to test only whether the
# reconstructed start value had COLLAPSED (< 25% of the end), so it measured
# False on every real window including a 2024-01-01 lookback that reported
# +85.5% against SPY's +57.5% purely from walk-back artifacts. It now also
# fires whenever the window starts before `earliest_observed_date`. These pin
# the ES-side rendering of that signal.


def _perf_series(*, unreliable: bool, observed: str | None) -> PerformanceSeries:
    return PerformanceSeries(
        start_date="2024-01-01",
        end_date="2026-07-30",
        base_value=315781.0,
        net_external_cashflow_in=52179.0,
        backfill_start_unreliable=unreliable,
        earliest_observed_date=observed,
    )


def test_backfill_warning_absent_when_window_is_observed() -> None:
    assert backfill_warning(_perf_series(unreliable=False, observed="2026-05-09")) == ""


def test_backfill_warning_names_the_observed_boundary_and_the_bias() -> None:
    html = backfill_warning(_perf_series(unreliable=True, observed="2026-05-09"))
    assert "modeled, not measured" in html
    # The boundary has to be in the copy — "some of this is modeled" without
    # saying how far back is not actionable.
    assert "2026-05-09" in html
    # And the bias has a direction; the old both-ways hedge understated it.
    assert "upward" in html


def test_backfill_warning_handles_a_fully_modeled_window() -> None:
    html = backfill_warning(_perf_series(unreliable=True, observed=None))
    assert "No part of this window" in html


def test_backfill_warning_does_not_claim_survivorship() -> None:
    # An earlier version of this copy said the walk-back "can only see
    # positions you still hold". That is false: it replays transactions
    # backward and DOES restore positions closed inside the covered span —
    # 33 reconstructed at 2024-01-01 against 12 held today on the live book.
    # The real limit is per-account transaction COVERAGE, and the dominant
    # error is contributions made before a feed started, not vanished losers.
    html = backfill_warning(_perf_series(unreliable=True, observed="2026-05-09"))
    assert "only see positions you still hold" not in html
    assert "exited losers" not in html
    assert "transaction history reaches" in html
    assert "deposits into that account are invisible" in html
