"""Pure fiscal calendar endpoints and legacy filename calendar membership.

Calendar IDs and date conventions are shared with IR classification. Filename
paths retain their own explicit year-label adapters; no issuer state is read.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

CALENDAR_YEAR = "calendar"
JANUARY_FYE = "veeva"
APRIL_FYE = "apr30"
NVO_CALENDAR = "nvo"
SEPTEMBER_FYE = "visa"
MAY_FYE = "oracle"

# Only these legacy filename issuers override the calendar-year fallback.
FILENAME_CALENDARS: dict[str, str] = {
    "VEEV": JANUARY_FYE,
    "RBRK": JANUARY_FYE,
    "TOL": "fye_10",
    "AMAT": "fye_10",
}


def period_end_for(ticker_calendar: str, year: int, q: int) -> date:
    """Map (calendar, year, q) → period-end ISO date.

    See `directives/fetch_ir_documents.md` for the per-issuer fiscal calendar.
    """
    if ticker_calendar in (CALENDAR_YEAR, NVO_CALENDAR):
        return [date(year, 3, 31), date(year, 6, 30), date(year, 9, 30), date(year, 12, 31)][q - 1]
    if ticker_calendar == JANUARY_FYE:
        # FY-N Q1 ends Apr 30 (N-1), Q4 ends Jan 31 (N).
        return [
            date(year - 1, 4, 30),
            date(year - 1, 7, 31),
            date(year - 1, 10, 31),
            date(year, 1, 31),
        ][q - 1]
    if ticker_calendar == APRIL_FYE:
        # Generic April-30 FYE: FY-N Q1 ends Jul 31 (N-1), Q4 ends Apr 30 (N).
        # No registered ticker uses this (Rubrik is Jan-FYE → JANUARY_FYE); it is
        # the calendar derived for a genuine April-30 issuer via fiscal_year_end.
        return [
            date(year - 1, 7, 31),
            date(year - 1, 10, 31),
            date(year, 1, 31),
            date(year, 4, 30),
        ][q - 1]
    if ticker_calendar == SEPTEMBER_FYE:
        # FY-N Q1 ends Dec 31 (N-1), Q4 ends Sep 30 (N).
        return [
            date(year - 1, 12, 31),
            date(year, 3, 31),
            date(year, 6, 30),
            date(year, 9, 30),
        ][q - 1]
    if ticker_calendar == MAY_FYE:
        # FY-N Q1 ends Aug 31 (N-1), Q4 ends May 31 (N). Q3 uses Feb-28 as the
        # conventional last-day key (the existing calendars all use fixed dates).
        return [
            date(year - 1, 8, 31),
            date(year - 1, 11, 30),
            date(year, 2, 28),
            date(year, 5, 31),
        ][q - 1]
    if ticker_calendar.startswith("fye_"):
        # Generic non-December FYE derived from tracked_companies.fiscal_year_end.
        # 'fye_MM' = the fiscal year ENDS in month MM (year N); Q4 ends that
        # month-end and earlier quarters step back 3 months each. Curated entries
        # above override this for 52/53-week names whose ends aren't month-ends.
        try:
            fy_month = int(ticker_calendar[4:6])
        except ValueError:
            raise ValueError(f"Unknown calendar id: {ticker_calendar!r}") from None
        end_month, end_year = fy_month - 3 * (4 - q), year
        while end_month <= 0:
            end_month, end_year = end_month + 12, end_year - 1
        if end_month == 12:
            return date(end_year, 12, 31)
        return date(end_year, end_month + 1, 1) - timedelta(days=1)
    raise ValueError(f"Unknown calendar id: {ticker_calendar!r}")


_FYE_TO_CALENDAR: dict[str, str] = {
    "12-31": CALENDAR_YEAR,
    "01-31": JANUARY_FYE,
    "04-30": APRIL_FYE,
    "09-30": SEPTEMBER_FYE,
    "05-31": MAY_FYE,
}


def calendar_id_from_fye(mmdd: str | None) -> str:
    """Derive a fiscal-calendar id from a ``fiscal_year_end`` ("MM-DD") string.

    Used by the headless auto-fetch path to attribute periods for tickers not in
    ``ISSUER_REGISTRY`` (the crawl supplies the ticker; the FYE supplies the
    calendar). Unknown / missing FYE falls back to the Dec-31 calendar. NVO's
    H1/9M/FY labeling can't be inferred from the FYE alone (it shares calendar's
    Dec-31), so NVO stays registry-only. An exact curated-table match wins (e.g.
    01-31 → veeva); any other non-December month falls back to the generic
    ``fye_MM`` calendar; December / missing / unparseable uses the Dec-31 calendar.
    """
    key = (mmdd or "").strip()
    if key in _FYE_TO_CALENDAR:
        return _FYE_TO_CALENDAR[key]
    try:
        month = int(key.split("-")[0])
    except (ValueError, IndexError):
        return CALENDAR_YEAR
    return f"fye_{month:02d}" if 1 <= month <= 11 else CALENDAR_YEAR


def summary_period_end(ticker: str, quarter: int, year: int) -> datetime:
    """Resolve summary labels while preserving their historical year convention.

    RBRK/VEEV summary labels precede the fiscal-ending year by one. AMAT/TOL
    and calendar-year filenames already use the fiscal-ending year.
    """
    if quarter not in (1, 2, 3, 4):
        raise KeyError(quarter)
    calendar = FILENAME_CALENDARS.get(ticker.upper(), CALENDAR_YEAR)
    fiscal_year = year + 1 if calendar == JANUARY_FYE else year
    endpoint = period_end_for(calendar, fiscal_year, quarter)
    return datetime.combine(endpoint, datetime.min.time())
