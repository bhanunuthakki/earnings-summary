"""CRUD layer for the Ask thread persistence tables (ask_sessions + ask_turns).

All timestamps follow the repo-wide naive-UTC convention: ISO 8601 strings
WITHOUT a timezone suffix, matching ``src/alerts/store.py:_now_iso``.  An
aware ``+00:00`` suffix would silently corrupt comparisons against naive
datetimes in the rest of the codebase.

The module is deliberately thin — no ORM, no dataclass ecosystem — to stay
consistent with the other user_state CRUD modules (saved_views.py, etc.).
Callers handle connection lifetime; all functions open and close their own
connections.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import cast
from uuid import uuid4

from clock import now_iso

# ---------------------------------------------------------------------------
# Naive-UTC convention
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    """Naive-UTC ISO string — the repo-wide convention for TEXT timestamps."""
    return now_iso()


def _open(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class AskSession:
    id: str
    scope: str
    title: str
    created_at: str
    updated_at: str


@dataclass(slots=True)
class AskTurnRow:
    id: int
    session_id: str
    role: str  # 'user' | 'assistant'
    text: str
    citations: Sequence[object] | None  # decoded from citations_json
    model: str | None
    created_at: str


# ---------------------------------------------------------------------------
# Session CRUD
# ---------------------------------------------------------------------------


def create_session(
    *,
    scope: str = "portfolio",
    title: str = "",
    db_path: Path,
) -> AskSession:
    """Insert a new ask_session row and return it."""
    sid = uuid4().hex
    now = _now_iso()
    conn = _open(db_path)
    try:
        conn.execute(
            "INSERT INTO ask_sessions (id, scope, title, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (sid, scope, title, now, now),
        )
        conn.commit()
    finally:
        conn.close()
    return AskSession(id=sid, scope=scope, title=title, created_at=now, updated_at=now)


def get_session(session_id: str, *, db_path: Path) -> AskSession | None:
    """Return the session or None if not found."""
    conn = _open(db_path)
    try:
        row = conn.execute("SELECT * FROM ask_sessions WHERE id = ?", (session_id,)).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    return _row_to_session(row)


def ensure_session(
    session_id: str | None,
    *,
    scope: str = "portfolio",
    db_path: Path,
) -> AskSession:
    """Return an existing session (by id) or create a new one."""
    if session_id:
        sess = get_session(session_id, db_path=db_path)
        if sess is not None:
            return sess
    return create_session(scope=scope, db_path=db_path)


def list_sessions(
    *,
    scope: str = "portfolio",
    limit: int = 50,
    db_path: Path,
) -> list[AskSession]:
    """Return sessions for a scope, most-recently-updated first."""
    conn = _open(db_path)
    try:
        rows = conn.execute(
            "SELECT * FROM ask_sessions WHERE scope = ? ORDER BY updated_at DESC LIMIT ?",
            (scope, limit),
        ).fetchall()
    finally:
        conn.close()
    return [_row_to_session(r) for r in rows]


def rename_session(session_id: str, title: str, *, db_path: Path) -> bool:
    """Update the session title; returns True if a row was touched."""
    now = _now_iso()
    conn = _open(db_path)
    try:
        cur = conn.execute(
            "UPDATE ask_sessions SET title = ?, updated_at = ? WHERE id = ?",
            (title.strip(), now, session_id),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def delete_session(session_id: str, *, db_path: Path) -> bool:
    """Delete a session and its turns (CASCADE); returns True if deleted."""
    conn = _open(db_path)
    try:
        cur = conn.execute("DELETE FROM ask_sessions WHERE id = ?", (session_id,))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def touch_session(session_id: str, *, db_path: Path) -> None:
    """Update ask_sessions.updated_at to now (called after each new turn)."""
    conn = _open(db_path)
    try:
        conn.execute(
            "UPDATE ask_sessions SET updated_at = ? WHERE id = ?",
            (_now_iso(), session_id),
        )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Turn CRUD
# ---------------------------------------------------------------------------


def append_turn(
    *,
    session_id: str,
    role: str,
    text: str,
    citations: Sequence[object] | None = None,
    model: str | None = None,
    db_path: Path,
) -> None:
    """Append one turn to a session and bump the session's updated_at."""
    now = _now_iso()
    citations_json = json.dumps(citations) if citations is not None else None
    conn = _open(db_path)
    try:
        conn.execute(
            "INSERT INTO ask_turns"
            " (session_id, role, text, citations_json, model, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (session_id, role, text, citations_json, model, now),
        )
        conn.execute(
            "UPDATE ask_sessions SET updated_at = ? WHERE id = ?",
            (now, session_id),
        )
        conn.commit()
    finally:
        conn.close()


def load_turns(session_id: str, *, db_path: Path) -> list[AskTurnRow]:
    """Return all turns for a session in chronological order."""
    conn = _open(db_path)
    try:
        rows = conn.execute(
            "SELECT * FROM ask_turns WHERE session_id = ? ORDER BY id ASC",
            (session_id,),
        ).fetchall()
    finally:
        conn.close()
    return [_row_to_turn(r) for r in rows]


def load_recent_history(
    session_id: str,
    *,
    db_path: Path,
    max_turns: int = 20,
    max_chars: int = 1200,
) -> list[dict[str, str]]:
    """Return the most-recent N turns as ``[{"role", "text"}, …]`` — the
    same shape as the client-side tail — for injection into the engine's
    history.  Texts are truncated to max_chars.  The returned list is
    suitable for ``sanitize_history``; the caller typically uses it instead
    of the client-supplied tail when a session_id is present."""
    turns = load_turns(session_id, db_path=db_path)
    recent = turns[-max_turns:]
    return [{"role": t.role, "text": t.text[:max_chars]} for t in recent]


# ---------------------------------------------------------------------------
# Row decoders
# ---------------------------------------------------------------------------


def _row_to_session(row: sqlite3.Row) -> AskSession:
    return AskSession(
        id=str(row["id"]),
        scope=str(row["scope"]),
        title=str(row["title"]),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


def _row_to_turn(row: sqlite3.Row) -> AskTurnRow:
    raw_cites = row["citations_json"]
    citations: Sequence[object] | None = None
    if raw_cites:
        try:
            parsed = json.loads(raw_cites)
            citations = cast("Sequence[object]", parsed) if isinstance(parsed, list) else None
        except (json.JSONDecodeError, TypeError):
            citations = None
    raw_model = row["model"]
    return AskTurnRow(
        id=int(row["id"]),
        session_id=str(row["session_id"]),
        role=str(row["role"]),
        text=str(row["text"]),
        citations=citations,
        model=str(raw_model) if raw_model else None,
        created_at=str(row["created_at"]),
    )


__all__ = [
    "AskSession",
    "AskTurnRow",
    "append_turn",
    "create_session",
    "delete_session",
    "ensure_session",
    "get_session",
    "list_sessions",
    "load_recent_history",
    "load_turns",
    "rename_session",
    "touch_session",
]
