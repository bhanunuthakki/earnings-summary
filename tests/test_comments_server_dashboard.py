"""Integration tests for the dashboard endpoints on execution/comments_server.py.

Builds a tiny SQLite DB + repo tree in tmp_path, spins up the Flask test
client, and exercises `GET /`, `GET /api/dashboard`, `GET /reports/<T>`.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest
from flask.testing import FlaskClient

# `execution/` isn't on sys.path by default; only `src/` (via pyproject pythonpath).
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "execution"))

import comments_server  # noqa: E402

from integrations.portfolio_tracker_client import LivePortfolio, LivePosition  # noqa: E402


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


def test_extracted_routes_preserve_endpoint_contract(client):
    """Extracted registrars keep the monolith's public Flask names."""
    rules = {
        rule.endpoint: rule.rule
        for rule in client.application.url_map.iter_rules()
        if rule.endpoint != "static"
    }
    # +1: peek_news_events (2026-07-31 pull-only news lane)
    # +2 net (wave3b, 2026-08-02): socratic_questions (POST /api/socratic/questions)
    # removed, replaced by start_socratic_questions (POST /actions/socratic-questions)
    # + socratic_questions_result (GET /api/socratic/questions/<ticker>) — Step 1 became
    # a background job; +1 peek_weekly_packet (GET /api/peek/weekly-packet, the Sunday-
    # packet band's read-only doorway). +1 post-earnings readout generation action.
    assert len(rules) == 154
    assert {
        endpoint: rules[endpoint]
        for endpoint in (
            "source_viewer",
            "source_pdf_page_image",
            "peek_alert",
            "peek_alerts",
            "peek_ticker",
            "peek_memo",
            "peek_review",
            "peek_provenance",
            "peek_documents",
            "peek_score",
            "peek_earnings_prep",
            "peek_earnings_readout",
            "peek_news_events",
            "peek_fit",
            "peek_whatif",
            "peek_etf_workup",
            "peek_discovery_compare",
            "peek_fact_provenance",
            "ticker_api",
            "ticker_page",
            "latest_report_for_ticker",
            "latest_dcf_for_ticker",
            "tickers_api",
            "alerts.digest_page",
            "alerts.feed_page",
            "alerts.alerts_page",
            "alerts.approve_or_dismiss_action",
            "alerts.dismiss_alert_api",
            "alerts.uncancel_action_api",
            "llm_budgets_api",
            "set_llm_budget",
            "dcf_globals_api",
            "ticker_settings_api",
            "notes_api",
            "notes_action_api",
        )
    } == {
        "source_viewer": "/source/<int:doc_id>",
        "source_pdf_page_image": "/source/<int:doc_id>/page/<int:page>.png",
        "peek_alert": "/api/peek/alert/<int:alert_id>",
        "peek_alerts": "/api/peek/alerts",
        "peek_ticker": "/api/peek/ticker/<ticker>",
        "peek_memo": "/api/peek/memo/<kind>",
        "peek_review": "/api/peek/review/<ticker>",
        "peek_provenance": "/api/peek/provenance",
        "peek_documents": "/api/peek/documents",
        "peek_score": "/api/peek/score",
        "peek_earnings_prep": "/api/peek/earnings-prep",
        "peek_earnings_readout": "/api/peek/earnings-readout",
        "peek_news_events": "/api/peek/news-events",
        "peek_fit": "/api/peek/fit",
        "peek_whatif": "/api/peek/whatif",
        "peek_etf_workup": "/api/peek/etf_workup",
        "peek_discovery_compare": "/api/peek/discovery-compare",
        "peek_fact_provenance": "/api/peek/provenance/<fact_ref>",
        "ticker_api": "/api/ticker/<ticker>",
        "ticker_page": "/ticker/<ticker>",
        "latest_report_for_ticker": "/reports/<ticker>",
        "latest_dcf_for_ticker": "/dcf/<ticker>",
        "tickers_api": "/api/tickers",
        "alerts.digest_page": "/digest",
        "alerts.feed_page": "/feed",
        "alerts.alerts_page": "/alerts",
        "alerts.approve_or_dismiss_action": "/approve",
        "alerts.dismiss_alert_api": "/api/alerts/<int:alert_id>/dismiss",
        "alerts.uncancel_action_api": "/api/actions/<int:action_id>/uncancel",
        "llm_budgets_api": "/api/llm-budgets",
        "set_llm_budget": "/api/llm-budgets/<purpose>",
        "dcf_globals_api": "/api/dcf-globals",
        "ticker_settings_api": "/api/ticker-settings/<ticker>",
        "notes_api": "/api/notes",
        "notes_action_api": "/api/notes/<int:note_id>/<action>",
    }


def test_dashboard_page_returns_shell(client):
    """GET / serves the eight-screen Work OS while panel APIs stay live."""
    resp = client.get("/")
    assert resp.status_code == 200
    assert resp.mimetype == "text/html"
    body = resp.get_data(as_text=True)
    assert "<title>Equity Research OS — Harvey/Legora Masterwork Edition</title>" in body
    assert 'class="app-sidebar"' in body
    for screen_id in (
        "screen-cockpit",
        "screen-performance",
        "screen-allocation",
        "screen-workspace",
        "screen-full-brief",
        "screen-analytics-playground",
        "screen-audit-log",
        "screen-execution-queue",
    ):
        assert f'id="{screen_id}"' in body
    assert 'id="cc-palette"' not in body
    assert 'id="cc-notes-drawer"' not in body
    assert "workOsLoadScreen" in body
    # The old Overview builder remains a live drill-through endpoint.
    overview = client.get("/api/panel/overview")
    assert overview.status_code == 200
    overview_body = overview.get_data(as_text=True)
    assert "cockpit-section" in overview_body
    assert "NU" in overview_body
    assert "MELI" in overview_body
    # The seeded thesis verdict renders as a kit status pill (.k-pill).
    assert "k-pill" in overview_body


def test_work_os_portfolio_api_hydrates_only_portfolio_companies(
    app_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        comments_server,
        "fetch_live_portfolio",
        lambda: LivePortfolio(
            available=True,
            api_url="http://tracker.test",
            total_market_value=250_000.0,
            as_of="2026-08-08",
            positions=[
                LivePosition(
                    "NU",
                    "Nubank",
                    100.0,
                    125_000.0,
                    90_000.0,
                    35_000.0,
                    50.0,
                )
            ],
        ),
    )
    local_client = comments_server.create_app(app_repo).test_client()

    response = local_client.get("/api/work-os/portfolio")

    assert response.status_code == 200
    assert response.mimetype == "application/json"
    payload = response.get_json()
    assert payload["status"] == "ok"
    assert payload["total_market_value"] == 250_000.0
    assert [row["ticker"] for row in payload["companies"]] == ["NU"]
    assert payload["companies"][0]["current_weight_pct"] == 50.0
    assert "MELI" not in response.get_data(as_text=True)


def test_dashboard_overview_excludes_action_blocks(client):
    """Maintenance is absent from Cockpit chrome and remains endpoint-only."""
    body = client.get("/").get_data(as_text=True)
    assert 'id="refresh-ir-form"' not in body
    assert 'data-endpoint="/api/panel/actions"' not in body


def test_mobile_inbox_redirects_to_the_responsive_cockpit(client: FlaskClient) -> None:
    resp = client.get("/mobile/inbox")
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith("/#screen-cockpit")


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
