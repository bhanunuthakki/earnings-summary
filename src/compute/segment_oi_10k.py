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

from models.facts import FiscalPeriodType, SegmentFact, Unit
from pipeline.segment_junction_writer import write_segment_facts_via_junction

_DOC_TYPES: frozenset[str] = frozenset({"fmp_10k_json", "fmp_10q_json"})
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
    r"[a-z]*\.?\s+(\d{1,2}),?\s+(\d{4})\b"
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


def _quarter_from_month(month: int) -> FiscalPeriodType | None:
    """Calendar-quarter mapping; off-calendar fiscal years use this as a default."""
    if month in (1, 2, 3, 4):
        return FiscalPeriodType.Q1
    if month in (5, 6, 7):
        return FiscalPeriodType.Q2
    if month in (8, 9, 10):
        return FiscalPeriodType.Q3
    if month in (11, 12):
        return FiscalPeriodType.Q4
    return None


def _period_type_from_descriptor(
    descriptor: str | None, period_end: datetime
) -> FiscalPeriodType | None:
    """'12 Months Ended' -> FY; '3 Months Ended' -> Qx (calendar); cumulative -> None."""
    if descriptor is None:
        return None
    desc = descriptor.lower()
    if "12 months" in desc:
        return FiscalPeriodType.FY
    if "3 months" in desc:
        return _quarter_from_month(period_end.month)
    # 6 / 9 / other cumulative periods — skip for now
    return None


def _resolve_periods(
    section: list[object],
) -> list[tuple[FiscalPeriodType | None, datetime | None]]:
    """For each items-column, return (period_type, period_end) or (None, None) to skip.

    The first row's value list contains period descriptors (e.g.
    ['3 Months Ended', None, '9 Months Ended']); the 'items' row has the
    column dates. The descriptor list and items list rarely match in length —
    in mixed-period sections, descriptors are split by None separators and
    each non-None descriptor applies to a contiguous group of items columns.
    """
    if not section or not isinstance(section[0], dict):
        return []
    title_vals = next(iter(section[0].values()))
    if not isinstance(title_vals, list):
        return []
    items_row: list[object] | None = None
    for row in section[:5]:
        if isinstance(row, dict) and "items" in row:
            v = row["items"]
            if isinstance(v, list):
                items_row = v
            break
    if items_row is None:
        return []

    descriptors = [d for d in title_vals if d is not None]
    n_cols = len(items_row)
    if not descriptors:
        per_col_descriptor: list[str | None] = [None] * n_cols
    elif len(descriptors) == 1:
        per_col_descriptor = [descriptors[0]] * n_cols
    else:
        cols_per_group = n_cols // len(descriptors)
        if cols_per_group * len(descriptors) != n_cols:
            per_col_descriptor = [descriptors[0]] * n_cols
        else:
            per_col_descriptor = [descriptors[i // cols_per_group] for i in range(n_cols)]

    out: list[tuple[FiscalPeriodType | None, datetime | None]] = []
    for i, item in enumerate(items_row):
        if not isinstance(item, str):
            out.append((None, None))
            continue
        period_end = _parse_date(item)
        if period_end is None:
            out.append((None, None))
            continue
        if i >= len(per_col_descriptor):
            out.append((None, None))
            continue
        period_type = _period_type_from_descriptor(per_col_descriptor[i], period_end)
        out.append((period_type, period_end))
    return out


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
    "income (loss) from operations",
    # Bank-style: segments don't separate non-operating items, so segment net
    # income is the closest OI proxy (used by JPM, SOFI).
    "net income/(loss)",
    "net income (loss)",
    # Homebuilder/industrial-style (used by WY, TOL).
    "net contribution (charge) to earnings",
    "net contribution to earnings",
    "earnings before income taxes",
    # Pre-tax (banks have minimal non-operating items at segment level).
    "income/(loss) before income tax",
    "income (loss) before income taxes",
)
# Per-segment capex disclosures live in their own section in capital-intensive
# 10-Ks. Labels span "Capital expenditures" (most common), "Property and
# equipment additions" (AMZN convention), and a few synonyms — kept broad
# enough to catch common variants without false-positive on a balance-sheet
# stock like "Property and equipment, net" (matcher is whitespace-anchored).
_CAPEX_PREFIXES: tuple[str, ...] = (
    "capital expenditures",
    "capital expenditure",
    "capital additions",
    "property and equipment additions",
    "purchases of property",
    "purchases of property, plant and equipment",
    "additions to long-lived assets",
    "additions to property",
    "additions to property, plant and equipment",
)
# Per-segment headcount disclosures are rarer than capex, but a few filers
# break them out — typically in human-capital sub-sections. The matcher is
# whitespace-anchored so "Employee benefits" (a comp line item) won't trip
# on the "employees" prefix.
_HEADCOUNT_PREFIXES: tuple[str, ...] = (
    "employees",
    "full-time employees",
    "headcount",
    "associates",
    "number of employees",
    "total employees",
)


def _matches_any(label: str, prefixes: tuple[str, ...]) -> bool:
    """Case-insensitive prefix match against any of the given strings."""
    lower = label.lower().strip()
    return any(lower == p or lower.startswith(p + " ") for p in prefixes)


def _is_revenue_label(label: str) -> bool:
    return _matches_any(label, _REVENUE_PREFIXES)


def _is_costs_label(label: str) -> bool:
    return _matches_any(label, _COSTS_PREFIXES)


def _is_oi_label(label: str) -> bool:
    return _matches_any(label, _OI_PREFIXES)


def _is_capex_label(label: str) -> bool:
    return _matches_any(label, _CAPEX_PREFIXES)


def _is_headcount_label(label: str) -> bool:
    return _matches_any(label, _HEADCOUNT_PREFIXES)


def _normalize_segment_name(label: str) -> str:
    """Strip XBRL prefixes like 'Operating Segments | ' from segment header labels."""
    if " | " in label:
        return label.rsplit(" | ", 1)[-1].strip()
    return label.strip()


def _emit_metric(
    segment: str,
    metric: str,
    values_per_period: list[float | int],
    periods: list[tuple[FiscalPeriodType | None, datetime | None]],
    ticker: str,
    source_doc_id: int,
    scale: int,
    unit: Unit,
) -> list[SegmentFact]:
    """Emit one SegmentFact per recognized period column for a metric row.

    Same period-column filtering as _emit_oi (skip None descriptors). Used
    for direct-emit metrics (capex, headcount) where a single row carries
    the segment's value verbatim — no rev/cost subtraction needed.
    """
    out: list[SegmentFact] = []
    for i, (period_type, period_end) in enumerate(periods):
        if period_type is None or period_end is None or i >= len(values_per_period):
            continue
        v = values_per_period[i]
        if not isinstance(v, (int, float)):
            continue
        out.append(
            SegmentFact(
                ticker=ticker,
                period_end=period_end,
                fiscal_period_type=period_type,
                segment_name=segment,
                metric=metric,
                value=Decimal(str(v * scale)),
                currency=None,
                unit=unit,
                source_doc_id=source_doc_id,
            )
        )
    return out


def _emit_oi(
    segment: str,
    oi_per_period: list[float | int],
    periods: list[tuple[FiscalPeriodType | None, datetime | None]],
    ticker: str,
    source_doc_id: int,
    scale: int,
) -> list[SegmentFact]:
    """Emit OI rows directly from a 'Total income from operations'-style row.

    Skips columns whose period_type is None (cumulative 6/9-month YTD columns
    in 10-Qs, or columns whose descriptor we don't recognize).
    """
    out: list[SegmentFact] = []
    for i, (period_type, period_end) in enumerate(periods):
        if period_type is None or period_end is None or i >= len(oi_per_period):
            continue
        v = oi_per_period[i]
        if not isinstance(v, (int, float)):
            continue
        out.append(
            SegmentFact(
                ticker=ticker,
                period_end=period_end,
                fiscal_period_type=period_type,
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
    periods: list[tuple[FiscalPeriodType | None, datetime | None]],
    ticker: str,
    source_doc_id: int,
    scale: int,
) -> list[SegmentFact]:
    """Compute OI = revenue - costs and emit one SegmentFact per recognized period column."""
    out: list[SegmentFact] = []
    for i, (period_type, period_end) in enumerate(periods):
        if period_type is None or period_end is None:
            continue
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
                period_end=period_end,
                fiscal_period_type=period_type,
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
    periods: list[tuple[FiscalPeriodType | None, datetime | None]],
    ticker: str,
    source_doc_id: int,
    scale: int,
) -> list[SegmentFact]:
    """Walk a segment-OI section and emit one row per (segment, period column).

    `periods[i]` is (period_type, period_end) for items-column i, or
    (None, None) to skip. Two emit paths: prefer direct OI ('Total income
    from operations' / 'Operating Income (Loss)') when present; otherwise
    compute OI = revenue - costs.
    """
    facts: list[SegmentFact] = []
    current_segment: str | None = None
    pending_revenue: list[float | int] | None = None
    pending_costs: list[float | int] | None = None
    pending_oi: list[float | int] | None = None
    pending_capex: list[float | int] | None = None
    pending_headcount: list[float | int] | None = None

    def flush() -> None:
        nonlocal pending_revenue, pending_costs, pending_oi
        nonlocal pending_capex, pending_headcount
        if current_segment is not None:
            segment = _normalize_segment_name(current_segment)
            if pending_oi is not None:
                facts.extend(_emit_oi(segment, pending_oi, periods, ticker, source_doc_id, scale))
            elif pending_revenue is not None and pending_costs is not None:
                facts.extend(
                    _emit_oi_from_rev_costs(
                        segment,
                        pending_revenue,
                        pending_costs,
                        periods,
                        ticker,
                        source_doc_id,
                        scale,
                    )
                )
            if pending_capex is not None:
                facts.extend(
                    _emit_metric(
                        segment,
                        "capex",
                        pending_capex,
                        periods,
                        ticker,
                        source_doc_id,
                        scale,
                        Unit.ACTUAL,
                    )
                )
            if pending_headcount is not None:
                # Headcount is a count, not a currency-scaled amount — ignore
                # the section's "$ in Millions" multiplier so an employee
                # count of 100,000 doesn't end up as 100B.
                facts.extend(
                    _emit_metric(
                        segment,
                        "headcount",
                        pending_headcount,
                        periods,
                        ticker,
                        source_doc_id,
                        1,
                        Unit.COUNT,
                    )
                )
        pending_revenue = None
        pending_costs = None
        pending_oi = None
        pending_capex = None
        pending_headcount = None

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
        elif _is_capex_label(label):
            pending_capex = values  # type: ignore[assignment]
        elif _is_headcount_label(label):
            pending_headcount = values  # type: ignore[assignment]
    flush()
    return facts


def extract_segment_oi_from_record(
    record: dict[str, object], source_doc_id: int, ticker: str
) -> list[SegmentFact]:
    """Scan a 10-K or 10-Q JSON top-level dict; return all segment_facts derived from segment-OI sections."""
    section_keys = _find_segment_oi_sections(record)
    out: list[SegmentFact] = []
    for key in section_keys:
        section = record[key]
        if not isinstance(section, list):
            continue
        periods = _resolve_periods(section)
        if not periods:
            continue
        title = next(iter(section[0].keys()), "") if isinstance(section[0], dict) else ""
        scale = _detect_scale(title)
        out.extend(_walk_section(section, periods, ticker, source_doc_id, scale))
    return out


def _insert_segment_facts(conn: sqlite3.Connection, facts: list[SegmentFact]) -> int:
    """Persist the batch through segment_periods + segment_dimensions.

    Returns the number of NEW dimension rows inserted — caller treats this
    as the "facts written" count (a duplicate run inserts zero). Mixed-unit
    batches (ACTUAL revenue/OI/capex + COUNT headcount) share one period
    anchor; the writer stores each dim's unit on its row when it differs
    from the period anchor's unit (per migration 0057).
    """
    return write_segment_facts_via_junction(conn, facts)


def extract_segment_oi_facts(conn: sqlite3.Connection, document_id: int, project_root: Path) -> int:
    """Read 10-K or 10-Q JSON, walk segment-OI sections, write the junction. Idempotent.

    Auto-detects 10-K vs 10-Q via doc_type; the per-column period resolver
    handles both annual and quarterly section shapes. All fact rows (OI,
    capex, headcount) flow through `_insert_segment_facts` into
    segment_periods + segment_dimensions — the legacy `segment_facts` table
    was retired in migration 0057, and `_mirror_facts_to_junction` was
    removed since `_insert_segment_facts` is now the single persistence
    path.
    """
    cur = conn.cursor()
    cur.execute("SELECT ticker, file_path, doc_type FROM documents WHERE id = ?", (document_id,))
    row = cur.fetchone()
    if row is None:
        raise ValueError(f"No document with id={document_id}")
    if row["doc_type"] not in _DOC_TYPES:
        raise ValueError(
            f"Document {document_id} is doc_type={row['doc_type']!r}, "
            f"not one of {sorted(_DOC_TYPES)}"
        )
    ticker = row["ticker"]
    abs_path = project_root / row["file_path"]
    with open(abs_path, encoding="utf-8") as f:
        record = json.load(f)
    if not isinstance(record, dict):
        raise ValueError(f"Expected dict in {abs_path}, got {type(record).__name__}")

    facts = extract_segment_oi_from_record(record, source_doc_id=document_id, ticker=ticker)
    inserted = _insert_segment_facts(conn, facts)
    conn.commit()
    return inserted
