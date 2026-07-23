"""raw_capture_sessions CRUD (alembic 0116) — the TRANSIENT capture staging.

A session is the durable home of the raw words from capture until the structured
musing exists. The lifecycle:

  captured  — raw words stored (text immediately; voice after transcription)
  landed    — the analyst_notes musing was created; the transcript is BLANKED
              (raw is transient — privacy floor) and ``note_id`` set
  failed    — a fallible step (transcription) failed; the transcript/audio is
              RETAINED for retry so the user's words are never lost

Dedup is by ``(channel, external_ref)``: ``new_session`` returns None when the
source message was already ingested, so re-polling the same Telegram update is a
no-op.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from user_state._db import now_iso, open_conn


@dataclass(frozen=True, slots=True)
class SessionRow:
    id: int
    channel: str
    media_kind: str
    transcript: str
    status: str
    note_id: int | None
    external_ref: str | None
    total_llm_cost: float
    distilled_at: str | None = None


def _row_to_session(row: sqlite3.Row) -> SessionRow:
    # distilled_at (0190) is decoded defensively — a pre-0190 DB has no such
    # column, and this store must not crash reading an older fixture.
    keys = row.keys() if hasattr(row, "keys") else ()
    distilled_at = None
    if "distilled_at" in keys and row["distilled_at"] is not None:
        distilled_at = str(row["distilled_at"])
    return SessionRow(
        id=int(row["id"]),
        channel=str(row["channel"]),
        media_kind=str(row["media_kind"]),
        transcript=str(row["transcript"]),
        status=str(row["status"]),
        note_id=None if row["note_id"] is None else int(row["note_id"]),
        external_ref=None if row["external_ref"] is None else str(row["external_ref"]),
        total_llm_cost=float(row["total_llm_cost"]),
        distilled_at=distilled_at,
    )


def new_session(
    *,
    channel: str,
    media_kind: str = "text",
    transcript: str = "",
    external_ref: str | None = None,
    purge_after: str | None = None,
    db_path: Path | str | None = None,
) -> int | None:
    """Insert a 'captured' session; return its id, or None on a dedup conflict
    (this (channel, external_ref) was already ingested)."""
    conn = open_conn(db_path)
    try:
        now = now_iso()
        try:
            cur = conn.execute(
                """
                INSERT INTO raw_capture_sessions
                    (channel, media_kind, transcript, status, external_ref,
                     total_llm_cost, created_at, updated_at, purge_after)
                VALUES (?, ?, ?, 'captured', ?, 0, ?, ?, ?)
                """,
                (channel, media_kind, transcript, external_ref, now, now, purge_after),
            )
        except sqlite3.IntegrityError:
            return None  # ux_raw_capture_external — already ingested
        conn.commit()
        return int(cur.lastrowid or 0)
    finally:
        conn.close()


def set_transcript(session_id: int, transcript: str, *, db_path: Path | str | None = None) -> None:
    """Store the transcribed words on a voice session (still 'captured')."""
    conn = open_conn(db_path)
    try:
        conn.execute(
            "UPDATE raw_capture_sessions SET transcript = ?, updated_at = ? WHERE id = ?",
            (transcript, now_iso(), session_id),
        )
        conn.commit()
    finally:
        conn.close()


def mark_landed(
    session_id: int,
    *,
    note_id: int,
    total_llm_cost: float = 0.0,
    db_path: Path | str | None = None,
) -> None:
    """The musing landed: set note_id, status='landed', and BLANK the transcript
    (raw words are transient — only the structured note + audit log survive)."""
    conn = open_conn(db_path)
    try:
        conn.execute(
            """
            UPDATE raw_capture_sessions
            SET status = 'landed', note_id = ?, transcript = '',
                total_llm_cost = ?, updated_at = ?
            WHERE id = ?
            """,
            (note_id, float(total_llm_cost), now_iso(), session_id),
        )
        conn.commit()
    finally:
        conn.close()


def mark_failed(session_id: int, *, db_path: Path | str | None = None) -> None:
    """A fallible step failed; RETAIN the transcript/audio for retry."""
    conn = open_conn(db_path)
    try:
        conn.execute(
            "UPDATE raw_capture_sessions SET status = 'failed', updated_at = ? WHERE id = ?",
            (now_iso(), session_id),
        )
        conn.commit()
    finally:
        conn.close()


def get_session(session_id: int, *, db_path: Path | str | None = None) -> SessionRow | None:
    conn = open_conn(db_path)
    try:
        row = conn.execute(
            "SELECT * FROM raw_capture_sessions WHERE id = ?", (session_id,)
        ).fetchone()
        return None if row is None else _row_to_session(row)
    finally:
        conn.close()


def list_undistilled_captured(*, db_path: Path | str | None = None) -> list[SessionRow]:
    """Captured sessions (raw transcript still landed, e.g. a bridged Claude
    session — ``channel='claude_session'``) eligible for the session-distill
    sweep (B4): ``status='captured'``, never distilled, non-empty transcript."""
    conn = open_conn(db_path)
    try:
        rows = conn.execute(
            "SELECT * FROM raw_capture_sessions "
            "WHERE status = 'captured' AND distilled_at IS NULL "
            "AND transcript != '' ORDER BY created_at ASC"
        ).fetchall()
        return [_row_to_session(r) for r in rows]
    finally:
        conn.close()


def mark_distilled(session_id: int, *, db_path: Path | str | None = None) -> None:
    """The session-distill sweep finished with this session: stamp
    ``distilled_at`` and BLANK the transcript (the same privacy floor as
    ``mark_landed`` — raw words are transient once the durable artifact
    exists). ``status``/``note_id`` are left untouched: a bridged transcript
    session was never headed for its own musing note, so 'captured' stays the
    terminal status rather than borrowing 'landed' semantics that imply one."""
    conn = open_conn(db_path)
    try:
        conn.execute(
            "UPDATE raw_capture_sessions SET distilled_at = ?, transcript = '', "
            "updated_at = ? WHERE id = ?",
            (now_iso(), now_iso(), session_id),
        )
        conn.commit()
    finally:
        conn.close()
