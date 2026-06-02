"""Tests for compute.kpi_extract_summaries value parsing.

Regression for the VEEV extraction abort: Haiku returned a non-numeric KPI value
("Q1 2026 ... InvalidOperation: ConversionSyntax"), and `_build_manifest` caught
only (TypeError, ValueError) — but `Decimal("N/A")` raises decimal.InvalidOperation
(an ArithmeticError), so one bad value aborted the whole ticker. `parse_decimal_value`
must degrade at the per-KPI scope (skip the value, keep extracting).
"""

from __future__ import annotations

from decimal import Decimal

from compute.kpi_extract_summaries import parse_decimal_value


def test_parse_decimal_value_parses_clean_numbers() -> None:
    assert parse_decimal_value(12.5) == Decimal("12.5")
    assert parse_decimal_value("17.8") == Decimal("17.8")
    assert parse_decimal_value(0) == Decimal("0")
    assert parse_decimal_value("-3.2") == Decimal("-3.2")


def test_parse_decimal_value_returns_none_for_unparseable() -> None:
    # Each of these raised decimal.InvalidOperation (or TypeError) and used to
    # abort the ticker; they must now skip just that KPI.
    for bad in ("N/A", "~17%", "n.m.", "", "1,200", "TBD", None):
        assert parse_decimal_value(bad) is None
