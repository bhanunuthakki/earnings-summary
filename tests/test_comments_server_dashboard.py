"""Integration tests for the dashboard endpoints on execution/comments_server.py.

Builds a tiny SQLite DB + repo tree in tmp_path, spins up the Flask test
client, and exercises `GET /`, `GET /api/dashboard`, `GET /reports/<T>`.
"""

from __future__ import annotations

import sqlite3
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from bs4 import BeautifulSoup
from flask.testing import FlaskClient

# `execution/` isn't on sys.path by default; only `src/` (via pyproject pythonpath).
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "execution"))

import comments_server  # noqa: E402

from integrations.portfolio_allocation import (  # noqa: E402
    PortfolioAllocationBucket,
    PortfolioAllocationBuckets,
    PortfolioAllocationProjection,
    PortfolioAllocationReconciliation,
    unavailable_portfolio_allocation,
)
from integrations.portfolio_offline_snapshot import OfflinePortfolioSnapshot  # noqa: E402
from integrations.portfolio_tracker_client import LivePortfolio, LivePosition  # noqa: E402


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


def create_dashboard_test_schema(conn: sqlite3.Connection) -> None:
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
    # +2 governed Copilot proposal routes; -3 retired compatibility routes
    # (/api/ask, /api/cockpit, /api/cron-health). +1 lazy shared-reader stylesheet.
    # +1 governed IR owner-decision action route.
    # +2 governed Work OS question draft/approval routes.
    # +2 governed README status/preview-or-apply routes.
    # +1 thesis-episode acknowledgement route.
    # +2 governed alert lifecycle routes (action and evidence).
    # +1 governed portfolio policy write proxy.
    # +1 governed sizing-intent checkpoint route.
    assert len(rules) == 167
    assert rules["readme_governance_status"] == "/api/readme-governance/status"
    assert rules["start_readme_update"] == "/actions/readme-update"
    assert rules["ir_approval_action"] == ("/api/ir-approval/candidates/<candidate_id>/<action>")
    assert rules["question_proposals_api"] == "/api/work-os/question-proposals"
    assert rules["approve_question_proposal_api"] == (
        "/api/work-os/question-proposals/<int:proposal_id>/approve"
    )
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
            "brief_library_api",
            "brief_body_api",
            "report_reader_css",
            "company_desk_api",
            "latest_dcf_for_ticker",
            "tickers_api",
            "ask_proposal_detail",
            "ask_proposal_decision",
            "alerts.digest_page",
            "alerts.feed_page",
            "alerts.alerts_page",
            "alerts.approve_or_dismiss_action",
            "alerts.dismiss_alert_api",
            "alerts.acknowledge_thesis_episode_api",
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
        "brief_library_api": "/api/work-os/briefs",
        "brief_body_api": "/api/work-os/briefs/<artifact_id>/body",
        "report_reader_css": "/api/work-os/report-reader.css",
        "company_desk_api": "/api/work-os/companies/<ticker>/desk",
        "latest_dcf_for_ticker": "/dcf/<ticker>",
        "tickers_api": "/api/tickers",
        "ask_proposal_detail": "/api/research/proposals/<int:proposal_id>",
        "ask_proposal_decision": "/api/research/proposals/<int:proposal_id>/decision",
        "alerts.digest_page": "/digest",
        "alerts.feed_page": "/feed",
        "alerts.alerts_page": "/alerts",
        "alerts.approve_or_dismiss_action": "/approve",
        "alerts.dismiss_alert_api": "/api/alerts/<int:alert_id>/dismiss",
        "alerts.acknowledge_thesis_episode_api": ("/api/thesis-episodes/<episode_id>/acknowledge"),
        "alerts.uncancel_action_api": "/api/actions/<int:action_id>/uncancel",
        "llm_budgets_api": "/api/llm-budgets",
        "set_llm_budget": "/api/llm-budgets/<purpose>",
        "dcf_globals_api": "/api/dcf-globals",
        "ticker_settings_api": "/api/ticker-settings/<ticker>",
        "notes_api": "/api/notes",
        "notes_action_api": "/api/notes/<int:note_id>/<action>",
    }


def test_replaced_compatibility_routes_are_absent(client: FlaskClient) -> None:
    assert client.post("/api/ask", json={"query": "legacy"}).status_code == 404
    assert client.get("/api/cockpit").status_code == 404
    assert client.get("/api/cron-health").status_code == 404


def test_dashboard_page_returns_shell(client: FlaskClient) -> None:
    """GET / serves the unified Work OS while legacy panel APIs stay live."""
    resp = client.get("/")
    assert resp.status_code == 200
    assert resp.mimetype == "text/html"
    body = resp.get_data(as_text=True)
    assert "<title>Counterread</title>" in body
    assert 'aria-label="Counterread home"' in body
    assert 'class="app-sidebar"' in body
    for screen_id in (
        "screen-cockpit",
        "screen-performance",
        "screen-workspace",
        "screen-brief-library",
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

    performance_risk = client.get("/api/panel/performance_risk")
    assert performance_risk.status_code == 200
    assert "Risk Explorer" in performance_risk.get_data(as_text=True)

    correlation = client.get("/api/panel/performance_risk?fragment=correlation")
    assert correlation.status_code == 200
    assert correlation.mimetype == "text/html"

    allocation = client.get("/api/panel/portfolio_allocation")
    assert allocation.status_code == 200
    allocation_body = allocation.get_data(as_text=True)
    # Work OS does not load HTMX. Performance must therefore arrive fully
    # rendered inside the on-demand allocation route rather than remaining a
    # dead revealed-trigger placeholder.
    assert 'id="csec-performance"' in allocation_body
    assert 'hx-get="/api/panel/portfolio"' not in allocation_body
    assert BeautifulSoup(allocation_body, "html.parser").select(".cc-loading") == []

    health = client.get("/api/panel/portfolio_health")
    assert health.status_code == 200
    health_body = health.get_data(as_text=True)
    assert 'data-src="/api/panel/portfolio_health?fragment=thesis"' in health_body

    health_fragment = client.get("/api/panel/portfolio_health?fragment=thesis")
    assert health_fragment.status_code == 200
    assert health_fragment.mimetype == "text/html"


def test_work_os_portfolio_api_hydrates_only_portfolio_companies(
    app_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = sqlite3.connect(app_repo / "data" / "portfolio.db")
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
        """
    )
    conn.close()
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
    monkeypatch.setattr(
        comments_server,
        "fetch_portfolio_allocation",
        _available_allocation,
    )
    local_client = comments_server.create_app(app_repo).test_client()

    response = local_client.get("/api/work-os/portfolio")

    assert response.status_code == 200
    assert response.mimetype == "application/json"
    payload = response.get_json()
    assert payload["status"] == "ok"
    assert payload["total_market_value"] == 250_000.0
    assert payload["allocation"]["state"] == "available"
    assert [row["ticker"] for row in payload["companies"]] == ["NU"]
    assert payload["companies"][0]["current_weight_pct"] == 50.0
    assert "MELI" not in response.get_data(as_text=True)


def test_work_os_portfolio_api_uses_governed_snapshot_only_after_tracker_failure(
    app_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    snapshot = OfflinePortfolioSnapshot(
        source_identity="fixture:sha256:" + "a" * 64,
        as_of="2026-08-08",
        portfolio=LivePortfolio(
            available=True,
            api_url="snapshot://governed-local",
            total_market_value=250_000.0,
            as_of="2026-08-08",
            positions=[LivePosition("NU", "Nubank", 100.0, 125_000.0, 90_000.0, 35_000.0, 50.0)],
            envelope_warnings=["portfolio_offline_snapshot"],
        ),
    )
    monkeypatch.setattr(
        comments_server,
        "fetch_live_portfolio",
        lambda: LivePortfolio(available=False, api_url="http://tracker.test", error="account 1234"),
    )
    monkeypatch.setattr(comments_server, "fetch_portfolio_allocation", _available_allocation)
    monkeypatch.setattr(
        comments_server,
        "read_configured_offline_portfolio_snapshot",
        lambda: snapshot,
    )

    payload = (
        comments_server.create_app(app_repo).test_client().get("/api/work-os/portfolio").get_json()
    )

    assert payload["tracker_state"] == "offline_snapshot"
    assert payload["tracker_detail"] == "Offline snapshot · 2026-08-08"
    assert payload["total_market_value"] == 250_000.0
    assert payload["companies"][0]["current_weight_pct"] == 50.0
    assert "account 1234" not in str(payload)


def test_work_os_portfolio_api_serializes_single_alert_identity(
    app_repo: Path,
) -> None:
    conn = sqlite3.connect(app_repo / "data" / "portfolio.db")
    conn.execute(
        "CREATE TABLE alerts ("
        "id INTEGER PRIMARY KEY, ticker TEXT NOT NULL, trigger_kind TEXT NOT NULL, "
        "fired_at TEXT NOT NULL, status TEXT NOT NULL, evidence_json TEXT NOT NULL, "
        "signature_sha TEXT NOT NULL)"
    )
    conn.execute(
        "INSERT INTO alerts "
        "(id,ticker,trigger_kind,fired_at,status,evidence_json,signature_sha) "
        "VALUES (17,'NU','earnings_tone','2026-08-08T10:00:00+00:00','pending','{}',"
        "'sig-dashboard-17')"
    )
    conn.commit()
    conn.close()

    payload = (
        comments_server.create_app(app_repo).test_client().get("/api/work-os/portfolio").get_json()
    )

    action = payload["actions"][0]
    assert action["action_id"] == "alert:17"
    assert action["action_type"] == "earnings_tone"
    assert action["lifecycle_state"] == "pending"
    assert action["source_ref"] == "alert:17"
    assert action["evidence_ref"] == "sig-dashboard-17"


def test_work_os_portfolio_api_includes_latest_persisted_earnings_readout(
    app_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        comments_server,
        "fetch_portfolio_allocation",
        lambda: unavailable_portfolio_allocation("positions_unavailable"),
    )
    conn = sqlite3.connect(app_repo / "data" / "portfolio.db")
    conn.execute(
        "UPDATE transcripts SET fiscal_period_type='Q2', period_end='2026-06-30' WHERE ticker='NU'"
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

    payload = (
        comments_server.create_app(app_repo).test_client().get("/api/work-os/portfolio").get_json()
    )

    readout = payload["companies"][0]["latest_earnings_readout"]
    assert readout["artifact_id"] == 44
    assert readout["period_label"] == "Q2 · Jun 2026"
    assert readout["route"] == "/api/peek/earnings-readout?ticker=NU&artifact_id=44"
    assert payload["earnings_readouts"] == [readout]


def test_work_os_portfolio_api_keeps_research_rows_when_allocation_is_unavailable(
    app_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        comments_server,
        "fetch_live_portfolio",
        lambda: LivePortfolio(available=True, api_url="http://tracker.test"),
    )
    monkeypatch.setattr(
        comments_server,
        "fetch_portfolio_allocation",
        lambda: unavailable_portfolio_allocation("securities_unavailable"),
    )

    response = comments_server.create_app(app_repo).test_client().get("/api/work-os/portfolio")
    payload = response.get_json()

    assert response.status_code == 200
    assert payload["status"] == "degraded"
    assert payload["allocation"]["state"] == "unavailable"
    assert payload["allocation"]["reason_codes"] == ["securities_unavailable"]
    assert "portfolio_allocation_unavailable" in payload["warnings"]
    assert [row["ticker"] for row in payload["companies"]] == ["NU"]


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


def test_ticker_page_redirects_to_ticker_aware_company_desk(client: FlaskClient) -> None:
    response = client.get("/ticker/nu")

    assert response.status_code == 302
    assert response.headers["Location"] == "/?screen=company-desk&ticker=NU"


def test_healthz_still_works(client):
    """Pre-existing endpoint must not regress."""
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.get_json()["status"] == "ok"
