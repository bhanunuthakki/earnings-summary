"""Fiscal-year period cadence helpers for the DCF workbook builder."""

from __future__ import annotations

from collections import defaultdict

DEFAULT_PERIODS: tuple[str, ...] = ("Q1", "Q2", "Q3", "Q4")


def detect_fy_periods(
    records_i: dict[tuple[int, str], object],
    default: tuple[str, ...] = ("Q1", "Q2", "Q3", "Q4"),
) -> tuple[str, ...]:
    """Canonical period labels that make up ONE fiscal year for this issuer.

    Generalises the "four quarters per year" assumption to any consistent cadence
    — e.g. a semi-annual filer (BHP) that reports only H1/H2 as Q2/Q4, which sum
    to the fiscal year exactly as four quarters do. The cadence is the LARGEST
    period-set that recurs across >=2 fiscal years, so a single partial year (the
    current in-progress year, or an IPO mid-ramp) is never mistaken for it; with
    too little history to establish one, fall back to quarterly so short-history
    names self-skip below instead of building on a one-off period set.
    """
    by_fy: dict[int, set[str]] = defaultdict(set)
    for y, p in records_i:
        by_fy[y].add(p)
    counts: dict[frozenset[str], int] = defaultdict(int)
    for s in by_fy.values():
        if s:
            counts[frozenset(s)] += 1
    recurring = [s for s, n in counts.items() if n >= 2]
    return tuple(sorted(max(recurring, key=len))) if recurring else default
