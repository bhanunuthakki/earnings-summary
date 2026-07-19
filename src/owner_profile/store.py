"""Versioned persistence for ``owner_profile_facts`` (0159).

Append-and-supersede over the ``model_provenance.versioning`` helpers, the
SAME ``positioning_intents`` precedent (``positioning/store.py``): a save
never destroys history — the prior latest row for the same
``(user_id, category, key)`` flips ``is_latest=0`` with
``superseded_at``/``superseded_by_id`` stamped, and the partial unique index
guarantees exactly one authoritative row per fact.

Gated assertion (§7.1, refined by owner ruling 2026-07-19): ``append_fact``
always lands the new row at whatever ``status`` the caller passes — the CALLER
decides. The policy line: ``'affirmed'`` is for facts the owner authored
(typed directly here, OR entered in an owner-authored source file the importer
snapshots — wealthplan's plan, the tracker's CIO_CONTEXT; the owner already
vetted those values at the source, so re-ratifying them is manufactured work);
``'proposed'`` is for machine-derived *inferences about the owner*, which §7.1
still gates. Nothing outside this module flips an existing row to
``'affirmed'`` except :func:`affirm_fact`, which requires an explicit call
(the packet-walk approve action). There is no code path that auto-promotes a
proposed fact.

Idempotent imports: ``append_fact`` compares the new value against the current
latest row for the same key (by parsed-dict equality, not string equality —
key order must not matter) and no-ops (returns the existing row's id, writes
nothing) when unchanged. This is what makes re-running
``execution/import_owner_capacity.py`` on an unchanged source file a clean
no-op instead of a growing pile of identical rows.

Reads degrade like every sibling store: a missing table (pre-0159 substrate)
returns ``None``/``[]``, and a row whose ``value_json`` no longer parses as
JSON is skipped LOUDLY (logged with the row id), never guessed at.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import cast

from identity import DEFAULT_USER_ID
from model_provenance.versioning import mark_superseded_by, supersede_current
from owner_profile.models import CATEGORIES, PROVENANCES, STATUSES

log = logging.getLogger(__name__)

_TABLE = "owner_profile_facts"


@dataclass(frozen=True, slots=True)
class OwnerProfileFactRow:
    """One decoded row of ``owner_profile_facts``."""

    id: int
    user_id: str
    category: str  # 'capacity' | 'appetite' | 'behavioral'
    key: str
    value: dict[str, object]  # decoded value_json
    narrative: str
    provenance: str
    status: str  # 'proposed' | 'affirmed' | 'rejected'
    affirmed_at: str | None
    review_horizon_days: int | None
    source_detail: str | None
    created_at: str  # naive-UTC ISO
    is_latest: bool
    superseded_at: str | None
    superseded_by_id: int | None


def _now_iso() -> str:
    return datetime.now(UTC).replace(tzinfo=None).isoformat()


def _decode_row(row: sqlite3.Row) -> OwnerProfileFactRow | None:
    try:
        value = json.loads(str(row["value_json"]))
    except (ValueError, TypeError):
        log.warning(
            {
                "event": "owner_profile_fact_undecodable",
                "id": int(row["id"]),
                "hint": "value_json no longer parses; inspect/fix the row",
            }
        )
        return None
    if not isinstance(value, dict):
        log.warning(
            {
                "event": "owner_profile_fact_undecodable",
                "id": int(row["id"]),
                "hint": "value_json did not decode to an object",
            }
        )
        return None
    return OwnerProfileFactRow(
        id=int(row["id"]),
        user_id=str(row["user_id"]),
        category=str(row["category"]),
        key=str(row["key"]),
        value=cast("dict[str, object]", value),
        narrative=str(row["narrative"]),
        provenance=str(row["provenance"]),
        status=str(row["status"]),
        affirmed_at=row["affirmed_at"],
        review_horizon_days=(
            int(row["review_horizon_days"]) if row["review_horizon_days"] is not None else None
        ),
        source_detail=row["source_detail"],
        created_at=str(row["created_at"]),
        is_latest=bool(row["is_latest"]),
        superseded_at=row["superseded_at"],
        superseded_by_id=int(row["superseded_by_id"]) if row["superseded_by_id"] else None,
    )


def _latest_for_key(
    conn: sqlite3.Connection, *, user_id: str, category: str, key: str
) -> sqlite3.Row | None:
    conn.row_factory = sqlite3.Row
    return conn.execute(
        f"SELECT * FROM {_TABLE} WHERE user_id = ? AND category = ? AND key = ? "
        "AND is_latest = 1 LIMIT 1",
        (user_id, category, key),
    ).fetchone()


def append_fact(
    conn: sqlite3.Connection,
    *,
    category: str,
    key: str,
    value: dict[str, object],
    narrative: str,
    provenance: str,
    status: str = "proposed",
    review_horizon_days: int | None = None,
    source_detail: str | None = None,
    user_id: str = DEFAULT_USER_ID,
) -> int:
    """Persist a new fact version; returns its row id.

    No-ops (returns the existing latest id, writes nothing) when a latest row
    already exists for this ``(user_id, category, key)`` with an identical
    ``(value, narrative)`` pair — the idempotency guarantee an on-demand
    importer depends on (its narrative templates are deterministic over the
    same input, so an unchanged source file still no-ops). A changed value
    OR narrative always supersedes, regardless of the prior row's status (a
    rejected-then-changed fact resurfaces as a fresh 'proposed' row for the
    owner to re-decide; a rejected-and-truly-unchanged fact stays rejected
    and quiet). Narrative is part of this check — not just value — because
    the packet walk's Update/Rewrite path (tenet-2 Phase 3) edits ONLY the
    narrative (the structured value is re-entered by a fresh importer run,
    never freehand): a value-only equality check would silently swallow that
    edit as a no-op.
    """
    if category not in CATEGORIES:
        raise ValueError(f"category must be one of {sorted(CATEGORIES)}, got {category!r}")
    if provenance not in PROVENANCES:
        raise ValueError(f"provenance must be one of {sorted(PROVENANCES)}, got {provenance!r}")
    if status not in STATUSES:
        raise ValueError(f"status must be one of {sorted(STATUSES)}, got {status!r}")
    if not key.strip():
        raise ValueError("key must be non-empty")

    existing = _latest_for_key(conn, user_id=user_id, category=category, key=key)
    if existing is not None:
        try:
            existing_value = json.loads(str(existing["value_json"]))
        except (ValueError, TypeError):
            existing_value = None
        if existing_value == value and str(existing["narrative"]) == narrative:
            return int(existing["id"])

    now = _now_iso()
    superseded = supersede_current(
        conn,
        table=_TABLE,
        entity_where="user_id = :user_id AND category = :category AND key = :key",
        entity_params={"user_id": user_id, "category": category, "key": key},
        now=now,
    )
    affirmed_at = now if status == "affirmed" else None
    cur = conn.execute(
        f"INSERT INTO {_TABLE} (user_id, category, key, value_json, narrative, provenance, "
        "status, affirmed_at, review_horizon_days, source_detail, created_at, is_latest) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)",
        (
            user_id,
            category,
            key,
            json.dumps(value, sort_keys=True),
            narrative,
            provenance,
            status,
            affirmed_at,
            review_horizon_days,
            source_detail,
            now,
        ),
    )
    new_id = int(cur.lastrowid or 0)
    mark_superseded_by(conn, table=_TABLE, superseded_ids=superseded, new_id=new_id)
    return new_id


def affirm_fact(
    conn: sqlite3.Connection, fact_id: int, *, user_id: str = DEFAULT_USER_ID
) -> OwnerProfileFactRow | None:
    """Promote a ``proposed`` fact to ``affirmed`` — the ONLY path that sets
    status='affirmed' outside an owner-provenance ``append_fact`` call.
    Returns the updated row, or None when the id is missing, not the latest
    row, or not currently ``proposed`` (idempotent no-op on a re-tap)."""
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        f"SELECT * FROM {_TABLE} WHERE id = ? AND user_id = ?", (fact_id, user_id)
    ).fetchone()
    if row is None or not bool(row["is_latest"]) or str(row["status"]) != "proposed":
        return None
    now = _now_iso()
    conn.execute(
        f"UPDATE {_TABLE} SET status = 'affirmed', affirmed_at = ? WHERE id = ?",
        (now, fact_id),
    )
    row = conn.execute(f"SELECT * FROM {_TABLE} WHERE id = ?", (fact_id,)).fetchone()
    return _decode_row(row) if row is not None else None


def reject_fact(conn: sqlite3.Connection, fact_id: int, *, user_id: str = DEFAULT_USER_ID) -> bool:
    """Reject a ``proposed`` fact (status -> 'rejected'). Returns True when a
    proposed latest row was rejected (idempotent no-op otherwise)."""
    cur = conn.execute(
        f"UPDATE {_TABLE} SET status = 'rejected' "
        "WHERE id = ? AND user_id = ? AND is_latest = 1 AND status = 'proposed'",
        (fact_id, user_id),
    )
    return cur.rowcount > 0


def reaffirm_fact(
    conn: sqlite3.Connection, fact_id: int, *, user_id: str = DEFAULT_USER_ID
) -> OwnerProfileFactRow | None:
    """Refresh ``affirmed_at`` on an already-``affirmed`` latest row — the
    packet walk's "Still true" verb on an EXPIRING fact (tenet-2 Phase 3,
    §4 delivery seam 5 / §7 decision 6 "both").

    Distinct from :func:`affirm_fact` (proposed -> affirmed, the ratify-a-new-
    import path): this never changes ``status`` or ``value`` — it just resets
    the review-horizon clock on a fact the owner re-confirms unchanged. A real
    change goes through the Update/Rewrite path instead, which supersedes via
    :func:`append_fact` (a new version, still gated through affirmation).
    Returns ``None`` (no-op) when the id is missing, not the latest row, or
    not currently ``affirmed``."""
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        f"SELECT * FROM {_TABLE} WHERE id = ? AND user_id = ?", (fact_id, user_id)
    ).fetchone()
    if row is None or not bool(row["is_latest"]) or str(row["status"]) != "affirmed":
        return None
    now = _now_iso()
    conn.execute(f"UPDATE {_TABLE} SET affirmed_at = ? WHERE id = ?", (now, fact_id))
    row = conn.execute(f"SELECT * FROM {_TABLE} WHERE id = ?", (fact_id,)).fetchone()
    return _decode_row(row) if row is not None else None


def retire_fact(conn: sqlite3.Connection, fact_id: int, *, user_id: str = DEFAULT_USER_ID) -> bool:
    """Retire an ``affirmed`` fact the owner says is no longer true (status ->
    'rejected'). This is the packet walk's "Drop" verb on an EXPIRING fact —
    deliberately a SEPARATE path from :func:`reject_fact` (which only ever
    touches a ``proposed`` row, the ratify-a-new-import path): retiring a fact
    the owner has lived with for a while is a distinct, explicit owner action,
    never automatic. Returns True when an affirmed latest row was retired
    (idempotent no-op otherwise)."""
    cur = conn.execute(
        f"UPDATE {_TABLE} SET status = 'rejected' "
        "WHERE id = ? AND user_id = ? AND is_latest = 1 AND status = 'affirmed'",
        (fact_id, user_id),
    )
    return cur.rowcount > 0


def list_expiring_facts(
    conn: sqlite3.Connection,
    *,
    now: datetime | None = None,
    user_id: str = DEFAULT_USER_ID,
) -> list[OwnerProfileFactRow]:
    """Affirmed facts past their review horizon (``affirmed_at +
    review_horizon_days`` elapsed) — the ONE shared "needs re-affirmation"
    predicate reused by the governor's ``profile_drift`` moment class, the
    Ledger packet walk, and the Sunday Telegram packet (tenet-2 Phase 3, §7
    decision 6 "both": quarterly affirmation packets AND event triggers all
    read this same list, so the expiry definition can never drift between
    surfaces). A fact with no ``review_horizon_days`` on file never expires —
    not every category enforces a cadence yet. Degrades to ``[]`` on a
    missing table or an unparseable ``affirmed_at`` (never guesses)."""
    stamp = now or datetime.now(UTC).replace(tzinfo=None)
    out: list[OwnerProfileFactRow] = []
    for row in list_facts(conn, status="affirmed", user_id=user_id):
        if row.review_horizon_days is None or not row.affirmed_at:
            continue
        try:
            affirmed = datetime.fromisoformat(row.affirmed_at)
        except ValueError:
            continue
        if affirmed + timedelta(days=row.review_horizon_days) <= stamp:
            out.append(row)
    return out


def list_facts(
    conn: sqlite3.Connection,
    *,
    status: str | None = None,
    category: str | None = None,
    user_id: str = DEFAULT_USER_ID,
) -> list[OwnerProfileFactRow]:
    """Latest-row-only listing, optionally filtered by status and/or category.
    Undecodable rows are skipped loudly; a missing table degrades to ``[]``."""
    conn.row_factory = sqlite3.Row
    where = ["user_id = ?", "is_latest = 1"]
    params: list[object] = [user_id]
    if status is not None:
        where.append("status = ?")
        params.append(status)
    if category is not None:
        where.append("category = ?")
        params.append(category)
    try:
        rows = conn.execute(
            f"SELECT * FROM {_TABLE} WHERE {' AND '.join(where)} ORDER BY id",
            params,
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    out: list[OwnerProfileFactRow] = []
    for row in rows:
        decoded = _decode_row(row)
        if decoded is not None:
            out.append(decoded)
    return out


def get_fact(
    conn: sqlite3.Connection, fact_id: int, *, user_id: str = DEFAULT_USER_ID
) -> OwnerProfileFactRow | None:
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            f"SELECT * FROM {_TABLE} WHERE id = ? AND user_id = ?", (fact_id, user_id)
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    return None if row is None else _decode_row(row)


def get_current_profile(
    conn: sqlite3.Connection, *, user_id: str = DEFAULT_USER_ID
) -> dict[str, list[OwnerProfileFactRow]]:
    """Affirmed facts only, grouped by category — the "what the coach may
    actually use" read. Ambient learning keeps proposing; only this view
    reflects what the owner has ratified (§3.1 principle 1 / §7.1)."""
    grouped: dict[str, list[OwnerProfileFactRow]] = {c: [] for c in sorted(CATEGORIES)}
    for row in list_facts(conn, status="affirmed", user_id=user_id):
        grouped.setdefault(row.category, []).append(row)
    return grouped


__all__ = [
    "OwnerProfileFactRow",
    "affirm_fact",
    "append_fact",
    "get_current_profile",
    "get_fact",
    "list_expiring_facts",
    "list_facts",
    "reaffirm_fact",
    "reject_fact",
    "retire_fact",
]
