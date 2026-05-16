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
    purge_duplicate_kpi_facts,
    record_validation_issue,
)


_KPI_FACTS_LOGICAL_UNIQUE = (
    "CREATE UNIQUE INDEX uq_kpi_facts_logical "
    "ON kpi_facts (ticker, period_end, fiscal_period_type, kpi_definition_id)"
)
_KPI_FACTS_PROVENANCE_UNIQUE = (
    "CREATE UNIQUE INDEX uq_kpi_facts_provenance "
    "ON kpi_facts (ticker, period_end, fiscal_period_type, kpi_definition_id, source_doc_id)"
)


def _create_schema(conn: sqlite3.Connection, *, legacy_unique: bool = False) -> None:
    """Build the kpi_* + validation_issues tables.

    `legacy_unique=True` recreates the pre-0030 wider unique index (kept for the
    purge test, which needs to set up duplicate rows that the post-0030 schema
    would reject outright)."""
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
    conn.execute(_KPI_FACTS_PROVENANCE_UNIQUE if legacy_unique else _KPI_FACTS_LOGICAL_UNIQUE)
    conn.commit()


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    _create_schema(c)
    return c


@pytest.fixture
def legacy_conn() -> sqlite3.Connection:
    """Pre-0030 schema: the wider unique index allows different source_doc_id
    rows for the same logical KPI period (the very state the purge cleans up)."""
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    _create_schema(c, legacy_unique=True)
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


def test_persist_manifest_skips_different_source_doc_id(conn: sqlite3.Connection) -> None:
    """Post-0030 narrower UNIQUE: a second run with a fresher source_doc_id for
    the same logical KPI period must NOT land a second row (this is the bug from
    the RBRK 2026-05-13 report — duplicate "Latest" observations). The
    `INSERT OR IGNORE` in `_insert_kpi_fact` is what enforces DO NOTHING here."""
    first = KpiExtractionManifest(
        ticker="RBRK",
        period_end=datetime(2026, 1, 31),
        fiscal_period_type=FiscalPeriodType.Q4,
        source_doc_id=9676,
        values=[KpiValue(name="Revenue YoY Growth (USD)", value=Decimal("46.33"), unit=Unit.PERCENT)],
    )
    second = first.model_copy(update={"source_doc_id": 9705})

    persist_manifest(conn, run_id="run-a", manifest=first)
    result = persist_manifest(conn, run_id="run-b", manifest=second)

    assert result.inserted == 0
    assert result.skipped_existing == 1

    rows = conn.execute(
        "SELECT source_doc_id FROM kpi_facts WHERE ticker = 'RBRK'"
    ).fetchall()
    assert len(rows) == 1
    # First-write wins (DO NOTHING); the original source_doc_id stays in place.
    assert dict(rows[0])["source_doc_id"] == 9676


def test_purge_duplicate_kpi_facts_keeps_max_source_doc_id(legacy_conn: sqlite3.Connection) -> None:
    """Backfill purge keeps exactly one row per (ticker, period_end,
    fiscal_period_type, kpi_definition_id) — the one with the highest
    source_doc_id (most-recently-ingested document). Mirrors the prod
    RBRK Revenue YoY case (source_doc_id 9676 vs 9705 → keep 9705)."""
    kpi_def_id = find_or_create_kpi_definition(
        legacy_conn, ticker="RBRK", name="Revenue YoY Growth (USD)",
        unit=Unit.PERCENT, primary_source=SourceType.IR_DOC,
    )

    rows = [
        # Two source_doc_ids for the same logical (RBRK, 2026-01-31, Q4, Revenue YoY)
        ("RBRK", datetime(2026, 1, 31), "Q4", kpi_def_id, "46.33", "percent", 9676),
        ("RBRK", datetime(2026, 1, 31), "Q4", kpi_def_id, "46.33", "percent", 9705),
        # Three for an older quarter to ensure the purge generalises beyond pairs
        ("RBRK", datetime(2025, 10, 31), "Q3", kpi_def_id, "48.26", "percent", 9600),
        ("RBRK", datetime(2025, 10, 31), "Q3", kpi_def_id, "48.26", "percent", 9676),
        ("RBRK", datetime(2025, 10, 31), "Q3", kpi_def_id, "48.26", "percent", 9705),
        # A row that should be left alone (no duplicates)
        ("RBRK", datetime(2025, 7, 31), "Q2", kpi_def_id, "51.19", "percent", 9705),
    ]
    legacy_conn.executemany(
        "INSERT INTO kpi_facts "
        "(ticker, period_end, fiscal_period_type, kpi_definition_id, value, unit, source_doc_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    legacy_conn.commit()
    assert legacy_conn.execute("SELECT COUNT(*) FROM kpi_facts").fetchone()[0] == 6

    deleted = purge_duplicate_kpi_facts(legacy_conn)
    legacy_conn.commit()
    assert deleted == 3  # 1 from the Q4 pair + 2 from the Q3 triple

    survivors = legacy_conn.execute(
        "SELECT period_end, source_doc_id FROM kpi_facts ORDER BY period_end DESC"
    ).fetchall()
    assert [(str(dict(r)["period_end"]), dict(r)["source_doc_id"]) for r in survivors] == [
        ("2026-01-31 00:00:00", 9705),
        ("2025-10-31 00:00:00", 9705),
        ("2025-07-31 00:00:00", 9705),
    ]


def test_purge_duplicate_kpi_facts_is_idempotent(legacy_conn: sqlite3.Connection) -> None:
    """Calling purge on an already-clean table is a no-op (returns 0)."""
    kpi_def_id = find_or_create_kpi_definition(
        legacy_conn, ticker="MELI", name="Revenue Growth (FXN)",
        unit=Unit.PERCENT, primary_source=SourceType.IR_DOC,
    )
    legacy_conn.execute(
        "INSERT INTO kpi_facts "
        "(ticker, period_end, fiscal_period_type, kpi_definition_id, value, unit, source_doc_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("MELI", datetime(2025, 12, 31), "Q4", kpi_def_id, "96.0", "percent", 100),
    )
    legacy_conn.commit()

    assert purge_duplicate_kpi_facts(legacy_conn) == 0
    assert legacy_conn.execute("SELECT COUNT(*) FROM kpi_facts").fetchone()[0] == 1
