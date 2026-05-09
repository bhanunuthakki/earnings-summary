"""Persist structured KPI extractions from IR documents into kpi_facts + validation_issues.

This module is the interface between (a) an LLM-extracted manifest of KPI values
and (b) the provenance-tagged kpi_facts table. The LLM produces a typed payload
(KpiExtractionManifest); this module validates it, looks up / creates the
matching kpi_definitions row per (ticker, name), inserts kpi_facts rows tagged
with source_doc_id, and writes validation_issues for anything that fails
sanity checks (range bounds, missing units, etc.).

Single source of truth for the data contract — no other module should INSERT
directly into kpi_facts.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, Field

from models.documents import SourceType
from models.facts import FiscalPeriodType, Unit
from models.kpis import ThesisTier
from models.validation import Severity, ValidationRule


class KpiValue(BaseModel):
    """One LLM-extracted KPI value tied to a tracked metric."""

    name: str = Field(min_length=1, max_length=200)
    value: Decimal
    unit: Unit
    confidence: float = Field(ge=0.0, le=1.0, default=0.95)


class KpiExtractionManifest(BaseModel):
    """A batch of KPI values extracted from one IR document.

    `source_doc_id` is the documents.id of the IR PDF the LLM read; provenance
    chains every emitted kpi_facts row back to that document.
    """

    ticker: str
    period_end: datetime
    fiscal_period_type: FiscalPeriodType
    source_doc_id: int
    primary_source: SourceType = SourceType.IR_DOC
    values: list[KpiValue]


def _find_kpi_definition(
    conn: sqlite3.Connection, ticker: str, name: str
) -> int | None:
    """Return kpi_definitions.id for (ticker, name), or None if not registered."""
    cur = conn.execute(
        "SELECT id FROM kpi_definitions WHERE ticker = ? AND name = ?",
        (ticker.upper(), name),
    )
    row = cur.fetchone()
    return int(row["id"]) if row is not None else None


def _create_kpi_definition(
    conn: sqlite3.Connection,
    *,
    ticker: str,
    name: str,
    unit: Unit,
    primary_source: SourceType,
    threshold_tier: ThesisTier | None = None,
) -> int:
    """Register a new kpi_definitions row; returns the new id."""
    cur = conn.execute(
        "INSERT INTO kpi_definitions "
        "(ticker, name, unit, primary_source, fallback_source, ir_url, "
        " threshold_tier, threshold_low, threshold_high, notes) "
        "VALUES (?, ?, ?, ?, NULL, NULL, ?, NULL, NULL, NULL)",
        (
            ticker.upper(),
            name,
            unit.value,
            primary_source.value,
            threshold_tier.value if threshold_tier is not None else None,
        ),
    )
    return int(cur.lastrowid) if cur.lastrowid is not None else 0


def find_or_create_kpi_definition(
    conn: sqlite3.Connection,
    *,
    ticker: str,
    name: str,
    unit: Unit,
    primary_source: SourceType,
) -> int:
    """Lookup-or-insert kpi_definitions for (ticker, name); returns the id."""
    existing = _find_kpi_definition(conn, ticker, name)
    if existing is not None:
        return existing
    return _create_kpi_definition(
        conn, ticker=ticker, name=name, unit=unit, primary_source=primary_source
    )


def _insert_kpi_fact(
    conn: sqlite3.Connection,
    *,
    ticker: str,
    period_end: datetime,
    fiscal_period_type: FiscalPeriodType,
    kpi_definition_id: int,
    value: Decimal,
    unit: Unit,
    source_doc_id: int,
) -> bool:
    """Insert one kpi_facts row. Returns True if a new row was inserted."""
    cur = conn.execute(
        "INSERT OR IGNORE INTO kpi_facts "
        "(ticker, period_end, fiscal_period_type, kpi_definition_id, "
        " value, unit, source_doc_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            ticker.upper(),
            period_end,
            fiscal_period_type.value,
            kpi_definition_id,
            str(value),
            unit.value,
            source_doc_id,
        ),
    )
    return cur.rowcount > 0


def record_validation_issue(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    source_doc_id: int | None,
    ticker: str | None,
    severity: Severity,
    rule: ValidationRule,
    raw_value: str | None,
    expected: str | None,
) -> int:
    """Insert one validation_issues row; returns its id."""
    cur = conn.execute(
        "INSERT INTO validation_issues "
        "(run_id, source_doc_id, ticker, severity, rule, raw_value, expected, "
        " raised_at, resolved_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL)",
        (
            run_id,
            source_doc_id,
            ticker.upper() if ticker is not None else None,
            severity.value,
            rule.value,
            raw_value,
            expected,
            datetime.now(),
        ),
    )
    return int(cur.lastrowid) if cur.lastrowid is not None else 0


def _validate_value_range(value: Decimal, unit: Unit) -> tuple[bool, str | None]:
    """Sanity-check a (value, unit) pair. Returns (is_ok, reason_if_not)."""
    if unit is Unit.PERCENT and (value < Decimal("-1000") or value > Decimal("1000")):
        return (False, f"percent={value} outside plausible range [-1000, 1000]")
    if unit is Unit.RATIO and (value < Decimal("-100") or value > Decimal("100")):
        return (False, f"ratio={value} outside plausible range [-100, 100]")
    if unit is Unit.BPS and (value < Decimal("-100000") or value > Decimal("100000")):
        return (False, f"bps={value} outside plausible range")
    return (True, None)


class PersistResult(BaseModel):
    """Per-manifest application outcome."""

    inserted: int
    skipped_existing: int
    validation_issues: int


def persist_manifest(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    manifest: KpiExtractionManifest,
) -> PersistResult:
    """Apply one KpiExtractionManifest. Validates each value, inserts kpi_facts,
    emits validation_issues for failures, and returns a per-manifest tally."""
    inserted = 0
    skipped = 0
    issues = 0

    for kpi in manifest.values:
        ok, reason = _validate_value_range(kpi.value, kpi.unit)
        if not ok:
            record_validation_issue(
                conn,
                run_id=run_id,
                source_doc_id=manifest.source_doc_id,
                ticker=manifest.ticker,
                severity=Severity.WARN,
                rule=ValidationRule.PLAUSIBLE_RANGE,
                raw_value=f"{kpi.name}={kpi.value} {kpi.unit.value}",
                expected=reason,
            )
            issues += 1
            continue

        kpi_def_id = find_or_create_kpi_definition(
            conn,
            ticker=manifest.ticker,
            name=kpi.name,
            unit=kpi.unit,
            primary_source=manifest.primary_source,
        )
        was_inserted = _insert_kpi_fact(
            conn,
            ticker=manifest.ticker,
            period_end=manifest.period_end,
            fiscal_period_type=manifest.fiscal_period_type,
            kpi_definition_id=kpi_def_id,
            value=kpi.value,
            unit=kpi.unit,
            source_doc_id=manifest.source_doc_id,
        )
        if was_inserted:
            inserted += 1
        else:
            skipped += 1

    conn.commit()
    return PersistResult(
        inserted=inserted, skipped_existing=skipped, validation_issues=issues
    )
