"""Tests for unified nature-aware atomic KPI admission, Excel cell locators, and ledger rendering."""

from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import cast

import openpyxl
import pytest

from compute.kpi_resolver import semantic_series_identity_sql
from execution import extract_kpis_from_ir as ekfi
from models.facts import (
    Currency,
    FactLocator,
    FiscalPeriodType,
    LegacyEscapeHatch,
    LocatorKind,
    SpreadsheetCellRef,
    Unit,
)
from models.kpis import KpiMetricNature
from pipeline.kpi_atomic_admission import build_atomic_semantic_context
from pipeline.kpi_batch_admission import run_batch_kpi_admission
from pipeline.kpi_persistence import KpiExtractionManifest, KpiValue, persist_manifest
from pipeline.kpi_semantics import (
    KpiAccountingBasis,
    KpiSemanticStatus,
    current_kpi_semantic_context,
    semantic_admission_sql,
)
from pipeline.peeks import render_fact_provenance_peek
from report.sections import thesis as thesis_section

_SCHEMA_DDL = """
CREATE TABLE documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    source_type TEXT NOT NULL,
    doc_type TEXT NOT NULL,
    file_path TEXT NOT NULL,
    sha256 TEXT NOT NULL DEFAULT 'abc',
    fetched_at TIMESTAMP,
    fetch_status TEXT DEFAULT 'ok',
    raw_bytes_size INTEGER DEFAULT 0,
    period_end TEXT,
    source_url TEXT,
    accession_number TEXT,
    filing_date TEXT
);
CREATE TABLE transcripts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id INTEGER,
    ticker TEXT,
    period_end TEXT,
    fiscal_period_type TEXT
);
CREATE TABLE evidence_document_versions (
    document_version_id TEXT PRIMARY KEY,
    legacy_document_id INTEGER
);
CREATE TABLE kpi_definitions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    name TEXT NOT NULL,
    unit TEXT NOT NULL,
    primary_source TEXT NOT NULL,
    fallback_source TEXT,
    ir_url TEXT,
    threshold_tier TEXT,
    threshold_low FLOAT,
    threshold_high FLOAT,
    notes TEXT,
    reporting_cadence TEXT NOT NULL DEFAULT 'quarterly',
    definition_origin TEXT NOT NULL DEFAULT 'analyst',
    UNIQUE(ticker, name)
);
CREATE TABLE kpi_facts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    period_end TIMESTAMP NOT NULL,
    fiscal_period_type TEXT NOT NULL,
    kpi_definition_id INTEGER NOT NULL,
    value NUMERIC(24, 6) NOT NULL,
    unit TEXT NOT NULL,
    currency TEXT,
    source_doc_id INTEGER NOT NULL,
    confidence FLOAT NOT NULL DEFAULT 1.0,
    extracted_by TEXT,
    source_excerpt VARCHAR(1024),
    locator TEXT,
    computed_from TEXT,
    formula_id INTEGER,
    supersedes_id INTEGER
);
CREATE UNIQUE INDEX uq_kpi_facts_provenance
    ON kpi_facts (ticker, period_end, fiscal_period_type, kpi_definition_id, source_doc_id);
CREATE TABLE kpi_fact_semantic_contexts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kpi_fact_id INTEGER NOT NULL,
    revision INTEGER NOT NULL,
    supersedes_context_id INTEGER,
    metric_name_as_reported TEXT NOT NULL,
    reported_period_end TEXT,
    reported_period_start TEXT,
    period_role TEXT NOT NULL,
    publication_lane TEXT NOT NULL,
    accounting_basis TEXT NOT NULL,
    consolidation_scope TEXT NOT NULL,
    dimensions_json TEXT NOT NULL,
    unit_scale TEXT NOT NULL,
    source_row_label TEXT,
    source_column_header TEXT,
    source_value_text TEXT,
    source_precision_json TEXT,
    status TEXT NOT NULL,
    reason_code TEXT,
    reviewed_by TEXT NOT NULL,
    knowledge_at TEXT NOT NULL,
    kpi_definition_revision_id TEXT,
    UNIQUE(kpi_fact_id, revision)
);
CREATE TABLE validation_issues (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    source_doc_id INTEGER,
    ticker TEXT,
    severity TEXT NOT NULL,
    rule TEXT NOT NULL,
    raw_value TEXT,
    expected TEXT,
    raised_at TIMESTAMP NOT NULL,
    resolved_at TIMESTAMP
);
CREATE TABLE ingestion_runs (
    run_id TEXT PRIMARY KEY,
    started_at TIMESTAMP NOT NULL,
    ended_at TIMESTAMP,
    directive TEXT NOT NULL,
    ticker_scope TEXT,
    status TEXT NOT NULL,
    error_summary TEXT
);
CREATE TABLE stage_transitions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    ticker TEXT NOT NULL,
    period_end TIMESTAMP,
    stage TEXT NOT NULL,
    status TEXT NOT NULL,
    started_at TIMESTAMP,
    ended_at TIMESTAMP,
    error_msg TEXT
);
"""


def _make_conn(*, with_evidence_binding: bool = False) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA_DDL)
    conn.execute(
        "INSERT INTO documents (id, ticker, source_type, doc_type, file_path, fetched_at, period_end) "
        "VALUES (1, 'NVO', 'ir_doc', 'ir_press_release', 'ir_documents/NVO/q3.pdf', "
        "'2026-11-05 10:00:00', '2026-09-30')"
    )
    if with_evidence_binding:
        conn.execute(
            "INSERT INTO evidence_document_versions (document_version_id, legacy_document_id) "
            "VALUES ('doc-ver-1', 1)"
        )
    conn.commit()
    return conn


def test_atomic_admission_all_four_metric_natures() -> None:
    conn = _make_conn()
    manifest = KpiExtractionManifest(
        ticker="NVO",
        period_end=datetime(2026, 9, 30),
        fiscal_period_type=FiscalPeriodType.Q3,
        source_doc_id=1,
        auto_admit=True,
        reviewed_by="session:nvo_q3_2026",
        extracted_by="ir_press_release_readout",
        values=[
            KpiValue(
                name="Wegovy CER sales growth",
                value=Decimal("42.5"),
                unit=Unit.PERCENT,
                metric_nature=KpiMetricNature.REPORTED_SCALAR,
                source_excerpt="Wegovy sales increased by 42.5% at constant exchange rates.",
                locator=FactLocator(pdf_page=3),
            ),
            KpiValue(
                name="Oral semaglutide US TRx share (inferred)",
                value=Decimal("18.0"),
                unit=Unit.PERCENT,
                metric_nature=KpiMetricNature.INFERRED_EXTRAPOLATED,
                basis_note="Management stated >2M weekly scripts vs ~11M category total.",
                locator=LegacyEscapeHatch(reason="derived from script commentary on call"),
            ),
            KpiValue(
                name="Active GLP-1 patients",
                value=Decimal("114000000"),
                unit=Unit.COUNT,
                metric_nature=KpiMetricNature.QUALITATIVE_OR_WEAKLY_QUANTIFIED,
                source_excerpt="More than 114 million patients reached globally in Q3 2026.",
                locator=FactLocator(pdf_page=4),
            ),
            KpiValue(
                name="CagriSema regulatory approval milestone",
                value=Decimal("1"),
                unit=Unit.COUNT,
                source_value_text="1",
                unit_scale=None,
                metric_nature=KpiMetricNature.BINARY_OR_ADHOC_MILESTONE,
                source_excerpt="Milestone 1 achieved: NDA submitted in Q3 2026.",
                locator=LegacyEscapeHatch(reason="binary regulatory milestone press announcement"),
            ),
        ],
    )
    res = persist_manifest(conn, run_id="run-1", manifest=manifest)
    assert res.inserted == 4
    assert res.skipped_existing == 0
    assert res.validation_issues == 0

    rows = conn.execute(
        "SELECT kf.id, kd.name, ksc.status, ksc.publication_lane, ksc.dimensions_json, "
        "ksc.source_precision_json, ksc.unit_scale, ksc.source_value_text, ksc.reviewed_by "
        "FROM kpi_facts kf "
        "JOIN kpi_definitions kd ON kd.id = kf.kpi_definition_id "
        "JOIN kpi_fact_semantic_contexts ksc ON ksc.kpi_fact_id = kf.id "
        "ORDER BY kf.id"
    ).fetchall()
    assert len(rows) == 4
    for r in rows:
        assert r["status"] == KpiSemanticStatus.ADMITTED.value
        assert r["publication_lane"] == "current_actual"
        assert r["reviewed_by"] == "session:nvo_q3_2026"

    assert json.loads(rows[0]["dimensions_json"]) == {}
    assert rows[0]["source_precision_json"] is None
    assert json.loads(rows[1]["dimensions_json"]) == {"metric_nature": "inferred_extrapolated"}
    assert "approximate" in rows[1]["source_precision_json"]
    assert rows[2]["unit_scale"] == "millions"
    assert rows[2]["source_value_text"] == "114"
    assert json.loads(rows[3]["dimensions_json"]) == {"metric_nature": "binary_or_adhoc_milestone"}


def test_atomic_binary_milestone_flip_skips_magnitude_jump_quarantine() -> None:
    conn = _make_conn()
    conn.execute(
        "INSERT INTO documents (id, ticker, source_type, doc_type, file_path, fetched_at, period_end) "
        "VALUES (2, 'NVO', 'ir_doc', 'ir_press_release', 'ir_documents/NVO/q4.pdf', "
        "'2026-11-10 10:00:00', '2026-12-31')"
    )
    m0 = KpiExtractionManifest(
        ticker="NVO",
        period_end=datetime(2026, 9, 30),
        fiscal_period_type=FiscalPeriodType.Q3,
        source_doc_id=1,
        auto_admit=True,
        values=[
            KpiValue(
                name="CagriSema regulatory milestones",
                value=Decimal("0"),
                unit=Unit.RATIO,
                metric_nature=KpiMetricNature.BINARY_OR_ADHOC_MILESTONE,
                source_excerpt="Phase 3 filing pending submission at quarter end (status 0).",
                locator=FactLocator(pdf_page=1),
            )
        ],
    )
    m1 = KpiExtractionManifest(
        ticker="NVO",
        period_end=datetime(2026, 12, 31),
        fiscal_period_type=FiscalPeriodType.Q4,
        source_doc_id=2,
        auto_admit=True,
        values=[
            KpiValue(
                name="CagriSema regulatory milestones",
                value=Decimal("1"),
                unit=Unit.RATIO,
                metric_nature=KpiMetricNature.BINARY_OR_ADHOC_MILESTONE,
                source_excerpt="FDA NDA accepted in Q4 2026 (status 1).",
                locator=FactLocator(pdf_page=1),
            )
        ],
    )
    r0 = persist_manifest(conn, run_id="r0", manifest=m0)
    r1 = persist_manifest(conn, run_id="r1", manifest=m1)
    assert r0.inserted == 1
    assert r1.inserted == 1
    assert r1.validation_issues == 0
    statuses = [
        row["status"]
        for row in conn.execute("SELECT status FROM kpi_fact_semantic_contexts").fetchall()
    ]
    assert statuses == ["admitted", "admitted"]


def test_atomic_binary_milestone_non_numeric_excerpt_admits_under_ratio_unit() -> None:
    conn = _make_conn()
    manifest = KpiExtractionManifest(
        ticker="NVO",
        period_end=datetime(2026, 9, 30),
        fiscal_period_type=FiscalPeriodType.Q3,
        source_doc_id=1,
        auto_admit=True,
        reviewed_by="session:nvo_non_numeric_milestone",
        values=[
            KpiValue(
                name="CagriSema NDA submission status",
                value=Decimal("1"),
                unit=Unit.RATIO,
                metric_nature=KpiMetricNature.BINARY_OR_ADHOC_MILESTONE,
                source_excerpt="CagriSema NDA submitted to FDA during the third quarter.",
                locator=LegacyEscapeHatch(reason="qualitative milestone press statement"),
            )
        ],
    )
    res = persist_manifest(conn, run_id="r-non-num", manifest=manifest)
    assert res.inserted == 1
    assert res.validation_issues == 0
    ctx = current_kpi_semantic_context(conn, kpi_fact_id=1)
    assert ctx is not None and ctx.context.status is KpiSemanticStatus.ADMITTED
    assert ctx.context.dimensions == {"metric_nature": "binary_or_adhoc_milestone"}


def test_atomic_admission_fail_closed_and_series_separation() -> None:
    conn = _make_conn()
    # reported_scalar rejects LegacyEscapeHatch
    with pytest.raises(ValueError, match="concrete FactLocator"):
        build_atomic_semantic_context(
            metric_name="Gross bookings",
            period_end=date(2026, 9, 30),
            value=Decimal("100"),
            unit=Unit.MILLIONS,
            extracted_by="ir_press_release_readout",
            locator=LegacyEscapeHatch(reason="no locator supplied"),
            source_excerpt="Gross bookings were $100M",
            metric_nature=KpiMetricNature.REPORTED_SCALAR,
        )

    # Same definition id with one reported_scalar and one inferred_extrapolated point stay isolated
    conn.execute(
        "INSERT INTO documents (id, ticker, source_type, doc_type, file_path, fetched_at, period_end) "
        "VALUES (2, 'NVO', 'ir_doc', 'ir_press_release', 'ir_documents/NVO/q4.pdf', "
        "'2026-11-10 10:00:00', '2026-12-31')"
    )
    persist_manifest(
        conn,
        run_id="r-rep",
        manifest=KpiExtractionManifest(
            ticker="NVO",
            period_end=datetime(2026, 9, 30),
            fiscal_period_type=FiscalPeriodType.Q3,
            source_doc_id=1,
            auto_admit=True,
            values=[
                KpiValue(
                    name="Oral Wegovy sales",
                    value=Decimal("354"),
                    unit=Unit.MILLIONS,
                    currency=Currency.USD,
                    source_excerpt="Oral Wegovy sales reached $354M.",
                    locator=FactLocator(pdf_page=2),
                )
            ],
        ),
    )
    persist_manifest(
        conn,
        run_id="r-inf",
        manifest=KpiExtractionManifest(
            ticker="NVO",
            period_end=datetime(2026, 12, 31),
            fiscal_period_type=FiscalPeriodType.Q4,
            source_doc_id=2,
            auto_admit=True,
            values=[
                KpiValue(
                    name="Oral Wegovy sales",
                    value=Decimal("500"),
                    unit=Unit.MILLIONS,
                    currency=Currency.USD,
                    metric_nature=KpiMetricNature.INFERRED_EXTRAPOLATED,
                    basis_note="Run-rate extrapolated from weekly scripts.",
                    locator=LegacyEscapeHatch(reason="extrapolated figure"),
                )
            ],
        ),
    )
    join_sql, where_sql = semantic_admission_sql(conn, fail_closed=True)
    id_sql = semantic_series_identity_sql(conn, fact_relation="kpi_facts")
    visible = conn.execute(
        f"SELECT kf.id, kf.period_end FROM kpi_facts kf {join_sql} "
        f"WHERE {where_sql} AND {id_sql} ORDER BY kf.id"
    ).fetchall()
    # Latest head is inferred_extrapolated, so reported_scalar point is NOT merged into it
    assert len(visible) == 1
    assert str(visible[0]["period_end"]).startswith("2026-12-31")


def test_atomic_replay_upgrades_legacy_unknown_context() -> None:
    conn = _make_conn()
    legacy = KpiExtractionManifest(
        ticker="NVO",
        period_end=datetime(2026, 9, 30),
        fiscal_period_type=FiscalPeriodType.Q3,
        source_doc_id=1,
        auto_admit=False,
        values=[
            KpiValue(
                name="Wegovy CER sales growth",
                value=Decimal("42.5"),
                unit=Unit.PERCENT,
                source_excerpt="Wegovy sales increased 42.5% CER.",
                locator=FactLocator(pdf_page=3),
            )
        ],
    )
    r1 = persist_manifest(conn, run_id="r1", manifest=legacy)
    assert r1.inserted == 1
    ctx1 = current_kpi_semantic_context(conn, kpi_fact_id=1)
    assert ctx1 is not None and ctx1.context.status is KpiSemanticStatus.LEGACY_UNKNOWN

    atomic = legacy.model_copy(update={"auto_admit": True, "reviewed_by": "session:upgrade"})
    r2 = persist_manifest(conn, run_id="r2", manifest=atomic)
    assert r2.inserted == 0
    assert r2.skipped_existing == 1
    ctx2 = current_kpi_semantic_context(conn, kpi_fact_id=1)
    assert ctx2 is not None
    assert ctx2.revision == 2
    assert ctx2.supersedes_context_id == ctx1.id
    assert ctx2.context.status is KpiSemanticStatus.ADMITTED
    assert ctx2.reviewed_by == "session:upgrade"


def test_extract_kpis_spreadsheet_cell_validation_and_admit(tmp_path: Path) -> None:
    wb_rel = Path("ir_documents/NU/nu_financial_data_q3_2026.xlsx")
    wb_path = tmp_path / wb_rel
    wb_path.parent.mkdir(parents=True, exist_ok=True)
    wb = openpyxl.Workbook()
    ws = wb.active
    assert ws is not None
    ws.title = "P&L"
    ws["G14"] = 12.4
    wb.save(wb_path)
    wb.close()

    conn = _make_conn(with_evidence_binding=True)
    conn.execute(
        "INSERT INTO documents (id, ticker, source_type, doc_type, file_path, fetched_at, period_end) "
        "VALUES (2, 'NU', 'ir_doc', 'ir_historical_spreadsheet', ?, '2026-11-10 10:00:00', NULL)",
        (wb_rel.as_posix(),),
    )
    conn.execute(
        "INSERT INTO evidence_document_versions (document_version_id, legacy_document_id) "
        "VALUES ('doc-ver-2', 2)"
    )
    # Document without evidence binding
    conn.execute(
        "INSERT INTO documents (id, ticker, source_type, doc_type, file_path, fetched_at, period_end) "
        "VALUES (3, 'NU', 'ir_doc', 'ir_historical_spreadsheet', ?, '2026-11-10 10:00:00', NULL)",
        (wb_rel.as_posix(),),
    )
    conn.commit()

    valid_manifest = ekfi.ManifestFile(
        manifests=[
            KpiExtractionManifest(
                ticker="NU",
                period_end=datetime(2026, 9, 30),
                fiscal_period_type=FiscalPeriodType.Q3,
                source_doc_id=2,
                extracted_by="ir_spreadsheet",
                auto_admit=True,
                reviewed_by="session:nu_q3",
                values=[
                    KpiValue(
                        name="Monthly ARPAC",
                        value=Decimal("12.4"),
                        unit=Unit.ACTUAL,
                        currency=Currency.USD,
                        source_excerpt="Monthly ARPAC was $12.4 in Q3 2026.",
                        locator=FactLocator(
                            spreadsheet_cell=SpreadsheetCellRef(
                                sheet_name="P&L", cell_address="G14"
                            )
                        ),
                    )
                ],
            )
        ]
    )
    res = ekfi.apply_manifest(conn, valid_manifest, state_root=tmp_path)
    assert res["failed"] == 0
    assert res["inserted"] == 1
    row = conn.execute("SELECT locator FROM kpi_facts ORDER BY id DESC LIMIT 1").fetchone()
    parsed = FactLocator.from_json(row["locator"])
    assert parsed is not None
    assert parsed.kind is LocatorKind.SPREADSHEET_CELL
    assert parsed.spreadsheet_cell is not None
    assert parsed.spreadsheet_cell.sheet_name == "P&L"
    assert parsed.spreadsheet_cell.cell_address == "G14"

    # Missing sheet fails closed cleanly
    bad_sheet_manifest = ekfi.ManifestFile(
        manifests=[
            KpiExtractionManifest(
                ticker="NU",
                period_end=datetime(2026, 9, 30),
                fiscal_period_type=FiscalPeriodType.Q3,
                source_doc_id=2,
                auto_admit=True,
                values=[
                    KpiValue(
                        name="Missing metric",
                        value=Decimal("1.0"),
                        unit=Unit.RATIO,
                        source_excerpt="1.0 ratio",
                        locator=FactLocator(
                            spreadsheet_cell=SpreadsheetCellRef(
                                sheet_name="Nonexistent", cell_address="A1"
                            )
                        ),
                    )
                ],
            )
        ]
    )
    res_bad_sheet = ekfi.apply_manifest(conn, bad_sheet_manifest, state_root=tmp_path)
    assert res_bad_sheet["failed"] == 1

    # Unbound evidence document fails closed when auto_admit=True
    unbound_manifest = ekfi.ManifestFile(
        manifests=[
            KpiExtractionManifest(
                ticker="NU",
                period_end=datetime(2026, 9, 30),
                fiscal_period_type=FiscalPeriodType.Q3,
                source_doc_id=3,
                auto_admit=True,
                values=[
                    KpiValue(
                        name="Monthly ARPAC",
                        value=Decimal("12.4"),
                        unit=Unit.ACTUAL,
                        currency=Currency.USD,
                        source_excerpt="Monthly ARPAC was $12.4",
                        locator=FactLocator(
                            spreadsheet_cell=SpreadsheetCellRef(
                                sheet_name="P&L", cell_address="G14"
                            )
                        ),
                    )
                ],
            )
        ]
    )
    res_unbound = ekfi.apply_manifest(conn, unbound_manifest, state_root=tmp_path)
    assert res_unbound["failed"] == 1


def test_spreadsheet_cell_peek_renders_sheet_cell_and_viewer_link(tmp_path: Path) -> None:
    db = tmp_path / "peek.db"
    conn = sqlite3.connect(db)
    conn.executescript(_SCHEMA_DDL)
    conn.execute(
        "INSERT INTO documents (id, ticker, source_type, doc_type, file_path, fetched_at) "
        "VALUES (9, 'NU', 'ir_doc', 'ir_historical_spreadsheet', "
        "'ir_documents/NU/nu_data.xlsx', '2026-11-10 10:00:00')"
    )
    conn.execute(
        "INSERT INTO kpi_definitions (id, ticker, name, unit, primary_source) "
        "VALUES (1, 'NU', 'Monthly ARPAC', 'actual', 'ir_doc')"
    )
    loc = FactLocator(
        locator_version=2,
        kind=LocatorKind.SPREADSHEET_CELL,
        spreadsheet_cell=SpreadsheetCellRef(
            sheet_name="P&L", cell_address="G14", table_name="Key Metrics"
        ),
        verbatim_snippet="Monthly ARPAC: $12.4",
    )
    conn.execute(
        "INSERT INTO kpi_facts (id, ticker, period_end, fiscal_period_type, kpi_definition_id, "
        "value, unit, source_doc_id, locator, extracted_by) "
        "VALUES (1, 'NU', '2026-09-30', 'Q3', 1, 12.4, 'actual', 9, ?, 'ir_spreadsheet')",
        (loc.to_json(),),
    )
    conn.commit()
    conn.close()

    html = render_fact_provenance_peek(db, tmp_path, "kpi_facts:1")
    assert html is not None
    assert "Monthly ARPAC" in html
    assert "P&amp;L" in html
    assert "G14" in html
    assert "Key Metrics" in html
    assert "Monthly ARPAC: $12.4" in html
    assert 'href="/source/9"' in html


def test_thesis_ledger_surfaces_non_scalar_holdings_status_without_weakening_scalar(
    tmp_path: Path,
) -> None:
    repo = tmp_path
    hdir = repo / "micro_thesis" / "holdings"
    hdir.mkdir(parents=True, exist_ok=True)
    holdings_payload = {
        "ticker": "NVO",
        "thesis": "GLP-1 volume expansion.",
        "verdict": "buy",
        "tier_1_kpis": [
            {
                "name": "CagriSema regulatory milestones",
                "metric_nature": "binary_or_adhoc_milestone",
                "status": "Yellow",
                "source_excerpt": "NDA submitted; FDA decision expected H1 2027.",
                "break_condition": "CRL or multi-year safety hold.",
                "source": "press releases",
            },
            {
                "name": "Oral Wegovy commentary",
                "metric_nature": "qualitative_or_weakly_quantified",
                "status": "Green",
                "current": "Management cited strong initial demand across channels.",
                "note": "Qualitative demand signal until script share stabilizes.",
                "source": "transcripts",
            },
            {
                "name": "Wegovy CER revenue growth",
                "metric_nature": "reported_scalar",
                "status": "Green",
                "current": "+42% YoY",
                "break_condition": "<15% YoY",
                "source": "earnings release",
            },
        ],
    }
    (hdir / "NVO.json").write_text(json.dumps(holdings_payload), encoding="utf-8")

    # Initialize empty DB without kpi_facts so reported_scalar stays strictly unknown
    db = repo / "data" / "portfolio.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db)
    conn.executescript(_SCHEMA_DDL)
    conn.execute(
        "CREATE TABLE thesis_evaluations (ticker TEXT, evaluated_at TEXT, overall_status TEXT, "
        "rule_evaluations_json TEXT, soft_rule_evaluations_json TEXT)"
    )
    conn.commit()
    conn.row_factory = sqlite3.Row
    section = thesis_section.build("NVO", repo, conn=conn)
    conn.close()

    by_name = {row.name: row for row in section.kpi_ledger}
    milestone = by_name["CagriSema regulatory milestones"]
    assert milestone.metric_nature == "binary_or_adhoc_milestone"
    assert milestone.current_status == "yellow"
    assert milestone.latest_source_excerpt == "NDA submitted; FDA decision expected H1 2027."

    qual = by_name["Oral Wegovy commentary"]
    assert qual.metric_nature == "qualitative_or_weakly_quantified"
    assert qual.current_status == "green"
    assert qual.latest_source_excerpt == "Management cited strong initial demand across channels."

    scalar = by_name["Wegovy CER revenue growth"]
    assert scalar.metric_nature == "reported_scalar"
    assert scalar.current_status == "unknown"


def test_kpi_batch_admission_merges_cosmetic_newline_signatures() -> None:
    conn = _make_conn()
    conn.execute(
        "INSERT INTO kpi_definitions (id, ticker, name, unit, primary_source) "
        "VALUES (10, 'BKNG', 'Gross bookings', 'millions', 'ir_doc')"
    )
    conn.execute(
        "INSERT INTO kpi_facts (id, ticker, period_end, fiscal_period_type, kpi_definition_id, "
        "value, unit, source_doc_id, extracted_by) "
        "VALUES (1, 'BKNG', '2026-03-31', 'Q1', 10, 41000, 'millions', 1, 'issuer_manifest_v1'), "
        "(2, 'BKNG', '2026-06-30', 'Q2', 10, 43000, 'millions', 1, 'issuer_manifest_v1'), "
        "(3, 'BKNG', '2026-09-30', 'Q3', 10, 45000, 'millions', 1, 'issuer_manifest_v1')"
    )
    conn.execute(
        "INSERT INTO kpi_fact_semantic_contexts (kpi_fact_id, revision, supersedes_context_id, "
        "metric_name_as_reported, reported_period_end, period_role, publication_lane, "
        "accounting_basis, consolidation_scope, dimensions_json, unit_scale, status, reason_code, "
        "reviewed_by, knowledge_at) VALUES "
        "(1, 1, NULL, 'Total Gross \nBookings', '2026-03-31', 'current', 'current_actual', "
        "'management', 'consolidated', '{}', 'millions', 'admitted', NULL, 'r', '2026-05-01T00:00:00Z'), "
        "(2, 1, NULL, 'Total Gross Bookings', '2026-06-30', 'current', 'current_actual', "
        "'management', 'consolidated', '{}', 'millions', 'admitted', NULL, 'r', '2026-08-01T00:00:00Z')"
    )
    conn.commit()
    report = run_batch_kpi_admission(
        conn, tickers=("BKNG",), apply=True, require_resolved_view=False
    )
    assert report.total_admitted_applied == 1
    assert report.skipped_counts.get("ambiguous_existing_admitted_series", 0) == 0
    ctx3 = current_kpi_semantic_context(conn, kpi_fact_id=3)
    assert ctx3 is not None and ctx3.context.status is KpiSemanticStatus.ADMITTED
    assert ctx3.context.metric_name_as_reported == "Total Gross Bookings"
    assert ctx3.context.accounting_basis is KpiAccountingBasis.MANAGEMENT
    _ = cast("object", ctx3)
