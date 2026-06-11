"""Read/write API for saved_views (alembic 0079) — persisted ViewSpec pivots.

The storage side of the slice-and-dice substrate (master build P5.1): one
row per named view, holding the ViewSpec JSON verbatim. This module is
deliberately schema-agnostic about the spec itself — callers (the
/api/views routes, the Explore panel) validate through
``viewspec.spec.ViewSpec.from_dict`` before saving and after loading, so
the store never becomes a second place the spec schema lives.

Saving under an existing (user_id, name) replaces that view's spec — the
name is the stable handle the cockpit/report embed hooks reference.
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


@dataclass(slots=True)
class SavedViewRow:
    """One row of saved_views, fully decoded."""

    id: int
    user_id: str
    name: str
    spec: dict[str, object]
    created_at: datetime
    updated_at: datetime


def save_view(
    *,
    name: str,
    spec: dict[str, object],
    user_id: str = DEFAULT_USER_ID,
    db_path: Path | str | None = None,
) -> SavedViewRow:
    """Upsert one named view: INSERT, or replace the spec when (user_id,
    name) already exists. Raises ValueError on an empty name."""
    clean = name.strip()
    if not clean:
        raise ValueError("view name must be non-empty")
    now = now_iso()
    conn = open_conn(db_path)
    try:
        conn.execute(
            """
            INSERT INTO saved_views (user_id, name, spec_json, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT (user_id, name) DO UPDATE SET
              spec_json = excluded.spec_json,
              updated_at = excluded.updated_at
            """,
            (user_id, clean, json.dumps(spec), now, now),
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM saved_views WHERE user_id = ? AND name = ?",
            (user_id, clean),
        ).fetchone()
        if row is None:  # pragma: no cover - upsert guarantees presence
            raise LookupError(f"saved_views ({user_id!r}, {clean!r}) missing after write")
        return _row_to_dc(row)
    finally:
        conn.close()


def list_views(
    *,
    user_id: str = DEFAULT_USER_ID,
    db_path: Path | str | None = None,
) -> list[SavedViewRow]:
    """All of a user's views, most recently updated first."""
    conn = open_conn(db_path)
    try:
        rows = conn.execute(
            "SELECT * FROM saved_views WHERE user_id = ? ORDER BY updated_at DESC, id DESC",
            (user_id,),
        ).fetchall()
        return [_row_to_dc(r) for r in rows]
    finally:
        conn.close()


def get_view(view_id: int, *, db_path: Path | str | None = None) -> SavedViewRow | None:
    """One view by id, or None."""
    conn = open_conn(db_path)
    try:
        row = conn.execute("SELECT * FROM saved_views WHERE id = ?", (view_id,)).fetchone()
        return None if row is None else _row_to_dc(row)
    finally:
        conn.close()


def delete_view(view_id: int, *, db_path: Path | str | None = None) -> bool:
    """Hard-delete one view. True when a row was removed. (Unlike notes,
    a saved view is a query, not memory — deleting it loses nothing.)"""
    conn = open_conn(db_path)
    try:
        cur = conn.execute("DELETE FROM saved_views WHERE id = ?", (view_id,))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def _row_to_dc(row: sqlite3.Row) -> SavedViewRow:
    spec: dict[str, object] = {}
    try:
        parsed: object = json.loads(str(row["spec_json"]))
    except ValueError:  # pragma: no cover - json_valid CHECK forbids this
        parsed = None
    if isinstance(parsed, dict):
        spec = cast("dict[str, object]", parsed)
    return SavedViewRow(
        id=int(row["id"]),
        user_id=str(row["user_id"]),
        name=str(row["name"]),
        spec=spec,
        created_at=parse_dt(row["created_at"]),
        updated_at=parse_dt(row["updated_at"]),
    )
