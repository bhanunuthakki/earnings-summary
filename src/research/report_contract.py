"""Deterministic report draft binding; semantic and product acceptance stay separate."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Generator
from contextlib import contextmanager
from datetime import UTC, date, datetime
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from provenance.issuer_registry import IssuerRegistry
from provenance.research_snapshot import ResearchSnapshotRequest, verify_research_snapshot
from provenance.source_read_context import SourceReadContext
from research.decision_brief import verify_memo_snapshot_node, verify_memo_snapshot_reference
from research.memo_claim_support import (
    MemoDerivedFigure,
    MemoReportedValue,
    MemoSourceExcerpt,
    read_evidence_node_texts,
    readable_source_text,
    verified_value_display,
    verify_memo_analysis_text,
    verify_reported_memo_claim,
)


class ReportBindingError(ValueError):
    pass


class _Closed(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ReportParagraph(_Closed):
    paragraph_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    voice: Literal["reported", "analyst_inference"]
    text: str = Field(min_length=1, max_length=8000)
    excerpts: tuple[MemoSourceExcerpt, ...] = ()
    values: tuple[MemoReportedValue, ...] = ()
    derived: tuple[MemoDerivedFigure, ...] = ()


class ReportSection(_Closed):
    title: str = Field(min_length=1, max_length=160)
    paragraphs: tuple[ReportParagraph, ...] = Field(min_length=1)


class ReportDraft(_Closed):
    schema_version: Literal["bound_report_draft.v1"] = "bound_report_draft.v1"
    family: Literal["pre", "post", "memo"]
    ticker: str = Field(pattern=r"^[A-Z][A-Z0-9.-]{0,31}$")
    market_mic: str = Field(pattern=r"^[A-Z0-9]{4}$")
    issuer_id: str = Field(min_length=1)
    research_snapshot_id: str = Field(min_length=1)
    drafted_at: AwareDatetime
    event_date: date | None = None
    sections: tuple[ReportSection, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def exact_population(self) -> ReportDraft:
        titles = tuple(section.title for section in self.sections)
        ids = tuple(p.paragraph_id for section in self.sections for p in section.paragraphs)
        if len(set(titles)) != len(titles) or len(set(ids)) != len(ids):
            raise ValueError("report sections and paragraph identities must be unique")
        if self.family in {"pre", "post"} and self.event_date is None:
            raise ValueError("earnings reports require the exact event date")
        return self


class BoundReport(_Closed):
    """Source/numerical binding is not a semantic or completeness certificate."""

    schema_version: Literal["bound_report_receipt.v1"] = "bound_report_receipt.v1"
    bound_at: AwareDatetime
    draft_sha256: str
    snapshot_sha256: str
    paragraph_ids: tuple[str, ...]
    binding_status: Literal["verified"] = "verified"
    source_completeness: Literal["not_assessed"] = "not_assessed"
    semantic_review: Literal["required"] = "required"
    decision_grade: Literal[False] = False
    temporal_status: Literal["contemporaneous_draft", "historical_reconstruction", "not_applicable"]


@contextmanager
def report_read_snapshot(conn: sqlite3.Connection) -> Generator[None]:
    """Keep nested consumers in one snapshot without closing caller transactions."""
    owns_snapshot = not conn.in_transaction
    if owns_snapshot:
        conn.execute("BEGIN")
    try:
        yield
    finally:
        if owns_snapshot and conn.in_transaction:
            conn.rollback()


def bind_report_draft(
    conn: sqlite3.Connection,
    draft: ReportDraft,
    *,
    required_titles: tuple[str, ...],
    source_context: SourceReadContext | None = None,
) -> BoundReport:
    with report_read_snapshot(conn):
        return _bind_report_draft(
            conn, draft, required_titles=required_titles, source_context=source_context
        )


def _bind_report_draft(
    conn: sqlite3.Connection,
    draft: ReportDraft,
    *,
    required_titles: tuple[str, ...],
    source_context: SourceReadContext | None,
) -> BoundReport:
    """Read only: reconstruct native evidence before checking every paragraph."""
    # Revalidate even model_copy/model_construct inputs before accessing evidence.
    draft = ReportDraft.model_validate_json(draft.model_dump_json())
    bound_at = datetime.now(UTC)
    if draft.drafted_at > bound_at:
        raise ReportBindingError("report_draft_timestamp_in_future")
    if tuple(section.title for section in draft.sections) != required_titles:
        raise ReportBindingError("report_section_population_mismatch")
    admission = verify_research_snapshot(
        conn, draft.research_snapshot_id, source_context=source_context
    )
    row = conn.execute(
        "SELECT request_json FROM research_snapshot_headers WHERE research_snapshot_id=?",
        (draft.research_snapshot_id,),
    ).fetchone()
    if row is None:
        raise ReportBindingError("report_snapshot_missing")
    snapshot = ResearchSnapshotRequest.model_validate_json(str(row[0]))
    if snapshot.research_universe.issuer_id != draft.issuer_id:
        raise ReportBindingError("report_issuer_scope_mismatch")
    listing = IssuerRegistry(conn).resolve_listing(
        draft.market_mic, draft.ticker, knowledge_at=snapshot.cutoff_at
    )
    if listing.issuer_id != draft.issuer_id:
        raise ReportBindingError("report_listing_issuer_mismatch")
    if max(snapshot.recorded_at, snapshot.cutoff_at) > draft.drafted_at:
        raise ReportBindingError("report_snapshot_recorded_after_draft")
    paragraphs = tuple(p for section in draft.sections for p in section.paragraphs)
    for paragraph in paragraphs:
        if not paragraph.excerpts and not paragraph.values:
            raise ReportBindingError("report_paragraph_evidence_missing")
        excerpts: list[tuple[str, tuple[str, ...]]] = []
        for excerpt in paragraph.excerpts:
            if len(set(excerpt.evidence_node_ids)) != len(excerpt.evidence_node_ids):
                raise ReportBindingError("report_excerpt_duplicate_node")
            for node_id in excerpt.evidence_node_ids:
                verify_memo_snapshot_node(conn, node_id, snapshot, draft.drafted_at)
            texts = tuple(read_evidence_node_texts(conn, excerpt.evidence_node_ids))
            excerpts.append((excerpt.text, texts))
        values: list[str] = []
        for value in paragraph.values:
            verify_memo_snapshot_reference(
                conn,
                value.reference,
                snapshot,
                draft.ticker,
                draft.drafted_at,
                source_context=source_context,
            )
            values.append(verified_value_display(conn, value, source_context=source_context))
        verify_memo_analysis_text(paragraph.text, tuple(excerpts), tuple(values), paragraph.derived)
        if paragraph.voice == "reported":
            if paragraph.derived:
                raise ReportBindingError("reported_paragraph_cannot_claim_analyst_calculation")
            nodes = tuple(
                node for excerpt in paragraph.excerpts for node in excerpt.evidence_node_ids
            )
            verify_reported_memo_claim(
                conn,
                paragraph.text,
                nodes,
                paragraph.values,
                source_context=source_context,
            )
            if excerpts and readable_source_text(paragraph.text) not in tuple(
                readable_source_text(excerpt) for excerpt, _texts in excerpts
            ):
                raise ReportBindingError("reported_paragraph_must_match_selected_excerpt")
    temporal: Literal["contemporaneous_draft", "historical_reconstruction", "not_applicable"] = (
        "not_applicable"
    )
    if (
        draft.family == "post"
        and draft.event_date is not None
        and draft.drafted_at.astimezone(UTC).date() < draft.event_date
    ):
        raise ReportBindingError("post_report_precedes_event")
    if draft.family == "pre" and draft.event_date is not None:
        start = datetime.combine(draft.event_date, datetime.min.time(), tzinfo=UTC)
        temporal = (
            "contemporaneous_draft"
            if max(draft.drafted_at, bound_at) < start
            else "historical_reconstruction"
        )
        # A claimed contemporaneous PRE cannot contain post-event knowledge.
        if temporal == "contemporaneous_draft" and snapshot.cutoff_at >= start:
            raise ReportBindingError("pre_report_contains_post_event_knowledge")
    return BoundReport(
        bound_at=bound_at,
        draft_sha256=report_draft_sha256(draft),
        snapshot_sha256=admission.member_set_sha256,
        paragraph_ids=tuple(p.paragraph_id for p in paragraphs),
        temporal_status=temporal,
    )


def report_draft_sha256(draft: ReportDraft) -> str:
    """Canonical commitment shared by binding and its retained renderer."""
    payload = json.dumps(draft.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()
