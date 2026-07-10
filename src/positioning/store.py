"""Versioned persistence for positioning intents (``positioning_intents``, 0145).

Append-and-supersede over the ``model_provenance.versioning`` helpers (the
``dcf_runs`` precedent): a save never destroys history — the prior profile
flips ``is_latest=0`` with ``superseded_at``/``superseded_by_id`` stamped, and
the partial unique index guarantees exactly one authoritative row per user.

Reads degrade like the sibling stores: a missing table returns ``None`` / ``[]``
(pre-0145 substrate), and a row whose ``profile_json`` no longer validates is
skipped LOUDLY (logged with the row id) rather than guessed at — the schema
changed, fix the data or the model, don't invent a profile.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime

from pydantic import ValidationError

from identity import DEFAULT_USER_ID
from model_provenance.versioning import mark_superseded_by, supersede_current
from positioning.profile import PositioningProfile

log = logging.getLogger(__name__)

_TABLE = "positioning_intents"


@dataclass(frozen=True, slots=True)
class PositioningIntentRow:
    """One decoded row of ``positioning_intents``."""

    id: int
    user_id: str
    narrative: str
    profile: PositioningProfile
    source: str  # 'coach' | 'manual'
    coach_session_id: str | None
    created_at: str  # naive-UTC ISO
    is_latest: bool
    superseded_at: str | None
    superseded_by_id: int | None


def _now_iso() -> str:
    return datetime.now(UTC).replace(tzinfo=None).isoformat()


def append_intent(
    conn: sqlite3.Connection,
    *,
    narrative: str,
    profile: PositioningProfile,
    source: str,
    coach_session_id: str | None = None,
    user_id: str = DEFAULT_USER_ID,
) -> int:
    """Persist a new authoritative positioning intent; returns its row id.

    Supersede → insert → back-link, in one transaction on the caller's
    connection (the caller commits). Raises ``ValueError`` on an empty
    profile — 'no targets expressed' must stay representable only as 'no
    intent row', never as an empty-but-latest row.
    """
    if profile.is_empty():
        raise ValueError("refusing to persist an empty positioning profile")
    if source not in ("coach", "manual"):
        raise ValueError(f"source must be 'coach' or 'manual', got {source!r}")
    now = _now_iso()
    superseded = supersede_current(
        conn,
        table=_TABLE,
        entity_where="user_id = :user_id",
        entity_params={"user_id": user_id},
        now=now,
    )
    cur = conn.execute(
        f"INSERT INTO {_TABLE} (user_id, narrative, profile_json, source, "
        f"coach_session_id, created_at, is_latest) VALUES (?, ?, ?, ?, ?, ?, 1)",
        (user_id, narrative, profile.model_dump_json(), source, coach_session_id, now),
    )
    new_id = int(cur.lastrowid or 0)
    mark_superseded_by(conn, table=_TABLE, superseded_ids=superseded, new_id=new_id)
    return new_id


def _decode_row(row: sqlite3.Row) -> PositioningIntentRow | None:
    try:
        profile = PositioningProfile.model_validate_json(str(row["profile_json"]))
    except (ValidationError, ValueError):
        log.warning(
            {
                "event": "positioning_intent_undecodable",
                "id": int(row["id"]),
                "hint": "profile_json no longer validates; inspect/fix the row",
            }
        )
        return None
    return PositioningIntentRow(
        id=int(row["id"]),
        user_id=str(row["user_id"]),
        narrative=str(row["narrative"]),
        profile=profile,
        source=str(row["source"]),
        coach_session_id=row["coach_session_id"],
        created_at=str(row["created_at"]),
        is_latest=bool(row["is_latest"]),
        superseded_at=row["superseded_at"],
        superseded_by_id=int(row["superseded_by_id"]) if row["superseded_by_id"] else None,
    )


def latest_intent(
    conn: sqlite3.Connection, *, user_id: str = DEFAULT_USER_ID
) -> PositioningIntentRow | None:
    """The authoritative positioning intent, or None (no intent saved yet, a
    pre-0145 substrate, or an undecodable latest row — all mean 'default to
    the current book')."""
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            f"SELECT * FROM {_TABLE} WHERE user_id = ? AND is_latest = 1 LIMIT 1",
            (user_id,),
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    if row is None:
        return None
    return _decode_row(row)


def list_intents(
    conn: sqlite3.Connection, *, user_id: str = DEFAULT_USER_ID, limit: int = 20
) -> list[PositioningIntentRow]:
    """Version history, newest first (superseded rows included). Undecodable
    rows are skipped loudly."""
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            f"SELECT * FROM {_TABLE} WHERE user_id = ? ORDER BY id DESC LIMIT ?",
            (user_id, int(limit)),
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    out: list[PositioningIntentRow] = []
    for row in rows:
        decoded = _decode_row(row)
        if decoded is not None:
            out.append(decoded)
    return out
