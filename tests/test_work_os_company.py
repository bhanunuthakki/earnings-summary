from __future__ import annotations

import sqlite3
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from flask.testing import FlaskClient

from integrations.portfolio_tracker_client import LivePortfolio, LivePosition
from pipeline.work_os_company import build_company_desk
from report.models import (
    BreakRuleEvaluation,
    BreakRuleObservation,
    KpiLedgerRow,
    SectionStatus,
    ThesisSection,
)
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
def _work_os_client(work_os_app_repo: Path, monkeypatch: pytest.MonkeyPatch) -> FlaskClient:
    monkeypatch.setattr(
        comments_server,
        "fetch_live_portfolio",
        lambda: LivePortfolio(
            available=False,
            api_url="http://tracker.test",
            error="test fixture unavailable",
        ),
    )
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


def _seed_earnings_store(repo_root: Path) -> None:
    conn = sqlite3.connect(repo_root / "data" / "portfolio.db")
    conn.executescript(
        """
        CREATE TABLE expected_earnings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL,
            expected_date TEXT NOT NULL
        );
        CREATE TABLE llm_artifacts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT,
            scope TEXT NOT NULL DEFAULT 'ticker',
            purpose TEXT NOT NULL,
            fiscal_period TEXT,
            content_md TEXT,
            generated_at TEXT,
            superseded_by_id INTEGER
        );
        CREATE TABLE earnings_surprises (
            ticker TEXT NOT NULL,
            release_date TEXT
        );
        """
    )
    conn.commit()
    conn.close()


def _insert_artifact(repo_root: Path, *, purpose: str, fiscal_period: str) -> None:
    conn = sqlite3.connect(repo_root / "data" / "portfolio.db")
    conn.execute(
        "INSERT INTO llm_artifacts "
        "(ticker, purpose, fiscal_period, content_md, generated_at) "
        "VALUES ('NU', ?, ?, 'persisted', '2026-08-14T11:44:51Z')",
        (purpose, fiscal_period),
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
    assert desk.current_decision.relationship == "conflict"
    assert desk.current_decision.freshness == "current"
    assert desk.current_decision.owner is not None
    assert desk.current_decision.owner.decision_id == 7
    assert desk.current_decision.owner.value == "hold"
    assert desk.current_decision.owner.revision == "2026-08-07T12:00:00Z"
    assert desk.current_decision.model is not None
    assert desk.current_decision.model.decision_id == 8
    assert desk.current_decision.model.value == "trim"
    assert desk.current_decision.model.revision == "2026-08-08T12:00:00Z"
    assert desk.conditions[0].stable_id == "decision:7:condition:0"
    assert desk.open_questions[0].stable_id == "analyst_note:11"
    assert desk.open_questions[0].revision == "2026-08-08T10:00:00Z"
    assert desk.open_questions[0].origin == "owner"
    assert desk.open_questions[0].approval == "owner-authored"
    assert desk.question_store_status == "ok"
    assert desk.latest_brief is None
    assert "position_snapshot_unavailable" in desk.warnings


def test_company_desk_projects_only_fresh_canonical_thesis_evidence(
    work_os_app_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The Desk must retain the report builder's PK-backed KPI evidence handle."""

    from pipeline import work_os_company

    canonical_thesis = ThesisSection(
        status=SectionStatus.OK,
        thesis_full="The thesis is grounded in the reported KPI series.",
        break_conditions=["Revenue declines for two quarters."],
        overall_breach_status="ok",
        last_evaluated_at=datetime(2026, 8, 20, tzinfo=UTC),
        break_rule_evaluations=[
            BreakRuleEvaluation(
                rule_id="revenue_floor",
                kpi_name="Revenue",
                comparator="lt",
                threshold=100.0,
                consecutive_periods=2,
                status="ok",
                detail="Above the revenue floor.",
                narrative="Revenue remains above the hard floor.",
                observations=[
                    BreakRuleObservation(period_end="2026-06-30", value=123.4, unit="USD M")
                ],
            )
        ],
        kpi_ledger=[
            KpiLedgerRow(
                name="Revenue",
                tier="tier_1",
                unit="USD M",
                kpi_definition_id=42,
                history=[("2026-06-30", 123.4)],
                current_status="green",
            )
        ],
    )

    def build_canonical_thesis(
        _ticker: str, _repo_root: Path, *, conn: sqlite3.Connection | None = None
    ) -> ThesisSection:
        del conn
        return canonical_thesis

    monkeypatch.setattr(work_os_company.thesis_section, "build", build_canonical_thesis)
    conn = sqlite3.connect(work_os_app_repo / "data" / "portfolio.db")
    conn.row_factory = sqlite3.Row
    try:
        desk = build_company_desk(
            work_os_app_repo,
            conn,
            "NU",
            generated_at=datetime(2026, 8, 23, tzinfo=UTC),
        )
    finally:
        conn.close()

    assert desk.thesis_risk.status == "available"
    assert desk.thesis_risk.overall_breach_status == "ok"
    assert desk.thesis_risk.break_rules[0].rule_id == "revenue_floor"
    assert desk.thesis_risk.break_rules[0].distance_to_threshold == pytest.approx(23.4)
    assert desk.thesis_risk.break_rules[0].provenance_ref == "thesis_evaluation:NU:revenue_floor"
    assert desk.kpi_summary.status == "available"
    assert desk.kpi_summary.items[0].evidence_ref == "kpi:NU:42"
    assert desk.kpi_summary.items[0].latest_value == pytest.approx(123.4)
    assert desk.kpi_summary.items[0].state == "tracked"


def test_company_desk_withholds_stale_or_noncanonical_thesis_facts(
    work_os_app_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from pipeline import work_os_company

    stale_thesis = ThesisSection(
        status=SectionStatus.OK,
        thesis_full="Do not surface this stale thesis as current.",
        overall_breach_status="ok",
        last_evaluated_at=datetime(2025, 1, 1, tzinfo=UTC),
        kpi_ledger=[
            KpiLedgerRow(
                name="Revenue",
                tier="tier_1",
                kpi_definition_id=42,
                history=[("2025-01-01", 123.4)],
                current_status="green",
            )
        ],
    )

    def build_stale_thesis(
        _ticker: str, _repo_root: Path, *, conn: sqlite3.Connection | None = None
    ) -> ThesisSection:
        del conn
        return stale_thesis

    monkeypatch.setattr(work_os_company.thesis_section, "build", build_stale_thesis)
    conn = sqlite3.connect(work_os_app_repo / "data" / "portfolio.db")
    conn.row_factory = sqlite3.Row
    try:
        desk = build_company_desk(
            work_os_app_repo,
            conn,
            "NU",
            generated_at=datetime(2026, 8, 23, tzinfo=UTC),
        )
    finally:
        conn.close()

    assert desk.thesis_risk.status == "unavailable"
    assert desk.thesis_risk.unavailable_reason == "stale"
    assert desk.thesis_risk.thesis is None
    assert desk.kpi_summary.status == "available"
    assert desk.kpi_summary.items[0].state == "stale"


def test_company_desk_projects_partial_kpi_states_and_rejects_future_facts(
    work_os_app_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A partial ledger stays useful without treating future facts as current."""

    from pipeline import work_os_company

    partial_thesis = ThesisSection(
        status=SectionStatus.OK,
        thesis_full="Project only evaluated facts.",
        overall_breach_status="warn",
        last_evaluated_at=datetime(2026, 8, 20, tzinfo=UTC),
        break_rule_evaluations=[
            BreakRuleEvaluation(
                rule_id="npl_floor",
                kpi_name="NPL 90+",
                comparator="gt",
                threshold=4.0,
                consecutive_periods=1,
                status="warn",
                detail="Approaching the threshold.",
                narrative="Asset quality needs monitoring.",
                observations=[
                    BreakRuleObservation(period_end="2026-06-30", value=3.7, unit="%")
                ],
            ),
            BreakRuleEvaluation(
                rule_id="future_rule",
                kpi_name="Future KPI",
                comparator="lt",
                threshold=0.0,
                consecutive_periods=1,
                status="unresolved",
                detail="No evaluated observation.",
                narrative="Awaiting data.",
            ),
        ],
        kpi_ledger=[
            KpiLedgerRow(
                name="NPL 90+",
                tier="tier_1",
                kpi_definition_id=11,
                history=[("2026-03-31", 3.2), ("2026-06-30", 3.7)],
                current_status="yellow",
            ),
            KpiLedgerRow(
                name="Unreported KPI",
                tier="tier_1",
                kpi_definition_id=12,
                current_status="unknown",
            ),
            KpiLedgerRow(
                name="Old KPI",
                tier="tier_1",
                kpi_definition_id=13,
                history=[("2025-01-01", 1.0)],
                current_status="green",
            ),
            KpiLedgerRow(
                name="Future KPI",
                tier="tier_1",
                kpi_definition_id=14,
                history=[("2026-10-01", 2.0)],
                current_status="green",
            ),
            KpiLedgerRow(
                name="Broken KPI",
                tier="tier_1",
                kpi_definition_id=15,
                history=[("2026-06-30", 9.0)],
                current_status="red",
            ),
        ],
    )

    def build_partial_thesis(
        _ticker: str, _repo_root: Path, *, conn: sqlite3.Connection | None = None
    ) -> ThesisSection:
        del conn
        return partial_thesis

    monkeypatch.setattr(work_os_company.thesis_section, "build", build_partial_thesis)
    conn = sqlite3.connect(work_os_app_repo / "data" / "portfolio.db")
    conn.row_factory = sqlite3.Row
    try:
        desk = build_company_desk(
            work_os_app_repo,
            conn,
            "MELI",
            generated_at=datetime(2026, 8, 23, tzinfo=UTC),
        )
    finally:
        conn.close()

    assert desk.thesis_risk.status == "available"
    assert desk.thesis_risk.break_rules[0].status == "warn"
    assert desk.thesis_risk.break_rules[0].distance_to_threshold == pytest.approx(-0.3)
    assert desk.thesis_risk.break_rules[1].status == "unresolved"
    assert desk.thesis_risk.break_rules[1].latest_period is None
    assert [item.state for item in desk.kpi_summary.items] == [
        "improving",
        "awaiting_data",
        "stale",
        "stale",
        "material_exception",
    ]
    assert desk.kpi_summary.status == "available"
    assert not work_os_company._is_fresh_thesis_timestamp(
        datetime(2026, 8, 24, tzinfo=UTC), as_of=datetime(2026, 8, 23, tzinfo=UTC)
    )


@pytest.mark.parametrize("ticker", ["NU", "NVO", "MELI", "SPARSE"])
def test_company_desk_thesis_projection_is_ticker_scoped_for_portfolio_and_sparse_names(
    work_os_app_repo: Path, monkeypatch: pytest.MonkeyPatch, ticker: str
) -> None:
    """Portfolio, evaluation, and sparse names share the same fail-closed contract."""

    from pipeline import work_os_company

    thesis = ThesisSection(
        status=SectionStatus.OK,
        thesis_full="A canonical thesis.",
        overall_breach_status="ok",
        last_evaluated_at=datetime(2026, 8, 20, tzinfo=UTC),
        break_rule_evaluations=[
            BreakRuleEvaluation(
                rule_id="canonical_floor",
                kpi_name="Revenue",
                comparator="lt",
                threshold=1.0,
                consecutive_periods=1,
                status="ok",
                detail="Evaluated.",
                narrative="Canonical rule.",
                observations=[BreakRuleObservation(period_end="2026-06-30", value=2.0, unit="M")],
            )
        ],
        kpi_ledger=[
            KpiLedgerRow(
                name="Revenue",
                tier="tier_1",
                kpi_definition_id=1,
                history=[("2026-06-30", 2.0)],
                current_status="green",
            )
        ],
    )

    monkeypatch.setattr(work_os_company.thesis_section, "build", lambda *_args, **_kwargs: thesis)
    conn = sqlite3.connect(work_os_app_repo / "data" / "portfolio.db")
    conn.row_factory = sqlite3.Row
    try:
        if ticker == "SPARSE":
            conn.execute(
                "INSERT INTO tracked_companies (ticker, name, list_type, instrument_type) "
                "VALUES ('SPARSE', 'Sparse Company', 'evaluation', 'equity')"
            )
            conn.commit()
        desk = build_company_desk(
            work_os_app_repo,
            conn,
            ticker,
            generated_at=datetime(2026, 8, 23, tzinfo=UTC),
        )
    finally:
        conn.close()

    assert desk.thesis_risk.status == "available"
    assert desk.thesis_risk.break_rules[0].provenance_ref == f"thesis_evaluation:{ticker}:canonical_floor"
    assert desk.kpi_summary.items[0].evidence_ref == f"kpi:{ticker}:1"


def test_company_desk_projects_live_tracker_position_without_losing_dcf(
    work_os_app_repo: Path,
) -> None:
    conn = sqlite3.connect(work_os_app_repo / "data" / "portfolio.db")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE dcf_runs (
            id INTEGER PRIMARY KEY,
            ticker TEXT NOT NULL,
            created_at TEXT NOT NULL,
            valuation_date TEXT,
            npv_per_share REAL,
            live_price REAL,
            currency TEXT,
            live_price_at TEXT
        );
        INSERT INTO dcf_runs VALUES (
            1, 'NU', '2026-08-08T10:00:00Z', '2026-08-08',
            18.50, 14.25, 'USD', '2026-08-08T09:30:00Z'
        );
        """
    )
    live = LivePortfolio(
        available=True,
        api_url="http://tracker.test",
        total_market_value=100_000.0,
        positions=[LivePosition("NU", "Nu Holdings", 10.0, 12_345.0, 10_000.0, 2_345.0, 12.345)],
    )
    try:
        desk = build_company_desk(work_os_app_repo, conn, "nu", live=live)
    finally:
        conn.close()

    assert desk.position.weight_pct == pytest.approx(12.345)
    assert desk.position.market_value == pytest.approx(12_345.0)
    assert desk.position.position_source == "portfolio_tracker_api"
    assert desk.position.position_state == "held"
    assert desk.position.price == pytest.approx(14.25)
    assert desk.position.fair_value == pytest.approx(18.50)
    assert desk.position.source == "latest_governed_dcf_run"


def test_company_desk_distinguishes_not_held_from_tracker_unavailable(
    work_os_app_repo: Path,
) -> None:
    conn = sqlite3.connect(work_os_app_repo / "data" / "portfolio.db")
    conn.row_factory = sqlite3.Row
    try:
        not_held = build_company_desk(
            work_os_app_repo,
            conn,
            "NU",
            live=LivePortfolio(
                available=True,
                api_url="http://tracker.test",
                as_of="2026-08-08",
            ),
        )
        unavailable = build_company_desk(
            work_os_app_repo,
            conn,
            "NU",
            live=LivePortfolio(
                available=False,
                api_url="http://tracker.test",
                error="offline",
            ),
        )
    finally:
        conn.close()

    assert not_held.position.position_state == "not_held"
    assert not_held.position.position_source == "portfolio_tracker_api"
    assert not_held.position.position_as_of == "2026-08-08"
    assert not_held.position.weight_pct is None
    assert "portfolio_tracker_unavailable" not in not_held.warnings
    assert unavailable.position.position_state == "unavailable"
    assert unavailable.position.position_source is None
    assert "portfolio_tracker_unavailable" in unavailable.warnings


def test_company_desk_api_is_read_only_and_no_store(
    work_os_client: FlaskClient, work_os_app_repo: Path
) -> None:
    _seed_company_state(work_os_app_repo)
    response = work_os_client.get("/api/work-os/companies/nu/desk")

    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    payload = response.get_json()
    assert payload["company"]["ticker"] == "NU"
    assert payload["current_decision"]["owner"]["value"] == "hold"
    assert payload["current_decision"]["model"]["value"] == "trim"
    assert payload["current_decision"]["relationship"] == "conflict"
    assert payload["conditions"][0]["status"] == "PENDING DATA"
    assert payload["conditions"][0]["evidence_ref"] == "financial_facts:NPL 90+:unobserved"
    assert payload["thesis_risk"]["status"] == "unavailable"
    assert payload["kpi_summary"] == {
        "status": "unavailable",
        "items": [],
        "unavailable_reason": "missing",
    }


def test_company_desk_api_fetches_one_canonical_tracker_snapshot(
    work_os_app_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = 0

    def fetch() -> LivePortfolio:
        nonlocal calls
        calls += 1
        return LivePortfolio(
            available=True,
            api_url="http://tracker.test",
            total_market_value=100_000.0,
            positions=[
                LivePosition(
                    "NU",
                    "Nu Holdings",
                    10.0,
                    12_345.0,
                    10_000.0,
                    2_345.0,
                    12.345,
                )
            ],
        )

    monkeypatch.setattr(comments_server, "fetch_live_portfolio", fetch)
    client = comments_server.create_app(work_os_app_repo).test_client()

    response = client.get("/api/work-os/companies/nu/desk")

    assert response.status_code == 200
    payload = response.get_json()
    assert calls == 1
    assert payload["position"]["weight_pct"] == pytest.approx(12.345)
    assert payload["position"]["market_value"] == pytest.approx(12_345.0)
    assert payload["position"]["position_source"] == "portfolio_tracker_api"


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
    assert desk.current_decision.relationship == "unavailable"
    assert desk.current_decision.freshness == "unavailable"
    assert desk.current_decision.owner is None
    assert desk.current_decision.model is None
    assert desk.conditions == []
    assert desk.open_questions == []
    assert desk.question_store_status == "unavailable"
    assert desk.status == "degraded"


def test_company_desk_marks_old_decision_state_stale(work_os_app_repo: Path) -> None:
    _seed_company_state(work_os_app_repo)
    conn = sqlite3.connect(work_os_app_repo / "data" / "portfolio.db")
    conn.row_factory = sqlite3.Row
    try:
        desk = build_company_desk(
            work_os_app_repo,
            conn,
            "NU",
            generated_at=datetime(2027, 1, 15, tzinfo=UTC),
        )
    finally:
        conn.close()

    assert desk.current_decision.freshness == "stale"
    assert desk.current_decision.stale_after_days == 90


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


def test_company_desk_exposes_real_pre_earnings_artifact_route(work_os_app_repo: Path) -> None:
    _seed_earnings_store(work_os_app_repo)
    conn = sqlite3.connect(work_os_app_repo / "data" / "portfolio.db")
    conn.execute(
        "INSERT INTO expected_earnings (ticker, expected_date) VALUES ('NU', '2026-08-11')"
    )
    conn.commit()
    conn.close()
    _insert_artifact(work_os_app_repo, purpose="pre_earnings_brief", fiscal_period="2026-08-11")
    conn = sqlite3.connect(work_os_app_repo / "data" / "portfolio.db")
    conn.row_factory = sqlite3.Row
    try:
        desk = build_company_desk(work_os_app_repo, conn, "NU", today=date(2026, 8, 4))
    finally:
        conn.close()
    assert desk.earnings_doorway.status == "available"
    assert desk.earnings_doorway.phase == "pre"
    assert desk.earnings_doorway.label == "Pre-earnings brief →"
    assert desk.earnings_doorway.route == "/api/peek/earnings-prep?ticker=NU"


def test_company_desk_t0_switches_only_when_matching_post_artifact_exists(
    work_os_app_repo: Path,
) -> None:
    _seed_earnings_store(work_os_app_repo)
    conn = sqlite3.connect(work_os_app_repo / "data" / "portfolio.db")
    conn.execute(
        "INSERT INTO expected_earnings (ticker, expected_date) VALUES ('NU', '2026-08-11')"
    )
    conn.execute(
        "INSERT INTO earnings_surprises (ticker, release_date) VALUES ('NU', '2026-08-11')"
    )
    conn.execute(
        "INSERT INTO transcripts (document_id, ticker, call_date, fiscal_period_type, period_end) VALUES (77, 'NU', '2026-08-11', 'Q2', '2026-06-30')"
    )
    conn.commit()
    conn.close()
    _insert_artifact(work_os_app_repo, purpose="pre_earnings_brief", fiscal_period="2026-08-11")
    conn = sqlite3.connect(work_os_app_repo / "data" / "portfolio.db")
    conn.row_factory = sqlite3.Row
    try:
        before = build_company_desk(work_os_app_repo, conn, "NU", today=date(2026, 8, 11))
    finally:
        conn.close()
    assert before.earnings_doorway.phase == "pre"
    _insert_artifact(work_os_app_repo, purpose="post_earnings_readout", fiscal_period="2026-06-30")
    conn = sqlite3.connect(work_os_app_repo / "data" / "portfolio.db")
    conn.row_factory = sqlite3.Row
    try:
        after = build_company_desk(work_os_app_repo, conn, "NU", today=date(2026, 8, 11))
    finally:
        conn.close()
    assert after.earnings_doorway.status == "available"
    assert after.earnings_doorway.phase == "post"
    assert after.earnings_doorway.label == "Post-earnings readout →"
    assert after.earnings_doorway.route == "/api/peek/earnings-readout?ticker=NU"


def test_company_desk_prefers_actual_release_date_over_transcript_call_date(
    work_os_app_repo: Path,
) -> None:
    _seed_earnings_store(work_os_app_repo)
    conn = sqlite3.connect(work_os_app_repo / "data" / "portfolio.db")
    conn.execute(
        "INSERT INTO expected_earnings (ticker, expected_date) VALUES ('NU', '2026-08-11')"
    )
    conn.execute(
        "INSERT INTO earnings_surprises (ticker, release_date) VALUES ('NU', '2026-08-11')"
    )
    conn.execute(
        "INSERT INTO transcripts "
        "(document_id, ticker, call_date, fiscal_period_type, period_end) "
        "VALUES (77, 'NU', '2026-08-12', 'Q2', '2026-06-30')"
    )
    conn.commit()
    conn.close()
    _insert_artifact(
        work_os_app_repo,
        purpose="post_earnings_readout",
        fiscal_period="2026-06-30",
    )
    conn = sqlite3.connect(work_os_app_repo / "data" / "portfolio.db")
    conn.row_factory = sqlite3.Row
    try:
        desk = build_company_desk(work_os_app_repo, conn, "NU", today=date(2026, 8, 11))
    finally:
        conn.close()

    assert desk.earnings_doorway.event_date == date(2026, 8, 11)
    assert desk.earnings_doorway.phase == "post"
    assert desk.earnings_doorway.route == "/api/peek/earnings-readout?ticker=NU"


def test_company_desk_post_window_without_artifact_is_honestly_pending(
    work_os_app_repo: Path,
) -> None:
    _seed_earnings_store(work_os_app_repo)
    conn = sqlite3.connect(work_os_app_repo / "data" / "portfolio.db")
    conn.execute(
        "INSERT INTO expected_earnings (ticker, expected_date) VALUES ('NU', '2026-08-11')"
    )
    conn.commit()
    conn.row_factory = sqlite3.Row
    try:
        desk = build_company_desk(work_os_app_repo, conn, "NU", today=date(2026, 8, 12))
    finally:
        conn.close()
    assert desk.earnings_doorway.status == "pending"
    assert desk.earnings_doorway.label == "Post-earnings readout pending"
    assert desk.earnings_doorway.route is None


@pytest.mark.parametrize("stored_date", [None, "not-a-date"])
def test_company_desk_missing_or_unparseable_calendar_has_no_dead_link(
    work_os_app_repo: Path, stored_date: str | None
) -> None:
    _seed_earnings_store(work_os_app_repo)
    conn = sqlite3.connect(work_os_app_repo / "data" / "portfolio.db")
    if stored_date is not None:
        conn.execute(
            "INSERT INTO expected_earnings (ticker, expected_date) VALUES ('NU', ?)", (stored_date,)
        )
    conn.commit()
    conn.row_factory = sqlite3.Row
    try:
        desk = build_company_desk(work_os_app_repo, conn, "NU", today=date(2026, 8, 11))
    finally:
        conn.close()
    assert desk.earnings_doorway.status == "unavailable"
    assert desk.earnings_doorway.route is None


def test_company_desk_keeps_latest_readout_reachable_outside_event_window(
    work_os_app_repo: Path,
) -> None:
    _seed_earnings_store(work_os_app_repo)
    conn = sqlite3.connect(work_os_app_repo / "data" / "portfolio.db")
    conn.execute(
        "INSERT INTO transcripts "
        "(document_id, ticker, call_date, fiscal_period_type, period_end) "
        "VALUES (77, 'NU', '2026-08-12', 'Q2', '2026-06-30')"
    )
    conn.commit()
    conn.close()
    _insert_artifact(
        work_os_app_repo,
        purpose="post_earnings_readout",
        fiscal_period="2026-06-30",
    )
    conn = sqlite3.connect(work_os_app_repo / "data" / "portfolio.db")
    conn.row_factory = sqlite3.Row
    try:
        desk = build_company_desk(work_os_app_repo, conn, "NU", today=date(2026, 11, 15))
    finally:
        conn.close()

    assert desk.earnings_doorway.status == "unavailable"
    assert desk.latest_earnings_readout is not None
    assert desk.latest_earnings_readout.period_label == "Q2 · Jun 2026"
    assert desk.latest_earnings_readout.route.endswith(
        f"&artifact_id={desk.latest_earnings_readout.artifact_id}"
    )
