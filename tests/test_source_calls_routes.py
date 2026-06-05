"""Route tests for the Data Cache surfaces wired into the command center:
``GET /api/source-calls`` (JSON) and ``GET /api/panel/source_calls`` (HTML).

Before this, cache effectiveness was reachable only from the show_source_calls
CLI (v6 re-grade, Smart caching). These prove the live routes serve the
cross-source rollup + the per-source panel. The substrate is built via alembic;
a few source_calls rows are seeded so the rollup is non-trivial.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest
from alembic.config import Config
from flask.testing import FlaskClient

from alembic import command

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "execution"))

import comments_server  # noqa: E402

_PRIOR_HEAD = "0059_kpi_facts_restatement"


def _build_db(db_path: Path) -> None:
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.stamp(cfg, _PRIOR_HEAD)
    command.upgrade(cfg, "head")
    conn = sqlite3.connect(str(db_path))
    try:
        # source_calls is created at migration 0032 (before the _PRIOR_HEAD we
        # stamp), so the 0059->head upgrade does not create it — make it here.
        conn.execute(
            "CREATE TABLE IF NOT EXISTS source_calls ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, source_name TEXT NOT NULL, "
            "kind TEXT NOT NULL, ticker TEXT, called_at TEXT NOT NULL, latency_ms INTEGER, "
            "status TEXT NOT NULL, http_code INTEGER, record_count INTEGER, notes TEXT)"
        )
        conn.executemany(
            "INSERT INTO source_calls (source_name, kind, called_at, latency_ms, status, "
            "record_count) VALUES (?,?,?,?,?,?)",
            [
                ("fmp", "income-statement", "2026-05-10", 120, "ok", 5),
                ("fmp", "income-statement", "2026-05-11", None, "skipped", None),
                ("fmp", "income-statement", "2026-05-12", None, "skipped", None),
                ("fmp", "income-statement", "2026-05-13", None, "skipped", None),
            ],
        )
        conn.commit()
    finally:
        conn.close()


@pytest.fixture
def client(tmp_path: Path) -> FlaskClient:
    db = tmp_path / "data" / "portfolio.db"
    db.parent.mkdir(parents=True)
    _build_db(db)
    return comments_server.create_app(tmp_path).test_client()


def test_source_calls_api_returns_overview_json(client: FlaskClient) -> None:
    resp = client.get("/api/source-calls")
    assert resp.status_code == 200
    assert resp.mimetype == "application/json"
    payload = resp.get_json()
    assert payload["total_calls"] == 4
    assert payload["calls_saved"] == 3
    assert payload["cache_skip_rate"] == pytest.approx(0.75)
    assert isinstance(payload["by_source"], list)
    assert payload["by_source"][0]["source_name"] == "fmp"


def test_source_calls_panel_route_renders_html(client: FlaskClient) -> None:
    resp = client.get("/api/panel/source_calls")
    assert resp.status_code == 200
    assert resp.mimetype == "text/html"
    body = resp.data.decode()
    assert "Data fetch cache" in body
    assert "75.0%" in body  # 3 of 4 calls served from cache
