"""Tests for table_extractors.base helpers."""

from __future__ import annotations

from table_extractors.base import iter_xbrl_table, parse_units


def test_parse_units_supports_twd_monetary_scale_priority_and_explicit_flag() -> None:
    twd = parse_units("Segment Info (Details) - NT$ in Millions")
    assert twd.currency == "TWD"
    assert twd.scale == "millions"
    assert twd.currency_explicit is True

    mixed = parse_units("Revenues (Details) - USD ($) shares in Thousands, $ in Millions")
    assert mixed.currency == "USD"
    assert mixed.scale == "millions"
    assert mixed.currency_explicit is True

    unmatched = parse_units("Unlabeled Table Header")
    assert unmatched.currency == "USD"
    assert unmatched.scale == "units"
    assert unmatched.currency_explicit is False


def test_iter_xbrl_table_zips_paired_duration_headers() -> None:
    section: list[dict[str, object]] = [
        {"Segment Revenue": ["3 Months Ended", "9 Months Ended"]},
        {"items": ["Sep. 30, 2025", "Sep. 30, 2025"]},
        {"Cloud": [100, 280]},
    ]
    rows = list(iter_xbrl_table(section))
    assert len(rows) == 1
    assert rows[0].period_labels == [
        "3 Months Ended Sep. 30, 2025",
        "9 Months Ended Sep. 30, 2025",
    ]
