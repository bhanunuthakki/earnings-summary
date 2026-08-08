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
from user_state._db import now_iso, open_conn, open_read_conn, parse_dt

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


_RESPONDED_STATUSES = ("refuted", "accepted", "deferred")


def list_responded_items(*, db_path: Path | str | None) -> list[RedTeamItemRow]:
    """Every item across ALL runs that has been responded to (status in
    refuted/accepted/deferred), oldest-response-first — the population
    ``redteam.decision_pnl`` scores. ``closed`` items are excluded (closed is
    a terminal administrative state distinct from "responded"; PR6 does not
    define what closes an item, so this store makes no assumption about it).
    ``[]`` on a missing DB/table, matching the store's read-tolerant posture."""
    try:
        conn = open_conn(db_path)
    except (FileNotFoundError, RuntimeError):
        return []
    try:
        if not _has_table(conn):
            return []
        marks = ",".join("?" for _ in _RESPONDED_STATUSES)
        rows = conn.execute(
            f"SELECT {', '.join(_ROW_COLUMNS)} FROM {_TABLE} "
            f"WHERE status IN ({marks}) AND responded_at IS NOT NULL "
            "ORDER BY responded_at ASC, id ASC",
            _RESPONDED_STATUSES,
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


def get_item(*, db_path: Path | str | None, item_id: int) -> RedTeamItemRow | None:
    """One item by id, or ``None`` when missing / the table/DB doesn't exist
    yet — the forced-response loop's (PR6) read before every transition."""
    try:
        conn = open_conn(db_path)
    except (FileNotFoundError, RuntimeError):
        return None
    try:
        if not _has_table(conn):
            return None
        row = conn.execute(
            f"SELECT {', '.join(_ROW_COLUMNS)} FROM {_TABLE} WHERE id = ?", (item_id,)
        ).fetchone()
        return None if row is None else _row_to_dc(row)
    finally:
        conn.close()


def list_items_by_status(
    *,
    db_path: Path | str | None,
    statuses: tuple[str, ...],
    conn: sqlite3.Connection | None = None,
) -> list[RedTeamItemRow]:
    """Every item across ALL run_keys currently in one of ``statuses``,
    oldest first. Powers ``redteam.gate.escalated_items`` — the persistent
    Home-band nag doesn't scope to the latest run, since a deferred item from
    an older month is still unresolved. ``[]`` on a missing DB/table."""
    if not statuses:
        return []
    try:
        db_conn = conn or open_read_conn(db_path)
    except (FileNotFoundError, RuntimeError):
        return []
    try:
        if not _has_table(db_conn):
            return []
        placeholders = ", ".join("?" * len(statuses))
        rows = db_conn.execute(
            f"SELECT {', '.join(_ROW_COLUMNS)} FROM {_TABLE} "
            f"WHERE status IN ({placeholders}) ORDER BY created_at ASC, id ASC",
            statuses,
        ).fetchall()
        return [_row_to_dc(r) for r in rows]
    finally:
        if conn is None:
            db_conn.close()


def set_response(
    *,
    db_path: Path | str | None,
    item_id: int,
    status: Status,
    response_md: str | None,
) -> RedTeamItemRow | None:
    """Persist a terminal response (``status='refuted'`` or ``'accepted'``):
    writes ``status``, ``response_md``, and stamps ``responded_at``. Returns
    the updated row, or ``None`` when ``item_id`` doesn't exist."""
    conn = open_conn(db_path)
    try:
        cur = conn.execute(
            f"UPDATE {_TABLE} SET status = ?, response_md = ?, responded_at = ? WHERE id = ?",
            (status, response_md, now_iso(), item_id),
        )
        if cur.rowcount == 0:
            return None
        conn.commit()
        row = conn.execute(
            f"SELECT {', '.join(_ROW_COLUMNS)} FROM {_TABLE} WHERE id = ?", (item_id,)
        ).fetchone()
        return None if row is None else _row_to_dc(row)
    finally:
        conn.close()


def increment_defer(*, db_path: Path | str | None, item_id: int) -> RedTeamItemRow | None:
    """The FIRST defer only: flips an ``open`` item to ``status='deferred'``
    and bumps ``defer_count`` 0->1 in one guarded UPDATE (``WHERE status =
    'open'``). Returns ``None`` when ``item_id`` is missing OR the item isn't
    currently ``open`` (already deferred/refuted/accepted/closed) — the
    caller (``redteam.response._defer``) reads that as "second defer,
    reject" since it already checked existence + terminal status upstream."""
    conn = open_conn(db_path)
    try:
        cur = conn.execute(
            f"UPDATE {_TABLE} SET defer_count = defer_count + 1, status = 'deferred' "
            "WHERE id = ? AND status = 'open'",
            (item_id,),
        )
        if cur.rowcount == 0:
            return None
        conn.commit()
        row = conn.execute(
            f"SELECT {', '.join(_ROW_COLUMNS)} FROM {_TABLE} WHERE id = ?", (item_id,)
        ).fetchone()
        return None if row is None else _row_to_dc(row)
    finally:
        conn.close()
