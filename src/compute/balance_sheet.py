"""Extract canonical line items from FMP balance_sheet documents.

Maps 32 FMP balance-sheet fields (cashAndCashEquivalents, totalAssets,
longTermDebt, ...) to canonical snake_case line_items.
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
from models.fmp_payloads import FmpBalanceSheetRecord

_DOC_TYPE = "fmp_balance_sheet"

_LINE_ITEM_SPEC: list[tuple[str, str, Unit]] = [
    ("cashAndCashEquivalents", "cash_and_equivalents", Unit.ACTUAL),
    ("shortTermInvestments", "short_term_investments", Unit.ACTUAL),
    ("cashAndShortTermInvestments", "cash_and_short_term_investments", Unit.ACTUAL),
    ("netReceivables", "net_receivables", Unit.ACTUAL),
    ("accountsReceivables", "accounts_receivable", Unit.ACTUAL),
    ("inventory", "inventory", Unit.ACTUAL),
    ("otherCurrentAssets", "other_current_assets", Unit.ACTUAL),
    ("totalCurrentAssets", "total_current_assets", Unit.ACTUAL),
    ("propertyPlantEquipmentNet", "property_plant_equipment_net", Unit.ACTUAL),
    ("goodwill", "goodwill", Unit.ACTUAL),
    ("intangibleAssets", "intangible_assets", Unit.ACTUAL),
    ("goodwillAndIntangibleAssets", "goodwill_and_intangible_assets", Unit.ACTUAL),
    ("longTermInvestments", "long_term_investments", Unit.ACTUAL),
    ("otherNonCurrentAssets", "other_non_current_assets", Unit.ACTUAL),
    ("totalNonCurrentAssets", "total_non_current_assets", Unit.ACTUAL),
    ("totalAssets", "total_assets", Unit.ACTUAL),
    ("accountPayables", "accounts_payable", Unit.ACTUAL),
    ("shortTermDebt", "short_term_debt", Unit.ACTUAL),
    ("deferredRevenue", "deferred_revenue", Unit.ACTUAL),
    ("otherCurrentLiabilities", "other_current_liabilities", Unit.ACTUAL),
    ("totalCurrentLiabilities", "total_current_liabilities", Unit.ACTUAL),
    ("longTermDebt", "long_term_debt", Unit.ACTUAL),
    ("deferredRevenueNonCurrent", "deferred_revenue_non_current", Unit.ACTUAL),
    ("otherNonCurrentLiabilities", "other_non_current_liabilities", Unit.ACTUAL),
    ("totalNonCurrentLiabilities", "total_non_current_liabilities", Unit.ACTUAL),
    ("totalLiabilities", "total_liabilities", Unit.ACTUAL),
    ("commonStock", "common_stock", Unit.ACTUAL),
    ("retainedEarnings", "retained_earnings", Unit.ACTUAL),
    ("additionalPaidInCapital", "additional_paid_in_capital", Unit.ACTUAL),
    ("totalStockholdersEquity", "total_stockholders_equity", Unit.ACTUAL),
    ("totalEquity", "total_equity", Unit.ACTUAL),
    ("totalDebt", "total_debt", Unit.ACTUAL),
    ("netDebt", "net_debt", Unit.ACTUAL),
]


def extract_facts_from_record(
    record: FmpBalanceSheetRecord,
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


def extract_balance_sheet_facts(
    conn: sqlite3.Connection, document_id: int, project_root: Path
) -> int:
    """Read documents[document_id]'s file, write FinancialFact rows. Idempotent.

    `*_ttm.json` files report a TTM-aligned snapshot; we override fiscal_period_type
    to TTM so it doesn't conflate with standalone-quarter point-in-time values.
    """
    _ticker, file_path_str = load_document_row(conn, document_id, _DOC_TYPE)
    records = read_records_json(project_root / file_path_str)
    period_override = FiscalPeriodType.TTM if file_path_str.endswith("_ttm.json") else None

    inserted = 0
    for idx, rec_data in enumerate(records):
        rec = FmpBalanceSheetRecord.model_validate(rec_data)
        facts = extract_facts_from_record(
            rec,
            source_doc_id=document_id,
            period_type_override=period_override,
            record_index=idx,
        )
        inserted += insert_financial_facts(conn, facts, extracted_by="fmp")
    conn.commit()
    return inserted
