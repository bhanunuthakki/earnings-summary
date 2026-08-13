"""Telegram side of the research loop (W1-8): push wonderings/proposals into the
thread and dispatch the inline-button callbacks back to the ONE action core.

Callback data is a compact ``kind:verb:id`` triple:
  ``rt:run:<task_id>``        run the two-pass engine (flag-gated) → push the card
  ``rp:<verb>:<proposal_id>`` the 4-action core (approve / further / steer / reject)
  ``st:<ticker>:<note_id>``   attribute a needs_ticker musing to a roster candidate
  ``wk:<verb>:<item_id>``     the Sunday packet's per-item verdict (PR2; item_id is a
                              weekly_packet_items row id) — accept / rewrite / drop / defer
  ``dn:<verb>:<decision_id>`` the point-of-intent decision nudge (PR2) — fill / skip
  ``al:<verb>:<artifact_id>`` the Incremental Dollar Recommendation card (P0.4b,
                              ``allocation.telegram_summary``) — why / open / dismiss
  ``tr:revert:<insight_id>``  (B4) undo an auto-adopted Tenet/stance receipt
  ``rx:<verb>:<task_id>``     (B7) the weekly packet's "expiring research"
                              line — run / session / drop a research_tasks
                              row before it silently expires

Free-text in the thread stays a musing (the capture path); the buttons are the
Wave-1 steering surface. A button 'steer' marks the proposal steered (the web inbox
carries the typed direction). Dispatch is pure orchestration over the already-tested
``research.proposals`` / ``research.run`` seams + the ``telegram`` client; the HTTP
send/answer are injected so it is unit-testable without the network.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Literal, cast

from capture import telegram
from research.proposals import (
    PROPOSAL_VERBS,
    ResearchProposal,
    ResearchTask,
    act_on_proposal,
    get_proposal,
    research_run_enabled,
)

SendFn = Callable[..., object]
AnswerFn = Callable[..., object]
EditFn = Callable[..., object]
RunFn = Callable[..., "int | None"]

# (button label, verb) — ASCII only (the thread stays emoji-free, like _CONFIRM).
_VERB_LABELS: tuple[tuple[str, str], ...] = (
    ("Approve", "approve"),
    ("Research further", "further"),
    ("Steer", "steer"),
    ("Reject", "reject"),
)


def proposal_keyboard(proposal_id: int) -> dict[str, object]:
    return telegram.inline_keyboard(
        [[(label, f"rp:{verb}:{proposal_id}")] for label, verb in _VERB_LABELS]
    )


def task_keyboard(task_id: int) -> dict[str, object]:
    return telegram.inline_keyboard([[("Research it", f"rt:run:{task_id}")]])


# The On My Mind action ladder on Telegram — one button per rung, callback data
# ``om:<verb>:<note_id>`` (the compact ``kind:verb:id`` triple parse_callback reads).
_LADDER_LABELS: tuple[tuple[str, str], ...] = (
    ("Incorporate", "incorporate"),
    ("Discuss", "discuss"),
    ("Save for later", "save"),
    ("Dismiss", "dismiss"),
)


def onmymind_keyboard(note_id: int) -> dict[str, object]:
    return telegram.inline_keyboard(
        [[(label, f"om:{verb}:{note_id}")] for label, verb in _LADDER_LABELS]
    )


def ticker_keyboard(note_id: int, candidates: Sequence[str]) -> dict[str, object] | None:
    """The needs_ticker attribution buttons — one per roster candidate,
    callback ``st:<TICKER>:<note_id>`` (the ticker rides the verb slot of the
    ``kind:verb:id`` triple). None when no usable candidates remain — the
    confirm then falls back to pointing at the Ledger's set-ticker chips."""
    seen: list[str] = []
    for c in candidates:
        t = str(c).strip().upper()
        if t and t not in seen:
            seen.append(t)
    if not seen:
        return None
    return telegram.inline_keyboard(
        [[(t, f"st:{t}:{note_id}") for t in seen[i : i + 3]] for i in range(0, len(seen), 3)]
    )


def _excerpt(text: str, limit: int = 600) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[:limit].rstrip() + "..."


def proposal_text(proposal: ResearchProposal) -> str:
    head = proposal.title.strip() or "(untitled)"
    if proposal.ticker:
        head = f"{proposal.ticker} - {head}"
    return f"{head}\n\n{_excerpt(proposal.body_md)}"


def send_proposal_card(
    token: str, chat_id: int, proposal: ResearchProposal, *, send: SendFn = telegram.send_message
) -> None:
    send(token, chat_id, proposal_text(proposal), reply_markup=proposal_keyboard(proposal.id))


def notify_new_task(
    token: str, chat_id: int, task: ResearchTask, *, send: SendFn = telegram.send_message
) -> None:
    """Tell the owner a wondering was caught. Offers a Research button only when the
    run flag is on (else detection-only — a chip with zero research spend)."""
    claim = task.claim.strip()
    if research_run_enabled():
        send(
            token,
            chat_id,
            f"Caught a wondering: {claim}\nResearch it?",
            reply_markup=task_keyboard(task.id),
        )
    else:
        send(token, chat_id, f"Caught a wondering: {claim}\n(Enable research to dig in.)")


def engage_brief_text(brief: dict[str, object]) -> str:
    """Render the stored ``engage_brief`` payload into a Telegram message body
    (ASCII, like the rest of the thread). Long fields are excerpted so the message
    stays well under Telegram's limit."""
    mode = str(brief.get("mode") or "brief")
    lines: list[str] = [
        "Stress-test of what you saved:" if mode == "stress" else "Brief of what you saved:"
    ]
    takeaways = brief.get("takeaways")
    if isinstance(takeaways, list):
        lines.extend(
            f"- {str(t).strip()}" for t in cast("list[object]", takeaways)[:5] if str(t).strip()
        )
    bull = _excerpt(str(brief.get("bull") or ""), 500)
    bear = _excerpt(str(brief.get("bear") or ""), 500)
    if bull:
        lines.append(f"\nBull: {bull}")
    if bear:
        lines.append(f"Bear: {bear}")
    if mode == "stress":
        for label, key in (
            ("What would change your mind", "changes_mind"),
            ("Second-order", "second_order"),
            ("Your book", "portfolio_map"),
        ):
            val = _excerpt(str(brief.get(key) or ""), 400)
            if val:
                lines.append(f"\n{label}: {val}")
    return "\n".join(lines)


def send_engage_brief(
    token: str,
    chat_id: int,
    brief: dict[str, object],
    note_id: int,
    *,
    send: SendFn = telegram.send_message,
) -> None:
    """Push the artifact brief into the thread with the On My Mind action ladder — the
    owner triages it (Incorporate / Discuss / Save / Dismiss) exactly like any card."""
    send(token, chat_id, engage_brief_text(brief), reply_markup=onmymind_keyboard(note_id))


def parse_callback(data: str | None) -> tuple[str, str, int] | None:
    """``'rp:approve:42'`` → ``('rp', 'approve', 42)``; malformed → None."""
    if not data:
        return None
    parts = data.split(":")
    if len(parts) != 3 or not parts[2].isdigit():
        return None
    return (parts[0], parts[1], int(parts[2]))


def _default_runner(
    task_id: int, *, db_path: Path | str | None, repo_root: Path | None
) -> int | None:
    from research.run import run_research_task

    return run_research_task(task_id, db_path=db_path, repo_root=repo_root)


def _state_stamp(label: str) -> str:
    """``"- approved 07-03"`` — the naive-UTC now-helper repo-wide (see
    ``research.governor._now`` / ``now_naive_utc``), MM-DD only: the stamp
    marks a card as handled, not a precise audit timestamp."""
    from user_state._db import now_naive_utc

    return f"- {label} {now_naive_utc().strftime('%m-%d')}"


def _stamp_card(
    token: str,
    update: telegram.Update,
    stamp: str,
    *,
    edit: EditFn,
) -> None:
    """Best-effort editMessage on the ORIGINAL card after a successful action:
    keep its text, append one state line, and strip the inline keyboard so a
    handled card can never be re-pressed as a no-op-looking retry. Needs both
    the chat + the originating message id (both parsed off callback_query);
    a failed or impossible edit never affects the action itself."""
    if update.chat_id is None or update.message_id is None:
        return
    body = update.message_text or ""
    text = f"{body}\n{stamp}" if body else stamp
    with contextlib.suppress(telegram.TelegramError):
        edit(token, update.chat_id, update.message_id, text, reply_markup=None)


def dispatch_callback(
    token: str,
    update: telegram.Update,
    *,
    db_path: Path | str | None = None,
    repo_root: Path | None = None,
    send: SendFn = telegram.send_message,
    answer: AnswerFn = telegram.answer_callback,
    edit: EditFn = telegram.edit_message,
    run: RunFn | None = None,
) -> str | None:
    """Handle one inline-button press. Returns a short status string (logs/tests).

    Trigger surface only — like the HTTP run route, it INVOKES the isolated engine
    (which keeps web-fetch and proposal-write in separate passes); it never fetches
    the web itself.

    On every SUCCESSFUL action, best-effort ``edit`` the original card in place
    (stamp + strip the keyboard) via :func:`_stamp_card` — a handled card stays
    visibly handled and its buttons stop doing anything. A stale/failed/
    malformed callback edits nothing."""
    cqid = update.callback_query_id
    callback_data = update.callback_data or ""
    if callback_data.startswith("spb:"):
        parts = callback_data.split(":")
        if len(parts) not in {2, 3} or (len(parts) == 3 and not parts[2].isdigit()):
            if cqid:
                answer(token, cqid, text="Unrecognized action.")
            return None
        verb = parts[1]
        obj_id = int(parts[2]) if len(parts) == 3 else None
        chat_id = update.chat_id

        from advisor import senior_partner_brief as spb
        from llm_artifact_store import read_artifact, read_current

        artifact = (
            read_artifact(obj_id, db_path=db_path)
            if obj_id is not None and verb != "dismiss_item"
            else None
        )
        if artifact is not None and (
            artifact.purpose != spb.PURPOSE
            or artifact.scope != "portfolio"
            or artifact.ticker is not None
        ):
            artifact = None
        if obj_id is not None and verb != "dismiss_item" and artifact is None:
            if cqid:
                answer(token, cqid, text="That brief is no longer available.")
            return "spb_stale"

        if verb == "review":
            inbox_url = spb.private_mobile_inbox_url()
            if cqid:
                answer(
                    token,
                    cqid,
                    text="Opening the private Inbox..."
                    if inbox_url
                    else "Inbox link not configured.",
                )
            if inbox_url and chat_id is not None:
                send(token, chat_id, inbox_url)
            return "spb_review" if inbox_url else "spb_review_unconfigured"

        if verb == "dismiss_item" and obj_id is not None:
            recorded, muted = spb.dismiss_routed_moment(obj_id, db_path=db_path)
            if cqid:
                if muted:
                    answer(
                        token,
                        cqid,
                        text=f"Dismissed; {muted.replace('_', ' ')} prompts are now muted.",
                    )
                else:
                    answer(token, cqid, text="Dismissed." if recorded else "Already handled.")
            return "spb_item_dismissed" if recorded else "spb_item_stale"

        if verb == "why":
            if artifact is None:
                artifact = read_current(
                    ticker=None,
                    purpose=spb.PURPOSE,
                    scope="portfolio",
                    db_path=db_path,
                )
            if artifact is None or not isinstance(artifact.content_json, dict):
                if cqid:
                    answer(token, cqid, text="No current brief on file.")
                return "spb_stale"
            try:
                brief = spb.SeniorPartnerBrief.model_validate(artifact.content_json)
            except Exception:
                if cqid:
                    answer(token, cqid, text="Could not read the current brief.")
                return "spb_stale"
            items = brief.action_requested_items() or brief.what_changed[:3]
            details = []
            for item in items[:3]:
                refs = ", ".join(item.source_refs[:3]) or "grounded current inputs"
                details.append(f"{item.title}\nSources: {refs}")
            if cqid:
                answer(token, cqid, text="Sending the brief's grounding...")
            if chat_id is not None:
                send(
                    token,
                    chat_id,
                    "\n\n".join(details)
                    if details
                    else "No material action was prioritized this week.",
                )
            return "spb_why"

        if verb in {"defer", "dismiss"}:
            recorded = spb.record_brief_action(
                cast("Literal['defer', 'dismiss']", verb),
                artifact_id=obj_id,
                db_path=db_path,
            )
            if cqid:
                answer(
                    token,
                    cqid,
                    text=verb.capitalize() + ("." if recorded else " could not be recorded."),
                )
            if recorded:
                _stamp_card(token, update, _state_stamp(verb), edit=edit)
            return f"spb_{verb}" if recorded else "spb_stale"

        if cqid:
            answer(token, cqid, text="Unrecognized action.")
        return None

    parsed = parse_callback(update.callback_data)
    if parsed is None:
        if cqid:
            answer(token, cqid, text="Unrecognized action.")
        return None
    kind, verb, obj_id = parsed
    chat_id = update.chat_id

    if kind == "rt" and verb == "run":
        if not research_run_enabled():
            if cqid:
                answer(token, cqid, text="Research is off.")
            return "run_disabled"
        runner = run or _default_runner
        try:
            proposal_id = runner(obj_id, db_path=db_path, repo_root=repo_root)
        except Exception:  # a run failure reverts the task; never break the loop
            if cqid:
                answer(token, cqid, text="Research failed; try again.")
            return "run_failed"
        if cqid:
            answer(token, cqid, text="Researching...")
        if proposal_id is not None and chat_id is not None:
            proposal = get_proposal(proposal_id, db_path=db_path)
            if proposal is not None:
                send_proposal_card(token, chat_id, proposal, send=send)
        _stamp_card(token, update, _state_stamp("researched"), edit=edit)
        return "ran"

    if kind == "rp" and verb in PROPOSAL_VERBS:
        applied = ""
        proposal = get_proposal(obj_id, db_path=db_path)
        if verb == "approve" and proposal is not None and proposal.kind == "question":
            from research.question_artifact import approve_question_proposal

            try:
                note = approve_question_proposal(obj_id, db_path=db_path)
                status = "approved"
                applied = f"open question #{note.id} persisted for {note.ticker or 'portfolio'}"
            except Exception:
                status = "pending"
        else:
            status = act_on_proposal(obj_id, verb, db_path=db_path)
        if (
            verb == "approve"
            and not applied
            and not (proposal is not None and proposal.kind == "question")
        ):
            from research.apply import apply_approved_proposal

            try:
                applied = apply_approved_proposal(obj_id, db_path=db_path)
            except Exception:  # never let a failed write break the callback ack
                applied = ""
        if cqid:
            suffix = f" {applied}" if applied else ""
            answer(token, cqid, text=f"{verb.capitalize()}: {status}.{suffix}")
        _stamp_card(token, update, _state_stamp(status), edit=edit)
        return status

    if kind == "om":
        # The On My Mind action ladder — the SAME action core the web route calls,
        # so a button press and a click behave identically. Safe by construction
        # (archive / context patch / inert task); never a web fetch or live write.
        from onmymind.feed import LADDER_VERBS, act_on_feed_item

        if verb not in LADDER_VERBS:
            if cqid:
                answer(token, cqid, text="Unrecognized action.")
            return None
        result = act_on_feed_item(obj_id, verb, db_path=db_path)
        if cqid:
            answer(token, cqid, text=result.message or ("Done." if result.ok else "Not found."))
        if result.ok:
            _stamp_card(token, update, _state_stamp(verb), edit=edit)
        return f"om_{verb}" if result.ok else "om_noop"

    if kind == "st":
        # A needs_ticker attribution from the thread — the same write-once
        # ``set_ticker`` action the Ledger's set-ticker chips call (PR #816),
        # so a button tap and a web click behave identically. The verb slot
        # carries the candidate ticker; the id is the note.
        from user_state.notes import set_ticker

        ticker = verb.strip().upper()
        if not ticker:
            if cqid:
                answer(token, cqid, text="Unrecognized action.")
            return None
        try:
            row = set_ticker(obj_id, ticker=ticker, db_path=db_path)
        except ValueError:
            # write-once: a stray second tap can never silently reassign
            if cqid:
                answer(token, cqid, text="Already attributed.")
            return "st_stale"
        if row is None:
            if cqid:
                answer(token, cqid, text="Unrecognized action.")
            return None
        if cqid:
            answer(token, cqid, text=f"Attributed to {ticker}.")
        _stamp_card(token, update, _state_stamp(f"attributed {ticker}"), edit=edit)
        return "st_set"

    if kind == "cp" and verb == "dismiss":
        # A coach-ping dismissal — the governor's training signal. Three
        # consecutive dismissals of a class auto-mute it (never argues).
        from research.governor import record_dismissal

        recorded, muted = record_dismissal(obj_id, db_path=db_path)
        if cqid:
            if muted:
                answer(
                    token,
                    cqid,
                    text=f"Dismissed — and {muted.replace('_', ' ')} pings are now muted.",
                )
            else:
                answer(token, cqid, text="Dismissed." if recorded else "Already handled.")
        if recorded:
            _stamp_card(token, update, _state_stamp("dismissed"), edit=edit)
        return "cp_dismissed" if recorded else "cp_stale"

    if kind == "cp" and verb == "review":
        # The Answer button on a falsifier_breach ping — the ping's own stated
        # next action (/review {ticker}) fired in-place, without the owner
        # retyping it. Same instant, LLM-free reply as the poller's /review
        # interception and the Ask-tab command.
        from research.governor import get_ping

        ping = get_ping(obj_id, db_path=db_path)
        if ping is None:
            if cqid:
                answer(token, cqid, text="Unrecognized action.")
            return None
        if not ping.ticker:
            if cqid:
                answer(token, cqid, text="No ticker on this ping.")
            return "cp_review_no_ticker"
        if cqid:
            answer(token, cqid, text=f"Reviewing {ping.ticker}...")
        if chat_id is not None:
            from advisor.position_review import review_reply_text

            # Same repo_root derivation as the poller's own /review
            # interception (poller.py's _pledge_and_annotate pattern) — the
            # poller never passes repo_root through explicitly, only db_path.
            root = repo_root or (Path(db_path).parent.parent if db_path else None)
            try:
                reply = (
                    review_reply_text(root, f"/review {ping.ticker}", plain=True)
                    if root is not None
                    else "Couldn't build that review (no database configured)."
                )
            except Exception:
                reply = f"Couldn't build a review for {ping.ticker}."
            send(token, chat_id, reply)
        _stamp_card(token, update, _state_stamp(f"reviewed {ping.ticker}"), edit=edit)
        return "cp_reviewed"

    if kind == "tr" and verb == "revert":
        # An adoption Revert tap (B4) — the inverse of session-distill
        # auto-adopt. revert_tenet is idempotent: a stale/second tap on an
        # already-reverted (or never-current) row returns None, so a
        # re-press of a handled card just answers "Already handled." rather
        # than erroring.
        from synthesis.tenets import revert_tenet

        result = revert_tenet(obj_id, db_path=db_path)
        if result is None:
            if cqid:
                answer(token, cqid, text="Already handled.")
            return "tr_stale"
        if cqid:
            answer(token, cqid, text="Reverted - prior belief restored.")
        _stamp_card(token, update, _state_stamp("reverted"), edit=edit)
        return "tr_reverted"

    if kind == "wk":
        # PR2 — the Sunday packet's action core (weekly_packet.apply_verdict).
        # 'awaiting' stashes a free-text reply (rewrite / decision fill-in);
        # a terminal verb applies immediately to the underlying substrate and
        # may complete the run, in which case the 'Packet clear' receipt goes
        # out right here (the receipt fires off the LAST verdict, not off the
        # original send — verdicts land async, hours or days apart).
        from pipeline.weekly_packet import apply_verdict, get_item, maybe_send_receipt

        result = apply_verdict(obj_id, verb, chat_id=chat_id, db_path=db_path)
        if result is None:
            if cqid:
                still_there = get_item(obj_id, db_path=db_path) is not None
                answer(
                    token, cqid, text="Already handled." if still_there else "Unrecognized action."
                )
            return "wk_stale"
        if result == "awaiting":
            if cqid:
                answer(token, cqid, text="Reply here with your own words.")
            return "wk_awaiting"
        if cqid:
            answer(token, cqid, text=f"{result.capitalize()}.")
        _stamp_card(token, update, _state_stamp(result), edit=edit)
        item = get_item(obj_id, db_path=db_path)
        if item is not None and chat_id is not None:
            with contextlib.suppress(Exception):
                maybe_send_receipt(token, chat_id, item.run_id, db_path=db_path, send=send)
        return f"wk_{result}"

    if kind == "al":
        # The Incremental Dollar Recommendation's Telegram card (P0.4b, PRD
        # §7.4 surface parity / §11.4 privacy). "why" answers inline with the
        # stored disconfirmers/uncertainty (no re-generation, no LLM call);
        # "open" answers with the deep-link path (the web surface is
        # Tailscale-only, so there is no public URL button to open);
        # "dismiss" calls the SAME action core the web route uses
        # (allocation.actions.act_on_recommendation) so a button press and a
        # web click behave identically.
        if verb == "why":
            from allocation.recommendation_artifact import PURPOSE
            from llm_artifact_store import read_current

            artifact = read_current(
                ticker=None, purpose=PURPOSE, scope="portfolio", db_path=db_path
            )
            if artifact is None or not isinstance(artifact.content_json, dict):
                if cqid:
                    answer(token, cqid, text="No recommendation on file.")
                return "al_stale"
            from allocation.recommendation_schema import IncrementalDollarRecommendation

            try:
                rec = IncrementalDollarRecommendation.model_validate(artifact.content_json)
            except Exception:
                if cqid:
                    answer(token, cqid, text="Could not read the recommendation.")
                return "al_stale"
            if cqid:
                answer(token, cqid, text="Sending why...")
            if chat_id is not None:
                unknowns = "; ".join(rec.main_unknowns[:3]) or "none stated"
                disconfirm = "; ".join(rec.disconfirming_evidence[:3]) or "none stated"
                send(
                    token,
                    chat_id,
                    f"Main uncertainty: {unknowns}\n\nDisconfirming evidence: {disconfirm}\n\n"
                    f"{rec.confidence_basis}",
                )
            return "al_why"
        if verb == "open":
            if cqid:
                answer(token, cqid, text="Open /#portfolio_allocation on the web dashboard.")
            return "al_open"
        if verb == "dismiss":
            from allocation.actions import RecommendationActionError, act_on_recommendation

            try:
                status = act_on_recommendation(obj_id, "dismiss", db_path=db_path)
            except RecommendationActionError:
                if cqid:
                    answer(token, cqid, text="Unrecognized action.")
                return None
            if cqid:
                answer(token, cqid, text="Dismissed.")
            _stamp_card(token, update, _state_stamp(status), edit=edit)
            return "al_dismissed"
        if cqid:
            answer(token, cqid, text="Unrecognized action.")
        return None

    if kind == "dn":
        # The point-of-intent decision nudge (PR2, Deliverable 2) — Fill in
        # now stashes the awaited two-line reply; Skip just records the
        # owner saw it (never re-nudged; the standing open-loops band keeps
        # the debt visible).
        from capture.decision_nudge import mark_skipped, start_fill_in

        if verb == "fill":
            if chat_id is not None:
                start_fill_in(obj_id, chat_id=chat_id, db_path=db_path)
            if cqid:
                answer(
                    token,
                    cqid,
                    text="Reply with two lines: conviction, then what would prove you wrong.",
                )
            return "dn_awaiting"
        if verb == "skip":
            mark_skipped(obj_id, db_path=db_path)
            if cqid:
                answer(token, cqid, text="Skipped.")
            _stamp_card(token, update, _state_stamp("skipped"), edit=edit)
            return "dn_skipped"
        if cqid:
            answer(token, cqid, text="Unrecognized action.")
        return None

    if kind == "rx":
        # B7 — the weekly packet's "expiring research" line, the action core
        # that kills silent expiry: a wondering that became a research task
        # nobody touched must never just vanish. 'run' respects the SAME
        # LEDGER_RESEARCH_RUN gate as rt:run (a RUN button never spends
        # unless research is explicitly turned on); 'session' hands back the
        # stored session_prompt for the B8 Claude-session bridge (no status
        # change — the task stays 'proposed' until a human closes the loop,
        # either by running it or dropping it, so it can still reappear next
        # week); 'drop' IS the "packet-acknowledged drop" that expires a task
        # immediately, independent of the weekly sweep's own
        # second-unanswered-week rule (execution/expire_stale_research.py).
        from research.proposals import get_task, set_task_status

        task = get_task(obj_id, db_path=db_path)
        if task is None or task.status != "proposed":
            if cqid:
                answer(token, cqid, text="Already handled.")
            return "rx_stale"

        if verb == "run":
            if not research_run_enabled():
                if cqid:
                    answer(token, cqid, text="Research is off.")
                return "run_disabled"
            runner = run or _default_runner
            try:
                proposal_id = runner(obj_id, db_path=db_path, repo_root=repo_root)
            except Exception:  # a run failure reverts the task; never break the loop
                if cqid:
                    answer(token, cqid, text="Research failed; try again.")
                return "run_failed"
            if cqid:
                answer(token, cqid, text="Researching...")
            if proposal_id is not None and chat_id is not None:
                proposal = get_proposal(proposal_id, db_path=db_path)
                if proposal is not None:
                    send_proposal_card(token, chat_id, proposal, send=send)
            _stamp_card(token, update, _state_stamp("researched"), edit=edit)
            return "rx_ran"

        if verb == "session":
            prompt = str(task.meta.get("session_prompt") or "").strip()
            if cqid:
                answer(token, cqid, text="Sending the session prompt...")
            if chat_id is not None:
                send(token, chat_id, prompt or "(no session prompt on file for this task)")
            _stamp_card(token, update, _state_stamp("sent to session"), edit=edit)
            return "rx_session"

        if verb == "drop":
            set_task_status(obj_id, "expired", db_path=db_path)
            if cqid:
                answer(token, cqid, text="Dropped.")
            _stamp_card(token, update, _state_stamp("dropped"), edit=edit)
            return "rx_dropped"

        if cqid:
            answer(token, cqid, text="Unrecognized action.")
        return None

    if cqid:
        answer(token, cqid, text="Unrecognized action.")
    return None
