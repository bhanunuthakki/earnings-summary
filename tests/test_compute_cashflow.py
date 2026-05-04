"""Tests for src/compute/cashflow.py."""

from __future__ import annotations

from decimal import Decimal

from compute.cashflow import extract_facts_from_record
from models.facts import Currency, FiscalPeriodType, Unit
from models.fmp_payloads import FmpCashFlowRecord

_SAMPLE = {
    "date": "2025-12-31",
    "symbol": "GOOG",
    "reportedCurrency": "USD",
    "period": "Q4",
    "netIncome": 30_000_000_000,
    "depreciationAndAmortization": 5_000_000_000,
    "stockBasedCompensation": 6_000_000_000,
    "operatingCashFlow": 40_000_000_000,
    "netCashProvidedByOperatingActivities": 40_000_000_000,
    "capitalExpenditure": -25_000_000_000,
    "freeCashFlow": 15_000_000_000,
    "commonStockRepurchased": -16_000_000_000,
    "commonDividendsPaid": -2_000_000_000,
}


def test_extract_cashflow_facts_canonical() -> None:
    """Each FMP cashflow field maps to its canonical snake_case line_item."""
    record = FmpCashFlowRecord.model_validate(_SAMPLE)
    facts = extract_facts_from_record(record, source_doc_id=200)

    by_line = {f.line_item: f for f in facts}
    assert by_line["operating_cash_flow"].value == Decimal("40000000000")
    assert by_line["free_cash_flow"].value == Decimal("15000000000")
    assert by_line["capital_expenditure"].value == Decimal("-25000000000")
    assert by_line["common_stock_repurchased"].value == Decimal("-16000000000")
    assert by_line["net_income_cf"].value == Decimal("30000000000")

    assert all(f.unit == Unit.ACTUAL for f in facts)
    assert all(f.currency == Currency.USD for f in facts)
    assert facts[0].fiscal_period_type == FiscalPeriodType.Q4


def test_cashflow_uses_cf_suffix_for_cross_source_distinction() -> None:
    """net_income from cashflow has _cf suffix to distinguish from income statement."""
    record = FmpCashFlowRecord.model_validate(_SAMPLE)
    facts = extract_facts_from_record(record, source_doc_id=1)
    line_items = {f.line_item for f in facts}
    assert "net_income_cf" in line_items
    assert "net_income" not in line_items
