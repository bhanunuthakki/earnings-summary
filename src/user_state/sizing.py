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


@dataclass(slots=True)
class PositionSizingIntentWithdrawalRow:
    """One immutable owner withdrawal of a previously recorded sizing intent."""

    id: int
    user_id: str
    sizing_intent_id: int
    reason: str
    created_at: datetime


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
        row_id = _insert_intent(
            conn,
            user_id=user_id,
            ticker=ticker,
            intent_kind=intent_kind,
            intent_value=intent_value,
            narrative=narrative,
        )
        conn.commit()
        return _fetch_one(conn, row_id)
    finally:
        conn.close()


def list_intents(
    user_id: str = DEFAULT_USER_ID,
    ticker: str | None = None,
    include_withdrawn: bool = False,
    include_superseded: bool = False,
    db_path: Path | str | None = None,
) -> list[PositionSizingIntentRow]:
    """Return sizing-intent rows newest first.

    Filtered by ``user_id`` always, and by ``ticker`` when given. Withdrawn
    and superseded rows are absent from the active projection unless explicitly
    requested for audit/history views.
    """
    conn = open_conn(db_path)
    try:
        exclusion = _inactive_exclusion(
            conn,
            include_withdrawn=include_withdrawn,
            include_superseded=include_superseded,
        )
        if ticker is None:
            rows = conn.execute(
                "SELECT * FROM position_sizing_intent WHERE user_id = ? "
                f"{exclusion} "
                "ORDER BY created_at DESC, id DESC",
                (user_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM position_sizing_intent WHERE user_id = ? AND ticker = ? "
                f"{exclusion} "
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
        exclusion = _inactive_exclusion(
            conn,
            include_withdrawn=False,
            include_superseded=False,
        )
        row = conn.execute(
            f"""
            SELECT * FROM position_sizing_intent
            WHERE user_id = ? AND ticker = ? AND intent_kind = ?
            {exclusion}
            ORDER BY created_at DESC, id DESC
            LIMIT 1
            """,
            (user_id, ticker, intent_kind),
        ).fetchone()
        return None if row is None else _row_to_dc(row)
    finally:
        conn.close()


def supersede_intents(
    *,
    user_id: str = DEFAULT_USER_ID,
    ticker: str,
    intent_kind: str,
    supersedes_intent_ids: tuple[int, ...],
    reason: str,
    intent_value: float | None = None,
    narrative: str | None = None,
    db_path: Path | str | None = None,
) -> PositionSizingIntentRow:
    """Append a current intent and explicitly retire the rows it consolidates.

    Old rows remain immutable and available through ``include_superseded``.
    A prior row may be superseded only once, preventing competing current
    histories from silently claiming the same evidence.
    """
    old_ids = tuple(dict.fromkeys(int(row_id) for row_id in supersedes_intent_ids))
    if not old_ids:
        raise ValueError("at least one superseded sizing intent is required")
    clean_reason = reason.strip()
    if not clean_reason:
        raise ValueError("supersession reason is required")
    normalized_ticker = ticker.upper()
    conn = open_conn(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        placeholders = ",".join("?" for _ in old_ids)
        rows = conn.execute(
            f"SELECT id,user_id,ticker FROM position_sizing_intent WHERE id IN ({placeholders})",
            old_ids,
        ).fetchall()
        if {int(row["id"]) for row in rows} != set(old_ids):
            raise LookupError("one or more sizing intents to supersede do not exist")
        if any(
            str(row["user_id"]) != user_id or str(row["ticker"]).upper() != normalized_ticker
            for row in rows
        ):
            raise ValueError("superseded sizing intents must belong to the same owner and ticker")
        already = conn.execute(
            f"SELECT superseded_intent_id FROM position_sizing_intent_supersessions "
            f"WHERE user_id=? AND superseded_intent_id IN ({placeholders})",
            (user_id, *old_ids),
        ).fetchall()
        if already:
            claimed = sorted(int(row["superseded_intent_id"]) for row in already)
            raise ValueError(f"sizing intents already superseded: {claimed}")
        current_id = _insert_intent(
            conn,
            user_id=user_id,
            ticker=normalized_ticker,
            intent_kind=intent_kind,
            intent_value=intent_value,
            narrative=narrative,
        )
        created_at = now_iso()
        conn.executemany(
            """
            INSERT INTO position_sizing_intent_supersessions(
                user_id,superseded_intent_id,superseding_intent_id,reason,created_at
            ) VALUES (?,?,?,?,?)
            """,
            [(user_id, old_id, current_id, clean_reason, created_at) for old_id in old_ids],
        )
        conn.commit()
        return _fetch_one(conn, current_id)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def withdraw_intent(
    *,
    user_id: str = DEFAULT_USER_ID,
    sizing_intent_id: int,
    reason: str,
    db_path: Path | str | None = None,
) -> PositionSizingIntentWithdrawalRow:
    """Withdraw one intent from active projections without deleting its history.

    The unique owner/intent key makes retries idempotent. A repeated request
    returns the existing immutable withdrawal receipt.
    """
    clean_reason = reason.strip()
    if not clean_reason:
        raise ValueError("withdrawal reason is required")
    conn = open_conn(db_path)
    try:
        intent = conn.execute(
            "SELECT id FROM position_sizing_intent WHERE id=? AND user_id=?",
            (int(sizing_intent_id), user_id),
        ).fetchone()
        if intent is None:
            raise LookupError(
                f"position_sizing_intent id={sizing_intent_id} not found for user {user_id!r}"
            )
        existing = conn.execute(
            "SELECT * FROM position_sizing_intent_withdrawals "
            "WHERE user_id=? AND sizing_intent_id=?",
            (user_id, int(sizing_intent_id)),
        ).fetchone()
        if existing is not None:
            return _withdrawal_row_to_dc(existing)
        now = now_iso()
        cur = conn.execute(
            """
            INSERT INTO position_sizing_intent_withdrawals(
                user_id,sizing_intent_id,reason,created_at
            ) VALUES (?,?,?,?)
            """,
            (user_id, int(sizing_intent_id), clean_reason, now),
        )
        withdrawal_id = int(cur.lastrowid or 0)
        conn.commit()
        row = conn.execute(
            "SELECT * FROM position_sizing_intent_withdrawals WHERE id=?",
            (withdrawal_id,),
        ).fetchone()
        if row is None:
            raise LookupError(f"sizing-intent withdrawal id={withdrawal_id} missing after write")
        return _withdrawal_row_to_dc(row)
    finally:
        conn.close()


def _inactive_exclusion(
    conn: sqlite3.Connection,
    *,
    include_withdrawn: bool,
    include_superseded: bool,
) -> str:
    fragments: list[str] = []
    if not include_withdrawn and _table_exists(conn, "position_sizing_intent_withdrawals"):
        fragments.append(
            "AND NOT EXISTS (SELECT 1 FROM position_sizing_intent_withdrawals AS withdrawal "
            "WHERE withdrawal.user_id=position_sizing_intent.user_id "
            "AND withdrawal.sizing_intent_id=position_sizing_intent.id)"
        )
    if not include_superseded and _table_exists(conn, "position_sizing_intent_supersessions"):
        fragments.append(
            "AND NOT EXISTS (SELECT 1 FROM position_sizing_intent_supersessions AS supersession "
            "WHERE supersession.user_id=position_sizing_intent.user_id "
            "AND supersession.superseded_intent_id=position_sizing_intent.id)"
        )
    return " ".join(fragments)


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (name,),
        ).fetchone()
        is not None
    )


def _insert_intent(
    conn: sqlite3.Connection,
    *,
    user_id: str,
    ticker: str,
    intent_kind: str,
    intent_value: float | None,
    narrative: str | None,
) -> int:
    now = now_iso()
    cur = conn.execute(
        """
        INSERT INTO position_sizing_intent(
            user_id, ticker, intent_kind, intent_value, narrative,
            created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (user_id, ticker.upper(), intent_kind, intent_value, narrative, now, now),
    )
    return int(cur.lastrowid or 0)


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


def _withdrawal_row_to_dc(row: sqlite3.Row) -> PositionSizingIntentWithdrawalRow:
    return PositionSizingIntentWithdrawalRow(
        id=int(row["id"]),
        user_id=str(row["user_id"]),
        sizing_intent_id=int(row["sizing_intent_id"]),
        reason=str(row["reason"]),
        created_at=parse_dt(row["created_at"]),
    )
