"""Render-path overlap of the independent tracker fetch groups (M2 audit F7).

Contract basis: ``validation-contract.md`` VAL-SERVE-015/024/025/026. The
work-os portfolio route runs its two independent tracker fetch groups — the
live-portfolio group and the v1 allocation group — concurrently, and the
company desk route overlaps its single liveness probe with the live fetch.
Per-call timeouts, each group's internal structure, and the fail-closed
degraded envelopes are unchanged.

The overlap proofs are STRUCTURAL: barrier spies prove the groups are
in-flight simultaneously without racing the wall clock (a serial
implementation waits out the barrier timeout and fails the flags). Deadline
non-stacking is pinned with a group-seam window plus one real-socket hang
run per ``PORTFOLIO_TRACKER_V1_READS`` state, and the degraded markers are
asserted against the captured pre-change fault-mode payloads (tracker-down,
hung, and both partial-failure directions).
"""

from __future__ import annotations

import socket
import sqlite3
import threading
import time
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any, TypeVar

import comments_server
import pytest

from integrations.portfolio_allocation import (
    PortfolioAllocationBucket,
    PortfolioAllocationBuckets,
    PortfolioAllocationProjection,
    PortfolioAllocationReconciliation,
    read_portfolio_allocation,
    unavailable_portfolio_allocation,
)
from integrations.portfolio_tracker_client import LivePortfolio, LivePosition
from integrations.portfolio_tracker_v1 import (
    HealthV1,
    PositionsV1Result,
    SecuritiesV1Result,
    V1Fetch,
)
from tests.comments_server_seams import patch_server_seam
from tests.test_comments_server_dashboard import create_dashboard_test_schema

_V1_FIXTURES = Path(__file__).parent / "fixtures" / "tracker_v1"
_BARRIER_WAIT_SECONDS = 5.0
# The configured per-call read timeouts are 4.0s for both transport clients;
# a hung tracker must fail closed within ONE such window, never the stacked
# two-group sum the pre-change serial path paid (~8s).
_HANG_WINDOW_MIN_SECONDS = 3.0
_HANG_WINDOW_MAX_SECONDS = 6.0
_GROUP_WINDOW_SECONDS = 0.35

_T = TypeVar("_T")


@pytest.fixture(params=["", "1"])
def v1_reads_state(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> str:
    """Both PORTFOLIO_TRACKER_V1_READS states: unset (legacy reads) and 1."""
    if request.param:
        monkeypatch.setenv("PORTFOLIO_TRACKER_V1_READS", request.param)
    else:
        monkeypatch.delenv("PORTFOLIO_TRACKER_V1_READS", raising=False)
    return request.param


@pytest.fixture(name="tracker_app_repo")
def _tracker_app_repo(tmp_path: Path) -> Path:
    """A repo_root whose hermetic test DB carries the minimal cockpit rows.

    Seeds the same rows as the dashboard suite's repo fixture plus the
    ``llm_artifacts`` table its healthy-path test creates inline, so the
    earnings-readout projection succeeds and the payload warnings carry only
    the tracker/allocation codes the captured fault-mode payloads show."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    conn = sqlite3.connect(str(data_dir / "portfolio.db"))
    create_dashboard_test_schema(conn)
    conn.execute(
        "INSERT INTO tracked_companies (ticker, name, list_type, instrument_type) "
        "VALUES ('NU', 'Nu Holdings', 'portfolio', 'equity')"
    )
    conn.execute(
        "INSERT INTO tracked_companies (ticker, name, list_type, instrument_type) "
        "VALUES ('MELI', 'MercadoLibre', 'evaluation', 'equity')"
    )
    conn.execute(
        "INSERT INTO transcripts (ticker, period_end, has_qa_section) VALUES ('NU', '2026-03-31', 1)"
    )
    conn.execute(
        "INSERT INTO thesis_evaluations (ticker, evaluated_at, overall_status, rule_evaluations_json) "
        "VALUES ('NU', '2026-05-18T10:00:00', 'intact', '[]')"
    )
    conn.execute(
        "INSERT INTO fmp_endpoint_status (ticker, endpoint, period, status, last_pulled) "
        "VALUES ('NU', 'income-statement', 'annual', 'ok', '2026-05-11T01:02:14')"
    )
    conn.executescript(
        """
        CREATE TABLE llm_artifacts (
            id INTEGER PRIMARY KEY,
            ticker TEXT,
            scope TEXT NOT NULL DEFAULT 'ticker',
            purpose TEXT NOT NULL,
            fiscal_period TEXT,
            content_md TEXT,
            generated_at TEXT,
            superseded_by_id INTEGER
        );
        INSERT INTO llm_artifacts VALUES (
            44, 'NU', 'ticker', 'post_earnings_readout', '2026-06-30',
            'persisted readout', '2026-08-14T11:44:51Z', NULL
        );
        """
    )
    conn.commit()
    conn.close()
    return tmp_path


def _available_allocation() -> PortfolioAllocationProjection:
    empty = PortfolioAllocationBucket(value=Decimal(0), weight_pct=Decimal(0))
    return PortfolioAllocationProjection(
        state="available",
        source_identity="portfolio_tracker_api_v1",
        as_of=date(2026, 8, 8),
        currency="USD",
        buckets=PortfolioAllocationBuckets(
            us_equity=empty,
            international_equity=empty,
            us_etf=empty,
            international_etf=empty,
            cash=empty,
            unclassified=empty,
        ),
        reconciliation=PortfolioAllocationReconciliation(
            position_total=Decimal(0),
            bucket_total=Decimal(0),
            difference=Decimal(0),
            is_reconciled=True,
        ),
    )


def _healthy_live() -> LivePortfolio:
    return LivePortfolio(
        available=True,
        api_url="http://tracker.test",
        total_market_value=250_000.0,
        positions=[LivePosition("NU", "Nu Holdings", 100.0, 125_000.0, 90_000.0, 35_000.0, 50.0)],
    )


# ---------------------------------------------------------------------------
# Work-OS portfolio route: the two fetch groups overlap (VAL-SERVE-015)
# ---------------------------------------------------------------------------


def test_portfolio_route_overlaps_both_tracker_groups_when_degraded(
    v1_reads_state: str,
    tracker_app_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both groups must be in flight simultaneously; the degraded markers stay
    exactly the captured tracker-down shape (status, tracker_state, warnings,
    allocation reason)."""
    started = {"live": threading.Event(), "allocation": threading.Event()}
    overlapped = {"live": False, "allocation": False}

    def fake_live() -> LivePortfolio:
        started["live"].set()
        overlapped["live"] = started["allocation"].wait(timeout=_BARRIER_WAIT_SECONDS)
        return LivePortfolio(
            available=False, api_url="http://tracker.test", error="stub: overlap probe"
        )

    def fake_allocation() -> PortfolioAllocationProjection:
        started["allocation"].set()
        overlapped["allocation"] = started["live"].wait(timeout=_BARRIER_WAIT_SECONDS)
        return unavailable_portfolio_allocation("health_unavailable")

    patch_server_seam(monkeypatch, "fetch_live_portfolio", fake_live)
    patch_server_seam(monkeypatch, "fetch_portfolio_allocation", fake_allocation)
    client = comments_server.create_app(tracker_app_repo).test_client()

    response = client.get("/api/work-os/portfolio")

    assert response.status_code == 200
    assert overlapped["live"] is True
    assert overlapped["allocation"] is True
    payload = response.get_json()
    assert payload["status"] == "degraded"
    assert payload["tracker_state"] == "unavailable"
    assert payload["warnings"] == [
        "portfolio_tracker_unavailable",
        "portfolio_allocation_unavailable",
    ]
    assert payload["allocation"]["state"] == "unavailable"
    assert payload["allocation"]["reason_codes"] == ["health_unavailable"]


def test_portfolio_route_overlaps_both_tracker_groups_and_serves_both_halves(
    v1_reads_state: str,
    tracker_app_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Happy path: the groups overlap and both groups' data reaches the
    payload unchanged."""
    started = {"live": threading.Event(), "allocation": threading.Event()}
    overlapped = {"live": False, "allocation": False}

    def fake_live() -> LivePortfolio:
        started["live"].set()
        overlapped["live"] = started["allocation"].wait(timeout=_BARRIER_WAIT_SECONDS)
        return _healthy_live()

    def fake_allocation() -> PortfolioAllocationProjection:
        started["allocation"].set()
        overlapped["allocation"] = started["live"].wait(timeout=_BARRIER_WAIT_SECONDS)
        return _available_allocation()

    patch_server_seam(monkeypatch, "fetch_live_portfolio", fake_live)
    patch_server_seam(monkeypatch, "fetch_portfolio_allocation", fake_allocation)
    client = comments_server.create_app(tracker_app_repo).test_client()

    response = client.get("/api/work-os/portfolio")

    assert response.status_code == 200
    assert overlapped["live"] is True
    assert overlapped["allocation"] is True
    payload = response.get_json()
    assert payload["status"] == "ok"
    assert payload["total_market_value"] == 250_000.0
    assert payload["allocation"]["state"] == "available"
    assert [row["ticker"] for row in payload["companies"]] == ["NU"]


def test_portfolio_route_deadline_is_the_slowest_group_not_the_sum(
    v1_reads_state: str,
    tracker_app_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two per-group deadline windows must overlap: the route completes within
    one window plus the local build, never the stacked sum (VAL-SERVE-025)."""

    def slow_live() -> LivePortfolio:
        time.sleep(_GROUP_WINDOW_SECONDS)
        return LivePortfolio(available=False, api_url="http://tracker.test", error="stub: down")

    def slow_allocation() -> PortfolioAllocationProjection:
        time.sleep(_GROUP_WINDOW_SECONDS)
        return unavailable_portfolio_allocation("health_unavailable")

    patch_server_seam(monkeypatch, "fetch_live_portfolio", slow_live)
    patch_server_seam(monkeypatch, "fetch_portfolio_allocation", slow_allocation)
    client = comments_server.create_app(tracker_app_repo).test_client()

    started = time.monotonic()
    response = client.get("/api/work-os/portfolio")
    elapsed = time.monotonic() - started

    assert response.status_code == 200
    assert response.get_json()["status"] == "degraded"
    # One window (0.35s) plus the local build; the serial sum (~0.7s) fails.
    assert _GROUP_WINDOW_SECONDS * 0.5 < elapsed < _GROUP_WINDOW_SECONDS * 1.7


@pytest.mark.parametrize("failing_group", ["live", "allocation"])
def test_portfolio_route_partial_failure_keeps_the_surviving_group(
    failing_group: str,
    v1_reads_state: str,
    tracker_app_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One group's failure never aborts or masks the other (VAL-SERVE-026):
    the surviving half keeps its data and the failed half keeps its explicit
    degraded disposition, with the groups still overlapping."""
    started = {"live": threading.Event(), "allocation": threading.Event()}
    overlapped = {"live": False, "allocation": False}

    def live_value() -> LivePortfolio:
        started["live"].set()
        overlapped["live"] = started["allocation"].wait(timeout=_BARRIER_WAIT_SECONDS)
        if failing_group == "live":
            return LivePortfolio(
                available=False, api_url="http://tracker.test", error="stub: live group down"
            )
        return _healthy_live()

    def allocation_value() -> PortfolioAllocationProjection:
        started["allocation"].set()
        overlapped["allocation"] = started["live"].wait(timeout=_BARRIER_WAIT_SECONDS)
        if failing_group == "allocation":
            return unavailable_portfolio_allocation("securities_unavailable")
        return _available_allocation()

    patch_server_seam(monkeypatch, "fetch_live_portfolio", live_value)
    patch_server_seam(monkeypatch, "fetch_portfolio_allocation", allocation_value)
    client = comments_server.create_app(tracker_app_repo).test_client()

    response = client.get("/api/work-os/portfolio")

    assert response.status_code == 200
    assert overlapped["live"] is True
    assert overlapped["allocation"] is True
    payload = response.get_json()
    assert payload["status"] == "degraded"
    if failing_group == "live":
        assert "portfolio_tracker_unavailable" in payload["warnings"]
        assert payload["allocation"]["state"] == "available"
        assert payload["allocation"]["reason_codes"] == []
    else:
        assert "portfolio_allocation_unavailable" in payload["warnings"]
        assert payload["allocation"]["state"] == "unavailable"
        assert payload["allocation"]["reason_codes"] == ["securities_unavailable"]
        assert payload["total_market_value"] == 250_000.0


# ---------------------------------------------------------------------------
# Real transport boundaries (VAL-SERVE-024 tracker-down, VAL-SERVE-025 hung)
# ---------------------------------------------------------------------------


def _assert_down_degraded_payload(payload: dict[str, Any]) -> None:
    """The captured tracker-down markers, identical in both v1 states."""
    assert payload["status"] == "degraded"
    assert payload["tracker_state"] == "unavailable"
    assert payload["warnings"] == [
        "portfolio_tracker_unavailable",
        "portfolio_allocation_unavailable",
    ]
    assert payload["allocation"]["state"] == "unavailable"
    assert payload["allocation"]["reason_codes"] == ["health_unavailable"]


def test_portfolio_route_with_a_refusing_tracker_stays_degraded_and_fast(
    v1_reads_state: str,
    tracker_app_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tracker-down (connection refused) keeps the degraded payload and buys
    no serialized timeout (VAL-SERVE-024), through the real transport
    clients in both v1 states."""
    monkeypatch.setenv("PORTFOLIO_TRACKER_API_URL", "http://127.0.0.1:9")
    client = comments_server.create_app(tracker_app_repo).test_client()

    started = time.monotonic()
    response = client.get("/api/work-os/portfolio")
    elapsed = time.monotonic() - started

    assert response.status_code == 200
    _assert_down_degraded_payload(response.get_json())
    assert elapsed < 2.0


class _HangingTracker:
    """Accepts connections and never responds (the stub's ``--hang`` mode).

    Accepted sockets are held open for the life of the context: a discarded
    accepted socket would be garbage-collected and reset the connection,
    which reads as fast-fail instead of a hung read."""

    def __init__(self) -> None:
        self._server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server.bind(("127.0.0.1", 0))
        self._server.listen(16)
        self._connections: list[socket.socket] = []
        self._thread = threading.Thread(target=self._accept_loop, daemon=True)

    def _accept_loop(self) -> None:
        while True:
            try:
                connection, _ = self._server.accept()
            except OSError:
                return
            self._connections.append(connection)

    def __enter__(self) -> str:
        self._thread.start()
        host, port = self._server.getsockname()
        return f"http://{host}:{port}"

    def __exit__(self, *exc_info: object) -> None:
        self._server.close()
        for connection in self._connections:
            connection.close()
        self._connections.clear()


def test_portfolio_route_with_a_hung_tracker_fails_closed_within_one_window(
    v1_reads_state: str,
    tracker_app_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A hung tracker fails closed with the same degraded payload shape within
    ONE per-call read-timeout window — the overlapping groups must not stack
    or multiply their timeouts (VAL-SERVE-025)."""
    hanging = _HangingTracker()
    with hanging as base_url:
        monkeypatch.setenv("PORTFOLIO_TRACKER_API_URL", base_url)
        client = comments_server.create_app(tracker_app_repo).test_client()

        started = time.monotonic()
        response = client.get("/api/work-os/portfolio")
        elapsed = time.monotonic() - started

    assert response.status_code == 200
    _assert_down_degraded_payload(response.get_json())
    assert _HANG_WINDOW_MIN_SECONDS < elapsed < _HANG_WINDOW_MAX_SECONDS


# ---------------------------------------------------------------------------
# Allocation group internals: overlapping reads, serial precedence
# ---------------------------------------------------------------------------


def _fixture_health() -> HealthV1:
    return HealthV1.model_validate_json((_V1_FIXTURES / "health.json").read_bytes())


def _fixture_positions() -> PositionsV1Result:
    return PositionsV1Result.model_validate_json((_V1_FIXTURES / "positions.json").read_bytes())


def _fixture_securities() -> SecuritiesV1Result:
    return SecuritiesV1Result.model_validate_json((_V1_FIXTURES / "securities.json").read_bytes())


def test_read_portfolio_allocation_overlaps_all_three_v1_reads() -> None:
    """The three v1 reads are independent snapshots and must be in flight
    simultaneously; the healthy projection is unchanged by the overlap."""
    started = {name: threading.Event() for name in ("health", "positions", "securities")}
    overlapped = {name: False for name in started}
    others = {
        "health": ("positions", "securities"),
        "positions": ("health", "securities"),
        "securities": ("health", "positions"),
    }

    class _BarrierReader:
        def probe_v1(self) -> V1Fetch[HealthV1]:
            started["health"].set()
            overlapped["health"] = all(
                started[name].wait(timeout=_BARRIER_WAIT_SECONDS) for name in others["health"]
            )
            return V1Fetch(available=True, endpoint="/api/v1/health", data=_fixture_health())

        def get_positions(self) -> V1Fetch[PositionsV1Result]:
            started["positions"].set()
            overlapped["positions"] = all(
                started[name].wait(timeout=_BARRIER_WAIT_SECONDS) for name in others["positions"]
            )
            return V1Fetch(
                available=True,
                endpoint="/api/v1/portfolio/positions",
                data=_fixture_positions(),
            )

        def get_securities(self) -> V1Fetch[SecuritiesV1Result]:
            started["securities"].set()
            overlapped["securities"] = all(
                started[name].wait(timeout=_BARRIER_WAIT_SECONDS) for name in others["securities"]
            )
            return V1Fetch(
                available=True, endpoint="/api/v1/securities", data=_fixture_securities()
            )

    result = read_portfolio_allocation(_BarrierReader())

    assert all(overlapped.values())
    # The official fixtures carry one unclassified security, so the healthy
    # projection itself is the captured "incomplete" baseline.
    assert result.state == "incomplete"
    assert result.reason_codes == ("portfolio_allocation_incomplete",)
    assert result.reconciliation.is_reconciled is True


def _mode_fetch(data: _T, mode: str, endpoint: str) -> V1Fetch[_T]:
    """One synthetic v1 read in the given fault mode: "ok", "unavailable"
    (explicit degraded envelope), or "raise" (transport-level exception)."""
    if mode == "raise":
        raise RuntimeError(f"stub fault: {endpoint} read failed")
    if mode == "unavailable":
        return V1Fetch(available=False, endpoint=endpoint, error="stub: unavailable")
    return V1Fetch(available=True, endpoint=endpoint, data=data)


class _FaultReader:
    """Instant synthetic allocation reader with per-read fault modes, so the
    RESULT precedence is pinned independent of completion order."""

    def __init__(self, health: str, positions: str, securities: str) -> None:
        self._modes = (health, positions, securities)

    def probe_v1(self) -> V1Fetch[HealthV1]:
        return _mode_fetch(_fixture_health(), self._modes[0], "/api/v1/health")

    def get_positions(self) -> V1Fetch[PositionsV1Result]:
        return _mode_fetch(_fixture_positions(), self._modes[1], "/api/v1/portfolio/positions")

    def get_securities(self) -> V1Fetch[SecuritiesV1Result]:
        return _mode_fetch(_fixture_securities(), self._modes[2], "/api/v1/securities")


@pytest.mark.parametrize(
    ("health", "positions", "securities", "expected_reason"),
    [
        ("unavailable", "ok", "ok", "health_unavailable"),
        ("raise", "ok", "ok", "health_unavailable"),
        ("ok", "unavailable", "ok", "positions_unavailable"),
        ("ok", "raise", "ok", "positions_unavailable"),
        ("ok", "ok", "unavailable", "securities_unavailable"),
        ("ok", "ok", "raise", "securities_unavailable"),
        ("raise", "raise", "raise", "health_unavailable"),
        ("unavailable", "unavailable", "unavailable", "health_unavailable"),
    ],
)
def test_read_portfolio_allocation_reports_the_serial_reason_codes(
    health: str, positions: str, securities: str, expected_reason: str
) -> None:
    """Degradation precedence is applied to the RESULTS in the fixed serial
    order (health, then positions, then securities) whatever the completion
    order: every fault mode reports the identical reason code, a failure is
    never reported as success, and the surviving reads are never re-walked."""
    result = read_portfolio_allocation(_FaultReader(health, positions, securities))

    assert result.state == "unavailable"
    assert result.reason_codes == (expected_reason,)
    assert result.buckets.us_equity.value is None


# ---------------------------------------------------------------------------
# Company desk route: one overlapping liveness probe (VAL-SERVE-015)
# ---------------------------------------------------------------------------


def test_desk_route_overlaps_probe_with_live_fetch_and_issues_one_probe(
    v1_reads_state: str,
    tracker_app_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The single liveness probe and the live-portfolio fetch are in flight
    simultaneously; the request still issues at most ONE probe and serves the
    fetched live book."""
    started = {"probe": threading.Event(), "fetch": threading.Event()}
    overlapped = {"probe": False, "fetch": False}
    probe_calls = {"count": 0}

    def fake_probe() -> tuple[bool, str]:
        probe_calls["count"] += 1
        started["probe"].set()
        overlapped["probe"] = started["fetch"].wait(timeout=_BARRIER_WAIT_SECONDS)
        return (True, "http://tracker.test")

    def fake_fetch() -> LivePortfolio:
        started["fetch"].set()
        overlapped["fetch"] = started["probe"].wait(timeout=_BARRIER_WAIT_SECONDS)
        return _healthy_live()

    patch_server_seam(monkeypatch, "probe_tracker", fake_probe)
    patch_server_seam(monkeypatch, "fetch_live_portfolio", fake_fetch)
    client = comments_server.create_app(tracker_app_repo).test_client()

    response = client.get("/api/work-os/companies/NU/desk")

    assert response.status_code == 200
    assert probe_calls["count"] == 1
    assert overlapped["probe"] is True
    assert overlapped["fetch"] is True
    payload = response.get_json()
    assert payload["position"]["weight_pct"] == pytest.approx(50.0)
    assert payload["position"]["market_value"] == pytest.approx(125_000.0)
    assert payload["position"]["position_source"] == "portfolio_tracker_api"


def test_desk_route_probe_failure_is_prompt_and_discards_the_inflight_fetch(
    v1_reads_state: str,
    tracker_app_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed probe keeps the captured degraded desk envelope, still issues
    exactly one probe, and never waits for the still-running fetch."""
    probe_calls = {"count": 0}

    def fake_probe() -> tuple[bool, str]:
        probe_calls["count"] += 1
        return (False, "http://tracker.test")

    def fake_fetch() -> LivePortfolio:
        time.sleep(0.6)
        return _healthy_live()

    patch_server_seam(monkeypatch, "probe_tracker", fake_probe)
    patch_server_seam(monkeypatch, "fetch_live_portfolio", fake_fetch)
    client = comments_server.create_app(tracker_app_repo).test_client()

    started = time.monotonic()
    response = client.get("/api/work-os/companies/NU/desk")
    elapsed = time.monotonic() - started

    assert response.status_code == 200
    assert probe_calls["count"] == 1
    assert elapsed < 0.3
    payload = response.get_json()
    assert payload["position"]["position_state"] == "unavailable"
    assert "portfolio_tracker_unavailable" in payload["warnings"]
