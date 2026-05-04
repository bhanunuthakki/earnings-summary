"""Extract canonical line items from FMP income_statement documents.

For each FmpIncomeStatementRecord, emit one financial_facts row per known line
item. Canonical line_item names are snake_case for cross-source consistency:
the same revenue figure pulled from FMP, SEC XBRL, or an IR press release uses
the same line_item value, differentiated by source_doc_id.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from models.facts import Currency, FinancialFact, FiscalPeriodType, Unit
from models.fmp_payloads import FmpIncomeStatementRecord

# (FMP field name, canonical line_item, unit)
_LINE_ITEM_SPEC: list[tuple[str, str, Unit]] = [
    ("revenue", "revenue", Unit.ACTUAL),
    ("costOfRevenue", "cost_of_revenue", Unit.ACTUAL),
    ("grossProfit", "gross_profit", Unit.ACTUAL),
    ("researchAndDevelopmentExpenses", "research_and_development", Unit.ACTUAL),
    ("sellingGeneralAndAdministrativeExpenses", "sga", Unit.ACTUAL),
    ("operatingExpenses", "operating_expenses", Unit.ACTUAL),
    ("operatingIncome", "operating_income", Unit.ACTUAL),
    ("ebit", "ebit", Unit.ACTUAL),
    ("ebitda", "ebitda", Unit.ACTUAL),
    ("netIncome", "net_income", Unit.ACTUAL),
    ("incomeBeforeTax", "income_before_tax", Unit.ACTUAL),
    ("incomeTaxExpense", "income_tax_expense", Unit.ACTUAL),
    ("interestIncome", "interest_income", Unit.ACTUAL),
    ("interestExpense", "interest_expense", Unit.ACTUAL),
    ("depreciationAndAmortization", "depreciation_and_amortization", Unit.ACTUAL),
    ("eps", "eps", Unit.ACTUAL),
    ("epsDiluted", "eps_diluted", Unit.ACTUAL),
    ("weightedAverageShsOut", "weighted_avg_shares", Unit.COUNT),
    ("weightedAverageShsOutDil", "weighted_avg_shares_diluted", Unit.COUNT),
]


def _parse_currency(s: str) -> Currency | None:
    """Coerce reportedCurrency to Currency enum, or None if not in our enum yet."""
    try:
        return Currency(s)
    except ValueError:
        return None


def extract_facts_from_record(
    record: FmpIncomeStatementRecord, source_doc_id: int
) -> list[FinancialFact]:
    """Convert one validated record to a list of FinancialFact rows."""
    period_end = datetime.fromisoformat(record.date)
    period_type = FiscalPeriodType(record.period)
    currency = _parse_currency(record.reportedCurrency)

    facts: list[FinancialFact] = []
    for fmp_field, canonical, unit in _LINE_ITEM_SPEC:
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


def _insert_facts(conn: sqlite3.Connection, facts: list[FinancialFact]) -> int:
    """Bulk-insert facts via INSERT OR IGNORE (UNIQUE index dedupes). Returns rowcount."""
    insert_sql = (
        "INSERT OR IGNORE INTO financial_facts "
        "(ticker, period_end, fiscal_period_type, line_item, value, "
        " currency, unit, source_doc_id, confidence) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
    )
    inserted = 0
    for f in facts:
        result = conn.execute(
            insert_sql,
            (
                f.ticker,
                f.period_end,
                f.fiscal_period_type.value,
                f.line_item,
                str(f.value),
                f.currency.value if f.currency is not None else None,
                f.unit.value,
                f.source_doc_id,
                f.confidence,
            ),
        )
        if result.rowcount > 0:
            inserted += 1
    return inserted


def _load_document_row(conn: sqlite3.Connection, document_id: int) -> tuple[str, str]:
    """Return (ticker, file_path) for the document, or raise if not income_statement."""
    cur = conn.cursor()
    cur.execute(
        "SELECT ticker, file_path, doc_type FROM documents WHERE id = ?",
        (document_id,),
    )
    row = cur.fetchone()
    if row is None:
        raise ValueError(f"No document with id={document_id}")
    if row["doc_type"] != "fmp_income_statement":
        raise ValueError(
            f"Document {document_id} is doc_type={row['doc_type']!r}, not 'fmp_income_statement'"
        )
    return (row["ticker"], row["file_path"])


def extract_income_statement_facts(
    conn: sqlite3.Connection, document_id: int, project_root: Path
) -> int:
    """Read documents[document_id]'s file, write FinancialFact rows.

    Idempotent on the UNIQUE index (ticker, period_end, fiscal_period_type,
    line_item, source_doc_id). Returns count of newly inserted rows.
    """
    _ticker, file_path_str = _load_document_row(conn, document_id)
    abs_path = project_root / file_path_str
    with open(abs_path, encoding="utf-8") as f:
        records = json.load(f)
    if not isinstance(records, list):
        raise ValueError(f"Expected list of records in {abs_path}, got {type(records).__name__}")

    inserted = 0
    for rec_data in records:
        rec = FmpIncomeStatementRecord.model_validate(rec_data)
        facts = extract_facts_from_record(rec, source_doc_id=document_id)
        inserted += _insert_facts(conn, facts)
    conn.commit()
    return inserted
