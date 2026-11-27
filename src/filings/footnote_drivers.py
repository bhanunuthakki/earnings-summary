"""Pure, source-consolidated SEC footnote driver extraction (Item 2B).

Slices high-signal notes from retained SEC Item 8 filing text (commitments,
construction-in-progress / depreciation useful life, SBC dilution overhang,
leases, one-off EBIT adjustments, RPO/backlog), enforces a closed ``fact_type``
taxonomy and fail-closed substring quote verification, and persists bounded
idempotent rows into the existing ``footnote_facts`` table without new schema
migrations or silent network crawls.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from llm.structured import StructuredParseError, call_llm_structured
from llm.untrusted import spotlight

log = logging.getLogger(__name__)

_ITEM_8_RX = re.compile(
    r"item\s*8\.?\s*[\u2014\u2013\-:]?\s*financial\s+statements",
    re.IGNORECASE,
)
_ITEM_9_RX = re.compile(
    r"item\s*9\.?\s*[\u2014\u2013\-:]?\s*changes",
    re.IGNORECASE,
)


def _extract_item_8_section(text: str) -> str | None:
    start = _ITEM_8_RX.search(text)
    if start is None:
        return None
    end = _ITEM_9_RX.search(text, pos=start.end())
    return text[start.start() : end.start()] if end is not None else text[start.start() :]


DEFAULT_MAX_CHARS = 120_000
MAX_ROWS_PER_FILING = 18

FootnoteFactType = Literal[
    "lease_commitment",
    "contingent_liability",
    "related_party_tx",
    "stock_based_comp",
    "off_balance_sheet",
    "goodwill_change",
    "segment_realignment",
    "restructuring_charge",
    "litigation_reserve",
    "purchase_commitment",
    "cip_depreciation_policy",
    "sbc_dilution_overhang",
    "one_off_ebit_adjustment",
    "rpo_backlog",
]

ALLOWED_FACT_TYPES: frozenset[str] = frozenset(
    {
        "lease_commitment",
        "contingent_liability",
        "related_party_tx",
        "stock_based_comp",
        "off_balance_sheet",
        "goodwill_change",
        "segment_realignment",
        "restructuring_charge",
        "litigation_reserve",
        "purchase_commitment",
        "cip_depreciation_policy",
        "sbc_dilution_overhang",
        "one_off_ebit_adjustment",
        "rpo_backlog",
    }
)

_NOTE_HEADER_RX = re.compile(r"(?im)^[ \t]*(?:note\s+\d+[a-z]?)\b[^\n]{0,140}$")

# High-signal note title / body keywords ordered roughly by analytical leverage.
_HIGH_SIGNAL_KEYWORDS: tuple[tuple[str, int], ...] = (
    ("commitments and contingencies", 10),
    ("purchase commitment", 10),
    ("unconditional purchase", 10),
    ("construction in progress", 9),
    ("useful life", 9),
    ("useful lives", 9),
    ("depreciation", 7),
    ("property and equipment", 7),
    ("property, plant", 7),
    ("stock-based compensation", 9),
    ("share-based compensation", 9),
    ("restricted stock", 8),
    ("equity incentive", 7),
    ("remaining performance obligation", 9),
    ("deferred revenue", 6),
    ("lease", 7),
    ("segment", 7),
    ("realignment", 8),
    ("reclassification", 8),
    ("restructuring", 7),
    ("severance", 7),
    ("impairment", 7),
    ("goodwill", 6),
    ("related party", 7),
    ("related-party", 7),
    ("litigation", 6),
    ("contingenc", 6),
    ("indemnif", 6),
    ("debt", 5),
    ("credit facilit", 5),
    ("convertible", 6),
    ("at-the-market", 7),
)

_BOILERPLATE_PENALTIES: tuple[str, ...] = (
    "summary of significant accounting policies",
    "recently issued accounting pronouncements",
    "recent accounting pronouncements",
    "income taxes",
    "earnings per share",
    "fair value measurements",
)

_PROMPT = """You are extracting high-signal footnote drivers from {ticker}'s
fiscal year {fiscal_year} 10-K Item 8 notes.

Extract ONLY material disclosures that affect unit economics, capex/depreciation
timing, off-balance-sheet commitments, dilution/SBC overhang, backlog, segment
reclassifications, or one-off EBIT items. Skip routine accounting policy
boilerplate.

Allowed `fact_type` values (closed enum; <= 32 chars):
  - "purchase_commitment"      (non-cancelable supplier/compute/cloud commitments)
  - "cip_depreciation_policy"  (construction-in-progress balances, asset useful-life changes)
  - "sbc_dilution_overhang"    (unrecognized SBC, unvested RSUs, ATM/convertible dilution)
  - "one_off_ebit_adjustment"  (restructuring, severance, tax disputes, store/asset closures)
  - "rpo_backlog"              (remaining performance obligations, backlog duration)
  - "lease_commitment"         (operating/finance lease obligations, JV lease starts)
  - "contingent_liability"
  - "related_party_tx"
  - "stock_based_comp"
  - "off_balance_sheet"
  - "goodwill_change"
  - "segment_realignment"
  - "restructuring_charge"
  - "litigation_reserve"

Emit a JSON array of up to 18 objects, each with:
{{
  "fact_type": "<one of the allowed values above>",
  "footnote_number": "Note 7" | "Note 12" | null,
  "description_md": "<2-3 sentences naming the specific mechanic, period, and impact>",
  "amount": <numeric value or null>,
  "currency": "USD" | "EUR" | ... | null,
  "unit": "thousands" | "millions" | "billions" | "years" | "shares" | null,
  "due_period": "YYYY-MM-DD" or null,
  "counterparty": "<named counterparty or null>",
  "status": "active" | "resolved" | "reclassified",
  "source_excerpt": "<VERBATIM quote 30-220 chars copied directly from the input text>"
}}

Rules:
  - Return ONLY a JSON array (empty [] if nothing material). No markdown fences.
  - Every `source_excerpt` MUST appear verbatim in the input text below.
  - Convert "$2.4 billion" to amount=2400, unit="millions" when monetary.

INPUT TEXT (Item 8 selected notes):
{item_8_text}
"""


class FootnoteDriverRow(BaseModel):
    model_config = ConfigDict(extra="ignore")

    fact_type: FootnoteFactType
    footnote_number: str | None = Field(default=None, max_length=16)
    description_md: str = Field(min_length=1)
    amount: float | None = None
    currency: str | None = Field(default=None, max_length=3)
    unit: str | None = Field(default=None, max_length=16)
    due_period: str | None = None
    counterparty: str | None = Field(default=None, max_length=255)
    status: str | None = Field(default="active", max_length=24)
    source_excerpt: str = Field(min_length=10)


_ROW_LIST_ADAPTER = TypeAdapter(list[FootnoteDriverRow])


@dataclass(frozen=True, slots=True)
class NoteSliceResult:
    sliced_text: str
    notes_found: int
    notes_selected: tuple[str, ...]
    truncated: bool
    fallback_used: bool


@dataclass(frozen=True, slots=True)
class FootnoteExtractionResult:
    ticker: str
    fiscal_year: int
    status: str
    slice_info: NoteSliceResult
    accepted_rows: tuple[FootnoteDriverRow, ...]
    rejected_count: int


def _normalize_ws(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()


def verify_source_excerpt(excerpt: str, source_text: str) -> bool:
    """Return True iff ``excerpt`` appears in ``source_text`` after whitespace folding."""
    norm_excerpt = _normalize_ws(excerpt)
    if len(norm_excerpt) < 10:
        return False
    return norm_excerpt in _normalize_ws(source_text)


def slice_high_signal_notes(
    item_8_text: str, *, max_chars: int = DEFAULT_MAX_CHARS
) -> NoteSliceResult:
    """Slice high-signal footnote sections from Item 8 text within ``max_chars``."""
    stripped = item_8_text.strip()
    if not stripped:
        return NoteSliceResult(
            sliced_text="",
            notes_found=0,
            notes_selected=(),
            truncated=False,
            fallback_used=False,
        )

    matches = list(_NOTE_HEADER_RX.finditer(stripped))
    if not matches:
        truncated = len(stripped) > max_chars
        return NoteSliceResult(
            sliced_text=stripped[:max_chars],
            notes_found=0,
            notes_selected=(),
            truncated=truncated,
            fallback_used=True,
        )

    sections: list[tuple[int, str, str, int]] = []
    for idx, match in enumerate(matches):
        start = match.start()
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(stripped)
        header = match.group(0).strip()
        body = stripped[start:end].strip()
        lower = body[:6000].lower()
        header_lower = header.lower()

        score = 0
        for kw, weight in _HIGH_SIGNAL_KEYWORDS:
            if kw in header_lower:
                score += weight * 2
            elif kw in lower:
                score += weight
        for penalty in _BOILERPLATE_PENALTIES:
            if penalty in header_lower:
                score -= 8
        sections.append((idx, header, body, score))

    positive = [sec for sec in sections if sec[3] > 0]
    chosen_pool = positive if positive else sections
    # Prioritize highest-scoring notes, then restore original filing order.
    ranked = sorted(chosen_pool, key=lambda s: (-s[3], s[0]))

    kept: list[tuple[int, str, str]] = []
    used_chars = 0
    truncated = False
    for idx, header, body, _score in ranked:
        addition = len(body) + (2 if kept else 0)
        if used_chars + addition <= max_chars:
            kept.append((idx, header, body))
            used_chars += addition
        elif not kept:
            kept.append((idx, header, body[:max_chars]))
            used_chars = max_chars
            truncated = True
            break
        else:
            truncated = True

    kept.sort(key=lambda s: s[0])
    sliced = "\n\n".join(body for _, _, body in kept)
    return NoteSliceResult(
        sliced_text=sliced,
        notes_found=len(sections),
        notes_selected=tuple(header for _, header, _ in kept),
        truncated=truncated or len(kept) < len(sections),
        fallback_used=False,
    )


def load_cached_item_8_text(
    *,
    repo_root: Path,
    ticker: str,
    fiscal_year: int | None = None,
) -> tuple[int, str] | None:
    """Read retained ``data/sec_text/<TICKER>_10k_<FY>.txt`` without network calls."""
    cache_dir = repo_root / "data" / "sec_text"
    if not cache_dir.is_dir():
        return None
    upper = ticker.upper()
    if fiscal_year is not None:
        candidate = cache_dir / f"{upper}_10k_{fiscal_year}.txt"
        candidates = [candidate] if candidate.is_file() else []
    else:
        candidates = sorted(cache_dir.glob(f"{upper}_10k_*.txt"), reverse=True)
    for path in candidates:
        m = re.search(r"_10k_(\d{4})\.txt$", path.name)
        if m is None:
            continue
        fy = int(m.group(1))
        text = path.read_text(encoding="utf-8", errors="replace")
        item_8 = _extract_item_8_section(text)
        if item_8 and item_8.strip():
            return fy, item_8
        if text.strip():
            return fy, text
    return None


def extract_footnote_drivers_from_text(
    *,
    ticker: str,
    fiscal_year: int,
    item_8_text: str,
    max_chars: int = DEFAULT_MAX_CHARS,
    llm_caller: Callable[..., object] | None = None,
) -> FootnoteExtractionResult:
    """Slice Item 8 notes, run structured extraction, and verify excerpts fail-closed."""
    slice_info = slice_high_signal_notes(item_8_text, max_chars=max_chars)
    if not slice_info.sliced_text:
        return FootnoteExtractionResult(
            ticker=ticker.upper(),
            fiscal_year=fiscal_year,
            status="no_item_8",
            slice_info=slice_info,
            accepted_rows=(),
            rejected_count=0,
        )

    prompt = _PROMPT.format(
        ticker=ticker.upper(),
        fiscal_year=fiscal_year,
        item_8_text=spotlight(slice_info.sliced_text, source="SEC 10-K Item 8 selected notes"),
    )
    caller = llm_caller or call_llm_structured
    try:
        raw_result = caller(
            prompt,
            purpose="footnote_extraction",
            ticker=ticker.upper(),
            scope="filing_footnotes",
            expect="array",
            schema=_ROW_LIST_ADAPTER,
        )
        validated = (
            _ROW_LIST_ADAPTER.validate_json(raw_result)
            if isinstance(raw_result, str)
            else _ROW_LIST_ADAPTER.validate_python(raw_result)
        )
    except StructuredParseError as exc:
        log.warning(
            {"event": "footnote_structured_failed", "ticker": ticker.upper(), "error": str(exc)}
        )
        return FootnoteExtractionResult(
            ticker=ticker.upper(),
            fiscal_year=fiscal_year,
            status="structured_failed",
            slice_info=slice_info,
            accepted_rows=(),
            rejected_count=0,
        )
    except Exception as exc:
        log.warning({"event": "footnote_llm_failed", "ticker": ticker.upper(), "error": str(exc)})
        return FootnoteExtractionResult(
            ticker=ticker.upper(),
            fiscal_year=fiscal_year,
            status="llm_failed",
            slice_info=slice_info,
            accepted_rows=(),
            rejected_count=0,
        )

    accepted: list[FootnoteDriverRow] = []
    rejected = 0
    for row in validated:
        if row.fact_type not in ALLOWED_FACT_TYPES:
            rejected += 1
            continue
        if not verify_source_excerpt(row.source_excerpt, slice_info.sliced_text):
            rejected += 1
            continue
        accepted.append(row)
        if len(accepted) >= MAX_ROWS_PER_FILING:
            break

    return FootnoteExtractionResult(
        ticker=ticker.upper(),
        fiscal_year=fiscal_year,
        status="ok",
        slice_info=slice_info,
        accepted_rows=tuple(accepted),
        rejected_count=rejected,
    )


def _parse_due_period(value: str | None) -> str | None:
    if not value:
        return None
    cleaned = value.strip()
    try:
        return datetime.fromisoformat(cleaned).isoformat()
    except ValueError:
        return None


def persist_footnote_drivers(
    conn: sqlite3.Connection,
    *,
    ticker: str,
    fiscal_year: int,
    rows: Sequence[FootnoteDriverRow],
    filing_doc_id: int | None = None,
    period_end: datetime | None = None,
) -> int:
    """Idempotently persist verified footnote drivers into ``footnote_facts``.

    Caller owns connection lifecycle; this function commits on success.
    """
    if not rows:
        return 0
    upper = ticker.upper()
    resolved_period_end = (period_end or datetime(fiscal_year, 12, 31)).isoformat()
    now_iso = datetime.now(UTC).isoformat()
    inserted = 0

    for row in rows:
        footnote_num = (row.footnote_number or "").strip()
        excerpt = row.source_excerpt.strip()[:1024]
        existing = conn.execute(
            """
            SELECT 1 FROM footnote_facts
            WHERE ticker = ?
              AND period_end = ?
              AND fiscal_period_type = 'FY'
              AND COALESCE(footnote_number, '') = ?
              AND fact_type = ?
              AND COALESCE(source_excerpt, '') = ?
            LIMIT 1
            """,
            (upper, resolved_period_end, footnote_num, row.fact_type, excerpt),
        ).fetchone()
        if existing is not None:
            continue
        due_iso = _parse_due_period(row.due_period)
        conn.execute(
            """
            INSERT INTO footnote_facts(
                ticker, period_end, fiscal_period_type, filing_doc_id,
                footnote_number, fact_type, description_md,
                amount, currency, unit, due_period, counterparty,
                status, source_excerpt, extracted_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                upper,
                resolved_period_end,
                "FY",
                filing_doc_id,
                footnote_num or None,
                row.fact_type,
                row.description_md.strip(),
                float(row.amount) if row.amount is not None else None,
                (row.currency or "USD")[:3] if row.amount is not None or row.currency else None,
                row.unit[:16] if row.unit else None,
                due_iso,
                row.counterparty[:255] if row.counterparty else None,
                (row.status or "active")[:24],
                excerpt,
                now_iso,
            ),
        )
        inserted += 1

    conn.commit()
    return inserted
