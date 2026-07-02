"""Versioned-model provenance — append-and-supersede instead of overwrite.

A *versioned model output* table carries three columns:

    is_latest        INTEGER NOT NULL DEFAULT 1
    superseded_at    TEXT      (naive-UTC; stamped when a newer version lands)
    superseded_by_id INTEGER   (id of the version that replaced this row; NO
                                REFERENCES — the repo-wide FK-poisoning invariant)

Instead of destroying the prior row (``INSERT OR REPLACE``), landing a new version:

    1. flips the current row(s) for the same *entity* to is_latest=0 and stamps
       superseded_at                                   -> ``supersede_current``
    2. inserts the new row with is_latest=1            (the caller's own INSERT)
    3. back-links the just-superseded rows to the new id -> ``mark_superseded_by``

Generic over any such table; the first user is ``dcf_runs`` (entity = ticker +
segment). History is preserved, and a partial unique index ``WHERE is_latest=1``
keeps exactly one current version per entity.

SECURITY: ``table`` and ``entity_where`` are interpolated into SQL. They are
internal, code-supplied identifiers — never pass user input into them. Values
always travel as bound parameters.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime


def _now_iso() -> str:
    """Naive-UTC ISO 8601 — matches the schema's canonical TEXT-timestamp format."""
    return datetime.now(UTC).replace(tzinfo=None).isoformat()


def supersede_current(
    conn: sqlite3.Connection,
    *,
    table: str,
    entity_where: str,
    entity_params: dict[str, object],
    now: str | None = None,
) -> list[int]:
    """Flip every ``is_latest=1`` row of ``table`` matching ``entity_where`` to
    ``is_latest=0``, stamping ``superseded_at``. Returns the ids that were current
    (so the caller can back-link them to the new version after inserting it).

    ``entity_where`` is a SQL predicate over ``table`` using named params present
    in ``entity_params`` — e.g.
    ``"ticker = :ticker AND COALESCE(segment_name, '') = :seg"``.
    """
    stamp = now or _now_iso()
    ids = [
        int(r[0])
        for r in conn.execute(
            f"SELECT id FROM {table} WHERE ({entity_where}) AND is_latest = 1",
            entity_params,
        ).fetchall()
    ]
    if ids:
        conn.execute(
            f"UPDATE {table} SET is_latest = 0, superseded_at = :__now "
            f"WHERE ({entity_where}) AND is_latest = 1",
            {**entity_params, "__now": stamp},
        )
    return ids


def mark_superseded_by(
    conn: sqlite3.Connection,
    *,
    table: str,
    superseded_ids: list[int],
    new_id: int,
) -> None:
    """Back-link the rows ``supersede_current`` flipped to the version that
    replaced them. No-op when nothing was superseded (the first version of an
    entity)."""
    if not superseded_ids:
        return
    marks = ",".join("?" for _ in superseded_ids)
    conn.execute(
        f"UPDATE {table} SET superseded_by_id = ? WHERE id IN ({marks})",
        [new_id, *superseded_ids],
    )
