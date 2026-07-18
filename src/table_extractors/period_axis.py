"""10-Q period-axis disambiguation (segment_quarterly_framework.md §2).

Extracted OUT of ``generic_xbrl_capture.py`` (the ONE sanctioned edit to that
otherwise-frozen module: it now imports ``parse_trailing_date`` from here
under its old private name ``_parse_period`` — behavior byte-identical, only
the trailing-date parsing logic's *location* changed).

``generic_xbrl_capture.py``'s own docstring frames 10-Q period-axis
resolution as underdetermined "from the column label alone" — true for a
cold walker with no external context. The segment-quarterly pipeline
(``compute.segment_quarterly_10q``) is never cold: the source filename
(``{T}_form_10q_{Y}_{Q}.json``) already names the nominal fiscal quarter, and
``tracked_companies.fiscal_year_end`` gives the calendar to project expected
quarter-end dates from. Combined with parsing the duration-months prefix a
column header carries (``"6 Months Ended June 30, 2025"``) — which
``generic_xbrl_capture._parse_period`` discards, keeping only the trailing
date — the axis is deterministically resolvable in the large majority of
cases; the residual ambiguous case is deferred to an LLM Stage B fallback
(``compute.segment_quarterly_10q``'s ``segment_10q_period_disambiguate``
purpose), never silently guessed.
"""

from __future__ import annotations

import calendar
import re
from dataclasses import dataclass
from datetime import date, datetime
from typing import Literal

# Period-column header date formats — moved verbatim from
# generic_xbrl_capture._DATE_FORMATS (byte-identical values; only the name
# lost its leading underscore since it's now a cross-module public constant,
# the same convention llm_client.FAST_CLASSIFIER_MODEL / JSON_FENCE_RE already
# use for cross-module private-style imports).
DATE_FORMATS = ("%b. %d, %Y", "%b %d, %Y", "%B %d, %Y", "%m/%d/%Y", "%Y-%m-%d")

_TRAILING_DATE_RX = re.compile(r"([A-Z][a-z]{2}\.?\s+\d{1,2},\s+\d{4})$")

# A WIDER trailing-date matcher than _TRAILING_DATE_RX above — that one is
# kept byte-identical to generic_xbrl_capture's original (3-letter month
# abbreviation only, matching the 10-K FYE-column convention it was tuned
# against). A 10-Q's "N Months Ended" duration-prefixed header commonly
# spells the month out in full (the design doc's own worked example:
# "6 Months Ended June 30, 2025") — parse_period_label uses THIS regex so a
# full month name doesn't fall through to ambiguous/off_cycle for want of an
# abbreviation. Never used by parse_trailing_date (that stays the frozen,
# reused-by-generic_xbrl_capture behavior).
_TRAILING_DATE_FULL_RX = re.compile(r"([A-Z][a-z]+\.?\s+\d{1,2},\s+\d{4})$")

# "6 Months Ended", "3 Months Ended", "9 months ended" — the duration prefix
# generic_xbrl_capture._parse_period discards. This is the whole axis fix.
_DURATION_RX = re.compile(r"(\d{1,2})\s*Months?\s+Ended", re.IGNORECASE)

NominalQuarter = Literal["Q1", "Q2", "Q3"]

# Nominal quarter -> cumulative-months expectation for its "N Months Ended"
# YTD column (Q1's own discrete quarter IS its 3-month column, so Q1 never
# has a distinct cumulative shape).
_CUMULATIVE_MONTHS: dict[NominalQuarter, int] = {"Q1": 3, "Q2": 6, "Q3": 9}
# Nominal quarter -> how many months before the fiscal-year-end this quarter's
# own end-date falls (Q1 = 9 months before FYE, ..., matches a Q4/FY anchor
# being 0 months before FYE).
_MONTHS_BEFORE_FYE: dict[NominalQuarter, int] = {"Q1": 9, "Q2": 6, "Q3": 3}

PeriodAxisClass = Literal[
    "current_discrete",
    "current_cumulative",
    "prior_year_comparative",
    "ambiguous_same_date",
    "off_cycle",
]


def parse_trailing_date(period_label: str) -> datetime | None:
    """Parse a column header to its trailing period_end, or None when it
    isn't a date. Moved verbatim from
    ``generic_xbrl_capture._parse_period`` — the FY-column matcher there now
    imports this function under its old name.

    A trailing 'Dec. 31, 2024' is pulled out of longer headers
    ('12 Months Ended Dec. 31, 2024') before parsing.
    """
    s = period_label.strip()
    if not s:
        return None
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    m = _TRAILING_DATE_RX.search(s)
    if m:
        for fmt in ("%b. %d, %Y", "%b %d, %Y", "%B %d, %Y"):
            try:
                return datetime.strptime(m.group(1), fmt)
            except ValueError:
                continue
    return None


@dataclass(slots=True)
class ParsedPeriodLabel:
    """A column header split into BOTH halves generic_xbrl_capture's
    ``_parse_period`` collapses into one: the duration-months prefix AND the
    trailing end-date."""

    raw: str
    duration_months: int | None  # 3 / 6 / 9 / 12, or None if unparseable
    end_date: datetime | None


def _parse_end_date_permissive(period_label: str) -> datetime | None:
    """Like ``parse_trailing_date``, but the trailing-date fallback also
    accepts a spelled-out month name ("June 30, 2025"), not just a 3-letter
    abbreviation — 10-Q "N Months Ended" headers commonly spell it out. Kept
    separate from ``parse_trailing_date`` so that function (reused verbatim
    by ``generic_xbrl_capture._parse_period``) stays byte-identical."""
    s = period_label.strip()
    if not s:
        return None
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    m = _TRAILING_DATE_FULL_RX.search(s)
    if m:
        for fmt in ("%b. %d, %Y", "%b %d, %Y", "%B %d, %Y"):
            try:
                return datetime.strptime(m.group(1), fmt)
            except ValueError:
                continue
    return None


def parse_period_label(label: str) -> ParsedPeriodLabel:
    """Extract the duration-months prefix + trailing end-date from a 10-Q
    column header. Uses the permissive end-date parser (full OR abbreviated
    month names) since a "N Months Ended" duration prefix commonly precedes
    a spelled-out month — not ``parse_trailing_date``, which stays frozen for
    generic_xbrl_capture's own 10-K FYE-column convention."""
    end_date = _parse_end_date_permissive(label)
    m = _DURATION_RX.search(label)
    duration = int(m.group(1)) if m else None
    return ParsedPeriodLabel(raw=label, duration_months=duration, end_date=end_date)


def _shift_months_end_clamped(d: date, months_back: int) -> date:
    """``d`` shifted back ``months_back`` calendar months, clamping the day to
    the target month's length (Jan-31 minus 9 months -> Apr-30, not Apr-31).
    """
    total = d.year * 12 + (d.month - 1) - months_back
    year, month0 = divmod(total, 12)
    month = month0 + 1
    day = min(d.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def expected_period_ends(
    nominal_quarter: NominalQuarter,
    fiscal_year: int,
    fye_month: int,
    fye_day: int,
) -> tuple[date, date]:
    """(current_end, prior_year_end) for ``nominal_quarter`` of ``fiscal_year``,
    projected from the issuer's fiscal-year-end (month, day). Handles
    non-December FYEs (VEEV/RBRK Jan-31, AMAT/TOL Oct-31) via calendar-safe
    month arithmetic — never a hardcoded quarter-length assumption.
    """
    fye_day_clamped = min(fye_day, calendar.monthrange(fiscal_year, fye_month)[1])
    fye_date = date(fiscal_year, fye_month, fye_day_clamped)
    current_end = _shift_months_end_clamped(fye_date, _MONTHS_BEFORE_FYE[nominal_quarter])
    prior_year_end = _shift_months_end_clamped(current_end, 12)
    return current_end, prior_year_end


@dataclass(slots=True)
class PeriodAxisResult:
    classification: PeriodAxisClass
    parsed: ParsedPeriodLabel
    current_end: date
    prior_year_end: date
    # Empty string for a clean classification; a skip-reason string (the same
    # vocabulary generic_xbrl_capture.CaptureAudit.skip uses) otherwise — one
    # shared skip-reason taxonomy across both capture passes.
    reason_code: str = ""


def classify_period_column(
    label: str,
    *,
    nominal_quarter: NominalQuarter,
    fiscal_year: int,
    fye_month: int,
    fye_day: int,
) -> PeriodAxisResult:
    """Classify one 10-Q column header per segment_quarterly_framework.md §2.3.

    Never re-derives the calendar blind — ``fiscal_year``/``fye_month``/
    ``fye_day`` come from the filename + ``tracked_companies.fiscal_year_end``
    (or its FYE-detection fallback), never a guess made inside this function.
    """
    parsed = parse_period_label(label)
    current_end, prior_year_end = expected_period_ends(
        nominal_quarter, fiscal_year, fye_month, fye_day
    )
    cumulative_months = _CUMULATIVE_MONTHS[nominal_quarter]
    end = parsed.end_date.date() if parsed.end_date is not None else None

    if end is not None and end == current_end:
        if parsed.duration_months == 3:
            return PeriodAxisResult("current_discrete", parsed, current_end, prior_year_end)
        if parsed.duration_months == cumulative_months:
            return PeriodAxisResult("current_cumulative", parsed, current_end, prior_year_end)
        return PeriodAxisResult(
            "ambiguous_same_date",
            parsed,
            current_end,
            prior_year_end,
            "period_axis_ambiguous_no_duration_prefix",
        )

    if end is not None and end == prior_year_end:
        if parsed.duration_months in (3, cumulative_months):
            return PeriodAxisResult("prior_year_comparative", parsed, current_end, prior_year_end)
        # A prior-year column with a duration that fits neither cadence: not
        # one of the algorithm's clean cases. Off-cycle rather than a silent
        # guess — same reason vocabulary as an unparseable/off-cycle column.
        return PeriodAxisResult(
            "off_cycle", parsed, current_end, prior_year_end, "off_cycle_or_unparseable_period"
        )

    return PeriodAxisResult(
        "off_cycle", parsed, current_end, prior_year_end, "off_cycle_or_unparseable_period"
    )
