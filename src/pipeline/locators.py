"""Single source of truth for building v2 ``FactLocator`` payloads.

Mirrors how ``pipeline.kpi_persistence.persist_manifest`` is already "the
single source of truth" for the KPI insert path (that module's own
docstring) — every extractor that wants a renderable, click-through-able
locator calls into ONE of the ``*_locator`` helpers here rather than
constructing ``FactLocator(kind=..., table_cell=...)`` by hand. Each helper
stamps ``locator_version=2`` and the matching ``kind`` automatically, so an
extractor cannot accidentally emit a v2-shaped payload without the
discriminant the peek dispatch (``FactLocator.effective_kind()``) relies on.

See docs/design/provenance_clickthrough.md §3.1 for the design this module
implements, and §1 for the schema the helpers build.
"""

from __future__ import annotations

import logging
import re
import sqlite3

from pydantic import BaseModel, Field

from models.facts import (
    DerivedRef,
    FactLocator,
    HtmlSpanRef,
    LocatorKind,
    TableCellRef,
    TranscriptSpanRef,
    VendorFieldRef,
)
from models.validation import Severity, ValidationRule
from pipeline.kpi_persistence import record_validation_issue

log = logging.getLogger(__name__)


class LegacyEscapeHatch(BaseModel):
    """Explicit, grep-able opt-out for a writer that genuinely cannot produce
    a renderable locator yet. Requires a reason — "I forgot" is not
    machine-checkable, but a reviewer can grep ``LegacyEscapeHatch(`` in a
    diff and ask why. Every use logged (``log_escape_hatch``,
    rule=LOCATOR_ESCAPE_HATCH) so the coverage audit
    (execution/provenance_coverage_report.py) can count them.
    """

    reason: str = Field(min_length=8)


def log_escape_hatch(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    source_doc_id: int | None,
    ticker: str | None,
    escape_hatch: LegacyEscapeHatch,
) -> None:
    """Record one ``LegacyEscapeHatch`` use as a validation_issues row.

    Best-effort — a missing validation_issues table (a minimal test fixture)
    must never break the write path this is guarding. Severity is WARN (this
    repo's only non-HALT level); the value being written is real and was
    never in question, only its provenance render-ability.
    """
    try:
        record_validation_issue(
            conn,
            run_id=run_id,
            source_doc_id=source_doc_id,
            ticker=ticker,
            severity=Severity.WARN,
            rule=ValidationRule.LOCATOR_ESCAPE_HATCH,
            raw_value=None,
            expected=escape_hatch.reason,
        )
    except sqlite3.Error:
        log.warning(
            {
                "event": "locator_escape_hatch_log_failed",
                "ticker": ticker,
                "reason": escape_hatch.reason,
            }
        )


def table_cell_locator(
    *,
    section: str | None,
    table_title: str | None = None,
    row_label: str | None,
    row_axis_path: list[str] | None = None,
    column_header: str | None,
    json_path: str | None,
    cell_value_as_extracted: str | None = None,
    verbatim_snippet: str | None = None,
) -> FactLocator:
    """A ``fmp_json_table`` locator: FMP statement-endpoint arrays, FMP
    form-10-K/10-Q section tables, generic_xbrl_capture note tables, segment
    crosstab cells (docs/design/provenance_clickthrough.md §1.5)."""
    cell = TableCellRef(
        section=section,
        table_title=table_title,
        row_label=row_label,
        row_axis_path=list(row_axis_path) if row_axis_path else [],
        column_header=column_header,
        cell_value_as_extracted=cell_value_as_extracted,
        json_path=json_path,
    )
    return FactLocator(
        section=section,
        json_path=json_path,
        locator_version=2,
        kind=LocatorKind.FMP_JSON_TABLE,
        table_cell=cell,
        verbatim_snippet=(verbatim_snippet or cell_value_as_extracted),
    )


def pdf_slide_locator(
    *,
    pdf_page: int,
    verbatim_snippet: str,
    bbox: tuple[float, float, float, float] | None = None,
) -> FactLocator:
    """A ``pdf_slide`` locator (IR-deck / supplement PDF pages, Phase B's
    render target — the locator itself is Phase-A-safe to emit today; a bare
    page number without a bbox is still fully renderable per §1.2's note)."""
    return FactLocator(
        pdf_page=pdf_page,
        locator_version=2,
        kind=LocatorKind.PDF_SLIDE,
        pdf_bbox=bbox,
        verbatim_snippet=verbatim_snippet,
    )


def transcript_span_locator(
    *,
    transcript_line: int,
    verbatim_snippet: str,
    speaker_turn_index: int | None = None,
    char_start: int | None = None,
    char_end: int | None = None,
) -> FactLocator:
    """A ``transcript_span`` locator (earnings-call / IR transcript quotes)."""
    return FactLocator(
        transcript_line=transcript_line,
        locator_version=2,
        kind=LocatorKind.TRANSCRIPT_SPAN,
        transcript_span=TranscriptSpanRef(
            transcript_line=transcript_line,
            speaker_turn_index=speaker_turn_index,
            char_start=char_start,
            char_end=char_end,
        ),
        verbatim_snippet=verbatim_snippet,
    )


def html_span_locator(
    *,
    doc_id: int | None,
    quote: str,
    char_start: int | None = None,
    char_end: int | None = None,
) -> FactLocator:
    """An ``html_span`` locator (SEC EDGAR HTML read directly, 8-K exhibit
    HTML — Phase C's primary consumer; included here so the schema and the
    escape-hatch/coverage plumbing are ready when that pipeline lands it)."""
    return FactLocator(
        locator_version=2,
        kind=LocatorKind.HTML_SPAN,
        html_span=HtmlSpanRef(doc_id=doc_id, char_start=char_start, char_end=char_end, quote=quote),
        verbatim_snippet=quote,
    )


def vendor_field_locator(*, endpoint: str, field: str, period: str | None = None) -> FactLocator:
    """A ``vendor_field`` locator — the honest floor for a plain vendor
    endpoint value with no underlying filing (a live quote, a market-cap
    snapshot)."""
    return FactLocator(
        locator_version=2,
        kind=LocatorKind.VENDOR_FIELD,
        vendor_field=VendorFieldRef(endpoint=endpoint, field=field, period=period),
    )


def derived_locator(*, derived: DerivedRef, verbatim_snippet: str | None = None) -> FactLocator:
    """A ``derived`` locator wrapping a formula + its inputs (the typed
    promotion of ``kpi_facts.computed_from``; see §1.2/§3.2)."""
    return FactLocator(
        locator_version=2,
        kind=LocatorKind.DERIVED,
        derived=derived,
        verbatim_snippet=verbatim_snippet,
    )


_WS_RX = re.compile(r"\s+")


def _normalize(text: str) -> str:
    return _WS_RX.sub(" ", text).strip().casefold()


def verify_quote_in_source(quote: str, source_text: str) -> bool:
    """Whitespace/case-normalized substring check — the anti-hallucination
    gate for LLM-returned anchors (Phase C's §3.3). False means: reject the
    locator, do not persist a fabricated anchor. Deliberately a simple
    substring check, not fuzzy matching — a locator that can't be found
    verbatim isn't something the peek can highlight reliably; a fuzzy "close
    enough" quote is worse than an honest legacy fallback."""
    if not quote.strip():
        return False
    return _normalize(quote) in _normalize(source_text)
