"""Read/write API for user_kpi_registry (alembic 0060).

Each row is the user's declaration that *this* KPI on *this* ticker should
load-bear the thesis. The trigger framework joins kpi_facts against this
table to decide whether an observed inflection is alert-worthy. Single-row
natural key is ``(user_id, ticker, kpi_name)`` — repeated upserts on that
tuple update the existing row rather than fanning out duplicates.

Read paths:
  list_kpis            — full per-user (optionally per-ticker) listing
  get_thesis_breakers  — convenience filter on is_thesis_breaker=1

Write paths:
  upsert_kpi  — insert or update by natural key; bumps updated_at on every call
  delete_kpi  — hard delete by row id
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from identity import DEFAULT_USER_ID
from user_state._db import now_iso, open_conn, parse_dt


@dataclass(slots=True)
class UserKpiRegistryRow:
    """One row of user_kpi_registry, fully decoded.

    `is_thesis_breaker` round-trips as bool even though the column is
    INTEGER (0/1) — every consumer wants a Python truthy value.
    """

    id: int
    user_id: str
    ticker: str
    kpi_name: str
    threshold_direction: str | None
    threshold_value: float | None
    is_thesis_breaker: bool
    scaffold_source: str | None
    notes: str | None
    created_at: datetime
    updated_at: datetime


def upsert_kpi(
    *,
    user_id: str = DEFAULT_USER_ID,
    ticker: str,
    kpi_name: str,
    threshold_direction: str | None = None,
    threshold_value: float | None = None,
    is_thesis_breaker: bool = False,
    scaffold_source: str | None = None,
    notes: str | None = None,
    db_path: Path | str | None = None,
) -> UserKpiRegistryRow:
    """Insert a registry row or update the existing one on natural-key conflict.

    Natural key: ``(user_id, ticker, kpi_name)`` (enforced by the unique
    index ``uq_user_kpi_registry_user_ticker_kpi``). On update, every
    non-key column is overwritten with the new value and ``updated_at`` is
    bumped to now; ``created_at`` from the original row is preserved.
    """
    if threshold_direction is not None and threshold_direction not in ("above", "below"):
        raise ValueError(
            f"threshold_direction must be 'above', 'below', or None; got {threshold_direction!r}"
        )
    conn = open_conn(db_path)
    try:
        now = now_iso()
        existing = conn.execute(
            "SELECT id, created_at FROM user_kpi_registry "
            "WHERE user_id = ? AND ticker = ? AND kpi_name = ?",
            (user_id, ticker, kpi_name),
        ).fetchone()
        if existing is None:
            cur = conn.execute(
                """
                INSERT INTO user_kpi_registry(
                    user_id, ticker, kpi_name,
                    threshold_direction, threshold_value, is_thesis_breaker,
                    scaffold_source, notes, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    user_id,
                    ticker,
                    kpi_name,
                    threshold_direction,
                    threshold_value,
                    1 if is_thesis_breaker else 0,
                    scaffold_source,
                    notes,
                    now,
                    now,
                ),
            )
            row_id = int(cur.lastrowid or 0)
        else:
            row_id = int(existing["id"])
            conn.execute(
                """
                UPDATE user_kpi_registry SET
                    threshold_direction = ?,
                    threshold_value     = ?,
                    is_thesis_breaker   = ?,
                    scaffold_source     = ?,
                    notes               = ?,
                    updated_at          = ?
                WHERE id = ?
                """,
                (
                    threshold_direction,
                    threshold_value,
                    1 if is_thesis_breaker else 0,
                    scaffold_source,
                    notes,
                    now,
                    row_id,
                ),
            )
        conn.commit()
        return _fetch_one(conn, row_id)
    finally:
        conn.close()


def list_kpis(
    user_id: str = DEFAULT_USER_ID,
    ticker: str | None = None,
    db_path: Path | str | None = None,
) -> list[UserKpiRegistryRow]:
    """Return all registry rows for ``user_id``, optionally filtered to one ticker.

    Sort order is ``ticker ASC, kpi_name ASC`` — stable and intuitive for the
    dashboard's per-ticker view.
    """
    conn = open_conn(db_path)
    try:
        if ticker is None:
            rows = conn.execute(
                "SELECT * FROM user_kpi_registry WHERE user_id = ? "
                "ORDER BY ticker ASC, kpi_name ASC",
                (user_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM user_kpi_registry WHERE user_id = ? AND ticker = ? "
                "ORDER BY kpi_name ASC",
                (user_id, ticker),
            ).fetchall()
        return [_row_to_dc(r) for r in rows]
    finally:
        conn.close()


def get_thesis_breakers(
    user_id: str = DEFAULT_USER_ID,
    ticker: str | None = None,
    db_path: Path | str | None = None,
) -> list[UserKpiRegistryRow]:
    """Convenience: registry rows where ``is_thesis_breaker = 1``.

    Used by the trigger framework to scope which KPIs can actually fire a
    'thesis_drift' alert (informational KPIs in the registry don't).
    """
    conn = open_conn(db_path)
    try:
        if ticker is None:
            rows = conn.execute(
                "SELECT * FROM user_kpi_registry "
                "WHERE user_id = ? AND is_thesis_breaker = 1 "
                "ORDER BY ticker ASC, kpi_name ASC",
                (user_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM user_kpi_registry "
                "WHERE user_id = ? AND ticker = ? AND is_thesis_breaker = 1 "
                "ORDER BY kpi_name ASC",
                (user_id, ticker),
            ).fetchall()
        return [_row_to_dc(r) for r in rows]
    finally:
        conn.close()


def delete_kpi(row_id: int, db_path: Path | str | None = None) -> bool:
    """Hard-delete a registry row by id. Returns True iff a row was removed."""
    conn = open_conn(db_path)
    try:
        cur = conn.execute("DELETE FROM user_kpi_registry WHERE id = ?", (row_id,))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def _fetch_one(conn: sqlite3.Connection, row_id: int) -> UserKpiRegistryRow:
    row = conn.execute("SELECT * FROM user_kpi_registry WHERE id = ?", (row_id,)).fetchone()
    if row is None:
        raise LookupError(f"user_kpi_registry id={row_id} not found after write")
    return _row_to_dc(row)


def _row_to_dc(row: sqlite3.Row) -> UserKpiRegistryRow:
    threshold_value_raw = row["threshold_value"]
    return UserKpiRegistryRow(
        id=int(row["id"]),
        user_id=str(row["user_id"]),
        ticker=str(row["ticker"]),
        kpi_name=str(row["kpi_name"]),
        threshold_direction=(
            None if row["threshold_direction"] is None else str(row["threshold_direction"])
        ),
        threshold_value=(None if threshold_value_raw is None else float(threshold_value_raw)),
        is_thesis_breaker=bool(row["is_thesis_breaker"]),
        scaffold_source=(None if row["scaffold_source"] is None else str(row["scaffold_source"])),
        notes=(None if row["notes"] is None else str(row["notes"])),
        created_at=parse_dt(row["created_at"]),
        updated_at=parse_dt(row["updated_at"]),
    )
