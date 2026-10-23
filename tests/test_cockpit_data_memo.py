"""M2 F5: the shared cockpit-data memo — one ``build_cockpit_rows`` per cold
paint cycle instead of one per route.

The full-data routes (``/api/panel/overview`` and
``/api/work-os/evaluation``) share one
data-level memo keyed on DATA identity (repo root + resolved DB path + schema
revision), never the request path. These tests pin the contract the validation
suite asserts (VAL-SERVE-018..023):

* one build per cold paint cycle, and still one across a warm repaint;
* the key separates databases and checkouts, not request paths;
* mapped mutation routes drop the memo with the response cache (never a
  narrower set), unmapped families keep it inside the 30s window, and an
  unknown route fails safe to a full clear;
* the 30s staleness bound survives: a memo entry expires on its own data
  clock, and a response entry rendered from memoized data cannot outlive it;
* fail-closed degradation: a build error never poisons the memo, and a Busy
  lookup builds directly;
* the entry count is bounded (oldest-first eviction) and concurrent cold
  readers coalesce into one build (single-flight);
* the response contracts are untouched: the overview fragment keeps
  ETag/304, the work-os hydrations keep no-store and never emit an ETag.
"""

from __future__ import annotations

import sqlite3
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

import pytest

if TYPE_CHECKING:
    from flask import Flask
    from flask.testing import FlaskClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "execution"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import comments_server
import comments_server_cockpit_memo as memo_module
import comments_server_context
import comments_server_panel_cache as panel_cache_module
from comments_server_cockpit_memo import (
    CockpitDataBusy,
    CockpitDataHit,
    CockpitDataMemo,
    CockpitDataReservation,
    cockpit_data_memo_key,
)
from comments_server_panel_cache import PanelCacheEntry, PanelResponseCache

from pipeline.research_cockpit import build_cockpit_rows
from tests.comments_server_seams import patch_server_seam

PAINT_CYCLE_PATHS = (
    "/api/panel/overview",
    "/api/work-os/portfolio",
    "/api/work-os/evaluation",
)


@pytest.fixture
def app(tmp_path: Path, migrated_db: Callable[..., Path]) -> Flask:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    migrated_db(data_dir / "portfolio.db")
    return comments_server.create_app(tmp_path)


@pytest.fixture
def client(app: Flask) -> FlaskClient:
    return app.test_client()


@pytest.fixture
def build_calls(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Count ``build_cockpit_rows`` invocations through the server's name."""
    calls: list[int] = []
    real_build = build_cockpit_rows

    def counting_build(
        conn: sqlite3.Connection, repo_root: Path, *, include_fundamentals: bool = True
    ) -> dict[str, list[Any]]:
        if include_fundamentals:
            calls.append(1)
        return real_build(conn, repo_root, include_fundamentals=include_fundamentals)

    patch_server_seam(monkeypatch, "build_cockpit_rows", counting_build)
    return calls


def _memo_of(app: Flask) -> CockpitDataMemo:
    memo = cast(object, app.config["COCKPIT_DATA_MEMO"])
    assert isinstance(memo, CockpitDataMemo)
    return memo


def _memo_key_of(app: Flask) -> str:
    key = cast(object, app.config["COCKPIT_DATA_MEMO_KEY"])
    assert isinstance(key, str)
    return key


# ---------------------------------------------------------------------------
# One build per paint cycle (VAL-SERVE-012 / request-matrix)
# ---------------------------------------------------------------------------


def test_cold_paint_cycle_builds_cockpit_rows_once(
    client: FlaskClient, build_calls: list[int]
) -> None:
    """Overview → portfolio → evaluation on a cold app: three 200s, ONE build."""
    for path in PAINT_CYCLE_PATHS:
        resp = client.get(path)
        assert resp.status_code == 200, path
    assert len(build_calls) == 1
    # The published snapshot serves every route: the memo holds exactly the
    # one entry for this data identity.
    assert _memo_of(client.application).entry_count() == 1


def test_warm_repaint_reuses_the_same_snapshot(client: FlaskClient, build_calls: list[int]) -> None:
    """A second full paint cycle within the TTL adds no build."""
    for path in PAINT_CYCLE_PATHS * 2:
        assert client.get(path).status_code == 200
    assert len(build_calls) == 1


def test_memo_serves_the_same_rows_a_fresh_build_would(
    client: FlaskClient, build_calls: list[int], tmp_path: Path
) -> None:
    """The memoized snapshot equals a fresh direct build of the same data."""
    for path in PAINT_CYCLE_PATHS:
        assert client.get(path).status_code == 200
    assert len(build_calls) == 1

    memo = _memo_of(client.application)
    lookup = memo.get_or_reserve(_memo_key_of(client.application))
    assert isinstance(lookup, CockpitDataHit)

    db_path = tmp_path / "data" / "portfolio.db"
    conn = sqlite3.connect(db_path)
    try:
        fresh = build_cockpit_rows(conn, tmp_path)
    finally:
        conn.close()
    memo_rows = {role: list(rows) for role, rows in lookup.entry.rows.items()}
    assert repr(memo_rows) == repr(fresh)


# ---------------------------------------------------------------------------
# Data-identity keying (VAL-SERVE-018 / VAL-SERVE-021)
# ---------------------------------------------------------------------------


def test_memo_key_separates_databases_not_request_paths(tmp_path: Path) -> None:
    db_a = tmp_path / "a" / "portfolio.db"
    db_a.parent.mkdir()
    db_a.write_bytes(b"")
    db_b = tmp_path / "b" / "portfolio.db"
    db_b.parent.mkdir()
    db_b.write_bytes(b"")
    other_root = tmp_path / "other"
    other_root.mkdir()

    same_a = cockpit_data_memo_key(tmp_path, db_a)
    assert same_a == cockpit_data_memo_key(tmp_path, db_a)
    # Different database identity → different key.
    assert same_a != cockpit_data_memo_key(tmp_path, db_b)
    # Different checkout identity → different key.
    assert same_a != cockpit_data_memo_key(other_root, db_a)


def test_two_apps_over_two_databases_never_share_a_snapshot(
    tmp_path: Path, migrated_db: Callable[..., Path], build_calls: list[int]
) -> None:
    """Two apps in ONE process, each with its own migrated DB: the first paint
    of the second app must build again (its data identity is different), and
    its memo must hold its own entry."""
    for rel in ("one", "two"):
        data_dir = tmp_path / rel / "data"
        data_dir.mkdir(parents=True)
        migrated_db(data_dir / "portfolio.db")
    app_one = comments_server.create_app(tmp_path / "one")
    app_two = comments_server.create_app(tmp_path / "two")

    for app in (app_one, app_two):
        for path in PAINT_CYCLE_PATHS:
            resp = app.test_client().get(path)
            assert resp.status_code == 200, path
    assert len(build_calls) == 2  # one per database, none shared


# ---------------------------------------------------------------------------
# Mutation-family invalidation (VAL-SERVE-019)
# ---------------------------------------------------------------------------


def test_mapped_overview_mutation_drops_the_memo(
    client: FlaskClient, build_calls: list[int]
) -> None:
    """/api/decisions/pass maps to the overview + evaluation families: the
    next paint must rebuild (memo cleared with the response cache)."""
    for path in PAINT_CYCLE_PATHS:
        assert client.get(path).status_code == 200
    assert len(build_calls) == 1

    resp = client.post(
        "/api/decisions/pass",
        json={"ticker": "NU", "reason": "memo invalidation probe"},
    )
    assert resp.status_code == 200
    assert _memo_of(client.application).entry_count() == 0

    # Every cockpit-memo surface rebuilds on the next paint.
    for path in PAINT_CYCLE_PATHS:
        rebuilt = client.get(path)
        assert rebuilt.status_code == 200
    assert len(build_calls) == 2


def test_work_os_mapped_mutation_drops_the_memo_too(
    client: FlaskClient, build_calls: list[int]
) -> None:
    """A mutation mapped to a work-os family clears the shared memo even
    though the entry was published via a different route's request. The
    overview response entry itself stays hot (the mutation's families do not
    touch it), so the memo's rebuild shows up on the work-os repaint."""
    for path in PAINT_CYCLE_PATHS:
        assert client.get(path).status_code == 200
    assert len(build_calls) == 1

    resp = client.post("/api/sizing-intents", json={"ticker": "NU", "conviction": 4})
    assert resp.status_code == 200
    assert _memo_of(client.application).entry_count() == 0

    # The portfolio projection stays separate; evaluation rebuilds full data.
    portfolio = client.get("/api/work-os/portfolio")
    assert portfolio.status_code == 200
    assert len(build_calls) == 1
    rebuilt = client.get("/api/work-os/evaluation")
    assert rebuilt.status_code == 200
    assert rebuilt.headers["X-Panel-Cache"] == "miss"
    assert len(build_calls) == 2
    # The overview response entry was NOT in the mutation's families: it
    # still serves from the response cache without consulting the memo.
    overview = client.get("/api/panel/overview")
    assert overview.headers["X-Panel-Cache"] == "hit"
    assert len(build_calls) == 2


def test_unmapped_family_mutation_keeps_the_memo(
    client: FlaskClient, build_calls: list[int]
) -> None:
    """A mutation whose registry families do not feed the cockpit (ticker
    settings → diet) keeps the memo inside its 30s window — the same window
    the response cache already granted pre-memo."""
    for path in PAINT_CYCLE_PATHS:
        assert client.get(path).status_code == 200
    assert len(build_calls) == 1

    resp = client.post(
        "/api/ticker-settings/NU",
        json={"auto_pre_earnings_brief": True},
    )
    assert resp.status_code == 200
    assert _memo_of(client.application).entry_count() == 1

    for path in PAINT_CYCLE_PATHS:
        assert client.get(path).status_code == 200
    assert len(build_calls) == 1  # memo served every route


def test_unknown_mutation_fails_safe_to_a_full_memo_clear(
    client: FlaskClient, build_calls: list[int]
) -> None:
    """An unregistered mutation route clears the memo exactly like the
    response cache — a new mutation surface can never serve memo-stale
    cockpit rows."""
    client.application.add_url_rule(
        "/test/unregistered-mutation",
        "test_unregistered_mutation",
        lambda: ("", 204),
        methods=["POST"],
    )
    for path in PAINT_CYCLE_PATHS:
        assert client.get(path).status_code == 200
    assert len(build_calls) == 1

    assert client.post("/test/unregistered-mutation").status_code == 204
    assert _memo_of(client.application).entry_count() == 0
    assert client.get("/api/panel/overview").status_code == 200
    assert len(build_calls) == 2


# ---------------------------------------------------------------------------
# The 30s staleness bound survives the memo (VAL-SERVE-020)
# ---------------------------------------------------------------------------


def test_memo_entry_expires_on_its_own_data_clock() -> None:
    """A published snapshot stops serving after the TTL — no lookup can
    return data older than the window."""
    memo = CockpitDataMemo(ttl_seconds=0.05, wait_seconds=0.5)
    reservation = memo.get_or_reserve("k")
    assert isinstance(reservation, CockpitDataReservation)
    entry = memo.publish(reservation, {"evaluation": []})
    assert entry.data_as_of <= entry.expires_at

    fresh = memo.get_or_reserve("k")
    assert isinstance(fresh, CockpitDataHit)
    assert fresh.entry is entry

    time.sleep(0.06)
    expired = memo.get_or_reserve("k")
    assert isinstance(expired, CockpitDataReservation)
    memo.abandon(expired)
    assert memo.entry_count() == 0


def test_response_entry_cannot_outlive_the_memo_data_clock() -> None:
    """A memo-fed response entry expires when the memo's data clock says so,
    not a full response TTL after insertion — the pre-memo staleness bound
    (no served cockpit data older than the TTL at response time) holds."""
    cache = PanelResponseCache(ttl_seconds=30.0, max_entries=8, wait_seconds=0.5)
    now = time.monotonic()
    reservation = cache.get_or_reserve("p")
    assert isinstance(reservation, panel_cache_module.PanelCacheReservation)
    cache.store(
        reservation,
        PanelCacheEntry(
            body=b"memo-fed", content_type="text/html", etag="v1", expires_at=now + 0.05
        ),
    )
    hit = cache.get_or_reserve("p")
    assert isinstance(hit, panel_cache_module.PanelCacheHit)

    time.sleep(0.06)
    expired = cache.get_or_reserve("p")
    assert isinstance(expired, panel_cache_module.PanelCacheReservation)
    cache.abandon(expired)


def test_plain_response_entries_keep_the_insert_clock() -> None:
    """Entries without a data clock (every non-memo surface) keep the standard
    inserted-at + TTL lifetime — expires_at=None is the default."""
    cache = PanelResponseCache(ttl_seconds=30.0, max_entries=8, wait_seconds=0.5)
    reservation = cache.get_or_reserve("p")
    assert isinstance(reservation, panel_cache_module.PanelCacheReservation)
    cache.store(reservation, PanelCacheEntry(body=b"plain", content_type="text/html", etag="v1"))
    hit = cache.get_or_reserve("p")
    assert isinstance(hit, panel_cache_module.PanelCacheHit)
    assert hit.entry.expires_at is None


# ---------------------------------------------------------------------------
# Fail-closed degradation
# ---------------------------------------------------------------------------


def test_build_failure_never_poisons_the_memo(
    client: FlaskClient, monkeypatch: pytest.MonkeyPatch, build_calls: list[int]
) -> None:
    """A raising build propagates (unchanged semantics) and leaves the memo
    empty; the next request rebuilds and publishes."""
    real_build = comments_server_context.build_cockpit_rows

    def exploding_build(conn: sqlite3.Connection, repo_root: Path) -> dict[str, list[Any]]:
        build_calls.append(1)
        raise RuntimeError("builder exploded")

    patch_server_seam(monkeypatch, "build_cockpit_rows", exploding_build)
    assert client.get("/api/panel/overview").status_code == 500
    assert _memo_of(client.application).entry_count() == 0

    patch_server_seam(monkeypatch, "build_cockpit_rows", real_build)
    ok = client.get("/api/panel/overview")
    assert ok.status_code == 200
    assert len(build_calls) == 2
    assert _memo_of(client.application).entry_count() == 1


def test_busy_lookup_degrades_to_a_direct_build() -> None:
    """A caller that cannot reserve or wait (Busy) builds directly instead of
    failing — the memo can only make a response faster, never wrong."""
    memo = CockpitDataMemo(ttl_seconds=30.0, max_entries=4, wait_seconds=0.01)
    first = memo.get_or_reserve("k")
    assert isinstance(first, CockpitDataReservation)
    busy = memo.get_or_reserve("k")
    assert isinstance(busy, CockpitDataBusy)
    # The busy caller "builds directly": nothing was published for it.
    assert memo.entry_count() == 0
    memo.abandon(first)


def test_in_flight_bound_is_bounded_by_max_entries() -> None:
    """At most ``max_entries`` concurrent single-flight builds: the
    (max_entries + 1)-th distinct cold key degrades to Busy, not an
    unbounded reservation set."""
    memo = CockpitDataMemo(ttl_seconds=30.0, max_entries=2, wait_seconds=0.01)
    a = memo.get_or_reserve("a")
    b = memo.get_or_reserve("b")
    assert isinstance(a, CockpitDataReservation)
    assert isinstance(b, CockpitDataReservation)
    assert isinstance(memo.get_or_reserve("c"), CockpitDataBusy)
    memo.abandon(a)
    memo.abandon(b)


# ---------------------------------------------------------------------------
# Bound + concurrency (VAL-SERVE-023)
# ---------------------------------------------------------------------------


def test_entry_count_is_bounded_with_oldest_first_eviction() -> None:
    memo = CockpitDataMemo(ttl_seconds=30.0, max_entries=2, wait_seconds=0.5)
    for key, marker in (("k1", 1), ("k2", 2), ("k3", 3)):
        reservation = memo.get_or_reserve(key)
        assert isinstance(reservation, CockpitDataReservation)
        time.sleep(0.01)
        memo.publish(reservation, {"evaluation": [], "portfolio": []})
        del marker
    assert memo.entry_count() == 2
    # The oldest snapshot (k1) was evicted deterministically; k2/k3 stay hot.
    assert isinstance(memo.get_or_reserve("k1"), CockpitDataReservation)
    assert isinstance(memo.get_or_reserve("k2"), CockpitDataHit)
    assert isinstance(memo.get_or_reserve("k3"), CockpitDataHit)


def test_concurrent_cold_readers_coalesce_into_one_build() -> None:
    """N threads racing the same cold key: exactly one reservation, every
    waiter gets the published entry, no lock is shared across keys."""
    memo = CockpitDataMemo(ttl_seconds=30.0, max_entries=4, wait_seconds=5.0)
    reservation = memo.get_or_reserve("k")
    assert isinstance(reservation, CockpitDataReservation)

    results: list[Any] = []
    lock = threading.Lock()
    barrier = threading.Barrier(8)

    def reader() -> None:
        barrier.wait()
        outcome = memo.get_or_reserve("k")
        with lock:
            results.append(type(outcome).__name__)

    threads = [threading.Thread(target=reader) for _ in range(8)]
    for t in threads:
        t.start()
    time.sleep(0.05)  # let all readers pile onto the in-flight reservation
    memo.publish(reservation, {"evaluation": []})
    for t in threads:
        t.join()

    assert results.count("CockpitDataHit") == 8  # every waiter saw the entry
    assert memo.entry_count() == 1


def test_unrelated_keys_never_block_each_other() -> None:
    """A slow build on one key must not make a different key wait."""
    memo = CockpitDataMemo(ttl_seconds=30.0, max_entries=4, wait_seconds=5.0)
    slow = memo.get_or_reserve("slow")
    assert isinstance(slow, CockpitDataReservation)
    # A different key reserves instantly despite the outstanding slow build.
    other = memo.get_or_reserve("other")
    assert isinstance(other, CockpitDataReservation)
    memo.abandon(slow)
    memo.abandon(other)


# ---------------------------------------------------------------------------
# Preserved response contracts (VAL-SERVE-022)
# ---------------------------------------------------------------------------


def test_overview_keeps_etag_and_304_with_the_memo_warm(client: FlaskClient) -> None:
    for path in PAINT_CYCLE_PATHS:
        assert client.get(path).status_code == 200

    first = client.get("/api/panel/overview")
    etag = first.headers["ETag"]
    assert first.headers["Cache-Control"] == "no-cache"
    revalidated = client.get("/api/panel/overview", headers={"If-None-Match": etag})
    assert revalidated.status_code == 304
    assert revalidated.data == b""


def test_work_os_hydrations_keep_no_store_and_never_emit_etag(
    client: FlaskClient,
) -> None:
    for path in PAINT_CYCLE_PATHS:
        assert client.get(path).status_code == 200

    for path in ("/api/work-os/portfolio", "/api/work-os/evaluation"):
        resp = client.get(path)
        assert resp.status_code == 200, path
        cache_control = resp.headers.get("Cache-Control", "")
        assert "no-store" in cache_control, path
        assert "ETag" not in resp.headers, path
        assert resp.headers.get("X-Panel-Cache") == "hit", path
        assert resp.headers.get("Server-Timing"), path


def test_slow_build_consumes_memo_lifetime(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = [10.0]
    monkeypatch.setattr(memo_module, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    memo = CockpitDataMemo(ttl_seconds=30.0)
    reservation = memo.get_or_reserve("rows")
    assert isinstance(reservation, CockpitDataReservation)
    clock[0] = 25.0
    entry = memo.publish(reservation, {"portfolio": []})
    assert entry.data_as_of == 10.0
    assert entry.expires_at == 40.0
    clock[0] = 41.0
    assert isinstance(memo.get_or_reserve("rows"), CockpitDataReservation)
    assert memo.entry_count() == 0


def test_expired_build_releases_waiters_without_publication(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = [10.0]
    monkeypatch.setattr(memo_module, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    memo = CockpitDataMemo(ttl_seconds=30.0)
    reservation = memo.get_or_reserve("rows")
    assert isinstance(reservation, CockpitDataReservation)
    clock[0] = 41.0
    entry = memo.publish(reservation, {"portfolio": []})
    assert entry.expires_at == 40.0
    assert memo.entry_count() == 0
    assert reservation.ready.is_set()
    assert isinstance(memo.get_or_reserve("rows"), CockpitDataReservation)


def test_expired_response_is_not_published_and_releases_waiters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(panel_cache_module, "time", SimpleNamespace(monotonic=lambda: 41.0))
    cache = PanelResponseCache(ttl_seconds=30, max_entries=4)
    reservation = cache.get_or_reserve("panel")
    assert isinstance(reservation, panel_cache_module.PanelCacheReservation)
    cache.store(
        reservation,
        PanelCacheEntry(body=b"old", content_type="text/html", etag="old", expires_at=40.0),
    )
    assert reservation.ready.is_set()
    assert not getattr(cache, "_entries")
    assert isinstance(cache.get_or_reserve("panel"), panel_cache_module.PanelCacheReservation)


def test_slow_cold_build_cannot_receive_a_new_response_cache_lifetime(
    client: FlaskClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = [10.0]
    timer = SimpleNamespace(monotonic=lambda: clock[0])
    for module in (memo_module, panel_cache_module, comments_server_context):
        monkeypatch.setattr(module, "time", timer)
    calls: list[int] = []

    def slow_build(conn: sqlite3.Connection, root: Path) -> dict[str, list[Any]]:
        rows = build_cockpit_rows(conn, root)
        calls.append(1)
        clock[0] += 31.0
        return rows

    patch_server_seam(monkeypatch, "build_cockpit_rows", slow_build)
    for _ in range(2):
        assert client.get("/api/panel/overview").status_code == 200
        assert _memo_of(client.application).entry_count() == 0
    assert len(calls) == 2


def test_slow_tracker_cannot_extend_portfolio_research_freshness(
    client: FlaskClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    import comments_server_work_os_routes

    from integrations.portfolio_tracker_client import LivePortfolio

    clock = [10.0]
    timer = SimpleNamespace(monotonic=lambda: clock[0])
    for module in (panel_cache_module, comments_server_work_os_routes):
        monkeypatch.setattr(module, "time", timer, raising=False)
    calls: list[int] = []

    def slow_tracker() -> LivePortfolio:
        calls.append(1)
        clock[0] += 31.0
        return LivePortfolio(available=False, api_url="http://tracker.test")

    patch_server_seam(monkeypatch, "fetch_live_portfolio", slow_tracker)
    for _ in range(2):
        response = client.get("/api/work-os/portfolio")
        assert response.status_code == 200
        assert response.headers["X-Panel-Cache"] == "miss"
        assert _memo_of(client.application).entry_count() == 0
    assert len(calls) == 2
