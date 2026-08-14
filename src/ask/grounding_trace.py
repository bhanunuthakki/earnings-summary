"""Durable, content-addressed traces for the operational Ask retrieval path.

The strict sealed retrieval plane already owns its own replayable trace tables.
This module records the lighter SQL-fact and lexical-document path used by the
interactive product. It stores locators and content digests, not question text
or retrieved passages, so the audit trail does not duplicate prompt material.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit, urlunsplit

from pydantic import BaseModel, ConfigDict, Field

from ask.grounding import EvidenceItem
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite
from viewspec.engine import ViewResult

GroundingRoute = Literal["data", "narrative"]
GroundingOutcome = Literal["ready", "no_evidence", "retrieval_error"]
GroundingStrategy = Literal["sql_viewspec", "sql_facts_and_lexical_documents"]


class GroundingTraceError(RuntimeError):
    """A required operational retrieval trace could not be recorded."""


class _Closed(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class GroundingTraceItem(_Closed):
    ordinal: int = Field(ge=1)
    kind: str = Field(min_length=1, max_length=64)
    ticker: str | None = None
    metric_ref: str | None = None
    fact_ref: str | None = None
    period: str | None = None
    value: str | None = None
    unit: str | None = None
    source_doc_id: int | None = Field(default=None, ge=1)
    fact_id: int | None = Field(default=None, ge=1)
    fact_table: str | None = None
    href: str | None = None
    source_url: str | None = None
    evidence_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class GroundingTrace(_Closed):
    trace_id: str = Field(pattern=r"^ask-grounding:[0-9a-f]{64}$")
    outcome: GroundingOutcome
    item_count: int = Field(ge=0)


def narrative_trace_items(items: list[EvidenceItem]) -> tuple[GroundingTraceItem, ...]:
    """Reduce prompt evidence to stable audit coordinates and a content digest."""

    return tuple(
        GroundingTraceItem(
            ordinal=ordinal,
            kind=item.kind,
            ticker=item.ticker,
            fact_ref=item.fact_ref,
            period=item.period,
            value=item.value,
            source_doc_id=item.doc_id,
            href=item.href,
            source_url=_trace_source_url(item.source_url),
            evidence_sha256=_sha_text(item.text),
        )
        for ordinal, item in enumerate(items, start=1)
    )


def view_trace_items(result: ViewResult) -> tuple[GroundingTraceItem, ...]:
    """Flatten sourced ViewSpec cells into exact SQL retrieval coordinates."""

    items: list[GroundingTraceItem] = []
    for row in result.rows:
        for period, cell in zip(result.period_labels, row.cells, strict=True):
            source = cell.source
            if cell.raw is None:
                continue
            if source is None:
                raise GroundingTraceError("grounded numeric result is missing source provenance")
            payload = {
                "ticker": row.ticker,
                "metric_ref": row.metric.token(),
                "period": period,
                "value": repr(cell.raw),
                "unit": row.unit,
                "source": source.source,
                "source_doc_id": source.doc_id,
                "fact_id": source.fact_id,
                "fact_table": source.fact_table,
                "source_url": _trace_source_url(source.source_url),
            }
            items.append(
                GroundingTraceItem(
                    ordinal=len(items) + 1,
                    kind="fact",
                    ticker=row.ticker,
                    metric_ref=row.metric.token(),
                    period=period,
                    value=repr(cell.raw),
                    unit=row.unit,
                    source_doc_id=source.doc_id,
                    fact_id=source.fact_id,
                    fact_table=source.fact_table,
                    href=(f"/source/{source.doc_id}" if source.doc_id is not None else None),
                    source_url=_trace_source_url(source.source_url),
                    evidence_sha256=_sha_json(payload),
                )
            )
    return tuple(items)


def persist_grounding_trace(
    db_path: Path,
    *,
    question: str,
    scope_tickers: tuple[str, ...],
    route: GroundingRoute,
    strategy: GroundingStrategy,
    outcome: GroundingOutcome,
    items: tuple[GroundingTraceItem, ...],
    session_id: str | None,
    created_at: datetime | None = None,
) -> GroundingTrace:
    """Append one idempotent trace. Failure is fatal for strict grounded Ask."""

    normalized_question = " ".join(question.split())
    if not normalized_question:
        raise ValueError("grounding trace question must be non-empty")
    scope = tuple(sorted({ticker.strip().upper() for ticker in scope_tickers if ticker.strip()}))
    item_payload = [item.model_dump(mode="json") for item in items]
    item_set_json = _canonical_json(item_payload)
    item_set_sha256 = _sha_text(item_set_json)
    question_sha256 = _sha_text(normalized_question)
    scope_json = _canonical_json(list(scope))
    seed = _sha_json(
        {
            "item_set_sha256": item_set_sha256,
            "outcome": outcome,
            "question_sha256": question_sha256,
            "route": route,
            "scope_json": scope_json,
            "session_id": session_id,
            "strategy": strategy,
            "version": "ask-grounding-trace.v1",
        }
    )
    trace_id = f"ask-grounding:{seed}"
    stamp = (created_at or datetime.now(UTC)).astimezone(UTC).isoformat()
    values = (
        trace_id,
        trace_id,
        session_id,
        route,
        question_sha256,
        scope_json,
        strategy,
        outcome,
        len(items),
        item_set_json,
        item_set_sha256,
        stamp,
    )
    stored: sqlite3.Row | tuple[object, ...] | None = None
    try:
        conn = connect_sqlite(db_path, role=SQLiteConnectionRole.WRITER)
        try:
            with conn:
                conn.execute(
                    "INSERT INTO ask_grounding_traces "
                    "(trace_id,idempotency_key,session_id,route,question_sha256,scope_json,"
                    "strategy,outcome,item_count,item_set_json,item_set_sha256,created_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(idempotency_key) DO NOTHING",
                    values,
                )
                stored = conn.execute(
                    "SELECT trace_id,outcome,item_count,item_set_sha256 FROM "
                    "ask_grounding_traces WHERE idempotency_key=?",
                    (trace_id,),
                ).fetchone()
        finally:
            conn.close()
    except (sqlite3.Error, RuntimeError) as exc:
        raise GroundingTraceError("grounded Ask retrieval could not be recorded") from exc
    if stored is None or tuple(stored) != (trace_id, outcome, len(items), item_set_sha256):
        raise GroundingTraceError("grounded Ask retrieval trace conflicts with stored data")
    return GroundingTrace(trace_id=trace_id, outcome=outcome, item_count=len(items))


def _canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _trace_source_url(value: str | None) -> str | None:
    """Keep a public locator while dropping credentials, query, and fragment."""

    if not value:
        return None
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return None
    host = parsed.hostname
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    try:
        port = parsed.port
    except ValueError:
        return None
    netloc = f"{host}:{port}" if port is not None else host
    return urlunsplit((parsed.scheme, netloc, parsed.path, "", ""))


def _sha_json(value: object) -> str:
    return _sha_text(_canonical_json(value))


def _sha_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


__all__ = [
    "GroundingTrace",
    "GroundingTraceError",
    "GroundingTraceItem",
    "narrative_trace_items",
    "persist_grounding_trace",
    "view_trace_items",
]
