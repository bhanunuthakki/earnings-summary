"""Tests for src/pipeline/kpi_persistence.py — KPI manifest validation, persistence, validation_issues."""

from __future__ import annotations

import sqlite3
from datetime import datetime
from decimal import Decimal

import pytest

from models.documents import SourceType
from models.facts import FiscalPeriodType, Unit
from models.validation import Severity, ValidationRule
from pipeline.kpi_persistence import (
    KpiExtractionManifest,
    KpiValue,
    PersistResult,
    find_or_create_kpi_definition,
    persist_manifest,
    record_validation_issue,
)


def _create_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
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


def test_find_or_create_kpi_definition_inserts_when_missing(conn: sqlite3.Connection) -> None:
    """First call inserts; second call returns the same id."""
    id1 = find_or_create_kpi_definition(
        conn, ticker="MELI", name="Revenue Growth (FXN)",
        unit=Unit.PERCENT, primary_source=SourceType.IR_DOC,
    )
    id2 = find_or_create_kpi_definition(
        conn, ticker="MELI", name="Revenue Growth (FXN)",
        unit=Unit.PERCENT, primary_source=SourceType.IR_DOC,
    )
    assert id1 == id2
    assert id1 > 0


def test_persist_manifest_inserts_kpi_facts(conn: sqlite3.Connection) -> None:
    """End-to-end: manifest in, kpi_facts rows out."""
    manifest = KpiExtractionManifest(
        ticker="MELI",
        period_end=datetime(2024, 12, 31),
        fiscal_period_type=FiscalPeriodType.Q4,
        source_doc_id=42,
        primary_source=SourceType.IR_DOC,
        values=[
            KpiValue(name="Revenue Growth (FXN)", value=Decimal("96"), unit=Unit.PERCENT),
            KpiValue(name="GMV Growth (FXN)", value=Decimal("56"), unit=Unit.PERCENT),
        ],
    )
    result = persist_manifest(conn, run_id="r1", manifest=manifest)
    assert isinstance(result, PersistResult)
    assert result.inserted == 2
    assert result.skipped_existing == 0
    assert result.validation_issues == 0

    rows = conn.execute("SELECT ticker, value, unit FROM kpi_facts").fetchall()
    assert len(rows) == 2
    values = {Decimal(str(dict(r)["value"])) for r in rows}
    assert values == {Decimal("96"), Decimal("56")}


def test_persist_manifest_dedupes_on_rerun(conn: sqlite3.Connection) -> None:
    """Re-running the same manifest is a no-op (UNIQUE index dedupes)."""
    manifest = KpiExtractionManifest(
        ticker="MELI",
        period_end=datetime(2024, 12, 31),
        fiscal_period_type=FiscalPeriodType.Q4,
        source_doc_id=42,
        values=[KpiValue(name="OpMargin", value=Decimal("13.5"), unit=Unit.PERCENT)],
    )
    persist_manifest(conn, run_id="r1", manifest=manifest)
    second = persist_manifest(conn, run_id="r2", manifest=manifest)
    assert second.inserted == 0
    assert second.skipped_existing == 1


def test_persist_manifest_emits_validation_issue_on_out_of_range(conn: sqlite3.Connection) -> None:
    """A nonsense PERCENT (e.g. 5000) is rejected, validation_issue recorded, kpi_fact NOT inserted."""
    manifest = KpiExtractionManifest(
        ticker="NU",
        period_end=datetime(2025, 12, 31),
        fiscal_period_type=FiscalPeriodType.Q4,
        source_doc_id=99,
        values=[
            KpiValue(name="Activity Rate", value=Decimal("83"), unit=Unit.PERCENT),
            KpiValue(name="Bogus", value=Decimal("5000"), unit=Unit.PERCENT),
        ],
    )
    result = persist_manifest(conn, run_id="r1", manifest=manifest)
    assert result.inserted == 1
    assert result.validation_issues == 1

    issues = conn.execute(
        "SELECT severity, rule, raw_value, ticker FROM validation_issues"
    ).fetchall()
    assert len(issues) == 1
    issue = dict(issues[0])
    assert issue["severity"] == Severity.WARN.value
    assert issue["rule"] == ValidationRule.PLAUSIBLE_RANGE.value
    assert issue["ticker"] == "NU"


def test_record_validation_issue_inserts_row(conn: sqlite3.Connection) -> None:
    """Direct call to record_validation_issue writes a row with the given fields."""
    issue_id = record_validation_issue(
        conn,
        run_id="run-x",
        source_doc_id=100,
        ticker="GOOG",
        severity=Severity.HALT,
        rule=ValidationRule.SOURCE_DISAGREEMENT,
        raw_value="fmp=10, sec=11",
        expected="diff < 0.5%",
    )
    assert issue_id > 0
    row = conn.execute("SELECT * FROM validation_issues WHERE id = ?", (issue_id,)).fetchone()
    assert dict(row)["severity"] == "halt"
    assert dict(row)["rule"] == "source_disagreement"


def test_kpi_value_rejects_invalid_confidence() -> None:
    """KpiValue.confidence must be in [0, 1]."""
    with pytest.raises(ValueError):
        KpiValue(name="x", value=Decimal("1"), unit=Unit.PERCENT, confidence=1.5)


def test_kpi_value_rejects_empty_name() -> None:
    """KpiValue.name must be non-empty."""
    with pytest.raises(ValueError):
        KpiValue(name="", value=Decimal("1"), unit=Unit.PERCENT)
