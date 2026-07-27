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
    existing outcome, except that a confirmed/corrected tracker row processes
    newly arrived pending siblings through its shared group core;
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
    get_tracker_draft_group,
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
    cutoff_at = datetime.now(UTC).replace(tzinfo=None)
    cutoff = cutoff_at.isoformat()
    lower_bound = (cutoff_at - timedelta(days=_ADVICE_LOOKBACK_DAYS)).isoformat()
    if ticker:
        row = conn.execute(
            "SELECT id FROM decisions WHERE UPPER(ticker) = ? "
            "AND made_at BETWEEN ? AND ? ORDER BY made_at DESC LIMIT 1",
            (ticker.upper(), lower_bound, cutoff),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT id FROM decisions WHERE scope = 'portfolio' "
            "AND made_at BETWEEN ? AND ? ORDER BY made_at DESC LIMIT 1",
            (lower_bound, cutoff),
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
    draft_row: DecisionDraftRow,
    *,
    db_path: Path | str | None,
    connection: sqlite3.Connection | None = None,
) -> tuple[int, str]:
    """Resolve the confirmed draft to (decision_id, receipt). Raises
    :class:`DraftActionError` when there is nothing decision-shaped to attach
    to (the caller's real option is then Dismiss)."""
    draft = draft_row.draft
    if draft is None:
        raise DraftActionError(f"draft {draft_row.id} has no parsed fields to confirm")

    owns_connection = connection is None
    conn = open_conn(db_path) if connection is None else connection
    try:
        action = draft.proposed_action
        ticker = draft.proposed_ticker

        if action in DISPOSITION_ACTIONS:
            card = _card_artifact_for(conn, draft.linked_advice_artifact_id)
            if card is not None:
                from research.investment_decision_card import act_on_card

                receipt = act_on_card(
                    card[0],
                    action,
                    db_path=db_path,
                    notes=draft.proposed_rationale,
                    _connection=conn,
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
            if owns_connection:
                conn.commit()
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
            if owns_connection:
                conn.commit()
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
            if owns_connection:
                conn.commit()
        _maybe_note_profile_implication(draft, db_path=db_path)
        return decision_id, f"attached_to_decision:{decision_id}"
    finally:
        if owns_connection:
            conn.close()


def confirm_draft(draft_id: int, *, db_path: Path | str | None = None) -> dict[str, object]:
    """Accept parsed fields as-is, idempotently.

    A confirmed/corrected tracker representative still processes newly pending
    siblings in its group; other terminal drafts return ``already_actioned``.
    """
    conn = open_conn(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = get_draft(draft_id, db_path=db_path, connection=conn)
        if row is None:
            raise DraftActionError(f"no decision_drafts row for id={draft_id}")
        if (
            row.source_channel == "tracker"
            and row.source_external_id
            and row.status not in {"dismissed", "expired"}
        ):
            result = _confirm_tracker_fill_group_locked(conn, draft_id, db_path=db_path)
            conn.commit()
            return result
        if row.status in _TERMINAL_STATUSES:
            conn.rollback()
            return {
                "draft_id": draft_id,
                "decision_id": row.decision_id,
                "receipt": "already_actioned",
            }
        decision_id, receipt = _apply_confirmed_action(row, db_path=db_path, connection=conn)
        now = now_iso()
        updated = conn.execute(
            "UPDATE decision_drafts SET status = 'confirmed', decision_id = ?, "
            "confirmed_at = ?, updated_at = ? WHERE id = ? "
            "AND status NOT IN ('confirmed', 'corrected', 'dismissed', 'expired')",
            (decision_id, now, now, draft_id),
        )
        if updated.rowcount != 1:
            raise DraftActionError(
                f"draft {draft_id} changed while confirmation held the write lock"
            )
        conn.commit()
        return {"draft_id": draft_id, "decision_id": decision_id, "receipt": receipt}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _tracker_fill_group(
    conn: sqlite3.Connection,
    draft_id: int,
) -> tuple[DecisionDraftRow, list[DecisionDraftRow]]:
    """Return the representative and every row for its tracker trade group.

    ``source_external_id`` is intentionally the human-scale trade identity
    (ticker + day + direction), while ``idempotency_key`` retains each split
    fill's amount/quantity identity. Group actions therefore create one Owner
    Decision without deleting or coalescing the underlying fill evidence.
    """
    group = get_tracker_draft_group(draft_id, connection=conn)
    if group is None:
        raise DraftActionError(f"no decision_drafts row for id={draft_id}")
    representative, rows = group
    if representative.source_channel != "tracker" or not representative.source_external_id:
        raise DraftActionError(f"draft {draft_id} is not a tracker fill group")
    return representative, rows


def _confirm_tracker_fill_group_locked(
    conn: sqlite3.Connection,
    draft_id: int,
    *,
    db_path: Path | str | None,
) -> dict[str, object]:
    """Confirm all split fills in one tracker trade group as one decision.

    The decision amount is the sum of the split-fill amounts. Every source row
    remains intact and is linked to the same decision id, so review is concise
    without sacrificing fill-level auditability. Re-running is idempotent.
    """
    representative, rows = _tracker_fill_group(conn, draft_id)
    existing_ids = {
        row.decision_id
        for row in rows
        if row.status in {"confirmed", "corrected"} and row.decision_id is not None
    }
    if len(existing_ids) > 1:
        raise DraftActionError(
            f"tracker fill group {representative.source_external_id} links multiple decisions"
        )
    pending = [row for row in rows if row.status == "awaiting_confirmation"]
    if existing_ids:
        decision_id = next(iter(existing_ids))
        if not pending:
            return {
                "draft_id": draft_id,
                "decision_id": decision_id,
                "fill_count": len(rows),
                "receipt": "already_actioned",
            }
        pending_usd = [
            row.draft.proposed_amount_usd
            for row in pending
            if row.draft is not None and row.draft.proposed_amount_usd is not None
        ]
        pending_pct = [
            row.draft.proposed_amount_pct
            for row in pending
            if row.draft is not None and row.draft.proposed_amount_pct is not None
        ]
        decision = conn.execute(
            "SELECT ticker, recommendation_kind, decided_by, size_usd, size_pct "
            "FROM decisions WHERE id = ?",
            (decision_id,),
        ).fetchone()
        if decision is None or str(decision[2]) != "owner":
            raise DraftActionError(
                f"tracker fill group {representative.source_external_id} "
                "does not match its confirmed owner decision"
            )
        for row in pending:
            if row.draft is None or row.draft.proposed_action not in {"buy", "sell"}:
                raise DraftActionError(f"draft {row.id} is not a tracker buy/sell fill")
        current_size_usd = float(decision[3]) if decision[3] is not None else None
        current_size_pct = float(decision[4]) if decision[4] is not None else None
        size_usd = (
            (current_size_usd or 0.0) + sum(pending_usd)
            if current_size_usd is not None or pending_usd
            else None
        )
        size_pct = (
            (current_size_pct or 0.0) + sum(pending_pct)
            if current_size_pct is not None or pending_pct
            else None
        )
        now = now_iso()
        audit_note = (
            "\n\n---\n"
            f"Tracker aggregate corrected at {now}: attached {len(pending)} "
            f"late fill(s); group now contains "
            f"{sum(row.status in {'confirmed', 'corrected'} for row in rows) + len(pending)} "
            "active fill(s)."
        )
        conn.execute(
            "UPDATE decisions SET size_usd = ?, size_pct = ?, "
            "recommendation_value = ?, "
            "user_notes = COALESCE(user_notes, '') || ? WHERE id = ?",
            (size_usd, size_pct, size_pct, audit_note, decision_id),
        )
        pending_updates: list[tuple[int, str, str, str, int]] = []
        for row in pending:
            if row.draft is None:
                raise DraftActionError(f"draft {row.id} has no parsed tracker fill fields")
            inherited = row.draft.model_copy(
                update={
                    "proposed_ticker": str(decision[0]),
                    "proposed_action": str(decision[1]),
                }
            )
            pending_updates.append((decision_id, now, now, inherited.model_dump_json(), row.id))
        updated = conn.executemany(
            "UPDATE decision_drafts SET status = 'confirmed', decision_id = ?, "
            "confirmed_at = ?, updated_at = ?, draft_json = ? WHERE id = ? "
            "AND status = 'awaiting_confirmation'",
            pending_updates,
        )
        if updated.rowcount != len(pending):
            raise DraftActionError(
                f"tracker fill group {representative.source_external_id} "
                "changed while confirmation held the write lock"
            )
        return {
            "draft_id": draft_id,
            "decision_id": decision_id,
            "fill_count": sum(
                row.status in {"confirmed", "corrected", "awaiting_confirmation"} for row in rows
            ),
            "added_fill_count": len(pending),
            "receipt": "decision_aggregate_corrected",
        }
    if not pending:
        return {
            "draft_id": draft_id,
            "decision_id": representative.decision_id,
            "fill_count": len(rows),
            "receipt": "already_actioned",
        }
    if representative.draft is None:
        raise DraftActionError(f"draft {draft_id} has no parsed fields to confirm")

    ticker = representative.draft.proposed_ticker
    action = representative.draft.proposed_action
    if action not in {"buy", "sell"}:
        raise DraftActionError(f"draft {draft_id} is not a tracker buy/sell fill")
    for row in pending:
        if (
            row.draft is None
            or row.draft.proposed_ticker != ticker
            or row.draft.proposed_action != action
        ):
            raise DraftActionError(
                f"tracker fill group {representative.source_external_id} has inconsistent fields"
            )

    usd_values = [
        row.draft.proposed_amount_usd
        for row in pending
        if row.draft is not None and row.draft.proposed_amount_usd is not None
    ]
    pct_values = [
        row.draft.proposed_amount_pct
        for row in pending
        if row.draft is not None and row.draft.proposed_amount_pct is not None
    ]
    aggregated = representative.draft.model_copy(
        update={
            "proposed_amount_usd": sum(usd_values) if usd_values else None,
            "proposed_amount_pct": sum(pct_values) if pct_values else None,
        }
    )
    now = now_iso()
    decision_id = _write_owner_decision(
        conn,
        ticker=aggregated.proposed_ticker,
        recommendation_kind=action,
        recommendation_value=aggregated.proposed_amount_pct,
        size_usd=aggregated.proposed_amount_usd,
        size_pct=aggregated.proposed_amount_pct,
        rationale_excerpt=aggregated.proposed_rationale,
        advice_artifact_id=aggregated.linked_advice_artifact_id,
    )
    updated = conn.executemany(
        "UPDATE decision_drafts SET status = 'confirmed', decision_id = ?, "
        "confirmed_at = ?, updated_at = ? WHERE id = ? "
        "AND status = 'awaiting_confirmation'",
        [(decision_id, now, now, row.id) for row in pending],
    )
    if updated.rowcount != len(pending):
        raise DraftActionError(
            f"tracker fill group {representative.source_external_id} "
            "changed while confirmation held the write lock"
        )
    return {
        "draft_id": draft_id,
        "decision_id": decision_id,
        "fill_count": len(pending),
        "receipt": "decision_recorded",
    }


def confirm_tracker_fill_group(
    draft_id: int, *, db_path: Path | str | None = None
) -> dict[str, object]:
    """Serialize group confirmation before reading state, then atomically
    create/correct the Owner Decision and link every underlying fill."""
    conn = open_conn(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        result = _confirm_tracker_fill_group_locked(conn, draft_id, db_path=db_path)
        conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _correct_tracker_fill_group_locked(
    conn: sqlite3.Connection,
    draft_id: int,
    corrected_fields: dict[str, object],
    *,
    db_path: Path | str | None,
) -> dict[str, object]:
    """Apply an owner correction to one tracker group without rewriting fills.

    The decision stores the corrected group-level ticker/action/total. The
    underlying rows retain their raw per-fill amounts as audit evidence while
    receiving the corrected categorical fields and one shared decision link.
    """
    representative, rows = _tracker_fill_group(conn, draft_id)
    existing_ids = {
        row.decision_id
        for row in rows
        if row.status in {"confirmed", "corrected"} and row.decision_id is not None
    }
    if len(existing_ids) > 1:
        raise DraftActionError(
            f"tracker fill group {representative.source_external_id} links multiple decisions"
        )
    pending = [row for row in rows if row.status == "awaiting_confirmation"]
    if not pending:
        return {
            "draft_id": draft_id,
            "decision_id": next(iter(existing_ids), representative.decision_id),
            "fill_count": len(rows),
            "receipt": "already_actioned",
        }

    existing_decision = None
    if existing_ids:
        existing_decision = conn.execute(
            "SELECT id, ticker, recommendation_kind, size_usd, size_pct "
            "FROM decisions WHERE id = ? AND decided_by = 'owner'",
            (next(iter(existing_ids)),),
        ).fetchone()
        if existing_decision is None:
            raise DraftActionError(
                f"tracker fill group {representative.source_external_id} "
                "does not match its confirmed owner decision"
            )
    if representative.draft is None:
        raise DraftActionError(f"draft {draft_id} has no parsed fields to correct")

    pending_usd = [
        row.draft.proposed_amount_usd
        for row in pending
        if row.draft is not None and row.draft.proposed_amount_usd is not None
    ]
    pending_pct = [
        row.draft.proposed_amount_pct
        for row in pending
        if row.draft is not None and row.draft.proposed_amount_pct is not None
    ]
    active = [row for row in rows if row.status in {"confirmed", "corrected"}]
    active_usd = [
        row.draft.proposed_amount_usd
        for row in active
        if row.draft is not None and row.draft.proposed_amount_usd is not None
    ]
    active_pct = [
        row.draft.proposed_amount_pct
        for row in active
        if row.draft is not None and row.draft.proposed_amount_pct is not None
    ]
    decision_usd = (
        float(existing_decision[3])
        if existing_decision and existing_decision[3] is not None
        else None
    )
    decision_pct = (
        float(existing_decision[4])
        if existing_decision and existing_decision[4] is not None
        else None
    )
    base_usd = (
        (decision_usd if decision_usd is not None else sum(active_usd)) + sum(pending_usd)
        if decision_usd is not None or active_usd or pending_usd
        else None
    )
    base_pct = (
        (decision_pct if decision_pct is not None else sum(active_pct)) + sum(pending_pct)
        if decision_pct is not None or active_pct or pending_pct
        else None
    )
    base = representative.draft.model_dump()
    base.update(
        {
            "proposed_ticker": (
                str(existing_decision[1])
                if existing_decision
                else representative.draft.proposed_ticker
            ),
            "proposed_action": (
                str(existing_decision[2])
                if existing_decision
                else representative.draft.proposed_action
            ),
            "proposed_amount_usd": base_usd,
            "proposed_amount_pct": base_pct,
        }
    )
    try:
        corrected = DecisionDraft.model_validate({**base, **corrected_fields})
    except ValueError as exc:
        raise DraftActionError(f"invalid correction for tracker group {draft_id}: {exc}") from exc
    if corrected.proposed_action not in {"buy", "sell"} or not corrected.proposed_ticker:
        raise DraftActionError(f"tracker group {draft_id} correction requires ticker and buy/sell")

    now = now_iso()
    if existing_decision is None:
        decision_id = _write_owner_decision(
            conn,
            ticker=corrected.proposed_ticker,
            recommendation_kind=corrected.proposed_action,
            recommendation_value=corrected.proposed_amount_pct,
            size_usd=corrected.proposed_amount_usd,
            size_pct=corrected.proposed_amount_pct,
            rationale_excerpt=corrected.proposed_rationale,
            advice_artifact_id=corrected.linked_advice_artifact_id,
        )
    else:
        decision_id = int(existing_decision[0])
        audit_note = (
            "\n\n---\n"
            f"Tracker group corrected from Inbox at {now}: "
            f"{corrected.proposed_ticker} {corrected.proposed_action}; "
            f"size_usd={corrected.proposed_amount_usd!r}; "
            f"size_pct={corrected.proposed_amount_pct!r}."
        )
        conn.execute(
            "UPDATE decisions SET ticker = ?, scope = 'ticker', recommendation_kind = ?, "
            "recommendation_value = ?, size_usd = ?, size_pct = ?, rationale_excerpt = ?, "
            "user_notes = COALESCE(user_notes, '') || ? WHERE id = ?",
            (
                corrected.proposed_ticker,
                corrected.proposed_action,
                corrected.proposed_amount_pct,
                corrected.proposed_amount_usd,
                corrected.proposed_amount_pct,
                corrected.proposed_rationale,
                audit_note,
                decision_id,
            ),
        )

    evidence_updates: list[tuple[str, str, int]] = []
    for row in rows:
        row_draft = row.draft or corrected
        evidence_draft = row_draft.model_copy(
            update={
                "proposed_ticker": corrected.proposed_ticker,
                "proposed_action": corrected.proposed_action,
                "proposed_rationale": corrected.proposed_rationale,
            }
        )
        evidence_updates.append((evidence_draft.model_dump_json(), now, row.id))
    evidence_updated = conn.executemany(
        "UPDATE decision_drafts SET draft_json = ?, updated_at = ? WHERE id = ?",
        evidence_updates,
    )
    if evidence_updated.rowcount != len(rows):
        raise DraftActionError(
            f"tracker fill group {representative.source_external_id} "
            "changed while correction updated its evidence"
        )
    updates = [(decision_id, now, now, row.id) for row in pending]
    updated = conn.executemany(
        "UPDATE decision_drafts SET status = 'corrected', decision_id = ?, "
        "confirmed_at = ?, updated_at = ? WHERE id = ? "
        "AND status = 'awaiting_confirmation'",
        updates,
    )
    if updated.rowcount != len(pending):
        raise DraftActionError(
            f"tracker fill group {representative.source_external_id} "
            "changed while correction held the write lock"
        )
    return {
        "draft_id": draft_id,
        "decision_id": decision_id,
        "fill_count": len(pending),
        "receipt": "tracker_group_corrected",
    }


def correct_tracker_fill_group(
    draft_id: int,
    corrected_fields: dict[str, object],
    *,
    db_path: Path | str | None = None,
) -> dict[str, object]:
    """Serialize and atomically correct one tracker fill group."""
    conn = open_conn(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        result = _correct_tracker_fill_group_locked(
            conn, draft_id, corrected_fields, db_path=db_path
        )
        conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def correct_draft(
    draft_id: int, corrected_fields: dict[str, object], *, db_path: Path | str | None = None
) -> dict[str, object]:
    """Validate owner-supplied corrections merged over the parsed draft, then
    apply the SAME resolution :func:`confirm_draft` uses. ``original_text``
    is never touched — only ``draft_json`` changes."""
    conn = open_conn(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = get_draft(draft_id, db_path=db_path, connection=conn)
        if row is None:
            raise DraftActionError(f"no decision_drafts row for id={draft_id}")
        if (
            row.source_channel == "tracker"
            and row.source_external_id
            and row.status not in {"dismissed", "expired"}
        ):
            result = _correct_tracker_fill_group_locked(
                conn, draft_id, corrected_fields, db_path=db_path
            )
            conn.commit()
            return result
        if row.status in _TERMINAL_STATUSES:
            conn.rollback()
            return {
                "draft_id": draft_id,
                "decision_id": row.decision_id,
                "receipt": "already_actioned",
            }
        base = row.draft.model_dump() if row.draft is not None else {"intent": "correction"}
        merged = {**base, **corrected_fields}
        try:
            corrected = DecisionDraft.model_validate(merged)
        except ValueError as exc:
            raise DraftActionError(f"invalid correction for draft {draft_id}: {exc}") from exc

        corrected_row = replace(row, draft=corrected)
        decision_id, receipt = _apply_confirmed_action(
            corrected_row, db_path=db_path, connection=conn
        )
        now = now_iso()
        updated = conn.execute(
            "UPDATE decision_drafts SET status = 'corrected', decision_id = ?, "
            "confirmed_at = ?, updated_at = ?, draft_json = ? WHERE id = ? "
            "AND status NOT IN ('confirmed', 'corrected', 'dismissed', 'expired')",
            (decision_id, now, now, corrected.model_dump_json(), draft_id),
        )
        if updated.rowcount != 1:
            raise DraftActionError(f"draft {draft_id} changed while correction held the write lock")
        conn.commit()
        return {"draft_id": draft_id, "decision_id": decision_id, "receipt": receipt}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def dismiss_draft(draft_id: int, *, db_path: Path | str | None = None) -> dict[str, object]:
    """Dismiss without deleting the raw capture (PRD §11.6). Idempotent."""
    conn = open_conn(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = get_draft(draft_id, db_path=db_path, connection=conn)
        if row is None:
            raise DraftActionError(f"no decision_drafts row for id={draft_id}")
        if row.status in _TERMINAL_STATUSES:
            conn.rollback()
            return {"draft_id": draft_id, "receipt": "already_actioned"}
        now = now_iso()
        updated = conn.execute(
            "UPDATE decision_drafts SET status = 'dismissed', dismissed_at = ?, "
            "updated_at = ? WHERE id = ? "
            "AND status NOT IN ('confirmed', 'corrected', 'dismissed', 'expired')",
            (now, now, draft_id),
        )
        if updated.rowcount != 1:
            raise DraftActionError(f"draft {draft_id} changed while dismissal held the write lock")
        conn.commit()
        return {"draft_id": draft_id, "receipt": "dismissed"}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def dismiss_tracker_fill_group(
    draft_id: int, *, db_path: Path | str | None = None
) -> dict[str, object]:
    """Dismiss every pending split fill in one group without deleting evidence."""
    conn = open_conn(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        representative, rows = _tracker_fill_group(conn, draft_id)
        pending = [row for row in rows if row.status == "awaiting_confirmation"]
        if not pending:
            conn.rollback()
            return {
                "draft_id": draft_id,
                "fill_count": len(rows),
                "receipt": "already_actioned",
            }
        now = now_iso()
        updated = conn.executemany(
            "UPDATE decision_drafts SET status = 'dismissed', dismissed_at = ?, "
            "updated_at = ? WHERE id = ? AND status = 'awaiting_confirmation'",
            [(now, now, row.id) for row in pending],
        )
        if updated.rowcount != len(pending):
            raise DraftActionError(
                f"tracker fill group {representative.source_external_id} "
                "changed while dismissal held the write lock"
            )
        conn.commit()
        return {
            "draft_id": representative.id,
            "fill_count": len(pending),
            "receipt": "dismissed",
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


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
    "confirm_tracker_fill_group",
    "correct_draft",
    "correct_tracker_fill_group",
    "dismiss_draft",
    "dismiss_tracker_fill_group",
    "expire_stale_drafts",
]
