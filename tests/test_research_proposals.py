"""Explicit research task and proposal store behavior."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from research import proposals

PRIOR_HEAD = "0059_kpi_facts_restatement"


@pytest.fixture
def db_path(tmp_path: Path, migrated_db: Callable[..., Path]) -> Path:
    return migrated_db(tmp_path / "ledger.db", stamp=PRIOR_HEAD)


def test_create_list_and_status(db_path: Path) -> None:
    tid = proposals.create_task(note_id=1, claim="do margins hold?", ticker="NU", db_path=db_path)
    assert tid > 0
    tasks = proposals.list_tasks(status="proposed", db_path=db_path)
    assert len(tasks) == 1
    assert tasks[0].claim == "do margins hold?"
    assert tasks[0].ticker == "NU"
    proposals.set_task_status(tid, "running", db_path=db_path)
    got = proposals.get_task(tid, db_path=db_path)
    assert got is not None and got.status == "running"


# ---------------------------------------------------------------------------
# Red-team wave A: source_note_ids is a real field, parsed from the DB column
# ---------------------------------------------------------------------------


def test_proposal_source_note_ids_round_trip(db_path: Path) -> None:
    """The DB column was populated but never mapped — ResearchProposal had no
    field, so the Ledger card's "from your note" backlink was dead by
    construction. A stored JSON array must round-trip into ints."""
    pid = proposals.create_proposal(
        task_id=None,
        kind="memo",
        ticker="NU",
        title="NU margin question",
        body_md="Margins hold.",
        source_note_ids="[54]",
        db_path=db_path,
    )
    got = proposals.get_proposal(pid, db_path=db_path)
    assert got is not None
    assert got.source_note_ids == [54]
    listed = proposals.list_proposals(status="pending", db_path=db_path)
    assert [p.source_note_ids for p in listed] == [[54]]


# ---------------------------------------------------------------------------
# B7 — set_task_extras (the estimated_cost_usd / task_metadata_json columns)
# ---------------------------------------------------------------------------


def test_set_task_extras_merges_without_clobbering(db_path: Path) -> None:
    tid = proposals.create_task(note_id=None, claim="c", ticker="NU", db_path=db_path)
    proposals.set_task_extras(
        tid, estimated_cost_usd=0.40, session_prompt="prompt v1", db_path=db_path
    )
    task = proposals.get_task(tid, db_path=db_path)
    assert task is not None
    assert task.estimated_cost_usd == 0.40
    assert task.metadata == {"session_prompt": "prompt v1"}

    # A second call only touching packeted_at must NOT clobber session_prompt
    # or estimated_cost_usd — read-merge-write, like patch_note_context.
    proposals.set_task_extras(tid, packeted_at="2026-07-20T00:00:00", db_path=db_path)
    task2 = proposals.get_task(tid, db_path=db_path)
    assert task2 is not None
    assert task2.estimated_cost_usd == 0.40
    assert task2.metadata["session_prompt"] == "prompt v1"
    assert task2.metadata["packeted_at"] == "2026-07-20T00:00:00"


def test_set_task_extras_unanswered_weeks_accumulates(db_path: Path) -> None:
    tid = proposals.create_task(note_id=None, claim="c", ticker=None, db_path=db_path)
    proposals.set_task_extras(tid, unanswered_weeks=1, db_path=db_path)
    proposals.set_task_extras(tid, unanswered_weeks=2, db_path=db_path)
    task = proposals.get_task(tid, db_path=db_path)
    assert task is not None
    assert task.metadata["unanswered_weeks"] == 2


def test_set_task_extras_missing_task_is_a_noop(db_path: Path) -> None:
    proposals.set_task_extras(999999, estimated_cost_usd=1.0, db_path=db_path)  # must not raise


def test_proposal_source_note_ids_garbage_degrades_to_empty(db_path: Path) -> None:
    for raw in ("not json", '{"a": 1}', '[1, "x", true]', "[]"):
        pid = proposals.create_proposal(
            task_id=None,
            kind="memo",
            ticker="NU",
            title=f"garbage {raw!r}",
            body_md="body",
            source_note_ids=raw,
            db_path=db_path,
        )
        got = proposals.get_proposal(pid, db_path=db_path)
        assert got is not None
        # Non-list / unparseable -> []; a mixed list keeps only real ints
        # (bools are not note ids).
        assert got.source_note_ids == ([1] if raw.startswith("[1") else [])
