"""Shared helpers for compute/* extractors that produce financial_facts rows.

Each extractor (income_statement, balance_sheet, cashflow, ...) plugs a
(fmp_field, canonical_line_item, unit) spec into `extract_facts_with_spec`.
The spec is a list — order doesn't matter for correctness, only for sort
stability of the returned list. Adding a line item is one tuple in the spec.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Protocol

from models.facts import Currency, FinancialFact, FiscalPeriodType, Unit
from pipeline.restatement_detector import insert_with_restatement_detection


class FmpStatementRecordLike(Protocol):
    """Shape shared by FMP income / balance / cashflow records."""

    date: str
    symbol: str
    period: str
    reportedCurrency: str | None


def parse_currency(s: str | None) -> Currency | None:
    """Coerce reportedCurrency to Currency enum, or None if absent or unknown."""
    if s is None:
        return None
    try:
        return Currency(s)
    except ValueError:
        return None


def extract_facts_with_spec(
    record: FmpStatementRecordLike,
    source_doc_id: int,
    line_item_spec: list[tuple[str, str, Unit]],
    period_type_override: FiscalPeriodType | None = None,
) -> list[FinancialFact]:
    """Convert a record to FinancialFact rows using a (fmp_field, canonical, unit) spec.

    Skips None values (field not present in record). Currency comes from the
    record's reportedCurrency field; Unit.COUNT facts get currency=None.

    `period_type_override`: when set, overrides the FMP `period` field. Used by
    the TTM extractor path: FMP's `_ttm.json` files have period='Q1'/'Q3'/etc
    (the latest quarter ending the trailing window) but the value is TTM —
    callers detect that via file_path and pass FiscalPeriodType.TTM here.
    """
    period_end = datetime.fromisoformat(record.date)
    period_type = period_type_override if period_type_override is not None else FiscalPeriodType(record.period)
    currency = parse_currency(record.reportedCurrency)

    facts: list[FinancialFact] = []
    for fmp_field, canonical, unit in line_item_spec:
        value = getattr(record, fmp_field)
        if value is None:
            continue
        facts.append(
            FinancialFact(
                ticker=record.symbol.upper(),
                period_end=period_end,
                fiscal_period_type=period_type,
                line_item=canonical,
                value=Decimal(str(value)),
                currency=currency if unit is Unit.ACTUAL else None,
                unit=unit,
                source_doc_id=source_doc_id,
                confidence=1.0,
            )
        )
    return facts


def insert_financial_facts(
    conn: sqlite3.Connection,
    facts: list[FinancialFact],
    *,
    extracted_by: str,
) -> int:
    """Bulk-insert facts. Returns rowcount actually inserted.

    Each row is written via `insert_with_restatement_detection` so that when a
    later filing (e.g. 10-K restating a Q1 value previously sourced from a
    10-Q) lands on an existing logical key, the new row links back to the
    incumbent via `supersedes_id`. The UNIQUE index on
    (ticker, period_end, fiscal_period_type, line_item, source_doc_id) still
    dedupes same-document replays.

    `extracted_by` is required — the audit-trail column is meaningless if
    callers leave it NULL.
    """
    inserted = 0
    for f in facts:
        new_id, _ = insert_with_restatement_detection(
            conn,
            ticker=f.ticker,
            period_end=f.period_end,
            fiscal_period_type=f.fiscal_period_type.value,
            line_item=f.line_item,
            value=f.value,
            currency=f.currency.value if f.currency is not None else None,
            unit=f.unit.value,
            source_doc_id=f.source_doc_id,
            confidence=f.confidence,
            extracted_by=extracted_by,
        )
        if new_id is not None:
            inserted += 1
    return inserted


def load_document_row(
    conn: sqlite3.Connection, document_id: int, expected_doc_type: str
) -> tuple[str, str]:
    """Return (ticker, file_path) for the document; raise if doc_type mismatch."""
    cur = conn.cursor()
    cur.execute(
        "SELECT ticker, file_path, doc_type FROM documents WHERE id = ?",
        (document_id,),
    )
    row = cur.fetchone()
    if row is None:
        raise ValueError(f"No document with id={document_id}")
    if row["doc_type"] != expected_doc_type:
        raise ValueError(
            f"Document {document_id} is doc_type={row['doc_type']!r}, not {expected_doc_type!r}"
        )
    return (row["ticker"], row["file_path"])


def read_records_json(file_path: Path) -> list[dict[str, object]]:
    """Read an FMP JSON file expected to be a list of dicts."""
    with open(file_path, encoding="utf-8") as f:
        records = json.load(f)
    if not isinstance(records, list):
        raise ValueError(f"Expected list of records in {file_path}, got {type(records).__name__}")
    return records
