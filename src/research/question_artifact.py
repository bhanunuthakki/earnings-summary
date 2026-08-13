"""Governed lifecycle for questions proposed by thesis or Ask engagement.

Drafting is inert: it creates only a research proposal.  A durable
``analyst_notes`` question is written only after an explicit owner approval,
with a deterministic source reference so retries return the same note.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from research.proposals import create_proposal, get_proposal
from user_state._db import now_iso, open_conn, open_read_conn
from user_state.notes import AnalystNoteRow, create_note, get_note


class QuestionProposalContent(BaseModel):
    model_config = ConfigDict(frozen=True)

    schema_version: Literal["question_proposal.v1"] = "question_proposal.v1"
    ticker: str = Field(min_length=1, max_length=16)
    body: str = Field(min_length=1, max_length=2_000)
    origin: Literal["thesis", "engagement", "model"]
    evidence_ref: str | None = Field(default=None, max_length=500)
    supersedes_note_id: int | None = Field(default=None, gt=0)
    expected_revision: str | None = None

    @model_validator(mode="after")
    def _correction_requires_cas(self) -> QuestionProposalContent:
        if self.supersedes_note_id is not None and not self.expected_revision:
            raise ValueError("question correction requires expected_revision")
        if self.supersedes_note_id is None and self.expected_revision is not None:
            raise ValueError("expected_revision requires supersedes_note_id")
        return self


def draft_question_proposal(
    *,
    ticker: str,
    body: str,
    origin: Literal["thesis", "engagement", "model"],
    evidence_ref: str | None = None,
    supersedes_note_id: int | None = None,
    expected_revision: str | None = None,
    db_path: Path | str | None = None,
    create_fn: Callable[..., int] = create_proposal,
) -> int:
    """Persist an inert, typed proposal; never writes analyst_notes."""

    content = QuestionProposalContent(
        ticker=ticker.strip().upper(),
        body=body.strip(),
        origin=origin,
        evidence_ref=evidence_ref,
        supersedes_note_id=supersedes_note_id,
        expected_revision=expected_revision,
    )
    return int(
        create_fn(
            task_id=None,
            kind="question",
            ticker=content.ticker,
            title=f"Open question: {content.ticker}",
            body_md=content.body,
            evidence_json=(json.dumps([{"fact_ref": evidence_ref}]) if evidence_ref else "[]"),
            artifact_json=content.model_dump_json(),
            provenance="derived",
            db_path=db_path,
        )
    )


def _existing(
    proposal_id: int,
    *,
    db_path: Path | str | None,
    conn: sqlite3.Connection | None = None,
) -> AnalystNoteRow | None:
    source_ref = f"question-proposal:{proposal_id}"
    db_conn = conn or open_read_conn(db_path)
    try:
        row = db_conn.execute(
            "SELECT id FROM analyst_notes WHERE source = 'advisor' AND source_ref = ? "
            "ORDER BY id DESC LIMIT 1",
            (source_ref,),
        ).fetchone()
    finally:
        if conn is None:
            db_conn.close()
    return None if row is None else get_note(int(row["id"]), db_path=db_path, conn=conn)


def _validate_correction_target(
    content: QuestionProposalContent,
    *,
    db_path: Path | str | None,
    conn: sqlite3.Connection | None = None,
) -> None:
    if content.supersedes_note_id is None:
        return
    old = get_note(content.supersedes_note_id, db_path=db_path, conn=conn)
    if old is None:
        raise LookupError(f"analyst_notes id={content.supersedes_note_id} not found")
    if old.kind != "question" or old.status != "open":
        raise ValueError("question correction target must be an open question")
    if (old.ticker or "").upper() != content.ticker:
        raise ValueError("question correction target ticker does not match proposal")


def _apply_content(
    proposal_id: int,
    content: QuestionProposalContent,
    *,
    db_path: Path | str | None,
    conn: sqlite3.Connection | None,
    create_fn: Callable[..., AnalystNoteRow],
    supersede_fn: Callable[..., AnalystNoteRow] | None,
) -> AnalystNoteRow:
    existing = _existing(proposal_id, db_path=db_path, conn=conn)
    if existing is not None:
        return existing
    _validate_correction_target(content, db_path=db_path, conn=conn)
    source_ref = f"question-proposal:{proposal_id}"
    context: dict[str, object] = {
        "origin": content.origin,
        "approval": "owner-approved",
        "proposal_id": proposal_id,
    }
    if content.evidence_ref:
        context["evidence_ref"] = content.evidence_ref
    if content.supersedes_note_id is not None:
        if supersede_fn is None:
            from user_state.notes import supersede_note

            supersede_fn = supersede_note
        return supersede_fn(
            content.supersedes_note_id,
            body=content.body,
            kind="question",
            source="advisor",
            source_ref=source_ref,
            context=context,
            expected_revision=content.expected_revision,
            db_path=db_path,
            conn=conn,
        )
    return create_fn(
        ticker=content.ticker,
        kind="question",
        body=content.body,
        anchor_type="ticker",
        anchor_key=content.ticker,
        fact_ref=content.evidence_ref,
        source="advisor",
        source_ref=source_ref,
        context=context,
        db_path=db_path,
        conn=conn,
    )


def apply_question_proposal(
    proposal_id: int,
    *,
    db_path: Path | str | None = None,
    get_fn: Callable[..., Any] = get_proposal,
    create_fn: Callable[..., AnalystNoteRow] = create_note,
    supersede_fn: Callable[..., AnalystNoteRow] | None = None,
) -> AnalystNoteRow:
    """Apply one already owner-approved question proposal idempotently."""

    existing = _existing(proposal_id, db_path=db_path)
    if existing is not None:
        return existing
    prop = get_fn(proposal_id, db_path=db_path)
    if prop is None or getattr(prop, "kind", None) != "question":
        raise ValueError(f"proposal {proposal_id} is not a question proposal")
    if getattr(prop, "status", None) != "approved":
        raise ValueError("question proposal requires explicit owner approval")
    raw = getattr(prop, "artifact_json", None)
    if not raw:
        raise ValueError("question proposal carries no typed content")
    content = QuestionProposalContent.model_validate_json(raw)
    return _apply_content(
        proposal_id,
        content,
        db_path=db_path,
        conn=None,
        create_fn=create_fn,
        supersede_fn=supersede_fn,
    )


def approve_question_proposal(
    proposal_id: int,
    *,
    db_path: Path | str | None = None,
) -> AnalystNoteRow:
    """Atomically record owner approval and write the durable question."""

    conn = open_conn(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT kind, status, artifact_json FROM research_proposals WHERE id = ?",
            (proposal_id,),
        ).fetchone()
        if row is None or str(row["kind"]) != "question":
            raise ValueError(f"proposal {proposal_id} is not a question proposal")
        if str(row["status"]) not in {"pending", "approved"}:
            raise ValueError("question proposal cannot be approved from its current status")
        raw = row["artifact_json"]
        if not raw:
            raise ValueError("question proposal carries no typed content")
        content = QuestionProposalContent.model_validate_json(str(raw))
        note = _apply_content(
            proposal_id,
            content,
            db_path=db_path,
            conn=conn,
            create_fn=create_note,
            supersede_fn=None,
        )
        conn.execute(
            "UPDATE research_proposals SET status = 'approved', updated_at = ? WHERE id = ?",
            (now_iso(), proposal_id),
        )
        conn.commit()
        return note
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


__all__ = [
    "QuestionProposalContent",
    "apply_question_proposal",
    "approve_question_proposal",
    "draft_question_proposal",
]
