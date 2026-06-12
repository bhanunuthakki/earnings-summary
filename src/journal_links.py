"""Journal ↔ decision/position links — validation + reconciliation (S15 PR1).

``analyst_notes`` rows can reference the decision (decisions.id) and/or the
position-lifecycle stint (position_entries.id) they are about (alembic 0093).
This module owns everything that crosses those table boundaries:

  link_note / unlink_note
      The validating write path over ``user_state.notes.set_note_links`` —
      a link target must exist (LookupError otherwise), because the columns
      are deliberately plain INTEGERs (no REFERENCES clause; see 0093's
      docstring for the partial-schema rationale).

  linkable_targets(ticker)
      What the journal panel offers in its link dropdown: recent decisions
      + lifecycle stints for one name, each pre-labeled and carrying its
      concluded state.

  pending_reconciliation()
      The "pending reconciliation" view: OPEN notes whose linked decision
      has been graded (outcome recorded, not 'pending') or whose linked
      position has exited. Each item carries a suggested resolution line so
      one click closes the loop.

  reconcile_linked_notes()
      The morning rung (stage 0c, after ``sync_position_lifecycle``):
      auto-resolves the subset that opted in at link time
      (``link_auto_resolve=1``); everything else stays for the view and the
      inbox resurfacing. Auto-resolution stamps an honest resolution_note
      ("auto-resolved: …") so memory records *how* it closed.

Read paths are best-effort (missing DB / pre-0093 schema → empty), matching
decision_extractor / position_lifecycle; the write path fails loud like the
rest of user_state.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from identity import DEFAULT_USER_ID
from user_state.notes import AnalystNoteRow, get_note, resolve_note, set_note_links

log = logging.getLogger(__name__)

TARGET_DECISION = "decision"
TARGET_POSITION = "position"

# A decision is concluded once an outcome is actually recorded ('pending' is
# the at-insert placeholder, not a verdict); a position once it has exited.
_DECISION_CONCLUDED_SQL = (
    "d.outcome_at IS NOT NULL AND COALESCE(d.outcome_label, 'pending') != 'pending'"
)


@dataclass(frozen=True, slots=True)
class LinkTarget:
    """One linkable (or linked) object, pre-labeled for display."""

    kind: str  # TARGET_DECISION | TARGET_POSITION
    target_id: int
    ticker: str
    label: str  # "TRIM 20% · 2026-05-02" / "2026-01-15 → open"
    concluded: bool
    conclusion: str | None  # "graded correct (+18.2%)" / "exited 2026-06-01"


@dataclass(frozen=True, slots=True)
class ReconciliationItem:
    """One open note whose linked object has concluded."""

    note: AnalystNoteRow
    target: LinkTarget
    suggested_resolution: str


# ---------------------------------------------------------------------------
# DB plumbing (read side — best-effort)
# ---------------------------------------------------------------------------


def _resolve(db_path: Path | str | None) -> Path | None:
    if db_path is not None:
        return Path(db_path)
    try:
        from db import DB_PATH

        return Path(DB_PATH)
    except ImportError:
        return None


def _open_ro(db_path: Path | str | None) -> sqlite3.Connection | None:
    try:
        path = _resolve(db_path)
        if path is None or not path.exists():
            return None
        conn = sqlite3.connect(str(path), timeout=5.0)
        conn.execute("PRAGMA busy_timeout = 5000")
        conn.row_factory = sqlite3.Row
        return conn
    except (sqlite3.Error, OSError):
        return None


def _safe_rows(
    conn: sqlite3.Connection, sql: str, params: tuple[object, ...] = ()
) -> list[sqlite3.Row]:
    try:
        return conn.execute(sql, params).fetchall()
    except sqlite3.Error:
        return []  # missing table / pre-0093 column — degrade to empty


# ---------------------------------------------------------------------------
# Target labeling
# ---------------------------------------------------------------------------


def _decision_target(row: sqlite3.Row) -> LinkTarget:
    kind = str(row["recommendation_kind"]).upper()
    value = row["recommendation_value"]
    head = f"{kind} {float(value):g}%" if value is not None else kind
    made = str(row["made_at"] or "")[:10]
    conviction = str(row["conviction"]) if row["conviction"] else None
    label = f"{head} · {made}" + (f" · {conviction} conviction" if conviction else "")
    outcome_at = row["outcome_at"]
    outcome_label = str(row["outcome_label"] or "pending")
    concluded = outcome_at is not None and outcome_label != "pending"
    conclusion: str | None = None
    if concluded:
        conclusion = f"graded {outcome_label}"
        if row["outcome_pct"] is not None:
            conclusion += f" ({float(row['outcome_pct']):+.1f}%)"
    return LinkTarget(
        kind=TARGET_DECISION,
        target_id=int(row["id"]),
        ticker=str(row["ticker"]).upper(),
        label=label,
        concluded=concluded,
        conclusion=conclusion,
    )


def _position_target(row: sqlite3.Row) -> LinkTarget:
    entry = str(row["entry_date"]) if row["entry_date"] else "opening unknown"
    exit_date = str(row["exit_date"]) if row["exit_date"] else None
    label = f"{entry} → {exit_date or 'open'}"
    conclusion: str | None = None
    if exit_date is not None:
        conclusion = f"exited {exit_date}"
        if row["outcome_vs_thesis"]:
            conclusion += f", {row['outcome_vs_thesis']}"
    return LinkTarget(
        kind=TARGET_POSITION,
        target_id=int(row["id"]),
        ticker=str(row["ticker"]).upper(),
        label=label,
        concluded=exit_date is not None,
        conclusion=conclusion,
    )


def linkable_targets(
    *,
    ticker: str,
    db_path: Path | str | None,
    user_id: str = DEFAULT_USER_ID,
    decisions_limit: int = 8,
    positions_limit: int = 5,
) -> list[LinkTarget]:
    """Recent decisions + lifecycle stints for one name — the journal panel's
    link dropdown. Decisions first (newest-first), then stints (open first).
    Empty on a missing DB / pre-link schema."""
    conn = _open_ro(db_path)
    if conn is None:
        return []
    try:
        t = ticker.upper()
        targets: list[LinkTarget] = []
        for row in _safe_rows(
            conn,
            "SELECT * FROM decisions WHERE ticker = ? ORDER BY made_at DESC, id DESC LIMIT ?",
            (t, decisions_limit),
        ):
            targets.append(_decision_target(row))
        for row in _safe_rows(
            conn,
            "SELECT * FROM position_entries WHERE user_id = ? AND ticker = ? "
            "ORDER BY (exit_date IS NULL) DESC, COALESCE(exit_date, '') DESC, id DESC LIMIT ?",
            (user_id, t, positions_limit),
        ):
            targets.append(_position_target(row))
        return targets
    finally:
        conn.close()


def get_target(
    *,
    kind: str,
    target_id: int,
    db_path: Path | str | None,
) -> LinkTarget | None:
    """One target by kind+id, labeled — None when it (or its table) is absent."""
    conn = _open_ro(db_path)
    if conn is None:
        return None
    try:
        if kind == TARGET_DECISION:
            rows = _safe_rows(conn, "SELECT * FROM decisions WHERE id = ?", (target_id,))
            return _decision_target(rows[0]) if rows else None
        if kind == TARGET_POSITION:
            rows = _safe_rows(conn, "SELECT * FROM position_entries WHERE id = ?", (target_id,))
            return _position_target(rows[0]) if rows else None
        return None
    finally:
        conn.close()


def targets_for_notes(
    notes: list[AnalystNoteRow], *, db_path: Path | str | None
) -> dict[tuple[str, int], LinkTarget]:
    """Batch labeling for a note list render: every (kind, id) a note in the
    list links to → its LinkTarget. Two IN-queries total."""
    decision_ids = sorted({n.decision_id for n in notes if n.decision_id is not None})
    position_ids = sorted({n.position_entry_id for n in notes if n.position_entry_id is not None})
    if not decision_ids and not position_ids:
        return {}
    conn = _open_ro(db_path)
    if conn is None:
        return {}
    try:
        out: dict[tuple[str, int], LinkTarget] = {}
        if decision_ids:
            marks = ",".join("?" * len(decision_ids))
            for row in _safe_rows(
                conn, f"SELECT * FROM decisions WHERE id IN ({marks})", tuple(decision_ids)
            ):
                target = _decision_target(row)
                out[(TARGET_DECISION, target.target_id)] = target
        if position_ids:
            marks = ",".join("?" * len(position_ids))
            for row in _safe_rows(
                conn, f"SELECT * FROM position_entries WHERE id IN ({marks})", tuple(position_ids)
            ):
                target = _position_target(row)
                out[(TARGET_POSITION, target.target_id)] = target
        return out
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Link / unlink (the validating write path)
# ---------------------------------------------------------------------------


def link_note(
    note_id: int,
    *,
    decision_id: int | None = None,
    position_entry_id: int | None = None,
    auto_resolve: bool = False,
    db_path: Path | str | None = None,
) -> AnalystNoteRow | None:
    """Attach a note to a decision and/or a position stint.

    Validates each supplied target exists (LookupError when it doesn't —
    including when its table is absent); raises ValueError when neither is
    supplied. ``auto_resolve`` is the note's latest stated choice — relinking
    overwrites it. Returns the updated note, None when the note is missing."""
    if decision_id is None and position_entry_id is None:
        raise ValueError("provide decision_id and/or position_entry_id")
    for kind, target_id in (
        (TARGET_DECISION, decision_id),
        (TARGET_POSITION, position_entry_id),
    ):
        if target_id is None:
            continue
        if get_target(kind=kind, target_id=target_id, db_path=db_path) is None:
            raise LookupError(f"{kind} id={target_id} not found")
    if decision_id is not None and position_entry_id is not None:
        return set_note_links(
            note_id,
            decision_id=decision_id,
            position_entry_id=position_entry_id,
            link_auto_resolve=auto_resolve,
            db_path=db_path,
        )
    if decision_id is not None:
        return set_note_links(
            note_id,
            decision_id=decision_id,
            link_auto_resolve=auto_resolve,
            db_path=db_path,
        )
    return set_note_links(
        note_id,
        position_entry_id=position_entry_id,
        link_auto_resolve=auto_resolve,
        db_path=db_path,
    )


def unlink_note(note_id: int, *, db_path: Path | str | None = None) -> AnalystNoteRow | None:
    """Detach both links (and the auto-resolve flag). The note stays open —
    unlinking is how a pending-reconciliation item says "this note outlives
    its decision"."""
    return set_note_links(
        note_id,
        decision_id=None,
        position_entry_id=None,
        link_auto_resolve=False,
        db_path=db_path,
    )


# ---------------------------------------------------------------------------
# Reconciliation — the view + the rung
# ---------------------------------------------------------------------------


def pending_reconciliation(
    *,
    db_path: Path | str | None,
    user_id: str = DEFAULT_USER_ID,
    ticker: str | None = None,
    limit: int = 50,
) -> list[ReconciliationItem]:
    """OPEN notes whose linked decision graded / linked position exited,
    newest-concluded first. A note linked to both objects surfaces once —
    the decision conclusion wins (it carries the verdict)."""
    conn = _open_ro(db_path)
    if conn is None:
        return []
    try:
        ticker_clause = " AND n.ticker = ?" if ticker is not None else ""
        ticker_params = (ticker.upper(),) if ticker is not None else ()
        # d.*/p.* lead the SELECT so the labelers read the target columns;
        # the only added name (note_id) is aliased — no Row-key collisions.
        decision_rows = _safe_rows(
            conn,
            f"""
            SELECT d.*, n.id AS note_id
            FROM analyst_notes n JOIN decisions d ON d.id = n.decision_id
            WHERE n.user_id = ? AND n.status = 'open' AND {_DECISION_CONCLUDED_SQL}
            {ticker_clause}
            ORDER BY d.outcome_at DESC, n.id DESC LIMIT ?
            """,
            (user_id, *ticker_params, limit),
        )
        position_rows = _safe_rows(
            conn,
            f"""
            SELECT p.*, n.id AS note_id
            FROM analyst_notes n JOIN position_entries p ON p.id = n.position_entry_id
            WHERE n.user_id = ? AND n.status = 'open' AND p.exit_date IS NOT NULL
            {ticker_clause}
            ORDER BY p.exit_date DESC, n.id DESC LIMIT ?
            """,
            (user_id, *ticker_params, limit),
        )
    finally:
        conn.close()

    items: list[ReconciliationItem] = []
    seen: set[int] = set()
    for rows, labeler in ((decision_rows, _decision_target), (position_rows, _position_target)):
        for row in rows:
            note_id = int(row["note_id"])
            if note_id in seen:
                continue
            note = get_note(note_id, db_path=db_path)
            if note is None:
                continue
            target = labeler(row)
            seen.add(note_id)
            items.append(
                ReconciliationItem(
                    note=note, target=target, suggested_resolution=_suggestion(target)
                )
            )
    return items[:limit]


def _suggestion(target: LinkTarget) -> str:
    return (
        f"Linked {target.kind} concluded — {target.ticker} {target.label}: "
        f"{target.conclusion or 'concluded'}."
    )


def pending_reconciliation_note_ids(
    *, db_path: Path | str | None, user_id: str = DEFAULT_USER_ID
) -> dict[int, str]:
    """note_id → one-line conclusion, for surfaces that already hold the
    notes (the inbox stream) and only need the marker."""
    return {
        item.note.id: _suggestion(item.target)
        for item in pending_reconciliation(db_path=db_path, user_id=user_id)
    }


def reconcile_linked_notes(
    *,
    db_path: Path | str | None,
    user_id: str = DEFAULT_USER_ID,
) -> dict[str, int]:
    """The morning pass: resolve open notes that opted into auto-resolve and
    whose linked object concluded; count (don't touch) the rest. Idempotent —
    a resolved note leaves the pending set. Returns
    ``{auto_resolved, pending, db_unavailable}``."""
    items = pending_reconciliation(db_path=db_path, user_id=user_id)
    if not items:
        conn = _open_ro(db_path)
        if conn is None:
            return {"auto_resolved": 0, "pending": 0, "db_unavailable": 1}
        conn.close()
        return {"auto_resolved": 0, "pending": 0, "db_unavailable": 0}
    auto = pending = 0
    for item in items:
        if item.note.link_auto_resolve:
            resolve_note(
                item.note.id,
                resolution_note=f"auto-resolved: {item.suggested_resolution}",
                db_path=db_path,
            )
            auto += 1
            log.info(
                {
                    "event": "journal_link_auto_resolved",
                    "note_id": item.note.id,
                    "target": f"{item.target.kind}:{item.target.target_id}",
                }
            )
        else:
            pending += 1
    return {"auto_resolved": auto, "pending": pending, "db_unavailable": 0}
