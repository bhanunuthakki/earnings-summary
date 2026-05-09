"""Tests for src/pipeline/refresh_eval.py — auto-trigger derive + evaluate chain."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path

import pytest

from models.kpis import BreachStatus
from pipeline.refresh_eval import refresh_for_tickers


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
            value NUMERIC(24, 6) NOT NULL,
            currency TEXT,
            unit TEXT NOT NULL,
            source_doc_id INTEGER NOT NULL,
            confidence REAL DEFAULT 1.0
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
            threshold_low REAL,
            threshold_high REAL,
            notes TEXT,
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
            source_doc_id INTEGER NOT NULL
        );
        CREATE UNIQUE INDEX uq_kpi_facts_provenance
        ON kpi_facts (ticker, period_end, fiscal_period_type, kpi_definition_id, source_doc_id);
        CREATE TABLE thesis_state (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL UNIQUE,
            thesis TEXT,
            last_updated TIMESTAMP,
            breach_status TEXT,
            raw_json TEXT NOT NULL,
            ingested_at TIMESTAMP NOT NULL
        );
        CREATE TABLE thesis_evaluations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL,
            evaluated_at TIMESTAMP NOT NULL,
            overall_status TEXT NOT NULL,
            rule_evaluations_json TEXT NOT NULL,
            run_id TEXT
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


def _seed_facts(conn: sqlite3.Connection, ticker: str) -> None:
    """Seed enough financial_facts to allow derivation."""
    cur = conn.execute(
        "INSERT INTO documents (ticker, source_type, doc_type, file_path, "
        "sha256, fetched_at, fetch_status, raw_bytes_size) "
        "VALUES (?, 'fmp', 'fmp_income_statement', ?, ?, ?, 'ok', 1)",
        (ticker, f"data/historical/fmp/{ticker}_income_statement_quarterly.json",
         "a" * 64, datetime.now()),
    )
    doc_id = int(cur.lastrowid) if cur.lastrowid is not None else 0
    pe = datetime(2024, 12, 31)
    for line, val in [("revenue", 1000), ("operating_income", 200), ("net_income", 150), ("gross_profit", 500)]:
        conn.execute(
            "INSERT INTO financial_facts (ticker, period_end, fiscal_period_type, "
            "line_item, value, currency, unit, source_doc_id) "
            "VALUES (?, ?, 'Q4', ?, ?, 'USD', 'actual', ?)",
            (ticker, pe, line, str(val), doc_id),
        )
    conn.execute(
        "INSERT INTO thesis_state (ticker, raw_json, ingested_at) VALUES (?, '{}', ?)",
        (ticker, datetime.now()),
    )
    conn.commit()


def _write_holdings(tmp_path: Path, ticker: str, threshold: float = 0) -> None:
    payload = {
        "ticker": ticker,
        "thesis": "test",
        "break_rules": [
            {
                "rule_id": "op_margin_below",
                "kpi_name": "Operating Margin (GAAP)",
                "comparator": "lt",
                "threshold": threshold,
                "unit": "percent",
                "consecutive_periods": 1,
                "narrative": f"OpMargin < {threshold}",
            },
        ],
    }
    (tmp_path / f"{ticker}.json").write_text(json.dumps(payload), encoding="utf-8")


def test_refresh_for_tickers_derives_and_evaluates(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """End-to-end: facts in, derived KPIs + evaluation appear."""
    _seed_facts(conn, "X")
    _write_holdings(tmp_path, "X", threshold=0)

    results = refresh_for_tickers(conn, tickers=["X"], holdings_dir=tmp_path)
    assert len(results) == 1
    assert results[0].ticker == "X"
    assert results[0].derived_kpi_rows_inserted >= 3  # 3 margins (no YoY without prior year)
    assert results[0].eval_status == BreachStatus.OK  # 200/1000 = 20% margin, > 0% threshold

    # thesis_evaluations row was appended
    row = conn.execute(
        "SELECT overall_status FROM thesis_evaluations WHERE ticker = 'X'"
    ).fetchone()
    assert row is not None
    assert dict(row)["overall_status"] == "ok"


def test_refresh_for_tickers_skips_missing_holdings(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """Ticker without a holdings JSON returns eval_error='holdings_spec_not_found' but doesn't raise."""
    _seed_facts(conn, "Y")
    # Don't write holdings/Y.json

    results = refresh_for_tickers(conn, tickers=["Y"], holdings_dir=tmp_path)
    assert len(results) == 1
    assert results[0].eval_status is None
    assert results[0].eval_error == "holdings_spec_not_found"
    # Derive still ran successfully
    assert results[0].derived_kpi_rows_inserted >= 3


def test_refresh_for_tickers_breach_propagates(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """If derived margin is below threshold, eval_status = BREACH."""
    _seed_facts(conn, "Z")
    _write_holdings(tmp_path, "Z", threshold=50)  # threshold higher than actual 20%

    results = refresh_for_tickers(conn, tickers=["Z"], holdings_dir=tmp_path)
    assert results[0].eval_status == BreachStatus.BREACH
