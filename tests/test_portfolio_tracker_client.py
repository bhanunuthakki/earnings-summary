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

import pytest
import requests

from integrations import portfolio_tracker_client as ptc
from integrations.portfolio_tracker_client import (
    LivePortfolio,
    LivePosition,
    PortfolioAnalytics,
    TaxLot,
    fetch_live_portfolio,
    fetch_portfolio_analytics,
    tax_treatment,
)
from pipeline.portfolio_panel import (
    WindowSelection,
    compose_portfolio_page,
    render_live_portfolio_section,
    render_portfolio_analytics_sections,
    validated_window,
)

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
    assert "isn't running" in html  # humane lede, not a raw requests repr
    assert "uvicorn portfolio_tracker.api.main:app" in html  # the start hint
    # The raw error survives, but only inside the collapsed technical details.
    assert "offline-tech" in html
    assert "ConnectionError: nope" in html
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
    assert "vs policy</th>" in html
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
    html = compose_portfolio_page(analytics, live, "<div>SYNTH</div>")
    # Tracker fully down → exactly ONE offline note (the live section's, which
    # carries the start hint) — not a second analytics offline panel.
    assert html.count("offline-tech") == 1
    assert "uvicorn portfolio_tracker.api.main:app" in html
    assert "Portfolio analytics" not in html
    assert "SYNTH" in html


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
    html = compose_portfolio_page(analytics, live, "")
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


def test_compose_page_renders_window_bar_with_echoed_values() -> None:
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
    window = WindowSelection(start_date="2026-01-01", end_date="2026-06-10", include_backfill=True)
    html = compose_portfolio_page(analytics, live, "", window=window)
    # The bar renders even with the tracker down (it doubles as a retry
    # control), echoing the applied window into the inputs.
    assert 'id="pf-window-bar"' in html
    assert 'value="2026-01-01"' in html and 'value="2026-06-10"' in html
    assert 'id="pf-backfill" checked' in html
    assert 'data-preset="ytd"' in html and 'data-preset="default"' in html
    assert "/api/panel/portfolio" in html  # the refetch script targets this panel
    # Still exactly ONE offline note alongside the bar.
    assert html.count("offline-tech") == 1


def test_compose_page_default_window_bar_is_unset() -> None:
    analytics = PortfolioAnalytics(available=False, api_url="http://x", errors={})
    live = LivePortfolio(available=False, api_url="http://x", error="down")
    html = compose_portfolio_page(analytics, live, "")
    assert 'id="pf-window-bar"' in html
    assert 'id="pf-start" value=""' in html and 'id="pf-end" value=""' in html
    assert 'id="pf-backfill">' in html  # unchecked
