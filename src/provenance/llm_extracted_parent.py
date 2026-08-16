"""Resolve the primary document an ``llm_extracted`` documents row was read from.

Legacy ``llm_summary`` rows are repaired only from a deterministic source key:
the summary basename derives one processed transcript basename. A candidate
must share the ticker, have the transcript-audio / earnings-call type pair, and
have that exact basename. Zero or multiple candidates are unresolved rather
than guessed from fiscal-period heuristics. The matched parent's period is the
canonical period for the summary and its dependent KPI facts.

Investor-update summaries use the canonical IR-document path as their source
identity: ticker plus the already-stored child period must match one
``ir_investor_update`` document at
``ir_documents/<ticker>/<period>/ir_investor_update__<sha8>.<extension>``.
The cache's Q/year label is never converted into a fiscal date.

The two IR synthesized document kinds retain their direct ticker-and-period
matching because their filenames do not encode a processed transcript.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime

from models.documents import DocType, SourceType

_DOC_TYPE_LLM_SUMMARY = "llm_summary"
_DOC_TYPE_IR_PRESS_RELEASE_SYNTH = "ir_press_release_synthesized"
_DOC_TYPE_IR_PRESENTATION_SYNTH = "ir_presentation_synthesized"

_TRANSCRIPT_CANDIDATE = (
    SourceType.TRANSCRIPT_AUDIO.value,
    DocType.EARNINGS_CALL_TRANSCRIPT.value,
)
_INVESTOR_UPDATE_CANDIDATE = (SourceType.IR_DOC.value, DocType.IR_INVESTOR_UPDATE.value)
_SIMPLE_CANDIDATES: dict[str, tuple[tuple[str, str], ...]] = {
    _DOC_TYPE_IR_PRESS_RELEASE_SYNTH: ((SourceType.IR_DOC.value, DocType.IR_PRESS_RELEASE.value),),
    _DOC_TYPE_IR_PRESENTATION_SYNTH: ((SourceType.IR_DOC.value, DocType.IR_PRESENTATION.value),),
}
_LLM_SUMMARY_BASENAME_RX = re.compile(
    r"^(?P<transcript>[A-Z][A-Z0-9.]*_Q[1-4]_20\d{2})_summary\.txt$"
)
_INVESTOR_UPDATE_SUMMARY_BASENAME_RX = re.compile(
    r"^[A-Z][A-Z0-9.]*_Q[1-4]_20\d{2}_investor_update_summary\.txt$"
)
_CANONICAL_INVESTOR_UPDATE_PATH_RX = re.compile(
    r"(?:^|.*/)ir_documents/(?P<ticker>[A-Z][A-Z0-9.]*)/"
    r"(?P<period>20\d{2}-\d{2}-\d{2})/ir_investor_update__[0-9a-fA-F]{8}\.[A-Za-z0-9]+$"
)


def _parse_period_end(raw: object) -> datetime | None:
    """Normalize a stored period stamp without inventing a replacement date."""
    if isinstance(raw, datetime):
        parsed = raw
    else:
        try:
            parsed = datetime.fromisoformat(str(raw).strip())
        except ValueError:
            return None
    return parsed.replace(tzinfo=None) if parsed.tzinfo is not None else parsed


def _derived_transcript_basename(summary_file_path: str) -> str | None:
    """Return the exact processed-transcript basename encoded by a summary."""
    basename = summary_file_path.replace("\\", "/").rsplit("/", maxsplit=1)[-1]
    match = _LLM_SUMMARY_BASENAME_RX.fullmatch(basename)
    if match is None:
        return None
    return f"{match.group('transcript')}.txt"


def _is_investor_update_summary(summary_file_path: str) -> bool:
    """Identify the distinct investor-update cache shape without deriving a date."""
    basename = summary_file_path.replace("\\", "/").rsplit("/", maxsplit=1)[-1]
    return _INVESTOR_UPDATE_SUMMARY_BASENAME_RX.fullmatch(basename) is not None


def _document_basename(file_path: object) -> str:
    return str(file_path or "").replace("\\", "/").rsplit("/", maxsplit=1)[-1]


@dataclass(frozen=True, slots=True)
class ParentMatch:
    parent_document_id: int
    parent_period_end: datetime
    confidence: str  # "unique"
    candidate_count: int


def _resolve_exact_basename_parent(
    conn: sqlite3.Connection,
    *,
    ticker: str,
    source_type: str,
    parent_doc_type: str,
    expected_name: str,
) -> ParentMatch | None:
    """Resolve one primary document only when its source key is unique."""
    rows = conn.execute(
        "SELECT id, period_end, file_path FROM documents "
        "WHERE UPPER(ticker) = ? AND source_type = ? AND doc_type = ?",
        (ticker.upper(), source_type, parent_doc_type),
    ).fetchall()
    candidates = [row for row in rows if _document_basename(row["file_path"]) == expected_name]
    if len(candidates) != 1:
        return None
    candidate = candidates[0]
    parent_period_end = _parse_period_end(candidate["period_end"])
    if parent_period_end is None:
        return None
    return ParentMatch(
        parent_document_id=int(candidate["id"]),
        parent_period_end=parent_period_end,
        confidence="unique",
        candidate_count=1,
    )


def _resolve_llm_summary_parent(
    conn: sqlite3.Connection,
    *,
    ticker: str,
    file_path: str,
    period_end: datetime | str | None,
) -> ParentMatch | None:
    """Resolve a transcript or investor-update summary via its own strict route."""
    if _is_investor_update_summary(file_path):
        return _resolve_investor_update_parent(conn, ticker=ticker, period_end=period_end)
    transcript_basename = _derived_transcript_basename(file_path)
    if transcript_basename is None:
        return None
    source_type, parent_doc_type = _TRANSCRIPT_CANDIDATE
    return _resolve_exact_basename_parent(
        conn,
        ticker=ticker,
        source_type=source_type,
        parent_doc_type=parent_doc_type,
        expected_name=transcript_basename,
    )


def _resolve_investor_update_parent(
    conn: sqlite3.Connection, *, ticker: str, period_end: datetime | str | None
) -> ParentMatch | None:
    """Resolve one investor update by canonical source path and stored period."""
    requested_period_end = _parse_period_end(period_end)
    if requested_period_end is None:
        return None
    requested_period = requested_period_end.date().isoformat()
    source_type, parent_doc_type = _INVESTOR_UPDATE_CANDIDATE
    rows = conn.execute(
        "SELECT id, period_end, file_path FROM documents "
        "WHERE UPPER(ticker) = ? AND source_type = ? AND doc_type = ? "
        "AND substr(period_end, 1, 10) = ?",
        (ticker.upper(), source_type, parent_doc_type, requested_period),
    ).fetchall()
    candidates: list[sqlite3.Row] = []
    for row in rows:
        match = _CANONICAL_INVESTOR_UPDATE_PATH_RX.fullmatch(
            str(row["file_path"] or "").replace("\\", "/")
        )
        if (
            match is not None
            and match.group("ticker") == ticker.upper()
            and match.group("period") == requested_period
        ):
            candidates.append(row)
    if len(candidates) != 1:
        return None
    candidate = candidates[0]
    parent_period_end = _parse_period_end(candidate["period_end"])
    if parent_period_end is None or parent_period_end.date().isoformat() != requested_period:
        return None
    return ParentMatch(
        parent_document_id=int(candidate["id"]),
        parent_period_end=parent_period_end,
        confidence="unique",
        candidate_count=1,
    )


def resolve_parent(
    conn: sqlite3.Connection,
    *,
    ticker: str,
    doc_type: str,
    file_path: str,
    period_end: datetime | str | None,
) -> ParentMatch | None:
    """Return a deterministic parent match, or ``None`` when resolution is unsafe."""
    if doc_type == _DOC_TYPE_LLM_SUMMARY:
        return _resolve_llm_summary_parent(
            conn, ticker=ticker, file_path=file_path, period_end=period_end
        )

    candidate_types = _SIMPLE_CANDIDATES.get(doc_type, ())
    if not candidate_types or period_end is None:
        return None
    period_prefix = str(period_end)[:10]
    candidates: list[sqlite3.Row] = []
    for source_type, parent_doc_type in candidate_types:
        candidates.extend(
            conn.execute(
                "SELECT id, period_end FROM documents "
                "WHERE UPPER(ticker) = ? AND source_type = ? AND doc_type = ? "
                "AND substr(period_end, 1, 10) = ?",
                (ticker.upper(), source_type, parent_doc_type, period_prefix),
            ).fetchall()
        )
    if len(candidates) != 1:
        return None
    candidate = candidates[0]
    parent_period_end = _parse_period_end(candidate["period_end"])
    if parent_period_end is None:
        return None
    return ParentMatch(
        parent_document_id=int(candidate["id"]),
        parent_period_end=parent_period_end,
        confidence="unique",
        candidate_count=1,
    )
