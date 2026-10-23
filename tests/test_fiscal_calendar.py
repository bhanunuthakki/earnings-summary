"""Parity of shared fiscal calendars and historical filename year labels."""

from datetime import date, datetime

import pytest

from compute.fiscal_calendar import calendar_id_from_fye, period_end_for, summary_period_end
from compute.transcript_ingest import ParsedFilename, map_to_period
from models.facts import FiscalPeriodType

# Cross-path fiscal periods retain their distinct historical year labels.
_FISCAL_CALENDAR_YEARS = (2024, 2025, 2026, 2028)


@pytest.mark.parametrize("year", _FISCAL_CALENDAR_YEARS)
@pytest.mark.parametrize("quarter", (1, 2, 3, 4))
@pytest.mark.parametrize("ticker", ("RBRK", "VEEV", "AMAT", "TOL", "GOOG", "NVDA", "UNKNOWN"))
def test_filename_paths_and_ir_calendar_preserve_label_conventions(
    ticker: str,
    quarter: int,
    year: int,
) -> None:
    if ticker in {"RBRK", "VEEV"}:
        calendar = "veeva"
        month, day, offset = ((4, 30, -1), (7, 31, -1), (10, 31, -1), (1, 31, 0))[quarter - 1]
        summary_label = year - 1
    elif ticker in {"AMAT", "TOL"}:
        calendar = "fye_10"
        month, day, offset = ((1, 31, 0), (4, 30, 0), (7, 31, 0), (10, 31, 0))[quarter - 1]
        summary_label = year
    else:
        calendar = "calendar"
        month, day, offset = ((3, 31, 0), (6, 30, 0), (9, 30, 0), (12, 31, 0))[quarter - 1]
        summary_label = year
    expected = datetime(year + offset, month, day)
    transcript = map_to_period(ParsedFilename(ticker, quarter, year))
    assert transcript.period_end == expected
    assert transcript.fiscal_period_type is FiscalPeriodType(f"Q{quarter}")
    assert summary_period_end(ticker, quarter, summary_label) == expected
    assert period_end_for(calendar, year, quarter) == expected.date()


@pytest.mark.parametrize("year", _FISCAL_CALENDAR_YEARS)
@pytest.mark.parametrize("quarter", (1, 2, 3, 4))
@pytest.mark.parametrize(
    "calendar,ends",
    [
        ("calendar", ((3, 31, 0), (6, 30, 0), (9, 30, 0), (12, 31, 0))),
        ("nvo", ((3, 31, 0), (6, 30, 0), (9, 30, 0), (12, 31, 0))),
        ("veeva", ((4, 30, -1), (7, 31, -1), (10, 31, -1), (1, 31, 0))),
        ("apr30", ((7, 31, -1), (10, 31, -1), (1, 31, 0), (4, 30, 0))),
        ("visa", ((12, 31, -1), (3, 31, 0), (6, 30, 0), (9, 30, 0))),
        ("oracle", ((8, 31, -1), (11, 30, -1), (2, 28, 0), (5, 31, 0))),
        ("fye_01", ((4, 30, -1), (7, 31, -1), (10, 31, -1), (1, 31, 0))),
        ("fye_10", ((1, 31, 0), (4, 30, 0), (7, 31, 0), (10, 31, 0))),
        ("fye_11", ((2, 0, 0), (5, 31, 0), (8, 31, 0), (11, 30, 0))),
    ],
)
def test_ir_calendar_families_and_leap_years_keep_existing_endpoints(
    calendar: str,
    ends: tuple[tuple[int, int, int], ...],
    quarter: int,
    year: int,
) -> None:
    month, day, offset = ends[quarter - 1]
    if day == 0:
        day = 29 if year % 4 == 0 else 28
    assert period_end_for(calendar, year, quarter) == date(year + offset, month, day)


def test_filename_case_behavior_is_preserved() -> None:
    # ParsedFilename is also constructed directly; its historical lowercase
    # behavior differs from the summary adapter's explicit uppercasing.
    assert map_to_period(ParsedFilename("rbrk", 1, 2026)).period_end == datetime(2026, 3, 31)
    assert summary_period_end("rbrk", 1, 2026) == datetime(2026, 4, 30)


@pytest.mark.parametrize("quarter", (0, 5))
def test_filename_invalid_quarters_keep_key_error(quarter: int) -> None:
    for ticker in ("RBRK", "AMAT", "UNKNOWN"):
        with pytest.raises(KeyError):
            map_to_period(ParsedFilename(ticker, quarter, 2026))
        with pytest.raises(KeyError):
            summary_period_end(ticker, quarter, 2026)


def test_ir_invalid_quarter_and_calendar_behavior_is_preserved() -> None:
    assert period_end_for("calendar", 2026, 0) == date(2026, 12, 31)
    assert period_end_for("fye_10", 2026, 0) == date(2025, 10, 31)
    with pytest.raises(IndexError):
        period_end_for("veeva", 2026, 5)
    for calendar in ("unknown", "fye_xx"):
        with pytest.raises(ValueError, match="Unknown calendar id"):
            period_end_for(calendar, 2026, 1)
    assert calendar_id_from_fye(None) == "calendar"
    assert calendar_id_from_fye("99-99") == "calendar"
    assert calendar_id_from_fye("01-25") == "fye_01"
