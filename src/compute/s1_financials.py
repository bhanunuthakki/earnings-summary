"""Extract audited financial statements from a cached S-1 prospectus.

Recently-IPO'd issuers (e.g. FRVO / Fervo Energy) have no 10-K yet, and FMP
returns empty arrays for their income/balance/cashflow endpoints until it
ingests the first 10-Q. The audited consolidated statements DO exist — inside
the S-1 "F-pages" (Index to Consolidated Financial Statements). This module
parses those statements out of the HTML-stripped S-1 text cached at
``data/sec_text/<TICKER>_s1_<FY>.txt`` and emits canonical FinancialFact rows,
so downstream consumers (thesis evaluator, timeseries layer, DCF historicals)
have something to anchor on.

Provenance & precedence
-----------------------
Facts are written with ``extracted_by='s1'`` and tied to a ``documents`` row
whose ``source_quality_tier='s1_provisional'`` — the LOWEST tier (rank 0 in
``timeseries.loaders.SOURCE_QUALITY_TIER_RANK``). When FMP / SEC-XBRL ingest
the company's real filings later, those higher-tier rows win for the same
logical key. The S-1 snapshot is the oldest view of these numbers and is meant
to be superseded.

Parsing approach
----------------
The HTML-stripped text flattens each statement into a regular shape: a
line-item label (with trailing dotted leaders) on its own line, followed by
one line per period column. Sign convention follows the issuer's presentation
— parenthesised values are negative, an em/en dash is nil (0). Statements are
parsed region-by-region (operations / balance sheet / cash flow) so a label
that appears in more than one statement (e.g. "Net loss", "Depreciation and
amortization") maps to the right canonical line_item per statement.

Values are scaled to actual units (the F-pages report "in thousands"), matching
FMP's actual-dollar convention; per-share amounts are left unscaled.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from compute._common import insert_financial_facts, load_document_row
from models.facts import Currency, FinancialFact, FiscalPeriodType, Unit

log = logging.getLogger(__name__)

EXTRACTED_BY = "s1"

# ---------------------------------------------------------------------------
# Label -> canonical line_item specs, one per statement.
#
# Each entry: (match_mode, match_text, [canonical_line_items], unit, scale).
#   match_mode: "eq" (normalized label equals match_text) or
#               "sw" (normalized label starts with match_text).
#   scale: multiplier applied to the printed figure to reach actual units
#          (1000 for "in thousands" lines; 1 for per-share amounts).
# A single S-1 label may populate several canonical line_items (e.g. a
# "basic and diluted" line feeds both the basic and diluted canonical names),
# mirroring how the FMP extractors carry parallel fields.
# ---------------------------------------------------------------------------

_SpecEntry = tuple[str, str, list[str], Unit, int]

_OPERATIONS_SPEC: list[_SpecEntry] = [
    ("eq", "revenues", ["revenue"], Unit.ACTUAL, 1000),
    ("sw", "general and administrative expense", ["sga"], Unit.ACTUAL, 1000),
    ("eq", "depreciation and amortization", ["depreciation_and_amortization"], Unit.ACTUAL, 1000),
    ("eq", "operating loss", ["operating_income"], Unit.ACTUAL, 1000),
    ("eq", "operating income", ["operating_income"], Unit.ACTUAL, 1000),
    ("eq", "interest income", ["interest_income"], Unit.ACTUAL, 1000),
    ("eq", "loss before income taxes", ["income_before_tax"], Unit.ACTUAL, 1000),
    ("eq", "income before income taxes", ["income_before_tax"], Unit.ACTUAL, 1000),
    ("eq", "net loss", ["net_income"], Unit.ACTUAL, 1000),
    ("eq", "net income", ["net_income"], Unit.ACTUAL, 1000),
    ("sw", "net loss per share attributable to common", ["eps", "eps_diluted"], Unit.ACTUAL, 1),
    ("sw", "net income per share attributable to common", ["eps", "eps_diluted"], Unit.ACTUAL, 1),
    ("sw", "weighted average shares", ["weighted_avg_shares", "weighted_avg_shares_diluted"], Unit.COUNT, 1000),
]

_BALANCE_SHEET_SPEC: list[_SpecEntry] = [
    ("eq", "cash and cash equivalents", ["cash_and_equivalents"], Unit.ACTUAL, 1000),
    ("eq", "total current assets", ["total_current_assets"], Unit.ACTUAL, 1000),
    ("eq", "operating leases right of use assets", ["operating_lease_rou_asset"], Unit.ACTUAL, 1000),
    ("eq", "total assets", ["total_assets"], Unit.ACTUAL, 1000),
    ("eq", "accounts payable", ["accounts_payable"], Unit.ACTUAL, 1000),
    ("eq", "total current liabilities", ["total_current_liabilities"], Unit.ACTUAL, 1000),
    ("sw", "long-term debt", ["long_term_debt"], Unit.ACTUAL, 1000),
    ("eq", "total liabilities", ["total_liabilities"], Unit.ACTUAL, 1000),
    ("eq", "accumulated deficit", ["retained_earnings"], Unit.ACTUAL, 1000),
    ("sw", "total stockholders", ["total_stockholders_equity"], Unit.ACTUAL, 1000),
]

_CASHFLOW_SPEC: list[_SpecEntry] = [
    ("eq", "net loss", ["net_income_cf"], Unit.ACTUAL, 1000),
    ("eq", "net income", ["net_income_cf"], Unit.ACTUAL, 1000),
    ("eq", "depreciation and amortization", ["depreciation_and_amortization_cf"], Unit.ACTUAL, 1000),
    ("eq", "stock-based compensation", ["stock_based_compensation"], Unit.ACTUAL, 1000),
    ("sw", "net cash used in operating activities", ["operating_cash_flow", "net_cash_from_operating"], Unit.ACTUAL, 1000),
    ("sw", "net cash provided by operating activities", ["operating_cash_flow", "net_cash_from_operating"], Unit.ACTUAL, 1000),
    ("eq", "capital expenditures", ["capital_expenditure"], Unit.ACTUAL, 1000),
    ("sw", "net cash used in investing activities", ["net_cash_from_investing"], Unit.ACTUAL, 1000),
    ("sw", "net cash provided by investing activities", ["net_cash_from_investing"], Unit.ACTUAL, 1000),
    ("sw", "net cash provided by financing activities", ["net_cash_from_financing"], Unit.ACTUAL, 1000),
    ("sw", "net cash used in financing activities", ["net_cash_from_financing"], Unit.ACTUAL, 1000),
    ("sw", "net change in cash", ["net_change_in_cash"], Unit.ACTUAL, 1000),
    ("sw", "cash and cash equivalents and restricted cash at end of period", ["cash_at_end_of_period"], Unit.ACTUAL, 1000),
]


@dataclass(slots=True)
class S1Datum:
    """One parsed measurement before it is tied to a source document."""

    line_item: str
    period_end: datetime
    fiscal_period_type: FiscalPeriodType
    value: Decimal
    unit: Unit
    currency: Currency | None


# A "value cell": optional $, optional leading ( for negatives, digits with
# thousands separators, optional decimals, optional trailing ). A lone em dash
# (or "$—") is nil — the HTML stripper renders nil entities as U+2014 and maps
# en-dash entities to an ASCII hyphen, so those two cover every nil cell.
_VALUE_RX = re.compile(r"^\(?\$?\(?-?[\d,]+(?:\.\d+)?\)?$")
_NIL_TOKENS = {"—", "-", "$—", "——"}
_YEAR_RX = re.compile(r"^(19|20)\d{2}$")


def _normalize_label(raw: str) -> str:
    """Lowercase, drop trailing dotted leaders / page noise, collapse spaces."""
    s = raw.strip()
    # Remove trailing runs of dots / ellipsis / whitespace (the leader dots).
    s = re.sub(r"[.…\s]+$", "", s)
    s = re.sub(r"\s+", " ", s)
    return s.lower()


def _is_value_cell(s: str) -> bool:
    if s in _NIL_TOKENS:
        return True
    return bool(_VALUE_RX.match(s))


def _parse_value(s: str) -> Decimal | None:
    """Parse a printed figure to a signed Decimal. Nil dash -> 0. None if junk.

    Strip currency/grouping symbols before testing for parentheses so a negative
    written as ``$(57,788)`` (dollar sign outside the parens) is handled.
    """
    t = s.strip()
    if t in _NIL_TOKENS:
        return Decimal(0)
    t = t.replace("$", "").replace(",", "").strip()
    if not t or t in _NIL_TOKENS:
        return Decimal(0)
    negative = False
    if t.startswith("(") and t.endswith(")"):
        negative = True
        t = t[1:-1].strip()
    try:
        value = Decimal(t)
    except InvalidOperation:
        return None
    return -value if negative else value


def _audited_section_start(lines: list[str]) -> int:
    """Index of the auditor's report header — the start of the audited F-pages.

    The "Index to Consolidated Financial Statements" table of contents lists the
    same titles earlier with dotted page leaders; we skip those by requiring the
    matched line to be a bare header (no leader dots).
    """
    needle = "report of independent registered public accounting firm"
    for i, ln in enumerate(lines):
        norm = ln.strip().lower()
        if norm.startswith(needle) and ".." not in ln:
            return i
    return 0


_HEADER_RX: dict[str, re.Pattern[str]] = {
    "operations": re.compile(r"^consolidated statements? of operations\b", re.IGNORECASE),
    "balance": re.compile(r"^consolidated balance sheets?\b", re.IGNORECASE),
    "cashflow": re.compile(r"^consolidated statements? of cash flows?\b", re.IGNORECASE),
}

# Any line that opens a new statement / section — used to bound each region.
_SECTION_BOUNDARY_RX = re.compile(
    r"^(consolidated balance sheets?"
    r"|consolidated statements? of (operations|cash flows?|comprehensive|"
    r"redeemable|stockholders|changes|preferred)"
    r"|notes to (consolidated )?financial statements"
    r"|report of independent)",
    re.IGNORECASE,
)


def _find_statement_regions(lines: list[str]) -> dict[str, tuple[int, int]]:
    """Map each target statement to its [start, end) line range in the F-pages.

    Picks the first *bare* header (no leader dots) after the auditor's report,
    then bounds the region at the next section boundary.
    """
    start = _audited_section_start(lines)
    boundaries = [
        i for i in range(start + 1, len(lines))
        if _SECTION_BOUNDARY_RX.match(lines[i].strip()) and ".." not in lines[i]
    ]
    regions: dict[str, tuple[int, int]] = {}
    for name, rx in _HEADER_RX.items():
        header_idx = next(
            (
                i
                for i in range(start, len(lines))
                if rx.match(lines[i].strip()) and ".." not in lines[i]
            ),
            None,
        )
        if header_idx is None:
            continue
        end = next((b for b in boundaries if b > header_idx), len(lines))
        regions[name] = (header_idx, end)
    return regions


def _parse_period_columns(region: list[str]) -> list[datetime]:
    """Read the year columns from a statement region's header row.

    Anchors on "Year ended December 31," / "As of December 31," and collects the
    bare 4-digit years that follow, each becoming a fiscal-year-end period_end.
    """
    anchor_rx = re.compile(r"(year ended|years ended|as of)\b.*december\s+31", re.IGNORECASE)
    anchor = next((i for i, ln in enumerate(region) if anchor_rx.search(ln.strip())), None)
    if anchor is None:
        return []
    years: list[datetime] = []
    for ln in region[anchor + 1:]:
        s = ln.strip()
        if not s:
            continue
        if _YEAR_RX.match(s):
            years.append(datetime(int(s), 12, 31))
            continue
        # First non-year, non-blank line ends the column header block.
        break
    return years


def _scale_for_region(region: list[str]) -> int:
    """Default magnitude multiplier inferred from the '(... in thousands)' note."""
    head = " ".join(region[:8]).lower()
    if "in thousands" in head:
        return 1000
    if "in millions" in head:
        return 1_000_000
    return 1


def _match_spec(norm_label: str, spec: list[_SpecEntry]) -> _SpecEntry | None:
    for entry in spec:
        mode, text, *_ = entry
        if mode == "eq" and norm_label == text:
            return entry
        if mode == "sw" and norm_label.startswith(text):
            return entry
    return None


def _iter_data_lines(region: list[str], data_start: int):
    """Yield (normalized_label, [value_cells]) for each data row in a region.

    A data row is a label line followed (after blank lines) by one or more
    contiguous value cells. Header/sub-total grouping lines have no following
    value cells and are skipped.
    """
    i = data_start
    n = len(region)
    while i < n:
        raw = region[i].strip()
        if not raw or _is_value_cell(raw):
            i += 1
            continue
        # Candidate label — look ahead for contiguous value cells.
        j = i + 1
        cells: list[str] = []
        while j < n:
            s = region[j].strip()
            if not s:
                j += 1
                continue
            if _is_value_cell(s):
                cells.append(s)
                j += 1
                continue
            break
        if cells:
            yield _normalize_label(raw), cells
            i = j
        else:
            i += 1


def _parse_statement(
    region: list[str], spec: list[_SpecEntry]
) -> list[S1Datum]:
    """Parse one statement region into S1Datum rows using its label spec."""
    periods = _parse_period_columns(region)
    if not periods:
        return []
    default_scale = _scale_for_region(region)
    # Begin data parsing after the year-column header block.
    anchor_rx = re.compile(r"(year ended|years ended|as of)\b.*december\s+31", re.IGNORECASE)
    anchor = next((i for i, ln in enumerate(region) if anchor_rx.search(ln.strip())), 0)
    data_start = anchor + 1
    seen: set[str] = set()
    out: list[S1Datum] = []
    for norm_label, cells in _iter_data_lines(region, data_start):
        entry = _match_spec(norm_label, spec)
        if entry is None:
            continue
        _mode, _text, line_items, unit, scale = entry
        # Require exact column alignment so a flattening glitch can't silently
        # shift a value into the wrong fiscal year.
        if len(cells) != len(periods):
            log.debug(
                {
                    "event": "s1_column_misalign",
                    "label": norm_label,
                    "cells": len(cells),
                    "periods": len(periods),
                }
            )
            continue
        effective_scale = scale if scale != 1000 else default_scale
        for line_item in line_items:
            if line_item in seen:
                continue  # first occurrence wins (skip per-share restatement echoes)
            seen.add(line_item)
            currency = Currency.USD if unit is Unit.ACTUAL else None
            for period_end, cell in zip(periods, cells, strict=True):
                value = _parse_value(cell)
                if value is None:
                    continue
                out.append(
                    S1Datum(
                        line_item=line_item,
                        period_end=period_end,
                        fiscal_period_type=FiscalPeriodType.FY,
                        value=value * effective_scale,
                        unit=unit,
                        currency=currency,
                    )
                )
    return out


def _derive_free_cash_flow(data: list[S1Datum]) -> list[S1Datum]:
    """free_cash_flow = operating_cash_flow + capital_expenditure (capex < 0).

    FMP reports free_cash_flow directly; the S-1 doesn't, so we derive it the
    same way per fiscal period when both inputs are present.
    """
    ocf = {d.period_end: d for d in data if d.line_item == "operating_cash_flow"}
    capex = {d.period_end: d for d in data if d.line_item == "capital_expenditure"}
    out: list[S1Datum] = []
    for period_end, ocf_datum in ocf.items():
        capex_datum = capex.get(period_end)
        if capex_datum is None:
            continue
        out.append(
            S1Datum(
                line_item="free_cash_flow",
                period_end=period_end,
                fiscal_period_type=FiscalPeriodType.FY,
                value=ocf_datum.value + capex_datum.value,
                unit=Unit.ACTUAL,
                currency=Currency.USD,
            )
        )
    return out


def parse_s1_text(text: str) -> list[S1Datum]:
    """Parse the audited income statement, balance sheet, and cash-flow
    statement out of an HTML-stripped S-1 text blob."""
    lines = text.splitlines()
    regions = _find_statement_regions(lines)
    data: list[S1Datum] = []
    if "operations" in regions:
        lo, hi = regions["operations"]
        data += _parse_statement(lines[lo:hi], _OPERATIONS_SPEC)
    if "balance" in regions:
        lo, hi = regions["balance"]
        data += _parse_statement(lines[lo:hi], _BALANCE_SHEET_SPEC)
    if "cashflow" in regions:
        lo, hi = regions["cashflow"]
        data += _parse_statement(lines[lo:hi], _CASHFLOW_SPEC)
    data += _derive_free_cash_flow(data)
    return data


def build_financial_facts(
    data: list[S1Datum], source_doc_id: int, ticker: str
) -> list[FinancialFact]:
    """Tie parsed S1Datum rows to a source document, producing FinancialFacts."""
    facts: list[FinancialFact] = []
    for d in data:
        facts.append(
            FinancialFact(
                ticker=ticker.upper(),
                period_end=d.period_end,
                fiscal_period_type=d.fiscal_period_type,
                line_item=d.line_item,
                value=d.value,
                currency=d.currency,
                unit=d.unit,
                source_doc_id=source_doc_id,
                confidence=1.0,
            )
        )
    return facts


def extract_s1_financial_facts(
    conn: sqlite3.Connection, document_id: int, project_root: Path
) -> int:
    """Read the S-1 document's cached text, write FinancialFact rows. Idempotent.

    Mirrors the FMP extractors' ``(conn, document_id, project_root) -> int``
    signature so it can slot into the same dispatch shape. The document must be
    doc_type ``sec_s1``; facts are written with ``extracted_by='s1'``.
    """
    ticker, file_path_str = load_document_row(conn, document_id, "sec_s1")
    text = (project_root / file_path_str).read_text(encoding="utf-8")
    data = parse_s1_text(text)
    facts = build_financial_facts(data, source_doc_id=document_id, ticker=ticker)
    inserted = insert_financial_facts(conn, facts, extracted_by=EXTRACTED_BY)
    conn.commit()
    return inserted
