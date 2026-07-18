"""Shared scale/rate/count/equity value-classification guards for FMP-XBRL
note tables (segment_quarterly_framework.md §2.2).

Extracted OUT of ``generic_xbrl_capture.py`` (the ONE sanctioned edit to that
otherwise-frozen module: it now imports these under their old private names
— behavior byte-identical, only the location changed). ``compute.
segment_quarterly_10q`` (the new 10-Q segment extractor) needs the IDENTICAL
guards: same FMP JSON shape, same mis-scale traps (a rate/percent row in a
"$ in Millions" section, a share-count row hiding behind a "$ in Thousands"
title, the equity/share-rollforward family). This is flagged in the design
doc as a deliberate exception to the repo's "duplicate simple shared logic,
don't modularize" default — these are ~150 lines of hard-won, subtly-tuned
constants where a bugfix landing in one copy and not the other is exactly
the silent-drift failure mode the quality bar exists to prevent.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

# A scale we can trust to expand a value to whole units. A section reporting
# in "units" (no scale hint) is NOT captured — an unscaled magnitude is
# ambiguous (already-actual dollars? a share count? a ratio?).
SCALE_FACTOR: dict[str, int] = {
    "thousands": 1_000,
    "millions": 1_000_000,
    "billions": 1_000_000_000,
}

# Row-label tokens (case-insensitive substring) that mark a NON-monetary cell.
# A rate / percentage / ratio row in a $-scaled section is the #1 mis-scale trap.
RATE_TOKENS: tuple[str, ...] = (
    "rate",
    "percent",
    "percentage",
    " %",
    "(%)",
    "yield",
    "ratio",
    "basis point",
    "per annum",
)
# Per-share dollar amounts — real money, but reported per-share and so NOT
# scaled by the section's $-in-millions factor.
PER_SHARE_TOKENS: tuple[str, ...] = ("per share", "per diluted", "per basic")
# Share / unit COUNT rows — a count, not a currency level; the $-scale would
# be wrong and the true unit (COUNT) needs the share-scale, not the $-scale.
COUNT_TOKENS: tuple[str, ...] = (
    "number of",
    "shares outstanding",
    "weighted-average number",
    "weighted average number",
    "shares issued",
    "shares authorized",
    # Explicit unit-declaration parentheticals — see generic_xbrl_capture's
    # original comment history for the BN/RBRK cases this guards against.
    "(in shares)",
    "(shares)",
    "(in units)",
    "(units)",
)

# A unit suffix that declares per-share OR share-count content marks a
# UNIT-MIXED section: it interleaves per-share dollars, share counts, and
# aggregate dollars under one parse_units scale — DEFER the whole section.
PER_SHARE_SECTION_RX = re.compile(r"/\s*shares|per\s+share|shares\s+in\b", re.IGNORECASE)

# The statement-of-changes-in-equity / share-rollforward family is
# unit-ambiguous in the WORST way (FMP mislabels share-count rollforwards
# under a $-scale title) — DEFER the whole equity family to Stage B.
_EQUITY_HEAD_RX = re.compile(
    r"(?:common stock,?\s+|stockholders'?\s+|shareholders'?\s+)?equity\b(.*)",
    re.IGNORECASE,
)
# Backward-compat alias for the historical private name (generic_xbrl_capture
# imports it as ``_EQUITY_HEAD_RX``).
EQUITY_HEAD_RX = _EQUITY_HEAD_RX
CHANGES_IN_EQUITY_TOKENS = (
    "changes in equity",
    "changes in stockholders",
    "changes in shareholders",
)

# Strip the parse_units suffix (" - USD ($) $ in Millions") and the XBRL
# "(Details)"/"(Tables)"/"(Parenthetical)" boilerplate from an inner section
# title to get a clean semantic stem for the metric name.
UNIT_SUFFIX_RX = re.compile(r"\s+-\s+[A-Z]{3}\s*\(.*$")
DETAILS_SUFFIX_RX = re.compile(r"\s*\((?:Details|Tables|Parenthetical)[^)]*\)\s*$", re.IGNORECASE)

# kpi_definitions.name / KpiValue.name is VARCHAR(200); clip section-qualified
# names to fit (the canonical match key folds variants regardless).
NAME_MAX = 200

# A monetary cell that survives every label/magnitude guard but whose
# pre-scale magnitude sits in the percent/ratio band is a RESIDUAL mis-scale
# risk — captured (the program directive: capture every number), but
# down-weighted rather than trusted at face confidence.
RESIDUAL_RISK_RAW_CEILING = Decimal("100")
RESIDUAL_RISK_CONFIDENCE = 0.5


def is_unit_ambiguous_section(inner_title: str) -> bool:
    """True for sections whose declared scale can't be trusted per-row:
    per-share data, share-count columns, or the equity / share-rollforward
    family."""
    if PER_SHARE_SECTION_RX.search(inner_title):
        return True
    s = semantic_section_title(inner_title).lower().strip()
    if any(tok in s for tok in CHANGES_IN_EQUITY_TOKENS):
        return True
    m = EQUITY_HEAD_RX.match(s)
    if m is not None:
        rest = m.group(1).lstrip()
        if rest == "" or not rest[:1].isalnum():
            return True
    return False


def classify_value(label: str, raw: object, factor: int) -> tuple[Decimal | None, str, float]:
    """Decide whether a cell is a captureable monetary value.

    Returns ``(scaled_value, "", confidence)`` to capture, or
    ``(None, reason, 1.0)`` to defer.
    """
    ll = label.lower()
    if any(tok in ll for tok in RATE_TOKENS):
        return None, "rate_or_percent", 1.0
    if any(tok in ll for tok in COUNT_TOKENS):
        return None, "share_or_count", 1.0
    try:
        dv = Decimal(str(raw))
    except (InvalidOperation, ValueError, TypeError):
        return None, "non_numeric", 1.0
    if any(tok in ll for tok in PER_SHARE_TOKENS):
        return dv, "", 1.0
    if abs(dv) < 1:
        return None, "subunit_magnitude", 1.0
    confidence = RESIDUAL_RISK_CONFIDENCE if abs(dv) < RESIDUAL_RISK_RAW_CEILING else 1.0
    return dv * factor, "", confidence


def semantic_section_title(inner_title: str) -> str:
    """Clean stem for the metric name: title minus the unit + '(Details)' suffix."""
    t = UNIT_SUFFIX_RX.sub("", inner_title)
    t = DETAILS_SUFFIX_RX.sub("", t)
    return " ".join(t.split())


def build_name(section_title: str, axis_path: list[str], label: str) -> str:
    """Section-qualified metric name: ``section — axis — row label``."""
    parts = [section_title, *axis_path, label]
    cleaned: list[str] = []
    for raw in parts:
        p = " ".join(str(raw).split())
        if not p:
            continue
        if p.endswith(("[Line Items]", "[Roll Forward]", "[Abstract]", "[Member]")):
            continue
        if cleaned and cleaned[-1].casefold() == p.casefold():
            continue
        cleaned.append(p)
    return " — ".join(cleaned)[:NAME_MAX]
