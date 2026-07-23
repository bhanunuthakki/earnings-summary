"""Tests for the session-distillation tap (B4 keystone, src/synthesis/session_distill.py).

Alembic tmp-db to head; every LLM call is an injected stub (zero live LLM).
Covers: candidate gating (idle/turn-count/already-distilled for Ask threads,
immediate eligibility for landed transcripts), the deterministic grounding
gate, per-candidate-type landing (musing / resolved_question / tenet_revision
/ stance_revision), the transient-defer vs success-completion contract, and
the ``land_session_notes.py transcript`` bridge kind.
"""

from __future__ import annotations

import sys
from datetime import timedelta
from pathlib import Path

import pytest
from alembic.config import Config

import ask.store as ask_store
import capture.sessions as capture_sessions
from alembic import command
from clock import now_naive_utc
from synthesis.session_distill import (
    Candidate,
    SessionRef,
    candidate_sessions,
    distill_session,
)
from synthesis.tenets import list_tenets, record_tenet
from user_state.notes import create_note, get_note, list_notes

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PRIOR_HEAD = "0059_kpi_facts_restatement"


def _cfg(db_path: Path) -> Config:
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return cfg


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    db = tmp_path / "ledger.db"
    cfg = _cfg(db)
    command.stamp(cfg, PRIOR_HEAD)
    command.upgrade(cfg, "head")
    return db


def _ask_session_with_turns(db_path: Path, *, user_turns: int = 2) -> ask_store.AskSession:
    sess = ask_store.create_session(scope="portfolio", db_path=db_path)
    for i in range(user_turns):
        ask_store.append_turn(
            session_id=sess.id, role="user", text=f"user turn {i}", db_path=db_path
        )
        ask_store.append_turn(
            session_id=sess.id, role="assistant", text=f"assistant reply {i}", db_path=db_path
        )
    return sess


# ---------------------------------------------------------------------------
# Migration sanity
# ---------------------------------------------------------------------------


def test_migration_adds_columns_and_budget_row(db_path: Path) -> None:
    """The two additive ``distilled_at`` columns are always present in this
    fixture (they're added by a migration inside the stamped-to-head range).
    ``llm_budgets`` itself is created by 0052 — BEFORE this fixture's
    ``PRIOR_HEAD`` (0059, matching ``test_tenet_distill.py``'s convention) —
    and ``command.stamp`` never executes DDL for revisions it stamps past, so
    the table doesn't exist in this fixture regardless of PRIOR_HEAD choice
    (the same reason ``test_tenet_distill.py`` never asserts on its own
    0132 budget row). The row-insert path is exercised for real against a
    genuine ``llm_budgets`` table when present."""
    import sqlite3

    conn = sqlite3.connect(str(db_path))
    try:
        ask_cols = {r[1] for r in conn.execute("PRAGMA table_info(ask_sessions)")}
        raw_cols = {r[1] for r in conn.execute("PRAGMA table_info(raw_capture_sessions)")}
        assert "distilled_at" in ask_cols
        assert "distilled_at" in raw_cols
        has_budgets = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='llm_budgets'"
        ).fetchone()
        if has_budgets is not None:
            row = conn.execute(
                "SELECT monthly_cap_usd, on_exceed FROM llm_budgets "
                "WHERE purpose = 'session_distill'"
            ).fetchone()
            assert row is not None
            assert row[0] == pytest.approx(15.00)
            assert row[1] == "warn"
    finally:
        conn.close()


def test_migration_seeds_budget_row_against_real_llm_budgets_table(tmp_path: Path) -> None:
    """Exercises the 0190 budget-row insert directly against a real
    ``llm_budgets`` table (the 0066+ column shape) — the fixture-DB gap noted
    above means the alembic-chain test above can't reach this path."""
    import sqlite3

    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    db = tmp_path / "budgets.db"
    conn = sqlite3.connect(str(db))
    conn.execute(
        """
        CREATE TABLE llm_budgets (
            purpose TEXT PRIMARY KEY,
            monthly_cap_usd REAL NOT NULL,
            warn_threshold_pct REAL NOT NULL,
            hard_block INTEGER NOT NULL DEFAULT 0,
            on_exceed TEXT NOT NULL DEFAULT 'skip',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            notes TEXT
        )
        """
    )
    conn.commit()
    conn.close()

    import importlib.util

    import sqlalchemy as sa

    module_path = PROJECT_ROOT / "alembic" / "versions" / "0190_session_distill.py"
    spec = importlib.util.spec_from_file_location("_migration_0190", module_path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    engine = sa.create_engine(f"sqlite:///{db}")
    try:
        with engine.connect() as bind:
            ctx = MigrationContext.configure(bind)
            with Operations.context(ctx):
                mod.upgrade()
            row = bind.execute(
                sa.text(
                    "SELECT monthly_cap_usd, on_exceed FROM llm_budgets "
                    "WHERE purpose = 'session_distill'"
                )
            ).fetchone()
            assert row is not None
            assert row[0] == pytest.approx(15.00)
            assert row[1] == "warn"
    finally:
        engine.dispose()


# ---------------------------------------------------------------------------
# candidate_sessions gating
# ---------------------------------------------------------------------------


def test_ask_session_excluded_while_recent(db_path: Path) -> None:
    _ask_session_with_turns(db_path, user_turns=2)
    refs = candidate_sessions(db_path)  # real clock — session is seconds old
    assert refs == []


def test_ask_session_included_once_idle(db_path: Path) -> None:
    sess = _ask_session_with_turns(db_path, user_turns=2)
    future = now_naive_utc() + timedelta(hours=5)
    refs = candidate_sessions(db_path, now=future)
    assert SessionRef(source="ask", session_id=sess.id) in refs


def test_ask_session_excluded_below_min_user_turns(db_path: Path) -> None:
    _ask_session_with_turns(db_path, user_turns=1)
    future = now_naive_utc() + timedelta(hours=5)
    refs = candidate_sessions(db_path, now=future)
    assert refs == []


def test_ask_session_excluded_once_distilled(db_path: Path) -> None:
    sess = _ask_session_with_turns(db_path, user_turns=2)
    ask_store.mark_session_distilled(sess.id, db_path=db_path)
    future = now_naive_utc() + timedelta(hours=5)
    refs = candidate_sessions(db_path, now=future)
    assert refs == []


def test_raw_captured_session_included_immediately(db_path: Path) -> None:
    rid = capture_sessions.new_session(
        channel="claude_session",
        media_kind="text",
        transcript="a bridged transcript",
        db_path=db_path,
    )
    assert rid is not None
    refs = candidate_sessions(db_path)  # no idle wait for a landed transcript
    assert SessionRef(source="raw", session_id=str(rid)) in refs


# ---------------------------------------------------------------------------
# Grounding gate
# ---------------------------------------------------------------------------


def test_fabricated_citation_is_skipped_groundless(db_path: Path) -> None:
    rid = capture_sessions.new_session(
        channel="claude_session",
        media_kind="text",
        transcript="the owner's own words",
        db_path=db_path,
    )
    assert rid is not None
    ref = SessionRef(source="raw", session_id=str(rid))

    def call(_prompt: str) -> list[Candidate]:
        return [{"type": "musing", "text": "a fabricated claim", "citations": ["Z99"]}]

    counts = distill_session(ref, db_path=db_path, call=call)
    assert counts["skipped_groundless"] == 1
    assert counts["musings"] == 0


# ---------------------------------------------------------------------------
# musing landing + channel
# ---------------------------------------------------------------------------


def test_musing_lands_with_ask_channel(db_path: Path) -> None:
    sess = _ask_session_with_turns(db_path, user_turns=2)
    ref = SessionRef(source="ask", session_id=sess.id)

    def call(_prompt: str) -> list[Candidate]:
        return [{"type": "musing", "text": "a keeper thought", "citations": ["U1"]}]

    counts = distill_session(ref, db_path=db_path, call=call)
    assert counts["musings"] == 1
    musings = [
        n for n in list_notes(kind="musing", db_path=db_path) if n.body == "a keeper thought"
    ]
    assert len(musings) == 1
    assert musings[0].context is not None
    assert musings[0].context["channel"] == "ask"


def test_musing_lands_with_claude_session_channel_for_raw_source(db_path: Path) -> None:
    rid = capture_sessions.new_session(
        channel="claude_session",
        media_kind="text",
        transcript="deep session braindump",
        db_path=db_path,
    )
    assert rid is not None
    ref = SessionRef(source="raw", session_id=str(rid))

    def call(_prompt: str) -> list[Candidate]:
        return [{"type": "musing", "text": "a bridged keeper", "citations": ["T1"]}]

    distill_session(ref, db_path=db_path, call=call)
    musings = [
        n for n in list_notes(kind="musing", db_path=db_path) if n.body == "a bridged keeper"
    ]
    assert len(musings) == 1
    assert musings[0].context is not None
    assert musings[0].context["channel"] == "claude_session"


# ---------------------------------------------------------------------------
# resolved_question
# ---------------------------------------------------------------------------


def test_resolved_question_resolves_the_cited_open_note(db_path: Path) -> None:
    note = create_note(
        ticker=None,
        kind="question",
        body="should I trim the winner?",
        source="manual",
        db_path=db_path,
    )
    sess = _ask_session_with_turns(db_path, user_turns=2)
    ref = SessionRef(source="ask", session_id=sess.id)

    def call(_prompt: str) -> list[Candidate]:
        return [
            {
                "type": "resolved_question",
                "text": "decided to hold, not trim",
                "resolves": f"N{note.id}",
                "citations": ["U1"],
            }
        ]

    counts = distill_session(ref, db_path=db_path, call=call)
    assert counts["resolved"] == 1
    resolved = get_note(note.id, db_path=db_path)
    assert resolved is not None
    assert resolved.status == "resolved"
    assert resolved.resolution_note == "decided to hold, not trim"


def test_resolved_question_with_unshown_token_is_skipped_groundless(db_path: Path) -> None:
    create_note(
        ticker=None, kind="question", body="a real open question", source="manual", db_path=db_path
    )
    sess = _ask_session_with_turns(db_path, user_turns=2)
    ref = SessionRef(source="ask", session_id=sess.id)

    def call(_prompt: str) -> list[Candidate]:
        return [
            {
                "type": "resolved_question",
                "text": "resolves a note that was never shown",
                "resolves": "N999999",
                "citations": ["U1"],
            }
        ]

    counts = distill_session(ref, db_path=db_path, call=call)
    assert counts["resolved"] == 0
    assert counts["skipped_groundless"] == 1


# ---------------------------------------------------------------------------
# tenet_revision — reuse a shown scope_key (supersede) vs new tenet
# ---------------------------------------------------------------------------


def test_tenet_revision_on_shown_scope_key_supersedes_prior_current(db_path: Path) -> None:
    old = record_tenet(
        body_md="Let winning theses run.", scope_key="exit-discipline", db_path=db_path
    )
    assert old.scope_key == "tenet:exit-discipline"
    sess = _ask_session_with_turns(db_path, user_turns=2)
    ref = SessionRef(source="ask", session_id=sess.id)

    def call(_prompt: str) -> list[Candidate]:
        return [
            {
                "type": "tenet_revision",
                "text": "Let winners run by default; trim on a double when conviction fades.",
                "scope_key": "tenet:exit-discipline",
                "citations": ["U1", "A2"],
            }
        ]

    counts = distill_session(ref, db_path=db_path, call=call)
    assert counts["adopted_tenets"] == 1

    from synthesis.insights import get_insight

    refreshed_old = get_insight(old.id, db_path=db_path)
    assert refreshed_old is not None
    assert refreshed_old.status == "superseded"

    current = [
        t for t in list_tenets(status="current", db_path=db_path) if t.scope_key == old.scope_key
    ]
    assert len(current) == 1
    assert current[0].provenance == "session_distill"
    assert "trim on a double" in current[0].body_md


def test_tenet_revision_with_unshown_scope_key_is_skipped_groundless(db_path: Path) -> None:
    record_tenet(body_md="Let winning theses run.", scope_key="exit-discipline", db_path=db_path)
    sess = _ask_session_with_turns(db_path, user_turns=2)
    ref = SessionRef(source="ask", session_id=sess.id)

    def call(_prompt: str) -> list[Candidate]:
        return [
            {
                "type": "tenet_revision",
                "text": "a fabricated revision of a belief never shown",
                "scope_key": "tenet:never-shown",
                "citations": ["U1"],
            }
        ]

    counts = distill_session(ref, db_path=db_path, call=call)
    assert counts["adopted_tenets"] == 0
    assert counts["skipped_groundless"] == 1


def test_new_tenet_auto_adopts_as_current(db_path: Path) -> None:
    sess = _ask_session_with_turns(db_path, user_turns=2)
    ref = SessionRef(source="ask", session_id=sess.id)

    def call(_prompt: str) -> list[Candidate]:
        return [
            {
                "type": "tenet_revision",
                "text": "I want to own long-term prosperity driven by technology.",
                "scope_key": None,
                "citations": ["U1"],
            }
        ]

    counts = distill_session(ref, db_path=db_path, call=call)
    assert counts["adopted_tenets"] == 1
    current = list_tenets(status="current", db_path=db_path)
    assert len(current) == 1
    assert current[0].provenance == "session_distill"


# ---------------------------------------------------------------------------
# stance_revision
# ---------------------------------------------------------------------------


def test_stance_revision_lands_current_stance(db_path: Path) -> None:
    sess = _ask_session_with_turns(db_path, user_turns=2)
    ref = SessionRef(source="ask", session_id=sess.id)

    def call(_prompt: str) -> list[Candidate]:
        return [
            {
                "type": "stance_revision",
                "text": "conviction is intact through the guidance cut",
                "ticker": "meli",
                "citations": ["U1"],
            }
        ]

    counts = distill_session(ref, db_path=db_path, call=call)
    assert counts["adopted_stances"] == 1

    from synthesis.insights import list_insights

    stances = list_insights(kind="stance", scope_key="MELI", status="current", db_path=db_path)
    assert len(stances) == 1
    assert stances[0].provenance == "session_distill"


def test_stance_revision_without_ticker_is_skipped_groundless(db_path: Path) -> None:
    sess = _ask_session_with_turns(db_path, user_turns=2)
    ref = SessionRef(source="ask", session_id=sess.id)

    def call(_prompt: str) -> list[Candidate]:
        return [{"type": "stance_revision", "text": "no ticker given", "citations": ["U1"]}]

    counts = distill_session(ref, db_path=db_path, call=call)
    assert counts["adopted_stances"] == 0
    assert counts["skipped_groundless"] == 1


# ---------------------------------------------------------------------------
# Transient failure vs success — the distilled_at contract
# ---------------------------------------------------------------------------


def test_transient_failure_defers_and_does_not_mark_distilled(db_path: Path) -> None:
    rid = capture_sessions.new_session(
        channel="claude_session", media_kind="text", transcript="some words", db_path=db_path
    )
    assert rid is not None
    ref = SessionRef(source="raw", session_id=str(rid))

    def call(_prompt: str) -> list[Candidate]:
        raise RuntimeError("llm CLI transiently unavailable")

    counts = distill_session(ref, db_path=db_path, call=call)
    assert counts["deferred_transient"] == 1
    row = capture_sessions.get_session(rid, db_path=db_path)
    assert row is not None
    assert row.distilled_at is None
    assert row.transcript == "some words"  # never blanked on a deferred session


def test_hard_stop_propagates(db_path: Path) -> None:
    from llm.cli import LLMSetupError

    rid = capture_sessions.new_session(
        channel="claude_session", media_kind="text", transcript="some words", db_path=db_path
    )
    assert rid is not None
    ref = SessionRef(source="raw", session_id=str(rid))

    def call(_prompt: str) -> list[Candidate]:
        raise LLMSetupError("claude CLI not found")

    with pytest.raises(LLMSetupError):
        distill_session(ref, db_path=db_path, call=call)
    row = capture_sessions.get_session(rid, db_path=db_path)
    assert row is not None
    assert row.distilled_at is None


def test_success_sets_distilled_at_and_blanks_raw_transcript(db_path: Path) -> None:
    rid = capture_sessions.new_session(
        channel="claude_session", media_kind="text", transcript="some words", db_path=db_path
    )
    assert rid is not None
    ref = SessionRef(source="raw", session_id=str(rid))

    def call(_prompt: str) -> list[Candidate]:
        return []  # nothing durable happened — still a successful sweep

    distill_session(ref, db_path=db_path, call=call)
    row = capture_sessions.get_session(rid, db_path=db_path)
    assert row is not None
    assert row.distilled_at is not None
    assert row.transcript == ""


def test_success_marks_ask_session_distilled(db_path: Path) -> None:
    sess = _ask_session_with_turns(db_path, user_turns=2)
    ref = SessionRef(source="ask", session_id=sess.id)
    future = now_naive_utc() + timedelta(hours=5)

    def call(_prompt: str) -> list[Candidate]:
        return []

    distill_session(ref, db_path=db_path, call=call)
    # a second sweep at the same "now" no longer sees this session — proof
    # distilled_at was stamped (AskSession itself doesn't expose the column).
    refs = candidate_sessions(db_path, now=future)
    assert refs == []


# ---------------------------------------------------------------------------
# land_session_notes.py — the transcript bridge kind
# ---------------------------------------------------------------------------


def test_land_session_notes_transcript_kind_writes_a_raw_session(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repo_root = tmp_path / "repo"
    (repo_root / "data").mkdir(parents=True)
    db = repo_root / "data" / "portfolio.db"
    cfg = _cfg(db)
    command.stamp(cfg, PRIOR_HEAD)
    command.upgrade(cfg, "head")

    transcript_file = tmp_path / "transcript.txt"
    transcript_file.write_text("the whole deep-session braindump", encoding="utf-8")

    argv = [
        "land_session_notes.py",
        "transcript",
        "--repo-root",
        str(repo_root),
        "--file",
        str(transcript_file),
        "--session-ref",
        "test-bridge-1",
    ]
    monkeypatch.setattr(sys, "argv", argv)

    from execution import land_session_notes

    rc = land_session_notes.main()
    assert rc == 0

    sessions = capture_sessions.list_undistilled_captured(db_path=db)
    matches = [s for s in sessions if s.transcript == "the whole deep-session braindump"]
    assert len(matches) == 1
    assert matches[0].channel == "claude_session"
    assert matches[0].status == "captured"
