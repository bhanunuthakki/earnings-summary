"""Tests for src/compute/as_reported.py."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from compute.as_reported import extract_facts_from_record
from models.facts import Currency, FiscalPeriodType, Unit
from models.fmp_payloads import FmpAsReportedRecord

_SAMPLE = {
    "date": "2026-03-30",
    "symbol": "GOOG",
    "reportedCurrency": "USD",
    "period": "Q1",
    "fiscalYear": 2026,
    "data": {
        "documenttype": "10-Q",
        "entityregistrantname": "Alphabet Inc.",
        "revenueremainingperformanceobligation": 130_000_000_000,
        "contractwithcustomerliability": 6_500_000_000,
        "contractwithcustomerliabilitycurrent": 6_000_000_000,
        "operatingleaseliability": 14_000_000_000,
        "operatingleaserightofuseasset": 13_500_000_000,
        "paymentsforrepurchaseofcommonstock": 18_000_000_000,
        "irrelevant_tag": "this is a string and should be ignored",
        "another_irrelevant": None,
    },
}


def test_extract_as_reported_facts_canonical() -> None:
    """Curated XBRL tags produce one fact each; non-numeric / missing skipped."""
    record = FmpAsReportedRecord.model_validate(_SAMPLE)
    facts = extract_facts_from_record(record, source_doc_id=300)

    by_line = {f.line_item: f for f in facts}
    assert by_line["rpo"].value == Decimal("130000000000")
    assert by_line["rpo"].currency == Currency.USD
    assert by_line["rpo"].unit == Unit.ACTUAL
    assert by_line["rpo"].fiscal_period_type == FiscalPeriodType.Q1
    assert by_line["rpo"].period_end == datetime(2026, 3, 30)
    assert by_line["rpo"].source_doc_id == 300

    assert by_line["contract_liabilities"].value == Decimal("6500000000")
    assert by_line["operating_lease_rou_asset"].value == Decimal("13500000000")


def test_skips_non_numeric_xbrl_values() -> None:
    """String-valued XBRL tags (documenttype, entityregistrantname) produce no facts."""
    record = FmpAsReportedRecord.model_validate(_SAMPLE)
    facts = extract_facts_from_record(record, source_doc_id=1)
    line_items = {f.line_item for f in facts}
    assert "documenttype" not in line_items
    assert "entityregistrantname" not in line_items


def test_skips_missing_tags() -> None:
    """If only a subset of curated tags is in `data`, only those produce facts."""
    minimal = {
        "date": "2026-03-30",
        "symbol": "X",
        "reportedCurrency": "USD",
        "period": "Q1",
        "data": {"revenueremainingperformanceobligation": 100},
    }
    record = FmpAsReportedRecord.model_validate(minimal)
    facts = extract_facts_from_record(record, source_doc_id=1)
    line_items = {f.line_item for f in facts}
    assert line_items == {"rpo"}
