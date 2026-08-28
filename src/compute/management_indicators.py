"""Durable staging for novel or one-off management indicators.

These are transcript-observed measurements that are useful research evidence
but do not yet have reviewed KPI semantics.  They deliberately never write to
``kpi_facts``: a reviewer must promote the concept through the KPI workflow
separately before it can affect a canonical series.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, Field

from models.facts import Unit


class IndicatorScope(StrEnum):
    CONSOLIDATED = "consolidated"
    SEGMENT = "segment"
    PRODUCT = "product"
    GEOGRAPHY = "geography"
    UNSPECIFIED = "unspecified"


class IndicatorRecurrence(StrEnum):
    RECURRING = "recurring"
    ONE_OFF = "one_off"
    UNKNOWN = "unknown"


class IndicatorPromotionStatus(StrEnum):
    PENDING_REVIEW = "pending_review"
    PROMOTED = "promoted"
    REJECTED = "rejected"


class ManagementIndicatorSchemaError(RuntimeError):
    """The required staging migration is absent, so observations cannot be retained."""


class ManagementIndicatorInput(BaseModel):
    """One source-bound, not-yet-canonical management measurement."""

    ticker: str = Field(min_length=1, max_length=16)
    transcript_segment_id: int = Field(gt=0)
    raw_label: str = Field(min_length=1, max_length=256)
    value: Decimal
    unit: Unit
    scope: IndicatorScope = IndicatorScope.UNSPECIFIED
    recurrence: IndicatorRecurrence = IndicatorRecurrence.UNKNOWN
    speaker: str | None = Field(default=None, max_length=256)
    source_excerpt: str = Field(min_length=1, max_length=2000)


class ManagementIndicatorExtractionManifest(BaseModel):
    """The unpromoted indicator portion of a transcript extraction result."""

    indicators: list[ManagementIndicatorInput] = Field(
        default_factory=list[ManagementIndicatorInput]
    )


@dataclass(frozen=True)
class _ResolvedIndicatorSource:
    source_doc_id: int
    transcript_id: int
    transcript_segment_id: int
    segment_sequence: int
    speaker: str | None
    time_code_start: str | None


def _table_available(conn: sqlite3.Connection) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='management_indicator_observations'"
        ).fetchone()
        is not None
    )


def _normalized_source_text(value: str) -> str:
    return " ".join(value.split())


def _resolve_indicator_source(
    conn: sqlite3.Connection,
    *,
    anchor_segment_id: int,
    ticker: str,
    source_excerpt: str,
) -> _ResolvedIndicatorSource:
    """Bind an extracted excerpt to exactly one segment of the named issuer's transcript."""
    columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(transcript_segments)")}
    speaker = "ts.speaker" if "speaker" in columns else "NULL"
    time_code = "ts.time_code_start" if "time_code_start" in columns else "NULL"
    anchor = conn.execute(
        "SELECT tr.document_id, tr.id, tr.ticker, d.ticker "
        "FROM transcript_segments ts JOIN transcripts tr ON tr.id=ts.transcript_id "
        "JOIN documents d ON d.id=tr.document_id "
        "WHERE ts.id=?",
        (anchor_segment_id,),
    ).fetchone()
    if anchor is None or anchor[0] is None:
        raise ValueError(f"transcript_segment_id={anchor_segment_id} has no source document")
    expected_ticker = ticker.strip().upper()
    transcript_ticker = str(anchor[2]).strip().upper()
    document_ticker = str(anchor[3]).strip().upper()
    if expected_ticker != transcript_ticker or expected_ticker != document_ticker:
        raise ValueError(
            f"indicator ticker={expected_ticker} does not match transcript/document ticker "
            f"{transcript_ticker}/{document_ticker}"
        )
    rows = conn.execute(
        "SELECT ts.id, ts.seq, " + speaker + ", " + time_code + ", ts.text "
        "FROM transcript_segments ts WHERE ts.transcript_id=? ORDER BY ts.seq, ts.id",
        (int(anchor[1]),),
    ).fetchall()
    excerpt = _normalized_source_text(source_excerpt)
    matches = [row for row in rows if excerpt in _normalized_source_text(str(row[4]))]
    if len(matches) != 1:
        raise ValueError(
            "management indicator source excerpt must match exactly one transcript segment; "
            f"matched={len(matches)}"
        )
    match = matches[0]
    return _ResolvedIndicatorSource(
        source_doc_id=int(anchor[0]),
        transcript_id=int(anchor[1]),
        transcript_segment_id=int(match[0]),
        segment_sequence=int(match[1]),
        speaker=_optional_text(match[2]),
        time_code_start=_optional_text(match[3]),
    )


def _optional_text(value: object) -> str | None:
    text = str(value).strip() if value is not None else ""
    return text or None


def _canonical_payload(
    indicator: ManagementIndicatorInput,
    *,
    source_doc_id: int,
    transcript_id: int,
    transcript_segment_id: int,
    segment_sequence: int,
    speaker: str | None,
    time_code_start: str | None,
) -> tuple[str, str]:
    locator = {
        "document_id": source_doc_id,
        "transcript_id": transcript_id,
        "transcript_segment_id": transcript_segment_id,
        "segment_sequence": segment_sequence,
        "time_code_start": time_code_start,
    }
    payload = {
        "ticker": indicator.ticker.upper(),
        "raw_label": indicator.raw_label,
        "value": str(indicator.value),
        "unit": indicator.unit.value,
        "scope": indicator.scope.value,
        "recurrence": indicator.recurrence.value,
        "speaker": speaker,
        "source_excerpt": indicator.source_excerpt,
        "source_locator": locator,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest(), json.dumps(
        locator, sort_keys=True, separators=(",", ":")
    )


def persist_indicator(conn: sqlite3.Connection, *, indicator: ManagementIndicatorInput) -> int:
    """Persist one pending-review indicator, or return its idempotent replay id.

    The staging schema is mandatory: silently discarding a novel observation
    would violate its source-preservation contract. Callers must apply
    migration 0031 before attempting a persistence run.
    """
    if not _table_available(conn):
        raise ManagementIndicatorSchemaError(
            "management_indicator_observations is missing; apply migration "
            "0031_add_management_indicator_observations before persisting novel indicators"
        )
    source = _resolve_indicator_source(
        conn,
        anchor_segment_id=indicator.transcript_segment_id,
        ticker=indicator.ticker,
        source_excerpt=indicator.source_excerpt,
    )
    if indicator.speaker is not None and indicator.speaker.strip() != (source.speaker or ""):
        raise ValueError("indicator speaker does not match the source transcript segment")
    idempotency_key, locator_json = _canonical_payload(
        indicator,
        source_doc_id=source.source_doc_id,
        transcript_id=source.transcript_id,
        transcript_segment_id=source.transcript_segment_id,
        segment_sequence=source.segment_sequence,
        speaker=source.speaker,
        time_code_start=source.time_code_start,
    )
    existing = conn.execute(
        "SELECT id FROM management_indicator_observations WHERE idempotency_key=?",
        (idempotency_key,),
    ).fetchone()
    if existing is not None:
        return int(existing[0])
    cur = conn.execute(
        "INSERT INTO management_indicator_observations "
        "(idempotency_key,ticker,transcript_segment_id,source_doc_id,raw_label,value,unit,scope,"
        "speaker,source_excerpt,source_locator_json,recurrence,promotion_status) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            idempotency_key,
            indicator.ticker.upper(),
            source.transcript_segment_id,
            source.source_doc_id,
            indicator.raw_label,
            str(indicator.value),
            indicator.unit.value,
            indicator.scope.value,
            source.speaker,
            indicator.source_excerpt,
            locator_json,
            indicator.recurrence.value,
            IndicatorPromotionStatus.PENDING_REVIEW.value,
        ),
    )
    if cur.lastrowid is None:
        raise RuntimeError("indicator insert did not return a row id")
    return int(cur.lastrowid)


def persist_indicators(
    conn: sqlite3.Connection, manifest: ManagementIndicatorExtractionManifest
) -> list[int]:
    """Persist all staging inputs in order; caller owns the surrounding transaction."""
    ids: list[int] = []
    for indicator in manifest.indicators:
        ids.append(persist_indicator(conn, indicator=indicator))
    return ids


def mark_indicator_reviewed(
    conn: sqlite3.Connection,
    *,
    indicator_id: int,
    status: IndicatorPromotionStatus,
    reviewed_by: str,
) -> None:
    """Record review state only; promotion never emits a canonical KPI fact."""
    if status is IndicatorPromotionStatus.PENDING_REVIEW:
        raise ValueError("review status must resolve pending_review")
    reviewer = reviewed_by.strip()
    if not reviewer:
        raise ValueError("reviewed_by must be non-empty")
    cur = conn.execute(
        "UPDATE management_indicator_observations SET promotion_status=?, reviewed_at=?, "
        "reviewed_by=? WHERE id=?",
        (status.value, datetime.now().isoformat(), reviewer, indicator_id),
    )
    if cur.rowcount != 1:
        raise ValueError(f"management indicator {indicator_id} does not exist")
