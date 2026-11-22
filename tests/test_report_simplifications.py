"""Focused unit tests for Wave A report, financials, earnings, and planner fixes."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from execution.extract_kpis_from_ir import refresh_sqlite_planner_stats
from report import builder as report_builder
from report.models import (
    KpiLedgerRow,
    KpiSnapshotRow,
    SectionStatus,
    SnapshotSection,
    ThesisSection,
    ValuationSnapshot,
)
from report.sections import earnings as earnings_section
from report.sections import financials as financials_section
from tests.test_report_canonical_financials import database as database


def test_hydrate_tier_1_kpi_strip_populates_latest_and_prior() -> None:
    snap = SnapshotSection(
        status=SectionStatus.OK,
        ticker="NU",
        valuation=ValuationSnapshot(),
        tier_1_kpi_row=[
            KpiSnapshotRow(name="Capital adequacy ratio", threshold="> 13%"),
            KpiSnapshotRow(name="Unextracted KPI"),
        ],
    )
    thesis = ThesisSection(
        status=SectionStatus.OK,
        kpi_ledger=[
            KpiLedgerRow(
                name="Capital adequacy ratio",
                tier="tier_1",
                unit="%",
                history=[
                    ("2026-03-31", None),
                    ("2026-06-30", 15.7),
                    ("2026-09-30", 16.1),
                ],
                current_status="green",
            )
        ],
    )
    hydrated = report_builder.hydrate_tier_1_kpi_strip(snap, thesis)
    first, second = hydrated.tier_1_kpi_row
    assert first.current == "16.1% (2026-09-30)"
    assert first.prior == "15.7% (2026-06-30)"
    assert first.threshold == "> 13%"
    assert first.status == "green"
    assert second.current is None
    assert second.status == "unknown"


def test_financials_build_legacy_fallback_respects_currency_and_explicit_as_of(
    database: sqlite3.Connection, tmp_path: Path
) -> None:
    database.execute(
        "INSERT INTO documents(id,ticker,source_type,doc_type,file_path,sha256,fetched_at,"
        "fetch_status,raw_bytes_size,source_quality_tier) VALUES "
        "(501,'DKKCO','fmp','fmp_income_statement','dkk.json','h1','2026-10-01','ok',1,'fmp_normalized')"
    )
    database.execute(
        "INSERT INTO evidence_document_versions("
        "document_version_id,document_key,version_sequence,observation_id,blob_sha256,"
        "issuer_id,ticker,document_type,form_type,language,legacy_document_id,recorded_at"
        ") SELECT 'dkk-doc','dkk-doc',1,observation_id,blob_sha256,issuer_id,"
        "'DKKCO','regulatory_filing','10-K','en',501,recorded_at "
        "FROM evidence_document_versions WHERE document_version_id='document-1'"
    )
    for period_end, fpt, rev in (
        ("2026-03-31 00:00:00", "Q1", "75000000000"),
        ("2026-06-30 00:00:00", "Q2", "78000000000"),
        ("2025-12-31 00:00:00", "FY", "290000000000"),
    ):
        database.execute(
            "INSERT INTO financial_facts(ticker,period_end,fiscal_period_type,line_item,"
            "value,currency,unit,source_doc_id) VALUES ('DKKCO', ?, ?, 'revenue', ?, 'DKK', 'actual', 501)",
            (period_end, fpt, rev),
        )
    database.commit()

    as_of_empty = financials_section.build(
        "DKKCO", tmp_path, conn=database, as_of=datetime(2026, 10, 1, tzinfo=UTC)
    )
    assert as_of_empty.status == SectionStatus.MISSING_DATA

    live = financials_section.build("DKKCO", tmp_path, conn=database, as_of=None)
    assert live.status == SectionStatus.OK
    assert live.currency == "DKK"
    rev_q = next(item for item in live.line_items if item.line_item == "Revenue")
    assert rev_q.unit == "DKK millions"
    assert rev_q.values == [75000.0, 78000.0]
    rev_a = next(item for item in live.annual_line_items if item.line_item == "Revenue")
    assert rev_a.unit == "DKK millions"
    assert rev_a.years == [2025]
    assert rev_a.values == [290000.0]


def test_earnings_build_uses_active_post_earnings_readout_when_doc_summaries_absent(
    migrated_db: Callable[..., Path], tmp_path: Path
) -> None:
    db = migrated_db(tmp_path / "earnings.db")
    conn = sqlite3.connect(db)
    try:
        conn.execute(
            "INSERT INTO documents(id,ticker,source_type,doc_type,file_path,sha256,fetched_at,"
            "fetch_status,raw_bytes_size,source_quality_tier) VALUES "
            "(701,'NU','manual','transcript','nu_q2.txt','sh1','2026-08-15','ok',1,'ir_primary')"
        )
        conn.execute(
            "INSERT INTO transcripts(id,document_id,ticker,call_date,fiscal_period_type,period_end) "
            "VALUES (901,701,'NU','2026-08-14','Q2','2026-06-30 00:00:00')"
        )
        conn.execute(
            "INSERT INTO llm_artifacts(ticker,scope,purpose,fiscal_period,content_md,content_json,"
            "input_sha256,model,prompt_version,generated_at) VALUES "
            "('NU','ticker','post_earnings_readout','2026-06-30',"
            "'## Quarter in one line\nStrong ROE 32.85%.\n\n## Thesis update\nIntact.',"
            "'{}','insha','claude-sonnet-4-6','v1','2026-11-22T00:00:00+00:00')"
        )
        conn.commit()
        section = earnings_section.build("NU", tmp_path, conn=conn)
    finally:
        conn.close()

    assert section.status == SectionStatus.OK
    assert section.missing is None
    assert len(section.full_quarters) == 1
    card = section.full_quarters[0]
    assert card.quarter == "Q2"
    assert card.year == 2026
    assert card.digest_md == "Strong ROE 32.85%."


def test_refresh_sqlite_planner_stats_runs_analyze_then_optimize(tmp_path: Path) -> None:
    db = tmp_path / "stats.db"
    conn = sqlite3.connect(db)
    try:
        conn.executescript(
            """
            CREATE TABLE kpi_fact_semantic_contexts (id INTEGER PRIMARY KEY, val TEXT);
            CREATE TABLE fact_resolution_outcomes (id INTEGER PRIMARY KEY, val TEXT);
            INSERT INTO kpi_fact_semantic_contexts(val) VALUES ('a');
            INSERT INTO fact_resolution_outcomes(val) VALUES ('b');
            """
        )
        conn.commit()
        assert (
            conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='sqlite_stat1'"
            ).fetchone()
            is None
        )
        refresh_sqlite_planner_stats(conn)
        assert (
            conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='sqlite_stat1'"
            ).fetchone()
            is not None
        )
        stat_tables = {
            str(r[0]) for r in conn.execute("SELECT DISTINCT tbl FROM sqlite_stat1").fetchall()
        }
        assert {"kpi_fact_semantic_contexts", "fact_resolution_outcomes"}.issubset(stat_tables)
        refresh_sqlite_planner_stats(conn)
    finally:
        conn.close()
