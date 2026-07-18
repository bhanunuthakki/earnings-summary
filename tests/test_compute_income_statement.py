"""Tests for src/compute/income_statement.py."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path

import pytest

from compute.income_statement import (
    extract_facts_from_record,
    extract_income_statement_facts,
)
from models.facts import Currency, FiscalPeriodType, Unit
from models.fmp_payloads import FmpIncomeStatementRecord

_SAMPLE_RECORD: dict[str, object] = {
    "date": "2025-12-31",
    "symbol": "GOOG",
    "reportedCurrency": "USD",
    "fiscalYear": "2025",
    "period": "FY",
    "revenue": 402_963_000_000,
    "costOfRevenue": 162_535_000_000,
    "grossProfit": 240_428_000_000,
    "operatingIncome": 129_166_000_000,
    "netIncome": 132_170_000_000,
    "ebit": 129_166_000_000,
    "ebitda": 165_000_000_000,
    "eps": 10.97,
    "epsDiluted": 10.85,
    "weightedAverageShsOut": 12_050_000_000,
    "weightedAverageShsOutDil": 12_180_000_000,
}


def _create_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE documents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL,
            source_type TEXT NOT NULL,
            doc_type TEXT NOT NULL,
            period_end TIMESTAMP,
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
            confidence REAL NOT NULL DEFAULT 1.0
        );
        CREATE UNIQUE INDEX uq_financial_facts_provenance
        ON financial_facts (ticker, period_end, fiscal_period_type, line_item, source_doc_id);
        """
    )
    conn.commit()


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    _create_schema(c)
    return c


def test_extract_facts_from_canonical_record() -> None:
    """A clean record produces one fact row per known line item with right unit/currency."""
    record = FmpIncomeStatementRecord.model_validate(_SAMPLE_RECORD)
    facts = extract_facts_from_record(record, source_doc_id=42, record_index=0)

    by_line = {f.line_item: f for f in facts}
    assert by_line["revenue"].value == 402_963_000_000
    assert by_line["revenue"].currency == Currency.USD
    assert by_line["revenue"].unit == Unit.ACTUAL
    assert by_line["revenue"].source_doc_id == 42

    assert by_line["weighted_avg_shares"].unit == Unit.COUNT
    assert by_line["weighted_avg_shares"].currency is None

    assert by_line["eps"].unit == Unit.ACTUAL
    assert float(by_line["eps"].value) == pytest.approx(10.97)

    assert by_line["revenue"].fiscal_period_type == FiscalPeriodType.FY
    assert by_line["revenue"].period_end == datetime(2025, 12, 31)
    assert by_line["revenue"].ticker == "GOOG"


def test_extract_facts_skips_none_fields() -> None:
    """Fields not present in the record are skipped (no fact row)."""
    minimal = {
        "date": "2025-12-31",
        "symbol": "X",
        "reportedCurrency": "USD",
        "period": "FY",
        "revenue": 100,
    }
    record = FmpIncomeStatementRecord.model_validate(minimal)
    facts = extract_facts_from_record(record, source_doc_id=1, record_index=0)
    line_items = {f.line_item for f in facts}
    assert "revenue" in line_items
    assert "ebitda" not in line_items
    assert "eps" not in line_items


def test_unknown_currency_yields_none() -> None:
    """A reportedCurrency outside our Currency enum yields currency=None (not raise)."""
    record_data = {**_SAMPLE_RECORD, "reportedCurrency": "ZZZ"}
    record = FmpIncomeStatementRecord.model_validate(record_data)
    facts = extract_facts_from_record(record, source_doc_id=1, record_index=0)
    assert all(f.currency is None for f in facts if f.unit == Unit.ACTUAL)


def test_invalid_period_raises() -> None:
    """An unrecognized fiscal period (e.g. 'X1') raises during extraction."""
    record_data = {**_SAMPLE_RECORD, "period": "X1"}
    record = FmpIncomeStatementRecord.model_validate(record_data)
    with pytest.raises(ValueError, match="X1"):
        extract_facts_from_record(record, source_doc_id=1, record_index=0)


def test_extract_income_statement_facts_writes_rows(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """End-to-end: write a sample JSON, register a documents row, run extractor."""
    sample_path = tmp_path / "data" / "historical" / "fmp"
    sample_path.mkdir(parents=True)
    file_path = sample_path / "GOOG_income_statement_annual.json"
    with open(file_path, "w") as f:
        json.dump([_SAMPLE_RECORD], f)

    conn.execute(
        "INSERT INTO documents "
        "(ticker, source_type, doc_type, file_path, sha256, fetched_at, fetch_status, raw_bytes_size) "
        "VALUES ('GOOG', 'fmp', 'fmp_income_statement', "
        "'data/historical/fmp/GOOG_income_statement_annual.json', "
        "'a' || replace(hex(randomblob(31)), 'a', 'b'), ?, 'ok', 1000)",
        (datetime.now(),),
    )
    conn.commit()
    cur = conn.execute("SELECT id FROM documents")
    document_id = cur.fetchone()["id"]

    inserted = extract_income_statement_facts(conn, document_id, project_root=tmp_path)
    assert inserted >= 11  # all the canonical fields present in sample

    rows = conn.execute(
        "SELECT line_item, value, currency, unit FROM financial_facts WHERE ticker = 'GOOG'"
    ).fetchall()
    by_line = {r["line_item"]: r for r in rows}
    assert by_line["revenue"]["currency"] == "USD"
    assert by_line["weighted_avg_shares"]["unit"] == "count"


def test_extract_income_statement_facts_idempotent(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """Re-running on the same document produces zero new rows."""
    sample_path = tmp_path / "data" / "historical" / "fmp"
    sample_path.mkdir(parents=True)
    file_path = sample_path / "GOOG_income_statement_annual.json"
    with open(file_path, "w") as f:
        json.dump([_SAMPLE_RECORD], f)

    conn.execute(
        "INSERT INTO documents "
        "(ticker, source_type, doc_type, file_path, sha256, fetched_at, fetch_status, raw_bytes_size) "
        "VALUES ('GOOG', 'fmp', 'fmp_income_statement', "
        "'data/historical/fmp/GOOG_income_statement_annual.json', "
        "'b' || replace(hex(randomblob(31)), 'a', 'c'), ?, 'ok', 1000)",
        (datetime.now(),),
    )
    conn.commit()
    document_id = conn.execute("SELECT id FROM documents").fetchone()["id"]

    first = extract_income_statement_facts(conn, document_id, project_root=tmp_path)
    second = extract_income_statement_facts(conn, document_id, project_root=tmp_path)
    assert first > 0
    assert second == 0


def test_extract_income_statement_facts_rejects_wrong_doc_type(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """Calling with a non-income-statement document_id raises."""
    conn.execute(
        "INSERT INTO documents "
        "(ticker, source_type, doc_type, file_path, sha256, fetched_at, fetch_status, raw_bytes_size) "
        "VALUES ('GOOG', 'fmp', 'fmp_balance_sheet', 'x', "
        "'c' || replace(hex(randomblob(31)), 'a', 'd'), ?, 'ok', 1000)",
        (datetime.now(),),
    )
    conn.commit()
    document_id = conn.execute("SELECT id FROM documents").fetchone()["id"]
    with pytest.raises(ValueError, match="not 'fmp_income_statement'"):
        extract_income_statement_facts(conn, document_id, project_root=tmp_path)
