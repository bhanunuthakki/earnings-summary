"""Read-only Settings surface for source collection policy."""

from __future__ import annotations

import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest
from flask.testing import FlaskClient

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "execution"))

import comments_server  # noqa: E402

from pipeline.data_policy_settings_panel import (  # noqa: E402
    PolicyDisplayState,
    build_data_policy_settings_view,
    read_fmp_operational_state,
    render_data_policy_settings_panel,
)
from pipeline.work_os_shell import render_work_os_shell  # noqa: E402


def test_policy_view_is_derived_from_the_canonical_authorization_contract() -> None:
    view = build_data_policy_settings_view()

    assert [role.role.value for role in view.roles] == [
        "portfolio",
        "evaluation",
        "watchlist",
        "index_member",
    ]
    rows = {row.key: row for row in view.rows}
    assert [cell.state for cell in rows["fmp_financial_facts"].cells] == [
        PolicyDisplayState.AUTOMATIC,
        PolicyDisplayState.ON_DEMAND,
        PolicyDisplayState.NEVER,
        PolicyDisplayState.SCREENING_ONLY,
    ]
    assert [cell.state for cell in rows["sec_companyfacts"].cells] == [
        PolicyDisplayState.AUTOMATIC,
        PolicyDisplayState.ON_DEMAND,
        PolicyDisplayState.NEVER,
        PolicyDisplayState.NEVER,
    ]
    assert [cell.state for cell in rows["sec_native_filings"].cells] == [
        PolicyDisplayState.AUTOMATIC,
        PolicyDisplayState.ON_DEMAND,
        PolicyDisplayState.NEVER,
        PolicyDisplayState.NEVER,
    ]
    assert [cell.state for cell in rows["ir_documents"].cells] == [
        PolicyDisplayState.AUTOMATIC,
        PolicyDisplayState.ON_DEMAND,
        PolicyDisplayState.NEVER,
        PolicyDisplayState.NEVER,
    ]
    assert [cell.state for cell in rows["text_transcripts"].cells] == [
        PolicyDisplayState.AUTOMATIC,
        PolicyDisplayState.ON_DEMAND,
        PolicyDisplayState.NEVER,
        PolicyDisplayState.NEVER,
    ]
    assert all(cell.state is PolicyDisplayState.NEVER for cell in rows["webcasts"].cells)


def test_fmp_operational_state_reads_current_recovery_state_without_mutation(
    tmp_path: Path,
) -> None:
    db = tmp_path / "portfolio.db"
    corpus_hash = "a" * 64
    with sqlite3.connect(db) as conn:
        conn.execute(
            "CREATE TABLE provider_circuit_state (provider TEXT, state TEXT, next_probe_at TEXT, "
            "last_reason_code TEXT, last_success_at TEXT)"
        )
        conn.execute(
            "CREATE TABLE fmp_work_backlog (work_id TEXT, ticker TEXT, state TEXT, "
            "priority INTEGER, created_at TEXT)"
        )
        conn.execute(
            "CREATE TABLE fmp_work_attempts (work_id TEXT, corpus_content_sha256 TEXT, "
            "corpus_captured_at TEXT)"
        )
        conn.execute(
            "INSERT INTO provider_circuit_state VALUES "
            "('fmp', 'OPEN', '2026-08-12T18:00:00', 'rate_limited', '2026-08-10T01:00:00')"
        )
        conn.executemany(
            "INSERT INTO fmp_work_backlog VALUES (?, ?, ?, ?, ?)",
            [
                ("work-rbrk", "RBRK", "PENDING", 300, "2026-08-12T10:00:00"),
                ("work-wix", "WIX", "LEASED", 200, "2026-08-12T11:00:00"),
                ("work-done", "RBRK", "SATISFIED", 300, "2026-08-11T10:00:00"),
            ],
        )
        conn.execute(
            "INSERT INTO fmp_work_attempts VALUES (?, ?, ?)",
            ("work-done", corpus_hash, "2026-08-11T12:00:00"),
        )
    before = db.stat().st_mtime_ns

    state = build_data_policy_settings_view(db_path=db).fmp_state

    assert state.circuit_state == "OPEN"
    assert state.circuit_admission == "blocked"
    assert state.provider_availability == "degraded"
    assert state.backlog_count == 2
    assert state.pending_count == 1
    assert state.leased_count == 1
    assert state.pending_tickers == ("RBRK", "WIX")
    assert state.next_probe_at == "2026-08-12T18:00:00"
    assert state.last_reason_code == "rate_limited"
    assert state.corpus_state == "available"
    assert state.corpus_ticker_count == 1
    assert state.last_corpus_at == "2026-08-11T12:00:00"
    assert db.stat().st_mtime_ns == before
    settings_html = render_data_policy_settings_panel(db_path=db)
    assert "Degraded" in settings_html
    assert "rate_limited" in settings_html
    assert "1 companies" in settings_html
    assert corpus_hash not in settings_html


def test_closed_circuit_without_a_success_is_permitted_but_never_live(tmp_path: Path) -> None:
    db = tmp_path / "portfolio.db"
    with sqlite3.connect(db) as conn:
        conn.execute(
            "CREATE TABLE provider_circuit_state (provider TEXT, state TEXT, next_probe_at TEXT, "
            "last_reason_code TEXT, last_success_at TEXT)"
        )
        conn.execute(
            "CREATE TABLE fmp_work_backlog (work_id TEXT, ticker TEXT, state TEXT, "
            "priority INTEGER, created_at TEXT)"
        )
        conn.execute(
            "CREATE TABLE fmp_work_attempts (work_id TEXT, corpus_content_sha256 TEXT, "
            "corpus_captured_at TEXT)"
        )
        conn.execute(
            "INSERT INTO provider_circuit_state VALUES ('fmp', 'CLOSED', NULL, NULL, NULL)"
        )

    state = build_data_policy_settings_view(db_path=db).fmp_state
    html = render_data_policy_settings_panel(db_path=db)

    assert state.circuit_state == "CLOSED"
    assert state.circuit_admission == "permitted"
    assert state.provider_availability == "permitted_unverified"
    assert state.last_success_at is None
    assert "Permitted / Unverified" in html
    assert ">Live<" not in html


def test_closed_circuit_with_stale_success_is_not_available(tmp_path: Path) -> None:
    db = tmp_path / "portfolio.db"
    with sqlite3.connect(db) as conn:
        conn.execute(
            "CREATE TABLE provider_circuit_state (provider TEXT, state TEXT, next_probe_at TEXT, "
            "last_reason_code TEXT, last_success_at TEXT)"
        )
        conn.execute(
            "CREATE TABLE fmp_work_backlog (work_id TEXT, ticker TEXT, state TEXT, "
            "priority INTEGER, created_at TEXT)"
        )
        conn.execute(
            "CREATE TABLE fmp_work_attempts (work_id TEXT, corpus_content_sha256 TEXT, "
            "corpus_captured_at TEXT)"
        )
        conn.execute(
            "INSERT INTO provider_circuit_state VALUES "
            "('fmp', 'CLOSED', NULL, NULL, '2026-08-10T12:00:00+00:00')"
        )

    state = read_fmp_operational_state(
        db,
        as_of=datetime(2026, 8, 12, 12, tzinfo=UTC),
    )

    assert state.circuit_state == "CLOSED"
    assert state.circuit_admission == "permitted"
    assert state.provider_availability == "permitted_unverified"
    assert state.last_success_freshness == "stale"


def test_closed_circuit_requires_recent_success_for_available(tmp_path: Path) -> None:
    db = tmp_path / "portfolio.db"
    with sqlite3.connect(db) as conn:
        conn.execute(
            "CREATE TABLE provider_circuit_state (provider TEXT, state TEXT, next_probe_at TEXT, "
            "last_reason_code TEXT, last_success_at TEXT)"
        )
        conn.execute(
            "CREATE TABLE fmp_work_backlog (work_id TEXT, ticker TEXT, state TEXT, "
            "priority INTEGER, created_at TEXT)"
        )
        conn.execute(
            "CREATE TABLE fmp_work_attempts (work_id TEXT, corpus_content_sha256 TEXT, "
            "corpus_captured_at TEXT)"
        )
        conn.execute(
            "INSERT INTO provider_circuit_state VALUES "
            "('fmp', 'CLOSED', NULL, NULL, '2026-08-12T11:30:00+00:00')"
        )

    state = read_fmp_operational_state(
        db,
        as_of=datetime(2026, 8, 12, 12, tzinfo=UTC),
    )

    assert state.provider_availability == "available"
    assert state.last_success_freshness == "recent"


def test_panel_is_legible_read_only_and_names_owner_approved_ir_sources() -> None:
    html = render_data_policy_settings_panel()

    assert 'data-settings-panel="data-collection"' in html
    assert "Portfolio" in html
    assert "Evaluation" in html
    assert "Watchlist" in html
    assert "Index members" in html
    assert "Automatic full" in html
    assert "Metadata only" in html
    assert "Screening only" in html
    assert "SEC CompanyFacts" in html
    assert "SEC native filings" in html
    assert "IR financial documents" in html
    assert "Text transcripts" in html
    assert "Webcasts" in html
    assert "Last 5 reported quarters" in html
    assert "https://ir.rubrik.com/financials/quarterly-results/default.aspx" in html
    assert "https://investors.wix.com/financials" in html
    assert "rubrik_quarter_table" in html
    assert "wix_visible_quarter" in html
    assert "Telemetry unavailable" in html
    assert "Save" not in html
    assert "Run now" not in html
    assert "fetch('/api/ir-approval/candidates/'" in html
    assert "fetch('http" not in html
    assert "<form" not in html


def test_work_os_exposes_operations_and_related_settings_without_legacy_embedded_tabs() -> None:
    html = render_work_os_shell()

    assert 'id="screen-execution-queue"' in html
    assert '"screen-execution-queue": "/api/panel/operations"' in html
    assert "workOsLoadScreen(target, operations)" in html
    assert "window.workOsOpenRelatedView = function" in html
    assert 'id="opsTabQueue"' not in html
    assert 'id="opsTabSettings"' not in html
    assert 'data-settings-panel="data-collection"' not in html
    assert "Pipeline Health" not in html
    assert "100% Sync" not in html
    assert "78% Free" not in html
    assert "DB Idempotency" not in html


@pytest.fixture
def client(tmp_path: Path) -> FlaskClient:
    (tmp_path / "data").mkdir()
    return comments_server.create_app(tmp_path).test_client()


def test_panel_route_is_read_only(client: FlaskClient) -> None:
    response = client.get("/api/panel/data_policy_settings")

    assert response.status_code == 200
    assert response.mimetype == "text/html"
    assert 'data-settings-panel="data-collection"' in response.get_data(as_text=True)
    assert client.post("/api/panel/data_policy_settings").status_code == 405


def test_sec_coverage_state_and_rendering(tmp_path: Path) -> None:
    db = tmp_path / "portfolio.db"
    with sqlite3.connect(db) as conn:
        conn.execute(
            "CREATE TABLE tracked_companies (ticker TEXT, name TEXT, list_type TEXT, "
            "sec_validated BOOLEAN, filing_regime TEXT, archived_at TIMESTAMP)"
        )
        conn.executemany(
            "INSERT INTO tracked_companies VALUES (?, ?, ?, ?, ?, ?)",
            [
                ("RBRK", "Rubrik", "portfolio", 1, "10-K", None),
                ("WIX", "Wix.com", "portfolio", 0, "20-F", None),
                ("ABNB", "Airbnb", "evaluation", 1, "10-K", None),
                ("SNOW", "Snowflake", "evaluation", 0, "10-K", None),
                ("AMAT", "Applied Materials", "watchlist", 0, "10-K", None),
                ("OLD", "Archived Co", "portfolio", 1, "10-K", "2026-08-01 00:00:00"),
            ],
        )

    view = build_data_policy_settings_view(db_path=db)
    cov = view.sec_coverage

    assert cov.total_tracked == 5
    assert cov.portfolio_count == 2
    assert cov.evaluation_count == 2
    assert cov.watchlist_count == 1
    assert cov.validated_count == 2
    assert cov.gap_count == 2

    html = render_data_policy_settings_panel(db_path=db)
    assert "SEC collection priority &amp; coverage gaps" in html
    assert "Portfolio issuers" in html
    assert "SEC Profile Gaps" in html
    assert "Rubrik" in html
    assert "Wix.com" in html
    assert "Coverage gap" in html
    assert "Automatic full" in html
    assert "Archived Co" not in html


def test_fmp_recovery_event_receipts_in_panel(tmp_path: Path) -> None:
    db = tmp_path / "portfolio.db"
    with sqlite3.connect(db) as conn:
        conn.execute(
            "CREATE TABLE provider_circuit_state (provider TEXT, state TEXT, next_probe_at TEXT, "
            "last_reason_code TEXT, last_success_at TEXT)"
        )
        conn.execute(
            "CREATE TABLE fmp_work_backlog (work_id TEXT, ticker TEXT, state TEXT, "
            "priority INTEGER, created_at TEXT)"
        )
        conn.execute(
            "CREATE TABLE fmp_work_attempts (work_id TEXT, corpus_content_sha256 TEXT, "
            "corpus_captured_at TEXT)"
        )
        conn.execute(
            "CREATE TABLE fmp_recovery_events (event_id TEXT, provider TEXT, work_id TEXT, "
            "attempt_id TEXT, event_type TEXT, reason_code TEXT, state_from TEXT, state_to TEXT, "
            "circuit_revision INTEGER, recorded_at TEXT)"
        )
        conn.execute(
            "INSERT INTO provider_circuit_state VALUES ('fmp', 'HALF_OPEN', '2026-08-14T20:00:00', "
            "'rate_limit_probe', '2026-08-14T10:00:00')"
        )
        conn.execute(
            "INSERT INTO fmp_recovery_events VALUES "
            "('ev-1', 'fmp', 'w-1', 'att-1', 'circuit_half_open', 'probe_window_reached', 'OPEN', 'HALF_OPEN', 5, '2026-08-14T19:55:00')"
        )

    view = build_data_policy_settings_view(db_path=db)
    assert len(view.fmp_state.recent_events) == 1
    assert view.fmp_state.recent_events[0].event_type == "circuit_half_open"

    html = render_data_policy_settings_panel(db_path=db)
    assert "Recent recovery receipts &amp; transitions" in html
    assert "circuit_half_open" in html
    assert "probe_window_reached" in html
