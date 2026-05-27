"""Daily morning HTML digest renderer.

One self-contained HTML page per day with five sections:

  1. Header                     — date + "what's new since yesterday" label
  2. What's new (last 24h)      — pending alerts fired in the window
  3. Outstanding queued actions — pending actions whose alert is outside (2)
  4. Upcoming this week         — hard-coded "no calendar integration yet"
                                   stub (filled in by a later PR)
  5. Cross-holding rollup       — hard-coded "deferred per roadmap" stub

Empty-state path: sections 1+2 collapse to "Nothing fired in the last
24h" rather than emitting empty divs, so the morning open of an empty
day still looks intentional.
"""

from __future__ import annotations

import html
from collections.abc import Mapping
from datetime import UTC, date, datetime, time, timedelta
from io import StringIO
from pathlib import Path

from alerts import (
    AlertRow,
    QueuedActionRow,
    list_pending_actions,
    list_pending_alerts,
    list_queued_actions_for_alert,
)
from dashboard._card import render_alert_card, render_queued_action
from dashboard._styles import CSS


def render_morning_digest(
    date: date,
    user_id: str = "bhanu",
    db_path: Path | None = None,
) -> str:
    """Return the full HTML string for the morning digest of ``date``.

    The 24h window is defined as ``[date - 1d, date + 1d)`` in UTC — wide
    enough that the morning of ``date`` always picks up "yesterday after-
    close" prints regardless of the user's local timezone. The store
    queries by ``fired_at >=`` (no upper bound) but the populated set is
    further filtered in this renderer to ensure we don't surface alerts
    fired *after* the digest date (e.g. when a digest is re-generated for
    a historical date).
    """
    window_start, window_end = _window_for_date(date)
    pending_alerts_all = list_pending_alerts(
        user_id=user_id,
        since=window_start,
        db_path=db_path,
    )
    pending_alerts = [a for a in pending_alerts_all if a.fired_at < window_end]
    pending_alerts.sort(key=lambda a: (a.ticker, a.trigger_kind))

    actions_per_alert: dict[int, list[QueuedActionRow]] = {}
    for a in pending_alerts:
        actions_per_alert[a.id] = _pending_actions_for(a.id, db_path)

    in_section_2_ids = {a.id for a in pending_alerts}
    all_pending_actions = list_pending_actions(user_id=user_id, db_path=db_path)
    outstanding_actions = [
        qa for qa in all_pending_actions if qa.alert_id not in in_section_2_ids
    ]

    body = StringIO()
    body.write('<div class="l1-shell">')
    _render_header(body, date)
    _render_whats_new(body, pending_alerts, actions_per_alert)
    _render_outstanding(body, outstanding_actions)
    _render_upcoming_stub(body)
    _render_crossholding_stub(body)
    _render_footer(body, date)
    body.write("</div>")

    return _document(date, body.getvalue())


# ----------------------------------------------------------------------------
# Sections
# ----------------------------------------------------------------------------


def _render_header(body: StringIO, render_date: date) -> None:
    body.write('<header class="l1-header">')
    body.write(f"<h1>Morning digest · {_esc(render_date.isoformat())}</h1>")
    body.write(
        '<div class="l1-subtitle">'
        "What's new since yesterday — pending alerts, outstanding drafts, "
        "upcoming events, cross-holding rollup."
        "</div>"
    )
    body.write("</header>")


def _render_whats_new(
    body: StringIO,
    alerts: list[AlertRow],
    actions_per_alert: Mapping[int, list[QueuedActionRow]],
) -> None:
    body.write('<section class="dash-section dash-whats-new">')
    body.write('<div class="dash-section-header">')
    body.write('<div class="dash-section-title">What\'s new (last 24h)</div>')
    body.write(f'<div class="dash-section-count">{len(alerts)} alert(s)</div>')
    body.write("</div>")

    if not alerts:
        body.write(
            '<div class="empty-state">'
            "Nothing fired in the last 24h. ✓ Your portfolio looks quiet."
            "</div>"
        )
        body.write("</section>")
        return

    for alert in alerts:
        render_alert_card(
            body,
            alert,
            actions=list(actions_per_alert.get(alert.id, [])),
            show_status_badge=False,
        )
    body.write("</section>")


def _render_outstanding(body: StringIO, actions: list[QueuedActionRow]) -> None:
    body.write('<section class="dash-section dash-outstanding">')
    body.write('<div class="dash-section-header">')
    body.write('<div class="dash-section-title">Outstanding queued actions</div>')
    body.write(f'<div class="dash-section-count">{len(actions)} pending</div>')
    body.write("</div>")

    if not actions:
        body.write(
            '<div class="empty-state">No outstanding queued actions.</div>'
        )
        body.write("</section>")
        return

    grouped: dict[int, list[QueuedActionRow]] = {}
    for qa in actions:
        grouped.setdefault(qa.alert_id, []).append(qa)

    for alert_id, qas in grouped.items():
        body.write('<div class="alert-card">')
        body.write('<div class="alert-card-head">')
        body.write(
            f'<span class="trigger-badge">alert #{alert_id}</span>'
            f'<span class="fired-at">{len(qas)} draft(s) pending</span>'
        )
        body.write("</div>")
        body.write('<div class="queued-actions">')
        for qa in qas:
            render_queued_action(body, qa)
        body.write("</div>")
        body.write("</div>")
    body.write("</section>")


def _render_upcoming_stub(body: StringIO) -> None:
    body.write('<section class="dash-section dash-upcoming">')
    body.write('<div class="dash-section-header">')
    body.write('<div class="dash-section-title">Upcoming this week</div>')
    body.write("</div>")
    body.write(
        '<div class="empty-state">'
        "No calendar integration yet — earnings dates and SayDo verdicts "
        "will surface here once a later PR wires the source."
        "</div>"
    )
    body.write("</section>")


def _render_crossholding_stub(body: StringIO) -> None:
    body.write('<section class="dash-section dash-crossholding">')
    body.write('<div class="dash-section-header">')
    body.write('<div class="dash-section-title">Cross-holding rollup</div>')
    body.write("</div>")
    body.write(
        '<div class="empty-state">'
        "Cross-holding synthesis deferred (per roadmap)."
        "</div>"
    )
    body.write("</section>")


def _render_footer(body: StringIO, render_date: date) -> None:
    generated_at = datetime.now(UTC).isoformat(timespec="seconds")
    body.write('<div class="l1-footer">')
    body.write(f"<span>Digest · {_esc(render_date.isoformat())}</span>")
    body.write(f"<span>generated {_esc(generated_at)}</span>")
    body.write("</div>")


# ----------------------------------------------------------------------------
# Internals
# ----------------------------------------------------------------------------


def _window_for_date(render_date: date) -> tuple[datetime, datetime]:
    """24h window straddling ``render_date``.

    Start: render_date − 1d at 00:00 UTC.
    End:   render_date + 1d at 00:00 UTC.

    The wide window picks up after-close prints filed in yesterday's
    evening (any TZ) and morning-of-render fires alike. Filtering down
    to "alerts strictly before end" guards against a re-generated digest
    surfacing alerts from after the digest date.
    """
    start_dt = datetime.combine(render_date - timedelta(days=1), time.min, tzinfo=UTC)
    end_dt = datetime.combine(render_date + timedelta(days=1), time.min, tzinfo=UTC)
    return start_dt, end_dt


def _pending_actions_for(alert_id: int, db_path: Path | None) -> list[QueuedActionRow]:
    """Return only the pending queued actions for an alert. The store's
    ``list_queued_actions_for_alert`` returns all statuses; the digest
    only wants the actionable subset."""
    all_qa = list_queued_actions_for_alert(alert_id, db_path=db_path)
    return [qa for qa in all_qa if qa.status == "pending"]


def _document(render_date: date, body: str) -> str:
    title = f"Morning digest · {render_date.isoformat()}"
    return f"""<!doctype html>
<html lang="en" data-theme="dark">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_esc(title)}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500;600&family=Source+Serif+4:opsz,wght@8..60,400;8..60,500;8..60,600&display=swap" rel="stylesheet">
<style>{CSS}</style>
</head>
<body>
{body}
</body>
</html>
"""


def _esc(text: str) -> str:
    return html.escape(text, quote=True)
