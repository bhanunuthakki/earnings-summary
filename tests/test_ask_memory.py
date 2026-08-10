"""Tests for Ask thread persistence (S3): ask_sessions + ask_turns schema,
the store CRUD layer, and the engine's load/save behaviour.

LLM seams are monkeypatched throughout — the suite never spends.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from ask import engine as ask_engine
from ask import narrative_transport
from ask.context import ContextPack
from ask.engine import AskTurn, fold_events, respond_turn
from ask.store import (
    append_turn,
    create_session,
    delete_session,
    ensure_session,
    get_session,
    list_sessions,
    load_recent_history,
    load_turns,
    rename_session,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_SCHEMA_SQL = """
CREATE TABLE ask_sessions (
    id      TEXT PRIMARY KEY,
    scope   TEXT NOT NULL DEFAULT 'portfolio',
    title   TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE ask_turns (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT NOT NULL REFERENCES ask_sessions(id) ON DELETE CASCADE,
    role        TEXT NOT NULL,
    text        TEXT NOT NULL,
    citations_json TEXT,
    model       TEXT,
    created_at  TEXT NOT NULL
);
"""


@pytest.fixture
def session_db(tmp_path: Path) -> Path:
    """Minimal DB with just the ask_sessions + ask_turns tables."""
    db = tmp_path / "ask_test.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(_SCHEMA_SQL)
    conn.commit()
    conn.close()
    return db


@pytest.fixture
def full_db(tmp_path: Path) -> Path:
    """DB that also has tracked_companies (for the engine's catalog path)."""
    db = tmp_path / "portfolio.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(
        _SCHEMA_SQL
        + """
    CREATE TABLE tracked_companies (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id TEXT NOT NULL DEFAULT 'bhanu',
        ticker TEXT NOT NULL,
        name TEXT NOT NULL,
        list_type TEXT NOT NULL,
        archived_at TEXT
    );
    """
    )
    conn.commit()
    conn.close()
    return db


def _portfolio_pack() -> ContextPack:
    return ContextPack(
        scope="portfolio",
        default_tickers=["TST"],
        system_context="SYSTEM",
    )


# ---------------------------------------------------------------------------
# store.py CRUD
# ---------------------------------------------------------------------------


class TestStoreSession:
    def test_create_returns_uuid_hex(self, session_db: Path) -> None:
        sess = create_session(scope="portfolio", title="first", db_path=session_db)
        assert len(sess.id) == 32
        assert sess.scope == "portfolio"
        assert sess.title == "first"

    def test_get_session_roundtrip(self, session_db: Path) -> None:
        sess = create_session(db_path=session_db)
        loaded = get_session(sess.id, db_path=session_db)
        assert loaded is not None
        assert loaded.id == sess.id

    def test_get_session_missing_returns_none(self, session_db: Path) -> None:
        assert get_session("nonexistent", db_path=session_db) is None

    def test_ensure_session_creates_when_none(self, session_db: Path) -> None:
        sess = ensure_session(None, db_path=session_db)
        assert sess.id
        assert get_session(sess.id, db_path=session_db) is not None

    def test_ensure_session_loads_existing(self, session_db: Path) -> None:
        created = create_session(title="hello", db_path=session_db)
        loaded = ensure_session(created.id, db_path=session_db)
        assert loaded.id == created.id
        assert loaded.title == "hello"

    def test_ensure_session_creates_when_id_unknown(self, session_db: Path) -> None:
        sess = ensure_session("badid", db_path=session_db)
        assert sess.id != "badid"

    def test_list_sessions_most_recent_first(self, session_db: Path) -> None:
        a = create_session(title="a", db_path=session_db)
        b = create_session(title="b", db_path=session_db)
        # Touch b so it has a newer updated_at
        rename_session(b.id, "b-renamed", db_path=session_db)
        rows = list_sessions(scope="portfolio", db_path=session_db)
        assert rows[0].id == b.id
        assert rows[1].id == a.id

    def test_rename_session(self, session_db: Path) -> None:
        sess = create_session(title="old", db_path=session_db)
        ok = rename_session(sess.id, "new title", db_path=session_db)
        assert ok
        updated = get_session(sess.id, db_path=session_db)
        assert updated is not None
        assert updated.title == "new title"

    def test_rename_session_missing_returns_false(self, session_db: Path) -> None:
        assert not rename_session("ghost", "x", db_path=session_db)

    def test_delete_session_cascades(self, session_db: Path) -> None:
        sess = create_session(db_path=session_db)
        append_turn(session_id=sess.id, role="user", text="hi", db_path=session_db)
        ok = delete_session(sess.id, db_path=session_db)
        assert ok
        assert get_session(sess.id, db_path=session_db) is None
        conn = sqlite3.connect(str(session_db))
        count = conn.execute(
            "SELECT COUNT(*) FROM ask_turns WHERE session_id = ?", (sess.id,)
        ).fetchone()[0]
        conn.close()
        assert count == 0

    def test_delete_session_missing_returns_false(self, session_db: Path) -> None:
        assert not delete_session("ghost", db_path=session_db)


class TestStoreTurns:
    def test_append_and_load(self, session_db: Path) -> None:
        sess = create_session(db_path=session_db)
        append_turn(session_id=sess.id, role="user", text="what is NU?", db_path=session_db)
        append_turn(
            session_id=sess.id, role="assistant", text="NU is a fintech", db_path=session_db
        )
        turns = load_turns(sess.id, db_path=session_db)
        assert len(turns) == 2
        assert turns[0].role == "user"
        assert turns[1].role == "assistant"

    def test_citations_json_roundtrip(self, session_db: Path) -> None:
        sess = create_session(db_path=session_db)
        cites = [{"n": 1, "label": "10-K", "href": "/source/1"}]
        append_turn(
            session_id=sess.id,
            role="assistant",
            text="see [1]",
            citations=cites,
            db_path=session_db,
        )
        turns = load_turns(sess.id, db_path=session_db)
        assert turns[0].citations == cites

    def test_load_recent_history_caps(self, session_db: Path) -> None:
        sess = create_session(db_path=session_db)
        for i in range(25):
            append_turn(session_id=sess.id, role="user", text=f"q{i}", db_path=session_db)
            append_turn(session_id=sess.id, role="assistant", text=f"a{i}", db_path=session_db)
        hist = load_recent_history(sess.id, db_path=session_db, max_turns=10)
        assert len(hist) == 10
        # most recent 10 turns end with the last assistant reply
        assert "a24" in hist[-1]["text"]
        # and the oldest of the 10 is near the end of the 50-turn range
        assert hist[0]["text"] in {f"q{i}" for i in range(20, 25)} | {
            f"a{i}" for i in range(20, 25)
        }

    def test_load_recent_history_truncates_text(self, session_db: Path) -> None:
        sess = create_session(db_path=session_db)
        long_text = "x" * 5000
        append_turn(session_id=sess.id, role="user", text=long_text, db_path=session_db)
        hist = load_recent_history(sess.id, db_path=session_db, max_chars=100)
        assert len(hist[0]["text"]) == 100

    def test_append_updates_session_updated_at(self, session_db: Path) -> None:
        sess = create_session(db_path=session_db)
        old_ts = sess.updated_at
        append_turn(session_id=sess.id, role="user", text="hi", db_path=session_db)
        fresh = get_session(sess.id, db_path=session_db)
        assert fresh is not None
        assert fresh.updated_at >= old_ts


# ---------------------------------------------------------------------------
# engine.py — portfolio narrative with session_id
# ---------------------------------------------------------------------------


def _fake_stream(text: str) -> Iterator[dict[str, object]]:
    """Minimal monkeypatch for the narrative LLM transport."""
    yield {"type": "delta", "text": text}
    yield {"type": "final", "text": text}


def _no_evidence(
    question: str,
    *,
    repo_root: object,
    db_path: object,
    scope_tickers: object,
    cache_key: object = None,
) -> list[object]:
    del question, repo_root, db_path, scope_tickers, cache_key
    return []


def _empty_evidence_block(evidence: object) -> str:
    del evidence
    return ""


def _make_fake_stream(text: str):
    def fake(prompt: str, *, purpose: str = "ask_answer") -> Iterator[dict[str, object]]:
        del prompt
        return _fake_stream(text)

    return fake


class TestEngineSessionPersistence:
    def test_first_turn_creates_user_and_asst_rows(
        self, monkeypatch: pytest.MonkeyPatch, full_db: Path, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(
            narrative_transport, "stream_llm_text", _make_fake_stream("answer1")
        )
        monkeypatch.setattr(ask_engine, "gather_evidence", _no_evidence)
        monkeypatch.setattr(ask_engine, "build_evidence_block", _empty_evidence_block)

        sess = create_session(db_path=full_db)
        turn = AskTurn(text="hello", session_id=sess.id)
        list(respond_turn(turn, _portfolio_pack(), db_path=full_db, repo_root=tmp_path))

        turns = load_turns(sess.id, db_path=full_db)
        assert len(turns) == 2
        assert turns[0].role == "user"
        assert turns[0].text == "hello"
        assert turns[1].role == "assistant"
        assert turns[1].text == "answer1"

    def test_second_turn_loads_history_from_db(
        self, monkeypatch: pytest.MonkeyPatch, full_db: Path, tmp_path: Path
    ) -> None:
        """The second turn's prompt must include the first turn's text."""
        prompts: list[str] = []

        def fake_stream(prompt: str, *, purpose: str = "ask_answer") -> Iterator[dict[str, object]]:
            prompts.append(prompt)
            yield {"type": "delta", "text": "answer"}
            yield {"type": "final", "text": "answer"}

        monkeypatch.setattr(narrative_transport, "stream_llm_text", fake_stream)
        monkeypatch.setattr(ask_engine, "gather_evidence", _no_evidence)
        monkeypatch.setattr(ask_engine, "build_evidence_block", _empty_evidence_block)

        sess = create_session(db_path=full_db)
        turn1 = AskTurn(text="first question", session_id=sess.id)
        list(respond_turn(turn1, _portfolio_pack(), db_path=full_db, repo_root=tmp_path))

        turn2 = AskTurn(text="second question", session_id=sess.id)
        list(respond_turn(turn2, _portfolio_pack(), db_path=full_db, repo_root=tmp_path))

        assert len(prompts) == 2
        assert "first question" in prompts[1]

    def test_no_session_id_uses_client_history(
        self, monkeypatch: pytest.MonkeyPatch, full_db: Path, tmp_path: Path
    ) -> None:
        """Legacy path: no session_id → client history tail is used."""
        prompts: list[str] = []

        def fake_stream(prompt: str, *, purpose: str = "ask_answer") -> Iterator[dict[str, object]]:
            prompts.append(prompt)
            yield {"type": "delta", "text": "ok"}
            yield {"type": "final", "text": "ok"}

        monkeypatch.setattr(narrative_transport, "stream_llm_text", fake_stream)
        monkeypatch.setattr(ask_engine, "gather_evidence", _no_evidence)
        monkeypatch.setattr(ask_engine, "build_evidence_block", _empty_evidence_block)

        turn = AskTurn(
            text="new question",
            session_id=None,
            history=[{"role": "user", "text": "prior client turn"}],
        )
        list(respond_turn(turn, _portfolio_pack(), db_path=full_db, repo_root=tmp_path))
        assert "prior client turn" in prompts[0]

    def test_session_error_degrades_gracefully(
        self, monkeypatch: pytest.MonkeyPatch, full_db: Path, tmp_path: Path
    ) -> None:
        """DB write failures must not break the event stream."""
        monkeypatch.setattr(narrative_transport, "stream_llm_text", _make_fake_stream("ok"))
        monkeypatch.setattr(ask_engine, "gather_evidence", _no_evidence)
        monkeypatch.setattr(ask_engine, "build_evidence_block", _empty_evidence_block)
        # Point the turn at a non-existent session — _store_append_turn will fail.
        # The engine should still yield a final event.
        turn = AskTurn(text="hi", session_id="doesnotexist")
        events = list(respond_turn(turn, _portfolio_pack(), db_path=full_db, repo_root=tmp_path))
        finals = [e for e in events if e.get("type") == "final"]
        assert finals  # stream completed despite DB write failure

    def test_fold_events_includes_session_id_passthrough(
        self, monkeypatch: pytest.MonkeyPatch, full_db: Path, tmp_path: Path
    ) -> None:
        """fold_events output from a session-backed turn still has status/kind/text."""
        monkeypatch.setattr(
            narrative_transport, "stream_llm_text", _make_fake_stream("the answer")
        )
        monkeypatch.setattr(ask_engine, "gather_evidence", _no_evidence)
        monkeypatch.setattr(ask_engine, "build_evidence_block", _empty_evidence_block)

        sess = create_session(db_path=full_db)
        turn = AskTurn(text="question", session_id=sess.id)
        events = list(respond_turn(turn, _portfolio_pack(), db_path=full_db, repo_root=tmp_path))
        result = fold_events(events)
        assert result["status"] == "ok"
        assert result["kind"] == "narrative"
        assert result["text"] == "the answer"


# ---------------------------------------------------------------------------
# thread cap behaviour (max_turns / max_chars from load_recent_history)
# ---------------------------------------------------------------------------


class TestHistoryCaps:
    def test_engine_caps_history_to_max_turns(
        self, monkeypatch: pytest.MonkeyPatch, full_db: Path, tmp_path: Path
    ) -> None:
        """Even a session with 30 stored turns sends at most max_turns to the
        prompt (the full thread is preserved in DB regardless)."""
        prompts: list[str] = []

        def fake_stream(prompt: str, *, purpose: str = "ask_answer") -> Iterator[dict[str, object]]:
            prompts.append(prompt)
            yield {"type": "delta", "text": "ok"}
            yield {"type": "final", "text": "ok"}

        monkeypatch.setattr(narrative_transport, "stream_llm_text", fake_stream)
        monkeypatch.setattr(ask_engine, "gather_evidence", _no_evidence)
        monkeypatch.setattr(ask_engine, "build_evidence_block", _empty_evidence_block)

        sess = create_session(db_path=full_db)
        # Seed 30 prior turns directly (no LLM call).
        for i in range(15):
            append_turn(session_id=sess.id, role="user", text=f"q{i}", db_path=full_db)
            append_turn(session_id=sess.id, role="assistant", text=f"a{i}", db_path=full_db)

        turn = AskTurn(text="final question", session_id=sess.id)
        list(respond_turn(turn, _portfolio_pack(), db_path=full_db, repo_root=tmp_path))

        # The engine calls sanitize_history which caps at _MAX_HISTORY_TURNS=8
        prompt = prompts[0]
        # Oldest turns (q0/a0) must NOT appear; the recent tail must.
        assert "q0" not in prompt
        assert "q14" in prompt

        # All 30 + the new turn+answer are still in the DB.
        db_turns = load_turns(sess.id, db_path=full_db)
        # 30 seeded + 1 user ("final question") + 1 assistant ("ok")
        assert len(db_turns) == 32
