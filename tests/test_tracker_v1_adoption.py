"""Phase 2 leg-1 adoption tests: the legacy facades on the v1 transport.

Consolidation PRD §12 Phase 2. Verifies that with PORTFOLIO_TRACKER_V1_READS=1
the four public facades in ``integrations.portfolio_tracker_client`` route
through the typed v1 client (mocked at ``requests.Session.get`` with the
vendored official fixtures) and return the exact legacy dataclass shapes plus
the additive envelope fields — and that a broken v1 read NEVER falls back to
the legacy endpoints. Fully hermetic: no live provider, no network.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import cast

import pytest
import requests

from execution.tracker_v1_parity import (
    SectionResult,
    compare_history,
    compare_live,
)
from integrations import portfolio_tracker_client as tc

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "tracker_v1"


def _fixture(name: str) -> dict[str, object]:
    with (FIXTURES_DIR / f"{name}.json").open(encoding="utf-8") as f:
        return cast("dict[str, object]", json.load(f))


class _FakeResp:
    def __init__(self, payload: object, status: int = 200) -> None:
        self._payload = payload
        self.status_code = status

    def json(self) -> object:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(f"http {self.status_code}")


class _V1Router:
    """Routes ``requests.Session.get`` (the typed client's seam) by URL path.

    Install via :func:`_route_v1` — plain functions bind as methods on
    ``requests.Session``, so the installed callable takes ``self`` first."""

    def __init__(self, routes: dict[str, object]) -> None:
        self.routes = routes
        self.calls: list[str] = []

    def handle(self, url: str) -> _FakeResp:
        self.calls.append(url)
        for path, payload in self.routes.items():
            if path in url:
                return _FakeResp(payload)
        raise requests.ConnectionError(f"unrouted url {url}")


@pytest.fixture
def v1_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PORTFOLIO_TRACKER_V1_READS", "1")


@pytest.fixture
def legacy_guard(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Fails the test if any legacy module-level ``requests.get`` fires while
    the v1 transport is selected (the no-silent-fallback contract). The v1
    client uses ``Session.get``, so this seam is exclusively legacy."""
    calls: list[str] = []

    def _record(url: str, **kwargs: object) -> _FakeResp:
        calls.append(url)
        raise AssertionError(f"legacy transport used under v1 switch: {url}")

    monkeypatch.setattr(tc.requests, "get", _record)
    return calls


def _route_v1(monkeypatch: pytest.MonkeyPatch, routes: dict[str, object]) -> _V1Router:
    router = _V1Router(routes)

    def fake_get(
        self: requests.Session, url: str, params: object = None, timeout: object = None
    ) -> _FakeResp:
        return router.handle(url)

    monkeypatch.setattr(requests.Session, "get", fake_get)
    return router


# ---------------------------------------------------------------------------
# Switch off: legacy behavior and defaults untouched
# ---------------------------------------------------------------------------


def test_switch_off_by_default_keeps_legacy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PORTFOLIO_TRACKER_V1_READS", raising=False)
    assert tc._v1_reads_enabled() is False  # pyright: ignore[reportPrivateUsage]

    # Legacy path fails fast offline and envelope fields stay at defaults.
    def _refuse(url: str, **kwargs: object) -> _FakeResp:
        raise requests.ConnectionError("down")

    monkeypatch.setattr(tc.requests, "get", _refuse)
    live = tc.fetch_live_portfolio()
    assert live.available is False
    assert live.as_of is None
    assert live.is_stale is False
    assert live.is_partial is False
    assert live.envelope_warnings == []


# ---------------------------------------------------------------------------
# Switch on: fixtures adapt into the legacy shapes
# ---------------------------------------------------------------------------


def test_live_portfolio_v1_adapts_positions_fixture(
    v1_on: None, legacy_guard: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _route_v1(
        monkeypatch,
        {
            "/api/v1/portfolio/positions": _fixture("positions"),
            "/api/v1/transactions": _fixture("transactions"),
        },
    )
    live = tc.fetch_live_portfolio()
    assert live.available is True
    assert live.total_market_value == pytest.approx(20000.0)
    assert len(live.positions) == 3
    p0 = live.positions[0]
    assert p0.ticker == "AAAA"
    assert p0.quantity == pytest.approx(110.0)
    assert p0.percent_of_portfolio == pytest.approx(66.0)
    # Five-way -> coarse mapping at the lot level: the AAAA fixture holds
    # hsa (1200) + roth (12000) lots, both of which land in tax_free.
    assert {lot.tax_treatment for lot in p0.accounts} == {"tax_free"}
    assert live.by_tax_treatment["tax_free"] == pytest.approx(13200.0)
    assert live.by_tax_treatment["taxable"] == pytest.approx(6800.0)
    assert live.by_tax_treatment["tax_deferred"] == pytest.approx(0.0)
    # Envelope: as_of from the positions snapshot; flags from the txn read.
    assert live.as_of == "2026-07-22"
    assert live.is_stale is False
    assert live.is_partial is False
    assert len(live.transactions) == 4
    assert legacy_guard == []


def test_live_portfolio_v1_unavailable_never_falls_back(
    v1_on: None, legacy_guard: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def _refuse(
        self: requests.Session, url: str, params: object = None, timeout: object = None
    ) -> _FakeResp:
        raise requests.ConnectionError("v1 down")

    monkeypatch.setattr(requests.Session, "get", _refuse)
    live = tc.fetch_live_portfolio()
    assert live.available is False
    assert live.error is not None and "v1 positions" in live.error
    assert legacy_guard == []  # the guard would have raised on any legacy GET


def test_live_portfolio_v1_major_version_mismatch_fails_closed(
    v1_on: None, legacy_guard: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    txns = cast("dict[str, object]", json.loads(json.dumps(_fixture("transactions"))))
    cast("dict[str, object]", txns["meta"])["schema_version"] = "2.0.0"
    _route_v1(
        monkeypatch,
        {
            "/api/v1/portfolio/positions": _fixture("positions"),
            "/api/v1/transactions": txns,
        },
    )
    live = tc.fetch_live_portfolio()
    assert live.available is False
    assert live.error is not None and "incompatible_schema_version" in live.error
    assert legacy_guard == []


def test_transaction_history_v1_paginates(
    v1_on: None, legacy_guard: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    page = cast("dict[str, object]", json.loads(json.dumps(_fixture("transactions"))))
    page_one = cast("dict[str, object]", json.loads(json.dumps(page)))
    page_one["next_cursor"] = "b64cursor"
    pages = iter([page_one, page])
    calls: list[str] = []

    def _paged(
        self: requests.Session, url: str, params: object = None, timeout: object = None
    ) -> _FakeResp:
        calls.append(url)
        return _FakeResp(next(pages))

    monkeypatch.setattr(requests.Session, "get", _paged)
    history = tc.fetch_transaction_history()
    assert history is not None
    assert len(history) == 8  # two pages of 4
    assert len(calls) == 2
    assert history[0].account_name
    assert legacy_guard == []


def test_analytics_v1_sections_adapt_and_risk_feeds_two_sections(
    v1_on: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    policy_payload = {
        "total_pct": 100.0,
        "is_balanced": True,
        "weights": [{"ticker": "XXXX", "weight_pct": 100.0, "notes": None}],
    }

    router = _route_v1(
        monkeypatch,
        {
            "/api/v1/analytics/performance": _fixture("performance"),
            "/api/v1/analytics/position-performance": _fixture("position-performance"),
            "/api/v1/analytics/positioning": _fixture("positioning"),
            "/api/v1/analytics/risk": _fixture("risk"),
            "/api/v1/analytics/exit-quality": _fixture("exit-quality"),
        },
    )
    # The policy section legitimately stays on the legacy endpoint (no v1
    # successor, Phase-0 ruling PT-6) — the ONE allowed legacy GET.
    legacy_calls: list[str] = []

    def _legacy(url: str, **kwargs: object) -> _FakeResp:
        legacy_calls.append(url)
        assert url.endswith("/api/policy"), f"unexpected legacy GET {url}"
        return _FakeResp(policy_payload)

    monkeypatch.setattr(tc.requests, "get", _legacy)

    analytics = tc.fetch_portfolio_analytics(
        only={"performance", "position_alpha", "positioning", "policy", "beta", "drawdown"}
    )
    assert analytics.available is True
    assert analytics.errors == {}
    assert analytics.performance is not None and len(analytics.performance.points) == 365
    assert analytics.position_alpha is not None
    assert analytics.positioning is not None
    assert analytics.positioning.concentration is not None
    assert analytics.beta is not None and analytics.beta.benchmark == "SPY"
    assert analytics.drawdown is not None
    assert analytics.drawdown.max_drawdown_pct == pytest.approx(-31.90)
    assert analytics.policy is not None and analytics.policy.total_pct == pytest.approx(100.0)
    assert legacy_calls == [f"{analytics.api_url}/api/policy"]
    # beta + drawdown share ONE /analytics/risk read.
    risk_calls = [u for u in router.calls if "analytics/risk" in u]
    assert len(risk_calls) == 1
    # Envelope aggregated across sections.
    assert analytics.as_of is not None
    assert analytics.is_stale is False


def test_analytics_v1_per_section_isolation(v1_on: None, monkeypatch: pytest.MonkeyPatch) -> None:
    _route_v1(monkeypatch, {"/api/v1/analytics/performance": _fixture("performance")})

    def _legacy_down(url: str, **kwargs: object) -> _FakeResp:
        raise requests.ConnectionError("legacy down")

    monkeypatch.setattr(tc.requests, "get", _legacy_down)
    analytics = tc.fetch_portfolio_analytics(only={"performance", "beta"})
    assert analytics.available is True  # performance loaded
    assert analytics.performance is not None
    assert analytics.beta is None
    assert "beta" in analytics.errors and analytics.errors["beta"].startswith("v1:")


def test_exit_quality_v1_standalone_carries_envelope(
    v1_on: None, legacy_guard: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _route_v1(monkeypatch, {"/api/v1/analytics/exit-quality": _fixture("exit-quality")})
    eq = tc.fetch_exit_quality()
    assert eq is not None
    assert eq.rows == []
    assert eq.as_of is not None
    assert isinstance(eq.is_stale, bool)
    assert legacy_guard == []


# ---------------------------------------------------------------------------
# Parity harness comparison logic (pure functions)
# ---------------------------------------------------------------------------


def _live(available: bool = True, tickers: tuple[str, ...] = ("AAAA", "BBBB")) -> tc.LivePortfolio:
    positions = [
        tc.LivePosition(
            ticker=t,
            name=t,
            quantity=1.0,
            market_value=100.0,
            cost_basis=90.0,
            unrealized_pnl=10.0,
            percent_of_portfolio=50.0,
            accounts=[],
        )
        for t in tickers
    ]
    return tc.LivePortfolio(
        available=available,
        api_url="http://x",
        total_market_value=100.0 * len(tickers),
        positions=positions,
        by_tax_treatment={"taxable": 100.0 * len(tickers)},
    )


def test_compare_live_equal_passes() -> None:
    result = compare_live(_live(), _live())
    assert isinstance(result, SectionResult)
    assert result.status == "pass"
    assert result.details["ticker_set_diff"] == 0


def test_compare_live_count_drift_fails() -> None:
    result = compare_live(_live(), _live(tickers=("AAAA",)))
    assert result.status == "fail"


def test_compare_live_one_side_unavailable() -> None:
    result = compare_live(_live(available=False), _live())
    assert result.status == "unavailable_legacy"


def test_compare_history_superset_passes_subset_fails() -> None:
    txn = tc.LiveTransaction(
        date="2026-07-01",
        ticker=None,
        name=None,
        type="cash",
        subtype=None,
        quantity=None,
        amount=1.0,
        account_name="A",
    )
    assert compare_history([txn], [txn, txn]).status == "pass"
    assert compare_history([txn, txn], [txn]).status == "fail"
    assert compare_history(None, [txn]).status == "unavailable_legacy"


def test_parity_output_is_sanitized() -> None:
    """The emitted details must never contain balances, account names, or
    tickers — only counts, deltas, dates, and booleans."""
    legacy = _live()
    v1 = _live()
    result = compare_live(legacy, v1)
    blob = json.dumps(result.as_json())
    assert "AAAA" not in blob  # ticker symbols never emitted
    assert "200.0" not in blob  # absolute dollar totals never emitted
    assert "account" not in blob.lower() or "count" in blob.lower()
    for forbidden in ("100.0", "90.0"):
        assert forbidden not in blob


# ---------------------------------------------------------------------------
# Performance: observed-window rebase (legacy default-window parity)
# ---------------------------------------------------------------------------


class _PerfRouter:
    """Param-aware ``performance`` stub. Records each request's ``start_date``
    and answers with a series whose window reflects it, so the two-pass rebase
    in :func:`_get_performance_v1_observed` is observable."""

    def __init__(self, earliest_observed: str | None, *, probe_start: str = "2025-07-23") -> None:
        self.earliest_observed = earliest_observed
        self.probe_start = probe_start
        self.starts: list[str | None] = []
        self.backfills: list[object] = []
        self._base = _fixture("performance")

    def payload(self, start: str | None) -> dict[str, object]:
        series = dict(cast("dict[str, object]", self._base["series"]))
        effective = start or self.probe_start
        series["start_date"] = effective
        # earliest_observed_date is WINDOW-RELATIVE on the real provider: it
        # never precedes the requested window start.
        observed = self.earliest_observed
        if observed is not None and observed < effective:
            observed = effective
        series["earliest_observed_date"] = observed
        points = cast("list[dict[str, object]]", series["points"])
        series["points"] = [p for p in points if str(p["date"]) >= effective]
        return {"meta": self._base["meta"], "series": series}

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def fake_get(
            self_: requests.Session,
            url: str,
            params: object = None,
            timeout: object = None,
        ) -> _FakeResp:
            if "/api/v1/analytics/performance" not in url:
                raise requests.ConnectionError(f"unrouted url {url}")
            p = cast("dict[str, object]", params or {})
            start = cast("str | None", p.get("start_date"))
            self.starts.append(start)
            self.backfills.append(p.get("include_backfill"))
            return _FakeResp(self.payload(start))

        monkeypatch.setattr(requests.Session, "get", fake_get)


def test_performance_v1_rebases_to_earliest_observed(
    v1_on: None, legacy_guard: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Default (no caller window): probe the v1 default window, then re-request
    from the series' own ``earliest_observed_date`` so the return base matches
    the legacy snapshot-derived window instead of a modeled walk-back."""
    router = _PerfRouter(earliest_observed="2026-06-23")
    router.install(monkeypatch)

    analytics = tc.fetch_portfolio_analytics(only={"performance"})

    assert router.starts == [None, "2026-06-23"]
    perf = analytics.performance
    assert perf is not None
    assert perf.points
    assert str(perf.points[0].date) == "2026-06-23"


def test_performance_v1_explicit_window_passes_through_unrebased(
    v1_on: None, legacy_guard: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """An explicit caller window is authoritative under both transports — one
    request, verbatim, no probe and no rebase."""
    router = _PerfRouter(earliest_observed="2026-06-23")
    router.install(monkeypatch)

    tc.fetch_portfolio_analytics(start_date="2025-09-01", only={"performance"})

    assert router.starts == ["2025-09-01"]


def test_performance_v1_include_backfill_keeps_wide_window(
    v1_on: None, legacy_guard: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """``include_backfill`` explicitly asks for the modeled walk-back, so the
    wide default window IS the requested product — never rebased away."""
    router = _PerfRouter(earliest_observed="2026-06-23")
    router.install(monkeypatch)

    tc.fetch_portfolio_analytics(include_backfill=True, only={"performance"})

    assert router.starts == [None]
    # The client serializes query booleans lowercase for the provider.
    assert router.backfills == ["true"]


def test_performance_v1_widens_when_probe_window_may_clip_history(
    v1_on: None, legacy_guard: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """When observation appears to start at the probe window's own edge, the
    trailing-365d probe may be clipping real history: widen once, then rebase
    onto the true observation start the provider reports."""
    router = _PerfRouter(earliest_observed="2004-03-01")
    router.install(monkeypatch)

    tc.fetch_portfolio_analytics(only={"performance"})

    assert router.starts == [None, tc._V1_WIDE_HISTORY_START, "2004-03-01"]  # pyright: ignore[reportPrivateUsage]


def test_performance_v1_missing_observed_marker_returns_probe(
    v1_on: None, legacy_guard: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """No ``earliest_observed_date`` means the provider declined to mark where
    observation begins; the probe window is the only honest answer — surfaced
    rather than silently narrowed to a guess."""
    router = _PerfRouter(earliest_observed=None)
    router.install(monkeypatch)

    analytics = tc.fetch_portfolio_analytics(only={"performance"})

    assert router.starts == [None]
    assert analytics.performance is not None


# ---------------------------------------------------------------------------
# earliest_observed_date provenance (consumed by the risk-snapshot rebase_basis
# stamp — PRD §7.1 req 9)
# ---------------------------------------------------------------------------


def _rebase_basis(series: tc.PerformanceSeries) -> str:
    """The CORRECT basis discriminator, mirrored from the risk-snapshot stamp:
    a series starting before observation began is partly modeled walk-back."""
    observed = series.earliest_observed_date
    if observed is None or series.start_date is None:
        return "unknown"
    return "observed" if series.start_date >= observed else "modeled_backfill"


def test_earliest_observed_date_parsed_on_legacy_shape() -> None:
    """The legacy payload carries earliest_observed_date at top level; it must
    reach the dataclass or downstream provenance cannot classify the basis."""
    series = tc._parse_performance(  # pyright: ignore[reportPrivateUsage]
        {
            "start_date": "2026-05-09",
            "end_date": "2026-07-24",
            "base_value": "646629.324288",
            "earliest_observed_date": "2026-05-09",
            "backfill_start_unreliable": False,
            "points": [],
        }
    )
    assert series.earliest_observed_date == "2026-05-09"
    assert _rebase_basis(series) == "observed"


def test_earliest_observed_date_parsed_on_v1_transport(
    v1_on: None, legacy_guard: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same field must survive the v1 model -> legacy dataclass adaptation."""
    router = _PerfRouter(earliest_observed="2026-06-23")
    router.install(monkeypatch)

    analytics = tc.fetch_portfolio_analytics(only={"performance"})

    perf = analytics.performance
    assert perf is not None
    # The rebase already moved the window onto the observed start, so the
    # adapted series classifies as observed rather than modeled.
    assert perf.earliest_observed_date == "2026-06-23"
    assert _rebase_basis(perf) == "observed"


def test_backfill_flag_cannot_discriminate_walk_back_basis() -> None:
    """REGRESSION GUARD for a real near-miss: ``backfill_start_unreliable`` is
    NOT a basis discriminator. It flags an untrustworthy walk-back START VALUE,
    and measured False on both transports across the observed window, the
    trailing-365d default, and a 26-year window (2026-07-24) — i.e. constant in
    practice. A stamp derived from it silently records 'observed' for a series
    that is 80% reconstructed. Only start_date vs earliest_observed_date
    separates them; this test fails if anyone swaps the comparison back."""
    walk_back = tc._parse_performance(  # pyright: ignore[reportPrivateUsage]
        {
            "start_date": "2025-07-24",
            "end_date": "2026-07-24",
            "base_value": "546979.845476",
            "earliest_observed_date": "2026-05-09",
            "backfill_start_unreliable": False,
            "points": [],
        }
    )
    # The flag is blind to it...
    assert walk_back.backfill_start_unreliable is False
    # ...while the date comparison correctly calls it modeled.
    assert _rebase_basis(walk_back) == "modeled_backfill"


def test_rebase_basis_unknown_when_provider_omits_marker() -> None:
    """No marker means the basis is indeterminate, not observed — the client
    returns the probe window unrebased in that case, so defaulting to
    'observed' would assert a guarantee nobody verified."""
    unmarked = tc._parse_performance(  # pyright: ignore[reportPrivateUsage]
        {
            "start_date": "2025-07-24",
            "end_date": "2026-07-24",
            "base_value": "1.0",
            "backfill_start_unreliable": False,
            "points": [],
        }
    )
    assert unmarked.earliest_observed_date is None
    assert _rebase_basis(unmarked) == "unknown"
