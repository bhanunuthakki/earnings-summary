"""Tests for src/compute/segment_oi_10k.py — 10-K segment-OI extractor."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from compute.segment_oi_10k import (
    _is_xbrl_filler_label,
    _normalize_segment_name,
    _parse_date,
    extract_segment_oi_from_record,
)
from models.facts import FiscalPeriodType, Unit

_NBSP = "\xa0"


def test_parse_date_variants() -> None:
    """Common 10-K date formats parse to a datetime."""
    assert _parse_date("Dec. 31, 2024") == datetime(2024, 12, 31)
    assert _parse_date("December 31, 2024") == datetime(2024, 12, 31)
    assert _parse_date("Sept. 28, 2024") == datetime(2024, 9, 28)
    assert _parse_date("Jan 1, 2020") == datetime(2020, 1, 1)
    assert _parse_date("garbage") is None


def test_normalize_segment_name_strips_xbrl_prefix() -> None:
    """`Operating Segments | Google Cloud` becomes `Google Cloud`."""
    assert _normalize_segment_name("Operating Segments | Google Cloud") == "Google Cloud"
    assert _normalize_segment_name("Google Services") == "Google Services"
    assert _normalize_segment_name("Reconciling items") == "Reconciling items"


def test_xbrl_filler_label_matched() -> None:
    """The repeated [Line Items] row is a filler, not a segment header."""
    assert _is_xbrl_filler_label("Segment Reporting Information [Line Items]")
    assert _is_xbrl_filler_label("Geographical Areas [Line Items]")
    assert not _is_xbrl_filler_label("Google Services")
    assert not _is_xbrl_filler_label("Operating Segments | Google Cloud")


def test_extract_walks_segments_and_emits_oi() -> None:
    """End-to-end on a synthetic 10-K JSON shape with two segments."""
    record: dict[str, object] = {
        "Information about Segments and Operating Income (Details) - $ in Millions": [
            {
                "Information about Segments and Operating Income (Details) - $ in Millions": [
                    "12 Months Ended"
                ]
            },
            {"items": ["Dec. 31, 2024", "Dec. 31, 2023"]},
            {"Segment Reporting Information [Line Items]": [_NBSP, _NBSP]},
            {"Total revenues": [350018, 307394]},
            {"Total costs and expenses": [237628, 223101]},
            {"Google Services": [_NBSP, _NBSP]},
            {"Segment Reporting Information [Line Items]": [_NBSP, _NBSP]},
            {"Total revenues": [304930, 272543]},
            {"Total costs and expenses": [183667, 176685]},
            {"Other Bets": [_NBSP, _NBSP]},
            {"Segment Reporting Information [Line Items]": [_NBSP, _NBSP]},
            {"Total revenues": [1648, 1527]},
            {"Total income from operations": [-4444, -4095]},
        ]
    }

    facts = extract_segment_oi_from_record(record, source_doc_id=42, ticker="GOOG")

    by_key = {(f.segment_name, f.period_end): f for f in facts}
    google_services_2024 = by_key[("Google Services", datetime(2024, 12, 31))]
    assert google_services_2024.value == Decimal("121263000000")
    assert google_services_2024.metric == "operating_income"
    assert google_services_2024.fiscal_period_type == FiscalPeriodType.FY
    assert google_services_2024.unit == Unit.ACTUAL
    assert google_services_2024.source_doc_id == 42

    other_bets_2023 = by_key[("Other Bets", datetime(2023, 12, 31))]
    assert other_bets_2023.value == Decimal("-4095000000")


def test_consolidated_rows_before_segments_are_not_emitted() -> None:
    """Rows before the first real segment header don't accidentally emit facts."""
    record: dict[str, object] = {
        "Segment Reporting and Operating Income": [
            {"Segment Reporting and Operating Income": ["12 Months Ended"]},
            {"items": ["Dec. 31, 2024"]},
            {"Segment Reporting Information [Line Items]": [_NBSP]},
            # These are consolidated totals — no segment header has fired yet
            {"Total revenues": [350000]},
            {"Total costs and expenses": [240000]},
            # First real segment header
            {"Google Services": [_NBSP]},
            {"Segment Reporting Information [Line Items]": [_NBSP]},
            {"Total revenues": [300000]},
            {"Total costs and expenses": [180000]},
        ]
    }
    facts = extract_segment_oi_from_record(record, source_doc_id=1, ticker="X")
    segments = {f.segment_name for f in facts}
    assert segments == {"Google Services"}
