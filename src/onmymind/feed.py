"""On My Mind feed — read model + the ONE action core.

The read model (:func:`load_feed`) is a keyset-paginated reverse-chron slice of
``analyst_notes`` (``source='capture'``), with the Wondering badge resolved in a
single BATCHED lookup (no N+1 across a page). The action core
(:func:`act_on_feed_item`) is the one place the ladder verbs execute, so the
web route and the Telegram callback share identical behavior (mirrors
``research.proposals.act_on_proposal``).

Ladder state lives in ``context_json['ladder']`` (save / discuss / incorporated);
dismiss is a ``status`` change (archive). Incorporate reuses the research tap's
task IDEMPOTENTLY per note (``ensure_task_for_note``) — one wondering never yields
two tasks.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from identity import DEFAULT_USER_ID
from research.proposals import ResearchTask, ensure_task_for_note, get_tasks_for_notes
from user_state.notes import (
    FEED_KINDS,
    AnalystNoteRow,
    archive_note,
    get_note,
    list_capture_feed,
    patch_note_context,
)

# The feed is flag-gated. Default flipped ON 2026-07-14 (owner sign-off): the
# overhauled feed (inline answers, contextual actions, inline chat) is the
# intended Ledger — with it OFF the owner saw the pre-overhaul plain Musings
# list with no action buttons and no rendered answers ("unresponsive buttons").
# Set LEDGER_ONMYMIND=0 to fall back to the legacy list.
_ON = frozenset({"1", "true", "yes", "on"})
_OFF = frozenset({"0", "false", "no", "off"})


def onmymind_enabled() -> bool:
    return os.environ.get("LEDGER_ONMYMIND", "1").strip().lower() not in _OFF


# The ladder rungs, canonical spelling shared by the route, the Telegram callback,
# and the panel JS. 'save' = save-for-later; 'discuss' opens a Socratic thread;
# 'worldview' stages the musing as a candidate Tenet (a lesson-learned's proper
# home, not the research queue).
LADDER_VERBS: tuple[str, ...] = ("dismiss", "save", "discuss", "incorporate", "worldview")

# ladder values persisted in context_json (dismiss is a status change, not a value)
_LADDER_VALUE: dict[str, str] = {
    "save": "saved",
    "discuss": "discuss",
    "incorporate": "incorporated",
    "worldview": "worldview",
}

# Persisted ladder value → the short display label the badge shows (renderer +
# web route share this so the initial paint and the post-action update agree).
LADDER_LABELS: dict[str, str] = {
    "saved": "saved",
    "discuss": "discussing",
    "incorporated": "in research",
    "worldview": "in worldview",
}

DEFAULT_PAGE = 30
_CURSOR_SEP = "|"


@dataclass(frozen=True, slots=True)
class FeedItem:
    """One rendered feed row: the note + its derived display state."""

    note: AnalystNoteRow
    item_type: str  # 'musing' | 'doc' | 'link'
    ladder: str | None  # None | 'saved' | 'discuss' | 'incorporated'
    wondering: ResearchTask | None  # the batched-resolved research task, if any
    # B7 routing regression fix (2026-07-25): a wondering triaged to answer_now
    # or belief_candidate creates NO task, so `wondering` above is None for it —
    # exactly the "silently disappears" failure B7 was built to close on the
    # OTHER two routes. wondering_route tells the badge which of the three B7
    # routes fired (None ⇒ not a detected wondering at all), independent of
    # whether a task exists; wondering_why carries the triage's one-line
    # reasoning for the two taskless routes (empty for research_task, where the
    # task itself is the receipt).
    wondering_route: str | None = None  # "research_task" | "answer_now" | "belief_candidate"
    wondering_why: str = ""


@dataclass(frozen=True, slots=True)
class FeedPage:
    items: list[FeedItem]
    next_cursor: str | None  # opaque keyset cursor; None ⇒ last page


@dataclass(frozen=True, slots=True)
class ActionResult:
    """The outcome of one ladder action — enough for the web route (JSON) and the
    Telegram callback (a short ack) to both act without re-deriving anything."""

    ok: bool
    removed: bool = False  # dismiss ⇒ the card leaves the feed
    ladder: str | None = None  # the new ladder value (save / discuss / incorporated)
    task_id: int | None = None  # incorporate ⇒ the research task id
    thread_url: str | None = None  # discuss ⇒ where to continue the thread
    message: str = ""  # short human line (Telegram ack)


def _item_type(note: AnalystNoteRow) -> str:
    """A reading carries its shape in context ('doc' | 'link'); everything else in
    the feed is a musing (a captured thought)."""
    it = str((note.context or {}).get("item_type") or "")
    return it if it in ("doc", "link") else "musing"


def _ladder(note: AnalystNoteRow) -> str | None:
    val = (note.context or {}).get("ladder")
    return str(val) if isinstance(val, str) and val else None


# The two B7 routes that create no task — research.proposals._route_wondering
# never stamps context['research_route'] for research_task (the task row IS
# that route's receipt), so this set is exhaustive for the context-derived case.
_TASKLESS_WONDERING_ROUTES = frozenset({"answer_now", "belief_candidate"})


def _wondering_route_and_why(note: AnalystNoteRow, *, has_task: bool) -> tuple[str | None, str]:
    """Which B7 route this note's wondering took, and why (taskless routes
    only) — derived from the SAME note row the feed already fetched, so this
    costs no extra query (no N+1 across a page). Task presence always wins
    (it's the ground truth); the context stamp is only consulted for the two
    routes that leave no other trace."""
    if has_task:
        return "research_task", ""
    ctx = note.context or {}
    route = ctx.get("research_route")
    if not isinstance(route, str) or route not in _TASKLESS_WONDERING_ROUTES:
        return None, ""
    why = ctx.get("research_route_why")
    return route, str(why)[:200] if isinstance(why, str) else ""


def _encode_cursor(note: AnalystNoteRow) -> str:
    # created_at is a naive-UTC datetime; isoformat() reproduces the stored TEXT
    # exactly, so the keyset compare in list_capture_feed round-trips.
    return f"{note.created_at.isoformat()}{_CURSOR_SEP}{note.id}"


def _decode_cursor(cursor: str | None) -> tuple[str | None, int | None]:
    if not cursor or _CURSOR_SEP not in cursor:
        return None, None
    created, _, rid = cursor.rpartition(_CURSOR_SEP)
    if not created or not rid.isdigit():
        return None, None
    return created, int(rid)


def load_feed(
    *,
    cursor: str | None = None,
    limit: int = DEFAULT_PAGE,
    user_id: str = DEFAULT_USER_ID,
    db_path: Path | str | None = None,
) -> FeedPage:
    """One keyset page of the feed, newest first, with the Wondering badge resolved
    in a single batched query. Best-effort empty page on a missing DB."""
    before_created, before_id = _decode_cursor(cursor)
    limit = max(1, min(int(limit), 200))
    # Fetch one extra to know whether a next page exists without a second query.
    rows = list_capture_feed(
        user_id=user_id,
        kinds=FEED_KINDS,
        limit=limit + 1,
        before_created_at=before_created,
        before_id=before_id,
        db_path=db_path,
    )
    has_more = len(rows) > limit
    rows = rows[:limit]
    tasks = get_tasks_for_notes([r.id for r in rows], db_path=db_path)
    items = []
    for r in rows:
        task = tasks.get(r.id)
        route, why = _wondering_route_and_why(r, has_task=task is not None)
        items.append(
            FeedItem(
                note=r,
                item_type=_item_type(r),
                ladder=_ladder(r),
                wondering=task,
                wondering_route=route,
                wondering_why=why,
            )
        )
    next_cursor = _encode_cursor(rows[-1]) if (has_more and rows) else None
    return FeedPage(items=items, next_cursor=next_cursor)


def load_feed_item(
    note_id: int,
    *,
    db_path: Path | str | None = None,
) -> FeedItem | None:
    """One feed row by note id — the card-level refresh read (a single card
    re-renders in place after set-ticker / capture instead of repainting the
    whole list, which would destroy any open inline chat). None for a missing,
    archived, or non-feed note."""
    note = get_note(note_id, db_path=db_path)
    if note is None or note.kind not in FEED_KINDS or note.status == "archived":
        return None
    tasks = get_tasks_for_notes([note.id], db_path=db_path)
    task = tasks.get(note.id)
    route, why = _wondering_route_and_why(note, has_task=task is not None)
    return FeedItem(
        note=note,
        item_type=_item_type(note),
        ladder=_ladder(note),
        wondering=task,
        wondering_route=route,
        wondering_why=why,
    )


def _thread_url_for(note: AnalystNoteRow) -> str:
    """Where 'discuss' hands off. A ticker-scoped item continues in that name's
    command-center chat; a portfolio-level item continues on the dashboard. (The
    deep Socratic thread wiring is a later phase — this is the doorway.)"""
    return f"/ticker/{note.ticker}" if note.ticker else "/"


def act_on_feed_item(
    note_id: int,
    verb: str,
    *,
    db_path: Path | str | None = None,
) -> ActionResult:
    """The ONE action core for the ladder rungs. Pure, safe-by-construction: it
    archives / patches context / find-or-creates an inert research task or a
    proposed Tenet. It NEVER fetches the web, calls an LLM, or writes a live
    artifact (incorporate stages a ``proposed`` task, worldview a ``proposed``
    Tenet — the owner still acts on each explicitly). Raises ValueError on an
    unknown verb."""
    if verb not in LADDER_VERBS:
        raise ValueError(f"unknown ladder verb {verb!r}; expected one of {LADDER_VERBS}")

    if verb == "dismiss":
        row = archive_note(note_id, db_path=db_path)
        if row is None:
            return ActionResult(ok=False, message="Not found.")
        return ActionResult(ok=True, removed=True, message="Dismissed.")

    note = get_note(note_id, db_path=db_path)
    if note is None:
        return ActionResult(ok=False, message="Not found.")

    if verb == "incorporate":
        task_id = ensure_task_for_note(note_id, db_path=db_path)
        if task_id is None:
            return ActionResult(ok=False, message="Not found.")
        patch_note_context(note_id, {"ladder": "incorporated"}, db_path=db_path)
        return ActionResult(
            ok=True, ladder="incorporated", task_id=task_id, message="Sent to research."
        )

    if verb == "worldview":
        # Stage the musing's own words as a candidate Tenet (proposed) WITHOUT an
        # LLM distill call — same no-fetch/no-LLM discipline as 'incorporate'. The
        # owner rewords/approves it in the Worldview panel.
        from synthesis.tenets import ensure_tenet_for_note

        tenet = ensure_tenet_for_note(note_id, body_md=note.body[:500], db_path=db_path)
        if tenet is None:
            return ActionResult(ok=False, message="Nothing to stage.")
        patch_note_context(note_id, {"ladder": "worldview"}, db_path=db_path)
        return ActionResult(ok=True, ladder="worldview", message="Staged as a candidate Tenet.")

    if verb == "discuss":
        patch_note_context(note_id, {"ladder": "discuss"}, db_path=db_path)
        url = _thread_url_for(note)
        return ActionResult(
            ok=True,
            ladder="discuss",
            thread_url=url,
            message="Let's dig into it — continue in the web thread.",
        )

    # save-for-later
    patch_note_context(note_id, {"ladder": _LADDER_VALUE["save"]}, db_path=db_path)
    return ActionResult(ok=True, ladder="saved", message="Saved for later.")
