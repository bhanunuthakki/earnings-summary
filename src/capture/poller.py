"""The capture poller loop — long-poll Telegram, ingest each musing, advance the
offset cursor.

Runs as a standalone process (a Windows scheduled task via
``execution/capture_poller.py``), never a Flask thread: capture liveness is
decoupled from the dashboard being open, and the task gets RestartOnFailure +
per-run logs for free. The offset is persisted so a restart never re-ingests a
message, and each ingest is independently dedup-guarded on ``tg:<update_id>``.

The loop is pure orchestration over the already-tested ``ingest`` + ``telegram``
seams; ``poll_once`` is unit-testable with a mocked Telegram client.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import time
from pathlib import Path
from typing import cast

from capture import ingest, research_notify, telegram
from capture.matcher import RosterIndex
from research.proposals import detect_and_create_task, get_task, tap_enabled

log = logging.getLogger(__name__)

# Per-status confirmation replies (plain ASCII — no emoji). A duplicate/empty
# gets no reply (silence = "already had it / nothing to say"). The
# needs_ticker text is the NO-BUTTONS fallback — when the candidate keyboard
# rides along, _confirm asks in-channel instead of sending the owner away.
_CONFIRM: dict[str, str] = {
    "landed": "Captured.",
    "reading_landed": "Saved to On My Mind.",
    "needs_ticker": "Captured. (Which ticker? Tap a candidate in the Ledger.)",
    "transcription_failed": "Couldn't transcribe that one - I kept the audio, try again?",
    "no_audio": "That voice note came through empty - try again?",
}


def _confirm_status(result: ingest.IngestResult) -> str:
    """Ambiguity rides on a LANDED result (``needs_ticker`` flag, never its own
    status) — surface it as its own confirm key so the reply can ask for the
    ticker instead of a bare 'Captured.'."""
    if result.status == "landed" and result.needs_ticker:
        return "needs_ticker"
    return result.status


def load_offset(path: Path | str) -> int | None:
    """The next getUpdates offset, or None on first run / unreadable file."""
    try:
        raw: object = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if isinstance(raw, dict):
        offset = cast("dict[str, object]", raw).get("offset")
        if isinstance(offset, int):
            return offset
    return None


def save_offset(path: Path | str, offset: int) -> None:
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"offset": offset}), encoding="utf-8")


def _needs_ticker_markup(
    result: ingest.IngestResult, db_path: Path | str | None
) -> dict[str, object] | None:
    """Set-ticker buttons on a needs_ticker confirm — the ambiguity is
    resolvable in one tap from the thread (the same write-once ``set_ticker``
    action the Ledger's chips call, via ``st:<ticker>:<note_id>``) instead of
    dead-ending at 'go open the Ledger'. Best-effort: any read failure falls
    back to the plain confirm text."""
    if not (result.status == "landed" and result.needs_ticker and result.note_id is not None):
        return None
    try:
        from user_state.notes import get_note

        note = get_note(result.note_id, db_path=db_path)
        cands = (note.context or {}).get("ticker_candidates") if note is not None else None
        if not isinstance(cands, list):
            return None
        return research_notify.ticker_keyboard(
            result.note_id, [str(c) for c in cast("list[object]", cands)]
        )
    except Exception:  # a failed keyboard never blocks the confirm
        return None


def _ladder_markup(result: ingest.IngestResult) -> dict[str, object] | None:
    """The On My Mind action-ladder keyboard for a landed capture — only when the
    feed flag is on (default off ⇒ None ⇒ the plain confirm, behavior unchanged).
    Lets the owner triage a thought/reading from the thread the moment it lands."""
    from onmymind.feed import onmymind_enabled

    if not (onmymind_enabled() and result.status == "landed" and result.note_id is not None):
        return None
    return research_notify.onmymind_keyboard(result.note_id)


def _message_id_of(result: object) -> int | None:
    """The ``message_id`` of a sendMessage result (Telegram returns the sent
    Message), or None. Used to remember which message carried a card so a later
    reply can be routed back to its note."""
    if isinstance(result, dict):
        mid = cast("dict[str, object]", result).get("message_id")
        if isinstance(mid, int):
            return mid
    return None


def _confirm(
    token: str,
    update: telegram.Update,
    status: str,
    ticker: str | None,
    *,
    enabled: bool,
    reply_markup: dict[str, object] | None = None,
) -> int | None:
    """Best-effort capture confirmation back into the thread. ``reply_markup``
    carries the On My Mind ladder buttons when the feed flag is on. Returns the
    sent message's id (so the caller can stash it on the note for reply-routing),
    or None when nothing was sent."""
    if not enabled or update.chat_id is None:
        return None
    text = _CONFIRM.get(status)
    if status == "landed" and ticker:
        text = f"Captured. ({ticker})"
    if status == "needs_ticker" and reply_markup is not None:
        text = "Captured. Which ticker?"  # the candidate buttons carry the rest
    if not text:
        return None
    # a failed confirmation never blocks capture
    with contextlib.suppress(telegram.TelegramError):
        result = telegram.send_message(token, update.chat_id, text, reply_markup=reply_markup)
        return _message_id_of(result)
    return None


def _stash_card_mid(note_id: int | None, mid: int | None, db_path: Path | str | None) -> None:
    """Remember which Telegram message carried a note's card
    (``context_json['telegram_message_id']``), so a later free-text REPLY to
    that message routes back to the note through the shared reply core. Uses the
    context_json idiom — no migration. Fire-and-forget: a stash failure never
    affects the capture that already landed."""
    if note_id is None or mid is None or db_path is None:
        return
    try:
        from user_state.notes import patch_note_context

        patch_note_context(note_id, {"telegram_message_id": mid}, db_path=db_path)
    except Exception:  # a failed stash never blocks capture
        return


def _tap(result: ingest.IngestResult, db_path: Path | str | None) -> int | None:
    """Fire the detection tap on a landed musing (fire-and-forget; never raises).
    Returns the new 'proposed' task id (an inert chip), or None."""
    if not (tap_enabled() and result.status == "landed" and result.note_id is not None):
        return None
    return detect_and_create_task(result.note_id, db_path=db_path, channel="telegram")


def _notify_wondering(
    token: str,
    update: telegram.Update,
    task_id: int,
    db_path: Path | str | None,
    *,
    enabled: bool,
) -> None:
    """Push a 'caught a wondering' card back into the thread (best-effort)."""
    if not enabled or update.chat_id is None:
        return
    task = get_task(task_id, db_path=db_path)
    if task is None:
        return
    with contextlib.suppress(telegram.TelegramError):
        research_notify.notify_new_task(token, update.chat_id, task)


def _pledge_and_annotate(
    token: str,
    update: telegram.Update,
    result: ingest.IngestResult,
    db_path: Path | str | None,
    *,
    enabled: bool,
) -> None:
    """The entry-coaching taps (W2): a pledge-shaped musing captures an owner
    decision and gets the catalyst-test challenge back; an annotation-shaped
    follow-up fills the newest pending stub's NULL conviction/falsifier.
    Fire-and-forget — never affects the landing."""
    if result.status != "landed" or result.note_id is None:
        return
    try:
        from research.pledge import (
            annotate_latest_pending,
            build_challenge,
            detect_and_capture_pledge,
        )
        from user_state.notes import get_note

        pledge = detect_and_capture_pledge(result.note_id, channel="telegram", db_path=db_path)
        if pledge is not None:
            if enabled and update.chat_id is not None:
                repo_root = Path(db_path).parent.parent if db_path else None
                reply = build_challenge(pledge, repo_root=repo_root, db_path=db_path)
                with contextlib.suppress(telegram.TelegramError):
                    telegram.send_message(token, update.chat_id, reply)
            return
        note = get_note(result.note_id, db_path=db_path)
        if note is None:
            return
        annotated = annotate_latest_pending(note.body, db_path=db_path)
        if annotated is not None and enabled and update.chat_id is not None:
            with contextlib.suppress(telegram.TelegramError):
                telegram.send_message(
                    token, update.chat_id, f"Noted — recorded on decision #{annotated}."
                )
    except Exception:  # entry coaching must never break capture
        return


def _answer(
    token: str,
    update: telegram.Update,
    result: ingest.IngestResult,
    db_path: Path | str | None,
    *,
    enabled: bool,
) -> None:
    """Answer a question-shaped capture back into the thread (best-effort).

    Uses the SAME answer core the web feed stores (``onmymind.respond``), so a
    question asked from Telegram and one typed at the desk get the identical
    answer — one brain, two mouths. ``answer_capture`` returns None (and never
    touches the engine) for a plain musing, so this is a cheap no-op for the
    common case. Fire-and-forget — an answer failure never affects capture."""
    if not (result.status == "landed" and result.note_id is not None) or db_path is None:
        return
    try:
        from onmymind.respond import answer_capture

        repo_root = Path(db_path).parent.parent
        answer = answer_capture(result.note_id, repo_root=repo_root, db_path=db_path)
    except Exception:  # answering must never break the poll loop
        return
    if answer and enabled and update.chat_id is not None:
        with contextlib.suppress(telegram.TelegramError):
            telegram.send_message(token, update.chat_id, answer)


def _decision_draft_tap(result: ingest.IngestResult, db_path: Path | str | None) -> None:
    """Fire-and-forget Decision Draft parse tap (P2.1 — PRD §9.2). Rides the
    already-landed musing note; its OWN governed purpose
    (``decision_draft_parse``), not a branch inside ``capture.triage``.

    Dispatch-ladder position: this call site lives in ``poll_once``'s
    ordinary-capture branch, which only runs once
    ``capture.coach_reply.dispatch`` has already returned False for this
    update — i.e. strictly AFTER coach-reply, matching the agreed ladder
    order (slash-commands -> pending-awaits -> card-reply -> coach-reply ->
    ordinary capture -> this tap).

    Blanket-except, like the coach_reply dispatch call site and every other
    sibling tap (_pledge_and_annotate / _artifact_brief / _answer): a
    draft-parser failure — or a propagated LLM hard stop — must NEVER block
    plain capture, which has already landed by the time this runs."""
    if result.status != "landed" or result.note_id is None:
        return
    try:
        from capture.decision_draft import parse_note

        parse_note(result.note_id, db_path=db_path)
    except Exception:
        log.warning({"event": "decision_draft_tap_error"}, exc_info=True)


_BRIEF_OFF = frozenset({"0", "false", "no", ""})


def artifact_brief_enabled() -> bool:
    """Auto artifact-briefing runs by DEFAULT (the owner chose auto-on-capture). Set
    ``LEDGER_ARTIFACT_BRIEF=0`` to disable — the kill switch for the web-fetch + LLM
    spend the brief incurs."""
    return os.environ.get("LEDGER_ARTIFACT_BRIEF", "1").strip().lower() not in _BRIEF_OFF


def roster_tickers(roster: RosterIndex | None) -> tuple[str, ...]:
    """The tracked ticker universe (for the brief's portfolio linkage)."""
    if roster is None:
        return ()
    return tuple(sorted(set(roster.symbol_to_ticker.values())))


def _artifact_brief(
    token: str,
    update: telegram.Update,
    result: ingest.IngestResult,
    roster: RosterIndex | None,
    db_path: Path | str | None,
    *,
    enabled: bool,
) -> None:
    """Auto-brief tap: when the intent tap flagged an artifact musing (it stamped
    ``engage_intent`` on the note), fetch + brief the artifact and push it back into
    the thread with the action ladder. Fire-and-forget — a fetch/LLM/parse failure
    NEVER affects capture. Gated by ``LEDGER_ARTIFACT_BRIEF`` (default on)."""
    if not (artifact_brief_enabled() and result.status == "landed" and result.note_id is not None):
        return
    try:
        from research.brief import generate_brief_for_note
        from user_state.notes import get_note

        note = get_note(result.note_id, db_path=db_path)
        if note is None or not isinstance((note.context or {}).get("engage_intent"), dict):
            return  # not an artifact musing — nothing to brief
        cache_dir = Path(db_path).parent / "artifact_cache" if db_path else None
        brief = generate_brief_for_note(
            result.note_id,
            db_path=db_path,
            holdings=roster_tickers(roster),
            cache_dir=cache_dir,
        )
    except Exception:  # auto-brief must never break capture
        return
    if brief and enabled and update.chat_id is not None:
        with contextlib.suppress(telegram.TelegramError):
            research_notify.send_engage_brief(token, update.chat_id, brief, result.note_id)


def _review_reply(repo_root: Path | None, text: str) -> str:
    """The instant, no-LLM ``/review`` reply for the poller's slash-command
    interception. Reuses ``advisor.position_review.review_reply_text`` — the
    same parser + renderer the Ask-tab ``/review`` command calls — so a review
    asked from Telegram and one asked from the web chat behave identically.
    Never raises: a missing repo_root or a build failure degrades to a short
    apology (per-update failures must never break the poll loop)."""
    if repo_root is None:
        return "Couldn't build that review (no database configured)."
    try:
        from advisor.position_review import review_reply_text

        return review_reply_text(repo_root, text, plain=True)
    except Exception:
        ticker = text.split()[1].upper() if len(text.split()) > 1 else "that ticker"
        return f"Couldn't build a review for {ticker}."


def _redteam_reply(db_path: Path | str | None, text: str) -> str:
    """The instant, no-LLM ``/redteam`` reply for the poller's slash-command
    interception. Reuses ``redteam.telegram_cmd.redteam_reply_text`` — the
    SAME numbered-list/response logic ``redteam.response.respond`` backs for
    the cockpit's ``/api/red_team/<id>/respond`` route, so a response typed
    from Telegram and one clicked in the web panel behave identically. Never
    raises: a missing db_path or any failure degrades to a short apology
    (per-update failures must never break the poll loop)."""
    if db_path is None:
        return "Couldn't reach the red-team store (no database configured)."
    try:
        from redteam.telegram_cmd import redteam_reply_text

        return redteam_reply_text(text, db_path=db_path)
    except Exception:
        return "Couldn't process that /redteam command."


def _dispatch_pending_reply(
    token: str, update: telegram.Update, db_path: Path | str | None
) -> bool:
    """Check the shared awaited-reply stash (PR2 — the Sunday packet's Rewrite
    action + the decision nudge's Fill-in-now) BEFORE the default musing
    route. Returns True iff this text was consumed as an awaited reply (the
    caller must ``continue``, never falling through to ``ingest_capture``).
    An expired/absent await returns False — capture is never permanently
    hijacked."""
    if update.chat_id is None or not update.text:
        return False
    from capture import decision_nudge, pending_replies
    from pipeline import weekly_packet

    pending = pending_replies.peek(update.chat_id, db_path=db_path)
    if pending is None:
        return False
    if pending.kind == decision_nudge.FILL_IN_KIND:
        decision_nudge.handle_fill_in_reply(
            token, update.chat_id, update.text, pending, db_path=db_path
        )
        return True
    if pending.kind == weekly_packet.REWRITE_KIND:
        item, wrote = weekly_packet.handle_awaited_reply(pending, update.text, db_path=db_path)
        with contextlib.suppress(telegram.TelegramError):
            telegram.send_message(
                token,
                update.chat_id,
                "Recorded." if wrote else "Couldn't parse that - tap the button again to retry.",
            )
        if item is not None and item.verdict is not None:
            with contextlib.suppress(Exception):
                weekly_packet.maybe_send_receipt(
                    token, update.chat_id, item.run_id, db_path=db_path
                )
        return True
    return False


def _find_reply_target(mid: int, db_path: Path | str | None) -> int | None:
    """The most recent OPEN note whose card was sent as Telegram message ``mid``
    (``context_json['telegram_message_id']``). ``list_notes`` excludes
    archived/superseded, so a dismissed card never re-captures a reply. None when
    no live note carries that message id (an old/expired card ⇒ the caller falls
    through to ordinary capture). Best-effort — a read failure returns None."""
    if db_path is None:
        return None
    try:
        from user_state.notes import list_notes

        for note in list_notes(limit=100, db_path=db_path):
            if (note.context or {}).get("telegram_message_id") == mid:
                return note.id
    except Exception:  # a failed lookup never hijacks capture
        return None
    return None


def _reply_to_card(
    token: str,
    update: telegram.Update,
    note_id: int,
    result: dict[str, object],
    db_path: Path | str | None,
) -> None:
    """Reply to the owner after a card-reply routed through ``handle_reply``:
    an action / note sends its short receipt; a chat answers the reply text via
    the SAME ask engine ``_answer`` uses at capture time (one brain). Best-effort
    — a reply-send / answer failure never matters (the routing already happened)."""
    if update.chat_id is None:
        return
    if str(result.get("mode")) == "acted":
        receipt = str(result.get("receipt") or "").strip()
        if receipt:
            with contextlib.suppress(telegram.TelegramError):
                telegram.send_message(token, update.chat_id, receipt)
        return
    # chat: answer the reply text in the card's ticker context.
    if db_path is None or not update.text:
        return
    try:
        from onmymind.respond import answer_text
        from user_state.notes import get_note

        note = get_note(note_id, db_path=db_path)
        tickers = [note.ticker] if note is not None and note.ticker else []
        repo_root = Path(db_path).parent.parent
        answer = answer_text(update.text, tickers=tickers, repo_root=repo_root, db_path=db_path)
    except Exception:  # answering must never break the poll loop
        return
    if answer:
        with contextlib.suppress(telegram.TelegramError):
            telegram.send_message(token, update.chat_id, answer)


def _dispatch_card_reply(token: str, update: telegram.Update, db_path: Path | str | None) -> bool:
    """A free-text REPLY to an On My Mind card (Telegram's ``reply_to_message``)
    → the SAME reply core the web reply box calls (``onmymind.reply.handle_reply``
    — one brain, two mouths). Returns True iff consumed (the caller must
    ``continue``, never falling through to ``ingest_capture``). An unknown /
    expired message id, or any failure, returns False so capture is never
    hijacked (degrade-silently, like ``_answer`` / ``_pledge_and_annotate``)."""
    if update.chat_id is None or not update.text or update.reply_to_message_id is None:
        return False
    if db_path is None:
        return False
    try:
        note_id = _find_reply_target(update.reply_to_message_id, db_path)
        if note_id is None:
            return False
        from onmymind.reply import handle_reply

        result = handle_reply(note_id, update.text, db_path=db_path)
    except Exception:  # any failure falls through to ordinary capture
        return False
    _reply_to_card(token, update, note_id, result, db_path)
    return True


def poll_once(
    token: str,
    *,
    db_path: Path | str | None,
    offset_path: Path | str,
    audio_dir: Path | str,
    roster: RosterIndex | None = None,
    poll_timeout: int = 50,
    confirm: bool = True,
) -> dict[str, int]:
    """One long-poll batch: ingest every update, advance the offset. Returns a
    small counts dict for the run log. Never raises on a per-update failure."""
    offset = load_offset(offset_path)
    updates = telegram.get_updates(token, offset=offset, timeout=poll_timeout)
    counts: dict[str, int] = {"updates": len(updates)}

    def bump(key: str) -> None:
        counts[key] = counts.get(key, 0) + 1

    for update in updates:
        bump(f"kind_{update.kind}")
        if update.chat_id is not None:
            # Persist the owner's chat id so the coach can INITIATE (governed
            # pings) without waiting for an inbound message. Best-effort.
            from capture.token_store import save_chat_id

            save_chat_id(update.chat_id)
        if update.kind == "text" and update.text:
            if update.text.lstrip().startswith("/"):
                # Telegram bot commands (/start, /review, ...) are chrome, not
                # musings — never capture them.
                stripped = update.text.lstrip()
                low = stripped.lower()
                if low.startswith("/review"):
                    # The coach's pings tell the owner to "/review {ticker}" —
                    # make that work in the channel that carries the ping.
                    # Reuses the exact ask-tab parsing/rendering so the two
                    # surfaces never drift; LLM-free and fast.
                    bump("command_review")
                    if confirm and update.chat_id is not None:
                        repo_root = Path(db_path).parent.parent if db_path else None
                        reply = _review_reply(repo_root, stripped)
                        with contextlib.suppress(telegram.TelegramError):
                            telegram.send_message(token, update.chat_id, reply)
                    continue
                if low.startswith("/redteam"):
                    # PR6 — the monthly Red Team's forced-response loop reachable
                    # from Telegram: bare "/redteam" lists open items, "/redteam
                    # <n> refute|accept|defer [text]" answers one. Reuses
                    # redteam.response.respond via redteam.telegram_cmd — the SAME
                    # state machine the cockpit's /api/red_team/<id>/respond route
                    # calls. LLM-free and fast, same shape as /review above.
                    bump("command_redteam")
                    if confirm and update.chat_id is not None:
                        reply = _redteam_reply(db_path, stripped)
                        with contextlib.suppress(telegram.TelegramError):
                            telegram.send_message(token, update.chat_id, reply)
                    continue
                bump("command")
                if confirm and update.chat_id is not None:
                    if low.startswith("/start"):
                        with contextlib.suppress(telegram.TelegramError):
                            telegram.send_message(
                                token,
                                update.chat_id,
                                "Ledger capture is live - send a voice memo or a thought "
                                "and it lands in your Ledger.",
                            )
                    else:
                        with contextlib.suppress(telegram.TelegramError):
                            telegram.send_message(
                                token,
                                update.chat_id,
                                "Not a command here. /review <TICKER> works; everything "
                                "else lives in the web chat (Ask tab).",
                            )
                continue
            if update.chat_id is not None and _dispatch_pending_reply(token, update, db_path):
                bump("pending_reply")
                continue
            # A free-text REPLY to an On My Mind card → the shared reply core
            # (handle_reply), the SAME brain the web reply box uses. An unknown /
            # expired card falls through to ordinary capture.
            if _dispatch_card_reply(token, update, db_path):
                bump("card_reply")
                continue
            # A free-text reply to a governed COACH PING (B3) — either a direct
            # Telegram reply-to-message, or a fresh message inside the
            # short window after the ping went out. Consumed replies never
            # fall through to ordinary capture; a bug here must never break
            # plain capture, hence the blanket except.
            try:
                from capture import coach_reply

                if coach_reply.dispatch(token, update, roster=roster, db_path=db_path):
                    bump("coach_reply")
                    continue
            except Exception:
                log.warning({"event": "coach_reply_dispatch_error"}, exc_info=True)
            # A bare URL → land as an On My Mind reading (a link the analyst
            # found), not a musing.  Anything else is stream-of-consciousness.
            url = ingest._extract_url(update.text)
            if url:
                rd = ingest.ingest_reading(
                    channel="telegram",
                    url=url,
                    external_ref=f"tg:{update.update_id}",
                    db_path=db_path,
                )
                # report as "reading_landed" so _confirm sends the right reply
                mid = _confirm(
                    token,
                    update,
                    "reading_landed" if rd.status == "landed" else rd.status,
                    None,
                    enabled=confirm,
                    reply_markup=_ladder_markup(rd),
                )
                _stash_card_mid(rd.note_id, mid, db_path)
                bump(f"reading_{rd.status}")
            else:
                result = ingest.ingest_capture(
                    channel="telegram",
                    media_kind="text",
                    text=update.text,
                    external_ref=f"tg:{update.update_id}",
                    roster=roster,
                    db_path=db_path,
                )
                bump(result.status)
                mid = _confirm(
                    token,
                    update,
                    _confirm_status(result),
                    result.ticker,
                    enabled=confirm,
                    reply_markup=_needs_ticker_markup(result, db_path) or _ladder_markup(result),
                )
                _stash_card_mid(result.note_id, mid, db_path)
                tid = _tap(result, db_path)
                if tid is not None:
                    bump("wondering")
                    _notify_wondering(token, update, tid, db_path, enabled=confirm)
                _pledge_and_annotate(token, update, result, db_path, enabled=confirm)
                _artifact_brief(token, update, result, roster, db_path, enabled=confirm)
                _answer(token, update, result, db_path, enabled=confirm)
                _decision_draft_tap(result, db_path)
        elif update.kind == "voice" and update.voice_file_id:
            dest = Path(audio_dir) / f"tg_{update.update_id}.oga"
            try:
                file_path = telegram.get_file_path(token, update.voice_file_id)
                Path(audio_dir).mkdir(parents=True, exist_ok=True)
                telegram.download_file(token, file_path, dest)
            except telegram.TelegramError:
                bump("download_failed")
                continue
            result = ingest.ingest_capture(
                channel="telegram",
                media_kind="voice",
                audio_path=dest,
                external_ref=f"tg:{update.update_id}",
                roster=roster,
                db_path=db_path,
            )
            bump(result.status)
            if result.status == "landed":
                dest.unlink(missing_ok=True)  # raw audio is transient — purge once landed
            mid = _confirm(
                token,
                update,
                _confirm_status(result),
                result.ticker,
                enabled=confirm,
                reply_markup=_needs_ticker_markup(result, db_path) or _ladder_markup(result),
            )
            _stash_card_mid(result.note_id, mid, db_path)
            tid = _tap(result, db_path)
            if tid is not None:
                bump("wondering")
                _notify_wondering(token, update, tid, db_path, enabled=confirm)
            _pledge_and_annotate(token, update, result, db_path, enabled=confirm)
            _artifact_brief(token, update, result, roster, db_path, enabled=confirm)
            _answer(token, update, result, db_path, enabled=confirm)
            _decision_draft_tap(result, db_path)
        elif update.kind == "document" and update.document_file_id:
            # A PDF, deck, or any document file → land as an On My Mind reading.
            # The file is downloaded and stored locally so future text extraction
            # can read it; the local path is indexed in context_json.
            docs_dir = Path(audio_dir).parent / "docs"
            safe_name = (update.document_file_name or "file").replace("/", "_")
            dest = docs_dir / f"tg_{update.update_id}_{safe_name}"
            try:
                file_path = telegram.get_file_path(token, update.document_file_id)
                docs_dir.mkdir(parents=True, exist_ok=True)
                telegram.download_file(token, file_path, dest)
            except telegram.TelegramError:
                bump("download_failed")
                continue
            rd = ingest.ingest_reading(
                channel="telegram",
                local_path=dest,
                file_name=update.document_file_name,
                mime_type=update.document_mime_type,
                caption=update.text,
                external_ref=f"tg:{update.update_id}",
                db_path=db_path,
            )
            mid = _confirm(
                token,
                update,
                "reading_landed" if rd.status == "landed" else rd.status,
                None,
                enabled=confirm,
                reply_markup=_ladder_markup(rd),
            )
            _stash_card_mid(rd.note_id, mid, db_path)
            bump(f"reading_{rd.status}")
        elif update.kind == "callback":
            # The Phase-1 research surface: an inline-button press (run a wondering,
            # or approve/further/steer/reject a proposal) → the one action core.
            bump("callback")
            with contextlib.suppress(telegram.TelegramError):
                research_notify.dispatch_callback(token, update, db_path=db_path)

    new_offset = telegram.next_offset(updates)
    if new_offset is not None:
        save_offset(offset_path, new_offset)
    return counts


def run_forever(
    token: str,
    *,
    db_path: Path | str | None,
    offset_path: Path | str,
    audio_dir: Path | str,
    roster: RosterIndex | None = None,
    poll_timeout: int = 50,
    error_backoff: float = 5.0,
) -> None:
    """Poll until killed. A transient Telegram/network error backs off and
    retries (the loop must survive any single-batch failure); the long-poll
    itself is the pace (no busy-wait)."""
    while True:
        try:
            counts = poll_once(
                token,
                db_path=db_path,
                offset_path=offset_path,
                audio_dir=audio_dir,
                roster=roster,
                poll_timeout=poll_timeout,
            )
            if counts.get("updates"):
                log.info({"event": "capture_poll", **counts})
        except telegram.TelegramError as exc:
            log.warning({"event": "capture_poll_telegram_error", "error": str(exc)})
            time.sleep(error_backoff)
        except Exception as exc:
            log.error({"event": "capture_poll_error", "error": str(exc)})
            time.sleep(error_backoff)
