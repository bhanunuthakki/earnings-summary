"""coach_reply — route a free-text REPLY to a governed coach ping (B3).

``execution/run_coach_pings.py`` pushes deterministic coach findings (a
falsifier breach, an overdue annotation stub, a conviction cohort grading
below its bar, ...) but was SEND-ONLY: it discarded the ``message_id``
Telegram's ``sendMessage`` returns, so when the owner replied in free text
(rather than tapping Dismiss/Answer) there was nothing to route the reply
back to — the reply fell through to ordinary capture with the coach-finding
linkage lost. That reply is the single most valuable signal a coach ping can
get back (it's the owner reasoning about a finding the coach surfaced
unprompted), and it was landing on the floor.

**Safety invariant**: the owner's raw text ALWAYS lands verbatim as a musing
linked to the ping via ``context_json['coach_ping_id']`` — interpretation
(the classified outcome below) can be wrong; capture never is. Every step
after the musing lands is best-effort and degrades to a plain filed note on
any failure; nothing here can un-land a captured reply.

Owner ruling (2026-07-19): a reply to a coach ping is a RESPONSE, not a new
initiation — it is exempt from the governor's DAILY_CAP/WEEKLY_CAP (those
caps bound the coach speaking first, not the owner answering back).

Outcome enum (``_OUTCOMES`` + the window-only ``unrelated``):
  acknowledge       — owner has seen/accepted the finding, nothing more to do
  dismiss           — routes through ``governor.record_dismissal`` (preserves
                       the 3-consecutive-dismissals auto-mute training signal)
  annotate_decision — the reply supplies missing detail for the DECISION this
                       ping references (only when ``source_ref`` names one)
  profile_fact      — see the module-level comment in ``_apply_outcome``: no
                       clean staging path exists yet, so this degrades to
                       ``note`` (documented, not silently dropped)
  note              — the default/fail-open outcome: filed, linked, done
  unrelated         — WINDOW MODE ONLY: the reply doesn't address the
                       finding at all; the message is handed back to
                       ordinary capture untouched

Two ways a reply resolves to a ping (``find_reply_ping``):
  direct — Telegram's own reply-to-message threading
           (``update.reply_to_message_id`` == the ping's stored
           ``telegram_message_id``, migration 0188)
  window — no reply-to-message set, but the latest ``sent`` ping is within
           the last ``_WINDOW_HOURS`` hours (the owner just typed a follow-up
           without using Telegram's reply gesture)

A window match is only a GUESS, so window mode classifies BEFORE landing:
``unrelated`` — or a classifier that died and can't vouch for the match —
returns the message to the poller's ordinary capture chain (confirm /
wondering tap / ledger answer all intact). Only a confident related verdict
consumes the update. Direct replies skip that pre-check: the reply gesture
itself is the owner saying "this is about that ping", so the text lands and
links first, and classification only picks the follow-up action.
"""

from __future__ import annotations

import contextlib
import logging
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from capture import ingest, telegram
from capture.matcher import RosterIndex
from research.governor import mark_ping_acted, record_dismissal
from user_state._db import now_naive_utc, open_conn

log = logging.getLogger(__name__)

PURPOSE = "coach_reply_intent"

# The closed outcome enum. 'unrelated' is accepted ONLY in window mode (see
# classify_reply) — a direct reply-to-message is unambiguously about the ping
# it replied to, so 'unrelated' would never make sense there.
_OUTCOMES: tuple[str, ...] = ("acknowledge", "dismiss", "annotate_decision", "profile_fact", "note")

_WINDOW_HOURS = 6
_SELECT_COLS = "id, class_, ticker, body, source_ref, status, created_at"


class _ReplyWire(BaseModel):
    model_config = ConfigDict(extra="forbid")

    intent: Literal[
        "acknowledge", "dismiss", "annotate_decision", "profile_fact", "note", "unrelated"
    ]
    reason: str = Field(default="", max_length=200)


_REPLY_ADAPTER = TypeAdapter(_ReplyWire)


@dataclass(frozen=True, slots=True)
class PingLike:
    """One ``coach_pings`` row, just enough to classify + apply a reply
    outcome against — mirrors ``governor.PingRow`` but adds ``body``/
    ``source_ref``/``created_at`` (the reply core needs the finding text for
    the classify prompt and the source ref for ``annotate_decision``)."""

    id: int
    class_: str
    ticker: str | None
    body: str
    source_ref: str | None
    status: str
    created_at: str


@dataclass(frozen=True, slots=True)
class ReplyVerdict:
    intent: str  # one of _OUTCOMES, or 'unrelated' in window mode
    reason: str = ""
    # True only when the classifier itself failed (transient LLM error) and the
    # intent is the fail-open default — window mode uses this to hand the
    # message back to ordinary capture instead of consuming it on zero evidence.
    degraded: bool = False


ClassifyCall = Callable[[str], "dict[str, object]"]


def _row_to_ping(row: sqlite3.Row) -> PingLike:
    return PingLike(
        id=int(row[0]),
        class_=str(row[1]),
        ticker=row[2],
        body=str(row[3]),
        source_ref=row[4],
        status=str(row[5]),
        created_at=str(row[6]),
    )


def find_reply_ping(
    update: telegram.Update, *, db_path: Path | str | None
) -> tuple[PingLike, str] | None:
    """Resolve one Telegram update to the coach ping it's replying to, or
    None. Two modes, tried in this order:

    - direct: ``update.reply_to_message_id`` matches a ping's stored
      ``telegram_message_id`` (migration 0188) — Telegram's own threading.
    - window: no reply-to-message set, AND the latest ``status='sent'`` ping
      landed within the last ``_WINDOW_HOURS`` hours — the owner typed a
      follow-up without using the reply gesture.

    Degrades to None on any DB error (e.g. a pre-0188 schema missing the
    ``telegram_message_id`` column) — the caller falls through to ordinary
    capture, never hijacking it."""
    try:
        conn = open_conn(db_path)
    except Exception:
        return None
    try:
        if update.reply_to_message_id is not None:
            row = conn.execute(
                f"SELECT {_SELECT_COLS} FROM coach_pings WHERE telegram_message_id = ?",
                (update.reply_to_message_id,),
            ).fetchone()
            return (_row_to_ping(row), "direct") if row is not None else None
        cutoff = (now_naive_utc() - timedelta(hours=_WINDOW_HOURS)).isoformat()
        row = conn.execute(
            f"SELECT {_SELECT_COLS} FROM coach_pings WHERE status = 'sent' AND created_at >= ? "
            "ORDER BY created_at DESC, id DESC LIMIT 1",
            (cutoff,),
        ).fetchone()
        return (_row_to_ping(row), "window") if row is not None else None
    except sqlite3.OperationalError:
        return None  # pre-0188 schema — no telegram_message_id column yet
    finally:
        conn.close()


def _render_tenets(db_path: Path | str | None) -> str:
    """Owner's current Worldview, compactly, for classify context. Degrades to
    empty on any failure (missing table, import error) — the prompt reads fine
    without it, just less informed."""
    try:
        from synthesis.tenets import list_tenets

        tenets = list_tenets(status="current", db_path=db_path)
    except Exception:
        return ""
    text = "\n".join(f"- {' '.join(t.body_md.split())}" for t in tenets)
    return text[:1200]


def _build_prompt(
    ping: PingLike, reply_text: str, tenets_text: str, *, allow_unrelated: bool
) -> str:
    outcomes = [*_OUTCOMES, "unrelated"] if allow_unrelated else list(_OUTCOMES)
    parts = [
        "The owner replied in free text to a coach-initiated ping — a deterministic "
        "finding the coach pushed unprompted (a broken falsifier, an overdue decision "
        "annotation, a conviction cohort grading below its bar, ...). Route the reply "
        "to exactly one outcome.\n\n",
        f"Ping class: {ping.class_}\n",
        f"Ping body: {ping.body[:600]}\n\n",
        f"Owner reply: {reply_text[:800]}\n\n",
    ]
    if tenets_text:
        parts.append(f"Owner's current worldview (Tenets), for context only:\n{tenets_text}\n\n")
    parts.append(
        "Outcomes:\n"
        "- acknowledge: they've seen/accepted the finding; nothing more to do.\n"
        "- dismiss: they want this finding dropped / cleared / marked irrelevant.\n"
        "- annotate_decision: the reply supplies missing detail (conviction, "
        "falsifier, rationale) for an owner DECISION this ping references.\n"
        "- profile_fact: the reply states a durable fact ABOUT THE OWNER "
        "(capacity, appetite, behavior) worth remembering beyond this one ping.\n"
        "- note: an additive comment worth keeping, with no clearer routing.\n"
    )
    if allow_unrelated:
        parts.append(
            "- unrelated: the reply doesn't address the coach finding at all — an "
            "unrelated new thought that happened to arrive soon after.\n"
        )
    parts.append(
        "\nWhen unsure, choose 'note' — filing safely is always correct; acting is not.\n\n"
        'Return JSON ONLY: {"intent": "' + "|".join(outcomes) + '", "reason": "<one line>"}'
    )
    return "".join(parts)


def _default_call(prompt: str) -> dict[str, object]:
    from llm.structured import call_llm_structured

    obj = call_llm_structured(
        prompt,
        purpose=PURPOSE,
        expect="object",
        required_keys=("intent",),
        schema=_REPLY_ADAPTER,
    )
    return _ReplyWire.model_validate(obj).model_dump()


def classify_reply(
    ping: PingLike,
    reply_text: str,
    *,
    mode: str,
    db_path: Path | str | None = None,
    call: ClassifyCall | None = None,
) -> ReplyVerdict:
    """Classify one coach-ping reply. Fail-open to ``note`` on ANY classifier
    exception or an intent outside the mode's valid set — 'unrelated' is only
    ever a CONFIDENT verdict (never a failure fallback), so a classifier that
    dies never un-links a musing that already landed."""
    allow_unrelated = mode == "window"
    valid = {*_OUTCOMES, "unrelated"} if allow_unrelated else set(_OUTCOMES)
    prompt = _build_prompt(
        ping, reply_text, _render_tenets(db_path), allow_unrelated=allow_unrelated
    )
    try:
        raw = (call or _default_call)(prompt)
    except Exception:
        log.warning(
            {"event": "coach_reply_classify_failed", "ping_id": ping.id, "mode": mode},
            exc_info=True,
        )
        return ReplyVerdict(intent="note", degraded=True)
    raw_intent = raw.get("intent")
    intent = raw_intent.strip().lower() if isinstance(raw_intent, str) else ""
    if intent not in valid:
        intent = "note"
    return ReplyVerdict(intent=intent, reason=str(raw.get("reason") or "")[:200])


def _decision_id_from_source_ref(source_ref: str | None) -> int | None:
    if not source_ref or not source_ref.startswith("decision:"):
        return None
    try:
        return int(source_ref.split(":", 1)[1])
    except ValueError:
        return None


def _apply_outcome(
    verdict: ReplyVerdict, ping: PingLike, note_id: int, *, db_path: Path | str | None
) -> str:
    """Deterministically apply one classified outcome; returns the receipt
    text. NEVER raises — an outcome-side-effect failure degrades to the plain
    filed-note receipt (the musing is already safely landed and linked; only
    the outcome-specific extra write is lost, and that is logged loudly)."""
    intent = verdict.intent
    filed_receipt = f"Noted — linked to the {ping.class_} finding."
    try:
        # 'unrelated' never reaches here: window mode resolves it BEFORE landing
        # (dispatch returns False, ordinary capture takes the message) and
        # direct mode never offers it.
        if intent == "acknowledge":
            mark_ping_acted(ping.id, db_path=db_path)
            return f"Logged against the {ping.class_} finding."
        if intent == "dismiss":
            recorded, muted = record_dismissal(ping.id, db_path=db_path)
            if not recorded:
                return "Already handled."
            if muted:
                return f"Dismissed — {muted.replace('_', ' ')} pings are now muted."
            return "Dismissed."
        if intent == "annotate_decision":
            decision_id = _decision_id_from_source_ref(ping.source_ref)
            if decision_id is not None:
                from user_state.notes import set_note_links

                set_note_links(note_id, decision_id=decision_id, db_path=db_path)
                mark_ping_acted(ping.id, db_path=db_path)
                return f"Filed against decision #{decision_id}."
            return filed_receipt  # no decision on this ping — nothing to annotate against
        if intent == "profile_fact":
            # owner_profile.store.append_fact requires a CLOSED category
            # ('capacity'|'appetite'|'behavioral') and a schema-validated value
            # dict — one small Pydantic model PER FACT KIND
            # (owner_profile/models.py), not a generic "arbitrary text" shape.
            # Extracting a real (category, key, value, narrative) from a free-
            # text reply would need its own structured-extraction call, which
            # is out of scope here. Documented fallback, not a silent drop:
            # the reply still lands and stays linked as a plain note.
            return filed_receipt
        return filed_receipt  # 'note', or any fail-open fallback
    except Exception:
        log.warning(
            {"event": "coach_reply_apply_failed", "ping_id": ping.id, "intent": intent},
            exc_info=True,
        )
        return filed_receipt


def dispatch(
    token: str,
    update: telegram.Update,
    *,
    roster: RosterIndex | None,
    db_path: Path | str | None,
) -> bool:
    """The poller's coach-reply entry point. Returns True iff this update was
    consumed (the caller must ``continue``, never falling through to ordinary
    capture); False lets the poller's normal text handling run instead.

    Never raises: any failure BEFORE the musing lands returns False (ordinary
    capture handles the message); any failure AFTER it lands is swallowed and
    still returns True (the musing is safely captured either way, per the
    module's safety invariant)."""
    try:
        found = find_reply_ping(update, db_path=db_path)
    except Exception:
        log.warning({"event": "coach_reply_transient", "stage": "find"}, exc_info=True)
        return False
    if found is None:
        return False
    ping, mode = found
    log.info(
        {"event": "coach_reply_matched", "ping_id": ping.id, "class": ping.class_, "mode": mode}
    )

    if update.chat_id is None:
        return False

    verdict: ReplyVerdict | None = None
    if mode == "window":
        # Window mode classifies BEFORE landing: a window match is only a
        # guess ("a message soon after a ping"), and consuming it would skip
        # the poller's full capture chain (confirm / wondering tap / ledger
        # answer). 'unrelated' — or a classifier that died and can't vouch for
        # the match — hands the message back to ordinary capture, where a
        # question like "What's my cost basis on MELI?" still gets answered.
        verdict = classify_reply(ping, update.text or "", mode=mode, db_path=db_path)
        if verdict.intent == "unrelated" or verdict.degraded:
            log.info(
                {
                    "event": "coach_reply_window_released",
                    "ping_id": ping.id,
                    "degraded": verdict.degraded,
                }
            )
            return False

    result = ingest.ingest_capture(
        channel="telegram",
        media_kind="text",
        text=update.text,
        external_ref=f"tg:{update.update_id}",
        roster=roster,
        db_path=db_path,
    )
    if result.status != "landed" or result.note_id is None:
        return False  # duplicate/empty — nothing landed, nothing to route
    note_id = result.note_id

    with contextlib.suppress(Exception):  # a stash failure never affects capture
        from user_state.notes import patch_note_context

        patch_note_context(
            note_id, {"coach_ping_id": ping.id, "coach_ping_class": ping.class_}, db_path=db_path
        )

    receipt = filed_receipt = f"Noted — linked to the {ping.class_} finding."
    try:
        if verdict is None:  # direct mode: the reply-to gesture already vouches
            verdict = classify_reply(ping, update.text or "", mode=mode, db_path=db_path)
        log.info(
            {
                "event": "coach_reply_intent",
                "ping_id": ping.id,
                "intent": verdict.intent,
                "mode": mode,
            }
        )
        receipt = _apply_outcome(verdict, ping, note_id, db_path=db_path)
        log.info({"event": "coach_reply_applied", "ping_id": ping.id, "intent": verdict.intent})
    except Exception:  # classify_reply / _apply_outcome already fail open internally;
        # this is a belt-and-suspenders net for anything unexpected in between.
        log.warning({"event": "coach_reply_transient", "stage": "classify_apply"}, exc_info=True)
        receipt = filed_receipt

    with contextlib.suppress(telegram.TelegramError):
        telegram.send_message(token, update.chat_id, receipt)
    return True


__all__ = [
    "PURPOSE",
    "PingLike",
    "ReplyVerdict",
    "classify_reply",
    "dispatch",
    "find_reply_ping",
]
