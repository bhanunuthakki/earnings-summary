"""Integration tests for the dashboard endpoints on execution/comments_server.py.

Builds a tiny SQLite DB + repo tree in tmp_path, spins up the Flask test
client, and exercises `GET /`, `GET /api/dashboard`, `GET /reports/<T>`.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

# `execution/` isn't on sys.path by default; only `src/` (via pyproject pythonpath).
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "execution"))

import comments_server  # noqa: E402


def _create_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE tracked_companies (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT DEFAULT 'bhanu',
            ticker TEXT NOT NULL,
            name TEXT NOT NULL,
            list_type TEXT NOT NULL,
            added_at TIMESTAMP,
            sec_validated INTEGER DEFAULT 0,
            ir_url TEXT,
            instrument_type TEXT,
            filing_regime TEXT,
            fiscal_year_end TEXT,
            fmp_data_saved INTEGER DEFAULT 0,
            fmp_data_upto TEXT,
            archived_at TIMESTAMP,
            UNIQUE(user_id, ticker)
        );
        CREATE TABLE transcripts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            document_id INTEGER,
            ticker TEXT NOT NULL,
            call_date TIMESTAMP,
            fiscal_period_type TEXT,
            period_end TIMESTAMP,
            source_url TEXT,
            has_qa_section INTEGER
        );
        CREATE TABLE thesis_evaluations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL,
            evaluated_at TIMESTAMP NOT NULL,
            overall_status TEXT NOT NULL,
            rule_evaluations_json TEXT,
            run_id TEXT
        );
        CREATE TABLE fmp_endpoint_status (
            ticker TEXT NOT NULL,
            endpoint TEXT NOT NULL,
            period TEXT NOT NULL,
            status TEXT,
            http_code INTEGER,
            record_count INTEGER,
            earliest_date TEXT,
            latest_date TEXT,
            file_path TEXT,
            file_bytes INTEGER,
            error_msg TEXT,
            last_pulled TIMESTAMP
        );
        """
    )
    conn.commit()


@pytest.fixture
def app_repo(tmp_path: Path):
    """A repo_root with `data/portfolio.db` seeded with a minimal schema + rows."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    db_path = data_dir / "portfolio.db"
    conn = sqlite3.connect(str(db_path))
    _create_schema(conn)
    conn.execute(
        "INSERT INTO tracked_companies (ticker, name, list_type, instrument_type) "
        "VALUES ('NU', 'Nu Holdings', 'portfolio', 'equity')"
    )
    conn.execute(
        "INSERT INTO tracked_companies (ticker, name, list_type, instrument_type) "
        "VALUES ('MELI', 'MercadoLibre', 'evaluation', 'equity')"
    )
    conn.execute(
        "INSERT INTO transcripts (ticker, period_end, has_qa_section) "
        "VALUES ('NU', '2026-03-31', 1)"
    )
    conn.execute(
        "INSERT INTO thesis_evaluations (ticker, evaluated_at, overall_status, rule_evaluations_json) "
        "VALUES ('NU', '2026-05-18T10:00:00', 'intact', '[]')"
    )
    conn.execute(
        "INSERT INTO fmp_endpoint_status (ticker, endpoint, period, status, last_pulled) "
        "VALUES ('NU', 'income-statement', 'annual', 'ok', '2026-05-11T01:02:14')"
    )
    conn.commit()
    conn.close()
    return tmp_path


@pytest.fixture
def client(app_repo: Path):
    app = comments_server.create_app(app_repo)
    return app.test_client()


def test_dashboard_page_returns_shell(client):
    """GET / now serves the unified tabbed command-center shell, with the
    Overview tab server-inlined (the Research cockpit, P1.2) for first paint
    and the other tabs as lazy placeholders."""
    resp = client.get("/")
    assert resp.status_code == 200
    assert resp.mimetype == "text/html"
    body = resp.get_data(as_text=True)
    assert "<title>Portfolio · command center</title>" in body
    # Top-bar section nav + sub-tab rows (five-section IA, UX PR2) + inlined Overview.
    assert 'class="cc-topnav"' in body
    assert 'data-theme-target="home"' in body
    assert 'data-panel="overview"' in body
    # Other tabs lazy-load from /api/panel/<name>.
    assert 'data-endpoint="/api/panel/holdings"' in body
    assert 'data-endpoint="/api/panel/budget"' in body
    # Overview is inlined → the seeded tickers appear on first paint, as
    # cockpit rows (this minimal schema lacks the enrichment tables — the
    # cockpit degrades to base fields rather than 500-ing).
    assert "cockpit-section" in body
    assert "NU" in body
    assert "MELI" in body
    # The seeded thesis verdict renders as a badge.
    assert "cockpit-badge" in body


def test_dashboard_overview_excludes_action_blocks(client):
    """P1.2 moved the IR-KPI + maintenance blocks out of Overview; they serve
    from the Governance → Actions fragment instead."""
    body = client.get("/").get_data(as_text=True)
    assert 'id="refresh-ir-form"' not in body
    assert 'data-endpoint="/api/panel/actions"' in body


def test_actions_panel_fragment_serves_ir_form(client):
    resp = client.get("/api/panel/actions")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert 'id="refresh-ir-form"' in body
    assert "/actions/refresh-ir" in body
    assert "/actions/maintenance" in body


def test_dashboard_api_returns_grouped_json(client):
    resp = client.get("/api/dashboard")
    assert resp.status_code == 200
    payload = resp.get_json()
    assert set(payload.keys()) == {"portfolio", "evaluation"}
    assert [r["ticker"] for r in payload["portfolio"]] == ["NU"]
    assert [r["ticker"] for r in payload["evaluation"]] == ["MELI"]

    nu_row = payload["portfolio"][0]
    assert nu_row["fmp_last_pulled"] == "2026-05-11T01:02:14"
    assert nu_row["last_transcript"]["period_end"] == "2026-03-31"
    assert nu_row["last_transcript"]["has_qa_section"] is True
    assert nu_row["breach_status"] == "intact"
    assert nu_row["open_comments_count"] == 0


def test_dashboard_api_excludes_watchlist(app_repo, client):
    """Add a watchlist ticker post-fixture and verify it doesn't appear."""
    conn = sqlite3.connect(str(app_repo / "data" / "portfolio.db"))
    conn.execute(
        "INSERT INTO tracked_companies (ticker, name, list_type, instrument_type) "
        "VALUES ('SOFI', 'SoFi', 'watchlist', 'equity')"
    )
    conn.commit()
    conn.close()

    resp = client.get("/api/dashboard")
    payload = resp.get_json()
    all_tickers = {r["ticker"] for rows in payload.values() for r in rows}
    assert "SOFI" not in all_tickers


def test_reports_route_serves_latest_workspace_html(client, app_repo):
    research_dir = app_repo / "output" / "research" / "NU"
    research_dir.mkdir(parents=True)
    (research_dir / "2026-05-12_workspace.html").write_text("<html>old</html>")
    (research_dir / "2026-05-18_workspace.html").write_text(
        "<html>newer build</html>", encoding="utf-8"
    )

    resp = client.get("/reports/NU")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "newer build" in body


def test_reports_route_404_when_no_build(client):
    resp = client.get("/reports/NOTHING")
    assert resp.status_code == 404


def test_reports_route_uppercases_ticker(client, app_repo):
    research_dir = app_repo / "output" / "research" / "NU"
    research_dir.mkdir(parents=True)
    (research_dir / "2026-05-18_workspace.html").write_text("<html>nu</html>")

    resp = client.get("/reports/nu")  # lowercase request
    assert resp.status_code == 200


def test_healthz_still_works(client):
    """Pre-existing endpoint must not regress."""
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.get_json()["status"] == "ok"
