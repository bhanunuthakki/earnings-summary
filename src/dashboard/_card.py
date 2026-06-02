"""Shared alert-card renderer used by both the digest and the feed.

A "card" is one alert + its associated queued actions + the evidence
drawer. Both surfaces (digest's "what's new" section, feed's chrono-
logical list) render the same shape — the only difference is whether
the status badge appears (the digest's "what's new" is pending-only;
the feed mixes pending/approved/dismissed).
"""

from __future__ import annotations

import html
import json
from collections.abc import Mapping
from datetime import datetime
from io import StringIO
from typing import cast

from alerts import (
    ACTION_STATUS_APPLIED,
    ACTION_STATUS_CANCELLED,
    AlertRow,
    QueuedActionRow,
)
from dashboard.evidence_drawer import render_evidence_drawer

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
) -> None:
    """Render a single alert card into ``body``.

    ``actions`` is the list of queued actions to surface below the
    evidence drawer (typically pending ones for the digest, all of them
    for the feed). The caller decides the filter — the renderer just
    paints whatever it's given.
    """
    body.write('<div class="alert-card">')
    body.write('<div class="alert-card-head">')
    body.write(f'<span class="ticker-badge">{_esc(alert.ticker)}</span>')
    body.write(f'<span class="trigger-badge">{_esc(alert.trigger_kind)}</span>')
    if show_status_badge:
        status_class = f"status-{alert.status}"
        body.write(f'<span class="status-badge {status_class}">{_esc(alert.status)}</span>')
    fired_at_iso = alert.fired_at.isoformat(timespec="seconds")
    body.write(f'<span class="fired-at">{_esc(fired_at_iso)}</span>')
    body.write("</div>")

    memo_text = _memo_text_from_evidence(alert.evidence_json)
    if memo_text:
        body.write(f'<div class="alert-memo">{_esc(memo_text)}</div>')
    else:
        body.write(
            '<div class="alert-memo alert-memo-pending">'
            "memo pending — the action drafter hasn't synthesized this alert yet."
            "</div>"
        )

    body.write(render_evidence_drawer(alert))

    if actions:
        body.write('<div class="queued-actions">')
        body.write("<h4>Queued actions</h4>")
        for qa in actions:
            render_queued_action(body, qa)
        body.write("</div>")

    body.write("</div>")


def render_queued_action(body: StringIO, qa: QueuedActionRow) -> None:
    """Render one queued-action row. Visible state depends on ``status``:
    pending → approve/dismiss links + CLI hint; applied/cancelled →
    timestamp pill so the historical context is preserved on the feed."""
    kind_label = _ACTION_KIND_LABELS.get(qa.action_kind, qa.action_kind)
    qa_body = _action_body(qa.payload)
    body.write('<div class="queued-action">')
    body.write(f'<span class="qa-kind">{_esc(kind_label)}</span>')
    body.write(f'<div class="qa-body">{_esc(qa_body)}</div>')
    body.write('<div class="qa-actions">')
    if qa.status == ACTION_STATUS_APPLIED:
        body.write(f'<span class="qa-status-applied">applied {_esc(_iso(qa.applied_at))}</span>')
    elif qa.status == ACTION_STATUS_CANCELLED:
        body.write(
            f'<span class="qa-status-cancelled">cancelled {_esc(_iso(qa.cancelled_at))}</span>'
        )
    else:
        body.write(
            f'<a href="approve?action_id={qa.id}">approve</a>'
            f'<a href="approve?action_id={qa.id}&dismiss=1">dismiss</a>'
            '<span class="qa-cli">CLI: '
            "<code>python execution/approve_queued_action.py "
            f"--action-id {qa.id}</code></span>"
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


def _memo_text_from_evidence(raw: str) -> str | None:
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


def _iso(dt: datetime | None) -> str:
    if dt is None:
        return "—"
    return dt.isoformat(timespec="seconds")


def _esc(text: str) -> str:
    return html.escape(text, quote=True)
