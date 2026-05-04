"""Tests for src/compute/balance_sheet.py."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from compute.balance_sheet import extract_facts_from_record
from models.facts import Currency, FiscalPeriodType, Unit
from models.fmp_payloads import FmpBalanceSheetRecord

_SAMPLE = {
    "date": "2025-12-31",
    "symbol": "GOOG",
    "reportedCurrency": "USD",
    "period": "FY",
    "totalAssets": 500_000_000_000,
    "totalCurrentAssets": 200_000_000_000,
    "cashAndCashEquivalents": 95_000_000_000,
    "totalLiabilities": 130_000_000_000,
    "longTermDebt": 11_000_000_000,
    "totalStockholdersEquity": 350_000_000_000,
    "totalEquity": 350_000_000_000,
    "totalDebt": 25_000_000_000,
    "netDebt": -70_000_000_000,
    "retainedEarnings": 280_000_000_000,
}


def test_extract_balance_sheet_facts_canonical() -> None:
    """Each FMP balance field maps to its canonical snake_case line_item."""
    record = FmpBalanceSheetRecord.model_validate(_SAMPLE)
    facts = extract_facts_from_record(record, source_doc_id=99)

    by_line = {f.line_item: f for f in facts}
    assert by_line["total_assets"].value == Decimal("500000000000")
    assert by_line["total_assets"].currency == Currency.USD
    assert by_line["total_assets"].unit == Unit.ACTUAL
    assert by_line["long_term_debt"].value == Decimal("11000000000")
    assert by_line["total_stockholders_equity"].value == Decimal("350000000000")
    assert by_line["net_debt"].value == Decimal("-70000000000")

    assert facts[0].fiscal_period_type == FiscalPeriodType.FY
    assert facts[0].period_end == datetime(2025, 12, 31)
    assert facts[0].source_doc_id == 99
    assert facts[0].ticker == "GOOG"


def test_extract_balance_sheet_facts_skips_none() -> None:
    """Fields not in the record produce no fact row."""
    minimal = {
        "date": "2025-12-31",
        "symbol": "X",
        "reportedCurrency": "USD",
        "period": "FY",
        "totalAssets": 1_000,
    }
    record = FmpBalanceSheetRecord.model_validate(minimal)
    facts = extract_facts_from_record(record, source_doc_id=1)
    line_items = {f.line_item for f in facts}
    assert "total_assets" in line_items
    assert "goodwill" not in line_items
