"""Focused regressions for comments-server dependency and concurrency boundaries."""

from __future__ import annotations

import concurrent.futures
import sqlite3
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "execution"))
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import comments_server  # noqa: E402
from comments_server_panel_cache import (  # noqa: E402
    PanelCacheEntry,
    PanelCacheHit,
    PanelCacheReservation,
    PanelResponseCache,
)


def test_request_read_connection_uses_injected_database_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_root = tmp_path / "repo"
    default_db = repo_root / "data" / "portfolio.db"
    injected_db = tmp_path / "runtime" / "injected.db"
    default_db.parent.mkdir(parents=True)
    injected_db.parent.mkdir(parents=True)
    for path, marker in ((default_db, "shadow"), (injected_db, "injected")):
        with sqlite3.connect(path) as conn:
            conn.execute("CREATE TABLE marker (value TEXT NOT NULL)")
            conn.execute("INSERT INTO marker VALUES (?)", (marker,))

    opened: list[Path] = []

    def connect(path: Path, **_kwargs: object) -> sqlite3.Connection:
        opened.append(Path(path).resolve())
        return sqlite3.connect(path)

    def rows(conn: sqlite3.Connection, _repo_root: Path) -> dict[str, list[object]]:
        marker = str(conn.execute("SELECT value FROM marker").fetchone()[0])
        return {marker: []}

    monkeypatch.setattr(comments_server, "connect_sqlite", connect)
    monkeypatch.setattr(comments_server, "build_dashboard_rows", rows)

    client = comments_server.create_app(repo_root, db_path=injected_db).test_client()
    response = client.get("/api/dashboard")

    assert response.status_code == 200
    assert response.get_json() == {"injected": []}
    assert opened == [injected_db.resolve()]


def test_panel_cache_single_flights_same_key_without_serializing_other_keys() -> None:
    cache = PanelResponseCache(ttl_seconds=30.0, max_entries=8)
    first = cache.get_or_reserve("/api/panel/overview")
    other = cache.get_or_reserve("/api/panel/actions")
    assert isinstance(first, PanelCacheReservation)
    assert isinstance(other, PanelCacheReservation)

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        waiting = pool.submit(cache.get_or_reserve, "/api/panel/overview")
        with pytest.raises(concurrent.futures.TimeoutError):
            waiting.result(timeout=0.05)

        cache.store(
            first,
            PanelCacheEntry(
                body=b"overview",
                content_type="text/html",
                etag='"overview"',
            ),
        )
        result = waiting.result(timeout=1.0)

    assert isinstance(result, PanelCacheHit)
    assert result.entry.body == b"overview"
    cache.abandon(other)


def test_explore_to_dcf_injection_is_not_registered(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    sqlite3.connect(data_dir / "portfolio.db").close()

    client = comments_server.create_app(tmp_path).test_client()
    response = client.post(
        "/api/dcf/inject-fact",
        json={
            "ticker": "TEST",
            "token": "kpi:Operating margin",
            "field": "near_op_margin",
        },
    )

    assert response.status_code == 404
    assert not (data_dir / "dcf_assumptions" / "provenance_retry").exists()
