"""Extract canonical line items from FMP income_statement documents.

Maps 19 FMP fields (revenue, costOfRevenue, ..., weightedAverageShsOutDil) to
canonical snake_case line_items. Same revenue figure pulled from FMP, SEC XBRL,
or an IR press release uses the same line_item value, differentiated by
source_doc_id.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from compute._common import (
    extract_facts_with_spec,
    insert_financial_facts,
    load_document_row,
    read_records_json,
)
from models.facts import FinancialFact, FiscalPeriodType, Unit
from models.fmp_payloads import FmpIncomeStatementRecord

_DOC_TYPE = "fmp_income_statement"

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


def extract_facts_from_record(
    record: FmpIncomeStatementRecord,
    source_doc_id: int,
    period_type_override: FiscalPeriodType | None = None,
    record_index: int | None = None,
) -> list[FinancialFact]:
    """Convert one validated record to FinancialFact rows."""
    return extract_facts_with_spec(
        record,
        source_doc_id,
        _LINE_ITEM_SPEC,
        period_type_override=period_type_override,
        record_index=record_index,
    )


def extract_income_statement_facts(
    conn: sqlite3.Connection, document_id: int, project_root: Path
) -> int:
    """Read documents[document_id]'s file, write FinancialFact rows. Idempotent.

    For `*_ttm.json` files, the FMP `period` field still names the latest quarter
    ending the trailing 12-month window (e.g., "Q3"), but the values are
    trailing-12-month aggregates. We override fiscal_period_type to TTM so
    queries don't conflate TTM rolls with standalone quarters.
    """
    _ticker, file_path_str = load_document_row(conn, document_id, _DOC_TYPE)
    records = read_records_json(project_root / file_path_str)
    period_override = FiscalPeriodType.TTM if file_path_str.endswith("_ttm.json") else None

    inserted = 0
    for idx, rec_data in enumerate(records):
        rec = FmpIncomeStatementRecord.model_validate(rec_data)
        facts = extract_facts_from_record(
            rec,
            source_doc_id=document_id,
            period_type_override=period_override,
            record_index=idx,
        )
        inserted += insert_financial_facts(conn, facts, extracted_by="fmp")
    conn.commit()
    return inserted
