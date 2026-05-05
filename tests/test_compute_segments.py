"""Tests for src/compute/segments.py."""

from __future__ import annotations

import sqlite3
from datetime import datetime
from decimal import Decimal

import pytest

from compute.segments import extract_facts_from_record, extract_segment_facts
from models.facts import Currency, FiscalPeriodType, Unit
from models.fmp_payloads import FmpSegmentRecord

_PRODUCT_SAMPLE = {
    "date": "2025-12-31",
    "symbol": "GOOG",
    "reportedCurrency": "USD",
    "period": "FY",
    "fiscalYear": 2025,
    "data": {
        "Google Search & Other": 224_532_000_000,
        "YouTube Advertising Revenue": 40_367_000_000,
        "Google Cloud": 58_705_000_000,
        "Other Bets": 1_537_000_000,
    },
}

_GEO_SAMPLE = {
    "date": "2025-12-31",
    "symbol": "GOOG",
    "reportedCurrency": "USD",
    "period": "FY",
    "fiscalYear": 2025,
    "data": {
        "UNITED STATES": 194_229_000_000,
        "EMEA": 117_152_000_000,
        "Asia Pacific": 67_680_000_000,
    },
}


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
        CREATE TABLE segment_facts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL,
            period_end TIMESTAMP NOT NULL,
            fiscal_period_type TEXT NOT NULL,
            segment_name TEXT NOT NULL,
            metric TEXT NOT NULL,
            value NUMERIC(24, 6) NOT NULL,
            currency TEXT,
            unit TEXT NOT NULL,
            source_doc_id INTEGER NOT NULL
        );
        CREATE UNIQUE INDEX uq_segment_facts_provenance
        ON segment_facts (ticker, period_end, fiscal_period_type, segment_name, metric, source_doc_id);
        """
    )
    conn.commit()


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    _create_schema(c)
    return c


def test_extract_product_segment_facts() -> None:
    """One product segment record produces one row per segment with metric=revenue_by_product."""
    record = FmpSegmentRecord.model_validate(_PRODUCT_SAMPLE)
    facts = extract_facts_from_record(record, source_doc_id=10, metric="revenue_by_product")

    assert len(facts) == 4
    by_segment = {f.segment_name: f for f in facts}
    assert by_segment["Google Cloud"].value == Decimal("58705000000")
    assert by_segment["Google Cloud"].metric == "revenue_by_product"
    assert by_segment["Google Cloud"].currency == Currency.USD
    assert by_segment["Google Cloud"].unit == Unit.ACTUAL
    assert by_segment["Google Cloud"].fiscal_period_type == FiscalPeriodType.FY
    assert by_segment["Google Cloud"].period_end == datetime(2025, 12, 31)


def test_extract_geographic_segment_facts() -> None:
    """One geographic segment record produces one row per region."""
    record = FmpSegmentRecord.model_validate(_GEO_SAMPLE)
    facts = extract_facts_from_record(record, source_doc_id=11, metric="revenue_by_geography")

    assert len(facts) == 3
    segment_names = {f.segment_name for f in facts}
    assert "UNITED STATES" in segment_names
    assert "EMEA" in segment_names
    assert all(f.metric == "revenue_by_geography" for f in facts)


def test_extract_segment_facts_rejects_wrong_doc_type(
    conn: sqlite3.Connection,
) -> None:
    """Calling on a non-segment document_id raises."""
    conn.execute(
        "INSERT INTO documents "
        "(ticker, source_type, doc_type, file_path, sha256, fetched_at, fetch_status, raw_bytes_size) "
        "VALUES ('GOOG', 'fmp', 'fmp_income_statement', 'x', 'a' || replace(hex(randomblob(31)), 'a', 'b'), ?, 'ok', 1)",
        (datetime.now(),),
    )
    conn.commit()
    document_id = conn.execute("SELECT id FROM documents").fetchone()["id"]
    from pathlib import Path

    with pytest.raises(ValueError, match="not one of"):
        extract_segment_facts(conn, document_id, project_root=Path("."))
