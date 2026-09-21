"""S14 perceived-latency server seams: ETag/304 on /api/panel/* + the
in-memory panel-timing metrics endpoints.

The ETag layer is an after_request hook — every 200-OK GET panel fragment
carries a content ETag and `Cache-Control: no-cache`, and a matching
If-None-Match revalidation comes back 304 with an empty body (the
stale-while-revalidate client then keeps its cached copy). The metrics pair
(`POST`/`GET /api/metrics/panel`) is a deque ring for latency — plus, since
navigation_ia §5 (instrument-first), a durable ``panel_activation_counts``
(panel_id, day) counter bumped only by user-perceived activations (cold|swr).
"""

from __future__ import annotations

import sqlite3
import sys
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    from flask.testing import FlaskClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "execution"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import comments_server
from comments_server_panel_cache import (
    CLEAR_ALL,
    MUTATION_ROUTE_CACHE_REGISTRY,
)


@pytest.fixture
def client(tmp_path: Path, migrated_db: Callable[..., Path]) -> FlaskClient:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    migrated_db(data_dir / "portfolio.db")
    app = comments_server.create_app(tmp_path)
    return app.test_client()


# ---------------------------------------------------------------------------
# ETag / 304 on panel fragments
# ---------------------------------------------------------------------------


def test_panel_fragment_carries_etag_and_no_cache(client: FlaskClient) -> None:
    # /api/panel/actions renders with no DB dependency — fully deterministic.
    resp = client.get("/api/panel/actions")
    assert resp.status_code == 200
    assert resp.headers.get("ETag")
    assert resp.headers.get("Cache-Control") == "no-cache"
    assert resp.data  # the fragment body itself


def test_panel_fragment_revalidation_is_a_304(client: FlaskClient) -> None:
    first = client.get("/api/panel/actions")
    etag = first.headers["ETag"]
    again = client.get("/api/panel/actions", headers={"If-None-Match": etag})
    assert again.status_code == 304
    assert again.data == b""
    # A stale validator gets the full body back.
    stale = client.get("/api/panel/actions", headers={"If-None-Match": '"deadbeef"'})
    assert stale.status_code == 200
    assert stale.data == first.data


def test_fresh_panel_revalidation_skips_the_expensive_renderer(
    client: FlaskClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A matching validator must short-circuit before the route builder runs.

    A post-render ``make_conditional`` still pays the database/calculation cost,
    which is the dominant latency on the heavy panels.
    """
    from pipeline import dashboard_html

    calls = 0

    def _render() -> str:
        nonlocal calls
        calls += 1
        return "<section>cached actions</section>"

    monkeypatch.setattr(dashboard_html, "render_actions_panel", _render)
    first = client.get("/api/panel/actions")
    again = client.get(
        "/api/panel/actions",
        headers={"If-None-Match": first.headers["ETag"]},
    )
    assert first.status_code == 200
    assert again.status_code == 304
    assert calls == 1


def test_metrics_post_preserves_panel_response_cache(
    client: FlaskClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from pipeline import dashboard_html

    calls = 0

    def _render() -> str:
        nonlocal calls
        calls += 1
        return f"<section>actions render {calls}</section>"

    monkeypatch.setattr(dashboard_html, "render_actions_panel", _render)
    assert client.get("/api/panel/actions").status_code == 200
    assert (
        client.post(
            "/api/metrics/panel",
            json={"panel": "actions", "cache": "cold", "total_ms": 1},
        ).status_code
        == 204
    )
    second = client.get("/api/panel/actions")
    assert second.status_code == 200
    assert calls == 1
    assert b"render 1" in second.data


def test_state_change_invalidates_panel_response_cache(
    client: FlaskClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from pipeline import dashboard_html

    calls = 0

    def _render() -> str:
        nonlocal calls
        calls += 1
        return f"<section>actions render {calls}</section>"

    client.application.add_url_rule(
        "/test/domain-mutation",
        "test_domain_mutation",
        lambda: ("", 204),
        methods=["POST"],
    )
    monkeypatch.setattr(dashboard_html, "render_actions_panel", _render)
    assert client.get("/api/panel/actions").status_code == 200
    assert client.post("/test/domain-mutation").status_code == 204

    second = client.get("/api/panel/actions")
    assert second.status_code == 200
    assert calls == 2
    assert b"render 2" in second.data


@pytest.mark.parametrize("registered", [False, True])
@pytest.mark.parametrize("get_finishes_first", [False, True])
@pytest.mark.parametrize("mutation_status", [204, 500])
def test_mutation_completion_rejects_concurrent_old_state(
    client: FlaskClient,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    registered: bool,
    get_finishes_first: bool,
    mutation_status: int,
) -> None:
    from pipeline import dashboard_html

    state_path = tmp_path / "cache-state.txt"
    state_path.write_text("before", encoding="utf-8")
    mutation_started = threading.Event()
    allow_commit = threading.Event()
    old_state_read = threading.Event()
    allow_render = threading.Event()
    route = "/test/concurrent-mutation"
    if registered:
        monkeypatch.setitem(MUTATION_ROUTE_CACHE_REGISTRY, route, ("/api/panel/actions",))

    def mutate() -> tuple[str, int]:
        mutation_started.set()
        assert allow_commit.wait(5)
        state_path.write_text("after", encoding="utf-8")
        # An error response can follow a partial persisted mutation too.
        return "", mutation_status

    def render() -> str:
        value = state_path.read_text(encoding="utf-8")
        if value == "before":
            old_state_read.set()
            assert allow_render.wait(5)
        return f"<section>{value}</section>"

    def post() -> int:
        with client.application.test_client() as worker:
            return worker.post(route).status_code

    def get() -> bytes:
        with client.application.test_client() as worker:
            response = worker.get("/api/panel/actions")
            assert response.status_code == 200
            return response.data

    client.application.add_url_rule(route, "concurrent_mutation", mutate, methods=["POST"])
    monkeypatch.setattr(dashboard_html, "render_actions_panel", render)
    with ThreadPoolExecutor(max_workers=2) as pool:
        mutation = pool.submit(post)
        try:
            assert mutation_started.wait(5)
            reading = pool.submit(get)
            assert old_state_read.wait(5)
            if get_finishes_first:
                allow_render.set()
                assert reading.result(timeout=5) == b"<section>before</section>"
            allow_commit.set()
            assert mutation.result(timeout=5) == mutation_status
            allow_render.set()
            assert reading.result(timeout=5) == b"<section>before</section>"
        finally:
            allow_commit.set()
            allow_render.set()
    assert state_path.read_text(encoding="utf-8") == "after"
    assert client.get("/api/panel/actions").data == b"<section>after</section>"


# ---------------------------------------------------------------------------
# W6 mutation-route → cache-family registry
# ---------------------------------------------------------------------------


def test_mutation_route_registry_is_total_over_the_route_table(
    client: Any,
) -> None:
    """Every non-GET rule in the app is registered with explicit cache
    families (possibly an empty no-op) or deliberately marked CLEAR_ALL. An
    unregistered mutation route fails safe to a full clear(), so it must be
    impossible to add one without this test noticing."""
    rules: list[Any] = list(client.application.url_map.iter_rules())
    mutation_rules: set[str] = {
        route.rule
        for route in rules
        if {method for method in route.methods if method not in ("GET", "HEAD", "OPTIONS")}
    }
    all_rules: set[str] = {route.rule for route in rules}
    for rule in sorted(mutation_rules):
        families = MUTATION_ROUTE_CACHE_REGISTRY.get(rule)
        assert families is not None, (
            f"mutation route {rule!r} is not in MUTATION_ROUTE_CACHE_REGISTRY; "
            "register its cache families or mark it CLEAR_ALL"
        )
        assert isinstance(families, (tuple, type(CLEAR_ALL))), rule
    # No dead registry entries: a route removed from the app must drop its row.
    stale = sorted(set(MUTATION_ROUTE_CACHE_REGISTRY) - all_rules)
    assert stale == [], f"registry rows for routes no longer in the app: {stale}"


def test_decision_write_keeps_unrelated_panel_hot(client: Any, monkeypatch: Any) -> None:
    """A decision write invalidates the evaluation/decision families only — an
    unrelated hot panel fragment must stay cached (X-Panel-Cache: hit)."""
    from pipeline import dashboard_html

    calls = 0

    def _render() -> str:
        nonlocal calls
        calls += 1
        return f"<section>actions render {calls}</section>"

    monkeypatch.setattr(dashboard_html, "render_actions_panel", _render)
    assert client.get("/api/panel/actions").status_code == 200
    assert client.get("/api/panel/actions").headers["X-Panel-Cache"] == "hit"
    assert calls == 1

    # Manual pass/avoid decision write (→ Work OS evaluation + decision family).
    resp = client.post(
        "/api/decisions/pass",
        json={"ticker": "NU", "reason": "missed the setup"},
    )
    assert resp.status_code == 200

    assert client.get("/api/panel/actions").headers["X-Panel-Cache"] == "hit"
    assert calls == 1
    assert b"render 1" in client.get("/api/panel/actions").data


def _warm_panel(client: Any, path: str) -> None:
    """One 200 miss (store) followed by a guaranteed hit — returns nothing."""
    first: Any = client.get(path)
    assert first.status_code == 200
    assert first.headers["X-Panel-Cache"] == "miss"
    second: Any = client.get(path)
    assert second.status_code == 200
    assert second.headers["X-Panel-Cache"] == "hit"


def test_ticker_settings_write_evicts_diet_panel(client: Any) -> None:
    _warm_panel(client, "/api/panel/diet")

    response: Any = client.post(
        "/api/ticker-settings/NU",
        json={"auto_pre_earnings_brief": True},
    )
    assert response.status_code == 200

    rebuilt: Any = client.get("/api/panel/diet")
    assert rebuilt.status_code == 200
    assert rebuilt.headers["X-Panel-Cache"] == "miss"


def test_decision_write_evicts_overview_panel(client: Any) -> None:
    _warm_panel(client, "/api/panel/overview")

    response: Any = client.post(
        "/api/decisions/pass",
        json={"ticker": "NU", "reason": "overview invalidation probe"},
    )
    assert response.status_code == 200

    rebuilt: Any = client.get("/api/panel/overview")
    assert rebuilt.status_code == 200
    assert rebuilt.headers["X-Panel-Cache"] == "miss"


def test_sizing_intent_write_evicts_work_os_portfolio(client: Any) -> None:
    _warm_work_os(client, "/api/work-os/portfolio")

    response: Any = client.post(
        "/api/sizing-intents",
        json={"ticker": "NU", "conviction": 4},
    )
    assert response.status_code == 200

    rebuilt: Any = client.get("/api/work-os/portfolio")
    assert rebuilt.status_code == 200
    assert rebuilt.headers["X-Panel-Cache"] == "miss"


def test_earnings_readout_generation_evicts_work_os_portfolio(
    client: Any, monkeypatch: Any
) -> None:
    from earnings_readout import GENERATED, GenerateOutcome

    def _generate_readout(_db_path: Path, _repo_root: Path, _ticker: str) -> GenerateOutcome:
        return GenerateOutcome(GENERATED, "NU", "2026-06-30", None)

    monkeypatch.setattr(
        "earnings_readout.generate_for_ticker",
        _generate_readout,
    )
    _warm_work_os(client, "/api/work-os/portfolio")

    response: Any = client.post(
        "/api/earnings-readout/generate",
        json={"ticker": "NU"},
    )
    assert response.status_code == 200

    rebuilt: Any = client.get("/api/work-os/portfolio")
    assert rebuilt.status_code == 200
    assert rebuilt.headers["X-Panel-Cache"] == "miss"


def _warm_work_os(client: Any, path: str) -> None:
    """One 200 miss (store) followed by a guaranteed hit — returns nothing."""
    first: Any = client.get(path)
    assert first.status_code == 200
    assert first.headers["X-Panel-Cache"] == "miss"
    second: Any = client.get(path)
    assert second.status_code == 200
    assert second.headers["X-Panel-Cache"] == "hit"


def test_comment_write_evicts_exactly_its_mapped_family(
    client: Any,
) -> None:
    """A comment write evicts the Work OS desk + brief library families while
    leaving the unrelated Work OS evaluation surface cached (W6 task contract:
    'a comment write affects desk/briefs')."""
    _warm_work_os(client, "/api/work-os/evaluation")
    _warm_work_os(client, "/api/work-os/briefs")

    created: Any = client.post(
        "/comments",
        json={
            "ticker": "NU",
            "report_date": "2026-05-18",
            "anchor": {"type": "free_text", "key": "working capital heading"},
            "comment": "check the working-capital line",
        },
    )
    assert created.status_code == 201

    # Evaluation is untouched (still hot); briefs must rebuild (fresh miss).
    again: Any = client.get("/api/work-os/evaluation")
    assert again.status_code == 200
    assert again.headers["X-Panel-Cache"] == "hit"
    rebuilt: Any = client.get("/api/work-os/briefs")
    assert rebuilt.status_code == 200
    assert rebuilt.headers["X-Panel-Cache"] == "miss"


def test_unknown_mutation_clears_work_os_cache_too(
    client: Any,
) -> None:
    """The old fail-safe survives: an unregistered mutation route still evicts
    every cached surface, including the Phase-1 work-os families."""
    client.application.add_url_rule(
        "/test/unregistered-mutation",
        "test_unregistered_mutation",
        lambda: ("", 204),
        methods=["POST"],
    )
    _warm_work_os(client, "/api/work-os/evaluation")

    assert client.post("/test/unregistered-mutation").status_code == 204

    after: Any = client.get("/api/work-os/evaluation")
    assert after.status_code == 200
    assert after.headers["X-Panel-Cache"] == "miss"


def test_declared_noop_mutation_does_not_evict_work_os_cache(
    client: Any,
) -> None:
    """Observational telemetry (a registered no-op) must never evict anything."""
    _warm_work_os(client, "/api/work-os/evaluation")

    assert (
        client.post(
            "/api/metrics/panel",
            json={"panel": "evaluation", "cache": "cold", "total_ms": 1},
        ).status_code
        == 204
    )

    after: Any = client.get("/api/work-os/evaluation")
    assert after.status_code == 200
    assert after.headers["X-Panel-Cache"] == "hit"


def test_etag_scope_is_panel_gets_only(client: FlaskClient) -> None:
    """The hook must not touch non-panel routes (e.g. the JSON healthz) —
    they keep their default cache headers."""
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.headers.get("Cache-Control") != "no-cache"
    # 404 panels pass through the hook untouched.
    missing = client.get("/api/panel/definitely_not_a_panel")
    assert missing.status_code == 404


def test_responses_expose_server_timing(client: FlaskClient) -> None:
    resp = client.get("/healthz")
    timing = resp.headers.get("Server-Timing", "")
    assert timing.startswith("app;dur=")
    assert float(timing.removeprefix("app;dur=")) >= 0


# ---------------------------------------------------------------------------
# Panel timing metrics ring
# ---------------------------------------------------------------------------


def test_metrics_post_then_aggregate(client: FlaskClient) -> None:
    samples = [
        {
            "panel": "portfolio",
            "cache": "cold",
            "fetch_ms": 410.0,
            "render_ms": 40,
            "total_ms": 450,
        },
        {"panel": "portfolio", "cache": "swr", "render_ms": 12, "total_ms": 12.4},
        {
            "panel": "portfolio",
            "cache": "revalidate",
            "fetch_ms": 95,
            "total_ms": 95,
            "status": 304,
        },
        {"panel": "evals", "cache": "prefetch", "total_ms": 8},
    ]
    for s in samples:
        assert client.post("/api/metrics/panel", json=s).status_code == 204

    agg = client.get("/api/metrics/panel").get_json()
    assert agg["samples"] == 4
    by_key = {(r["panel"], r["cache"]): r for r in agg["rows"]}
    assert by_key[("portfolio", "cold")]["n"] == 1
    assert by_key[("portfolio", "cold")]["p50_ms"] == 450
    assert by_key[("evals", "prefetch")]["p50_ms"] == 8
    # Perceived = activations only; the 95ms background revalidate is excluded
    # (with it, p95 of {450, 12.4, 8} could not sit at 450).
    assert by_key[("portfolio", "revalidate")]["n"] == 1
    assert agg["perceived_p95_ms"] == 450
    assert agg["perceived_p50_ms"] == pytest.approx(12.4, abs=0.5)


def test_metrics_post_rejects_malformed(client: FlaskClient) -> None:
    assert client.post("/api/metrics/panel", json={"cache": "cold"}).status_code == 400
    assert (
        client.post("/api/metrics/panel", json={"panel": "x", "cache": "warp"}).status_code == 400
    )
    # Out-of-range / non-numeric timings are dropped to null, not 400.
    ok = client.post(
        "/api/metrics/panel",
        json={"panel": "x", "cache": "cold", "total_ms": "fast", "fetch_ms": -3},
    )
    assert ok.status_code == 204
    agg = client.get("/api/metrics/panel").get_json()
    # The null-total sample is held in the ring but aggregates to no row.
    assert agg["samples"] == 1
    assert agg["rows"] == []


def test_activation_counts_persist_per_panel_per_day(client: FlaskClient, tmp_path: Path) -> None:
    """navigation_ia §5: cold|swr samples bump the durable (panel_id, day)
    counter — prefetch/revalidate are speculative/background and must NOT
    count as visits. The GET aggregate surfaces the 30-day totals."""
    samples = [
        {"panel": "musings", "cache": "cold", "total_ms": 100},
        {"panel": "musings", "cache": "swr", "total_ms": 10},
        {"panel": "musings", "cache": "prefetch", "total_ms": 8},  # not a visit
        {"panel": "musings", "cache": "revalidate", "total_ms": 90},  # background
        {"panel": "portfolio_synthesis", "cache": "cold", "total_ms": 200},
    ]
    for s in samples:
        assert client.post("/api/metrics/panel", json=s).status_code == 204
    # Durable: the counter lives in the DB, not the ring.
    conn = sqlite3.connect(str(tmp_path / "data" / "portfolio.db"))
    try:
        rows = dict(
            conn.execute(
                "SELECT panel_id, SUM(count) FROM panel_activation_counts GROUP BY panel_id"
            ).fetchall()
        )
    finally:
        conn.close()
    assert rows == {"musings": 2, "portfolio_synthesis": 1}
    agg = client.get("/api/metrics/panel").get_json()
    assert agg["activations_30d"] == {"musings": 2, "portfolio_synthesis": 1}


def test_source_calls_panel_carries_latency_readout(client: FlaskClient) -> None:
    """System → Data Cache surfaces the timings: the fragment ships the
    latency section + the script that fetches the GET aggregate — including
    on the empty-DB degrade path."""
    resp = client.get("/api/panel/source_calls")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert 'id="sc-panel-latency"' in html
    assert "'/api/metrics/panel'" in html
    assert "Panel latency" in html
