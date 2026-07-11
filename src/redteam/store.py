"""``red_team_items`` CRUD — the Red Team engine's persisted-item store
(alembic 0147).

No FKs (repo-wide FK-poisoning invariant); naive-UTC TEXT stamps via
``user_state._db``. Read-tolerant like ``thesis_status``: a DB that predates
the migration degrades to empty reads rather than raising, so a panel render
never crashes on a stale DB.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import cast

from redteam.models import Kind, RedTeamItemRow, RedTeamLLMItem, Severity, Status
from user_state._db import now_iso, open_conn, parse_dt

_TABLE = "red_team_items"

_ROW_COLUMNS = (
    "id",
    "run_key",
    "ticker",
    "lens",
    "kind",
    "attack_md",
    "question_md",
    "proposed_change_md",
    "severity",
    "status",
    "defer_count",
    "response_md",
    "responded_at",
    "created_at",
)


def _row_to_dc(r: sqlite3.Row) -> RedTeamItemRow:
    return RedTeamItemRow(
        id=int(r["id"]),
        run_key=str(r["run_key"]),
        ticker=(str(r["ticker"]) if r["ticker"] is not None else None),
        lens=str(r["lens"]),
        kind=cast("Kind", str(r["kind"])),
        attack_md=str(r["attack_md"]),
        question_md=str(r["question_md"]),
        proposed_change_md=str(r["proposed_change_md"]),
        severity=cast("Severity", str(r["severity"])),
        status=cast("Status", str(r["status"])),
        defer_count=int(r["defer_count"] or 0),
        response_md=(str(r["response_md"]) if r["response_md"] is not None else None),
        responded_at=(parse_dt(r["responded_at"]) if r["responded_at"] is not None else None),
        created_at=parse_dt(r["created_at"]),
    )


def _has_table(conn: sqlite3.Connection) -> bool:
    try:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (_TABLE,)
        ).fetchone()
    except sqlite3.Error:
        return False
    return row is not None


def run_key_exists(*, db_path: Path | str | None, run_key: str) -> bool:
    """True iff at least one item already exists for ``run_key`` — the
    idempotency check ``engine.run_red_team`` consults before generating
    (skip-unless---force, per the directive's idempotency rule). Degrades to
    False on a missing DB/table so a fresh install doesn't false-block."""
    try:
        conn = open_conn(db_path)
    except (FileNotFoundError, RuntimeError):
        return False
    try:
        if not _has_table(conn):
            return False
        row = conn.execute(
            f"SELECT 1 FROM {_TABLE} WHERE run_key = ? LIMIT 1", (run_key,)
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def insert_item(
    *,
    db_path: Path | str | None,
    run_key: str,
    ticker: str | None,
    lens: str,
    kind: Kind,
    item: RedTeamLLMItem,
) -> int:
    """Persist one generated attack as a fresh ``status="open"`` row. Returns
    the new row id."""
    conn = open_conn(db_path)
    try:
        cur = conn.execute(
            f"INSERT INTO {_TABLE} "
            "(run_key, ticker, lens, kind, attack_md, question_md, "
            " proposed_change_md, severity, status, defer_count, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'open', 0, ?)",
            (
                run_key,
                ticker,
                lens,
                kind,
                item.attack_md,
                item.question_md,
                item.proposed_change_md,
                item.severity,
                now_iso(),
            ),
        )
        conn.commit()
        return int(cur.lastrowid or 0)
    finally:
        conn.close()


def list_items_for_run(*, db_path: Path | str | None, run_key: str) -> list[RedTeamItemRow]:
    """Every item for ``run_key``, oldest first. ``[]`` on a missing DB/table
    or an unknown run_key."""
    try:
        conn = open_conn(db_path)
    except (FileNotFoundError, RuntimeError):
        return []
    try:
        if not _has_table(conn):
            return []
        rows = conn.execute(
            f"SELECT {', '.join(_ROW_COLUMNS)} FROM {_TABLE} "
            "WHERE run_key = ? ORDER BY created_at ASC, id ASC",
            (run_key,),
        ).fetchall()
        return [_row_to_dc(r) for r in rows]
    finally:
        conn.close()


def latest_run_key(*, db_path: Path | str | None) -> str | None:
    """The most recently created run_key present in the table, or ``None``
    when the table is empty/absent — the panel's default scope."""
    try:
        conn = open_conn(db_path)
    except (FileNotFoundError, RuntimeError):
        return None
    try:
        if not _has_table(conn):
            return None
        row = conn.execute(
            f"SELECT run_key FROM {_TABLE} ORDER BY created_at DESC, id DESC LIMIT 1"
        ).fetchone()
        return str(row["run_key"]) if row is not None else None
    finally:
        conn.close()
