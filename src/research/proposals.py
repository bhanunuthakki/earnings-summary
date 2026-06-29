"""research_tasks / research_proposals store + the fire-and-forget detection tap.

Follows the codebase store convention (writers own a connection + commit; readers
best-effort ``[]`` on a missing table, the ``signals/store.py`` pattern). It owns
the two Phase-1 lifecycles and exposes ``act_on_proposal`` — the ONE action core
both the inbox HTMX route and the Telegram callback dispatch call (no logic
duplication).

The detection tap (``detect_and_create_task``) is fire-and-forget: it classifies
a freshly-landed musing and, on a wondering, writes a ``proposed`` task (the inert
chip). It NEVER auto-runs the research pass, and a detection failure NEVER affects
capture (it swallows errors and returns None).
"""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from research.detect import DetectCall, detect_wondering
from user_state._db import now_iso, open_conn
from user_state.notes import get_note

TASK_STATUSES: tuple[str, ...] = (
    "proposed",
    "running",
    "drafted",
    "approved",
    "rejected",
    "superseded",
)
PROPOSAL_VERBS: tuple[str, ...] = ("approve", "further", "steer", "reject")

# The detection tap runs by default (the regex pre-gate keeps it cheap — only
# wondering-shaped musings reach the LLM). Set LEDGER_RESEARCH_TAP=0 to disable.
_TAP_OFF = frozenset({"0", "false", "no", ""})


def tap_enabled() -> bool:
    return os.environ.get("LEDGER_RESEARCH_TAP", "1").strip().lower() not in _TAP_OFF


@dataclass(frozen=True, slots=True)
class ResearchTask:
    id: int
    note_id: int | None
    claim: str
    ticker: str | None
    status: str


def create_task(
    *, note_id: int | None, claim: str, ticker: str | None, db_path: Path | str | None = None
) -> int:
    conn = open_conn(db_path)
    try:
        now = now_iso()
        cur = conn.execute(
            "INSERT INTO research_tasks (note_id, claim, ticker, status, created_at, updated_at) "
            "VALUES (?, ?, ?, 'proposed', ?, ?)",
            (note_id, claim, ticker, now, now),
        )
        conn.commit()
        return int(cur.lastrowid or 0)
    finally:
        conn.close()


def _row_to_task(row: sqlite3.Row) -> ResearchTask:
    return ResearchTask(
        id=int(row["id"]),
        note_id=None if row["note_id"] is None else int(row["note_id"]),
        claim=str(row["claim"]),
        ticker=None if row["ticker"] is None else str(row["ticker"]),
        status=str(row["status"]),
    )


def get_task(task_id: int, *, db_path: Path | str | None = None) -> ResearchTask | None:
    conn = open_conn(db_path)
    try:
        row = conn.execute("SELECT * FROM research_tasks WHERE id = ?", (task_id,)).fetchone()
        return None if row is None else _row_to_task(row)
    finally:
        conn.close()


def list_tasks(
    *, status: str | None = None, db_path: Path | str | None = None
) -> list[ResearchTask]:
    conn = open_conn(db_path)
    try:
        if status is not None:
            rows = conn.execute(
                "SELECT * FROM research_tasks WHERE status = ? ORDER BY id DESC", (status,)
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM research_tasks ORDER BY id DESC").fetchall()
        return [_row_to_task(r) for r in rows]
    except sqlite3.Error:
        return []
    finally:
        conn.close()


def set_task_status(task_id: int, status: str, *, db_path: Path | str | None = None) -> None:
    if status not in TASK_STATUSES:
        raise ValueError(f"unknown task status {status!r}; expected one of {TASK_STATUSES}")
    conn = open_conn(db_path)
    try:
        conn.execute(
            "UPDATE research_tasks SET status = ?, updated_at = ? WHERE id = ?",
            (status, now_iso(), task_id),
        )
        conn.commit()
    finally:
        conn.close()


def detect_and_create_task(
    note_id: int, *, db_path: Path | str | None = None, call: DetectCall | None = None
) -> int | None:
    """The fire-and-forget tap: classify a freshly-landed musing; on a wondering,
    create a ``proposed`` task (the inert chip). Returns the task id or None.
    NEVER raises — a detection failure must not affect capture."""
    note = get_note(note_id, db_path=db_path)
    if note is None or note.kind != "musing":
        return None
    try:
        # Captured musings are owner-authored — provenance 'derived'; the
        # 'contains_fetched' inert marker is only ever on research proposals.
        verdict = detect_wondering(note.body, kind=note.kind, provenance="derived", call=call)
    except Exception:
        return None
    if not verdict.is_wondering:
        return None
    claim = verdict.claim.strip() or note.body[:200]
    return create_task(note_id=note_id, claim=claim, ticker=verdict.ticker, db_path=db_path)
