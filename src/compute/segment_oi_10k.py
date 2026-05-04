"""Extract segment operating income from FMP-served 10-K JSON segment notes.

The 10-K JSON is the parsed contents of the SEC filing. The segment-note
section typically reports total revenue and total costs per segment;
operating income is computed by subtraction. This extractor:

1. Finds top-level sections whose first row's title contains 'Segment'
   plus 'Operating Income' (case-sensitive — matches the filer convention).
2. Parses the column headers ('items' row) as fiscal-year-end dates.
3. Walks subsequent rows; tracks the current segment by spotting header
   rows where all values are NBSP (the FMP convention for empty cells).
4. For each segment, captures 'Total revenues' and 'Total costs and
   expenses' values, computes OI = revenue - costs, and emits a
   segment_facts row with metric='operating_income'.

Numeric scale is detected from the section title ('$ in Millions',
'$ in Thousands', etc.) and applied so values are stored in raw
currency units (Unit.ACTUAL) — same convention as the FMP regular
statements, for consistency in the metrics SQL view.

Heuristic by design — not all filers use the same shape. Tickers whose
shape doesn't match yield zero facts, which the bulk CLI logs as an OK
extraction with 0 inserted. Future iterations add ticker-specific overrides.
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from compute._common import load_document_row
from models.facts import FiscalPeriodType, SegmentFact, Unit

_DOC_TYPE = "fmp_10k_json"
_NBSP = "\xa0"

_MONTH_NUM: dict[str, int] = {
    "Jan": 1,
    "Feb": 2,
    "Mar": 3,
    "Apr": 4,
    "May": 5,
    "Jun": 6,
    "Jul": 7,
    "Aug": 8,
    "Sep": 9,
    "Oct": 10,
    "Nov": 11,
    "Dec": 12,
}

_DATE_RX = re.compile(
    r"^(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)"
    r"[a-z]*\.?\s+(\d{1,2}),?\s+(\d{4})$"
)


def _parse_date(s: str) -> datetime | None:
    """Parse 'Dec. 31, 2024' / 'December 31, 2024' / 'Sept. 28, 2024' to datetime."""
    m = _DATE_RX.match(s.strip())
    if m is None:
        return None
    mon = _MONTH_NUM.get(m.group(1))
    if mon is None:
        return None
    return datetime(int(m.group(3)), mon, int(m.group(2)))


def _detect_scale(title: str) -> int:
    """Read 'in Millions' / 'in Thousands' / 'in Billions' from a table title."""
    t = title.lower()
    if "in thousand" in t:
        return 1_000
    if "in million" in t:
        return 1_000_000
    if "in billion" in t:
        return 1_000_000_000
    return 1


def _is_blank_row(values: list[object]) -> bool:
    """True if every value is NBSP, empty, or None — i.e. a segment header row."""
    return all(v == _NBSP or v == "" or v is None for v in values)


def _is_xbrl_filler_label(label: str) -> bool:
    """Skip XBRL '[Line Items]' filler rows — they're inside each segment block,
    not real segment headers. Without this guard the walker mistakes them for a
    new segment and overwrites current_segment with the boilerplate label.
    """
    return "[line items]" in label.lower()


def _find_segment_oi_sections(record: dict[str, object]) -> list[str]:
    """Return top-level keys whose title looks like a segment-data table.

    Matches any section whose title contains 'segment' (case-insensitive) and
    is not a (Tables) listing or a Narrative row. The walker emits facts only
    when 'Total revenues'/'Total costs and expenses'/'Total income from
    operations' rows are present, so non-data sections quietly produce zero
    facts rather than false positives.
    """
    matches: list[str] = []
    for key, section in record.items():
        if not isinstance(section, list) or not section:
            continue
        first = section[0]
        if not isinstance(first, dict):
            continue
        title = next(iter(first.keys()), "")
        title_lower = title.lower()
        if "segment" not in title_lower:
            continue
        if "(tables)" in title_lower or "narrative" in title_lower:
            continue
        matches.append(key)
    return matches


def _parse_period_ends(section: list[object]) -> list[datetime] | None:
    """Find the 'items' row and parse its column headers as fiscal-year-end dates."""
    for row in section[:5]:
        if not isinstance(row, dict):
            continue
        items = row.get("items")
        if not isinstance(items, list):
            continue
        out: list[datetime] = []
        for s in items:
            if not isinstance(s, str):
                return None
            d = _parse_date(s)
            if d is None:
                return None
            out.append(d)
        return out
    return None


# Each label-matcher returns True if the row is a candidate for that
# pending-bucket. Order matters: revenue patterns checked before OI patterns
# (some filers use "Operating revenues" — clearly revenue, not OI).

_REVENUE_PREFIXES: tuple[str, ...] = (
    "total revenues",
    "total revenue",
    "net sales",
    "operating revenues",
    "revenue",
)
_COSTS_PREFIXES: tuple[str, ...] = (
    "total costs and expenses",
    "total operating expenses",
    "operating expenses",
    "costs and expenses",
)
_OI_PREFIXES: tuple[str, ...] = (
    "total income from operations",
    "operating income (loss)",
    "operating income",
    "segment operating income",
    "income from operations",
)


def _matches_any(label: str, prefixes: tuple[str, ...]) -> bool:
    """Case-insensitive prefix match against any of the given strings."""
    lower = label.lower().strip()
    return any(lower == p or lower.startswith(p + " ") or lower == p for p in prefixes)


def _is_revenue_label(label: str) -> bool:
    return _matches_any(label, _REVENUE_PREFIXES)


def _is_costs_label(label: str) -> bool:
    return _matches_any(label, _COSTS_PREFIXES)


def _is_oi_label(label: str) -> bool:
    return _matches_any(label, _OI_PREFIXES)


def _normalize_segment_name(label: str) -> str:
    """Strip XBRL prefixes like 'Operating Segments | ' from segment header labels."""
    if " | " in label:
        return label.rsplit(" | ", 1)[-1].strip()
    return label.strip()


def _emit_oi(
    segment: str,
    oi_per_period: list[float | int],
    period_ends: list[datetime],
    ticker: str,
    source_doc_id: int,
    scale: int,
) -> list[SegmentFact]:
    """Emit OI rows directly from a 'Total income from operations' row's values."""
    out: list[SegmentFact] = []
    for i, pe in enumerate(period_ends):
        if i >= len(oi_per_period):
            continue
        v = oi_per_period[i]
        if not isinstance(v, (int, float)):
            continue
        out.append(
            SegmentFact(
                ticker=ticker,
                period_end=pe,
                fiscal_period_type=FiscalPeriodType.FY,
                segment_name=segment,
                metric="operating_income",
                value=Decimal(str(v * scale)),
                currency=None,
                unit=Unit.ACTUAL,
                source_doc_id=source_doc_id,
            )
        )
    return out


def _emit_oi_from_rev_costs(
    segment: str,
    revenue_per_period: list[float | int],
    costs_per_period: list[float | int],
    period_ends: list[datetime],
    ticker: str,
    source_doc_id: int,
    scale: int,
) -> list[SegmentFact]:
    """Compute OI = revenue - costs and emit one SegmentFact per period."""
    out: list[SegmentFact] = []
    for i, pe in enumerate(period_ends):
        if i >= len(revenue_per_period) or i >= len(costs_per_period):
            continue
        rev_raw = revenue_per_period[i]
        cost_raw = costs_per_period[i]
        if not isinstance(rev_raw, (int, float)) or not isinstance(cost_raw, (int, float)):
            continue
        oi = (rev_raw - cost_raw) * scale
        out.append(
            SegmentFact(
                ticker=ticker,
                period_end=pe,
                fiscal_period_type=FiscalPeriodType.FY,
                segment_name=segment,
                metric="operating_income",
                value=Decimal(str(oi)),
                currency=None,
                unit=Unit.ACTUAL,
                source_doc_id=source_doc_id,
            )
        )
    return out


def _walk_section(
    section: list[object],
    period_ends: list[datetime],
    ticker: str,
    source_doc_id: int,
    scale: int,
) -> list[SegmentFact]:
    """Walk a segment-OI section and emit one row per (segment, period).

    Two emit paths: prefer a 'Total income from operations' row (direct OI)
    when present; otherwise compute OI from 'Total revenues' minus 'Total
    costs and expenses'.
    """
    facts: list[SegmentFact] = []
    current_segment: str | None = None
    pending_revenue: list[float | int] | None = None
    pending_costs: list[float | int] | None = None
    pending_oi: list[float | int] | None = None

    def flush() -> None:
        nonlocal pending_revenue, pending_costs, pending_oi
        if current_segment is not None:
            segment = _normalize_segment_name(current_segment)
            if pending_oi is not None:
                facts.extend(
                    _emit_oi(segment, pending_oi, period_ends, ticker, source_doc_id, scale)
                )
            elif pending_revenue is not None and pending_costs is not None:
                facts.extend(
                    _emit_oi_from_rev_costs(
                        segment,
                        pending_revenue,
                        pending_costs,
                        period_ends,
                        ticker,
                        source_doc_id,
                        scale,
                    )
                )
        pending_revenue = None
        pending_costs = None
        pending_oi = None

    for row in section[2:]:
        if not isinstance(row, dict) or len(row) != 1:
            continue
        label, values = next(iter(row.items()))
        if not isinstance(values, list):
            continue
        if _is_blank_row(values):
            if _is_xbrl_filler_label(label):
                continue
            flush()
            current_segment = label
            continue
        if _is_revenue_label(label):
            pending_revenue = values  # type: ignore[assignment]
        elif _is_costs_label(label):
            pending_costs = values  # type: ignore[assignment]
        elif _is_oi_label(label):
            pending_oi = values  # type: ignore[assignment]
    flush()
    return facts


def extract_segment_oi_from_record(
    record: dict[str, object], source_doc_id: int, ticker: str
) -> list[SegmentFact]:
    """Scan a 10-K JSON top-level dict; return all segment_facts derived from segment-OI sections."""
    section_keys = _find_segment_oi_sections(record)
    out: list[SegmentFact] = []
    for key in section_keys:
        section = record[key]
        if not isinstance(section, list):
            continue
        period_ends = _parse_period_ends(section)
        if period_ends is None:
            continue
        title = next(iter(section[0].keys()), "") if isinstance(section[0], dict) else ""
        scale = _detect_scale(title)
        out.extend(_walk_section(section, period_ends, ticker, source_doc_id, scale))
    return out


def _insert_segment_facts(conn: sqlite3.Connection, facts: list[SegmentFact]) -> int:
    insert_sql = (
        "INSERT OR IGNORE INTO segment_facts "
        "(ticker, period_end, fiscal_period_type, segment_name, metric, value, "
        " currency, unit, source_doc_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
    )
    inserted = 0
    for f in facts:
        result = conn.execute(
            insert_sql,
            (
                f.ticker,
                f.period_end,
                f.fiscal_period_type.value,
                f.segment_name,
                f.metric,
                str(f.value),
                f.currency.value if f.currency is not None else None,
                f.unit.value,
                f.source_doc_id,
            ),
        )
        if result.rowcount > 0:
            inserted += 1
    return inserted


def extract_segment_oi_facts(conn: sqlite3.Connection, document_id: int, project_root: Path) -> int:
    """Read 10-K JSON, walk segment-OI sections, write segment_facts. Idempotent."""
    ticker, file_path_str = load_document_row(conn, document_id, _DOC_TYPE)
    abs_path = project_root / file_path_str
    with open(abs_path, encoding="utf-8") as f:
        record = json.load(f)
    if not isinstance(record, dict):
        raise ValueError(f"Expected dict in {abs_path}, got {type(record).__name__}")

    facts = extract_segment_oi_from_record(record, source_doc_id=document_id, ticker=ticker)
    inserted = _insert_segment_facts(conn, facts)
    conn.commit()
    return inserted
