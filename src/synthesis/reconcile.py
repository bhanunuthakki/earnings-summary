"""Seed-corpus reconciliation — the one-time freshness pass, then the standing seam.

The owner's 2026-07-02 callout: the seed corpus went stale within a day of
landing (the NVDA-LEAP intent was resolved-rejected in a Claude chat while the
corpus — and two other sessions — still treated it as live). "If it becomes
stale, I just won't even use it because it will be shit." So every seed item
gets a one-tap verdict, and the coach may only lean on items that survived:

- verdicts: ``live`` (still true — reviewed and kept), ``superseded`` (a newer
  belief replaced it), ``resolved-rejected`` (considered and killed),
  ``done`` (played out / executed)
- falsifiers: the interview back-filled several with an ``(inferred)`` marker —
  words the owner never said. The coach may only quote a falsifier once the
  marker is gone: ``ratify`` strips it, ``edit`` replaces the text with the
  owner's own words, ``drop`` clears it.

Anything without a verdict stays visibly unreconciled; nothing here fires an
LLM. This module is pure state — the Ledger panel renders it, two routes call
it.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from user_state._db import now_iso, open_conn

RECONCILE_VERDICTS: tuple[str, ...] = ("live", "superseded", "resolved-rejected", "done")
FALSIFIER_ACTIONS: tuple[str, ...] = ("ratify", "edit", "drop")

# verdict → analyst_notes.status ('live' keeps the row open; the context stamp
# alone marks it reviewed)
_VERDICT_STATUS = {
    "live": None,
    "superseded": "superseded",
    "resolved-rejected": "resolved",
    "done": "resolved",
}

_INFERRED_RE = re.compile(r"\s*\(inferred\)\s*$", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class ReconcileItem:
    kind: str  # 'note' (musing/decision/intent) | 'theme' | 'falsifier'
    item_id: int
    label: str
    body: str
    source_ref: str | None = None


def list_unreconciled(db_path: Path | str | None = None) -> list[ReconcileItem]:
    """Seed items still awaiting a verdict, plus unratified falsifiers."""
    items: list[ReconcileItem] = []
    conn = open_conn(db_path)
    try:
        for row in conn.execute(
            """
            SELECT id, kind, body, source_ref FROM analyst_notes
            WHERE source_ref LIKE 'seed:%'
              AND json_extract(coalesce(context_json,'{}'), '$.reconcile') IS NULL
            ORDER BY id
            """
        ).fetchall():
            items.append(
                ReconcileItem(
                    kind="note",
                    item_id=int(row[0]),
                    label=str(row[1]),
                    body=str(row[2]),
                    source_ref=str(row[3] or ""),
                )
            )
        for row in conn.execute(
            """
            SELECT id, scope_key, body_md FROM insight_notes
            WHERE kind = 'theme' AND status = 'current'
              AND json_extract(coalesce(meta_json,'{}'), '$.reconcile') IS NULL
            ORDER BY id
            """
        ).fetchall():
            items.append(
                ReconcileItem(
                    kind="theme",
                    item_id=int(row[0]),
                    label=str(row[1]),
                    body=str(row[2]),
                )
            )
        for row in conn.execute(
            """
            SELECT id, ticker, falsifier FROM decisions
            WHERE decided_by = 'owner' AND falsifier LIKE '%(inferred)%'
            ORDER BY id
            """
        ).fetchall():
            items.append(
                ReconcileItem(
                    kind="falsifier",
                    item_id=int(row[0]),
                    label=str(row[1] or "portfolio"),
                    body=str(row[2]),
                )
            )
    finally:
        conn.close()
    return items


def list_missing_falsifiers(db_path: Path | str | None = None) -> list[ReconcileItem]:
    """Held positions whose owner decisions carry no falsifier at all — the
    tripwire-coverage gap the auto-reconcile moot-drop must never hide.

    A live held position with no falsifier has nothing for the break engine to
    guard (the attach flow skips empty falsifiers), and only the owner's own
    words are quotable — an irreducible owner-only ask. Position-level, not
    row-level: one ask per held ticker with owner decisions and ZERO falsifier
    text on any of them. An '(inferred)' falsifier is pending in the ratify
    queue, not a gap — asking twice for the same position violates the density
    standard. Routes to the newest owner decision on the name; closed positions
    never ask (the moot-drop already handled those)."""
    conn = open_conn(db_path)
    try:
        rows = conn.execute(
            """
            SELECT d.id, d.ticker, d.recommendation_kind FROM decisions d
            WHERE d.decided_by = 'owner' AND d.ticker IS NOT NULL
              AND d.id = (SELECT MAX(o.id) FROM decisions o
                          WHERE o.decided_by = 'owner' AND o.ticker = d.ticker)
              AND NOT EXISTS (
                    SELECT 1 FROM decisions f
                    WHERE f.decided_by = 'owner' AND f.ticker = d.ticker
                      AND TRIM(coalesce(f.falsifier, '')) != '')
              AND EXISTS (
                    SELECT 1 FROM tracked_companies t
                    WHERE t.ticker = d.ticker AND t.list_type = 'portfolio')
            ORDER BY d.ticker
            """
        ).fetchall()
        return [
            ReconcileItem(
                kind="falsifier-missing",
                item_id=int(row[0]),
                label=str(row[1]),
                body=str(row[2] or ""),
            )
            for row in rows
        ]
    finally:
        conn.close()


def reconcile_note(note_id: int, verdict: str, *, db_path: Path | str | None = None) -> bool:
    """Stamp a verdict on a seed note (musing / decision note / intent)."""
    if verdict not in RECONCILE_VERDICTS:
        raise ValueError(f"unknown verdict {verdict!r}; expected one of {RECONCILE_VERDICTS}")
    stamp = now_iso()
    status = _VERDICT_STATUS[verdict]
    conn = open_conn(db_path)
    try:
        cur = conn.execute(
            """
            UPDATE analyst_notes
            SET context_json = json_set(coalesce(context_json,'{}'),
                                        '$.reconcile', ?, '$.reconciled_at', ?),
                status = coalesce(?, status),
                updated_at = ?
            WHERE id = ?
            """,
            (verdict, stamp, status, stamp, note_id),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def reconcile_theme(insight_id: int, verdict: str, *, db_path: Path | str | None = None) -> bool:
    """Stamp a verdict on a theme insight; non-live verdicts retire it."""
    if verdict not in RECONCILE_VERDICTS:
        raise ValueError(f"unknown verdict {verdict!r}; expected one of {RECONCILE_VERDICTS}")
    stamp = now_iso()
    conn = open_conn(db_path)
    try:
        cur = conn.execute(
            """
            UPDATE insight_notes
            SET meta_json = json_set(coalesce(meta_json,'{}'),
                                     '$.reconcile', ?, '$.reconciled_at', ?),
                status = CASE WHEN ? = 'live' THEN status ELSE 'superseded' END,
                updated_at = ?
            WHERE id = ? AND kind = 'theme'
            """,
            (verdict, stamp, verdict, stamp, insight_id),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def falsifier_action(
    decision_id: int,
    action: str,
    *,
    text: str | None = None,
    db_path: Path | str | None = None,
) -> bool:
    """Ratify / edit / drop an owner decision's falsifier.

    Ratifying strips the trailing ``(inferred)`` marker — the falsifier becomes
    quotable by the coach. ``edit`` requires the replacement text (the owner's
    own words, quotable by construction)."""
    if action not in FALSIFIER_ACTIONS:
        raise ValueError(f"unknown action {action!r}; expected one of {FALSIFIER_ACTIONS}")
    if action == "edit" and not (text or "").strip():
        raise ValueError("edit requires the replacement falsifier text")
    conn = open_conn(db_path)
    try:
        row = conn.execute(
            "SELECT falsifier FROM decisions WHERE id = ? AND decided_by = 'owner'",
            (decision_id,),
        ).fetchone()
        if row is None:
            return False
        if action == "ratify":
            new_value: str | None = _INFERRED_RE.sub("", str(row[0] or "")).strip() or None
        elif action == "edit":
            new_value = str(text).strip()
        else:  # drop
            new_value = None
        conn.execute("UPDATE decisions SET falsifier = ? WHERE id = ?", (new_value, decision_id))
        # The audit trail of the ratification pass rides in user_notes.
        conn.execute(
            "UPDATE decisions SET user_notes = user_notes || ? WHERE id = ?",
            (f" · falsifier:{action} {json.dumps(now_iso())}", decision_id),
        )
        conn.commit()
        return True
    finally:
        conn.close()


def close_intent(
    source_ref: str,
    verdict: str,
    *,
    reason: str,
    closed_by: str,
    db_path: Path | str | None = None,
) -> bool:
    """Close a standing intent with provenance — the freshness rule's write path.

    Called by the claude_session landing channel when a chat resolves a topic
    the corpus still carries as live (the NVDA-LEAP failure mode). ``verdict``
    is ``resolved-rejected`` or ``done``; the closure stamps who/why so the
    coach's freshness gate can verify the intent is settled."""
    if verdict not in ("resolved-rejected", "done"):
        raise ValueError(f"intent closure verdict must be resolved-rejected|done, got {verdict!r}")
    stamp = now_iso()
    conn = open_conn(db_path)
    try:
        cur = conn.execute(
            """
            UPDATE analyst_notes
            SET context_json = json_set(coalesce(context_json,'{}'),
                                        '$.status', ?, '$.closed_by', ?,
                                        '$.closed_at', ?, '$.reason', ?,
                                        '$.reconcile', ?, '$.reconciled_at', ?),
                status = 'resolved',
                resolved_at = ?,
                updated_at = ?
            WHERE kind = 'intent' AND source_ref = ? AND status != 'resolved'
            """,
            (verdict, closed_by, stamp, reason, verdict, stamp, stamp, stamp, source_ref),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()
