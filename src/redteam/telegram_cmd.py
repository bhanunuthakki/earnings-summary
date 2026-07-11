"""Deterministic ``/redteam`` Telegram command (PR6 — monthly_red_team.md
Phase 2: "Telegram `/redteam` mirrors `/review`"). No LLM calls.

Mirrors the poller's existing ``/review`` interception
(``capture.poller._review_reply`` -> ``advisor.position_review.review_reply_text``):
a small reply-text builder the poller calls directly (Telegram has no Flask
job registry to hand off to), reusing the SAME state machine
(``redteam.response.respond``) the Flask cockpit's
``/api/red_team/<id>/respond`` route calls — no parallel logic, per the task
spec.

Grammar
-------
``/redteam`` — dense numbered list of open (``open`` or ``deferred``) items
for the most recent run_key.

``/redteam <n> refute|accept|defer [reasoning text]`` — ``n`` is the 1-based
index into that SAME numbered list, recomputed fresh on every call (Telegram
carries no server-side list-cursor state). Because it is recomputed each
time, ``n`` is only stable between one ``/redteam`` listing and the next
reply typed against it — if items resolve in between, the numbering shifts;
callers are told to re-list. ``refute`` requires the trailing text (the
owner's reasoning); ``accept``/``defer`` ignore it.
"""

from __future__ import annotations

from pathlib import Path

from redteam import gate, response, store
from redteam.models import RedTeamItemRow
from redteam.response import Action

_ACTIONS: tuple[Action, ...] = ("refute", "accept", "defer")
_USAGE = "Usage: /redteam <n> refute|accept|defer [reasoning text]"


def _open_items_for_latest_run(
    db_path: Path | str | None,
) -> tuple[str | None, list[RedTeamItemRow]]:
    run_key = store.latest_run_key(db_path=db_path)
    if run_key is None:
        return None, []
    items = store.list_items_for_run(db_path=db_path, run_key=run_key)
    open_items = [i for i in items if i.status in gate.UNRESOLVED_STATUSES]
    return run_key, open_items


def _list_reply(db_path: Path | str | None) -> str:
    run_key, open_items = _open_items_for_latest_run(db_path)
    if run_key is None:
        return "No red-team run yet - the First-Saturday pass hasn't produced a brief."
    if not open_items:
        return f"{run_key}: CLOSED - every item has been answered."
    lines = [f"{run_key} - {len(open_items)} open (reply /redteam <n> refute|accept|defer <text>):"]
    for i, item in enumerate(open_items, start=1):
        subject = item.ticker or "cross-book"
        flag = " [ESCALATED - already deferred once]" if item.status == "deferred" else ""
        attack = item.attack_md if len(item.attack_md) <= 140 else item.attack_md[:137] + "..."
        lines.append(f"{i}. [{item.severity.upper()}] {subject} ({item.lens}){flag}: {attack}")
    return "\n".join(lines)


def redteam_reply_text(text: str, *, db_path: Path | str | None) -> str:
    """The full ``/redteam`` reply. Never raises — every failure mode
    degrades to a short, actionable reply string (the poller's per-update
    degrade contract)."""
    parts = text.strip().split(maxsplit=3)
    if len(parts) == 1:
        return _list_reply(db_path)
    if len(parts) < 3:
        return _USAGE
    n_raw, action_low = parts[1], parts[2].lower()
    if action_low not in _ACTIONS:
        return _USAGE
    action = action_low  # narrowed to Action by the membership check above
    try:
        n = int(n_raw)
    except ValueError:
        return f"'{n_raw}' isn't a number. {_USAGE}"
    response_md = parts[3].strip() if len(parts) > 3 else None

    run_key, open_items = _open_items_for_latest_run(db_path)
    if run_key is None or n < 1 or n > len(open_items):
        return f"No item #{n} in the current open list. Send /redteam to see the numbered list."
    item = open_items[n - 1]

    try:
        response.respond(db_path=db_path, item_id=item.id, action=action, response_md=response_md)
    except response.ResponseRequiresTextError:
        return f"REFUTE needs your reasoning: /redteam {n} refute <text>."
    except response.SecondDeferRejectedError:
        return (
            f"#{n} ({item.ticker or 'cross-book'}) was already deferred once - it's now "
            "escalated. Respond with refute or accept."
        )
    except response.AlreadyRespondedError:
        return f"#{n} already has a response on file. Send /redteam to see the current list."
    except response.ItemNotFoundError:
        return f"No item #{n} in the current open list. Send /redteam to see the numbered list."
    except Exception:
        return f"Couldn't record that response for #{n} - try again?"

    verb = {"refute": "Refuted", "accept": "Accepted", "defer": "Deferred"}[action]
    return f"{verb} #{n} ({item.ticker or 'cross-book'})."


__all__ = ["redteam_reply_text"]
