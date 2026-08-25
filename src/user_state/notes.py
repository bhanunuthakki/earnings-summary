"""Read/write API for analyst_notes (alembic 0074) — durable analyst memory.

Comments live per (ticker, report_date); durable Ask threads live in SQLite and survive
report build; this table is the cross-build record of the analyst's
thinking. One row per thought, anchored to an object (ticker / report
section / KPI / fact / alert), classified by *semantics*:

  question     — something to find out (open until answered)
  decision     — a directive or judgment ("drop this KPI", "size up on dip")
  watch        — a watch-item to resurface on relevant events
  assumption   — a belief the thesis rests on, checkable later
  observation  — anything else worth remembering
  musing       — a captured stream-of-consciousness thought (The Ledger);
                 identity defaulted at write time so capture asks nothing

Lifecycle: ``open → resolved`` (answered/done), ``open → archived``
(no longer relevant), and correction is a supersede chain — a new row with
``supersedes_id`` pointing at the old one, never an in-place rewrite. Hard
deletes don't exist; memory is the point.

Rows arrive from three places (``source``): mirrored from report comments
by :func:`sync_store_comments` (idempotent on ``source_ref``), captured
from chat / alert flows (follow-on wiring), or created directly
(``manual``). Mirrored rows keep the comment as the system of record for
*comment-side* fields, but reconcile through watermarks stored in
``context_json`` so a manual edit on the notes side (reclassify kind,
resolve early) is never silently reverted by the next sync — see
:func:`sync_store_comments`.

Links (0093): a note may reference the decision and/or position-lifecycle
row it is about (``decision_id`` → decisions.id, ``position_entry_id`` →
position_entries.id; plain columns, validated in src/journal_links.py).
``link_auto_resolve`` records the analyst's choice at link time: resolve
the note automatically when the linked object concludes, or surface it in
the pending-reconciliation view. :func:`set_note_links` is the low-level
column write; the validating entry point is ``journal_links.link_note``.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, cast

from identity import DEFAULT_USER_ID
from user_state._db import now_iso, open_conn, open_read_conn, parse_dt

if TYPE_CHECKING:
    from datetime import date

    from comments import Comment, CommentStore

NOTE_KINDS: tuple[str, ...] = (
    "question",
    "decision",
    "watch",
    "assumption",
    "observation",
    "musing",
    # 'intent' (0130): a standing intent (e.g. an options-sleeve plan) with a
    # lifecycle — NOTE_STATUSES + closure provenance in context_json — so the
    # coach can follow up on it, and STOP once it's resolved/rejected (the
    # corpus-freshness rule: a nag about a settled topic destroys trust).
    "intent",
)
NOTE_STATUSES: tuple[str, ...] = ("open", "resolved", "superseded", "archived")
# 'advisor' (0077): notes written by advisor memo runs — honest provenance for
# the priors anchor, distinct from anything the analyst typed themselves.
# 'capture' (The Ledger): a stream-of-consciousness musing landed by the capture
# pipeline (Telegram / Gmail / desktop tray, voice or text) — distinct provenance
# from a typed `manual` note, so a musing is never mistaken for a deliberate edit.
NOTE_SOURCES: tuple[str, ...] = ("comment", "chat", "alert", "manual", "advisor", "capture")

# Comment intents → note kinds. Action routes collapse onto semantics:
# every thesis/KPI mutation directive is a *decision*; data-quality and
# style notes are *observations*. platform_change is deliberately absent —
# cross-workspace bugs go to the platform backlog, not analyst memory.
#
# `needs_triage` maps to *question*, NOT observation: a comment the classifier
# couldn't route is an open loop awaiting human disposition (Instrument
# Paradigm §1 — closed under no-fit, never silently flattened into an inert
# observation). It reconciles like any question — when the analyst reclassifies
# the comment to a real intent, the next sync rewrites the kind.
_INTENT_TO_KIND: dict[str, str] = {
    "ask_question": "question",
    "edit_thesis": "decision",
    "edit_structured": "decision",
    "drop_kpi": "decision",
    "extract_kpi": "decision",
    "curate_peers": "decision",
    "fix_data": "observation",
    "rewrite_section": "observation",
    "needs_triage": "question",
}

_COMMENT_STATUS_TO_NOTE: dict[str, str] = {
    "open": "open",
    "addressed": "resolved",
    "dismissed": "archived",
}

# A comment the classifier parked at the closed-under-no-fit terminal mirrors
# here as a kind="question" note whose context_json["intent"] == "needs_triage"
# — the durable discriminator the dedicated Triage surface (S11) filters on (a
# comment dies with its report build; this table is the cross-build record).
# ROUTABLE_INTENTS are the real comment intents the owner can route a parked
# item to (every _INTENT_TO_KIND key except the terminal itself).
TRIAGE_INTENT = "needs_triage"
ROUTABLE_INTENTS: tuple[str, ...] = tuple(k for k in _INTENT_TO_KIND if k != TRIAGE_INTENT)


@dataclass(slots=True)
class AnalystNoteRow:
    """One row of analyst_notes, fully decoded."""

    id: int
    user_id: str
    ticker: str | None
    kind: str
    status: str
    body: str
    anchor_type: str | None
    anchor_key: str | None
    source: str
    source_ref: str | None
    supersedes_id: int | None
    resolution_note: str | None
    context: dict[str, object] | None
    created_at: datetime
    updated_at: datetime
    resolved_at: datetime | None
    # 0093 links — None/False on a pre-0093 schema (decoded defensively).
    decision_id: int | None = None
    position_entry_id: int | None = None
    link_auto_resolve: bool = False
    # 0099 — the stable doorway handle (kpi:{ticker}:{def_id} / fin:…) the
    # anchored element carried, so the note re-binds across a metric rename.
    # None on a pre-0099 schema or a text-keyed anchor (decoded defensively).
    fact_ref: str | None = None


@dataclass(slots=True)
class SyncStats:
    """What one :func:`sync_store_comments` pass did."""

    created: int = 0
    updated: int = 0
    archived: int = 0
    skipped: int = 0


class NoteRevisionConflictError(RuntimeError):
    """The caller edited a stale note revision."""

    def __init__(self, current_revision: str) -> None:
        super().__init__("note revision conflict")
        self.current_revision = current_revision


def create_note(
    *,
    user_id: str = DEFAULT_USER_ID,
    ticker: str | None,
    kind: str,
    body: str,
    anchor_type: str | None = None,
    anchor_key: str | None = None,
    fact_ref: str | None = None,
    source: str = "manual",
    source_ref: str | None = None,
    context: dict[str, object] | None = None,
    decision_id: int | None = None,
    position_entry_id: int | None = None,
    link_auto_resolve: bool = False,
    db_path: Path | str | None = None,
    conn: sqlite3.Connection | None = None,
) -> AnalystNoteRow:
    """INSERT one open note. ``ticker=None`` records a portfolio-level note.

    ``fact_ref`` is the stable doorway handle (0099) the anchored datum carried;
    it lets the note re-bind across a metric rename. The link params are written
    verbatim (no cross-table validation here) — callers that take user input go
    through ``journal_links.link_note``."""
    _validate("kind", kind, NOTE_KINDS)
    _validate("source", source, NOTE_SOURCES)
    if not body.strip():
        raise ValueError("note body must be non-empty")
    db_conn = conn or open_conn(db_path)
    try:
        row_id = _insert(
            db_conn,
            user_id=user_id,
            ticker=ticker,
            kind=kind,
            status="open",
            body=body,
            anchor_type=anchor_type,
            anchor_key=anchor_key,
            fact_ref=fact_ref,
            source=source,
            source_ref=source_ref,
            supersedes_id=None,
            resolution_note=None,
            context=context,
            resolved_at=None,
            decision_id=decision_id,
            position_entry_id=position_entry_id,
            link_auto_resolve=link_auto_resolve,
        )
        if conn is None:
            db_conn.commit()
        return _fetch_one(db_conn, row_id)
    finally:
        if conn is None:
            db_conn.close()


def create_note_once(
    *,
    user_id: str = DEFAULT_USER_ID,
    ticker: str | None,
    kind: str,
    body: str,
    source: str,
    source_ref: str,
    anchor_type: str | None = None,
    anchor_key: str | None = None,
    context: dict[str, object] | None = None,
    db_path: Path | str | None = None,
) -> AnalystNoteRow:
    """Create one source-keyed note, or return the identical existing note.

    This is the retry-safe writer for pipelines whose downstream state change
    can be retried after the note commits. Reusing a source key with different
    semantic content fails loudly instead of silently changing durable memory.
    """
    if not source_ref.strip():
        raise ValueError("source_ref must be non-empty")
    _validate("kind", kind, NOTE_KINDS)
    _validate("source", source, NOTE_SOURCES)
    if not body.strip():
        raise ValueError("note body must be non-empty")
    normalized_ticker = ticker.upper() if ticker else None
    conn = open_conn(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        existing_row = conn.execute(
            "SELECT * FROM analyst_notes WHERE user_id=? AND source=? AND source_ref=?",
            (user_id, source, source_ref),
        ).fetchone()
        if existing_row is not None:
            existing = _row_to_dc(existing_row)
            if (
                existing.ticker != normalized_ticker
                or existing.kind != kind
                or existing.body != body
                or existing.context != context
            ):
                raise ValueError(
                    f"analyst note source_ref={source_ref!r} already exists with different content"
                )
            conn.commit()
            return existing
        row_id = _insert(
            conn,
            user_id=user_id,
            ticker=normalized_ticker,
            kind=kind,
            status="open",
            body=body,
            anchor_type=anchor_type,
            anchor_key=anchor_key,
            fact_ref=None,
            source=source,
            source_ref=source_ref,
            supersedes_id=None,
            resolution_note=None,
            context=context,
            resolved_at=None,
            decision_id=None,
            position_entry_id=None,
            link_auto_resolve=False,
        )
        conn.commit()
        return _fetch_one(conn, row_id)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_note(
    note_id: int,
    *,
    db_path: Path | str | None = None,
    conn: sqlite3.Connection | None = None,
) -> AnalystNoteRow | None:
    """One note by id, or None."""
    db_conn = conn or open_read_conn(db_path)
    try:
        row = db_conn.execute("SELECT * FROM analyst_notes WHERE id = ?", (note_id,)).fetchone()
        return None if row is None else _row_to_dc(row)
    finally:
        if conn is None:
            db_conn.close()


def list_notes(
    *,
    user_id: str = DEFAULT_USER_ID,
    ticker: str | None = None,
    kind: str | None = None,
    status: str | None = None,
    limit: int = 200,
    db_path: Path | str | None = None,
    conn: sqlite3.Connection | None = None,
) -> list[AnalystNoteRow]:
    """Newest-first notes, filtered.

    ``status=None`` (the default) returns *live* notes — everything except
    ``superseded`` and ``archived`` — which is what the priors anchor and
    any notes panel want. Pass an explicit status to target one bucket.
    ``ticker=None`` means all tickers (portfolio-level notes included), not
    "only the NULL-ticker notes".
    """
    if status is not None:
        _validate("status", status, NOTE_STATUSES)
    if kind is not None:
        _validate("kind", kind, NOTE_KINDS)
    clauses = ["user_id = ?"]
    params: list[object] = [user_id]
    if ticker is not None:
        clauses.append("ticker = ?")
        params.append(ticker.upper())
    if kind is not None:
        clauses.append("kind = ?")
        params.append(kind)
    if status is not None:
        clauses.append("status = ?")
        params.append(status)
    else:
        clauses.append("status NOT IN ('superseded', 'archived')")
    params.append(int(limit))
    db_conn = conn or open_read_conn(db_path)
    try:
        rows = db_conn.execute(
            "SELECT * FROM analyst_notes WHERE "
            + " AND ".join(clauses)
            + " ORDER BY created_at DESC, id DESC LIMIT ?",
            params,
        ).fetchall()
        return [_row_to_dc(r) for r in rows]
    finally:
        if conn is None:
            db_conn.close()


# The On My Mind feed's default kinds: a captured *thought* (musing) or a saved
# *reading* (observation). This is an ALLOW-LIST — source='capture' also carries
# `intent` (standing intents) and, later, `decision` rows, which have their own
# surfaces (Reconcile, decision capture) and must NOT leak into the reverse-chron
# working-memory feed. Keeping it explicit makes the feed forward-safe as new
# capture kinds land.
FEED_KINDS: tuple[str, ...] = ("musing", "observation")


def list_capture_feed(
    *,
    user_id: str = DEFAULT_USER_ID,
    kinds: tuple[str, ...] = FEED_KINDS,
    limit: int = 30,
    before_created_at: str | None = None,
    before_id: int | None = None,
    db_path: Path | str | None = None,
) -> list[AnalystNoteRow]:
    """The On My Mind feed read model: newest-first captured items
    (``source='capture'``) across ``kinds``, KEYSET-paginated on
    ``(created_at, id)``.

    Pass the last row's ``(created_at, id)`` as ``before_created_at`` /
    ``before_id`` to fetch the next page. Keyset (not OFFSET) so a capture landing
    mid-scroll never shifts the window and duplicates or skips a row — the feed's
    whole point is high-volume live capture. ``created_at`` is a naive-UTC ISO
    stamp (``now_iso``) on every capture row, so its lexical order is its
    chronological order and the cursor round-trips exactly.

    Best-effort: a missing DB / pre-0074 schema degrades to ``[]`` (the feed shows
    its empty state, never 500s the tab) — the ``list_triage_notes`` pattern.
    """
    for k in kinds:
        _validate("kind", k, NOTE_KINDS)
    if not kinds:
        return []
    clauses = [
        "user_id = ?",
        "source = 'capture'",
        f"kind IN ({', '.join('?' * len(kinds))})",
        "status NOT IN ('superseded', 'archived')",
    ]
    params: list[object] = [user_id, *kinds]
    if before_created_at is not None and before_id is not None:
        # Strict keyset: everything strictly older than the cursor row.
        clauses.append("(created_at < ? OR (created_at = ? AND id < ?))")
        params.extend([before_created_at, before_created_at, int(before_id)])
    params.append(int(limit))
    try:
        conn = open_conn(db_path)
    except (sqlite3.Error, OSError):
        return []
    try:
        rows = conn.execute(
            "SELECT * FROM analyst_notes WHERE "
            + " AND ".join(clauses)
            + " ORDER BY created_at DESC, id DESC LIMIT ?",
            params,
        ).fetchall()
    except sqlite3.Error:
        return []
    finally:
        conn.close()
    return [_row_to_dc(r) for r in rows]


def list_triage_notes(
    *,
    user_id: str = DEFAULT_USER_ID,
    limit: int = 200,
    db_path: Path | str | None = None,
) -> list[AnalystNoteRow]:
    """Open comments the classifier couldn't route (``needs_triage``), newest
    first — the dedicated Triage surface's query (S11).

    Filters on ``context_json['intent'] == 'needs_triage'`` in Python from the
    decoded context rather than a SQL ``json_extract`` so the reader stays
    portable across SQLite builds without the json1 extension. Best-effort: a
    missing DB / pre-0074 schema degrades to ``[]`` (the surface shows its empty
    state instead of 500-ing the tab)."""
    try:
        conn = open_conn(db_path)
    except (sqlite3.Error, OSError):
        return []
    try:
        rows = conn.execute(
            "SELECT * FROM analyst_notes WHERE user_id = ? AND source = 'comment' "
            "AND status = 'open' ORDER BY created_at DESC, id DESC LIMIT ?",
            (user_id, int(limit)),
        ).fetchall()
    except sqlite3.Error:
        return []
    finally:
        conn.close()
    decoded = [_row_to_dc(r) for r in rows]
    return [n for n in decoded if (n.context or {}).get("intent") == TRIAGE_INTENT]


def route_triage_note(
    note_id: int,
    *,
    intent: str,
    db_path: Path | str | None = None,
) -> AnalystNoteRow | None:
    """Route a parked ``needs_triage`` note to the real comment ``intent`` the
    classifier missed (S11 Triage surface). Rewrites the note ``kind`` (via the
    intent→kind map) AND the ``context_json['intent']`` watermark, so the note
    leaves the triage queue durably. Returns the updated row, or None when the
    note is missing; raises ``ValueError`` on a non-routable intent.

    The comment store stays system-of-record — the Triage route handler also
    best-effort updates the underlying comment's intent so a later re-sync keeps
    the two in step. When the report build that owned the comment is gone, this
    note-side write stands on its own: the next sync sees ``comment.intent ==
    context['intent']`` and never reverts it."""
    if intent not in ROUTABLE_INTENTS:
        raise ValueError(f"intent must be one of {ROUTABLE_INTENTS}, got {intent!r}")
    conn = open_conn(db_path)
    try:
        row = conn.execute("SELECT * FROM analyst_notes WHERE id = ?", (note_id,)).fetchone()
        if row is None:
            return None
        ctx = dict(_row_to_dc(row).context or {})
        ctx["intent"] = intent
        conn.execute(
            "UPDATE analyst_notes SET kind = ?, context_json = ?, updated_at = ? WHERE id = ?",
            (_INTENT_TO_KIND[intent], json.dumps(ctx), now_iso(), note_id),
        )
        conn.commit()
        return _fetch_one(conn, note_id)
    finally:
        conn.close()


def set_ticker(
    note_id: int,
    *,
    ticker: str,
    db_path: Path | str | None = None,
) -> AnalystNoteRow | None:
    """Attribute a ``needs_ticker`` musing to one of its candidates (PR9 Ledger
    set-ticker chips). Write-once-ish: refuses (raises ``ValueError``) when the
    note already carries a ticker, so a stray second tap can never silently
    reassign a note someone already attributed. Clears the now-stale
    ``needs_ticker`` / ``ticker_candidates`` context keys on success. Returns
    None when the note doesn't exist."""
    ticker = ticker.strip().upper()
    if not ticker:
        raise ValueError("ticker must be non-empty")
    conn = open_conn(db_path)
    try:
        row = conn.execute("SELECT * FROM analyst_notes WHERE id = ?", (note_id,)).fetchone()
        if row is None:
            return None
        current = _row_to_dc(row)
        if current.ticker is not None:
            raise ValueError(f"note {note_id} already has ticker {current.ticker!r}")
        ctx = dict(current.context or {})
        ctx.pop("needs_ticker", None)
        ctx.pop("ticker_candidates", None)
        conn.execute(
            "UPDATE analyst_notes SET ticker = ?, context_json = ?, updated_at = ? WHERE id = ?",
            (ticker, json.dumps(ctx), now_iso(), note_id),
        )
        conn.commit()
        return _fetch_one(conn, note_id)
    finally:
        conn.close()


def resolve_note(
    note_id: int,
    *,
    resolution_note: str | None = None,
    db_path: Path | str | None = None,
) -> AnalystNoteRow | None:
    """Mark a note resolved (answered / done). Returns the row, or None if missing."""
    return _set_status(note_id, "resolved", resolution_note=resolution_note, db_path=db_path)


def archive_note(note_id: int, *, db_path: Path | str | None = None) -> AnalystNoteRow | None:
    """Mark a note archived (no longer relevant). Returns the row, or None if missing."""
    return _set_status(note_id, "archived", resolution_note=None, db_path=db_path)


def unarchive_note(note_id: int, *, db_path: Path | str | None = None) -> AnalystNoteRow | None:
    """Undo an archive: return a note to ``open``. Returns the row, or None if
    missing. The reverse of :func:`archive_note`, for the inbox's optimistic
    Undo (Wave 3b)."""
    return _set_status(note_id, "open", resolution_note=None, db_path=db_path)


def reclassify_note(
    note_id: int, *, kind: str, db_path: Path | str | None = None
) -> AnalystNoteRow | None:
    """Change a note's kind ("this is really a watch-item"). Returns the row,
    or None if missing. On mirrored rows this sticks across re-syncs — the
    reconciler only rewrites kind when the comment-side intent moved."""
    _validate("kind", kind, NOTE_KINDS)
    conn = open_conn(db_path)
    try:
        cur = conn.execute(
            "UPDATE analyst_notes SET kind = ?, updated_at = ? WHERE id = ?",
            (kind, now_iso(), note_id),
        )
        if cur.rowcount == 0:
            return None
        conn.commit()
        return _fetch_one(conn, note_id)
    finally:
        conn.close()


def patch_note_context(
    note_id: int,
    patch: dict[str, object],
    *,
    db_path: Path | str | None = None,
) -> AnalystNoteRow | None:
    """Shallow-merge ``patch`` into a note's ``context_json`` (read-merge-write).

    The On My Mind action ladder records its state as ``context_json['ladder']``
    (save-for-later / discuss / incorporated); dismiss is a status change
    (``archive_note``), not a context patch. Returns the updated row, or None when
    the note is missing. Follows the same read-decode-merge-write shape as
    :func:`route_triage_note` so a manual context field is never clobbered
    wholesale."""
    conn = open_conn(db_path)
    try:
        row = conn.execute("SELECT * FROM analyst_notes WHERE id = ?", (note_id,)).fetchone()
        if row is None:
            return None
        ctx = dict(_row_to_dc(row).context or {})
        ctx.update(patch)
        conn.execute(
            "UPDATE analyst_notes SET context_json = ?, updated_at = ? WHERE id = ?",
            (json.dumps(ctx), now_iso(), note_id),
        )
        conn.commit()
        return _fetch_one(conn, note_id)
    finally:
        conn.close()


_UNSET: object = object()  # sentinel: "leave this link column alone"


def set_note_links(
    note_id: int,
    *,
    decision_id: int | None | object = _UNSET,
    position_entry_id: int | None | object = _UNSET,
    link_auto_resolve: bool | object = _UNSET,
    db_path: Path | str | None = None,
) -> AnalystNoteRow | None:
    """Low-level write of the 0093 link columns. Only supplied fields are
    touched; pass ``None`` to clear a link. NO cross-table validation —
    that is ``journal_links.link_note``'s job. Returns the updated row, or
    None when the note doesn't exist."""
    sets: list[str] = []
    params: list[object] = []
    if decision_id is not _UNSET:
        sets.append("decision_id = ?")
        params.append(decision_id)
    if position_entry_id is not _UNSET:
        sets.append("position_entry_id = ?")
        params.append(position_entry_id)
    if link_auto_resolve is not _UNSET:
        sets.append("link_auto_resolve = ?")
        params.append(1 if link_auto_resolve else 0)
    conn = open_conn(db_path)
    try:
        if not sets:
            row = conn.execute("SELECT * FROM analyst_notes WHERE id = ?", (note_id,)).fetchone()
            return None if row is None else _row_to_dc(row)
        sets.append("updated_at = ?")
        params.append(now_iso())
        params.append(note_id)
        cur = conn.execute(f"UPDATE analyst_notes SET {', '.join(sets)} WHERE id = ?", params)
        if cur.rowcount == 0:
            return None
        conn.commit()
        return _fetch_one(conn, note_id)
    finally:
        conn.close()


def supersede_note(
    note_id: int,
    *,
    body: str,
    kind: str | None = None,
    source: str | None = None,
    source_ref: str | None = None,
    context: dict[str, object] | None = None,
    expected_revision: str | None = None,
    db_path: Path | str | None = None,
    conn: sqlite3.Connection | None = None,
) -> AnalystNoteRow:
    """Correct a note: INSERT a replacement chained via ``supersedes_id`` and
    mark the original superseded. The replacement inherits ticker + anchor
    + links (the objects the thought is about) but is a fresh ``manual``
    row — the chain, not an edit, is the audit trail (same invariant as the
    fact tables' restatement chains). Raises LookupError when ``note_id`` is
    gone.
    """
    if kind is not None:
        _validate("kind", kind, NOTE_KINDS)
    if source is not None:
        _validate("source", source, NOTE_SOURCES)
    if not body.strip():
        raise ValueError("note body must be non-empty")
    db_conn = conn or open_conn(db_path)
    try:
        if conn is None:
            db_conn.execute("BEGIN IMMEDIATE")
        old_row = db_conn.execute("SELECT * FROM analyst_notes WHERE id = ?", (note_id,)).fetchone()
        if old_row is None:
            raise LookupError(f"analyst_notes id={note_id} not found")
        old = _row_to_dc(old_row)
        current_revision = str(old_row["updated_at"])
        if expected_revision is not None and expected_revision != current_revision:
            if conn is None:
                db_conn.rollback()
            raise NoteRevisionConflictError(current_revision)
        new_id = _insert(
            db_conn,
            user_id=old.user_id,
            ticker=old.ticker,
            kind=kind or old.kind,
            status="open",
            body=body,
            anchor_type=old.anchor_type,
            anchor_key=old.anchor_key,
            fact_ref=old.fact_ref,
            source=source or "manual",
            source_ref=source_ref,
            supersedes_id=old.id,
            resolution_note=None,
            context=context,
            resolved_at=None,
            decision_id=old.decision_id,
            position_entry_id=old.position_entry_id,
            link_auto_resolve=old.link_auto_resolve,
        )
        db_conn.execute(
            "UPDATE analyst_notes SET status = 'superseded', updated_at = ? WHERE id = ?",
            (now_iso(), note_id),
        )
        if conn is None:
            db_conn.commit()
        return _fetch_one(db_conn, new_id)
    finally:
        if conn is None:
            db_conn.close()


# ---------------------------------------------------------------------------
# Comment-store reconciliation
# ---------------------------------------------------------------------------


def sync_store_comments(
    repo_root: Path,
    *,
    ticker: str,
    report_date: date,
    user_id: str = DEFAULT_USER_ID,
    db_path: Path | str | None = None,
) -> SyncStats:
    """Reconcile one report's comment file into analyst_notes (idempotent).

    Mirrors every comment (except ``platform_change`` — that's backlog, not
    memory) as a ``source='comment'`` note keyed on
    ``source_ref = '<TICKER>/<report_date>/<comment_id>'``, so re-running is
    an upsert, not a duplicate.

    Conflict contract for mirrored rows: the comment system stays the
    system of record for its own fields, but only *changes* propagate —
    ``context_json`` carries watermarks (``intent``, ``comment_status``)
    from the last sync, and kind/status are only rewritten when the
    comment-side value moved since then. A note the analyst reclassified
    or resolved directly therefore survives any number of re-syncs, while
    a comment freshly marked addressed still resolves its note.

    A comment that vanished from the store (user deleted it) archives its
    note when the note is still open; resolved/archived notes are left
    alone — ``clear_addressed`` housekeeping must not erase memory.
    """
    import comments as comments_mod  # lazy: comments lazily imports us back

    store = comments_mod.load_store(repo_root, ticker, report_date)
    prefix = f"{store.ticker}/{report_date.isoformat()}/"
    stats = SyncStats()
    conn = open_conn(db_path)
    try:
        existing_rows = conn.execute(
            "SELECT * FROM analyst_notes WHERE user_id = ? AND source = 'comment' "
            "AND source_ref LIKE ?",
            (user_id, prefix + "%"),
        ).fetchall()
        existing = {str(r["source_ref"]): _row_to_dc(r) for r in existing_rows}
        seen: set[str] = set()

        for c in store.comments:
            ref = prefix + c.id
            seen.add(ref)
            note = existing.get(ref)
            if c.intent == "platform_change":
                # Backlog item, not memory. If an earlier sync mirrored it
                # (the intent was classified later), retire the leftover note.
                stats.skipped += 1
                if note is not None and note.status == "open":
                    _archive(conn, note.id)
                    stats.archived += 1
                continue
            if note is None:
                _insert_from_comment(conn, user_id=user_id, store=store, comment=c, ref=ref)
                stats.created += 1
            elif _update_from_comment(conn, note=note, comment=c, store=store):
                stats.updated += 1

        for ref, note in existing.items():
            if ref not in seen and note.status == "open":
                _archive(conn, note.id)
                stats.archived += 1
        conn.commit()
        return stats
    finally:
        conn.close()


def _kind_for_comment(intent: str | None, text: str) -> str:
    if intent in _INTENT_TO_KIND:
        return _INTENT_TO_KIND[intent]
    return "question" if text.rstrip().endswith("?") else "observation"


def _comment_context(store: CommentStore, comment: Comment) -> dict[str, object]:
    """The mirrored note's context_json: display extras + sync watermarks.

    Reconciler-owned on mirrored rows — rewritten whenever the comment side
    changes.
    """
    ctx: dict[str, object] = {
        "report_date": store.report_date.isoformat(),
        "intent": comment.intent,
        "comment_status": comment.status,
    }
    if comment.anchor.tab:
        ctx["tab"] = comment.anchor.tab
    if comment.selected_text:
        ctx["selected_text"] = comment.selected_text
    if comment.follow_up_thread:
        ctx["thread"] = [
            {"role": t.role, "text": t.text, "created_at": t.created_at.isoformat()}
            for t in comment.follow_up_thread
        ]
    return ctx


def _insert_from_comment(
    conn: sqlite3.Connection, *, user_id: str, store: CommentStore, comment: Comment, ref: str
) -> None:
    status = _COMMENT_STATUS_TO_NOTE[comment.status]
    resolved_at: str | None = None
    if status == "resolved":
        resolved_at = (
            comment.addressed_at.isoformat() if comment.addressed_at is not None else now_iso()
        )
    _insert(
        conn,
        user_id=user_id,
        ticker=store.ticker,
        kind=_kind_for_comment(comment.intent, comment.comment),
        status=status,
        body=comment.comment,
        anchor_type=comment.anchor.type,
        anchor_key=comment.anchor.key,
        fact_ref=comment.anchor.fact_ref,
        source="comment",
        source_ref=ref,
        supersedes_id=None,
        resolution_note=comment.resolution_note,
        context=_comment_context(store, comment),
        resolved_at=resolved_at,
        created_at=comment.created_at.isoformat(),
    )


def _update_from_comment(
    conn: sqlite3.Connection, *, note: AnalystNoteRow, comment: Comment, store: CommentStore
) -> bool:
    """Propagate comment-side *changes* onto a mirrored note. Returns True when
    anything was written. Watermarks (see :func:`sync_store_comments`) keep
    note-side manual edits authoritative when the comment didn't move."""
    ctx = note.context or {}
    new_ctx = _comment_context(store, comment)
    sets: list[str] = []
    params: list[object] = []

    if comment.comment != note.body:
        sets.append("body = ?")
        params.append(comment.comment)
    if comment.intent != ctx.get("intent"):
        sets.append("kind = ?")
        params.append(_kind_for_comment(comment.intent, comment.comment))
    if comment.status != ctx.get("comment_status"):
        status = _COMMENT_STATUS_TO_NOTE[comment.status]
        sets.append("status = ?")
        params.append(status)
        if status == "resolved":
            sets.append("resolved_at = ?")
            params.append(comment.addressed_at.isoformat() if comment.addressed_at else now_iso())
    if (comment.resolution_note or None) != note.resolution_note:
        sets.append("resolution_note = ?")
        params.append(comment.resolution_note)
    # Backfill/refresh the stable handle (0099) — a comment whose anchor gained a
    # fact_ref since the last sync re-binds the mirrored note by identity.
    if (comment.anchor.fact_ref or None) != note.fact_ref:
        sets.append("fact_ref = ?")
        params.append(comment.anchor.fact_ref)
    if new_ctx != ctx:
        sets.append("context_json = ?")
        params.append(json.dumps(new_ctx))
    if not sets:
        return False
    sets.append("updated_at = ?")
    params.append(now_iso())
    params.append(note.id)
    conn.execute(f"UPDATE analyst_notes SET {', '.join(sets)} WHERE id = ?", params)
    return True


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _validate(field: str, value: str, allowed: tuple[str, ...]) -> None:
    if value not in allowed:
        raise ValueError(f"{field} must be one of {allowed}, got {value!r}")


def _insert(
    conn: sqlite3.Connection,
    *,
    user_id: str,
    ticker: str | None,
    kind: str,
    status: str,
    body: str,
    anchor_type: str | None,
    anchor_key: str | None,
    source: str,
    source_ref: str | None,
    supersedes_id: int | None,
    resolution_note: str | None,
    context: dict[str, object] | None,
    resolved_at: str | None,
    created_at: str | None = None,
    decision_id: int | None = None,
    position_entry_id: int | None = None,
    link_auto_resolve: bool = False,
    fact_ref: str | None = None,
) -> int:
    now = now_iso()
    columns = [
        "user_id", "ticker", "kind", "status", "body", "anchor_type", "anchor_key",
        "source", "source_ref", "supersedes_id", "resolution_note", "context_json",
        "created_at", "updated_at", "resolved_at",
    ]  # fmt: skip
    values: list[object] = [
        user_id,
        ticker.upper() if ticker else None,
        kind,
        status,
        body,
        anchor_type,
        anchor_key,
        source,
        source_ref,
        supersedes_id,
        resolution_note,
        json.dumps(context) if context is not None else None,
        created_at or now,
        now,
        resolved_at,
    ]
    # Optional columns (0093 links + 0099 fact_ref) join the statement only when
    # actually set, so the default write path keeps working against pre-0093 /
    # pre-0099 / hand-rolled test schemas; passing one against a schema that
    # lacks the column fails loudly instead (the feature needs the migration).
    for column, value in (
        ("decision_id", decision_id),
        ("position_entry_id", position_entry_id),
        ("link_auto_resolve", 1 if link_auto_resolve else None),
        ("fact_ref", fact_ref),
    ):
        if value is not None:
            columns.append(column)
            values.append(value)
    cur = conn.execute(
        f"INSERT INTO analyst_notes({', '.join(columns)}) VALUES ({', '.join('?' * len(columns))})",
        values,
    )
    return int(cur.lastrowid or 0)


def _archive(conn: sqlite3.Connection, note_id: int) -> None:
    conn.execute(
        "UPDATE analyst_notes SET status = 'archived', updated_at = ? WHERE id = ?",
        (now_iso(), note_id),
    )


def _set_status(
    note_id: int,
    status: str,
    *,
    resolution_note: str | None,
    db_path: Path | str | None,
) -> AnalystNoteRow | None:
    conn = open_conn(db_path)
    try:
        now = now_iso()
        if status == "resolved":
            cur = conn.execute(
                "UPDATE analyst_notes SET status = 'resolved', resolved_at = ?, "
                "resolution_note = COALESCE(?, resolution_note), updated_at = ? WHERE id = ?",
                (now, resolution_note, now, note_id),
            )
        else:
            cur = conn.execute(
                "UPDATE analyst_notes SET status = ?, updated_at = ? WHERE id = ?",
                (status, now, note_id),
            )
        if cur.rowcount == 0:
            return None
        conn.commit()
        return _fetch_one(conn, note_id)
    finally:
        conn.close()


def _fetch_one(conn: sqlite3.Connection, row_id: int) -> AnalystNoteRow:
    row = conn.execute("SELECT * FROM analyst_notes WHERE id = ?", (row_id,)).fetchone()
    if row is None:
        raise LookupError(f"analyst_notes id={row_id} not found after write")
    return _row_to_dc(row)


def _link_col(row: sqlite3.Row, name: str) -> int | None:
    """A 0093 link column, or None on a pre-0093 / hand-rolled schema."""
    try:
        raw = row[name]
    except IndexError:
        return None
    return None if raw is None else int(raw)


def _text_col(row: sqlite3.Row, name: str) -> str | None:
    """A nullable text column that may be absent on a pre-migration / hand-rolled
    schema (e.g. 0099's ``fact_ref``)."""
    try:
        raw = row[name]
    except IndexError:
        return None
    return None if raw is None else str(raw)


def _row_to_dc(row: sqlite3.Row) -> AnalystNoteRow:
    raw_ctx = row["context_json"]
    context: dict[str, object] | None = None
    if raw_ctx is not None:
        try:
            parsed: object = json.loads(str(raw_ctx))
        except ValueError:
            parsed = None  # corrupt context is display sugar, never fatal
        if isinstance(parsed, dict):
            context = cast("dict[str, object]", parsed)
    raw_resolved = row["resolved_at"]
    raw_supersedes = row["supersedes_id"]
    raw_ticker = row["ticker"]
    return AnalystNoteRow(
        id=int(row["id"]),
        user_id=str(row["user_id"]),
        ticker=(None if raw_ticker is None else str(raw_ticker)),
        kind=str(row["kind"]),
        status=str(row["status"]),
        body=str(row["body"]),
        anchor_type=(None if row["anchor_type"] is None else str(row["anchor_type"])),
        anchor_key=(None if row["anchor_key"] is None else str(row["anchor_key"])),
        source=str(row["source"]),
        source_ref=(None if row["source_ref"] is None else str(row["source_ref"])),
        supersedes_id=(None if raw_supersedes is None else int(raw_supersedes)),
        resolution_note=(None if row["resolution_note"] is None else str(row["resolution_note"])),
        context=context,
        created_at=parse_dt(row["created_at"]),
        updated_at=parse_dt(row["updated_at"]),
        resolved_at=(None if raw_resolved is None else parse_dt(raw_resolved)),
        decision_id=_link_col(row, "decision_id"),
        position_entry_id=_link_col(row, "position_entry_id"),
        link_auto_resolve=bool(_link_col(row, "link_auto_resolve") or 0),
        fact_ref=_text_col(row, "fact_ref"),
    )
