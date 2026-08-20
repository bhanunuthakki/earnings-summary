"""Shared alert-card renderer used by the feed and the shell rails.

A "card" is one alert + its associated queued actions + the evidence
drawer. Every surface (feed's chronological list, the Home/Holding
rails' compact variants) renders the same shape — the caller chooses
whether the status badge appears and whether the drawer starts open.
"""

from __future__ import annotations

import html
import json
from collections.abc import Mapping
from datetime import UTC
from io import StringIO
from typing import cast

from alerts import (
    ACTION_STATUS_APPLIED,
    ACTION_STATUS_CANCELLED,
    AlertRow,
    QueuedActionRow,
)
from dashboard.evidence_drawer import render_evidence_drawer
from ui.time import stamp_html

_ACTION_KIND_LABELS: dict[str, str] = {
    "thesis_update": "Thesis update",
    "bear_append": "Bear-case append",
    "sizing_update": "Sizing change",
    "earnings_prep_append": "Earnings-prep note",
}


def render_alert_card(
    body: StringIO,
    alert: AlertRow,
    actions: list[QueuedActionRow],
    show_status_badge: bool,
    brief_provenance: Mapping[str, object] | None = None,
    *,
    drawer_open: bool = True,
    category: str = "",
    rank_why: str = "",
) -> None:
    """Render a single alert card into ``body``.

    ``actions`` is the list of queued actions to surface below the
    evidence drawer (the feed shows all of them). The caller decides
    the filter — the renderer just paints whatever it's given.

    ``brief_provenance`` is the ticker's latest brief_provenance_log
    payload (see evidence_drawer.load_brief_provenance) so fact_id
    citations resolve to (source, fetched_at) instead of the dead
    "no brief provenance" cell.

    ``drawer_open=False`` collapses the evidence drawer (the Holding
    rail's compact cards); the feed keeps it expanded.

    ``category`` / ``rank_why`` come from the inbox ranker (Inbox v2): the
    category feeds the chip filtering (``data-cat``), the why-string becomes
    the trigger badge's "why ranked here" tooltip. Both default empty for
    callers outside the ranked stream.
    """
    # data-when (naive-UTC seconds) feeds the inbox unread tracking — full
    # cards on /feed accent the same way the rail's compact ones do.
    fired = alert.fired_at
    if fired.tzinfo is not None:
        fired = fired.astimezone(UTC).replace(tzinfo=None)
    cat_attr = f' data-cat="{_esc(category)}"' if category else ""
    body.write(
        f'<div class="alert-card k-card k-card-stack"{cat_attr} '
        f'data-when="{fired.isoformat(timespec="seconds")}">'
    )
    body.write('<div class="alert-card-head">')
    # data-peek-ticker: in the shell, hovering the badge shows the ticker
    # mini-card (UX9); inert markup on the standalone feed page.
    body.write(
        f'<span class="k-tick-sym" data-peek-ticker="{_esc(alert.ticker)}">'
        f"{_esc(alert.ticker)}</span>"
    )
    why_attr = f' title="ranked: {_esc(rank_why)}"' if rank_why else ""
    body.write(f'<span class="k-chip"{why_attr}>{_esc(alert.trigger_kind)}</span>')
    if show_status_badge:
        tone = {"pending": "warn", "approved": "ok"}.get(alert.status, "")
        pill_cls = "k-pill" + (f" k-pill-{tone}" if tone else "")
        body.write(f'<span class="{pill_cls}">{_esc(alert.status)}</span>')
    body.write(stamp_html(alert.fired_at, css="fired-at"))
    body.write("</div>")

    memo_text = memo_text_from_evidence(alert.evidence_json)
    if memo_text:
        body.write(f'<div class="alert-memo">{_esc(memo_text)}</div>')
    else:
        body.write(
            '<div class="alert-memo alert-memo-pending">'
            "memo pending — the action drafter hasn't synthesized this alert yet."
            "</div>"
        )

    body.write(render_evidence_drawer(alert, brief_provenance, default_open=drawer_open))

    if actions:
        body.write('<div class="queued-actions">')
        body.write('<div class="k-card-row-title">Queued actions</div>')
        for qa in actions:
            render_queued_action(body, qa)
        body.write("</div>")

    body.write("</div>")


def render_queued_action(body: StringIO, qa: QueuedActionRow) -> None:
    """Render one queued-action row. Visible state depends on ``status``:
    pending → approve/dismiss buttons; applied/cancelled → timestamp pill so
    the historical context is preserved on the feed. No CLI hint — the owner
    ruled the ``python execution/approve_queued_action.py`` line out of the
    scan surface (2026-06-14 feed-density ruling, re-flagged 2026-07-30); the
    CLI remains available for scripting, it just doesn't ride every card."""
    kind_label = _ACTION_KIND_LABELS.get(qa.action_kind, qa.action_kind)
    qa_body = _action_body(qa.payload)
    body.write('<div class="queued-action">')
    body.write(f'<span class="k-chip">{_esc(kind_label)}</span>')
    body.write(f'<div class="qa-body">{_esc(qa_body)}</div>')
    body.write('<div class="qa-actions">')
    if qa.status == ACTION_STATUS_APPLIED:
        body.write(stamp_html(qa.applied_at, css="qa-status-applied", prefix="applied "))
    elif qa.status == ACTION_STATUS_CANCELLED:
        body.write(stamp_html(qa.cancelled_at, css="qa-status-cancelled", prefix="cancelled "))
    else:
        # Absolute hrefs: the card renders on / AND /feed, where a
        # relative "approve?..." would resolve to a different (dead) path per
        # surface. The live route is GET /approve (comments_server.py).
        body.write(
            f'<a class="k-btn k-btn-sm" href="/approve?action_id={qa.id}">approve</a>'
            f'<a class="k-btn k-btn-quiet k-btn-sm" '
            f'href="/approve?action_id={qa.id}&dismiss=1">dismiss</a>'
        )
    body.write("</div>")
    body.write("</div>")


def _action_body(payload: Mapping[str, object]) -> str:
    """Extract a renderable text body from a queued-action payload.

    The action-shape contract (per ``action_kind``) is the drafter's
    responsibility (PR-N5+). The card tries common keys in order
    (``body`` → ``narrative`` → fallback JSON dump) so it stays useful
    while shapes settle.
    """
    raw_body = payload.get("body")
    if isinstance(raw_body, str) and raw_body:
        return raw_body
    raw_narrative = payload.get("narrative")
    if isinstance(raw_narrative, str) and raw_narrative:
        return raw_narrative
    return json.dumps(dict(payload), sort_keys=True, default=str)


# Per-trigger evidence fields that carry the human "so-what" line, tried in
# order: an explicit ``memo`` first, then each sensor's own summary field —
# earnings_tone writes ``summary``, material_news ``why_material``, saydo_due
# ``narrative``. So the card's at-a-glance line shows real content instead of
# "memo pending" for every trigger that surfaces a summary in its evidence.
_MEMO_EVIDENCE_FIELDS: tuple[str, ...] = ("memo", "summary", "why_material", "narrative")


def memo_text_from_evidence(raw: str) -> str | None:
    """Pull the card's at-a-glance memo line from evidence_json.

    Tries ``_MEMO_EVIDENCE_FIELDS`` in order and returns the first non-empty
    string. Returns None (→ "memo pending") only when none is present, e.g. a
    kpi_inflection alert whose memo rides on the draft rather than the evidence.
    Malformed JSON also → None (the evidence drawer surfaces the parse error
    separately, so we don't double-render the warning).
    """
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if not isinstance(parsed, dict):
        return None
    parsed_map = cast("Mapping[str, object]", parsed)
    for field in _MEMO_EVIDENCE_FIELDS:
        value = parsed_map.get(field)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _esc(text: str) -> str:
    return html.escape(text, quote=True)
