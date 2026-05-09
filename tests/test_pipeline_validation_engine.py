"""Tests for src/pipeline/validation_engine.py — range, magnitude_jump, source_disagreement."""

from __future__ import annotations

import sqlite3
from datetime import datetime

import pytest

from models.facts import Unit
from models.validation import Severity, ValidationRule
from pipeline.validation_engine import (
    _check_financial_fact_ranges,
    _check_kpi_fact_ranges,
    _check_magnitude_jumps,
    _check_source_disagreement,
    run_all_checks,
)


def _create_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE documents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL,
            source_type TEXT NOT NULL,
            doc_type TEXT NOT NULL,
            file_path TEXT NOT NULL,
            sha256 TEXT NOT NULL,
            fetched_at TIMESTAMP NOT NULL,
            fetch_status TEXT NOT NULL,
            raw_bytes_size INTEGER NOT NULL
        );
        CREATE TABLE financial_facts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL,
            period_end TIMESTAMP NOT NULL,
            fiscal_period_type TEXT NOT NULL,
            line_item TEXT NOT NULL,
            value NUMERIC(24,6) NOT NULL,
            currency TEXT,
            unit TEXT NOT NULL,
            source_doc_id INTEGER NOT NULL
        );
        CREATE TABLE kpi_definitions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL,
            name TEXT NOT NULL,
            unit TEXT NOT NULL,
            primary_source TEXT NOT NULL,
            UNIQUE(ticker, name)
        );
        CREATE TABLE kpi_facts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL,
            period_end TIMESTAMP NOT NULL,
            fiscal_period_type TEXT NOT NULL,
            kpi_definition_id INTEGER NOT NULL,
            value NUMERIC(24,6) NOT NULL,
            unit TEXT NOT NULL,
            source_doc_id INTEGER NOT NULL
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
        """
    )
    conn.commit()


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    _create_schema(c)
    return c


def _doc(conn, ticker: str, source_type: str = "fmp") -> int:
    cur = conn.execute(
        "INSERT INTO documents (ticker, source_type, doc_type, file_path, sha256, "
        "fetched_at, fetch_status, raw_bytes_size) VALUES (?, ?, 'x', 'p', ?, ?, 'ok', 1)",
        (ticker, source_type, "a" * 64, datetime.now()),
    )
    return int(cur.lastrowid) if cur.lastrowid is not None else 0


def _ff(conn, *, ticker: str, line_item: str, value: float, period_end: datetime,
        fpt: str = "Q4", source_doc_id: int) -> None:
    conn.execute(
        "INSERT INTO financial_facts (ticker, period_end, fiscal_period_type, "
        "line_item, value, currency, unit, source_doc_id) "
        "VALUES (?, ?, ?, ?, ?, 'USD', 'actual', ?)",
        (ticker, period_end, fpt, line_item, str(value), source_doc_id),
    )
    conn.commit()


def _kpi(conn, *, ticker: str, name: str, unit: str, value: float,
         period_end: datetime, source_doc_id: int) -> None:
    cur = conn.execute(
        "INSERT INTO kpi_definitions (ticker, name, unit, primary_source) VALUES (?, ?, ?, 'fmp')",
        (ticker, name, unit),
    )
    kid = int(cur.lastrowid) if cur.lastrowid is not None else 0
    conn.execute(
        "INSERT INTO kpi_facts (ticker, period_end, fiscal_period_type, kpi_definition_id, "
        "value, unit, source_doc_id) VALUES (?, ?, 'Q4', ?, ?, ?, ?)",
        (ticker, period_end, kid, str(value), unit, source_doc_id),
    )
    conn.commit()


def test_range_check_skips_unbounded_line_items(conn: sqlite3.Connection) -> None:
    """Currency-agnostic design: revenue/op_income/net_income have no range bound, so no firings."""
    doc_id = _doc(conn, "X")
    _ff(conn, ticker="X", line_item="revenue", value=-1_000_000, period_end=datetime(2024, 12, 31), source_doc_id=doc_id)
    _ff(conn, ticker="X", line_item="net_income", value=-50_000_000, period_end=datetime(2024, 12, 31), source_doc_id=doc_id)
    outcome = _check_financial_fact_ranges(conn, run_id="r1", ticker="X")
    assert outcome.issues_inserted == 0


def test_range_check_fires_on_negative_total_assets(conn: sqlite3.Connection) -> None:
    doc_id = _doc(conn, "X")
    _ff(conn, ticker="X", line_item="total_assets", value=-1_000, period_end=datetime(2024, 12, 31), source_doc_id=doc_id)
    outcome = _check_financial_fact_ranges(conn, run_id="r1", ticker="X")
    assert outcome.issues_inserted == 1
    issue = conn.execute(
        "SELECT severity, rule FROM validation_issues WHERE ticker='X'"
    ).fetchone()
    assert dict(issue)["severity"] == Severity.WARN.value
    assert dict(issue)["rule"] == ValidationRule.PLAUSIBLE_RANGE.value


def test_kpi_range_check_fires_on_out_of_band_percent(conn: sqlite3.Connection) -> None:
    """Op margin > 1000% should fire (FNV's 5258% case)."""
    doc_id = _doc(conn, "FNV")
    _kpi(conn, ticker="FNV", name="OpMargin", unit=Unit.PERCENT.value, value=5000,
         period_end=datetime(2024, 12, 31), source_doc_id=doc_id)
    outcome = _check_kpi_fact_ranges(conn, run_id="r1", ticker="FNV")
    assert outcome.issues_inserted == 1


def test_magnitude_jump_fires_on_5x_sequential(conn: sqlite3.Connection) -> None:
    """Sequential same-key values differing by >5x get flagged."""
    doc_id = _doc(conn, "X")
    _ff(conn, ticker="X", line_item="revenue", value=100, period_end=datetime(2023, 12, 31), source_doc_id=doc_id)
    _ff(conn, ticker="X", line_item="revenue", value=1000, period_end=datetime(2024, 12, 31), source_doc_id=doc_id)
    outcome = _check_magnitude_jumps(conn, run_id="r1", ticker="X")
    assert outcome.issues_inserted == 1


def test_magnitude_jump_quiet_when_within_5x(conn: sqlite3.Connection) -> None:
    doc_id = _doc(conn, "X")
    _ff(conn, ticker="X", line_item="revenue", value=100, period_end=datetime(2023, 12, 31), source_doc_id=doc_id)
    _ff(conn, ticker="X", line_item="revenue", value=300, period_end=datetime(2024, 12, 31), source_doc_id=doc_id)
    outcome = _check_magnitude_jumps(conn, run_id="r1", ticker="X")
    assert outcome.issues_inserted == 0


def test_source_disagreement_only_across_distinct_source_types(conn: sqlite3.Connection) -> None:
    """Two FMP rows reporting the same period don't fire (intra-source aggregation noise)."""
    fmp1 = _doc(conn, "X", source_type="fmp")
    fmp2 = _doc(conn, "X", source_type="fmp")
    _ff(conn, ticker="X", line_item="revenue", value=100, period_end=datetime(2024, 12, 31), source_doc_id=fmp1)
    _ff(conn, ticker="X", line_item="revenue", value=200, period_end=datetime(2024, 12, 31), source_doc_id=fmp2)
    outcome = _check_source_disagreement(conn, run_id="r1", ticker="X")
    assert outcome.issues_inserted == 0


def test_source_disagreement_fires_when_fmp_vs_sec_diverge(conn: sqlite3.Connection) -> None:
    """FMP and SEC reporting different values for same (ticker, period_end, line_item) -> fire."""
    fmp = _doc(conn, "X", source_type="fmp")
    sec = _doc(conn, "X", source_type="sec_xbrl")
    _ff(conn, ticker="X", line_item="revenue", value=100, period_end=datetime(2024, 12, 31), source_doc_id=fmp)
    _ff(conn, ticker="X", line_item="revenue", value=200, period_end=datetime(2024, 12, 31), source_doc_id=sec)
    outcome = _check_source_disagreement(conn, run_id="r1", ticker="X")
    assert outcome.issues_inserted == 1


def test_run_all_checks_executes_every_rule(conn: sqlite3.Connection) -> None:
    """Smoke test: empty DB -> all 4 outcomes returned with 0 issues."""
    report = run_all_checks(conn, run_id="r1", ticker=None)
    assert len(report.outcomes) == 4
    assert all(o.issues_inserted == 0 for o in report.outcomes)
