"""Extract canonical line items from FMP cash_flow documents.

Maps 24 FMP cash-flow fields (netCashProvidedByOperatingActivities,
freeCashFlow, capitalExpenditure, ...) to canonical snake_case line_items.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping, Sequence
from pathlib import Path

from compute._common import (
    extract_facts_with_spec,
    insert_financial_facts,
    load_document_row,
    read_records_json,
)
from models.documents import SourceQualityTier
from models.facts import FinancialFact, FiscalPeriodType, Unit
from models.fmp_payloads import FmpCashFlowRecord

_DOC_TYPE = "fmp_cashflow"

_LINE_ITEM_SPEC: list[tuple[str, str, Unit]] = [
    ("netIncome", "net_income_cf", Unit.ACTUAL),
    ("depreciationAndAmortization", "depreciation_and_amortization_cf", Unit.ACTUAL),
    ("deferredIncomeTax", "deferred_income_tax", Unit.ACTUAL),
    ("stockBasedCompensation", "stock_based_compensation", Unit.ACTUAL),
    ("changeInWorkingCapital", "change_in_working_capital", Unit.ACTUAL),
    ("otherNonCashItems", "other_non_cash_items", Unit.ACTUAL),
    ("netCashProvidedByOperatingActivities", "net_cash_from_operating", Unit.ACTUAL),
    ("operatingCashFlow", "operating_cash_flow", Unit.ACTUAL),
    ("investmentsInPropertyPlantAndEquipment", "investments_in_ppe", Unit.ACTUAL),
    ("acquisitionsNet", "acquisitions_net", Unit.ACTUAL),
    ("purchasesOfInvestments", "purchases_of_investments", Unit.ACTUAL),
    ("salesMaturitiesOfInvestments", "sales_maturities_of_investments", Unit.ACTUAL),
    ("netCashProvidedByInvestingActivities", "net_cash_from_investing", Unit.ACTUAL),
    ("netDebtIssuance", "net_debt_issuance", Unit.ACTUAL),
    ("commonStockRepurchased", "common_stock_repurchased", Unit.ACTUAL),
    ("netDividendsPaid", "net_dividends_paid", Unit.ACTUAL),
    ("commonDividendsPaid", "common_dividends_paid", Unit.ACTUAL),
    ("netCashProvidedByFinancingActivities", "net_cash_from_financing", Unit.ACTUAL),
    ("netChangeInCash", "net_change_in_cash", Unit.ACTUAL),
    ("cashAtEndOfPeriod", "cash_at_end_of_period", Unit.ACTUAL),
    ("capitalExpenditure", "capital_expenditure", Unit.ACTUAL),
    ("freeCashFlow", "free_cash_flow", Unit.ACTUAL),
    ("incomeTaxesPaid", "income_taxes_paid", Unit.ACTUAL),
    ("interestPaid", "interest_paid", Unit.ACTUAL),
]


def extract_facts_from_record(
    record: FmpCashFlowRecord,
    source_doc_id: int,
    *,
    period_type_override: FiscalPeriodType | None = None,
    record_index: int,
) -> list[FinancialFact]:
    """Convert one validated record to FinancialFact rows.

    Note: `net_income_cf` and `depreciation_and_amortization_cf` use the `_cf`
    suffix to distinguish from the income-statement-sourced versions of the
    same metric (which can differ slightly due to timing). Cross-source
    reconciliation lives in the consumer.
    """
    return extract_facts_with_spec(
        record,
        source_doc_id,
        _LINE_ITEM_SPEC,
        period_type_override=period_type_override,
        record_index=record_index,
    )


def extract_cashflow_facts(
    conn: sqlite3.Connection,
    document_id: int,
    project_root: Path,
    *,
    records: Sequence[Mapping[str, object]] | None = None,
    commit: bool = True,
) -> int:
    """Read documents[document_id]'s file, write FinancialFact rows. Idempotent.

    `*_ttm.json` rows get fiscal_period_type=TTM (FMP labels the latest quarter
    in `period` even though the value is trailing-12-month).
    """
    _ticker, file_path_str = load_document_row(conn, document_id, _DOC_TYPE)
    source_records = read_records_json(project_root / file_path_str) if records is None else records
    period_override = FiscalPeriodType.TTM if file_path_str.endswith("_ttm.json") else None

    inserted = 0
    for idx, rec_data in enumerate(source_records):
        rec = FmpCashFlowRecord.model_validate(rec_data)
        facts = extract_facts_from_record(
            rec,
            source_doc_id=document_id,
            period_type_override=period_override,
            record_index=idx,
        )
        inserted += insert_financial_facts(
            conn, facts, extracted_by="fmp", tier=SourceQualityTier.FMP_NORMALIZED
        )
    if commit:
        conn.commit()
    return inserted
