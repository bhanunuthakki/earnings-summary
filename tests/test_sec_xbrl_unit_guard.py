"""SEC XBRL unit sanity guard (reader-tier-hardening PR3).

``insert_facts_from_companyfacts`` stamps every fact ACTUAL (or COUNT for share
counts). The guard at the persist point flags any value whose resolved unit is
NEITHER — an UNIT_MISMATCH validation issue — instead of silently persisting a
wrong unit. Today the resolved unit is always in {actual, count} by construction,
so the guard never fires on real payloads; these tests exercise the writer
directly and assert normal ingest stays clean.
"""

from __future__ import annotations

import sqlite3

import pipeline.sec_xbrl as sec_xbrl
from models.validation import ValidationRule

_SANE_SEC_UNITS = sec_xbrl._SANE_SEC_UNITS  # pyright: ignore[reportPrivateUsage]
_flag_non_actual_unit = sec_xbrl._flag_non_actual_unit  # pyright: ignore[reportPrivateUsage]

_DDL = """
CREATE TABLE validation_issues (
    id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL,
    source_doc_id INTEGER, ticker TEXT, severity TEXT NOT NULL, rule TEXT NOT NULL,
    raw_value TEXT, expected TEXT, raised_at TIMESTAMP NOT NULL,
    resolved_at TIMESTAMP, resolved_by TEXT, resolution_note TEXT
);
"""


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(_DDL)
    return conn


def test_sane_units_are_only_actual_and_count() -> None:
    assert set(_SANE_SEC_UNITS) == {"actual", "count"}


def test_flag_writes_unit_mismatch_issue() -> None:
    conn = _db()
    try:
        _flag_non_actual_unit(
            conn,
            run_id="r1",
            ticker="TST",
            line_item="revenue",
            period_end="2026-03-31",
            unit_code="pure",
            resolved_unit="ratio",
            source_doc_id=42,
        )
        conn.commit()
        row = conn.execute(
            "SELECT ticker, rule, severity, raw_value, expected, source_doc_id "
            "FROM validation_issues WHERE resolved_at IS NULL"
        ).fetchone()
        assert row is not None
        assert row["ticker"] == "TST"
        assert row["rule"] == ValidationRule.UNIT_MISMATCH.value
        assert row["severity"] == "warn"
        assert "pure" in row["raw_value"]
        assert "ratio" in row["raw_value"]
        assert row["source_doc_id"] == 42
        assert row["expected"] == "unit in {actual, count}"
    finally:
        conn.close()


def test_flag_is_best_effort_without_table() -> None:
    """A DB with no validation_issues table must not raise — ingest continues."""
    conn = sqlite3.connect(":memory:")
    try:
        # No table created — the guard swallows the sqlite error.
        _flag_non_actual_unit(
            conn,
            run_id="r1",
            ticker="TST",
            line_item="revenue",
            period_end="2026-03-31",
            unit_code="pure",
            resolved_unit="ratio",
            source_doc_id=1,
        )
    finally:
        conn.close()
