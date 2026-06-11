"""P5.1 Explore panel + ViewSpec routes on comments_server: the lazy panel
fragment, /api/viewspec/run + /catalog, the /api/views CRUD, and the
saved-view embed fragment.

The DB is built via alembic (stamp the 0078 head, upgrade to head → 0079
creates saved_views), mirroring test_journal_panel.py; the fact tables the
engine reads are raw DDL on top (they live far earlier in the chain than
the stamp point).
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest
from alembic.config import Config
from flask.testing import FlaskClient

from alembic import command
from pipeline.command_center_shell import render_shell
from pipeline.explore_panel import render_explore_panel, render_saved_views_list

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "execution"))

import comments_server  # noqa: E402

_PRIOR_HEAD = "0078_stance_scores"

_FACTS_DDL = """
CREATE TABLE documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    source_type TEXT NOT NULL,
    doc_type TEXT NOT NULL,
    file_path TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    fetched_at TIMESTAMP NOT NULL,
    fetch_status TEXT NOT NULL,
    raw_bytes_size INTEGER NOT NULL DEFAULT 0,
    source_url TEXT,
    source_quality_tier TEXT NOT NULL DEFAULT 'fmp_normalized'
);
CREATE TABLE financial_facts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    period_end TIMESTAMP NOT NULL,
    fiscal_period_type TEXT NOT NULL,
    line_item TEXT NOT NULL,
    value TEXT NOT NULL,
    unit TEXT NOT NULL DEFAULT 'actual',
    source_doc_id INTEGER NOT NULL,
    locator TEXT
);
CREATE TABLE tracked_companies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL DEFAULT 'bhanu',
    ticker TEXT NOT NULL,
    name TEXT NOT NULL,
    list_type TEXT NOT NULL
);
"""


def _build_db(db_path: Path) -> None:
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.stamp(cfg, _PRIOR_HEAD)
    command.upgrade(cfg, "head")
    conn = sqlite3.connect(db_path)
    conn.executescript(_FACTS_DDL)
    conn.execute(
        "INSERT INTO documents (id, ticker, source_type, doc_type, file_path, sha256,"
        " fetched_at, fetch_status, source_url) VALUES (1, 'TST', 'fmp',"
        " 'fmp_income_statement', 'f.json', 'a', '2026-01-05 10:00:00', 'ok',"
        " 'https://fmp.example/f.json')"
    )
    for pe, fpt, v in [
        ("2024-12-31 00:00:00", "Q4", 130.0),
        ("2025-03-31 00:00:00", "Q1", 120.0),
        ("2025-06-30 00:00:00", "Q2", 132.0),
        ("2025-09-30 00:00:00", "Q3", 150.0),
        ("2025-12-31 00:00:00", "Q4", 160.0),
    ]:
        conn.execute(
            "INSERT INTO financial_facts (ticker, period_end, fiscal_period_type,"
            " line_item, value, source_doc_id) VALUES ('TST', ?, ?, 'revenue', ?, 1)",
            (pe, fpt, v),
        )
    conn.execute(
        "INSERT INTO tracked_companies (user_id, ticker, name, list_type)"
        " VALUES ('bhanu', 'TST', 'Test Co', 'portfolio')"
    )
    conn.commit()
    conn.close()


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    db = tmp_path / "data" / "portfolio.db"
    db.parent.mkdir(parents=True)
    _build_db(db)
    return db


@pytest.fixture
def client(db_path: Path, tmp_path: Path) -> FlaskClient:
    assert db_path.exists()
    return comments_server.create_app(tmp_path).test_client()


_SPEC = {
    "tickers": ["TST"],
    "metrics": ["fin:revenue"],
    "transform": "level",
    "cadence": "quarterly",
    "periods": 8,
}


# ----------------------------------------------------------------------------
# panel fragment + shell registration
# ----------------------------------------------------------------------------


def test_explore_panel_renders_with_default_universe(db_path: Path) -> None:
    html_out = render_explore_panel(db_path)
    assert 'id="vx-root"' in html_out
    # The portfolio ticker pre-fills the universe and the catalog pickers.
    assert "TST" in html_out
    assert 'value="fin:revenue"' in html_out
    assert "Save view" in html_out


def test_explore_panel_is_ask_first(db_path: Path) -> None:
    """PR5: the thread + input lead the panel; the full builder survives
    untouched inside the Advanced fold (same ids, same JS contract)."""
    html_out = render_explore_panel(db_path)
    assert 'id="ask-thread"' in html_out
    assert 'id="ask-q"' in html_out
    assert "ask-chip" in html_out  # suggestion chips seed the empty thread
    assert 'class="ask-advanced"' in html_out
    for builder_id in ("vx-run", "vx-pick-fin", "vx-save", "vx-tickers"):
        assert f'id="{builder_id}"' in html_out


def test_explore_panel_route_and_views_fragment(client: FlaskClient) -> None:
    page = client.get("/api/panel/explore")
    assert page.status_code == 200
    assert b'id="vx-root"' in page.data
    frag = client.get("/api/panel/explore?fragment=views")
    assert frag.status_code == 200
    assert b"No saved views yet" in frag.data


def test_shell_carries_explore_tab() -> None:
    html_out = render_shell(overview_html="<div>x</div>")
    assert 'data-tab-target="explore"' in html_out
    assert 'data-endpoint="/api/panel/explore"' in html_out


# ----------------------------------------------------------------------------
# /api/viewspec/*
# ----------------------------------------------------------------------------


def test_run_endpoint_returns_fragment(client: FlaskClient) -> None:
    res = client.post("/api/viewspec/run", json={"spec": _SPEC})
    assert res.status_code == 200
    assert b"vx-matrix" in res.data
    assert b"Q4'25" in res.data.replace(b"&#x27;", b"'")
    # The spec object may also arrive bare (no {"spec": ...} wrapper).
    bare = client.post("/api/viewspec/run", json=_SPEC)
    assert bare.status_code == 200


def test_run_endpoint_validates(client: FlaskClient) -> None:
    res = client.post("/api/viewspec/run", json={"spec": {"tickers": [], "metrics": []}})
    assert res.status_code == 400
    err = res.get_json()["error"]
    assert "tickers" in err
    assert "metrics" in err


def test_catalog_endpoint(client: FlaskClient) -> None:
    res = client.get("/api/viewspec/catalog?tickers=TST")
    assert res.status_code == 200
    body = res.get_json()
    assert {"token": "fin:revenue", "label": "revenue", "tickers": 1} in body["fin"]
    assert body["kpi"] == []


# ----------------------------------------------------------------------------
# /api/views CRUD + embed fragment
# ----------------------------------------------------------------------------


def test_views_crud_and_embed(client: FlaskClient, db_path: Path) -> None:
    created = client.post("/api/views", json={"name": "Rev pivot", "spec": _SPEC})
    assert created.status_code == 201
    view = created.get_json()["view"]
    assert view["name"] == "Rev pivot"
    assert view["spec"]["tickers"] == ["TST"]

    listed = client.get("/api/views")
    assert [v["name"] for v in listed.get_json()["views"]] == ["Rev pivot"]

    # Upsert: same name replaces the spec, no second row.
    spec2 = dict(_SPEC, transform="yoy")
    again = client.post("/api/views", json={"name": "Rev pivot", "spec": spec2})
    assert again.status_code == 201
    assert again.get_json()["view"]["id"] == view["id"]
    assert len(client.get("/api/views").get_json()["views"]) == 1

    # The embed hook renders the stored view; ?chart=0 drops the SVG.
    frag = client.get(f"/api/views/{view['id']}/fragment")
    assert frag.status_code == 200
    assert b"vx-matrix" in frag.data
    no_chart = client.get(f"/api/views/{view['id']}/fragment?chart=0")
    assert b"<svg" not in no_chart.data

    # Saved chips render for the panel strip.
    strip = render_saved_views_list(db_path)
    assert "Rev pivot" in strip
    assert "data-spec=" in strip

    deleted = client.delete(f"/api/views/{view['id']}")
    assert deleted.status_code == 200
    assert client.delete(f"/api/views/{view['id']}").status_code == 404
    assert client.get(f"/api/views/{view['id']}/fragment").status_code == 404


def test_views_post_validates(client: FlaskClient) -> None:
    assert client.post("/api/views", json={"spec": _SPEC}).status_code == 400
    bad = client.post("/api/views", json={"name": "x", "spec": {"tickers": ["A"]}})
    assert bad.status_code == 400
    assert "metrics" in bad.get_json()["error"]


# ----------------------------------------------------------------------------
# /api/ask — one Ask-thread turn (PR5)
# ----------------------------------------------------------------------------


def test_ask_endpoint_compiles_runs_and_renders(
    client: FlaskClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """compile (mocked) → execute → fragment, one round trip; the previous
    spec rides through as refine context."""
    from viewspec import nl_compile
    from viewspec.spec import ViewSpec

    seen: dict[str, object] = {}

    def fake_compile(
        query: str,
        *,
        db_path: Path,
        context_tickers: list[str] | None = None,
        context_spec: dict[str, object] | None = None,
        run_id: str | None = None,
    ) -> nl_compile.NLCompileResult:
        seen["query"] = query
        seen["context_spec"] = context_spec
        return nl_compile.NLCompileResult(status="ok", spec=ViewSpec.from_dict(_SPEC))

    monkeypatch.setattr(nl_compile, "compile_nl_to_viewspec", fake_compile)
    res = client.post(
        "/api/ask",
        json={"query": "TST revenue", "tickers": ["TST"], "context_spec": {"tickers": ["TST"]}},
    )
    assert res.status_code == 200
    body = res.get_json()
    assert body["status"] == "ok"
    assert seen["query"] == "TST revenue"
    assert seen["context_spec"] == {"tickers": ["TST"]}
    assert "vx-matrix" in body["fragment"]
    assert body["spec"]["tickers"] == ["TST"]
    assert "series" in body["message"]


def test_ask_endpoint_degrades_on_compile_failure(
    client: FlaskClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from viewspec import nl_compile

    def fake_compile(query: str, **_kw: object) -> nl_compile.NLCompileResult:
        return nl_compile.NLCompileResult(status="error", message="no matching metric token")

    monkeypatch.setattr(nl_compile, "compile_nl_to_viewspec", fake_compile)
    res = client.post("/api/ask", json={"query": "garbage"})
    assert res.status_code == 200  # tri-state payload, never a 500
    body = res.get_json()
    assert body["status"] == "error"
    assert "no matching metric token" in body["message"]
    assert "fragment" not in body


def test_ask_endpoint_requires_query(client: FlaskClient) -> None:
    assert client.post("/api/ask", json={}).status_code == 400
