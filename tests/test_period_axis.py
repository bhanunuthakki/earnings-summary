"""Unit tests for table_extractors.period_axis (segment_quarterly_framework.md
§2.2-§2.3) — the 10-Q period-axis disambiguation algorithm: duration-months
+ end-date parsing, calendar-safe quarter-end projection (incl. non-December
FYEs), and the column classification table.
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from table_extractors.period_axis import (  # noqa: E402
    classify_period_column,
    expected_period_ends,
    parse_period_label,
    parse_trailing_date,
)


def test_parse_trailing_date_plain() -> None:
    parsed = parse_trailing_date("Dec. 31, 2024")
    assert parsed is not None
    assert parsed.date() == date(2024, 12, 31)


def test_parse_trailing_date_from_longer_header() -> None:
    # generic_xbrl_capture's original case: the duration prefix is discarded
    # by this function (by design — parse_period_label is what keeps it).
    parsed = parse_trailing_date("12 Months Ended Dec. 31, 2024")
    assert parsed is not None
    assert parsed.date() == date(2024, 12, 31)


def test_parse_trailing_date_unparseable() -> None:
    assert parse_trailing_date("Level 1") is None


def test_parse_period_label_extracts_both_halves() -> None:
    parsed = parse_period_label("6 Months Ended June 30, 2025")
    assert parsed.duration_months == 6
    assert parsed.end_date is not None
    assert parsed.end_date.date() == date(2025, 6, 30)


def test_parse_period_label_no_duration_prefix() -> None:
    parsed = parse_period_label("June 30, 2025")
    assert parsed.duration_months is None
    assert parsed.end_date is not None


def test_expected_period_ends_calendar_fye() -> None:
    # AMZN/GOOG-style Dec-31 FYE.
    current_end, prior_year_end = expected_period_ends("Q2", 2025, 12, 31)
    assert current_end == date(2025, 6, 30)
    assert prior_year_end == date(2024, 6, 30)


def test_expected_period_ends_q1() -> None:
    current_end, _ = expected_period_ends("Q1", 2025, 12, 31)
    assert current_end == date(2025, 3, 31)


def test_expected_period_ends_q3() -> None:
    current_end, prior_year_end = expected_period_ends("Q3", 2025, 12, 31)
    assert current_end == date(2025, 9, 30)
    assert prior_year_end == date(2024, 9, 30)


def test_expected_period_ends_non_december_fye_jan31() -> None:
    # VEEV/RBRK-style Jan-31 FYE: FY2026 -> Q1 end Apr-30-2025, Q2 end
    # Jul-31-2025, Q3 end Oct-31-2025 (end-of-month clamped, never "Apr-31").
    q1_end, _ = expected_period_ends("Q1", 2026, 1, 31)
    q2_end, _ = expected_period_ends("Q2", 2026, 1, 31)
    q3_end, _ = expected_period_ends("Q3", 2026, 1, 31)
    assert q1_end == date(2025, 4, 30)
    assert q2_end == date(2025, 7, 31)
    assert q3_end == date(2025, 10, 31)


def test_expected_period_ends_non_december_fye_oct31() -> None:
    # AMAT/TOL-style Oct-31 FYE.
    q1_end, _ = expected_period_ends("Q1", 2025, 10, 31)
    assert q1_end == date(2025, 1, 31)


def test_classify_current_discrete_q1() -> None:
    result = classify_period_column(
        "3 Months Ended March 31, 2025",
        nominal_quarter="Q1",
        fiscal_year=2025,
        fye_month=12,
        fye_day=31,
    )
    assert result.classification == "current_discrete"
    assert result.reason_code == ""


def test_classify_current_cumulative_q2() -> None:
    result = classify_period_column(
        "6 Months Ended June 30, 2025",
        nominal_quarter="Q2",
        fiscal_year=2025,
        fye_month=12,
        fye_day=31,
    )
    assert result.classification == "current_cumulative"


def test_classify_current_cumulative_q3_nine_months() -> None:
    result = classify_period_column(
        "9 Months Ended September 30, 2025",
        nominal_quarter="Q3",
        fiscal_year=2025,
        fye_month=12,
        fye_day=31,
    )
    assert result.classification == "current_cumulative"


def test_classify_prior_year_comparative() -> None:
    result = classify_period_column(
        "6 Months Ended June 30, 2024",
        nominal_quarter="Q2",
        fiscal_year=2025,
        fye_month=12,
        fye_day=31,
    )
    assert result.classification == "prior_year_comparative"


def test_classify_ambiguous_same_date_no_duration_prefix() -> None:
    # The exact failure mode the framework exists to fix: a column whose
    # trailing date lands on the current period-end but carries no
    # "N Months Ended" prefix generic_xbrl_capture would need to disambiguate.
    result = classify_period_column(
        "June 30, 2025",
        nominal_quarter="Q2",
        fiscal_year=2025,
        fye_month=12,
        fye_day=31,
    )
    assert result.classification == "ambiguous_same_date"
    assert result.reason_code == "period_axis_ambiguous_no_duration_prefix"


def test_classify_off_cycle_unrelated_date() -> None:
    result = classify_period_column(
        "3 Months Ended January 31, 2025",
        nominal_quarter="Q2",
        fiscal_year=2025,
        fye_month=12,
        fye_day=31,
    )
    assert result.classification == "off_cycle"
    assert result.reason_code == "off_cycle_or_unparseable_period"


def test_classify_off_cycle_unparseable_label() -> None:
    result = classify_period_column(
        "Level 1",
        nominal_quarter="Q2",
        fiscal_year=2025,
        fye_month=12,
        fye_day=31,
    )
    assert result.classification == "off_cycle"
