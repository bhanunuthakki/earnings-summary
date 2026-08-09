"""Read/write API for thesis_ledger_entries (alembic 0062).

Append-only by design. The ledger is the durable, time-ordered history of
every accepted thesis change — once a row lands, nothing in this module
can mutate or remove it. Renderers that want "current thesis" still read
the thesis_state / bear_case caches; renderers that want "how did the
thesis move over time" walk this table.

Surface intentionally narrow — ``append_entry`` for writes, ``list_entries``
for reads. There is no update or delete path; if a future requirement forces
a correction, it must be a new ledger row referencing the original (the
audit-trail invariant is too important to compromise for ergonomic edits).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from identity import DEFAULT_USER_ID
from user_state._db import now_iso, open_conn, open_read_conn, parse_dt


@dataclass(slots=True)
class ThesisLedgerEntryRow:
    """One row of thesis_ledger_entries, fully decoded."""

    id: int
    user_id: str
    ticker: str
    entry_kind: str
    body: str
    source_alert_id: int | None
    created_at: datetime
    accepted_at: datetime


def append_entry(
    *,
    user_id: str = DEFAULT_USER_ID,
    ticker: str,
    entry_kind: str,
    body: str,
    source_alert_id: int | None = None,
    db_path: Path | str | None = None,
) -> ThesisLedgerEntryRow:
    """INSERT one ledger row.

    ``created_at`` and ``accepted_at`` are set to the same now() timestamp:
    the existence of a ledger row IS the act of acceptance. The two columns
    are kept distinct in the schema so future flows (e.g. async two-step
    approval) can diverge them without a migration; for now they always
    agree.
    """
    conn = open_conn(db_path)
    try:
        now = now_iso()
        cur = conn.execute(
            """
            INSERT INTO thesis_ledger_entries(
                user_id, ticker, entry_kind, body, source_alert_id,
                created_at, accepted_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (user_id, ticker, entry_kind, body, source_alert_id, now, now),
        )
        row_id = int(cur.lastrowid or 0)
        conn.commit()
        return _fetch_one(conn, row_id)
    finally:
        conn.close()


def list_entries(
    *,
    user_id: str = DEFAULT_USER_ID,
    ticker: str,
    entry_kind: str | None = None,
    limit: int = 100,
    db_path: Path | str | None = None,
) -> list[ThesisLedgerEntryRow]:
    """Newest-first ledger entries for ``ticker``, optionally filtered to one ``entry_kind``.

    Keyword-only because ``ticker`` is required and the interleaved
    ``user_id`` default would make positional calls ambiguous. ``limit``
    defaults to 100 — enough for the dashboard's "ledger" panel without
    unbounded reads on tickers with deep history.
    """
    conn = open_conn(db_path)
    try:
        if entry_kind is None:
            rows = conn.execute(
                """
                SELECT * FROM thesis_ledger_entries
                WHERE user_id = ? AND ticker = ?
                ORDER BY created_at DESC, id DESC
                LIMIT ?
                """,
                (user_id, ticker, int(limit)),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT * FROM thesis_ledger_entries
                WHERE user_id = ? AND ticker = ? AND entry_kind = ?
                ORDER BY created_at DESC, id DESC
                LIMIT ?
                """,
                (user_id, ticker, entry_kind, int(limit)),
            ).fetchall()
        return [_row_to_dc(r) for r in rows]
    finally:
        conn.close()


def list_recent_entries(
    *,
    user_id: str = DEFAULT_USER_ID,
    limit: int = 20,
    db_path: Path | str | None = None,
    conn: sqlite3.Connection | None = None,
) -> list[ThesisLedgerEntryRow]:
    """Newest-first ledger entries across ALL tickers for ``user_id``.

    Powers the digest's cross-holding "recent thesis changes" panel — the
    append-only history of every accepted, alert-driven thesis edit, which
    otherwise has no reader on any surface. ``limit`` keeps the panel bounded.
    """
    db_conn = conn or open_read_conn(db_path)
    try:
        rows = db_conn.execute(
            """
            SELECT * FROM thesis_ledger_entries
            WHERE user_id = ?
            ORDER BY created_at DESC, id DESC
            LIMIT ?
            """,
            (user_id, int(limit)),
        ).fetchall()
        return [_row_to_dc(r) for r in rows]
    finally:
        if conn is None:
            db_conn.close()


def _fetch_one(conn: sqlite3.Connection, row_id: int) -> ThesisLedgerEntryRow:
    row = conn.execute("SELECT * FROM thesis_ledger_entries WHERE id = ?", (row_id,)).fetchone()
    if row is None:
        raise LookupError(f"thesis_ledger_entries id={row_id} not found after write")
    return _row_to_dc(row)


def _row_to_dc(row: sqlite3.Row) -> ThesisLedgerEntryRow:
    raw_source = row["source_alert_id"]
    return ThesisLedgerEntryRow(
        id=int(row["id"]),
        user_id=str(row["user_id"]),
        ticker=str(row["ticker"]),
        entry_kind=str(row["entry_kind"]),
        body=str(row["body"]),
        source_alert_id=(None if raw_source is None else int(raw_source)),
        created_at=parse_dt(row["created_at"]),
        accepted_at=parse_dt(row["accepted_at"]),
    )
