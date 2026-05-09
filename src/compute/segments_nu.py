"""NU-specific segment_facts extractor from FMP-served 10-K JSON.

NU files 20-F as a Cayman-incorporated foreign private issuer; FMP normalizes
the filing into the same `form_10k_*.json` shape used for US-GAAP 10-Ks. The
segment note follows IFRS conventions, which the generic `segment_oi_10k`
walker cannot decode:

  Segment information (Details):
    [0] title
    [1] items                                  # period-end column headers
    [2] IfrsStatementLineItems [Line Items]    # XBRL filler header
    [3] Revenue                                # consolidated revenue
    [4] "Reportable segments [member]"         # group header (all NBSP)
    [5] IfrsStatementLineItems [Line Items]
    [6] Revenue                                # reportable-segments aggregate
    [7] Non current assets
    [8] "Brazil [Member] | Reportable segments [member]"   # geography header
    [9] IfrsStatementLineItems [Line Items]
   [10] Revenue                                # Brazil revenue per period
   [11] Non current assets
    ...repeats per geography (Brazil, Mexico, Colombia, Cayman, Germany,
        Argentina, United States).

Each `<Geography> [Member] | Reportable segments [member]` line introduces a
geographic operating segment. We emit one segment_fact per (geography, period)
with `metric='revenue_by_geography'`. NU does not break out segment OI or
costs; this is a revenue-only labeler. Quarterly NU data lives in 6-K filings
not in the 10-K JSON, so this currently yields annual rows only.
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from compute.segment_oi_10k import (
    _detect_scale,
    _find_segment_oi_sections,
    _is_blank_row,
    _is_xbrl_filler_label,
    _resolve_periods,
)
from models.facts import Currency, FiscalPeriodType, SegmentFact, Unit

_TICKER = "NU"
_DOC_TYPES: frozenset[str] = frozenset({"fmp_10k_json", "fmp_10q_json"})

_GEO_HEADER_RX = re.compile(
    r"^(?P<geo>.+?)\s*\[Member\]\s*\|\s*Reportable\s+segments\s*\[member\]$",
    re.IGNORECASE,
)


def _is_geography_header(label: str) -> bool:
    return bool(_GEO_HEADER_RX.match(label))


def _normalize_geography(label: str) -> str:
    m = _GEO_HEADER_RX.match(label)
    return m.group("geo").strip() if m else label.strip()


def _is_revenue_label(label: str) -> bool:
    return label.lower().strip() == "revenue"


def extract_nu_segment_facts_from_record(
    record: dict[str, object], source_doc_id: int
) -> list[SegmentFact]:
    """Walk the NU `Segment information (Details)` section, emit revenue-by-geography facts.

    Pure function — no DB access. Returns one SegmentFact per (geography column,
    period column) where the underlying value is numeric. Skips the consolidated
    'Revenue' aggregate before any geography header is seen, and the
    'Reportable segments [member]' aggregate that comes between the consolidated
    figure and the per-geography blocks.
    """
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
        out.extend(_walk_nu_section(section, periods, scale, source_doc_id))
    return out


def _walk_nu_section(
    section: list[object],
    periods: list[tuple[FiscalPeriodType | None, datetime | None]],
    scale: int,
    source_doc_id: int,
) -> list[SegmentFact]:
    facts: list[SegmentFact] = []
    current_geo: str | None = None
    for row in section[2:]:
        if not isinstance(row, dict) or len(row) != 1:
            continue
        label, values = next(iter(row.items()))
        if not isinstance(values, list):
            continue
        if _is_blank_row(values):
            if _is_xbrl_filler_label(label):
                continue
            current_geo = _normalize_geography(label) if _is_geography_header(label) else None
            continue
        if current_geo is None or not _is_revenue_label(label):
            continue
        for i, (period_type, period_end) in enumerate(periods):
            if period_type is None or period_end is None or i >= len(values):
                continue
            v = values[i]
            if not isinstance(v, (int, float)):
                continue
            facts.append(
                SegmentFact(
                    ticker=_TICKER,
                    period_end=period_end,
                    fiscal_period_type=period_type,
                    segment_name=current_geo,
                    metric="revenue_by_geography",
                    value=Decimal(str(v * scale)),
                    currency=Currency.USD,
                    unit=Unit.ACTUAL,
                    source_doc_id=source_doc_id,
                )
            )
    return facts


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


def extract_nu_segment_facts(
    conn: sqlite3.Connection, document_id: int, project_root: Path
) -> int:
    """Read NU 10-K/10-Q JSON, walk segment-info section, write segment_facts. Idempotent."""
    cur = conn.cursor()
    cur.execute("SELECT ticker, file_path, doc_type FROM documents WHERE id = ?", (document_id,))
    row = cur.fetchone()
    if row is None:
        raise ValueError(f"No document with id={document_id}")
    if row["ticker"] != _TICKER:
        raise ValueError(f"Document {document_id} is ticker={row['ticker']!r}, expected {_TICKER!r}")
    if row["doc_type"] not in _DOC_TYPES:
        raise ValueError(
            f"Document {document_id} is doc_type={row['doc_type']!r}, "
            f"not one of {sorted(_DOC_TYPES)}"
        )
    abs_path = project_root / row["file_path"]
    with open(abs_path, encoding="utf-8") as f:
        record = json.load(f)
    if not isinstance(record, dict):
        raise ValueError(f"Expected dict in {abs_path}, got {type(record).__name__}")

    facts = extract_nu_segment_facts_from_record(record, source_doc_id=document_id)
    inserted = _insert_segment_facts(conn, facts)
    conn.commit()
    return inserted
