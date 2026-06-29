"""Telegram side of the research loop (W1-8): push wonderings/proposals into the
thread and dispatch the inline-button callbacks back to the ONE action core.

Callback data is a compact ``kind:verb:id`` triple:
  ``rt:run:<task_id>``        run the two-pass engine (flag-gated) → push the card
  ``rp:<verb>:<proposal_id>`` the 4-action core (approve / further / steer / reject)

Free-text in the thread stays a musing (the capture path); the buttons are the
Wave-1 steering surface. A button 'steer' marks the proposal steered (the web inbox
carries the typed direction). Dispatch is pure orchestration over the already-tested
``research.proposals`` / ``research.run`` seams + the ``telegram`` client; the HTTP
send/answer are injected so it is unit-testable without the network.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

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


def dispatch_callback(
    token: str,
    update: telegram.Update,
    *,
    db_path: Path | str | None = None,
    repo_root: Path | None = None,
    send: SendFn = telegram.send_message,
    answer: AnswerFn = telegram.answer_callback,
    run: RunFn | None = None,
) -> str | None:
    """Handle one inline-button press. Returns a short status string (logs/tests).

    Trigger surface only — like the HTTP run route, it INVOKES the isolated engine
    (which keeps web-fetch and proposal-write in separate passes); it never fetches
    the web itself."""
    parsed = parse_callback(update.callback_data)
    cqid = update.callback_query_id
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
        return "ran"

    if kind == "rp" and verb in PROPOSAL_VERBS:
        status = act_on_proposal(obj_id, verb, db_path=db_path)
        if cqid:
            answer(token, cqid, text=f"{verb.capitalize()}: {status}.")
        return status

    if cqid:
        answer(token, cqid, text="Unrecognized action.")
    return None
