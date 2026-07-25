# pyright: reportPrivateUsage=false
# This module reuses four private parsing helpers from segment_crosstabs_llm.py
# by cross-module import (_axis_to_dim_type, _parse_period_end,
# _parse_currency, _parse_decimal) -- the same established convention
# segment_quarterly_10q.py documents for its own reuse of
# generic_xbrl_capture._is_detail_section. segment_crosstabs_llm.py itself is
# not edited here.
"""FPI quarterly segment/product-revenue extractor via 6-K narrative text
(docs/design/segment_quarterly_framework.md §1.1, Phase 3).

## The Phase-3 spike verdict this module encodes

Before any of this was written, the design doc's own §7 risk #1 required an
empirical check: does FMP's ``financial-reports-json`` endpoint return
anything for ``period=Q1..Q3`` on a 20-F filer? Verified NO (2026-07-18) via
two independent signals for NU/ASML/NVO: (a) ``{T}_financial_reports_dates.json``
-- FMP's OWN catalog of which (fiscalYear, period) combos have a document --
lists ONLY ``FY``/``Q4`` entries for all three names across their entire
fetched history (ASML back to 2011); (b) every row in
``{T}_as_reported_financial_quarterly.json`` for these three tickers carries
``data.documenttype == '20-F'`` / ``documentfiscalperiodfocus == 'FY'`` --
i.e. even the "quarterly" cache is the same annual 20-F re-tagged, never a
real interim filing. This is a structural fact (20-F filers don't file a
10-Q-equivalent structured document SEC/FMP would index), not a subscription
quota artifact -- confirmed by the catalog gap existing across 15 fiscal
years, long before any current-session rate limiting. So: the ``fpi_6k``
route (6-K narrative, NOT financial-reports-json) is the only viable primary
source, exactly as the design doc's default-assumption framed it.

The follow-up question -- do 6-Ks actually CARRY a quarterly segment note at
all, and in what shape -- was checked per-ticker against real EDGAR filings
(read-only, 2026-07-18):

- **NU**: YES. The 1Q26 "Financial Statements" 6-K exhibit
  (accession 0001292814-26-003053, ``nufs1q26_6k.htm``) has a plain-HTML-text
  "34. SEGMENT INFORMATION" note with a geography table (revenue +
  non-current assets; Brazil/Mexico/Other countries) at QUARTERLY cadence --
  same shape ``directives/per_ticker_segment_extraction_notes.md`` already
  documents for NU's ANNUAL 20-F (single reportable segment, geography-only,
  no segment OI). Extractable via LLM narrative extraction.
- **NVO**: YES, richer than the annual 20-F. The 1Q26 "Company announcement"
  6-K exhibit (accession 0000353278-26-000018, ``caq12026.htm``) has
  plain-HTML-text "Adjusted sales split per therapy" (Wegovy/Ozempic/Rybelsus/
  insulins/rare disease, etc.) AND "Adjusted sales per geographical area"
  tables at quarterly cadence. Extractable via LLM narrative extraction.
- **ASML**: NO. The 1Q26 "Financial statements US GAAP" 6-K exhibit
  (accession 0001628280-26-025147, ``financialstatementsusgaa.htm``) is a
  12-slide image-scanned deck (``<img src="...00N.jpg" title="slideN">``,
  no OCR-able text) whose "Notes" slide is a single sentence deferring ALL
  segment/policy detail to the annual 20-F ("these unaudited Summary
  Consolidated Financial Statements should be read in conjunction with the
  Consolidated Financial Statements and Notes included within our 2025
  Annual Report"). Confirmed NO quarterly segment/system-type disclosure
  exists in any exhibit of this filing (also checked
  ``pressreleasefinancialresul.htm`` and ``presentationinvestorrela.htm`` --
  same image-only shape, no "New/Used systems" breakdown anywhere).

So Stage 2 builds a REAL route for NU/NVO and marks ASML honestly
(``fpi_annual_only``) rather than forcing a build the evidence refutes. Any
OTHER 20-F/40-F ticker (BHP, RIO, VALE, HDB, BN, CNQ, FNV) is untested by
this spike -- left at the existing ``fpi_route_unproven`` default
(``execution/audit_segment_quarterly_coverage.py``'s pre-existing fallback),
not silently assumed to work OR assumed broken.

**WIX added 2026-07-25** (Disclosure Intelligence v1 PRD, D1.2), same
protocol: fetched WIX's live 1Q26 6-K (accession 0001628280-26-034370, filed
2026-05-13), inspected ``index.json``, confirmed the earnings-release exhibit
(``firstquarter2026results.htm``) is real narrative/tabular text (32.8KB
stripped from 630KB raw, density 0.052 -- well clear of
``sec_6k_fetch._MIN_TEXT_DENSITY``) carrying a genuine quarterly product-line
revenue/bookings breakdown (Creative Subscriptions / Business Solutions /
Transaction / Partners), not an image-scanned deck. Marked "supported" below;
the exhibit-filename hint lives in ``pipeline.sec_6k_fetch._TICKER_EXHIBIT_HINT``.

## Shape difference from the annual crosstab extractor

``compute.segment_crosstabs_llm``'s JSON contract always writes
``metric='revenue'`` (1-axis) or ``metric='{subject}_revenue'`` (2-axis).
NU's 6-K table reports TWO metrics per geography cell (revenue AND
non-current assets) -- a `metric` field the annual contract doesn't carry.
Rather than bolt an incompatible field onto that already-shipped dataclass,
this module defines its own minimal ``SixKBreakdown``/``SixKCell`` shape with
an explicit ``metric`` string, and imports only the annual extractor's pure
parsing helpers (axis/period/currency/decimal) -- not its dataclasses or
persistence function, which are 1-axis/2-axis-shaped and don't generalize
cleanly to an explicit-metric field without changing that module's own
established contract.
"""

from __future__ import annotations

import json
import re
import sqlite3
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, cast

from compute.segment_crosstabs_llm import (
    _axis_to_dim_type,
    _parse_currency,
    _parse_decimal,
    _parse_period_end,
)
from llm_client import FAST_CLASSIFIER_MODEL, JSON_FENCE_RE, _call_claude
from models.facts import FiscalPeriodType, SegmentDimension, Unit
from pipeline.sec_6k_fetch import (
    fetch_6k_exhibit_text,
    locate_6k_exhibit,
    register_6k_document,
)
from pipeline.segment_junction_writer import write_segment_facts_junction
from pipeline.segment_quarterly_coverage import record_coverage
from table_extractors.period_axis import NominalQuarter, expected_period_ends

EXTRACTOR_ID = "segment_quarterly_6k_v1"

# Per-ticker classification from the Phase-3 spike (module docstring). Only
# "supported" tickers ever trigger a network fetch -- everything else either
# gets the honest confirmed-negative reason code (ASML) or is left for the
# audit script's existing generic fpi_route_unproven fallback (untested
# names), never a blind guess.
TickerStatus = Literal["supported", "confirmed_annual_only"]

_TICKER_6K_STATUS: dict[str, TickerStatus] = {
    "NU": "supported",
    "NVO": "supported",
    "WIX": "supported",
    "ASML": "confirmed_annual_only",
}

_SECTION_KEYWORDS = (
    "segment",
    "geographical area",
    "geographic area",
    "therapeutic area",
    "adjusted sales",
    "revenue",
    "net sales",
)
_MAX_TEXT_BUDGET = 60000

# Deterministic backstop for the prompt's no-subtotal instruction: a "Total"
# row persisted alongside its components double-counts every consumer that
# sums a dimension (observed in prod: NU 3Q24's "Total" geography rows,
# 2026-07 audit). Label-prefix match only -- a genuine segment named
# "Total..." would be a subtotal by construction in these filings.
_SUBTOTAL_NAME_PREFIXES = ("total", "subtotal", "consolidated")


def _is_subtotal_name(name: str) -> bool:
    return name.strip().lower().startswith(_SUBTOTAL_NAME_PREFIXES)


@dataclass
class SixKCell:
    name: str
    period_end: str
    fiscal_period_type: str
    value: float
    currency: str | None = None


@dataclass
class SixKBreakdown:
    axis: str
    metric: str
    cells: list[SixKCell] = field(default_factory=list[SixKCell])


@dataclass
class SegmentQuarterly6KResult:
    ticker: str
    year: int
    quarter: str
    source_doc_id: int | None = None
    source_accession: str | None = None
    extracted_at_start: str | None = None
    extracted_at_end: str | None = None
    elapsed_ms: int = 0
    model: str = FAST_CLASSIFIER_MODEL
    breakdowns: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    periods_inserted: int = 0
    dimensions_inserted: int = 0
    cells_skipped: int = 0
    skipped_reason: str | None = None


def extract_for_ticker(
    ticker: str,
    year: int,
    quarter: NominalQuarter,
    repo_root: Path,
    conn: sqlite3.Connection,
    *,
    fye_month: int = 12,
    fye_day: int = 31,
) -> SegmentQuarterly6KResult:
    """End-to-end: classify -> locate 6-K exhibit -> fetch -> LLM extract ->
    persist. Every non-"supported" or fetch-failure path writes an honest
    ``segment_quarterly_coverage`` row rather than staying silent, per the
    design doc's "never a silent skip" quality bar."""
    ticker = ticker.upper()
    current_end, _prior = expected_period_ends(quarter, year, fye_month, fye_day)
    period_end_dt = datetime(current_end.year, current_end.month, current_end.day)

    status = _TICKER_6K_STATUS.get(ticker)
    if status == "confirmed_annual_only":
        record_coverage(
            conn,
            ticker=ticker,
            period_end=period_end_dt,
            fiscal_period_type=quarter,
            dim_type=None,
            dim_name=None,
            status="not_disclosed",
            reason_code="fpi_annual_only",
            source_doc_id=None,
            method_version=EXTRACTOR_ID,
        )
        conn.commit()
        return SegmentQuarterly6KResult(
            ticker=ticker,
            year=year,
            quarter=quarter,
            skipped_reason=(
                "fpi_annual_only: Phase-3 spike (2026-07-18) confirmed no quarterly "
                "segment/product disclosure exists in this ticker's 6-K exhibits "
                "(image-only financial statements exhibit with a Notes section "
                "deferring all segment detail to the annual 20-F)"
            ),
        )
    if status != "supported":
        # Untested by this spike -- leave to audit_segment_quarterly_coverage.py's
        # existing fpi_route_unproven default rather than writing a coverage row
        # ourselves (we have no evidence either way for this ticker).
        return SegmentQuarterly6KResult(
            ticker=ticker,
            year=year,
            quarter=quarter,
            skipped_reason=(
                f"{ticker} is not in the Phase-3 spike-validated ticker set "
                "(NU, NVO, WIX) -- fpi_6k route unproven for this name, see "
                "compute.segment_quarterly_6k._TICKER_6K_STATUS"
            ),
        )

    located = locate_6k_exhibit(
        ticker, quarter=quarter, year=year, fye_month=fye_month, fye_day=fye_day
    )
    if located is None:
        record_coverage(
            conn,
            ticker=ticker,
            period_end=period_end_dt,
            fiscal_period_type=quarter,
            dim_type=None,
            dim_name=None,
            status="not_computable",
            reason_code="fpi_6k_exhibit_not_located",
            source_doc_id=None,
            method_version=EXTRACTOR_ID,
        )
        conn.commit()
        return SegmentQuarterly6KResult(
            ticker=ticker,
            year=year,
            quarter=quarter,
            skipped_reason="no matching 6-K exhibit found in the expected filing window",
        )

    fetched = fetch_6k_exhibit_text(located)
    if fetched is None:
        record_coverage(
            conn,
            ticker=ticker,
            period_end=period_end_dt,
            fiscal_period_type=quarter,
            dim_type=None,
            dim_name=None,
            status="not_computable",
            reason_code="fpi_6k_fetch_failed",
            source_doc_id=None,
            method_version=EXTRACTOR_ID,
        )
        conn.commit()
        return SegmentQuarterly6KResult(
            ticker=ticker,
            year=year,
            quarter=quarter,
            source_accession=located.accession,
            skipped_reason=f"HTTP fetch failed for {located.exhibit_url}",
        )
    if fetched.is_image_only:
        record_coverage(
            conn,
            ticker=ticker,
            period_end=period_end_dt,
            fiscal_period_type=quarter,
            dim_type=None,
            dim_name=None,
            status="not_computable",
            reason_code="fpi_6k_image_only_exhibit",
            source_doc_id=None,
            method_version=EXTRACTOR_ID,
        )
        conn.commit()
        return SegmentQuarterly6KResult(
            ticker=ticker,
            year=year,
            quarter=quarter,
            source_accession=located.accession,
            skipped_reason=(
                f"{located.exhibit_filename} is image-scanned (no extractable text) -- "
                "would need vision/OCR, out of scope for this text-only extractor"
            ),
        )

    doc_id = register_6k_document(
        conn, ticker=ticker, fetched=fetched, repo_root=repo_root, period_end=period_end_dt
    )

    relevant_text = _extract_relevant_text(fetched.plain_text)
    if not relevant_text.strip():
        record_coverage(
            conn,
            ticker=ticker,
            period_end=period_end_dt,
            fiscal_period_type=quarter,
            dim_type=None,
            dim_name=None,
            status="not_disclosed",
            reason_code="fpi_6k_no_segment_section",
            source_doc_id=doc_id,
            method_version=EXTRACTOR_ID,
        )
        conn.commit()
        return SegmentQuarterly6KResult(
            ticker=ticker,
            year=year,
            quarter=quarter,
            source_doc_id=doc_id,
            source_accession=located.accession,
            skipped_reason="no segment/geographic/product keyword section found in exhibit text",
        )

    start_dt = datetime.now(UTC)
    t0 = time.perf_counter()
    breakdowns = _ask_claude(ticker, year, quarter, relevant_text)
    elapsed_ms = int((time.perf_counter() - t0) * 1000)
    end_dt = datetime.now(UTC)

    periods_inserted, dimensions_inserted, cells_skipped = _persist_breakdowns(
        conn, ticker=ticker, source_doc_id=doc_id, breakdowns=breakdowns
    )
    if dimensions_inserted == 0:
        record_coverage(
            conn,
            ticker=ticker,
            period_end=period_end_dt,
            fiscal_period_type=quarter,
            dim_type=None,
            dim_name=None,
            status="not_disclosed",
            reason_code="fpi_6k_llm_found_no_breakdown",
            source_doc_id=doc_id,
            method_version=EXTRACTOR_ID,
        )
    conn.commit()

    return SegmentQuarterly6KResult(
        ticker=ticker,
        year=year,
        quarter=quarter,
        source_doc_id=doc_id,
        source_accession=located.accession,
        extracted_at_start=start_dt.isoformat(timespec="seconds").replace("+00:00", "Z"),
        extracted_at_end=end_dt.isoformat(timespec="seconds").replace("+00:00", "Z"),
        elapsed_ms=elapsed_ms,
        model=FAST_CLASSIFIER_MODEL,
        breakdowns=[_breakdown_to_dict(b) for b in breakdowns],
        periods_inserted=periods_inserted,
        dimensions_inserted=dimensions_inserted,
        cells_skipped=cells_skipped,
    )


# ---------------------------------------------------------------------------
# Text prep
# ---------------------------------------------------------------------------


def _extract_relevant_text(plain_text: str) -> str:
    """Whole-document keyword windowing: the 6-K exhibit is already flat text
    (unlike the annual extractor's JSON-section input), so instead of a
    section-key filter we take a window of text around each keyword hit,
    capped at the same prompt budget as the annual extractor."""
    lowered = plain_text.lower()
    spans: list[tuple[int, int]] = []
    for kw in _SECTION_KEYWORDS:
        start = 0
        while True:
            idx = lowered.find(kw, start)
            if idx == -1:
                break
            spans.append((max(0, idx - 200), min(len(plain_text), idx + 1500)))
            start = idx + len(kw)
    if not spans:
        return ""
    spans.sort()
    merged: list[tuple[int, int]] = []
    for s, e in spans:
        if merged and s <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
        else:
            merged.append((s, e))
    parts: list[str] = []
    total = 0
    for s, e in merged:
        chunk = plain_text[s:e]
        if total + len(chunk) > _MAX_TEXT_BUDGET:
            chunk = chunk[: _MAX_TEXT_BUDGET - total]
        parts.append(chunk)
        total += len(chunk)
        if total >= _MAX_TEXT_BUDGET:
            break
    return "\n\n---\n\n".join(parts)


# ---------------------------------------------------------------------------
# LLM call
# ---------------------------------------------------------------------------


def _build_prompt(ticker: str, year: int, quarter: str, text: str) -> str:
    return f"""You are extracting segment / product / geographic revenue breakdowns from
a foreign private issuer's 6-K interim report for {ticker}, fiscal quarter {quarter} {year}.

Find every table in the text below that breaks a financial metric down by a named
sub-group for THIS quarter (not just the full-year/annual comparative). Typical shapes:
revenue by geography, revenue by product/therapy/drug, non-current assets by geography.

For each breakdown found, return:
- "axis": one of "geography", "product", "channel", "customer_segment", "business_unit"
- "metric": a short snake_case tag for what's being measured -- "revenue" for a plain
  sales/revenue table, "non_current_assets" for a non-current-assets-by-geography table,
  or another short snake_case tag if the table measures something else explicitly labeled.
- "cells": one entry per row of the table, with the row's label ("name"), the metric's
  raw value for the current quarter (do NOT scale; if the filing says "3,586,566" with a
  header indicating thousands, return 3586566000 -- always express the value in the
  filing's own base currency units), the period end date (ISO YYYY-MM-DD) that the
  filing itself reports for the current-quarter column, the fiscal period type
  ("Q1"/"Q2"/"Q3"/"Q4"), and the reporting currency if stated (e.g. "USD"/"EUR"/"BRL"/"DKK").

Do NOT include prior-year comparative columns as separate cells -- only the current
quarter's column. Do NOT manufacture a breakdown that isn't explicitly presented as a
table in the text. If a table only presents Total/annual figures with no sub-group
breakdown, skip it. Do NOT include "Total"/subtotal rows as cells (e.g. "Total",
"Total revenue", a regional rollup that just sums other listed rows) -- only the
individual sub-group rows; a subtotal alongside its components double-counts.

Return STRICT JSON, no markdown fence, no commentary:

{{
  "breakdowns": [
    {{
      "axis": "geography",
      "metric": "revenue",
      "cells": [
        {{"name": "Brazil", "value": 3586566000, "period_end": "2026-03-31", "fiscal_period_type": "Q1", "currency": "BRL"}}
      ]
    }}
  ]
}}

If the text has no such breakdown at all, return {{"breakdowns": []}}.

6-K excerpt:
\"\"\"
{text}
\"\"\""""


def _ask_claude(ticker: str, year: int, quarter: str, text: str) -> list[SixKBreakdown]:
    prompt = _build_prompt(ticker, year, quarter, text)
    raw = _call_claude(prompt, model=FAST_CLASSIFIER_MODEL).strip()
    if raw.startswith("```"):
        raw = JSON_FENCE_RE.sub("", raw).strip()
    try:
        parsed: object = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if not isinstance(parsed, dict):
        return []
    parsed_obj = cast("dict[str, object]", parsed)
    items = parsed_obj.get("breakdowns")
    if not isinstance(items, list):
        return []
    out: list[SixKBreakdown] = []
    for entry in cast("list[object]", items):
        if not isinstance(entry, dict):
            continue
        entry_obj = cast("dict[str, object]", entry)
        axis = entry_obj.get("axis")
        metric = entry_obj.get("metric")
        cells_raw = entry_obj.get("cells")
        if not (isinstance(axis, str) and isinstance(metric, str) and isinstance(cells_raw, list)):
            continue
        cells: list[SixKCell] = []
        for cell in cast("list[object]", cells_raw):
            if not isinstance(cell, dict):
                continue
            cell_obj = cast("dict[str, object]", cell)
            name = cell_obj.get("name")
            value = cell_obj.get("value")
            period_end = cell_obj.get("period_end")
            fiscal_period_type = cell_obj.get("fiscal_period_type")
            currency = cell_obj.get("currency")
            if not (
                isinstance(name, str)
                and isinstance(value, (int, float))
                and isinstance(period_end, str)
                and isinstance(fiscal_period_type, str)
            ):
                continue
            cells.append(
                SixKCell(
                    name=name,
                    period_end=period_end,
                    fiscal_period_type=fiscal_period_type,
                    value=float(value),
                    currency=currency if isinstance(currency, str) else None,
                )
            )
        if not cells:
            continue
        out.append(SixKBreakdown(axis=axis, metric=metric.strip().lower(), cells=cells))
    return out


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def _persist_breakdowns(
    conn: sqlite3.Connection,
    *,
    ticker: str,
    source_doc_id: int,
    breakdowns: list[SixKBreakdown],
) -> tuple[int, int, int]:
    periods_inserted = 0
    dimensions_inserted = 0
    cells_skipped = 0

    grouped: dict[
        tuple[datetime, FiscalPeriodType, Any],
        list[SegmentDimension],
    ] = {}

    for breakdown in breakdowns:
        dim_type = _axis_to_dim_type(breakdown.axis)
        if dim_type is None:
            cells_skipped += len(breakdown.cells)
            continue
        metric = re.sub(r"[^a-z0-9_]+", "_", breakdown.metric.strip().lower()).strip("_")
        if not metric:
            metric = "revenue"
        for cell in breakdown.cells:
            period_end = _parse_period_end(cell.period_end)
            if period_end is None:
                cells_skipped += 1
                continue
            try:
                period_type = FiscalPeriodType(cell.fiscal_period_type.strip().upper())
            except ValueError:
                cells_skipped += 1
                continue
            value = _parse_decimal(cell.value)
            if value is None:
                cells_skipped += 1
                continue
            name = cell.name.strip()
            if not name:
                cells_skipped += 1
                continue
            if _is_subtotal_name(name):
                cells_skipped += 1
                continue
            currency = _parse_currency(cell.currency)
            grouped.setdefault((period_end, period_type, currency), []).append(
                SegmentDimension(
                    dim_type=dim_type,
                    dim_name=name,
                    value=value,
                    metric=metric,
                    disclosure_status="reported",
                    method_version=EXTRACTOR_ID,
                    confidence=1.0,
                    extracted_by=f"llm:{FAST_CLASSIFIER_MODEL}",
                    locator=json.dumps({"section": "6-K narrative extraction"}),
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


def _breakdown_to_dict(b: SixKBreakdown) -> dict[str, Any]:
    return {
        "axis": b.axis,
        "metric": b.metric,
        "cells": [asdict(c) for c in b.cells],
    }
