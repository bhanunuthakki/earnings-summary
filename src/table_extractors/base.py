"""Shared primitives for FMP-XBRL section parsing.

The FMP `form_10k` JSON files we cache at
`data/historical/fmp/{TICKER}_form_10k_{YEAR}.json` contain each filing's
tables already broken out as top-level sections. Each section is a list
of dicts with this shape:

    [
        {"<section title with unit hint>": ["<period 1>", "<period 2>", ...]},
        {"items": ["<period 1>", "<period 2>", ...]},               # optional
        {"<axis-label>": [\xa0, \xa0, ...]},                         # dimension marker
        {"<row-label>":  [<numeric>, <numeric>, ...]},               # concrete data row
        ...
    ]

A "dimension marker" is a row whose values are all unicode non-breaking
spaces (`\xa0`) — it acts as a header scoping all subsequent concrete
rows until the next dimension marker. The XBRL semantic: each concrete
row is reported under a chain of dimensions (axes).

This module exposes:

    XbrlRow             — typed view of one concrete row + its axis chain
    iter_xbrl_table     — walk a section, yield XbrlRow items
    parse_units         — extract Units(currency, scale) from a section title
    ExtractorContract   — the shape every per-kind extractor implements
    ExtractionOutcome   — return type for the orchestrator
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterator
from dataclasses import dataclass
from typing import cast

log = logging.getLogger(__name__)

# Unicode non-breaking space; FMP uses this to mark "no value in this column".
NBSP = "\xa0"


@dataclass(slots=True)
class Units:
    """Currency + scale parsed from a section title.

    Section titles look like
        'Leases - Future Minimum Lease Payments (Details) - USD ($) $ in Millions'
    so we infer currency='USD' and scale='millions'.
    """

    currency: str = "USD"
    scale: str = "units"  # 'thousands' | 'millions' | 'billions' | 'units'


@dataclass(slots=True)
class XbrlRow:
    """One concrete data row from an FMP XBRL section.

    Attributes:
        label: The leading key of the row dict (e.g. '2025', 'Total').
        axis_path: The chain of dimension labels that scope this row,
            most-recent last. Example: ['Operating Leases'].
        values: List of numeric values, one per period column.
        period_labels: The period column headers (e.g. ['Dec. 31, 2024', ...]).
    """

    label: str
    axis_path: list[str]
    values: list[float | int | None]
    period_labels: list[str]


@dataclass(slots=True)
class ExtractionOutcome:
    """Return type for every per-kind extractor.

    Stable enough across kinds that the orchestrator can aggregate without
    inspecting payload-specific shape.
    """

    table_kind: str
    ticker: str
    fiscal_year: int | None
    n_rows_proposed: int = 0
    n_rows_inserted: int = 0
    n_extractions_logged: int = 0
    n_mapping_proposals: int = 0
    status: str = "ok"  # 'ok' | 'skipped' | 'no_data' | 'llm_failed' | 'parse_failed'
    notes: str = ""


@dataclass(slots=True)
class ExtractorContract:
    """The contract every per-kind extractor's `extract` function obeys.

    Documented here as a dataclass for IDE introspection / docs purposes.
    The actual function signature mirrors these attributes.
    """

    table_kind: str
    extractor_id: str  # used in extractions.extractor_id
    extractor_version: str  # used in extractions.extractor_version


_SCALE_RX = re.compile(r"\bin\s+(thousands|millions|billions)\b", re.IGNORECASE)
_CURRENCY_RX = re.compile(r"\b(USD|EUR|GBP|JPY|DKK|BRL|CAD|CNY|CNH|AUD|CHF)\b")


def parse_units(section_title: str) -> Units:
    """Pull currency + scale out of a section-title suffix.

    Recognized suffixes (case-insensitive):
        'USD ($) $ in Millions'
        'USD ($) shares in Millions, $ in Millions'
        'DKK (kr) kr in Thousands'

    Falls back to ('USD', 'units') when nothing matches — safe default
    that won't mis-scale a value silently (caller can spot 'units' and
    flag for review).
    """
    currency_m = _CURRENCY_RX.search(section_title)
    scale_m = _SCALE_RX.search(section_title)
    currency = currency_m.group(1).upper() if currency_m else "USD"
    scale = scale_m.group(1).lower() if scale_m else "units"
    return Units(currency=currency, scale=scale)


def _is_axis_marker(values: list[object]) -> bool:
    """All-NBSP (or empty / None) row → it's an axis marker, not data."""
    if not values:
        return True
    for v in values:
        if v is None:
            continue
        if isinstance(v, str) and (v == NBSP or v.strip() == "" or set(v) == {NBSP}):
            continue
        return False
    return True


def _to_numeric(v: object) -> float | int | None:
    """Coerce a value to numeric or None."""
    if v is None:
        return None
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return v
    if isinstance(v, str):
        s = v.strip()
        if s in {"", NBSP} or set(s) == {NBSP}:
            return None
        try:
            if "." in s or "e" in s or "E" in s:
                return float(s)
            return int(s)
        except ValueError:
            return None
    return None


def iter_xbrl_table(section: list[dict[str, object]]) -> Iterator[XbrlRow]:
    """Walk an FMP section, yielding one XbrlRow per concrete data row.

    Behavior:
      - The first dict in `section` is the section header. Its key is the
        section title (we ignore it here — callers pass the title separately
        via `parse_units`). Its value is usually ['12 Months Ended'] or
        the actual period labels.
      - If the next dict has key 'items', its value is the canonical period
        labels (overrides the header dict).
      - Subsequent dicts are either axis markers (all-NBSP values) — which
        we push onto the axis stack — or concrete data rows (yielded).
      - There is no formal "pop axis" — XBRL sections are linear; each
        axis marker REPLACES the most recent axis at its depth. We treat
        consecutive axis markers as siblings: a new axis marker replaces
        the previous one in `axis_path`. This is a simplification but
        works for the table kinds we currently support (lease ladders,
        goodwill rollforward, share repurchases, etc.) where the axis
        depth is always 1.
    """
    if not section:
        return

    # Resolve period labels: prefer 'items' dict when present, else the
    # header dict's value.
    period_labels: list[str] = []
    body_start = 0
    if len(section) >= 1:
        head_v = next(iter(section[0].values()))
        if isinstance(head_v, list):
            period_labels = [str(x) for x in head_v]
        body_start = 1
    if len(section) >= 2:
        second = section[1]
        if "items" in second and isinstance(second["items"], list):
            period_labels = [str(x) for x in cast("list[object]", second["items"])]
            body_start = 2

    axis_path: list[str] = []
    for entry in section[body_start:]:
        if not isinstance(entry, dict) or not entry:
            continue
        label = next(iter(entry.keys()))
        raw_values = entry[label]
        if not isinstance(raw_values, list):
            continue
        values_objs: list[object] = list(cast("list[object]", raw_values))
        if _is_axis_marker(values_objs):
            # Replace the last axis at the same depth; for now we treat
            # all axis markers as depth-1 (overwrite). The label that
            # ends with '[Line Items]' or '[Roll Forward]' is XBRL
            # taxonomy boilerplate that we skip — it shouldn't push an
            # axis because it doesn't scope anything semantically.
            if label.endswith(("[Line Items]", "[Roll Forward]", "[Abstract]")):
                continue
            axis_path = [label]
            continue
        yield XbrlRow(
            label=label,
            axis_path=list(axis_path),
            values=[_to_numeric(v) for v in values_objs],
            period_labels=list(period_labels),
        )
