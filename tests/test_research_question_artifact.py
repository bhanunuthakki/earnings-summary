"""Governed persistence tests for thesis/engagement-derived questions."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from research.apply import apply_approved_proposal
from research.proposals import act_on_proposal, get_proposal
from research.question_artifact import (
    apply_question_proposal,
    approve_question_proposal,
    draft_question_proposal,
)
from user_state.notes import NoteRevisionConflictError, create_note, get_note, list_notes


@pytest.fixture(name="db_path")
def _db_path(tmp_path: Path, migrated_db: Callable[..., Path]) -> Path:
    return migrated_db(tmp_path / "question_artifact.db")


def test_draft_is_inert_until_explicit_owner_approval(db_path: Path) -> None:
    proposal_id = draft_question_proposal(
        ticker="nu",
        body="Is Mexico deposit growth rate-led?",
        origin="engagement",
        evidence_ref="fin:NU:deposits",
        db_path=db_path,
    )

    proposal = get_proposal(proposal_id, db_path=db_path)
    assert proposal is not None
    assert proposal.kind == "question"
    assert proposal.status == "pending"
    assert list_notes(ticker="NU", kind="question", status="open", db_path=db_path) == []
    with pytest.raises(ValueError, match="explicit owner approval"):
        apply_question_proposal(proposal_id, db_path=db_path)


def test_approval_persists_provenance_and_replays_idempotently(db_path: Path) -> None:
    proposal_id = draft_question_proposal(
        ticker="NU",
        body="Does deposit beta normalize without slowing growth?",
        origin="thesis",
        evidence_ref="thesis:NU:funding",
        db_path=db_path,
    )

    first = approve_question_proposal(proposal_id, db_path=db_path)
    second = approve_question_proposal(proposal_id, db_path=db_path)

    assert first.id == second.id
    assert first.source == "advisor"
    assert first.source_ref == f"question-proposal:{proposal_id}"
    assert first.context == {
        "origin": "thesis",
        "approval": "owner-approved",
        "proposal_id": proposal_id,
        "evidence_ref": "thesis:NU:funding",
    }
    assert len(list_notes(ticker="NU", kind="question", status="open", db_path=db_path)) == 1


def test_approved_correction_supersedes_with_revision_and_provenance(db_path: Path) -> None:
    original_id = draft_question_proposal(
        ticker="NU",
        body="Original question",
        origin="model",
        db_path=db_path,
    )
    original = approve_question_proposal(original_id, db_path=db_path)
    correction_id = draft_question_proposal(
        ticker="NU",
        body="Corrected question",
        origin="engagement",
        supersedes_note_id=original.id,
        expected_revision=original.updated_at.isoformat(),
        db_path=db_path,
    )
    corrected = approve_question_proposal(correction_id, db_path=db_path)

    prior = get_note(original.id, db_path=db_path)
    assert prior is not None and prior.status == "superseded"
    assert corrected.supersedes_id == original.id
    assert corrected.source_ref == f"question-proposal:{correction_id}"
    assert corrected.context is not None
    assert corrected.context["approval"] == "owner-approved"


def test_normal_proposal_approval_dispatch_persists_question(db_path: Path) -> None:
    proposal_id = draft_question_proposal(
        ticker="NU", body="Normal approval path", origin="engagement", db_path=db_path
    )
    assert act_on_proposal(proposal_id, "approve", db_path=db_path) == "approved"

    receipt = apply_approved_proposal(proposal_id, db_path=db_path)

    assert "open question" in receipt
    assert len(list_notes(ticker="NU", kind="question", status="open", db_path=db_path)) == 1


def test_correction_cannot_reclassify_another_kind_or_cross_tickers(db_path: Path) -> None:
    musing = create_note(ticker="NU", kind="musing", body="Keep this as a musing", db_path=db_path)
    proposal_id = draft_question_proposal(
        ticker="MELI",
        body="Must not overwrite NU",
        origin="model",
        supersedes_note_id=musing.id,
        expected_revision=musing.updated_at.isoformat(),
        db_path=db_path,
    )

    with pytest.raises(ValueError, match="open question"):
        approve_question_proposal(proposal_id, db_path=db_path)

    unchanged = get_note(musing.id, db_path=db_path)
    assert unchanged is not None and unchanged.status == "open" and unchanged.kind == "musing"
    proposal = get_proposal(proposal_id, db_path=db_path)
    assert proposal is not None and proposal.status == "pending"


def test_stale_correction_rolls_back_approval_status(db_path: Path) -> None:
    original = create_note(ticker="NU", kind="question", body="Original", db_path=db_path)
    proposal_id = draft_question_proposal(
        ticker="NU",
        body="Stale correction",
        origin="thesis",
        supersedes_note_id=original.id,
        expected_revision="stale-revision",
        db_path=db_path,
    )

    with pytest.raises(NoteRevisionConflictError):
        approve_question_proposal(proposal_id, db_path=db_path)

    proposal = get_proposal(proposal_id, db_path=db_path)
    unchanged = get_note(original.id, db_path=db_path)
    assert proposal is not None and proposal.status == "pending"
    assert unchanged is not None and unchanged.status == "open"
