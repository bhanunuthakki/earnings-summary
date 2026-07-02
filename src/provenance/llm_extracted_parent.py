"""Resolve the primary document an ``llm_extracted`` documents row was read from.

Per ``directives/data_provenance.md`` §2/§4: every ``llm_extracted`` documents row
must carry ``parent_document_id``. ``compute/kpi_extract_summaries.py``'s
``_ensure_summary_document_row`` historically inserted ``llm_summary`` /
``ir_press_release_synthesized`` / ``ir_presentation_synthesized`` rows without
one. This module is the single derivation used both to backfill the historical
orphans (``execution/backfill_llm_extracted_parents.py``) and to resolve the
parent at insert time going forward (wired into ``_ensure_summary_document_row``
via ``pipeline.kpi_persistence.guard_llm_extracted_parent`` as the backstop).

Matching key: ``(ticker, period_end date, one of the doc_type's allowed parent
(source_type, doc_type) pairs)``. ``execution/process_ir_documents.py`` and
``compute/kpi_extract_summaries.py`` both early-return when the target
``.tmp/*.txt`` cache file already exists, so a summary is generated exactly once,
from whichever eligible primary document existed first — when more than one
candidate matches, the earliest ``fetched_at`` is mechanically the one that was
actually read, not a guess.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime

from models.documents import DocType, SourceType

# The three doc_type strings compute/kpi_extract_summaries.py's _SourceSpec
# writes for llm_extracted rows. Not members of DocType (that enum covers
# primary-document kinds only) — kept as literals here, mirroring the source.
_DOC_TYPE_LLM_SUMMARY = "llm_summary"
_DOC_TYPE_IR_PRESS_RELEASE_SYNTH = "ir_press_release_synthesized"
_DOC_TYPE_IR_PRESENTATION_SYNTH = "ir_presentation_synthesized"

_TRANSCRIPT_CANDIDATES: tuple[tuple[str, str], ...] = (
    (SourceType.TRANSCRIPT_AUDIO.value, DocType.EARNINGS_CALL_TRANSCRIPT.value),
    (SourceType.IR_DOC.value, DocType.IR_TRANSCRIPT.value),
)
_INVESTOR_UPDATE_CANDIDATES: tuple[tuple[str, str], ...] = (
    (SourceType.IR_DOC.value, DocType.IR_INVESTOR_UPDATE.value),
)
_SIMPLE_CANDIDATES: dict[str, tuple[tuple[str, str], ...]] = {
    _DOC_TYPE_IR_PRESS_RELEASE_SYNTH: ((SourceType.IR_DOC.value, DocType.IR_PRESS_RELEASE.value),),
    _DOC_TYPE_IR_PRESENTATION_SYNTH: ((SourceType.IR_DOC.value, DocType.IR_PRESENTATION.value),),
}


def _candidate_types(doc_type: str, file_path: str) -> tuple[tuple[str, str], ...]:
    """(source_type, doc_type) pairs eligible as the parent for one orphan row."""
    if doc_type == _DOC_TYPE_LLM_SUMMARY:
        # The investor-update variant shares the plain "_summary.txt" regex
        # (kpi_extract_summaries._EARNINGS_SUMMARY_RX) but is generated from
        # the IR investor-update letter, not an earnings-call transcript.
        if "investor_update_summary" in file_path:
            return _INVESTOR_UPDATE_CANDIDATES
        return _TRANSCRIPT_CANDIDATES
    return _SIMPLE_CANDIDATES.get(doc_type, ())


def _parse_fetched_at(raw: object) -> datetime:
    """Robustly order a ``documents.fetched_at`` cell.

    The table has accumulated three storage flavors across writers: naive
    space-separated (``"2026-05-19 01:45:16.308875"``), naive "T"-separated
    (``"2026-05-06T23:05:46.997018"``), and "T"-separated with a ``+00:00``
    offset. A plain string comparison sorts these wrong whenever two rows share
    a calendar date but differ in separator (``' ' < 'T'`` in ASCII, independent
    of the actual time of day) — so callers MUST parse to real ``datetime``
    values before ordering. Aware timestamps are normalized to naive per the
    repo's naive-UTC storage convention (mixing aware/naive breaks comparison).
    Unparseable input sorts first (``datetime.min``) — a data anomaly, not
    expected in practice, but never worth crashing a backfill/insert over.
    """
    if isinstance(raw, datetime):
        dt = raw
    else:
        try:
            dt = datetime.fromisoformat(str(raw).strip())
        except ValueError:
            return datetime.min
    return dt.replace(tzinfo=None) if dt.tzinfo is not None else dt


@dataclass(frozen=True, slots=True)
class ParentMatch:
    parent_document_id: int
    confidence: str  # "unique" | "earliest_of_<N>"
    candidate_count: int


def resolve_parent(
    conn: sqlite3.Connection,
    *,
    ticker: str,
    doc_type: str,
    file_path: str,
    period_end: datetime | str | None,
) -> ParentMatch | None:
    """Best-effort primary-document match for one llm_extracted row.

    Returns ``None`` when no candidate parent exists — callers must flag that
    row for human review, not guess.
    """
    types = _candidate_types(doc_type, file_path)
    if not types or period_end is None:
        return None
    period_prefix = str(period_end)[:10]  # "YYYY-MM-DD", separator-agnostic

    candidates: list[tuple[int, object]] = []
    for source_type, parent_doc_type in types:
        rows = conn.execute(
            "SELECT id, fetched_at FROM documents "
            "WHERE ticker = ? AND source_type = ? AND doc_type = ? "
            "AND substr(period_end, 1, 10) = ?",
            (ticker.upper(), source_type, parent_doc_type, period_prefix),
        ).fetchall()
        candidates.extend((int(r["id"]), r["fetched_at"]) for r in rows)

    if not candidates:
        return None
    candidates.sort(key=lambda c: (_parse_fetched_at(c[1]), c[0]))
    winner_id = candidates[0][0]
    confidence = "unique" if len(candidates) == 1 else f"earliest_of_{len(candidates)}"
    return ParentMatch(
        parent_document_id=winner_id, confidence=confidence, candidate_count=len(candidates)
    )
