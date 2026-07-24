"""Point-of-intent decision nudge (PR2, Deliverable 2 — navigation_ia.md §3.2).

The verified failure mode: decisions #96 (AGX) and #97 (BKNG) sat with NULL
conviction/falsifier for days while a dashboard doorway pointed at them — the
owner never walked through it. This attacks the SAME gap at the moment of
intent instead: a same-day Telegram follow-up, one message per stub decision,
buttons only (no LLM leg at all — the scan and the message text are both
deterministic).

Flow:
  1. :func:`find_stub_decisions` — owner decisions made in the last N days
     still missing conviction or falsifier, that have never been nudged
     (``decision_nudges`` has ``UNIQUE(decision_id)`` — nudged AT MOST once
     ever, enforced at the schema level, not just here).
  2. :func:`send_nudges` — one message per stub, buttons [Fill in now / Skip].
  3. "Fill in now" (``dn:fill:<id>``, dispatched in
     ``capture.research_notify``) stashes an awaited reply in
     ``capture.pending_replies`` (kind ``decision_fill_in``) — the poller's
     text handler intercepts the OWNER's next free-text message (within the
     24h default expiry) and routes it to :func:`handle_fill_in_reply`
     instead of the default musing-capture route.
  4. The reply's lines are mapped onto the fields that are actually MISSING
     (conviction→falsifier order), written WRITE-ONCE (a set field is never
     overwritten), mirroring ``research.pledge.annotate_latest_pending``'s own
     write-once contract. A stub needing only the falsifier takes a single-line
     reply as that falsifier — never a misfile into the already-set conviction.
     An expired/consumed-elsewhere await simply falls through to normal
     capture — the owner's words are never dropped.
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from sqlite3 import IntegrityError
from typing import cast

from capture import pending_replies, telegram
from user_state._db import now_iso, now_naive_utc, open_conn

log = logging.getLogger(__name__)

SendFn = Callable[..., object]

DEFAULT_LOOKBACK_DAYS = 7
FILL_IN_KIND = "decision_fill_in"
NUDGE_STATUSES: tuple[str, ...] = ("sent", "awaiting_reply", "filled", "skipped", "expired")


@dataclass(frozen=True, slots=True)
class DecisionStub:
    id: int
    ticker: str
    recommendation_kind: str
    made_at: str


def _nudge_keyboard(decision_id: int) -> dict[str, object]:
    return telegram.inline_keyboard(
        [[("Fill in now", f"dn:fill:{decision_id}"), ("Skip", f"dn:skip:{decision_id}")]]
    )


def find_stub_decisions(
    *, lookback_days: int = DEFAULT_LOOKBACK_DAYS, db_path: Path | str | None = None
) -> list[DecisionStub]:
    """Owner decisions still missing conviction/falsifier, made within
    ``lookback_days``, never nudged. Mirrors ``open_loops._decision_stub_debt``'s
    WHERE clause (that band shows the standing backlog; this scan is scoped to
    recent + un-nudged so the nudge fires once, at the moment of intent)."""
    cutoff = (now_naive_utc() - timedelta(days=lookback_days)).isoformat()
    conn = open_conn(db_path)
    try:
        rows = conn.execute(
            """
            SELECT d.id, d.ticker, d.recommendation_kind, d.made_at FROM decisions d
            WHERE d.decided_by = 'owner'
              AND (d.conviction IS NULL OR d.falsifier IS NULL)
              AND d.made_at >= ?
              AND NOT EXISTS (SELECT 1 FROM decision_nudges n WHERE n.decision_id = d.id)
            ORDER BY d.id
            """,
            (cutoff,),
        ).fetchall()
        return [
            DecisionStub(
                id=int(r[0]),
                ticker=str(r[1] or ""),
                recommendation_kind=str(r[2] or ""),
                made_at=str(r[3] or ""),
            )
            for r in rows
        ]
    except Exception:  # a missing/pre-migration table must never crash the run
        return []
    finally:
        conn.close()


def _record_nudge(decision_id: int, *, chat_id: int | None, db_path: Path | str | None) -> bool:
    """Insert the (nudge-once) ledger row. Returns False if the decision was
    already nudged (the UNIQUE constraint fired — a race, not an error)."""
    conn = open_conn(db_path)
    try:
        conn.execute(
            "INSERT INTO decision_nudges (decision_id, chat_id, status, sent_at) "
            "VALUES (?, ?, 'sent', ?)",
            (decision_id, chat_id, now_iso()),
        )
        conn.commit()
        return True
    except IntegrityError:
        return False
    finally:
        conn.close()


def _set_nudge_status(decision_id: int, status: str, *, db_path: Path | str | None) -> None:
    if status not in NUDGE_STATUSES:
        raise ValueError(f"unknown nudge status {status!r}; expected one of {NUDGE_STATUSES}")
    conn = open_conn(db_path)
    try:
        extra = ", filled_at = ?" if status == "filled" else ""
        params: tuple[object, ...] = (status, now_iso()) if status == "filled" else (status,)
        conn.execute(
            f"UPDATE decision_nudges SET status = ?{extra} WHERE decision_id = ?",
            (*params, decision_id),
        )
        conn.commit()
    finally:
        conn.close()


def mark_skipped(decision_id: int, *, db_path: Path | str | None = None) -> None:
    """The 'Skip' button — the owner saw it and chose not to fill it in now
    (never re-nudged; the standing open-loops band still carries the debt)."""
    _set_nudge_status(decision_id, "skipped", db_path=db_path)


def _mark_awaiting(decision_id: int, *, expiry_hours: float, db_path: Path | str | None) -> None:
    until = (now_naive_utc() + timedelta(hours=expiry_hours)).isoformat()
    conn = open_conn(db_path)
    try:
        conn.execute(
            "UPDATE decision_nudges SET status = 'awaiting_reply', awaiting_until = ? "
            "WHERE decision_id = ?",
            (until, decision_id),
        )
        conn.commit()
    finally:
        conn.close()


def start_fill_in(
    decision_id: int,
    *,
    chat_id: int,
    expiry_hours: float = pending_replies.DEFAULT_EXPIRY_HOURS,
    db_path: Path | str | None = None,
) -> None:
    """The 'Fill in now' tap: stash the awaited reply + flip the ledger row.
    Best-effort against a decision that was never actually nudged (a stale
    button on an old card) — the stash still happens; the status update is a
    no-op UPDATE (0 rows) in that case."""
    pending_replies.stash(
        chat_id, FILL_IN_KIND, decision_id, expiry_hours=expiry_hours, db_path=db_path
    )
    _mark_awaiting(decision_id, expiry_hours=expiry_hours, db_path=db_path)


def apply_fill_in_reply(decision_id: int, text: str, *, db_path: Path | str | None = None) -> bool:
    """WRITE-ONCE fill the decision's NULL conviction/falsifier from a reply,
    mapping the reply's lines onto the fields that are ACTUALLY missing (in
    canonical order conviction→falsifier), not onto fixed positions. Returns
    True iff at least one field was written.

    Why by-missing, not by-position: a decision that already has its conviction
    (write-once) and needs only the falsifier takes a *single-line* reply as the
    falsifier — the owner types the one thing the ledger lacks, never a throwaway
    conviction line to hit slot 2. (Positional parsing misfiled that lone
    falsifier into the already-set conviction slot, wrote nothing, and dead-ended
    at 'couldn't parse that' — decision #95 RBRK, 2026-07-19.) When both fields
    are missing the reply is still conviction-then-falsifier: one line fills
    conviction, two lines fill both. An empty reply, or one whose lines all map
    to already-set fields, writes nothing (the fields stay NULL for the standing
    open-loops band to keep chasing)."""
    lines = [ln.strip() for ln in (text or "").strip().splitlines() if ln.strip()]
    if not lines:
        return False
    conn = open_conn(db_path)
    try:
        row = conn.execute(
            "SELECT conviction, falsifier FROM decisions WHERE id = ?", (decision_id,)
        ).fetchone()
        if row is None:
            return False
        pairs = (("conviction", row[0]), ("falsifier", row[1]))
        missing = [col for col, val in pairs if val is None]
        # strict=False on purpose: a reply may carry fewer lines than missing
        # fields (fill one, leave the other) or more (extras ignored).
        sets: dict[str, str] = {
            col: line for col, line in zip(missing, lines, strict=False) if line
        }
        if not sets:
            return False
        assignments = ", ".join(f"{col} = ?" for col in sets)
        conn.execute(
            f"UPDATE decisions SET {assignments} WHERE id = ?", (*sets.values(), decision_id)
        )
        conn.commit()
        return True
    finally:
        conn.close()


def handle_fill_in_reply(
    token: str,
    chat_id: int | None,
    text: str,
    pending: pending_replies.PendingReply,
    *,
    db_path: Path | str | None,
    send: SendFn = telegram.send_message,
) -> None:
    """Consume an awaited fill-in reply — apply it, mark the ledger, confirm
    in-thread. Always consumes the await (a malformed reply doesn't leave a
    dangling hijack of the NEXT message; the owner can tap the button again or
    the fields simply stay NULL for the open-loops band to keep surfacing)."""
    decision_id = pending.ref_id
    wrote = apply_fill_in_reply(decision_id, text, db_path=db_path)
    pending_replies.consume(pending.id, db_path=db_path)
    _set_nudge_status(decision_id, "filled" if wrote else "skipped", db_path=db_path)
    if chat_id is not None:
        msg = (
            f"Recorded on decision #{decision_id}."
            if wrote
            else "Didn't catch that — send the missing piece as text "
            "(conviction, and/or what would prove you wrong)."
        )
        with contextlib.suppress(telegram.TelegramError):
            send(token, chat_id, msg)


def _prefill_hint(stub: DecisionStub, *, db_path: Path | str | None) -> str | None:
    """Rationale/disconfirmer hint for the demoted nudge, in priority order:
    the linked advice artifact's content_json, else the decision's own
    rationale_excerpt. Best-effort None on any failure."""
    import json as _json

    conn = open_conn(db_path)
    try:
        row = conn.execute(
            "SELECT advice_artifact_id, rationale_excerpt FROM decisions WHERE id = ?",
            (stub.id,),
        ).fetchone()
        if row is None:
            return None
        artifact_id, rationale_excerpt = row[0], row[1]
        if artifact_id is not None:
            art = conn.execute(
                "SELECT content_json FROM llm_artifacts WHERE id = ?", (artifact_id,)
            ).fetchone()
            if art is not None and art[0]:
                try:
                    content_raw: object = _json.loads(art[0])
                except (ValueError, TypeError):
                    content_raw = None
                if isinstance(content_raw, dict):
                    content = cast("dict[str, object]", content_raw)
                    for key in (
                        "disconfirming_case",
                        "bear_case",
                        "risks",
                        "directional_thesis",
                        "thesis",
                    ):
                        val = content.get(key)
                        if isinstance(val, str) and val.strip():
                            return val.strip()[:280]
                        if isinstance(val, list) and val and isinstance(val[0], str):
                            return str(val[0]).strip()[:280]
        if isinstance(rationale_excerpt, str) and rationale_excerpt.strip():
            return rationale_excerpt.strip()[:280]
        return None
    except Exception:
        return None
    finally:
        conn.close()


def send_nudges(
    token: str,
    chat_id: int,
    *,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    db_path: Path | str | None = None,
    send: SendFn = telegram.send_message,
) -> int:
    """Send one nudge per un-nudged stub decision. Returns the count sent
    (best-effort per item — a single send failure never blocks the rest).

    Demoted (PRD §9.2): no longer a rigid two-line form. When a hint is
    resolvable, the message PREFILLS it and asks the owner to correct it
    only if material; absent a hint, the ask is a single soft optional line.
    Existing falsifiers stay valuable and are still requested when genuinely
    missing, but never as a compulsory field to preserve the decision."""
    stubs = find_stub_decisions(lookback_days=lookback_days, db_path=db_path)
    sent = 0
    for stub in stubs:
        head = (
            f"You logged {stub.ticker} {stub.recommendation_kind.upper()} on {stub.made_at[:10]}."
        )
        hint = _prefill_hint(stub, db_path=db_path)
        if hint:
            text = (
                f'{head} On file: "{hint}" — reply to confirm this as your conviction/what '
                "would prove you wrong, or correct it in one line. Skip is fine too."
            )
        else:
            text = (
                f"{head} One optional line if you have a quick take on conviction or what "
                "would change your mind — no pressure, Skip is fine."
            )
        try:
            send(token, chat_id, text, reply_markup=_nudge_keyboard(stub.id))
        except telegram.TelegramError:
            log.warning({"event": "decision_nudge_send_failed", "decision_id": stub.id})
            continue
        if not _record_nudge(stub.id, chat_id=chat_id, db_path=db_path):
            log.info({"event": "decision_nudge_already_recorded", "decision_id": stub.id})
        sent += 1
    return sent


__all__ = [
    "DEFAULT_LOOKBACK_DAYS",
    "FILL_IN_KIND",
    "DecisionStub",
    "apply_fill_in_reply",
    "find_stub_decisions",
    "handle_fill_in_reply",
    "mark_skipped",
    "send_nudges",
    "start_fill_in",
]
