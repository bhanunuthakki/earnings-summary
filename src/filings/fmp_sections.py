"""Partition a cached FMP ``financial-reports-json`` payload into sections.

This is the ``fmp_rfile`` half of the store: the filer's own XBRL R-file
sections (statements, footnotes, and their ``(Tables)`` / ``(Details)``
renderings), already partitioned by the filer. 673 annual payloads across 89
tickers are on disk today, so this half backfills with zero network calls —
which is why it is the cheaper of the two partitions to stand up first.

Three properties of the payload shape every consumer here has to respect,
measured in docs/design/filing_longitudinal_language.md §1.3:

* **Keys truncate at ~31 characters** and collide, which FMP resolves with an
  order-dependent ``_N`` suffix. Both facts are handled in
  ``models.normalize_stem`` / ``FilingSection.build``; nothing here compares
  raw keys.
* **The filename lies about the form.** ``NU_form_10k_2022.json`` declares
  ``Document Type: 20-F`` in its cover section. The declared type wins, always
  — see ``extract_meta``.
* **The payload is a bare dict of unvalidated JSON.** Every access goes
  through a shape check that raises ``SourceContractError`` on drift rather
  than coercing, per AGENTS.md's schema-drift rule.

Rows are rendered to text as ``label: v1 | v2 | v3`` rather than JSON (which
is what ``compute.segment_crosstabs_llm`` does for its prompt payloads)
because this text is stored for *diffing*: a JSON rendering makes every diff
hunk carry quoting and separator noise, and reorders nothing usefully.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import cast

from filings.models import (
    FilingForm,
    FiscalPeriod,
    SourceContractError,
)

log = logging.getLogger(__name__)

EXTRACTOR_VERSION = "fmp_rfile_v1"

#: Payload keys that are metadata, not sections.
_META_KEYS = frozenset({"symbol", "period", "year"})
#: Cover-section titles seen across filers and years.
_COVER_HINTS = ("cover", "document and entity information", "document and entity info")
#: Cap per rendered section. Detail sections are tabular and bounded in
#: practice; this only guards a pathological payload.
MAX_SECTION_CHARS = 400_000
MIN_SECTION_CHARS = 1

_NBSP = "\xa0"
_WS_RX = re.compile(r"\s+")
_MONTHS = {
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "may": 5,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "sept": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}
_DATE_RX = re.compile(r"([A-Za-z]{3,4})\.?\s+(\d{1,2}),?\s+(\d{4})")
_ISO_DATE_RX = re.compile(r"(\d{4})-(\d{2})-(\d{2})")

_FORM_ALIASES: dict[str, FilingForm] = {
    "10-K": FilingForm.FORM_10K,
    "10-K/A": FilingForm.FORM_10K,
    "10-Q": FilingForm.FORM_10Q,
    "10-Q/A": FilingForm.FORM_10Q,
    "20-F": FilingForm.FORM_20F,
    "20-F/A": FilingForm.FORM_20F,
    "40-F": FilingForm.FORM_40F,
    "40-F/A": FilingForm.FORM_40F,
    "6-K": FilingForm.FORM_6K,
    "S-1": FilingForm.FORM_S1,
    "S-1/A": FilingForm.FORM_S1,
}


@dataclass(slots=True)
class FmpFilingMeta:
    """What the payload says about itself, as opposed to what its name says."""

    symbol: str | None = None
    declared_form: FilingForm | None = None
    declared_form_raw: str | None = None
    fiscal_period: FiscalPeriod | None = None
    fiscal_year: int | None = None
    period_end: datetime | None = None
    warnings: list[str] = field(default_factory=list[str])


@dataclass(slots=True)
class FmpSectionSlice:
    key: str
    text: str
    ordinal: int
    row_count: int


@dataclass(slots=True)
class FmpParseResult:
    meta: FmpFilingMeta
    slices: list[FmpSectionSlice] = field(default_factory=list[FmpSectionSlice])
    warnings: list[str] = field(default_factory=list[str])


def load_payload(path: Path) -> dict[str, object]:
    """Read and shape-check one cached payload.

    Raises ``SourceContractError`` — never returns a partially-understood
    payload — when the file is unreadable, not JSON, or not the
    ``{"symbol": ..., "<section>": [...]}`` object this module parses. The
    caller dumps the offending file reference into coverage as
    ``SCHEMA_DRIFT`` instead of guessing at the new shape in-loop.
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SourceContractError(f"unreadable FMP payload {path}: {exc}") from exc
    try:
        parsed: object = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SourceContractError(f"invalid JSON in FMP payload {path}: {exc}") from exc
    if not isinstance(parsed, dict):
        raise SourceContractError(
            f"FMP payload {path} is a {type(parsed).__name__}, expected a JSON object"
        )
    return cast("dict[str, object]", parsed)


def _clean(value: object) -> str:
    if isinstance(value, str):
        return _WS_RX.sub(" ", value.replace(_NBSP, " ")).strip()
    if value is None:
        return ""
    return str(value)


def _parse_date(text: str) -> datetime | None:
    """Parse FMP's cover-page date renderings ("Dec. 31,  2025", ISO).

    Returns None rather than raising: a missing period-end is a degraded but
    workable row (fiscal year still resolves from the payload's own ``year``),
    whereas a raised error would drop an otherwise-good filing.
    """
    cleaned = _clean(text)
    iso = _ISO_DATE_RX.search(cleaned)
    if iso is not None:
        try:
            return datetime(int(iso.group(1)), int(iso.group(2)), int(iso.group(3)))
        except ValueError:
            return None
    m = _DATE_RX.search(cleaned)
    if m is None:
        return None
    month = _MONTHS.get(m.group(1).lower()[:4]) or _MONTHS.get(m.group(1).lower()[:3])
    if month is None:
        return None
    try:
        return datetime(int(m.group(3)), month, int(m.group(2)))
    except ValueError:
        return None


def _iter_rows(section: object) -> list[tuple[str, list[str]]]:
    """Normalize one section's payload into (label, values) rows.

    Tolerates the two shapes seen in the cache: a list of single-key dicts
    (the norm) and a bare list of scalars (rare, empty sections). Anything
    else yields no rows, which surfaces as an empty section rather than a
    crash — the section's *absence* is then visible in coverage.
    """
    rows: list[tuple[str, list[str]]] = []
    if not isinstance(section, list):
        return rows
    for item in cast("list[object]", section):
        if isinstance(item, dict):
            for label, values in cast("dict[str, object]", item).items():
                if isinstance(values, list):
                    rows.append((_clean(label), [_clean(v) for v in cast("list[object]", values)]))
                else:
                    rows.append((_clean(label), [_clean(values)]))
        elif isinstance(item, (str, int, float)):
            rows.append((_clean(item), []))
    return rows


def _render_section(rows: list[tuple[str, list[str]]]) -> str:
    """Render rows as diff-stable text: one ``label: v1 | v2`` line per row."""
    lines: list[str] = []
    for label, values in rows:
        rendered = " | ".join(v for v in values if v)
        lines.append(f"{label}: {rendered}" if rendered else label)
    return "\n".join(lines).strip()


def _cover_lookup(rows: list[tuple[str, list[str]]], label: str) -> str | None:
    """First non-empty value for a cover-page label (case-insensitive)."""
    wanted = label.lower()
    for row_label, values in rows:
        if row_label.lower() == wanted:
            for v in values:
                if v:
                    return v
    return None


def extract_meta(payload: dict[str, object]) -> FmpFilingMeta:
    """Read the filing's self-declared identity from its cover section.

    The declared ``Document Type`` is authoritative over the filename and over
    ``tracked_companies.filing_regime``: the audit found NU's payloads named
    ``form_10k`` while declaring ``20-F``, and a partition labeled with the
    wrong form would align 20-F items against 10-K items downstream. When the
    cover section is absent or declares nothing, the form is left ``None`` and
    the caller decides (it has the filename and the DB regime as fallbacks,
    both recorded as such).
    """
    meta = FmpFilingMeta(symbol=_clean(payload.get("symbol")) or None)

    raw_year = payload.get("year")
    if isinstance(raw_year, (str, int)):
        try:
            year = int(str(raw_year).strip()[:4])
            if 1990 <= year <= 2100:
                meta.fiscal_year = year
        except ValueError:
            meta.warnings.append("unparseable_year_field")

    raw_period = _clean(payload.get("period")).upper()
    if raw_period in {p.value for p in FiscalPeriod}:
        meta.fiscal_period = FiscalPeriod(raw_period)

    cover_rows: list[tuple[str, list[str]]] = []
    for key, value in payload.items():
        if key in _META_KEYS:
            continue
        if any(hint in key.lower() for hint in _COVER_HINTS):
            cover_rows = _iter_rows(value)
            break
    if not cover_rows:
        meta.warnings.append("no_cover_section")
        return meta

    declared = _cover_lookup(cover_rows, "Document Type")
    if declared:
        meta.declared_form_raw = declared
        resolved = _FORM_ALIASES.get(declared.upper())
        if resolved is None:
            meta.warnings.append(f"unrecognized_document_type:{declared[:32]}")
        else:
            meta.declared_form = resolved

    focus = _cover_lookup(cover_rows, "Document Fiscal Period Focus")
    if focus and focus.upper() in {p.value for p in FiscalPeriod}:
        meta.fiscal_period = FiscalPeriod(focus.upper())

    fy_focus = _cover_lookup(cover_rows, "Document Fiscal Year Focus")
    if fy_focus:
        try:
            fy = int(fy_focus.strip()[:4])
            if 1990 <= fy <= 2100:
                meta.fiscal_year = fy
        except ValueError:
            pass

    period_end_raw = _cover_lookup(cover_rows, "Document Period End Date")
    if period_end_raw:
        parsed = _parse_date(period_end_raw)
        if parsed is None:
            meta.warnings.append("unparseable_period_end")
        else:
            meta.period_end = parsed

    return meta


def parse_payload(path: Path) -> FmpParseResult:
    """Parse one cached payload into meta + rendered section slices.

    Raises ``SourceContractError`` when the file cannot be understood at all.
    An understood-but-empty payload returns a result with no slices and a
    ``no_sections`` warning, which the caller records as ``NO_SECTIONS_FOUND``
    — distinct from drift, and distinct from never having looked.
    """
    payload = load_payload(path)
    meta = extract_meta(payload)
    slices: list[FmpSectionSlice] = []
    warnings: list[str] = list(meta.warnings)
    empty_sections = 0

    for key, value in payload.items():
        if key in _META_KEYS:
            continue
        rows = _iter_rows(value)
        if not rows:
            empty_sections += 1
            continue
        text = _render_section(rows)
        if len(text) < MIN_SECTION_CHARS:
            empty_sections += 1
            continue
        slices.append(
            FmpSectionSlice(
                key=_clean(key)[:255],
                text=text[:MAX_SECTION_CHARS],
                ordinal=len(slices),
                row_count=len(rows),
            )
        )

    if not slices:
        warnings.append("no_sections")
    elif empty_sections:
        warnings.append(f"empty_sections:{empty_sections}")
    return FmpParseResult(meta=meta, slices=slices, warnings=warnings)


_FILENAME_RX = re.compile(
    r"^(?P<ticker>[A-Za-z.\-]+)_form_(?P<form>10k|10q)_(?P<year>\d{4})(?:_(?P<quarter>Q[1-4]))?\.json$",
    re.IGNORECASE,
)


@dataclass(slots=True)
class FmpFilenameHint:
    """What the cached filename claims — a hint, never the authority."""

    ticker: str
    form: FilingForm
    fiscal_year: int
    fiscal_period: FiscalPeriod


def parse_filename(path: Path) -> FmpFilenameHint | None:
    """Decode ``{TICKER}_form_10k_{YEAR}.json`` / ``..._form_10q_{Y}_{Q}.json``.

    Returns None for anything that isn't a filing payload (the FMP cache holds
    ~110k files, almost all of them endpoint dumps), so callers can glob
    broadly and filter here.
    """
    m = _FILENAME_RX.match(path.name)
    if m is None:
        return None
    form = FilingForm.FORM_10Q if m.group("form").lower() == "10q" else FilingForm.FORM_10K
    quarter = m.group("quarter")
    period = FiscalPeriod(quarter.upper()) if quarter else FiscalPeriod.FY
    return FmpFilenameHint(
        ticker=m.group("ticker").upper(),
        form=form,
        fiscal_year=int(m.group("year")),
        fiscal_period=period,
    )
