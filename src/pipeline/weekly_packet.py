"""The Sunday packet (PR2, Deliverable 1 — navigation_ia.md §3.1, "the flagship").

The owner binge-clears a BOUNDED packet in one sitting (verified 2026-07-02)
and ignores standing dashboard drips. This assembles the same open-loop reads
``pipeline.open_loops`` already counts — unreconciled notes/themes/falsifiers,
proposed Tenets, pending research proposals, and stub decisions (NULL
conviction/falsifier) — into ONE finite, ordered Telegram packet: a header,
one message per item with an inline keyboard, and a "Packet clear" receipt
once the LAST item gets a verdict (which may be hours or days after the
packet was sent — verdicts arrive async via button taps, so the receipt
fires from the callback dispatch, not from the send step).

Verdicts (per item, buttons `wk:<verb>:<packet_item id>` — a packet_items row
id, never a composite key, to keep callback data under Telegram's 64-byte
limit):

  * reconcile_note / reconcile_theme / reconcile_falsifier / tenet / proposal
    get [Accept draft / Rewrite / Drop / Defer]. Accept and Drop apply
    IMMEDIATELY to the underlying substrate via the SAME write paths the
    Ledger's web actions call (``synthesis.reconcile``, ``synthesis.tenets``,
    ``research.proposals.act_on_proposal``) — a button press and a web click
    behave identically. Rewrite stashes an awaited free-text reply
    (``capture.pending_replies``, kind ``packet_rewrite``, keyed by the
    packet_item id) — the poller's text handler applies it once the owner
    replies (falsifier → ``edit``; tenet → record a new owner-authored
    current row + reject the proposed one; proposal → ``steer``; note/theme
    → mark ``superseded`` + best-effort annotate the owner's words).
  * decision_stub gets [Fill in now / Defer] — Fill in now reuses the SAME
    awaited-reply mechanism + ``capture.decision_nudge.apply_fill_in_reply``
    (two-line conviction/falsifier, write-once).
  * profile_fact_expiring (tenet-2 Phase 3) gets [Still true / Rewrite / Drop
    / Defer] — an affirmed ``owner_profile_facts`` row past its review
    horizon. "Still true" calls ``owner_profile.store.reaffirm_fact`` (bumps
    ``affirmed_at``, no value change); "Drop" calls ``retire_fact`` (status ->
    'rejected', the AFFIRMED-only sibling of ``reject_fact``); "Rewrite"
    stashes an awaited reply the SAME way, and the applied text lands a NEW
    ``proposed`` fact superseding the old via ``append_fact`` — gated
    assertion holds even on the owner's own edit (it resurfaces at the next
    packet walk for an explicit affirm tap, never auto-promoted).
  * Defer is packet-bookkeeping ONLY (no substrate write) on every kind — the
    item's verdict stays NULL, so it naturally reappears in NEXT week's
    packet (a fresh assemble() re-derives from the same still-unresolved
    substrate; nothing is lost).

Idempotency key: ``(iso_year, iso_week)`` — one ``weekly_packet_runs`` row per
week. A re-run for the same week ADDS any newly-surfaced items (never
duplicates existing ones, per ``UNIQUE(run_id, item_kind, ref_id)``) and never
re-sends a card that already went out (``sent_at IS NOT NULL``).
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, cast

from capture import pending_replies, telegram
from user_state._db import now_iso, now_naive_utc, open_conn

if TYPE_CHECKING:
    from research.proposals import ResearchTask

log = logging.getLogger(__name__)

SendFn = Callable[..., object]
PredraftCall = Callable[[str], "dict[str, object]"]

PREDRAFT_PURPOSE = "weekly_packet_predraft"
REWRITE_KIND = "packet_rewrite"

ITEM_KINDS: tuple[str, ...] = (
    "reconcile_note",
    "reconcile_theme",
    "reconcile_falsifier",
    "tenet",
    "proposal",
    "decision_stub",
    "profile_fact_expiring",
)
_DRAFT_VERBS: tuple[str, ...] = ("accept", "rewrite", "drop", "defer")
_DECISION_VERBS: tuple[str, ...] = ("fillin", "defer")
PACKET_VERBS: tuple[str, ...] = ("accept", "rewrite", "drop", "defer", "fillin")

_ACT_LABELS: tuple[tuple[str, str], ...] = (
    ("Accept draft", "accept"),
    ("Rewrite", "rewrite"),
    ("Drop", "drop"),
    ("Defer", "defer"),
)
_DECISION_LABELS: tuple[tuple[str, str], ...] = (
    ("Fill in now", "fillin"),
    ("Defer", "defer"),
)
# tenet-2 Phase 3 (§4 delivery seam 5 / §7 decision 6 "both"): an expiring
# AFFIRMED owner-profile fact rides the SAME verb plumbing as every other
# packet item (accept/rewrite/drop/defer) — only the button copy changes to
# match "re-affirm", not "approve a new draft".
_PROFILE_FACT_LABELS: tuple[tuple[str, str], ...] = (
    ("Still true", "accept"),
    ("Rewrite", "rewrite"),
    ("Drop", "drop"),
    ("Defer", "defer"),
)

_RECONCILE_KIND_MAP: dict[str, str] = {
    "note": "reconcile_note",
    "theme": "reconcile_theme",
    "falsifier": "reconcile_falsifier",
}


@dataclass(frozen=True, slots=True)
class PacketItemPlan:
    """One assembled item, pre-persistence."""

    item_kind: str
    ref_id: int
    ticker: str | None
    title: str


@dataclass(frozen=True, slots=True)
class PacketItemRow:
    id: int
    run_id: int
    item_kind: str
    ref_id: int
    ticker: str | None
    title: str
    predraft: str | None
    message_id: int | None
    sent_at: str | None
    verdict: str | None
    acted_at: str | None
    order_index: int


@dataclass(frozen=True, slots=True)
class WeeklyPacketRun:
    id: int
    iso_year: int
    iso_week: int
    status: str
    total_items: int


@dataclass(slots=True)
class PacketSendReport:
    run_id: int
    iso_year: int
    iso_week: int
    total_items: int = 0
    items_sent: int = 0
    predrafted: int = 0
    degraded: int = 0
    cleared: bool = False
    # B7 — expiring-research cards delivered this send (separate from the
    # ITEM_KINDS substrates above: these ride research_tasks directly, not
    # weekly_packet_items, so they get their own counter).
    expiring_research_sent: int = 0


# --------------------------------------------------------------------------
# Assembly (deterministic — no LLM required to walk the packet)
# --------------------------------------------------------------------------


def _decision_stub_plans(db_path: Path | str | None) -> list[PacketItemPlan]:
    """Owner decisions still ungradeable — NULL conviction or falsifier. Same
    debt band ``pipeline.open_loops._decision_stub_debt`` counts, here as full
    rows (unbounded by age — the Sunday sweep owns the WHOLE backlog; the
    same-day nudge, ``capture.decision_nudge``, owns the fresh cases)."""
    conn = open_conn(db_path)
    try:
        rows = conn.execute(
            """
            SELECT id, ticker, recommendation_kind, made_at FROM decisions
            WHERE decided_by = 'owner' AND outcome_label = 'pending'
              AND (conviction IS NULL OR falsifier IS NULL)
            ORDER BY id
            """
        ).fetchall()
    except Exception:
        return []
    finally:
        conn.close()
    return [
        PacketItemPlan(
            item_kind="decision_stub",
            ref_id=int(r[0]),
            ticker=str(r[1]) if r[1] else None,
            title=(
                f"{(str(r[2]) or '').upper()} decision missing conviction/falsifier "
                f"(made {str(r[3] or '')[:10]})"
            ),
        )
        for r in rows
    ]


def assemble_packet(db_path: Path | str | None = None) -> list[PacketItemPlan]:
    """Walk every open-loop substrate into one ordered, finite list. Never
    raises — a substrate that errors (missing table on a pre-migration DB)
    just contributes nothing, matching ``open_loops.render_open_loops_band``'s
    per-queue try/except discipline."""
    plans: list[PacketItemPlan] = []

    try:
        from synthesis.reconcile import list_unreconciled

        for it in list_unreconciled(db_path):
            kind = _RECONCILE_KIND_MAP.get(it.kind, it.kind)
            ticker = it.label if it.kind == "falsifier" else None
            title = it.body.strip()[:220] or it.label
            plans.append(
                PacketItemPlan(item_kind=kind, ref_id=it.item_id, ticker=ticker, title=title)
            )
    except Exception:
        log.warning({"event": "weekly_packet_assemble_failed", "substrate": "reconcile"})

    try:
        from synthesis.tenets import list_tenets

        for t in list_tenets(status="proposed", db_path=db_path):
            plans.append(
                PacketItemPlan(
                    item_kind="tenet", ref_id=t.id, ticker=None, title=t.body_md.strip()[:220]
                )
            )
    except Exception:
        log.warning({"event": "weekly_packet_assemble_failed", "substrate": "tenet"})

    try:
        from research.proposals import list_proposals

        for p in list_proposals(status="pending", db_path=db_path):
            title = p.title.strip() or "(untitled proposal)"
            plans.append(
                PacketItemPlan(item_kind="proposal", ref_id=p.id, ticker=p.ticker, title=title)
            )
    except Exception:
        log.warning({"event": "weekly_packet_assemble_failed", "substrate": "proposal"})

    try:
        plans.extend(_decision_stub_plans(db_path))
    except Exception:
        log.warning({"event": "weekly_packet_assemble_failed", "substrate": "decision_stub"})

    try:
        plans.extend(_profile_fact_expiring_plans(db_path))
    except Exception:
        log.warning(
            {"event": "weekly_packet_assemble_failed", "substrate": "profile_fact_expiring"}
        )

    return plans


def _profile_fact_expiring_plans(db_path: Path | str | None) -> list[PacketItemPlan]:
    """Affirmed owner-profile facts past their review horizon (tenet-2 Phase
    3) — the SAME ``list_expiring_facts`` predicate the governor's
    ``profile_drift`` class and the Ledger packet walk both read, so the
    definition of "needs re-affirmation" never drifts between surfaces."""
    from owner_profile.store import list_expiring_facts
    from user_state._db import open_conn

    conn = open_conn(db_path)
    try:
        rows = list_expiring_facts(conn)
    finally:
        conn.close()
    return [
        PacketItemPlan(
            item_kind="profile_fact_expiring",
            ref_id=row.id,
            ticker=None,
            title=f"Re-affirm ({row.category}): {row.narrative.strip()[:200]}",
        )
        for row in rows
    ]


# --------------------------------------------------------------------------
# State (weekly_packet_runs / weekly_packet_items)
# --------------------------------------------------------------------------


def _row_to_run(row: object) -> WeeklyPacketRun:
    r = cast("tuple[object, ...]", row)
    return WeeklyPacketRun(
        id=int(cast("int", r[0])),
        iso_year=int(cast("int", r[1])),
        iso_week=int(cast("int", r[2])),
        status=str(r[3]),
        total_items=int(cast("int", r[4]) or 0),
    )


def get_run(run_id: int, *, db_path: Path | str | None = None) -> WeeklyPacketRun | None:
    conn = open_conn(db_path)
    try:
        row = conn.execute(
            "SELECT id, iso_year, iso_week, status, total_items FROM weekly_packet_runs "
            "WHERE id = ?",
            (run_id,),
        ).fetchone()
        return None if row is None else _row_to_run(row)
    finally:
        conn.close()


def ensure_run(
    *, now: datetime | None = None, db_path: Path | str | None = None
) -> WeeklyPacketRun:
    """Find-or-create the run for this ISO (year, week) — the idempotency key."""
    stamp = now or now_naive_utc()
    iso_year, iso_week, _ = stamp.isocalendar()
    conn = open_conn(db_path)
    try:
        row = conn.execute(
            "SELECT id, iso_year, iso_week, status, total_items FROM weekly_packet_runs "
            "WHERE iso_year = ? AND iso_week = ?",
            (iso_year, iso_week),
        ).fetchone()
        if row is not None:
            return _row_to_run(row)
        now_s = now_iso()
        cur = conn.execute(
            "INSERT INTO weekly_packet_runs (iso_year, iso_week, status, total_items, "
            "created_at, updated_at) VALUES (?, ?, 'open', 0, ?, ?)",
            (iso_year, iso_week, now_s, now_s),
        )
        conn.commit()
        return WeeklyPacketRun(
            id=int(cur.lastrowid or 0),
            iso_year=iso_year,
            iso_week=iso_week,
            status="open",
            total_items=0,
        )
    finally:
        conn.close()


def _row_to_item(row: object) -> PacketItemRow:
    r = cast("tuple[object, ...]", row)
    return PacketItemRow(
        id=int(cast("int", r[0])),
        run_id=int(cast("int", r[1])),
        item_kind=str(r[2]),
        ref_id=int(cast("int", r[3])),
        ticker=None if r[4] is None else str(r[4]),
        title=str(r[5]),
        predraft=None if r[6] is None else str(r[6]),
        message_id=None if r[7] is None else int(cast("int", r[7])),
        sent_at=None if r[8] is None else str(r[8]),
        verdict=None if r[9] is None else str(r[9]),
        acted_at=None if r[10] is None else str(r[10]),
        order_index=int(cast("int", r[11]) or 0),
    )


_ITEM_COLS = (
    "id, run_id, item_kind, ref_id, ticker, title, predraft, message_id, sent_at, "
    "verdict, acted_at, order_index"
)


def get_item(item_id: int, *, db_path: Path | str | None = None) -> PacketItemRow | None:
    conn = open_conn(db_path)
    try:
        row = conn.execute(
            f"SELECT {_ITEM_COLS} FROM weekly_packet_items WHERE id = ?", (item_id,)
        ).fetchone()
        return None if row is None else _row_to_item(row)
    finally:
        conn.close()


def list_items(run_id: int, *, db_path: Path | str | None = None) -> list[PacketItemRow]:
    conn = open_conn(db_path)
    try:
        rows = conn.execute(
            f"SELECT {_ITEM_COLS} FROM weekly_packet_items WHERE run_id = ? ORDER BY order_index",
            (run_id,),
        ).fetchall()
        return [_row_to_item(r) for r in rows]
    finally:
        conn.close()


def sync_items(
    run_id: int, plans: list[PacketItemPlan], *, db_path: Path | str | None = None
) -> list[PacketItemRow]:
    """Insert any plan not already present for this run (``INSERT OR IGNORE``
    on the ``(run_id, item_kind, ref_id)`` uniqueness), preserving every
    existing item's state untouched. Returns ALL current items, ordered."""
    conn = open_conn(db_path)
    try:
        row = conn.execute(
            "SELECT COALESCE(MAX(order_index), -1) FROM weekly_packet_items WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        next_index = int(row[0]) + 1 if row and row[0] is not None else 0
        now_s = now_iso()
        for plan in plans:
            cur = conn.execute(
                "INSERT OR IGNORE INTO weekly_packet_items "
                "(run_id, item_kind, ref_id, ticker, title, order_index, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (run_id, plan.item_kind, plan.ref_id, plan.ticker, plan.title, next_index, now_s),
            )
            if cur.rowcount:
                next_index += 1
        conn.execute(
            "UPDATE weekly_packet_runs SET total_items = "
            "(SELECT COUNT(*) FROM weekly_packet_items WHERE run_id = ?), updated_at = ? "
            "WHERE id = ?",
            (run_id, now_s, run_id),
        )
        conn.commit()
    finally:
        conn.close()
    return list_items(run_id, db_path=db_path)


def _mark_item(item_id: int, *, verdict: str, db_path: Path | str | None) -> None:
    conn = open_conn(db_path)
    try:
        conn.execute(
            "UPDATE weekly_packet_items SET verdict = ?, acted_at = ? WHERE id = ?",
            (verdict, now_iso(), item_id),
        )
        conn.commit()
    finally:
        conn.close()


def _set_predraft(item_id: int, predraft_text: str | None, *, db_path: Path | str | None) -> None:
    conn = open_conn(db_path)
    try:
        conn.execute(
            "UPDATE weekly_packet_items SET predraft = ? WHERE id = ?", (predraft_text, item_id)
        )
        conn.commit()
    finally:
        conn.close()


def _set_sent(item_id: int, message_id: int | None, *, db_path: Path | str | None) -> None:
    conn = open_conn(db_path)
    try:
        conn.execute(
            "UPDATE weekly_packet_items SET message_id = ?, sent_at = ? WHERE id = ?",
            (message_id, now_iso(), item_id),
        )
        conn.commit()
    finally:
        conn.close()


def _mark_run_status(run_id: int, status: str, *, db_path: Path | str | None) -> None:
    conn = open_conn(db_path)
    try:
        now_s = now_iso()
        if status == "complete":
            conn.execute(
                "UPDATE weekly_packet_runs SET status = ?, updated_at = ?, completed_at = ? "
                "WHERE id = ?",
                (status, now_s, now_s, run_id),
            )
        else:
            conn.execute(
                "UPDATE weekly_packet_runs SET status = ?, updated_at = ? WHERE id = ?",
                (status, now_s, run_id),
            )
        conn.commit()
    finally:
        conn.close()


def _pending_count(run_id: int, *, db_path: Path | str | None) -> int:
    conn = open_conn(db_path)
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM weekly_packet_items WHERE run_id = ? AND verdict IS NULL",
            (run_id,),
        ).fetchone()
        return int(row[0] or 0)
    finally:
        conn.close()


def _tally(run_id: int, *, db_path: Path | str | None) -> dict[str, int]:
    conn = open_conn(db_path)
    try:
        rows = conn.execute(
            "SELECT verdict, COUNT(*) FROM weekly_packet_items "
            "WHERE run_id = ? AND verdict IS NOT NULL GROUP BY verdict",
            (run_id,),
        ).fetchall()
        return {str(r[0]): int(r[1]) for r in rows}
    finally:
        conn.close()


# --------------------------------------------------------------------------
# The LLM pre-draft (per-item degrade — a call failure never blocks delivery)
# --------------------------------------------------------------------------

_KIND_LABELS: dict[str, str] = {
    "reconcile_note": "an unreconciled seed note awaiting a live/superseded/rejected verdict",
    "reconcile_theme": "an unreconciled standing theme",
    "reconcile_falsifier": "a decision falsifier still marked (inferred) — not yet the owner's own words",
    "tenet": "a machine-proposed Tenet (a belief about how the owner invests) awaiting approval",
    "proposal": "a pending research proposal awaiting approve/further/steer/reject",
    "decision_stub": "an owner decision missing its conviction or falsifier",
    "profile_fact_expiring": (
        "an affirmed owner-profile fact past its review horizon, awaiting re-affirmation"
    ),
}


def _predraft_prompt(item: PacketItemRow) -> str:
    kind_label = _KIND_LABELS.get(item.item_kind, item.item_kind)
    return (
        "You are pre-drafting ONE verdict for a buy-side analyst's Sunday review "
        "packet, so a one-tap confirm replaces re-reading from scratch. This item is "
        f"{kind_label}.\n\n"
        f"Ticker: {item.ticker or '(none)'}\nItem: {item.title}\n\n"
        "Propose ONE short suggested action in AT MOST 2 lines, shaped as "
        "'<ratify|drop|defer> - <one-line reason, grounded ONLY in the item text above>'. "
        "Never invent facts not present in the item text; if you are not confident, "
        "propose 'defer - needs the owner's own read'. Return JSON ONLY: "
        '{"draft": "<your <=2-line verdict>"}'
    )


def _default_predraft_call(prompt: str, *, ticker: str | None) -> dict[str, object]:
    from llm.structured import call_llm_structured

    obj = call_llm_structured(
        prompt,
        purpose=PREDRAFT_PURPOSE,
        ticker=ticker,
        scope="ledger",
        expect="object",
        required_keys=("draft",),
    )
    return cast("dict[str, object]", obj) if isinstance(obj, dict) else {}


def predraft(item: PacketItemRow, *, call: PredraftCall | None = None) -> str | None:
    """The per-item LLM pre-draft. Degrades to None on ANY transient failure
    (CLI hiccup, unparseable JSON) — the item still ships with its buttons,
    just without a suggested-verdict line; hard stops (a hard-capped budget,
    an unusable CLI) propagate so a systemic outage isn't swallowed item by
    item across the whole packet."""
    from llm.cli import is_hard_stop
    from llm.structured import StructuredParseError

    runner: PredraftCall = (
        call if call is not None else (lambda p: _default_predraft_call(p, ticker=item.ticker))
    )
    try:
        raw = runner(_predraft_prompt(item))
    except StructuredParseError:
        return None
    except Exception as exc:
        if is_hard_stop(exc):
            raise
        log.warning({"event": "weekly_packet_predraft_failed", "item_id": item.id})
        return None
    draft_val = raw.get("draft")
    if not isinstance(draft_val, str) or not draft_val.strip():
        return None
    lines = [ln.strip() for ln in draft_val.strip().splitlines() if ln.strip()]
    return "\n".join(lines[:2])


# --------------------------------------------------------------------------
# B7 — expiring research (kill silent expiry). These ride ``research_tasks``
# directly rather than the ``weekly_packet_items`` table above: a proposed
# task the owner never ran is the SUBSTRATE (research.proposals owns it), so
# "packeted"/"unanswered weeks" state lands on ITS OWN row (the repurposed
# ``cost_usd`` / ``run_id`` columns — see research.proposals' module
# docstring) instead of duplicating tracking here. Deterministic — no LLM
# call in this section (unlike the ITEM_KINDS predraft above).
# --------------------------------------------------------------------------

# A 'proposed' research task this many days old is "about to expire" and
# earns its own packet line every week it stays proposed. The actual expiry
# (execution/expire_stale_research.py) fires only after a packet-acknowledged
# Drop tap or a SECOND unanswered week (unanswered_weeks >= 2) — this constant
# is just the warn threshold for the FIRST appearance.
RESEARCH_EXPIRING_WARN_DAYS = 7


def _expiring_research_tasks(*, warn_days: int, db_path: Path | str | None) -> list[ResearchTask]:
    """'proposed' research_tasks rows old enough to warrant a packet line.
    A task already packeted once and STILL proposed is exactly a recurring
    (second, third, ...) unanswered week — no separate query needed, since
    ``list_tasks(status='proposed')`` already excludes anything run/dropped/
    expired. Never raises (matches ``assemble_packet``'s per-substrate
    try/except discipline) — a lookup failure just yields no lines this
    week, never blocks the rest of the packet."""
    from research.proposals import list_tasks

    cutoff = (now_naive_utc() - timedelta(days=warn_days)).isoformat()
    try:
        tasks = list_tasks(status="proposed", db_path=db_path)
    except Exception:
        log.warning({"event": "weekly_packet_assemble_failed", "substrate": "research_expiring"})
        return []
    return [t for t in tasks if t.created_at and t.created_at < cutoff]


def _already_packeted_this_week(task: ResearchTask, *, iso_year: int, iso_week: int) -> bool:
    """True when the task's stored ``packeted_at`` (meta JSON) already falls in
    THIS run's ISO week — guards ``send_packet`` being invoked more than once
    in the same week (it is safe to re-run generally) against double-bumping
    ``unanswered_weeks`` or re-sending the same card twice in one week."""
    packeted_at = task.meta.get("packeted_at")
    if not isinstance(packeted_at, str) or not packeted_at:
        return False
    try:
        dt = datetime.fromisoformat(packeted_at)
    except ValueError:
        return False
    y, w, _ = dt.isocalendar()
    return (y, w) == (iso_year, iso_week)


def _resolved_cost_usd(task: ResearchTask) -> float:
    """The task's stored cost estimate, falling back to the SAME deterministic
    table the B7 triage tap stamped it with at detection time (a pre-B7 task
    that predates the column being used has ``cost_usd IS NULL``)."""
    from research.triage import estimate_cost_usd

    return task.cost_usd if task.cost_usd is not None else estimate_cost_usd(task.ticker)


def expiring_research_message_text(task: ResearchTask) -> str:
    """Render the card. ``task.meta['unanswered_weeks']`` is read AS-IS — the
    caller (:func:`_send_expiring_research`) passes a task snapshot already
    carrying THIS send's incremented count, so the number shown always
    matches what gets persisted right after, never a stale pre-increment
    value. The note is suppressed on a task's first-ever appearance (weeks
    <= 1) — "unanswered 1 week" reads oddly for something surfacing for the
    first time; it only means something once it recurs."""
    head = f"{task.ticker + ' - ' if task.ticker else ''}{task.claim.strip()}"
    weeks = task.meta.get("unanswered_weeks")
    weeks_n = int(weeks) if isinstance(weeks, int) else 0
    age_note = f" (unanswered {weeks_n} weeks)" if weeks_n >= 2 else ""
    return (
        f"Expiring research: {head}{age_note}\n\n"
        f"Run (~${_resolved_cost_usd(task):.2f}) / -> session / Drop"
    )


def expiring_research_keyboard(task: ResearchTask) -> dict[str, object]:
    labels: tuple[tuple[str, str], ...] = (
        (f"Run (~${_resolved_cost_usd(task):.2f})", "run"),
        ("-> session", "session"),
        ("Drop", "drop"),
    )
    return telegram.inline_keyboard([[(label, f"rx:{verb}:{task.id}")] for label, verb in labels])


def _send_expiring_research(
    token: str,
    chat_id: int,
    *,
    run: WeeklyPacketRun,
    warn_days: int = RESEARCH_EXPIRING_WARN_DAYS,
    db_path: Path | str | None,
    send: SendFn,
) -> int:
    """Deliver one card per about-to-expire research task, and stamp each
    task's ``packeted_at`` + bump ``unanswered_weeks`` (B7 — kill silent
    expiry). Per-item degrade: a failed send for one task never blocks the
    others; never raises out to ``send_packet``."""
    import dataclasses

    from research.proposals import set_task_extras

    tasks = _expiring_research_tasks(warn_days=warn_days, db_path=db_path)
    sent = 0
    for task in tasks:
        if _already_packeted_this_week(task, iso_year=run.iso_year, iso_week=run.iso_week):
            continue
        try:
            weeks_raw = task.meta.get("unanswered_weeks")
            weeks = (int(weeks_raw) if isinstance(weeks_raw, int) else 0) + 1
            # Render from a snapshot ALREADY carrying this send's incremented
            # count, so the card the owner sees matches what gets persisted
            # right after — never a stale pre-increment number.
            outgoing = dataclasses.replace(task, meta={**task.meta, "unanswered_weeks": weeks})
            with contextlib.suppress(telegram.TelegramError):
                send(
                    token,
                    chat_id,
                    expiring_research_message_text(outgoing),
                    reply_markup=expiring_research_keyboard(outgoing),
                )
            set_task_extras(
                task.id,
                cost_usd=_resolved_cost_usd(task),
                packeted_at=now_iso(),
                unanswered_weeks=weeks,
                db_path=db_path,
            )
            sent += 1
        except Exception:
            log.warning(
                {"event": "weekly_packet_expiring_research_send_failed", "task_id": task.id}
            )
    return sent


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------


def item_message_text(item: PacketItemRow, draft_text: str | None) -> str:
    head = f"{item.ticker + ' - ' if item.ticker else ''}{item.title}"
    return f"{head}\n\n{draft_text}" if draft_text else head


def item_keyboard(item: PacketItemRow) -> dict[str, object]:
    if item.item_kind == "decision_stub":
        labels = _DECISION_LABELS
    elif item.item_kind == "profile_fact_expiring":
        labels = _PROFILE_FACT_LABELS
    else:
        labels = _ACT_LABELS
    return telegram.inline_keyboard([[(label, f"wk:{verb}:{item.id}") for label, verb in labels]])


def _extract_message_id(result: object) -> int | None:
    if isinstance(result, dict):
        mid = cast("dict[str, object]", result).get("message_id")
        if isinstance(mid, int):
            return mid
    return None


# --------------------------------------------------------------------------
# Delivery
# --------------------------------------------------------------------------


def send_packet(
    token: str,
    chat_id: int,
    *,
    now: datetime | None = None,
    db_path: Path | str | None = None,
    send: SendFn = telegram.send_message,
    predraft_call: PredraftCall | None = None,
) -> PacketSendReport:
    """Assemble + sync + deliver. Never sends a card twice (``sent_at`` gate)
    and never re-sends the receipt for an already-``complete`` run — safe to
    invoke repeatedly across a day (or a week) of partial progress.

    B7: the expiring-research section (research_tasks-backed, not an
    ITEM_KINDS substrate) is delivered FIRST, unconditionally — it has its
    own per-week idempotency (:func:`_already_packeted_this_week`), so it is
    safe alongside the ``items``-empty early return below (that return is
    about the OTHER substrates; a "Ritual clear" while expiring-research
    cards were just sent above it would read as contradictory, so it is
    suppressed whenever this section sent anything)."""
    plans = assemble_packet(db_path=db_path)
    run = ensure_run(now=now, db_path=db_path)
    items = sync_items(run.id, plans, db_path=db_path)
    report = PacketSendReport(run_id=run.id, iso_year=run.iso_year, iso_week=run.iso_week)
    report.expiring_research_sent = _send_expiring_research(
        token, chat_id, run=run, db_path=db_path, send=send
    )

    if not items:
        if not report.expiring_research_sent:
            with contextlib.suppress(telegram.TelegramError):
                send(token, chat_id, "Ritual clear - nothing waiting on you.")
            _mark_run_status(run.id, "clear", db_path=db_path)
            report.cleared = True
        return report

    report.total_items = len(items)
    if run.status == "complete":
        return report  # fully handled already — nothing left to deliver

    to_send = [it for it in items if it.sent_at is None]
    if not to_send:
        return report

    with contextlib.suppress(telegram.TelegramError):
        send(token, chat_id, f"Sunday packet: {len(items)} items")

    for item in to_send:
        draft_text: str | None = None
        try:
            draft_text = predraft(item, call=predraft_call)
        except Exception:
            draft_text = None  # a hard stop here still must not sink the rest of the packet
        if draft_text is not None:
            _set_predraft(item.id, draft_text, db_path=db_path)
            report.predrafted += 1
        else:
            report.degraded += 1
        result = send(
            token, chat_id, item_message_text(item, draft_text), reply_markup=item_keyboard(item)
        )
        _set_sent(item.id, _extract_message_id(result), db_path=db_path)
        report.items_sent += 1

    return report


# --------------------------------------------------------------------------
# Verdict dispatch (the packet's action core — mirrors research.proposals.act_on_proposal)
# --------------------------------------------------------------------------


def _apply_accept(item: PacketItemRow, *, db_path: Path | str | None) -> None:
    if item.item_kind == "reconcile_note":
        from synthesis.reconcile import reconcile_note

        reconcile_note(item.ref_id, "live", db_path=db_path)
    elif item.item_kind == "reconcile_theme":
        from synthesis.reconcile import reconcile_theme

        reconcile_theme(item.ref_id, "live", db_path=db_path)
    elif item.item_kind == "reconcile_falsifier":
        from synthesis.reconcile import falsifier_action

        falsifier_action(item.ref_id, "ratify", db_path=db_path)
    elif item.item_kind == "tenet":
        from synthesis.tenets import approve_tenet

        approve_tenet(item.ref_id, db_path=db_path)
    elif item.item_kind == "proposal":
        from research.proposals import act_on_proposal

        act_on_proposal(item.ref_id, "approve", db_path=db_path)
    elif item.item_kind == "profile_fact_expiring":
        from owner_profile.store import reaffirm_fact
        from user_state._db import open_conn

        conn = open_conn(db_path)
        try:
            reaffirm_fact(conn, item.ref_id)
            conn.commit()
        finally:
            conn.close()


def _apply_drop(item: PacketItemRow, *, db_path: Path | str | None) -> None:
    if item.item_kind == "reconcile_note":
        from synthesis.reconcile import reconcile_note

        reconcile_note(item.ref_id, "resolved-rejected", db_path=db_path)
    elif item.item_kind == "reconcile_theme":
        from synthesis.reconcile import reconcile_theme

        reconcile_theme(item.ref_id, "resolved-rejected", db_path=db_path)
    elif item.item_kind == "reconcile_falsifier":
        from synthesis.reconcile import falsifier_action

        falsifier_action(item.ref_id, "drop", db_path=db_path)
    elif item.item_kind == "tenet":
        from synthesis.tenets import reject_tenet

        reject_tenet(item.ref_id, db_path=db_path)
    elif item.item_kind == "proposal":
        from research.proposals import act_on_proposal

        act_on_proposal(item.ref_id, "reject", db_path=db_path)
    elif item.item_kind == "profile_fact_expiring":
        from owner_profile.store import retire_fact
        from user_state._db import open_conn

        conn = open_conn(db_path)
        try:
            retire_fact(conn, item.ref_id)
            conn.commit()
        finally:
            conn.close()


def _apply_rewrite(item: PacketItemRow, text: str, *, db_path: Path | str | None) -> bool:
    """Apply an owner-authored replacement. Returns whether it actually wrote
    anything (only a decision_stub's fill-in can meaningfully fail — a
    reconcile/tenet/proposal rewrite always succeeds on non-empty text)."""
    text = (text or "").strip()
    if not text:
        return False
    if item.item_kind == "reconcile_note":
        from synthesis.reconcile import reconcile_note
        from user_state.notes import patch_note_context

        reconcile_note(item.ref_id, "superseded", db_path=db_path)
        with contextlib.suppress(Exception):
            patch_note_context(item.ref_id, {"packet_rewrite": text}, db_path=db_path)
        return True
    if item.item_kind == "reconcile_theme":
        from synthesis.reconcile import reconcile_theme

        return reconcile_theme(item.ref_id, "superseded", db_path=db_path)
    if item.item_kind == "reconcile_falsifier":
        from synthesis.reconcile import falsifier_action

        return falsifier_action(item.ref_id, "edit", text=text, db_path=db_path)
    if item.item_kind == "tenet":
        from synthesis.insights import get_insight
        from synthesis.tenets import record_tenet, reject_tenet

        old = get_insight(item.ref_id, db_path=db_path)
        if old is None:
            return False
        record_tenet(
            body_md=text,
            scope_key=old.scope_key,
            source_note_ids=old.source_note_ids,
            status="current",
            provenance="owner",
            db_path=db_path,
        )
        reject_tenet(item.ref_id, db_path=db_path)
        return True
    if item.item_kind == "proposal":
        from research.proposals import act_on_proposal

        act_on_proposal(item.ref_id, "steer", steer_text=text, db_path=db_path)
        return True
    if item.item_kind == "decision_stub":
        from capture.decision_nudge import apply_fill_in_reply

        return apply_fill_in_reply(item.ref_id, text, db_path=db_path)
    if item.item_kind == "profile_fact_expiring":
        # An owner-typed rewrite lands a NEW proposed fact superseding the
        # old (gated assertion holds, §7.1): even the owner's own edit needs
        # a fresh affirm tap — it resurfaces via the SAME proposed-facts
        # packet source (ledger_panel._packet_build) next walk, never
        # auto-promoted here.
        from owner_profile.store import append_fact, get_fact
        from user_state._db import open_conn

        conn = open_conn(db_path)
        try:
            old = get_fact(conn, item.ref_id)
            if old is None:
                return False
            append_fact(
                conn,
                category=old.category,
                key=old.key,
                value=old.value,
                narrative=text,
                provenance="owner",
                status="proposed",
                review_horizon_days=old.review_horizon_days,
                source_detail="packet_rewrite",
            )
            conn.commit()
        finally:
            conn.close()
        return True
    return False


def apply_verdict(
    packet_item_id: int, verb: str, *, chat_id: int | None, db_path: Path | str | None = None
) -> str | None:
    """The packet's ONE action core. Returns the applied verdict
    ('accept'/'drop'/'defer'), ``'awaiting'`` when a free-text reply was
    stashed (rewrite / decision-stub fill-in), or None on an unknown verb, a
    missing item, or a stale re-press (the item already has a verdict)."""
    if verb not in PACKET_VERBS:
        return None
    item = get_item(packet_item_id, db_path=db_path)
    if item is None or item.verdict is not None:
        return None
    is_decision = item.item_kind == "decision_stub"
    allowed = _DECISION_VERBS if is_decision else _DRAFT_VERBS
    if verb not in allowed and not (is_decision and verb == "fillin"):
        return None

    if verb == "rewrite" or (verb == "fillin" and is_decision):
        if chat_id is not None:
            pending_replies.stash(chat_id, REWRITE_KIND, item.id, db_path=db_path)
        return "awaiting"
    if verb == "accept":
        _apply_accept(item, db_path=db_path)
    elif verb == "drop":
        _apply_drop(item, db_path=db_path)
    # 'defer' writes nothing to the substrate — bookkeeping only.
    _mark_item(item.id, verdict=verb, db_path=db_path)
    return verb


def handle_awaited_reply(
    pending: pending_replies.PendingReply, text: str, *, db_path: Path | str | None = None
) -> tuple[PacketItemRow | None, bool]:
    """Consume an awaited packet reply (rewrite or decision-stub fill-in).
    Returns ``(item, wrote)`` — ``item`` is None on a stale/missing/
    already-handled packet_item id. A decision_stub whose reply failed to
    parse leaves the item unhandled (``wrote=False``) so it resurfaces
    (via next week's assemble) rather than silently counting as done."""
    pending_replies.consume(pending.id, db_path=db_path)
    item = get_item(pending.ref_id, db_path=db_path)
    if item is None or item.verdict is not None:
        return None, False
    wrote = _apply_rewrite(item, text, db_path=db_path)
    if wrote or item.item_kind != "decision_stub":
        _mark_item(item.id, verdict="rewrite", db_path=db_path)
        item = get_item(item.id, db_path=db_path)  # re-fetch so .verdict reflects the write
    return item, wrote


def maybe_send_receipt(
    token: str,
    chat_id: int,
    run_id: int,
    *,
    db_path: Path | str | None = None,
    send: SendFn = telegram.send_message,
) -> bool:
    """After a verdict lands, check whether the run is now fully handled; if
    so, send the 'Packet clear' receipt and mark the run complete. Idempotent
    — a run already ``complete`` is a no-op, so a rare double-trigger race
    never double-sends."""
    run = get_run(run_id, db_path=db_path)
    if run is None or run.status == "complete":
        return False
    if _pending_count(run_id, db_path=db_path) > 0:
        return False
    tally = _tally(run_id, db_path=db_path)
    ratified = tally.get("accept", 0)
    rewritten = tally.get("rewrite", 0)
    dropped = tally.get("drop", 0)
    deferred = tally.get("defer", 0)
    total = ratified + rewritten + dropped + deferred
    text = (
        f"Packet clear - {total} handled "
        f"({ratified} ratified, {rewritten} rewritten, {dropped} dropped, {deferred} deferred)"
    )
    with contextlib.suppress(telegram.TelegramError):
        send(token, chat_id, text)
    _mark_run_status(run_id, "complete", db_path=db_path)
    return True


__all__ = [
    "ITEM_KINDS",
    "PACKET_VERBS",
    "PREDRAFT_PURPOSE",
    "RESEARCH_EXPIRING_WARN_DAYS",
    "REWRITE_KIND",
    "PacketItemPlan",
    "PacketItemRow",
    "PacketSendReport",
    "WeeklyPacketRun",
    "apply_verdict",
    "assemble_packet",
    "ensure_run",
    "expiring_research_keyboard",
    "expiring_research_message_text",
    "get_item",
    "get_run",
    "handle_awaited_reply",
    "item_keyboard",
    "item_message_text",
    "list_items",
    "maybe_send_receipt",
    "predraft",
    "send_packet",
    "sync_items",
]
