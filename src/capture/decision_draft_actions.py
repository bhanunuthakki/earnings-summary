"""The ONE action core for Decision Draft confirm/correct/dismiss/expire (PRD
§9.2/§11.6). Telegram callbacks, the desktop Ledger, and the mobile Inbox all
call THIS module — never a route handler that reimplements the state
transition, matching every other action-core in this repo (``research.
proposals.act_on_proposal``, ``research.investment_decision_card.act_on_card``).

Persistence rules (verbatim from the PRD):
  * confirming/correcting is the ONLY path that creates/updates an Owner
    Decision (``decisions.decided_by = 'owner'``);
  * reprocessing the same source never duplicates a decision — confirming an
    already-confirmed/corrected/dismissed draft is a no-op returning the
    existing outcome;
  * a Pass/Watch/Promote disposition linked to a live Investment Decision
    Card artifact reuses that card's OWN action core
    (``research.investment_decision_card.act_on_card``) so the two owner
    surfaces (the card's own disposition buttons, and a Decision Draft that
    happens to say the same thing) can never diverge or double-write;
  * migration 0195's ``ck_decision_drafts_decision_required`` CHECK enforces
    that every ``confirmed``/``corrected`` row carries a ``decision_id`` — a
    draft with no actionable ``proposed_action`` (a bare rationale/
    correction/request) confirms by ATTACHING to the most recent existing
    decision for its ticker (mirrors ``coach_reply``'s ``annotate_decision``
    intent) rather than fabricating a new one; when no such decision exists,
    confirming is refused (:class:`DraftActionError`) and the owner's real
    option is Dismiss (filed, not lost — the raw note stays intact either
    way).
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

from capture.decision_draft import (
    DISPOSITION_ACTIONS,
    DecisionDraft,
    DecisionDraftRow,
    get_draft,
    set_draft_status,
)
from user_state._db import now_iso, open_conn

log = logging.getLogger(__name__)


class DraftActionError(RuntimeError):
    """Raised when a confirm/correct/dismiss cannot be applied (unknown/
    terminal draft, or a confirm with nothing decision-shaped to attach to)."""


_TERMINAL_STATUSES = frozenset({"confirmed", "corrected", "dismissed", "expired"})
_ADVICE_LOOKBACK_DAYS = 30


def _now() -> str:
    return datetime.now(UTC).replace(tzinfo=None).isoformat()


def _most_recent_decision_id(conn: sqlite3.Connection, *, ticker: str | None) -> int | None:
    """The most recent decision for ``ticker`` (or the most recent
    portfolio-scope decision when ``ticker`` is None) within the standard
    advice-lookback window — the attach target for a rationale/correction/
    request confirm with no proposed action of its own."""
    cutoff = (datetime.now(UTC).replace(tzinfo=None)).isoformat()
    if ticker:
        row = conn.execute(
            "SELECT id FROM decisions WHERE UPPER(ticker) = ? "
            "AND made_at <= ? ORDER BY made_at DESC LIMIT 1",
            (ticker.upper(), cutoff),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT id FROM decisions WHERE scope = 'portfolio' "
            "AND made_at <= ? ORDER BY made_at DESC LIMIT 1",
            (cutoff,),
        ).fetchone()
    return int(row[0]) if row is not None else None


def _card_artifact_for(conn: sqlite3.Connection, artifact_id: int | None) -> tuple[int, str] | None:
    """(artifact_id, ticker) when ``artifact_id`` is a LIVE
    ``investment_decision_card`` artifact, else None."""
    if artifact_id is None:
        return None
    row = conn.execute(
        "SELECT id, ticker FROM llm_artifacts WHERE id = ? "
        "AND purpose = 'investment_decision_card'",
        (artifact_id,),
    ).fetchone()
    if row is None or not row[1]:
        return None
    return int(row[0]), str(row[1]).upper()


def _write_owner_decision(
    conn: sqlite3.Connection,
    *,
    ticker: str | None,
    recommendation_kind: str,
    recommendation_value: float | None,
    size_usd: float | None,
    size_pct: float | None,
    rationale_excerpt: str | None,
    advice_artifact_id: int | None,
) -> int:
    """Raw INSERT mirroring ``research.investment_decision_card.act_on_card``'s
    own INSERT shape — the one other place this repo writes an owner
    decision directly (no ``decision_extractor.record_decision``, which is
    shaped for the ADVISOR-extraction provenance chain, not this one)."""
    now = _now()
    scope = "portfolio" if not ticker else "ticker"
    cur = conn.execute(
        """
        INSERT INTO decisions (
            ticker, recommendation_kind, recommendation_value, decided_by, scope,
            size_usd, size_pct, advice_artifact_id, rationale_excerpt,
            made_at, created_at
        ) VALUES (?, ?, ?, 'owner', ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            ticker,
            recommendation_kind,
            recommendation_value,
            scope,
            size_usd,
            size_pct,
            advice_artifact_id,
            rationale_excerpt,
            now,
            now,
        ),
    )
    conn.commit()
    return int(cur.lastrowid or 0)


def _maybe_note_profile_implication(draft: DecisionDraft, *, db_path: Path | str | None) -> None:
    """A 'correction' intent MAY imply a consequential owner-profile change
    (PRD: "consequential owner-profile changes use the existing proposal/
    affirmation gate rather than direct mutation"). ``owner_profile.store.
    append_fact`` needs a CLOSED category and a schema-validated per-kind
    value dict (``owner_profile/models.py``) — there is no free-text ->
    per-kind-model extractor, and building one is explicitly out of scope
    here (documented gap, same shape as ``capture.coach_reply._apply_outcome``'s
    ``profile_fact`` branch). Confirming a correction therefore never mutates
    the profile store directly; the raw note (already landed, unabridged)
    remains the durable record. Intentionally a no-op today — kept as its
    own function so a future structured-extraction call has a single call
    site to land in."""
    del draft, db_path  # documented gap — see docstring


def _apply_confirmed_action(
    draft_row: DecisionDraftRow, *, db_path: Path | str | None
) -> tuple[int, str]:
    """Resolve the confirmed draft to (decision_id, receipt). Raises
    :class:`DraftActionError` when there is nothing decision-shaped to attach
    to (the caller's real option is then Dismiss)."""
    draft = draft_row.draft
    if draft is None:
        raise DraftActionError(f"draft {draft_row.id} has no parsed fields to confirm")

    conn = open_conn(db_path)
    try:
        action = draft.proposed_action
        ticker = draft.proposed_ticker

        if action in DISPOSITION_ACTIONS:
            card = _card_artifact_for(conn, draft.linked_advice_artifact_id)
            if card is not None:
                from research.investment_decision_card import act_on_card

                receipt = act_on_card(
                    card[0], action, db_path=db_path, notes=draft.proposed_rationale
                )
                # act_on_card is idempotent and doesn't hand back a decision_id
                # directly — look up what it just wrote (or already had).
                row = conn.execute(
                    "SELECT id FROM decisions WHERE advice_artifact_id = ? "
                    "AND recommendation_kind = ?",
                    (card[0], action),
                ).fetchone()
                decision_id = (
                    int(row[0])
                    if row is not None
                    else _most_recent_decision_id(conn, ticker=ticker or card[1])
                )
                if decision_id is None:
                    raise DraftActionError(
                        f"draft {draft_row.id}: act_on_card did not produce a decision row"
                    )
                return decision_id, receipt
            decision_id = _write_owner_decision(
                conn,
                ticker=ticker,
                recommendation_kind=action,
                recommendation_value=None,
                size_usd=None,
                size_pct=None,
                rationale_excerpt=draft.proposed_rationale,
                advice_artifact_id=draft.linked_advice_artifact_id,
            )
            return decision_id, f"{action}_recorded"

        if action in {"buy", "sell", "add", "trim", "hold"}:
            decision_id = _write_owner_decision(
                conn,
                ticker=ticker,
                recommendation_kind=action,
                recommendation_value=draft.proposed_amount_pct,
                size_usd=draft.proposed_amount_usd,
                size_pct=draft.proposed_amount_pct,
                rationale_excerpt=draft.proposed_rationale,
                advice_artifact_id=draft.linked_advice_artifact_id,
            )
            return decision_id, "decision_recorded"

        # rationale / correction / request with no proposed action of its own
        # -> attach to the most recent existing decision (coach_reply's
        # annotate_decision shape), never fabricate a new one.
        decision_id = _most_recent_decision_id(conn, ticker=ticker)
        if decision_id is None:
            raise DraftActionError(
                f"draft {draft_row.id}: nothing decision-shaped to confirm — dismiss instead"
            )
        if draft.proposed_rationale:
            conn.execute(
                "UPDATE decisions SET user_notes = COALESCE(user_notes, '') || ? WHERE id = ?",
                (f"\n\n---\nDraft #{draft_row.id}: {draft.proposed_rationale}", decision_id),
            )
            conn.commit()
        _maybe_note_profile_implication(draft, db_path=db_path)
        return decision_id, f"attached_to_decision:{decision_id}"
    finally:
        conn.close()


def confirm_draft(draft_id: int, *, db_path: Path | str | None = None) -> dict[str, object]:
    """Accept the parsed fields as-is. Idempotent: confirming an
    already-confirmed/corrected draft is a no-op returning its existing
    decision id and receipt 'already_actioned'."""
    row = get_draft(draft_id, db_path=db_path)
    if row is None:
        raise DraftActionError(f"no decision_drafts row for id={draft_id}")
    if row.status in _TERMINAL_STATUSES:
        return {"draft_id": draft_id, "decision_id": row.decision_id, "receipt": "already_actioned"}

    decision_id, receipt = _apply_confirmed_action(row, db_path=db_path)
    set_draft_status(
        draft_id, "confirmed", decision_id=decision_id, confirmed_at=now_iso(), db_path=db_path
    )
    return {"draft_id": draft_id, "decision_id": decision_id, "receipt": receipt}


def correct_draft(
    draft_id: int, corrected_fields: dict[str, object], *, db_path: Path | str | None = None
) -> dict[str, object]:
    """Validate owner-supplied corrections merged over the parsed draft, then
    apply the SAME resolution :func:`confirm_draft` uses. ``original_text``
    is never touched — only ``draft_json`` changes."""
    row = get_draft(draft_id, db_path=db_path)
    if row is None:
        raise DraftActionError(f"no decision_drafts row for id={draft_id}")
    if row.status in _TERMINAL_STATUSES:
        return {"draft_id": draft_id, "decision_id": row.decision_id, "receipt": "already_actioned"}
    base = row.draft.model_dump() if row.draft is not None else {"intent": "correction"}
    merged = {**base, **corrected_fields}
    try:
        corrected = DecisionDraft.model_validate(merged)
    except ValueError as exc:
        raise DraftActionError(f"invalid correction for draft {draft_id}: {exc}") from exc

    corrected_row = replace(row, draft=corrected)
    decision_id, receipt = _apply_confirmed_action(corrected_row, db_path=db_path)
    set_draft_status(
        draft_id,
        "corrected",
        decision_id=decision_id,
        confirmed_at=now_iso(),
        draft=corrected,
        db_path=db_path,
    )
    return {"draft_id": draft_id, "decision_id": decision_id, "receipt": receipt}


def dismiss_draft(draft_id: int, *, db_path: Path | str | None = None) -> dict[str, object]:
    """Dismiss without deleting the raw capture (PRD §11.6). Idempotent."""
    row = get_draft(draft_id, db_path=db_path)
    if row is None:
        raise DraftActionError(f"no decision_drafts row for id={draft_id}")
    if row.status in _TERMINAL_STATUSES:
        return {"draft_id": draft_id, "receipt": "already_actioned"}
    set_draft_status(draft_id, "dismissed", dismissed_at=now_iso(), db_path=db_path)
    return {"draft_id": draft_id, "receipt": "dismissed"}


def expire_stale_drafts(
    *, older_than_hours: float = 72.0, db_path: Path | str | None = None
) -> int:
    """Sweep ``awaiting_confirmation`` rows past their ``expires_at`` (or,
    absent one, older than ``older_than_hours``) to ``expired``. Best-effort
    maintenance — never raises; returns the count flipped."""
    conn = open_conn(db_path)
    try:
        now = now_iso()
        cutoff = (
            datetime.now(UTC).replace(tzinfo=None) - timedelta(hours=older_than_hours)
        ).isoformat()
        cur = conn.execute(
            "UPDATE decision_drafts SET status = 'expired', updated_at = ? "
            "WHERE status = 'awaiting_confirmation' "
            "AND ((expires_at IS NOT NULL AND expires_at <= ?) "
            "     OR (expires_at IS NULL AND created_at <= ?))",
            (now, now, cutoff),
        )
        conn.commit()
        return cur.rowcount or 0
    except sqlite3.Error:
        return 0
    finally:
        conn.close()


__all__ = [
    "DraftActionError",
    "confirm_draft",
    "correct_draft",
    "dismiss_draft",
    "expire_stale_drafts",
]
