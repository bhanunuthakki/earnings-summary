"""Read/write API for position_sizing_intent (alembic 0061).

Sizing intents are *time-stamped history* — every call to ``append_intent``
adds a new row. The dashboard's "current sizing posture" view is the
caller's job: typically ``latest_intent(ticker, intent_kind)`` for each
kind it cares about.

Why append-only:
  the user changes their sizing posture over time ("target 5% → 4% after
  the Q3 print"), and the audit trail of when each change happened is
  itself thesis-relevant. An UPDATE-in-place would destroy that history.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from identity import DEFAULT_USER_ID
from user_state._db import now_iso, open_conn, parse_dt


@dataclass(slots=True)
class PositionSizingIntentRow:
    """One row of position_sizing_intent, fully decoded."""

    id: int
    user_id: str
    ticker: str
    intent_kind: str
    intent_value: float | None
    narrative: str | None
    created_at: datetime
    updated_at: datetime


def append_intent(
    *,
    user_id: str = DEFAULT_USER_ID,
    ticker: str,
    intent_kind: str,
    intent_value: float | None = None,
    narrative: str | None = None,
    db_path: Path | str | None = None,
) -> PositionSizingIntentRow:
    """Insert a new sizing intent row. Never updates existing rows — the table
    is the per-user, per-ticker history of stated sizing posture.

    ``created_at`` and ``updated_at`` are set to the same now() timestamp on
    insert; ``updated_at`` is reserved for a future amend-row path that this
    PR doesn't define.
    """
    conn = open_conn(db_path)
    try:
        now = now_iso()
        cur = conn.execute(
            """
            INSERT INTO position_sizing_intent(
                user_id, ticker, intent_kind, intent_value, narrative,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (user_id, ticker, intent_kind, intent_value, narrative, now, now),
        )
        row_id = int(cur.lastrowid or 0)
        conn.commit()
        return _fetch_one(conn, row_id)
    finally:
        conn.close()


def list_intents(
    user_id: str = DEFAULT_USER_ID,
    ticker: str | None = None,
    db_path: Path | str | None = None,
) -> list[PositionSizingIntentRow]:
    """Return sizing-intent rows newest first.

    Filtered by ``user_id`` always, and by ``ticker`` when given.
    """
    conn = open_conn(db_path)
    try:
        if ticker is None:
            rows = conn.execute(
                "SELECT * FROM position_sizing_intent WHERE user_id = ? "
                "ORDER BY created_at DESC, id DESC",
                (user_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM position_sizing_intent WHERE user_id = ? AND ticker = ? "
                "ORDER BY created_at DESC, id DESC",
                (user_id, ticker),
            ).fetchall()
        return [_row_to_dc(r) for r in rows]
    finally:
        conn.close()


def latest_intent(
    *,
    user_id: str = DEFAULT_USER_ID,
    ticker: str,
    intent_kind: str,
    db_path: Path | str | None = None,
) -> PositionSizingIntentRow | None:
    """Most recent intent of ``intent_kind`` for ``ticker``, or None if none exists.

    Keyword-only because ``ticker`` and ``intent_kind`` are required and the
    interleaved ``user_id`` default makes positional calls ambiguous.
    """
    conn = open_conn(db_path)
    try:
        row = conn.execute(
            """
            SELECT * FROM position_sizing_intent
            WHERE user_id = ? AND ticker = ? AND intent_kind = ?
            ORDER BY created_at DESC, id DESC
            LIMIT 1
            """,
            (user_id, ticker, intent_kind),
        ).fetchone()
        return None if row is None else _row_to_dc(row)
    finally:
        conn.close()


def _fetch_one(conn: sqlite3.Connection, row_id: int) -> PositionSizingIntentRow:
    row = conn.execute("SELECT * FROM position_sizing_intent WHERE id = ?", (row_id,)).fetchone()
    if row is None:
        raise LookupError(f"position_sizing_intent id={row_id} not found after write")
    return _row_to_dc(row)


def _row_to_dc(row: sqlite3.Row) -> PositionSizingIntentRow:
    raw_intent_value = row["intent_value"]
    return PositionSizingIntentRow(
        id=int(row["id"]),
        user_id=str(row["user_id"]),
        ticker=str(row["ticker"]),
        intent_kind=str(row["intent_kind"]),
        intent_value=(None if raw_intent_value is None else float(raw_intent_value)),
        narrative=(None if row["narrative"] is None else str(row["narrative"])),
        created_at=parse_dt(row["created_at"]),
        updated_at=parse_dt(row["updated_at"]),
    )
