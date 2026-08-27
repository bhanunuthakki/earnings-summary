"""B7 — the two-phase research-task expiry sweep (execution/expire_stale_research.py).

Kill silent expiry: a 'proposed' task now expires only after (a) a SECOND
unanswered weekly-packet week, or (b) it was NEVER packeted at all and has
aged past the ``--days`` safety-net floor. A packet-acknowledged Drop tap
(capture.research_notify's ``rx:drop``) expires a task immediately and is out
of this sweep's scope entirely (tested in tests/test_research_notify.py).

Mirrors tests/test_research_proposals.py's alembic-replay fixture pattern —
``research_tasks`` is a real migrated table, not a hand-rolled schema, so this
exercises the actual ``estimated_cost_usd``/``task_metadata_json`` columns.
"""

from __future__ import annotations

import sqlite3
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PRIOR_HEAD = "0059_kpi_facts_restatement"

sys.path.insert(0, str(PROJECT_ROOT / "execution"))
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from expire_stale_research import (  # noqa: E402
    expire_stale_tasks,
    render_resurrect_section,
    resurrect_expired_preview,
)

from research.proposals import (  # noqa: E402
    create_task,
    get_task,
    set_task_extras,
    set_task_status,
)
from schema_compat import SchemaRevisionMismatch  # noqa: E402


@pytest.fixture
def db_path(tmp_path: Path, migrated_db: Callable[..., Path]) -> Path:
    return migrated_db(tmp_path / "ledger.db", stamp=PRIOR_HEAD)


def _age(db_path: Path, task_id: int, created_at: str) -> None:
    import sqlite3

    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("UPDATE research_tasks SET created_at = ? WHERE id = ?", (created_at, task_id))
        conn.commit()
    finally:
        conn.close()


def _status(db_path: Path, task_id: int) -> str:
    task = get_task(task_id, db_path=db_path)
    assert task is not None
    return task.status


def test_never_packeted_task_expires_past_the_days_floor(db_path: Path) -> None:
    old_id = create_task(note_id=None, claim="stale wondering", ticker="NU", db_path=db_path)
    _age(db_path, old_id, "2020-01-01T00:00:00")
    fresh_id = create_task(note_id=None, claim="fresh wondering", ticker="NU", db_path=db_path)

    ids = expire_stale_tasks(days=21, apply=False, db_path=db_path)
    assert ids == [old_id]
    assert _status(db_path, old_id) == "proposed"  # dry run changes nothing

    ids = expire_stale_tasks(days=21, apply=True, db_path=db_path)
    assert ids == [old_id]
    assert _status(db_path, old_id) == "expired"
    assert _status(db_path, fresh_id) == "proposed"

    # Idempotent — an expired task never matches again.
    assert expire_stale_tasks(days=21, apply=True, db_path=db_path) == []


def test_packeted_once_task_survives_the_days_floor(db_path: Path) -> None:
    """A task the weekly packet HAS surfaced once must NOT expire off the
    --days floor — only off its own two-week unanswered rule. Regression
    guard: the pre-B7 sweep would have expired this purely on age."""
    tid = create_task(note_id=None, claim="do NU margins hold?", ticker="NU", db_path=db_path)
    _age(db_path, tid, "2020-01-01T00:00:00")
    set_task_extras(tid, packeted_at="2020-01-08T00:00:00", unanswered_weeks=1, db_path=db_path)

    assert expire_stale_tasks(days=21, apply=True, db_path=db_path) == []
    assert _status(db_path, tid) == "proposed"


def test_second_unanswered_week_expires_regardless_of_days_floor(db_path: Path) -> None:
    """A task packeted TWICE (unanswered_weeks=2) expires even though it is
    only days old — the packet's own cycle, not raw age, is the trigger."""
    tid = create_task(note_id=None, claim="do NU margins hold?", ticker="NU", db_path=db_path)
    set_task_extras(tid, packeted_at="2026-07-15T00:00:00", unanswered_weeks=2, db_path=db_path)

    ids = expire_stale_tasks(days=365, apply=True, db_path=db_path)
    assert ids == [tid]
    assert _status(db_path, tid) == "expired"


def test_non_proposed_tasks_are_never_candidates(db_path: Path) -> None:
    tid = create_task(note_id=None, claim="c", ticker="NU", db_path=db_path)
    _age(db_path, tid, "2020-01-01T00:00:00")
    set_task_status(tid, "drafted", db_path=db_path)
    assert expire_stale_tasks(days=1, apply=True, db_path=db_path) == []


def test_resurrect_expired_preview_is_read_only(db_path: Path) -> None:
    tid = create_task(note_id=None, claim="MELI take rate?", ticker="MELI", db_path=db_path)
    set_task_status(tid, "expired", db_path=db_path)
    still_proposed = create_task(note_id=None, claim="x", ticker="NU", db_path=db_path)

    tasks = resurrect_expired_preview(db_path=db_path)
    assert [t.id for t in tasks] == [tid]
    # Never mutates — a second call sees the exact same state.
    assert _status(db_path, tid) == "expired"
    assert _status(db_path, still_proposed) == "proposed"


def test_render_resurrect_section_formats_the_backlog(db_path: Path) -> None:
    tid = create_task(note_id=None, claim="MELI take rate?", ticker="MELI", db_path=db_path)
    set_task_status(tid, "expired", db_path=db_path)
    tasks = resurrect_expired_preview(db_path=db_path)
    text = render_resurrect_section(tasks)
    assert "1 for one-time burst triage" in text
    assert f"#{tid} MELI - MELI take rate?" in text


def test_render_resurrect_section_empty_backlog() -> None:
    assert "No expired research tasks" in render_resurrect_section([])


def test_apply_refuses_a_database_behind_the_checkout_head(tmp_path: Path) -> None:
    stale_db = tmp_path / "stale.db"
    conn = sqlite3.connect(stale_db)
    try:
        conn.execute("CREATE TABLE alembic_version (version_num TEXT NOT NULL)")
        conn.execute("INSERT INTO alembic_version VALUES ('older_revision')")
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(SchemaRevisionMismatch):
        expire_stale_tasks(days=21, apply=True, db_path=stale_db)
