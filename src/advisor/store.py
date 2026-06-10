"""Read/write API for advisor_memos (alembic 0077).

One row per generated advisor artifact — next-dollar allocation memos
(portfolio-level), swap-discipline checks (holding vs watchlist alternative),
and, from P2.4, Socratic decision memos (the only kind that carries a
stance + horizon). Rows are dated records, never caches: a re-run appends a
new memo rather than replacing the old one, because the history of what the
advisor said — and when — is exactly what the P2.5 scorecard grades.

Follows the ``user_state`` CRUD conventions: keyword-only writes, loud
failure on a missing substrate (``user_state._db.open_conn``), ISO-text
timestamps via ``now_iso``.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import cast

from identity import DEFAULT_USER_ID
from user_state._db import now_iso, open_conn, parse_dt

MEMO_KINDS: tuple[str, ...] = ("next_dollar", "swap_check", "socratic")
SCORE_STATUSES: tuple[str, ...] = ("pending", "scored", "unscoreable")
STANCES: tuple[str, ...] = ("buy", "add", "hold", "trim", "sell")


@dataclass(slots=True)
class AdvisorMemoRow:
    """One row of advisor_memos, fully decoded."""

    id: int
    user_id: str
    kind: str
    ticker: str | None
    counter_ticker: str | None
    title: str
    body_md: str
    context: dict[str, object] | None
    stance: str | None
    horizon_days: int | None
    score_status: str
    note_id: int | None
    ledger_entry_id: int | None
    created_at: datetime


def insert_memo(
    *,
    user_id: str = DEFAULT_USER_ID,
    kind: str,
    ticker: str | None,
    title: str,
    body_md: str,
    counter_ticker: str | None = None,
    context: dict[str, object] | None = None,
    stance: str | None = None,
    horizon_days: int | None = None,
    note_id: int | None = None,
    ledger_entry_id: int | None = None,
    db_path: Path | str | None = None,
) -> AdvisorMemoRow:
    """INSERT one memo row. Validates the enums Python-side so callers fail
    with a readable message before the schema CHECK does."""
    if kind not in MEMO_KINDS:
        raise ValueError(f"kind must be one of {MEMO_KINDS}, got {kind!r}")
    if stance is not None and stance not in STANCES:
        raise ValueError(f"stance must be one of {STANCES} or None, got {stance!r}")
    if not title.strip() or not body_md.strip():
        raise ValueError("title and body_md must be non-empty")
    conn = open_conn(db_path)
    try:
        cur = conn.execute(
            """
            INSERT INTO advisor_memos(
                user_id, kind, ticker, counter_ticker, title, body_md,
                context_json, stance, horizon_days, score_status,
                note_id, ledger_entry_id, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)
            """,
            (
                user_id,
                kind,
                ticker.upper() if ticker else None,
                counter_ticker.upper() if counter_ticker else None,
                title,
                body_md,
                json.dumps(context) if context is not None else None,
                stance,
                horizon_days,
                note_id,
                ledger_entry_id,
                now_iso(),
            ),
        )
        row_id = int(cur.lastrowid or 0)
        conn.commit()
        return _fetch_one(conn, row_id)
    finally:
        conn.close()


def list_memos(
    *,
    user_id: str = DEFAULT_USER_ID,
    kind: str | None = None,
    ticker: str | None = None,
    limit: int = 50,
    db_path: Path | str | None = None,
) -> list[AdvisorMemoRow]:
    """Newest-first memos, optionally filtered by kind and/or ticker."""
    where = ["user_id = ?"]
    params: list[object] = [user_id]
    if kind is not None:
        where.append("kind = ?")
        params.append(kind)
    if ticker is not None:
        where.append("ticker = ?")
        params.append(ticker.upper())
    params.append(int(limit))
    conn = open_conn(db_path)
    try:
        rows = conn.execute(
            f"SELECT * FROM advisor_memos WHERE {' AND '.join(where)} "
            "ORDER BY created_at DESC, id DESC LIMIT ?",
            tuple(params),
        ).fetchall()
        return [_row_to_dc(r) for r in rows]
    finally:
        conn.close()


def get_memo(memo_id: int, *, db_path: Path | str | None = None) -> AdvisorMemoRow | None:
    """One memo by id, or None."""
    conn = open_conn(db_path)
    try:
        row = conn.execute("SELECT * FROM advisor_memos WHERE id = ?", (memo_id,)).fetchone()
        return None if row is None else _row_to_dc(row)
    finally:
        conn.close()


def link_memory(
    memo_id: int,
    *,
    note_id: int | None = None,
    ledger_entry_id: int | None = None,
    db_path: Path | str | None = None,
) -> None:
    """Backfill the memory backlinks after the note/ledger writes (the memo row
    is inserted first so those writes can reference its id in their bodies)."""
    sets: list[str] = []
    params: list[object] = []
    if note_id is not None:
        sets.append("note_id = ?")
        params.append(note_id)
    if ledger_entry_id is not None:
        sets.append("ledger_entry_id = ?")
        params.append(ledger_entry_id)
    if not sets:
        return
    params.append(memo_id)
    conn = open_conn(db_path)
    try:
        conn.execute(
            f"UPDATE advisor_memos SET {', '.join(sets)} WHERE id = ?",
            tuple(params),
        )
        conn.commit()
    finally:
        conn.close()


def _fetch_one(conn: sqlite3.Connection, row_id: int) -> AdvisorMemoRow:
    row = conn.execute("SELECT * FROM advisor_memos WHERE id = ?", (row_id,)).fetchone()
    if row is None:
        raise LookupError(f"advisor_memos id={row_id} not found after write")
    return _row_to_dc(row)


def _row_to_dc(row: sqlite3.Row) -> AdvisorMemoRow:
    raw_ctx = row["context_json"]
    context: dict[str, object] | None = None
    if raw_ctx:
        try:
            parsed = json.loads(str(raw_ctx))
            if isinstance(parsed, dict):
                context = cast("dict[str, object]", parsed)
        except json.JSONDecodeError:
            context = None
    return AdvisorMemoRow(
        id=int(row["id"]),
        user_id=str(row["user_id"]),
        kind=str(row["kind"]),
        ticker=(None if row["ticker"] is None else str(row["ticker"])),
        counter_ticker=(None if row["counter_ticker"] is None else str(row["counter_ticker"])),
        title=str(row["title"]),
        body_md=str(row["body_md"]),
        context=context,
        stance=(None if row["stance"] is None else str(row["stance"])),
        horizon_days=(None if row["horizon_days"] is None else int(row["horizon_days"])),
        score_status=str(row["score_status"]),
        note_id=(None if row["note_id"] is None else int(row["note_id"])),
        ledger_entry_id=(None if row["ledger_entry_id"] is None else int(row["ledger_entry_id"])),
        created_at=parse_dt(row["created_at"]),
    )
