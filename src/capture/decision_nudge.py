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
  4. The reply is parsed as two lines — conviction, then falsifier — and
     written WRITE-ONCE (a set field is never overwritten), mirroring
     ``research.pledge.annotate_latest_pending``'s own write-once contract.
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
    """Parse a two-line reply (conviction, then falsifier) and WRITE-ONCE fill
    the decision's NULL fields. Returns True iff at least one field was
    written. A single-line reply fills conviction only; an empty/unparseable
    reply writes nothing (the fields stay NULL for the standing open-loops
    band to keep chasing)."""
    lines = [ln.strip() for ln in (text or "").strip().splitlines() if ln.strip()]
    if not lines:
        return False
    conviction = lines[0] if lines else None
    falsifier = lines[1] if len(lines) >= 2 else None
    conn = open_conn(db_path)
    try:
        row = conn.execute(
            "SELECT conviction, falsifier FROM decisions WHERE id = ?", (decision_id,)
        ).fetchone()
        if row is None:
            return False
        sets: dict[str, str] = {}
        if row[0] is None and conviction:
            sets["conviction"] = conviction
        if row[1] is None and falsifier:
            sets["falsifier"] = falsifier
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
            else "Couldn't parse that — reply with conviction on one line, falsifier on the next."
        )
        with contextlib.suppress(telegram.TelegramError):
            send(token, chat_id, msg)


def send_nudges(
    token: str,
    chat_id: int,
    *,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    db_path: Path | str | None = None,
    send: SendFn = telegram.send_message,
) -> int:
    """Send one nudge per un-nudged stub decision. Returns the count sent
    (best-effort per item — a single send failure never blocks the rest)."""
    stubs = find_stub_decisions(lookback_days=lookback_days, db_path=db_path)
    sent = 0
    for stub in stubs:
        text = (
            f"You logged {stub.ticker} {stub.recommendation_kind.upper()} on "
            f"{stub.made_at[:10]} — one line each: conviction? what would prove you wrong?"
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
