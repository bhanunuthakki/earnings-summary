"""Extract 2-axis segment cross-tabs from FMP form_10k JSONs.

Where this fits:
  - FMP's product / geographic endpoints each give a flat 1-axis breakdown.
    "AWS by region" / "Google Cloud by region" / "META RoW splits" only
    appear in the 10-K narrative (segment information / disaggregation of
    revenue tables), and the data path to get those into our junction shape
    didn't exist before this module.
  - The junction schema (segment_periods + segment_dimensions, migration
    0055) supports cells like ``AWS revenue in US = $X`` via multiple
    segment_dimensions rows sharing a parent period row. This module
    populates that shape from filing text.

Per-ticker pipeline:
  1. Locate latest ``data/historical/fmp/{TICKER}_form_10k_{YEAR}.json``.
  2. Find sections whose key OR content head matches segment / geographic /
     disaggregation keywords (FMP truncates keys to ~31 chars, so the key
     alone is not enough); rank best-first and cap at the prompt budget.
  3. Single Haiku call with a strict-JSON contract returning a list of
     cross-tabs, each one a (subject_axis, subject_name) anchor with N cells
     keyed on (period_end, fiscal_period_type, secondary_name).
  4. Persist each cell via ``write_segment_facts_junction`` — one period row
     per (ticker, period_end, fiscal_period_type, source_doc_id) tuple, one
     dimension row per cell. ``metric`` encodes the subject (e.g.
     ``AWS_revenue``) so the renderer can derive ``parent_label`` and caption
     the expansion as "By Geography — under AWS".
  5. Cache to ``data/segment_crosstabs/{TICKER}.json`` keyed by source
     sha256 — re-running on the same filing is a no-op.

The cache file mirrors segment_definitions.py: ``extracted_at_start`` /
``extracted_at_end`` (ISO Z), elapsed_ms, the source sha256, and the parsed
cross-tab list verbatim so a downstream consumer can audit what the LLM
returned even after the DB rows have been compacted.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from pydantic import TypeAdapter

from llm.structured import call_llm_structured
from llm_client import FAST_CLASSIFIER_MODEL
from models.facts import (
    Currency,
    FiscalPeriodType,
    SegmentDimension,
    SegmentDimType,
    Unit,
)
from pipeline.segment_junction_writer import write_segment_facts_junction

# Two keyword tiers: priority keywords are the actual cross-tab signals;
# generic ones ("revenue", "sales") match half the filing and only fill
# whatever budget the priority sections leave over. Stems ("geograph",
# "disaggregat") so "geographical areas" / "disaggregated" also match.
_PRIORITY_KEYWORDS = (
    "segment",
    "geograph",
    "disaggregat",
)
_GENERIC_KEYWORDS = (
    "revenue",
    "sales",
)
_SECTION_KEYWORDS = _PRIORITY_KEYWORDS + _GENERIC_KEYWORDS
_MAX_TEXT_BUDGET = 60000
# FMP truncates top-level section keys to ~31 chars (deduped with _1/_2
# suffixes), but each section's first row repeats the full untruncated title —
# so keyword-match the key PLUS the section's first serialized line. Only the
# title row: scanning deeper content admits 40-88KB narrative sections that
# merely mention "segment" in passing (Income Taxes, PP&E, accounting
# policies) and they crowd out the real tables.
_TITLE_SCAN_CHARS = 500
# Per-section cap so one huge narrative section (e.g. a 69KB "Results for the
# year" prose dump) can't starve the actual segment/geography tables.
_MAX_SECTION_CHARS = 45000

# Deterministic backstop for the prompt's no-subtotal instruction: a "Total"
# cell persisted alongside its components double-counts every consumer that
# sums a dimension. Deliberately a local copy of segment_quarterly_6k's
# equivalent (simple shared logic stays duplicated in this repo).
_SUBTOTAL_NAME_PREFIXES = ("total", "subtotal", "consolidated")


def _is_subtotal_name(name: str) -> bool:
    return name.strip().lower().startswith(_SUBTOTAL_NAME_PREFIXES)


# Map LLM-returned axis strings to the SegmentDimType enum. The contract asks
# for the enum value verbatim ("product", "geography", "channel",
# "customer_segment", "business_unit"), but we also accept a few common
# synonyms so a slightly off LLM doesn't drop the whole cross-tab.
_AXIS_ALIASES: dict[str, SegmentDimType] = {
    "product": SegmentDimType.PRODUCT,
    "products": SegmentDimType.PRODUCT,
    "service": SegmentDimType.PRODUCT,
    "services": SegmentDimType.PRODUCT,
    "geography": SegmentDimType.GEOGRAPHY,
    "geographic": SegmentDimType.GEOGRAPHY,
    "geographies": SegmentDimType.GEOGRAPHY,
    "region": SegmentDimType.GEOGRAPHY,
    "regions": SegmentDimType.GEOGRAPHY,
    "country": SegmentDimType.GEOGRAPHY,
    "channel": SegmentDimType.CHANNEL,
    "channels": SegmentDimType.CHANNEL,
    "customer": SegmentDimType.CUSTOMER_SEGMENT,
    "customer_segment": SegmentDimType.CUSTOMER_SEGMENT,
    "customer_type": SegmentDimType.CUSTOMER_SEGMENT,
    "business_unit": SegmentDimType.BUSINESS_UNIT,
    "business_segment": SegmentDimType.BUSINESS_UNIT,
}


@dataclass
class CrosstabCell:
    period_end: str
    fiscal_period_type: str
    secondary_name: str
    value: float
    currency: str | None = None


@dataclass
class Crosstab:
    """One emitted breakdown — either a true 2-axis cross-tab or a 1-axis
    finer-grained breakdown.

    True 2-axis: ``subject_axis="product"``, ``subject_name="AWS"``,
    ``secondary_axis="geography"`` → ``metric="AWS_revenue"`` on dim rows so
    the renderer captions "By Geography — under AWS".

    1-axis fallback (when the filing presents geography at finer granularity
    than FMP's flat segments table, but doesn't cross-tab against products):
    ``subject_axis=None``, ``subject_name=None``, ``secondary_axis="geography"``
    → ``metric="revenue"`` on dim rows → renderer surfaces "By Geography"
    as a standalone expansion (no parent_label).
    """

    subject_axis: str | None
    subject_name: str | None
    secondary_axis: str
    cells: list[CrosstabCell] = field(default_factory=list)


@dataclass
class SegmentCrosstabsResult:
    ticker: str
    fiscal_year: int | None
    source_path: str | None
    source_sha256: str | None
    source_doc_id: int | None = None
    extracted_at_start: str | None = None
    extracted_at_end: str | None = None
    elapsed_ms: int = 0
    model: str = FAST_CLASSIFIER_MODEL
    cross_tabs: list[dict[str, Any]] = field(default_factory=list)
    periods_inserted: int = 0
    dimensions_inserted: int = 0
    cells_skipped: int = 0
    skipped_reason: str | None = None


def extract_for_ticker(
    ticker: str,
    repo_root: Path,
    db_conn: sqlite3.Connection,
    fiscal_year: int | None = None,
    refresh: bool = False,
) -> SegmentCrosstabsResult:
    """End-to-end extract → junction-write → cache.

    Idempotent on (ticker, source_sha256) unless ``refresh=True``. The DB
    side is independently idempotent via the writer's natural-key dedup, so
    cache + DB stay in sync even if the cache is deleted by hand.
    """
    ticker = ticker.upper()
    cache_path = _cache_path(repo_root, ticker)

    source_path, year = _locate_form_10k(repo_root, ticker, fiscal_year)
    if source_path is None:
        return SegmentCrosstabsResult(
            ticker=ticker,
            fiscal_year=None,
            source_path=None,
            source_sha256=None,
            skipped_reason=f"no _form_10k_*.json under data/historical/fmp/ for {ticker}",
        )

    raw_bytes = source_path.read_bytes()
    sha256 = hashlib.sha256(raw_bytes).hexdigest()

    if not refresh and cache_path.exists():
        cached_raw = json.loads(cache_path.read_text(encoding="utf-8"))
        if cached_raw.get("source_sha256") == sha256 and cached_raw.get("fiscal_year") == year:
            return _result_from_cache(cached_raw)

    doc_id = _lookup_doc_id(
        db_conn, ticker=ticker, fiscal_year=year, source_path=source_path, repo_root=repo_root
    )
    if doc_id is None:
        result = SegmentCrosstabsResult(
            ticker=ticker,
            fiscal_year=year,
            source_path=str(source_path),
            source_sha256=sha256,
            skipped_reason=(
                "no fmp_10k_json document row for this ticker/year — "
                "run the FMP doc index before extracting cross-tabs"
            ),
        )
        _write_cache(cache_path, result)
        return result

    payload = json.loads(raw_bytes.decode("utf-8"))
    relevant_text = _extract_relevant_text(payload)
    if not relevant_text.strip():
        result = SegmentCrosstabsResult(
            ticker=ticker,
            fiscal_year=year,
            source_path=str(source_path),
            source_sha256=sha256,
            source_doc_id=doc_id,
            skipped_reason="form_10k has no section matching segment/geographic/disaggregation keywords",
        )
        _write_cache(cache_path, result)
        return result

    start_dt = datetime.now(UTC)
    t0 = time.perf_counter()
    cross_tabs = _ask_claude(ticker, year, relevant_text)
    elapsed_ms = int((time.perf_counter() - t0) * 1000)
    end_dt = datetime.now(UTC)

    periods_inserted, dimensions_inserted, cells_skipped = _persist_cross_tabs(
        db_conn, ticker=ticker, source_doc_id=doc_id, cross_tabs=cross_tabs
    )
    db_conn.commit()

    result = SegmentCrosstabsResult(
        ticker=ticker,
        fiscal_year=year,
        source_path=str(source_path),
        source_sha256=sha256,
        source_doc_id=doc_id,
        extracted_at_start=start_dt.isoformat(timespec="seconds").replace("+00:00", "Z"),
        extracted_at_end=end_dt.isoformat(timespec="seconds").replace("+00:00", "Z"),
        elapsed_ms=elapsed_ms,
        model=FAST_CLASSIFIER_MODEL,
        cross_tabs=[_crosstab_to_dict(c) for c in cross_tabs],
        periods_inserted=periods_inserted,
        dimensions_inserted=dimensions_inserted,
        cells_skipped=cells_skipped,
    )
    _write_cache(cache_path, result)
    return result


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def _persist_cross_tabs(
    conn: sqlite3.Connection,
    *,
    ticker: str,
    source_doc_id: int,
    cross_tabs: list[Crosstab],
) -> tuple[int, int, int]:
    """Group cells by (period_end, fiscal_period_type, currency) and write each
    group as one period + N dim rows via the junction writer.

    Returns (periods_inserted, dimensions_inserted, cells_skipped). Cells with
    an unparseable axis / period / value get skipped and counted — the rest
    still land.
    """
    periods_inserted = 0
    dimensions_inserted = 0
    cells_skipped = 0

    # Pre-group across cross-tabs so one period row covers every dimension
    # cell that landed at the same (period_end, fiscal_period_type). Without
    # this, two cross-tabs touching the same quarter would both attempt to
    # insert the period — the writer dedupes, but pre-grouping keeps the
    # work explicit and the row counts predictable.
    grouped: dict[
        tuple[datetime, FiscalPeriodType, Currency | None],
        list[SegmentDimension],
    ] = {}

    for crosstab in cross_tabs:
        secondary_axis = _axis_to_dim_type(crosstab.secondary_axis)
        if secondary_axis is None:
            cells_skipped += len(crosstab.cells)
            continue
        # 1-axis fallback: when the LLM emits a finer-grained breakdown
        # without a true subject anchor, write metric='revenue' so the
        # renderer surfaces it as a standalone "By <axis>" expansion (no
        # parent_label). Otherwise, prefix the metric with the subject token
        # so the renderer can derive parent_label.
        if crosstab.subject_name and crosstab.subject_name.strip():
            subject_token = _subject_to_metric_token(crosstab.subject_name)
            if not subject_token:
                cells_skipped += len(crosstab.cells)
                continue
            metric = f"{subject_token}_revenue"
        else:
            metric = "revenue"

        for cell in crosstab.cells:
            period_end = _parse_period_end(cell.period_end)
            if period_end is None:
                cells_skipped += 1
                continue
            period_type = _parse_period_type(cell.fiscal_period_type)
            if period_type is None:
                cells_skipped += 1
                continue
            value = _parse_decimal(cell.value)
            if value is None:
                cells_skipped += 1
                continue
            secondary_name = cell.secondary_name.strip()
            if not secondary_name:
                cells_skipped += 1
                continue
            if _is_subtotal_name(secondary_name):
                cells_skipped += 1
                continue
            currency = _parse_currency(cell.currency)
            grouped.setdefault((period_end, period_type, currency), []).append(
                SegmentDimension(
                    dim_type=secondary_axis,
                    dim_name=secondary_name,
                    value=value,
                    metric=metric,
                )
            )

    for (period_end, period_type, currency), dims in grouped.items():
        p_ins, d_ins = write_segment_facts_junction(
            conn,
            ticker=ticker,
            period_end=period_end,
            fiscal_period_type=period_type,
            source_doc_id=source_doc_id,
            currency=currency,
            unit=Unit.ACTUAL,
            dimensions=dims,
        )
        periods_inserted += p_ins
        dimensions_inserted += d_ins
    return (periods_inserted, dimensions_inserted, cells_skipped)


# ---------------------------------------------------------------------------
# Source location, text extraction
# ---------------------------------------------------------------------------


def _cache_path(repo_root: Path, ticker: str) -> Path:
    out_dir = repo_root / "data" / "segment_crosstabs"
    out_dir.mkdir(parents=True, exist_ok=True)
    return out_dir / f"{ticker}.json"


def _locate_form_10k(
    repo_root: Path, ticker: str, fiscal_year: int | None
) -> tuple[Path | None, int | None]:
    fmp_dir = repo_root / "data" / "historical" / "fmp"
    if not fmp_dir.exists():
        return (None, None)
    candidates: list[tuple[int, Path]] = []
    pattern = re.compile(rf"^{re.escape(ticker)}_form_10k_(\d{{4}})\.json$")
    for p in fmp_dir.iterdir():
        m = pattern.match(p.name)
        if not m:
            continue
        candidates.append((int(m.group(1)), p))
    if not candidates:
        return (None, None)
    if fiscal_year is not None:
        for y, p in candidates:
            if y == fiscal_year:
                return (p, y)
        return (None, None)
    candidates.sort()
    y, p = candidates[-1]
    return (p, y)


def _lookup_doc_id(
    conn: sqlite3.Connection,
    *,
    ticker: str,
    fiscal_year: int | None,
    source_path: Path | None = None,
    repo_root: Path | None = None,
) -> int | None:
    """Find the documents.id for the fmp_10k_json behind this extraction.

    Match strategy, in order:
      1. ``file_path`` exact match on the repo-relative form of source_path.
         This is the natural key — the FMP doc indexer writes file_path as
         ``data/historical/fmp/{TICKER}_form_10k_{YEAR}.json`` so the
         relative form of our located file should match a row 1:1.
      2. ``file_path LIKE '%form_10k_{YEAR}.json'`` filename suffix match,
         in case repo-root resolution differs (e.g. running with a custom
         --repo-root that doesn't share the indexer's base path).
      3. ``period_end`` year match — same logic as
         document_table_extractor._lookup_doc_id. Works for systems where
         period_end is populated; in this repo's production DB the 10-K
         documents rows carry NULL period_end, so this path is the last
         resort.

    Returns None when the documents table is missing, all strategies miss,
    or the schema is unexpectedly shaped.
    """
    try:
        present = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='documents'"
        ).fetchone()
    except sqlite3.Error:
        return None
    if present is None:
        return None

    if source_path is not None:
        rel = _relative_file_path(source_path, repo_root)
        if rel is not None:
            try:
                row = conn.execute(
                    "SELECT id FROM documents "
                    "WHERE ticker = ? AND doc_type = 'fmp_10k_json' AND file_path = ? "
                    "LIMIT 1",
                    (ticker, rel),
                ).fetchone()
            except sqlite3.Error:
                row = None
            if row is not None:
                return int(row[0])
        # Filename suffix fallback — matches across repo-root variations.
        try:
            row = conn.execute(
                "SELECT id FROM documents "
                "WHERE ticker = ? AND doc_type = 'fmp_10k_json' AND file_path LIKE ? "
                "ORDER BY id DESC LIMIT 1",
                (ticker, f"%{source_path.name}"),
            ).fetchone()
        except sqlite3.Error:
            row = None
        if row is not None:
            return int(row[0])

    if fiscal_year is None:
        return None
    try:
        row = conn.execute(
            """
            SELECT id FROM documents
            WHERE ticker = ? AND doc_type = 'fmp_10k_json'
              AND period_end IS NOT NULL
              AND (
                strftime('%Y', period_end) = ? OR
                substr(period_end, 1, 4) = ?
              )
            ORDER BY period_end DESC
            LIMIT 1
            """,
            (ticker, str(fiscal_year), str(fiscal_year)),
        ).fetchone()
    except sqlite3.Error:
        return None
    if row is None:
        return None
    return int(row[0])


def _relative_file_path(source_path: Path, repo_root: Path | None) -> str | None:
    """Return source_path expressed relative to repo_root (forward-slash form).

    The FMP doc indexer stores file_path as ``data/historical/fmp/...`` with
    forward slashes; on Windows ``Path.relative_to`` yields backslashes, so
    we normalize. Returns None when source_path is not under repo_root.
    """
    if repo_root is None:
        return None
    try:
        rel = source_path.resolve().relative_to(repo_root.resolve())
    except ValueError:
        return None
    return rel.as_posix()


def _extract_relevant_text(payload: dict[str, object]) -> str:
    """Concatenate JSON-serialized rows from every keyword-matching section,
    best sections first.

    FMP's 10-K JSON is structured tabular data: each section is a list of
    small dicts like ``{"Net sales": [716924, 637959, 574785]}``. The labels
    are short and the values are numeric lists — neither survives a naive
    long-string-leaf filter (that's the pattern used by segment_definitions
    for narrative prose, which is the wrong tool here).

    So we serialize each section as one JSON line per child item, preserving
    the table's label/value structure for the LLM to read.

    Selection is title-aware: FMP truncates top-level keys to ~31 chars
    (``"Results for the year - Segmen_2"``), so keywords are matched against
    the key PLUS the section's first serialized row — which carries the full
    untruncated title. Matching sections are ranked by how many distinct
    priority keywords they hit (a segment-AND-geography table outranks a
    segment-only one, which outranks generic revenue/sales sections), ties
    in document order, and the budget is filled in rank order with a
    per-section cap so one huge matching section can't crowd out the
    actual tables.
    """
    # (rank, doc_order, key, section_text) — rank is -#priority-hits for
    # priority sections, +1 for generic-only ones, so sort() puts richer
    # sections first and stable doc order breaks ties.
    candidates: list[tuple[int, int, str, str]] = []
    for doc_order, (key, value) in enumerate(payload.items()):
        section_lines = _serialize_section(value)
        section_text = "\n".join(section_lines).strip()
        if not section_text:
            continue
        title_row = section_lines[0][:_TITLE_SCAN_CHARS]
        scan = f"{key}\n{title_row}".lower()
        priority_hits = sum(1 for kw in _PRIORITY_KEYWORDS if kw in scan)
        if priority_hits:
            rank = -priority_hits
        elif any(kw in scan for kw in _GENERIC_KEYWORDS):
            rank = 1
        else:
            continue
        candidates.append((rank, doc_order, str(key), section_text))
    candidates.sort()

    parts: list[str] = []
    total_chars = 0
    for _rank, _doc_order, key, section_text in candidates:
        if total_chars >= _MAX_TEXT_BUDGET:
            break
        allowed = min(_MAX_SECTION_CHARS, _MAX_TEXT_BUDGET - total_chars)
        section_text = section_text[:allowed]
        parts.append(f"=== {key} ===\n{section_text}")
        total_chars += len(section_text)
    return "\n\n".join(parts)


def _serialize_section(node: object) -> list[str]:
    """Render a section's payload as one line per child item.

    Lists become one JSON line per element; dicts become one JSON line for
    the whole dict (a typical FMP section is a list-of-dicts, each dict
    being one labeled row of a table).
    """
    if isinstance(node, list):
        out: list[str] = []
        for item in node:
            line = _dump_compact(item)
            if line:
                out.append(line)
        return out
    line = _dump_compact(node)
    return [line] if line else []


def _dump_compact(item: object) -> str:
    """JSON-serialize a single table row, normalising NBSPs in any string."""
    if isinstance(item, str):
        return item.strip().replace("\xa0", " ")
    try:
        return json.dumps(item, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        return ""


# ---------------------------------------------------------------------------
# LLM call
# ---------------------------------------------------------------------------


def _ask_claude(ticker: str, year: int | None, text: str) -> list[Crosstab]:
    """Call Haiku with the cross-tab JSON contract; parse + validate."""
    prompt = _build_prompt(ticker, year, text)
    parsed = call_llm_structured(
        prompt,
        purpose="segment_crosstab_extract",
        expect="object",
        required_keys=("cross_tabs",),
        schema=TypeAdapter(dict[str, object]),
    )
    items = parsed.get("cross_tabs")
    if not isinstance(items, list):
        return []
    out: list[Crosstab] = []
    for entry in items:
        if not isinstance(entry, dict):
            continue
        subject_axis = entry.get("subject_axis")
        subject_name = entry.get("subject_name")
        secondary_axis = entry.get("secondary_axis")
        cells_raw = entry.get("cells")
        # secondary_axis + cells are required; subject_axis/subject_name are
        # optional (null/None signals a 1-axis finer-grained breakdown).
        if not (isinstance(secondary_axis, str) and isinstance(cells_raw, list)):
            continue
        subject_axis_str: str | None = subject_axis if isinstance(subject_axis, str) else None
        subject_name_str: str | None = subject_name if isinstance(subject_name, str) else None
        cells: list[CrosstabCell] = []
        for cell in cells_raw:
            if not isinstance(cell, dict):
                continue
            period_end = cell.get("period_end")
            fiscal_period_type = cell.get("fiscal_period_type")
            secondary_name = cell.get("secondary_name")
            value = cell.get("value")
            currency = cell.get("currency")
            if not (
                isinstance(period_end, str)
                and isinstance(fiscal_period_type, str)
                and isinstance(secondary_name, str)
                and isinstance(value, (int, float))
            ):
                continue
            cells.append(
                CrosstabCell(
                    period_end=period_end,
                    fiscal_period_type=fiscal_period_type,
                    secondary_name=secondary_name,
                    value=float(value),
                    currency=currency if isinstance(currency, str) else None,
                )
            )
        if not cells:
            continue
        out.append(
            Crosstab(
                subject_axis=subject_axis_str,
                subject_name=subject_name_str,
                secondary_axis=secondary_axis,
                cells=cells,
            )
        )
    return out


def _build_prompt(ticker: str, year: int | None, text: str) -> str:
    return f"""You are extracting revenue breakdowns from a 10-K filing for {ticker} (fiscal year {year}).

We accept TWO shapes of output, both encoded in the same JSON:

(A) TRUE 2-AXIS CROSS-TAB — when the filing explicitly presents one revenue figure
    that splits along two axes at the same time (e.g. "AWS revenue in United States
    = $X" presented as a single table where both axes appear together).
    Encode with: ``subject_axis``, ``subject_name`` set; ``secondary_axis`` is the
    breakdown axis; each cell carries the value for one ``secondary_name``.
    Use SHORT canonical subject names — "AWS" not "Amazon Web Services",
    "Google Cloud" not "Alphabet Google Cloud Platform".

(B) FINER-GRAINED 1-AXIS BREAKDOWN — when the filing presents a single-axis revenue
    breakdown at finer granularity than a standard segment table (e.g. consolidated
    revenue by country, beyond just North America / International). Use when there's
    no explicit cross-tab but the filing exposes useful sub-segment detail.
    Encode with: ``subject_axis = null``, ``subject_name = null``; ``secondary_axis``
    is the breakdown axis (typically "geography"); cells carry one entry per label.

DO NOT manufacture a cross-tab by inferring product-in-region from separate
product-only and region-only tables. The filing must present each emitted breakdown
explicitly. If unsure, prefer (B) over (A).

DO NOT emit "Total"/subtotal rows or columns as cells (e.g. "Total", "Total net
sales", a regional rollup column that just sums other listed columns) -- only the
individual sub-group entries; a subtotal alongside its components double-counts.

Axis values must be one of: "geography", "channel", "customer_segment", "product",
"business_unit".

Period dates must be the period END date in ISO YYYY-MM-DD form. Fiscal period type
must be one of: "Q1", "Q2", "Q3", "Q4", "FY". Values are the raw reported number in
the filing's reporting currency (do NOT scale; if the filing says "$15,234" with a
"(in millions)" header, return 15234000000, not 15234).

Return STRICT JSON in this exact shape — no markdown fence, no commentary:

{{
  "cross_tabs": [
    {{
      "subject_axis": "product",
      "subject_name": "AWS",
      "secondary_axis": "geography",
      "cells": [
        {{
          "period_end": "2024-12-31",
          "fiscal_period_type": "FY",
          "secondary_name": "United States",
          "value": 90000000000,
          "currency": "USD"
        }}
      ]
    }},
    {{
      "subject_axis": null,
      "subject_name": null,
      "secondary_axis": "geography",
      "cells": [
        {{
          "period_end": "2024-12-31",
          "fiscal_period_type": "FY",
          "secondary_name": "Germany",
          "value": 38000000000,
          "currency": "USD"
        }}
      ]
    }}
  ]
}}

If the filing has neither true cross-tabs nor finer-grained breakdowns beyond what a
standard segment table would carry, return {{"cross_tabs": []}}.

10-K text:
\"\"\"
{text}
\"\"\""""


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------


def _axis_to_dim_type(axis: str) -> SegmentDimType | None:
    key = axis.strip().lower().replace(" ", "_")
    return _AXIS_ALIASES.get(key)


_SUBJECT_TOKEN_RE = re.compile(r"[^A-Za-z0-9_]+")


def _subject_to_metric_token(subject_name: str) -> str:
    """Canonicalize a subject name into the metric prefix.

    "AWS" → "AWS"; "Google Cloud" → "Google_Cloud"; "Reality Labs" →
    "Reality_Labs". The renderer parses back with ``rsplit('_revenue', 1)[0]``
    then ``.replace('_', ' ')`` so the parent_label round-trips to the human
    form.
    """
    collapsed = _SUBJECT_TOKEN_RE.sub("_", subject_name.strip())
    return collapsed.strip("_")


def _parse_period_end(raw: str) -> datetime | None:
    s = raw.strip()
    for fmt in ("%Y-%m-%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def _parse_period_type(raw: str) -> FiscalPeriodType | None:
    try:
        return FiscalPeriodType(raw.strip().upper())
    except ValueError:
        return None


def _parse_currency(raw: str | None) -> Currency | None:
    if raw is None:
        return None
    try:
        return Currency(raw.strip().upper())
    except ValueError:
        return None


def _parse_decimal(value: float | int) -> Decimal | None:
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


# ---------------------------------------------------------------------------
# Cache I/O
# ---------------------------------------------------------------------------


def _crosstab_to_dict(c: Crosstab) -> dict[str, Any]:
    return {
        "subject_axis": c.subject_axis,
        "subject_name": c.subject_name,
        "secondary_axis": c.secondary_axis,
        "cells": [asdict(cell) for cell in c.cells],
    }


def _result_from_cache(cached: dict[str, Any]) -> SegmentCrosstabsResult:
    fields = {f for f in SegmentCrosstabsResult.__dataclass_fields__}
    kwargs: dict[str, Any] = {k: v for k, v in cached.items() if k in fields}
    return SegmentCrosstabsResult(**kwargs)


def _write_cache(path: Path, result: SegmentCrosstabsResult) -> None:
    path.write_text(json.dumps(asdict(result), indent=2), encoding="utf-8")
