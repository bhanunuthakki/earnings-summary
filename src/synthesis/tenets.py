"""The Worldview Tenet store (P2) — CRUD over insight_notes kind='tenet'.

A **Tenet** is a durable, revisable belief about *how the owner invests*, grounded
in the musings/readings that formed it. Owner-typed Tenets are ``current``
immediately; machine-distilled ones (``synthesis.tenet_distill``) land ``proposed``
for a one-tap approval. Belief revision rides the existing insight_notes supersede
chain (a new row for the same ``scope_key`` flips the prior current to
superseded); **tension** is a distill-time overlap on that ``scope_key``, recorded
in ``meta_json.tensions`` and surfaced for reconcile.

``scope_key = 'tenet:<slug>'`` is the STABLE belief-topic handle — revision reuses
it, so a reworded belief supersedes cleanly instead of forking. NOT to be confused
with the entrenched ``conviction`` 1–5 rating (a Tenet is a belief about method,
not a confidence level on a name).
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Sequence
from pathlib import Path

from synthesis.insights import InsightRow, get_insight, list_insights, record_insight
from user_state._db import now_iso, open_conn

TENET_KIND = "tenet"
_SCOPE_PREFIX = "tenet:"

# The insight kinds a Revert tap may act on (B4, auto-adopt substrate): a
# brand-new Tenet OR stance auto-adopted straight from a session distill. A
# 'theme' or 'digest' insight is never owner-facing in this way, so it is not
# revertible through this path.
_REVERTIBLE_KINDS = (TENET_KIND, "stance")


def _slugify(text: str) -> str:
    words = re.findall(r"[a-z0-9]+", text.lower())
    return "-".join(words[:6])[:60] or "untitled"


def scope_key_for(body_md: str, scope_key: str | None) -> str:
    """The stable belief-topic slug. The owner may pass one (grouping a revision
    onto an existing belief); otherwise derive it from the body. Always normalized
    to the ``tenet:`` namespace so it never collides with a stance (``<TICKER>``)
    or theme (``theme:<slug>``).

    ``macro:`` is a recognized SIBLING namespace (2026-07-19 review: macro
    beliefs had nowhere to live) — a caller passing ``macro:<slug>`` keeps it
    verbatim, exactly like an explicit ``tenet:`` key, so a rates/FX/regime
    belief supersedes on its own chain and rides the Ask macro pack instead of
    being re-slugged into ``tenet:``."""
    if scope_key and scope_key.strip():
        s = scope_key.strip()
        if s.startswith((_SCOPE_PREFIX, "macro:")):
            return s
        return f"{_SCOPE_PREFIX}{_slugify(s)}"
    return f"{_SCOPE_PREFIX}{_slugify(body_md)}"


def current_tenet_for_scope(
    scope_key: str, *, db_path: Path | str | None = None
) -> InsightRow | None:
    """The standing (current) Tenet for a scope, or None — the overlap probe used
    for tension detection and revision."""
    rows = list_insights(kind=TENET_KIND, scope_key=scope_key, status="current", db_path=db_path)
    return rows[0] if rows else None


def record_tenet(
    *,
    body_md: str,
    scope_key: str | None = None,
    source_note_ids: Sequence[int] = (),
    status: str = "current",
    provenance: str = "derived",
    tension_with: int | None = None,
    db_path: Path | str | None = None,
) -> InsightRow:
    """Record a Tenet. Owner-typed → ``status='current'`` (supersedes the prior
    current for its scope_key = a revision). Machine-distilled → ``status='proposed'``
    (waits for approval; ``tension_with`` records an overlapping current Tenet's id
    in ``meta_json.tensions`` so the reconcile view can flag the contradiction)."""
    if not body_md.strip():
        raise ValueError("tenet body must be non-empty")
    key = scope_key_for(body_md, scope_key)
    ids = [int(x) for x in source_note_ids]
    meta: dict[str, object] | None = {"tensions": [int(tension_with)]} if tension_with else None
    new_id = record_insight(
        scope_key=key,
        kind=TENET_KIND,
        body_md=body_md.strip(),
        source_note_ids=ids,
        watermark_id=max(ids, default=None),
        meta=meta,
        provenance=provenance,
        status=status,
        db_path=db_path,
    )
    got = get_insight(new_id, db_path=db_path)
    if got is None:  # pragma: no cover — just inserted
        raise LookupError(f"tenet id={new_id} vanished after insert")
    return got


def list_tenets(
    *,
    status: str = "current",
    db_path: Path | str | None = None,
    conn: sqlite3.Connection | None = None,
) -> list[InsightRow]:
    """Tenets by status ('current' = the live Worldview; 'proposed' = the approval
    queue)."""
    return list_insights(kind=TENET_KIND, status=status, db_path=db_path, conn=conn)


def tenet_for_note(note_id: int, *, db_path: Path | str | None = None) -> InsightRow | None:
    """The newest live (proposed OR current) Tenet already citing this note, or None
    — the idempotency probe for staging a Tenet straight from an On My Mind musing
    (one musing never yields two candidate Tenets)."""
    found: InsightRow | None = None
    for status in ("proposed", "current"):
        for t in list_insights(kind=TENET_KIND, status=status, db_path=db_path):
            if note_id in t.source_note_ids and (found is None or t.id > found.id):
                found = t
    return found


def ensure_tenet_for_note(
    note_id: int, *, body_md: str, db_path: Path | str | None = None
) -> InsightRow | None:
    """Find-or-create a ``proposed`` Tenet grounded in a single On My Mind musing
    (IDEMPOTENT per note_id). The synchronous 'To Worldview' ladder action stages
    the musing's own words as the candidate belief WITHOUT an LLM distill call —
    the owner rewords/approves it in the Worldview panel (mirrors how 'incorporate'
    stages an inert research task). Returns the row, or None on an empty body."""
    existing = tenet_for_note(note_id, db_path=db_path)
    if existing is not None:
        return existing
    if not body_md.strip():
        return None
    return record_tenet(
        body_md=body_md,
        source_note_ids=(note_id,),
        status="proposed",
        provenance="derived",
        db_path=db_path,
    )


def approve_tenet(tenet_id: int, *, db_path: Path | str | None = None) -> InsightRow | None:
    """Promote a ``proposed`` Tenet to ``current``, superseding the prior current
    one for its scope_key (belief revision). Returns the row, or None when the id is
    missing or not in ``proposed``."""
    conn = open_conn(db_path)
    try:
        row = conn.execute(
            "SELECT * FROM insight_notes WHERE id = ? AND kind = ?", (tenet_id, TENET_KIND)
        ).fetchone()
        if row is None or str(row["status"]) != "proposed":
            return None
        scope = str(row["scope_key"])
        now = now_iso()
        prior = conn.execute(
            "SELECT id FROM insight_notes WHERE scope_key = ? AND kind = ? AND status = 'current'",
            (scope, TENET_KIND),
        ).fetchone()
        conn.execute(
            "UPDATE insight_notes SET status = 'current', supersedes_id = ?, updated_at = ? "
            "WHERE id = ?",
            (int(prior[0]) if prior else None, now, tenet_id),
        )
        if prior:
            conn.execute(
                "UPDATE insight_notes SET status = 'superseded', updated_at = ? WHERE id = ?",
                (now, int(prior[0])),
            )
        conn.commit()
    finally:
        conn.close()
    return get_insight(tenet_id, db_path=db_path)


def reject_tenet(tenet_id: int, *, db_path: Path | str | None = None) -> bool:
    """Reject a ``proposed`` Tenet (status → 'rejected'). Returns True when a
    proposed row was rejected (idempotent no-op otherwise)."""
    conn = open_conn(db_path)
    try:
        cur = conn.execute(
            "UPDATE insight_notes SET status = 'rejected', updated_at = ? "
            "WHERE id = ? AND kind = ? AND status = 'proposed'",
            (now_iso(), tenet_id, TENET_KIND),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def revert_tenet(insight_id: int, *, db_path: Path | str | None = None) -> InsightRow | None:
    """Undo an auto-adopted insight row (Tenet OR stance) — the inverse of the
    auto-adopt supersede (B4). Named ``revert_tenet`` for discoverability
    alongside the rest of this module's Tenet verbs, but it operates on any
    revertible ``insight_notes`` row regardless of kind (validated against
    :data:`_REVERTIBLE_KINDS`) — a stance auto-adopted from a session distill
    reverts through the exact same chain.

    The row must be ``status='current'`` — a second tap on an already-reverted
    (or never-current) row is an idempotent no-op, returning None so the
    Telegram/HTTP caller can answer "Already handled." rather than erroring.

    * A REVISION (``supersedes_id`` points at a still-existing predecessor):
      flip this row back to ``superseded`` and restore the predecessor to
      ``current`` — the supersede chain run in reverse. Returns the restored
      predecessor.
    * A brand-new adoption (no predecessor, or it's gone): flip this row to
      ``superseded`` (retire; nothing to restore). Returns the retired row.

    One connection, one commit — mirrors :func:`approve_tenet`'s bespoke-SQL
    shape."""
    conn = open_conn(db_path)
    try:
        row = conn.execute("SELECT * FROM insight_notes WHERE id = ?", (insight_id,)).fetchone()
        if (
            row is None
            or str(row["status"]) != "current"
            or str(row["kind"]) not in _REVERTIBLE_KINDS
        ):
            return None
        now = now_iso()
        supersedes_id = row["supersedes_id"]
        predecessor_id: int | None = None
        if supersedes_id is not None:
            predecessor = conn.execute(
                "SELECT id FROM insight_notes WHERE id = ?", (int(supersedes_id),)
            ).fetchone()
            if predecessor is not None:
                predecessor_id = int(predecessor[0])
        conn.execute(
            "UPDATE insight_notes SET status = 'superseded', updated_at = ? WHERE id = ?",
            (now, insight_id),
        )
        if predecessor_id is not None:
            conn.execute(
                "UPDATE insight_notes SET status = 'current', updated_at = ? WHERE id = ?",
                (now, predecessor_id),
            )
        conn.commit()
    finally:
        conn.close()
    return get_insight(
        predecessor_id if predecessor_id is not None else insight_id, db_path=db_path
    )


def supersede_tenet(
    tenet_id: int, *, body_md: str, db_path: Path | str | None = None
) -> InsightRow | None:
    """Owner revision of a standing Tenet: record a new ``current`` row on the same
    scope_key (which supersedes the old one). Returns the new row, or None when the
    id is missing / not a Tenet."""
    old = get_insight(tenet_id, db_path=db_path)
    if old is None or old.kind != TENET_KIND:
        return None
    return record_tenet(
        body_md=body_md,
        scope_key=old.scope_key,
        source_note_ids=old.source_note_ids,
        status="current",
        provenance="owner",
        db_path=db_path,
    )
