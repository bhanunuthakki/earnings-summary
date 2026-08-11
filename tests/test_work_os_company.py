from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from flask.testing import FlaskClient

from pipeline.work_os_company import build_company_desk
from tests.test_comments_server_dashboard import comments_server, create_dashboard_test_schema


@pytest.fixture(name="work_os_app_repo")
def _work_os_app_repo(tmp_path: Path) -> Path:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    conn = sqlite3.connect(data_dir / "portfolio.db")
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
        "INSERT INTO tracked_companies (ticker, name, list_type, instrument_type) "
        "VALUES ('NVO', 'Novo Nordisk', 'portfolio', 'equity')"
    )
    conn.commit()
    conn.close()
    return tmp_path


@pytest.fixture(name="work_os_client")
def _work_os_client(work_os_app_repo: Path) -> FlaskClient:
    return comments_server.create_app(work_os_app_repo).test_client()


def _seed_company_state(repo_root: Path) -> None:
    conn = sqlite3.connect(repo_root / "data" / "portfolio.db")
    conn.executescript(
        """
        CREATE TABLE decisions (
            id INTEGER PRIMARY KEY,
            ticker TEXT NOT NULL,
            recommendation_kind TEXT NOT NULL,
            recommendation_value REAL,
            conviction TEXT,
            made_at TEXT NOT NULL,
            outcome_label TEXT,
            decision_conditions TEXT,
            outcome_at TEXT,
            source_lens TEXT,
            decided_by TEXT
        );
        CREATE TABLE analyst_notes (
            id INTEGER PRIMARY KEY,
            user_id TEXT NOT NULL,
            ticker TEXT,
            kind TEXT NOT NULL,
            status TEXT NOT NULL,
            body TEXT NOT NULL,
            anchor_type TEXT,
            anchor_key TEXT,
            source TEXT NOT NULL,
            source_ref TEXT,
            supersedes_id INTEGER,
            resolution_note TEXT,
            context_json TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            resolved_at TEXT,
            decision_id INTEGER,
            position_entry_id INTEGER,
            link_auto_resolve INTEGER DEFAULT 0,
            fact_ref TEXT
        );
        """
    )
    conn.execute(
        "INSERT INTO decisions VALUES "
        "(7, 'NU', 'hold', 0.125, 'high', '2026-08-07T12:00:00Z', NULL, ?, NULL, "
        "'senior_partner', 'owner')",
        (
            '[{"metric":"NPL 90+","metric_source":"financial",'
            '"op":"gt","threshold":5.6,"unit":"percent","for_periods":2,"note":"Trim trigger"}]',
        ),
    )
    conn.execute(
        "INSERT INTO decisions VALUES "
        "(8, 'NU', 'trim', 0.1, 'medium', '2026-08-08T12:00:00Z', NULL, '[]', NULL, "
        "'position_review', 'advisor')"
    )
    conn.execute(
        "INSERT INTO analyst_notes VALUES "
        "(11, 'bhanu', 'NU', 'question', 'open', 'Is Mexico deposit growth rate-led?', "
        "'ticker', 'NU', 'manual', NULL, NULL, NULL, NULL, "
        "'2026-08-08T10:00:00Z', '2026-08-08T10:00:00Z', NULL, 7, NULL, 0, NULL)"
    )
    conn.commit()
    conn.close()


def test_company_desk_is_a_narrow_governed_read_model(work_os_app_repo: Path) -> None:
    _seed_company_state(work_os_app_repo)
    conn = sqlite3.connect(work_os_app_repo / "data" / "portfolio.db")
    conn.row_factory = sqlite3.Row
    try:
        desk = build_company_desk(work_os_app_repo, conn, "nu")
    finally:
        conn.close()

    assert desk.schema_version == "company_desk.v1"
    assert desk.company.ticker == "NU"
    assert desk.company.coverage_role == "portfolio"
    assert desk.current_decision is not None
    assert desk.current_decision.decision_id == 7
    assert desk.current_decision.owner_state == "hold"
    assert desk.current_decision.model_recommendation == "trim"
    assert desk.current_decision.revision == "2026-08-07T12:00:00Z"
    assert desk.conditions[0].stable_id == "decision:7:condition:0"
    assert desk.open_questions[0].stable_id == "analyst_note:11"
    assert desk.open_questions[0].revision == "2026-08-08T10:00:00Z"
    assert desk.latest_brief is None
    assert "position_snapshot_unavailable" in desk.warnings


def test_company_desk_api_is_read_only_and_no_store(
    work_os_client: FlaskClient, work_os_app_repo: Path
) -> None:
    _seed_company_state(work_os_app_repo)
    response = work_os_client.get("/api/work-os/companies/nu/desk")

    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    payload = response.get_json()
    assert payload["company"]["ticker"] == "NU"
    assert payload["current_decision"]["owner_state"] == "hold"
    assert payload["conditions"][0]["evidence_ref"] == "financial"


def test_company_desk_degrades_when_optional_tables_are_absent(
    work_os_app_repo: Path,
) -> None:
    conn = sqlite3.connect(work_os_app_repo / "data" / "portfolio.db")
    conn.row_factory = sqlite3.Row
    try:
        desk = build_company_desk(work_os_app_repo, conn, "MELI")
    finally:
        conn.close()

    assert desk.company.coverage_role == "evaluation"
    assert desk.current_decision is None
    assert desk.conditions == []
    assert desk.open_questions == []
    assert desk.status == "degraded"


def test_company_desk_exposes_latest_governed_dcf_snapshot(
    work_os_app_repo: Path,
) -> None:
    conn = sqlite3.connect(work_os_app_repo / "data" / "portfolio.db")
    conn.executescript(
        """
        CREATE TABLE dcf_runs (
            id INTEGER PRIMARY KEY, ticker TEXT NOT NULL, created_at TEXT NOT NULL,
            valuation_date TEXT, npv_per_share REAL, live_price REAL, live_price_at TEXT,
            currency TEXT, is_latest INTEGER, segment_name TEXT
        );
        """
    )
    conn.executemany(
        "INSERT INTO dcf_runs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (
                1,
                "NU",
                "2026-08-01T00:00:00Z",
                "2026-07-31",
                22.10,
                13.84,
                "2026-08-10T00:25:11Z",
                "USD",
                1,
                None,
            ),
            (
                2,
                "NVO",
                "2026-08-01T00:00:00Z",
                "2026-07-31",
                72.69,
                47.26,
                "2026-08-10T00:25:12Z",
                "USD",
                1,
                None,
            ),
            (
                3,
                "MELI",
                "2026-08-01T00:00:00Z",
                "2026-07-31",
                2_400.0,
                2_100.0,
                "2026-08-10T00:25:13Z",
                "USD",
                1,
                None,
            ),
            (
                4,
                "NVO",
                "2026-08-02T00:00:00Z",
                "2026-08-01",
                9_999.0,
                9_999.0,
                "2026-08-10T00:25:14Z",
                "USD",
                0,
                None,
            ),
            (
                5,
                "NU",
                "2026-08-03T00:00:00Z",
                "2026-08-02",
                9_999.0,
                9_999.0,
                "2026-08-10T00:25:15Z",
                "USD",
                1,
                "segment-a",
            ),
        ],
    )
    conn.commit()
    conn.row_factory = sqlite3.Row
    try:
        for ticker, price, fair_value in (
            ("NU", 13.84, 22.10),
            ("NVO", 47.26, 72.69),
            ("MELI", 2_100.0, 2_400.0),
        ):
            desk = build_company_desk(work_os_app_repo, conn, ticker)
            assert desk.position.price == price
            assert desk.position.fair_value == fair_value
            assert desk.position.currency == "USD"
            assert desk.position.price_as_of is not None
            assert desk.position.fair_value_as_of == "2026-07-31"
            assert desk.position.source == "latest_governed_dcf_run"
            assert "position_snapshot_unavailable" not in desk.warnings
    finally:
        conn.close()
