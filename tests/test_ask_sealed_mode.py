# pyright: reportPrivateUsage=false
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

import ask.engine as engine
import ask.store as store


def test_invalid_mode_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    for bad in ("permissive", "sealed", "shadow"):
        monkeypatch.setenv("ASK_RETRIEVAL_MODE", bad)
        with pytest.raises(ValueError, match="grounded or legacy"):
            engine.ask_retrieval_mode()


def test_final_assistant_append_is_exact_tail_compare_and_swap(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "ask.sqlite"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE ask_sessions (
          id TEXT PRIMARY KEY, updated_at TEXT NOT NULL
        );
        CREATE TABLE ask_turns (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          session_id TEXT NOT NULL, role TEXT NOT NULL, text TEXT NOT NULL,
          citations_json TEXT, model TEXT, created_at TEXT NOT NULL
        );
        INSERT INTO ask_sessions VALUES ('session-1','2026-01-01');
        INSERT INTO ask_turns(session_id,role,text,created_at)
          VALUES ('session-1','user','first','2026-01-01');
        INSERT INTO ask_turns(session_id,role,text,created_at)
          VALUES ('session-1','user','concurrent','2026-01-02');
        """
    )
    conn.commit()
    conn.close()

    def open_test(path: Path) -> sqlite3.Connection:
        opened = sqlite3.connect(path)
        opened.row_factory = sqlite3.Row
        return opened

    monkeypatch.setattr(store, "_open", open_test)
    with pytest.raises(ValueError, match="session advanced"):
        store.append_assistant_turn_if_user_tail(
            session_id="session-1",
            user_turn_id=1,
            user_text="first",
            text="stale answer",
            db_path=db_path,
        )
    assert [item.text for item in store.load_turns("session-1", db_path=db_path)] == [
        "first",
        "concurrent",
    ]
