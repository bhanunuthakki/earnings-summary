"""Tests for pipeline.segment_quarterly_coverage.record_coverage -- the
shared upsert writer both compute.segment_quarterly_10q (Phase 1's promoted
guards) and compute.segment_q4_derive (Phase 2) write through.

Covers: NULL-safe upsert for whole-filing-level rows (dim_type/dim_name
NULL), non-NULL per-segment upsert, and the degrade-gracefully no-op when
the table doesn't exist yet (pre-0168 schema / a synthetic fixture that
omits it).
"""

from __future__ import annotations

import sqlite3
import sys
from datetime import datetime
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from pipeline.segment_quarterly_coverage import (  # noqa: E402
    coverage_rows_for_ticker,
    record_coverage,
)

_SCHEMA = """
CREATE TABLE segment_quarterly_coverage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker VARCHAR(16) NOT NULL,
    period_end DATETIME NOT NULL,
    fiscal_period_type VARCHAR(8) NOT NULL,
    dim_type VARCHAR(16),
    dim_name VARCHAR(128),
    status VARCHAR(24) NOT NULL,
    reason_code VARCHAR(64),
    source_doc_id INTEGER,
    method_version VARCHAR(32),
    checked_at DATETIME NOT NULL
);
"""


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript(_SCHEMA)
    c.commit()
    return c


def test_whole_filing_level_upsert_is_null_safe(conn: sqlite3.Connection) -> None:
    """Two calls with dim_type=dim_name=None for the SAME logical key must
    update in place, not accumulate duplicate rows (the SQLite ON CONFLICT
    NULL-handling gotcha this module's docstring calls out)."""
    record_coverage(
        conn,
        ticker="TESTCO",
        period_end=datetime(2025, 12, 31),
        fiscal_period_type="Q4",
        dim_type=None,
        dim_name=None,
        status="tolerance_breach",
        reason_code="sum_exceeds_consolidated_revenue",
        source_doc_id=None,
        method_version="segment_q4_derive_v1",
    )
    record_coverage(
        conn,
        ticker="TESTCO",
        period_end=datetime(2025, 12, 31),
        fiscal_period_type="Q4",
        dim_type=None,
        dim_name=None,
        status="tolerance_breach",
        reason_code="sum_exceeds_consolidated_revenue",
        source_doc_id=42,
        method_version="segment_q4_derive_v1",
    )
    rows = coverage_rows_for_ticker(conn, "TESTCO")
    assert len(rows) == 1
    assert rows[0]["source_doc_id"] == 42  # second call updated in place


def test_per_segment_upsert_is_distinct_by_dim_name(conn: sqlite3.Connection) -> None:
    record_coverage(
        conn,
        ticker="TESTCO",
        period_end=datetime(2025, 12, 31),
        fiscal_period_type="Q4",
        dim_type="business_unit",
        dim_name="North America",
        status="not_computable",
        reason_code="missing_prior_anchor_for_subtraction",
        source_doc_id=1,
        method_version="segment_q4_derive_v1",
    )
    record_coverage(
        conn,
        ticker="TESTCO",
        period_end=datetime(2025, 12, 31),
        fiscal_period_type="Q4",
        dim_type="business_unit",
        dim_name="International",
        status="not_computable",
        reason_code="unmatched_segment_identity",
        source_doc_id=1,
        method_version="segment_q4_derive_v1",
    )
    rows = coverage_rows_for_ticker(conn, "TESTCO")
    assert len(rows) == 2
    dim_names = {r["dim_name"] for r in rows}
    assert dim_names == {"North America", "International"}


def test_missing_table_is_a_silent_noop() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    # No CREATE TABLE at all -- degrade-gracefully, must not raise.
    record_coverage(
        conn,
        ticker="TESTCO",
        period_end=datetime(2025, 12, 31),
        fiscal_period_type="Q4",
        dim_type=None,
        dim_name=None,
        status="not_computable",
        reason_code="missing_prior_anchor_for_subtraction",
        source_doc_id=None,
        method_version="segment_q4_derive_v1",
    )
    assert coverage_rows_for_ticker(conn, "TESTCO") == []
