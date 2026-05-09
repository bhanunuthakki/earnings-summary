"""Tests for src/pipeline/ — source routing, run accounting, queries.

Uses an in-memory SQLite DB seeded with the minimum schema and rows needed
for each test. Avoids touching the real portfolio.db.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime

import pytest

from models.documents import DocType, SourceType
from models.runs import StageName, StageStatus
from pipeline.queries import (
    documents_count_by_doc_type,
    documents_for,
    kpi_definitions_for,
    latest_document_for,
    tracked_companies_for_user,
)
from pipeline.run_accounting import (
    end_run,
    latest_stage_for,
    record_stage,
    stages_not_ok_for,
    start_run,
)
from pipeline.source_routing import plan_for_ticker


def _create_schema(conn: sqlite3.Connection) -> None:
    cur = conn.cursor()
    cur.executescript(
        """
        CREATE TABLE tracked_companies (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER DEFAULT 1,
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

        CREATE TABLE kpi_definitions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL,
            name TEXT NOT NULL,
            unit TEXT NOT NULL DEFAULT 'actual',
            primary_source TEXT NOT NULL,
            fallback_source TEXT,
            ir_url TEXT,
            threshold_tier TEXT,
            threshold_low REAL,
            threshold_high REAL,
            notes TEXT
        );

        CREATE TABLE documents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL,
            source_type TEXT NOT NULL,
            doc_type TEXT NOT NULL,
            period_start TIMESTAMP,
            period_end TIMESTAMP,
            file_path TEXT NOT NULL,
            sha256 TEXT NOT NULL,
            fetched_at TIMESTAMP NOT NULL,
            fetch_status TEXT NOT NULL,
            http_code INTEGER,
            raw_bytes_size INTEGER NOT NULL,
            source_url TEXT,
            parent_document_id INTEGER
        );

        CREATE TABLE ingestion_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL UNIQUE,
            started_at TIMESTAMP NOT NULL,
            ended_at TIMESTAMP,
            directive TEXT NOT NULL,
            ticker_scope TEXT NOT NULL,
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
            started_at TIMESTAMP NOT NULL,
            ended_at TIMESTAMP,
            error_msg TEXT
        );
        """
    )
    conn.commit()


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    _create_schema(c)
    return c


def _add_company(
    conn: sqlite3.Connection,
    ticker: str,
    instrument_type: str | None = "equity",
    filing_regime: str | None = "10-K",
    list_type: str = "portfolio",
) -> None:
    conn.execute(
        "INSERT INTO tracked_companies "
        "(user_id, ticker, name, list_type, instrument_type, filing_regime) "
        "VALUES (1, ?, ?, ?, ?, ?)",
        (ticker, ticker + " Inc.", list_type, instrument_type, filing_regime),
    )
    conn.commit()


def _add_kpi(
    conn: sqlite3.Connection,
    ticker: str,
    name: str,
    primary: str,
    fallback: str | None = None,
    ir_url: str | None = None,
) -> None:
    conn.execute(
        "INSERT INTO kpi_definitions "
        "(ticker, name, primary_source, fallback_source, ir_url) "
        "VALUES (?, ?, ?, ?, ?)",
        (ticker, name, primary, fallback, ir_url),
    )
    conn.commit()


# --- source_routing ---


def test_etf_routes_to_fmp_only(conn: sqlite3.Connection) -> None:
    """ETFs ignore the kpi_definitions registry and pull only from FMP (ETF endpoints)."""
    _add_company(conn, "FLKR", instrument_type="etf", filing_regime=None)
    _add_kpi(conn, "FLKR", "should_be_ignored", primary="ir_doc")
    plan = plan_for_ticker(conn, "FLKR")
    assert plan.sources == {SourceType.FMP}
    assert plan.primary_kpi_names == []


def test_equity_with_no_kpis_routes_to_fmp_only(conn: sqlite3.Connection) -> None:
    """Equities with no IR-override KPIs route only to FMP."""
    _add_company(conn, "AAPL")
    plan = plan_for_ticker(conn, "AAPL")
    assert plan.sources == {SourceType.FMP}
    assert plan.ir_urls == []


def test_equity_with_ir_kpi_adds_ir_doc_source(conn: sqlite3.Connection) -> None:
    """A KPI with primary_source=ir_doc adds IR_DOC to the source set."""
    _add_company(conn, "NU", instrument_type="adr", filing_regime="20-F")
    _add_kpi(
        conn,
        "NU",
        "NPL buckets",
        primary="ir_doc",
        fallback="sec_xbrl",
        ir_url="https://investors.nu/",
    )
    plan = plan_for_ticker(conn, "NU")
    assert SourceType.FMP in plan.sources
    assert SourceType.IR_DOC in plan.sources
    assert SourceType.SEC_XBRL in plan.sources
    assert plan.ir_urls == ["https://investors.nu/"]
    assert plan.primary_kpi_names == ["NPL buckets"]


def test_unknown_ticker_raises(conn: sqlite3.Connection) -> None:
    """Asking for a routing plan on an untracked ticker is a programmer error."""
    with pytest.raises(ValueError, match="not in tracked_companies"):
        plan_for_ticker(conn, "ZZZ")


def test_lowercase_ticker_normalizes(conn: sqlite3.Connection) -> None:
    """Lowercase ticker arg is uppercased before query."""
    _add_company(conn, "GOOG")
    _add_kpi(conn, "GOOG", "Cloud OI", primary="sec_xbrl", fallback="ir_doc")
    plan = plan_for_ticker(conn, "goog")
    assert plan.ticker == "GOOG"
    assert SourceType.SEC_XBRL in plan.sources


# --- run_accounting ---


def test_start_run_creates_row_with_in_progress_status(conn: sqlite3.Connection) -> None:
    run_id = start_run(conn, directive="test_directive", ticker_scope=["GOOG", "META"])
    cur = conn.execute("SELECT * FROM ingestion_runs WHERE run_id = ?", (run_id,))
    row = cur.fetchone()
    assert row is not None
    assert row["status"] == StageStatus.IN_PROGRESS.value
    assert row["ended_at"] is None
    assert json.loads(row["ticker_scope"]) == ["GOOG", "META"]


def test_record_stage_terminal_sets_ended_at(conn: sqlite3.Connection) -> None:
    run_id = start_run(conn, directive="d", ticker_scope=["GOOG"])
    record_stage(conn, run_id, "GOOG", StageName.INGEST, StageStatus.OK)
    last = latest_stage_for(conn, run_id, "GOOG")
    assert last == (StageName.INGEST, StageStatus.OK)


def test_record_stage_in_progress_leaves_ended_at_null(conn: sqlite3.Connection) -> None:
    run_id = start_run(conn, directive="d", ticker_scope=["GOOG"])
    record_stage(conn, run_id, "GOOG", StageName.INGEST, StageStatus.IN_PROGRESS)
    cur = conn.execute("SELECT ended_at FROM stage_transitions WHERE run_id = ?", (run_id,))
    assert cur.fetchone()["ended_at"] is None


def test_end_run_updates_status_and_ended_at(conn: sqlite3.Connection) -> None:
    run_id = start_run(conn, directive="d", ticker_scope=["GOOG"])
    end_run(conn, run_id, StageStatus.FAILED, error_summary="boom")
    cur = conn.execute(
        "SELECT status, ended_at, error_summary FROM ingestion_runs WHERE run_id = ?", (run_id,)
    )
    row = cur.fetchone()
    assert row["status"] == StageStatus.FAILED.value
    assert row["ended_at"] is not None
    assert row["error_summary"] == "boom"


def test_stages_not_ok_filters_correctly(conn: sqlite3.Connection) -> None:
    run_id = start_run(conn, directive="d", ticker_scope=["GOOG", "META"])
    record_stage(conn, run_id, "GOOG", StageName.INGEST, StageStatus.OK)
    record_stage(conn, run_id, "META", StageName.INGEST, StageStatus.OK)
    record_stage(conn, run_id, "META", StageName.PARSE, StageStatus.FAILED, error_msg="schema")
    not_ok = stages_not_ok_for(conn, run_id)
    assert len(not_ok) == 1
    assert not_ok[0][0] == "META"
    assert not_ok[0][2] == StageName.PARSE
    assert not_ok[0][3] == StageStatus.FAILED


# --- queries ---


def test_tracked_companies_only_classified(conn: sqlite3.Connection) -> None:
    """only_classified filters out tickers with NULL instrument_type."""
    _add_company(conn, "GOOG", instrument_type="equity")
    _add_company(conn, "ZZZ", instrument_type=None, filing_regime=None)
    classified = tracked_companies_for_user(conn, only_classified=True)
    assert {c.ticker for c in classified} == {"GOOG"}
    everyone = tracked_companies_for_user(conn, only_classified=False)
    assert {c.ticker for c in everyone} == {"GOOG", "ZZZ"}


def test_kpi_definitions_for_returns_rows(conn: sqlite3.Connection) -> None:
    _add_company(conn, "NU", instrument_type="adr", filing_regime="20-F")
    _add_kpi(conn, "NU", "NPL", primary="ir_doc")
    rows = kpi_definitions_for(conn, "NU")
    assert len(rows) == 1
    assert rows[0]["name"] == "NPL"


def _add_document(
    conn: sqlite3.Connection,
    ticker: str,
    doc_type: str,
    fetched_at: datetime,
    sha256: str,
) -> None:
    conn.execute(
        "INSERT INTO documents "
        "(ticker, source_type, doc_type, file_path, sha256, fetched_at, fetch_status, raw_bytes_size) "
        "VALUES (?, 'fmp', ?, ?, ?, ?, 'ok', 100)",
        (ticker, doc_type, f"data/historical/fmp/{ticker}_{doc_type}.json", sha256, fetched_at),
    )
    conn.commit()


def test_latest_document_for_returns_most_recent(conn: sqlite3.Connection) -> None:
    older = datetime(2025, 1, 1)
    newer = datetime(2026, 1, 1)
    _add_document(conn, "GOOG", DocType.FMP_INCOME_STATEMENT.value, older, "a" * 64)
    _add_document(conn, "GOOG", DocType.FMP_INCOME_STATEMENT.value, newer, "b" * 64)
    doc = latest_document_for(conn, "GOOG", DocType.FMP_INCOME_STATEMENT)
    assert doc is not None
    assert doc["sha256"] == "b" * 64


def test_documents_for_filters(conn: sqlite3.Connection) -> None:
    _add_document(conn, "GOOG", DocType.FMP_INCOME_STATEMENT.value, datetime(2025, 1, 1), "a" * 64)
    _add_document(conn, "GOOG", DocType.FMP_BALANCE_SHEET.value, datetime(2025, 1, 1), "b" * 64)
    income = documents_for(conn, "GOOG", doc_type=DocType.FMP_INCOME_STATEMENT)
    assert len(income) == 1
    all_ = documents_for(conn, "GOOG")
    assert len(all_) == 2


def test_documents_count_by_doc_type(conn: sqlite3.Connection) -> None:
    _add_document(conn, "GOOG", DocType.FMP_INCOME_STATEMENT.value, datetime(2025, 1, 1), "a" * 64)
    _add_document(conn, "META", DocType.FMP_INCOME_STATEMENT.value, datetime(2025, 1, 1), "b" * 64)
    _add_document(conn, "GOOG", DocType.FMP_BALANCE_SHEET.value, datetime(2025, 1, 1), "c" * 64)
    histogram = documents_count_by_doc_type(conn)
    counts = dict(histogram)
    assert counts[DocType.FMP_INCOME_STATEMENT.value] == 2
    assert counts[DocType.FMP_BALANCE_SHEET.value] == 1
