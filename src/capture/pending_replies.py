"""The shared awaited-free-text-reply stash (PR2 — Sunday packet + decision nudge).

Two producers stage a "the next thing you type here means something specific"
state instead of the default musing-capture route: the decision nudge's "Fill
in now" button (conviction + falsifier, two lines) and the Sunday packet's
"Rewrite" button (an owner-authored replacement for a reconcile/tenet/proposal
item). Rather than build the same await-state machinery twice, both stash a
row here; the poller's text handler checks ONE table, for ONE chat, before
falling through to ``ingest.ingest_capture`` — so capture can never be
permanently hijacked by a stale or abandoned await (a 24h expiry always wins).

Design constraints that shaped this:
  * one row per (chat_id, kind, ref_id) at a time — a stray double-tap of
    "Fill in now" must not stack two awaits for the same target;
  * "newest, unconsumed, unexpired" is the ONLY resolution rule — no attempt
    to disambiguate multiple concurrent awaits across different targets (a
    single-user localhost bot; the owner acts on one card at a time in
    practice, and an expired/consumed await is simply skipped);
  * consuming a row is explicit (:func:`consume`) so a caller can inspect
    before committing to using it (e.g. re-validate the reply is well-formed).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

from user_state._db import now_iso, now_naive_utc, open_conn

DEFAULT_EXPIRY_HOURS = 24


@dataclass(frozen=True, slots=True)
class PendingReply:
    id: int
    chat_id: int
    kind: str
    ref_id: int
    expires_at: str


def stash(
    chat_id: int,
    kind: str,
    ref_id: int,
    *,
    expiry_hours: float = DEFAULT_EXPIRY_HOURS,
    db_path: Path | str | None = None,
) -> int:
    """Record an await for this chat. Any prior unconsumed await for the same
    ``(chat_id, kind, ref_id)`` is superseded (deleted) first — a re-tap of
    the same button never stacks duplicate awaits."""
    expires_at = (now_naive_utc() + timedelta(hours=expiry_hours)).isoformat()
    conn = open_conn(db_path)
    try:
        conn.execute(
            "DELETE FROM pending_telegram_replies "
            "WHERE chat_id = ? AND kind = ? AND ref_id = ? AND consumed_at IS NULL",
            (chat_id, kind, ref_id),
        )
        cur = conn.execute(
            "INSERT INTO pending_telegram_replies "
            "(chat_id, kind, ref_id, expires_at, created_at) VALUES (?, ?, ?, ?, ?)",
            (chat_id, kind, ref_id, expires_at, now_iso()),
        )
        conn.commit()
        return int(cur.lastrowid or 0)
    finally:
        conn.close()


def peek(chat_id: int, *, db_path: Path | str | None = None) -> PendingReply | None:
    """The newest unconsumed, unexpired await for this chat, or None. Read-only —
    callers that will act on it should :func:`consume` afterward."""
    now = now_iso()
    conn = open_conn(db_path)
    try:
        row = conn.execute(
            "SELECT id, chat_id, kind, ref_id, expires_at FROM pending_telegram_replies "
            "WHERE chat_id = ? AND consumed_at IS NULL AND expires_at >= ? "
            "ORDER BY id DESC LIMIT 1",
            (chat_id, now),
        ).fetchone()
        return (
            None
            if row is None
            else PendingReply(
                id=int(row[0]),
                chat_id=int(row[1]),
                kind=str(row[2]),
                ref_id=int(row[3]),
                expires_at=str(row[4]),
            )
        )
    finally:
        conn.close()


def consume(reply_id: int, *, db_path: Path | str | None = None) -> None:
    """Mark an await handled so it is never matched again (idempotent)."""
    conn = open_conn(db_path)
    try:
        conn.execute(
            "UPDATE pending_telegram_replies SET consumed_at = ? "
            "WHERE id = ? AND consumed_at IS NULL",
            (now_iso(), reply_id),
        )
        conn.commit()
    finally:
        conn.close()


__all__ = ["DEFAULT_EXPIRY_HOURS", "PendingReply", "consume", "peek", "stash"]
