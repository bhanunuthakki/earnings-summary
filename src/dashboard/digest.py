"""Daily morning HTML digest renderer.

One self-contained HTML page per day with five sections:

  1. Header                     — date + "what's new since yesterday" label
  2. What's new (last 24h)      — pending alerts fired in the window
  3. Outstanding queued actions — pending actions whose alert is outside (2)
  4. Upcoming this week         — estimated next-earnings for tracked names
                                   no upcoming-earnings data source is persisted
                                   (earnings_surprises holds only past releases)
  5. Recent thesis changes      — the cross-holding thesis-ledger panel: the
                                   newest accepted, alert-driven thesis edits
                                   (_render_thesis_ledger over list_recent_entries)

Empty-state path: sections 1+2 collapse to "Nothing fired in the last
24h" rather than emitting empty divs, so the morning open of an empty
day still looks intentional.
"""

from __future__ import annotations

import html
import sqlite3
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
from identity import DEFAULT_USER_ID
from ui.tokens import FAVICON_LINK
from user_state.ledger import list_recent_entries


def render_morning_digest(
    date: date,
    user_id: str = DEFAULT_USER_ID,
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
    pending_alerts = [a for a in pending_alerts_all if _as_naive_utc(a.fired_at) < window_end]
    pending_alerts.sort(key=lambda a: (a.ticker, a.trigger_kind))

    actions_per_alert: dict[int, list[QueuedActionRow]] = {}
    for a in pending_alerts:
        actions_per_alert[a.id] = _pending_actions_for(a.id, db_path)

    in_section_2_ids = {a.id for a in pending_alerts}
    all_pending_actions = list_pending_actions(user_id=user_id, db_path=db_path)
    outstanding_actions = [qa for qa in all_pending_actions if qa.alert_id not in in_section_2_ids]

    body = StringIO()
    body.write('<div class="l1-shell">')
    _render_header(body, date)
    _render_whats_new(body, pending_alerts, actions_per_alert)
    _render_outstanding(body, outstanding_actions)
    _render_upcoming(body, date, db_path)
    _render_thesis_ledger(body, user_id, db_path)
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
        body.write('<div class="empty-state">No outstanding queued actions.</div>')
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


# There is no future-earnings calendar table; estimate each tracked name's next
# report as its latest known release + one quarter, and surface those landing in
# the next two weeks. Honest approximation (labelled "est."), built from the
# earnings_surprises history that already exists — no new fetch / cron.
_NEXT_EARNINGS_GAP_DAYS = 91
_UPCOMING_HORIZON_DAYS = 14


def _upcoming_earnings(
    db_path: Path | None, today: date, *, horizon_days: int = _UPCOMING_HORIZON_DAYS
) -> list[tuple[str, date]]:
    """Estimated next-earnings dates within the horizon for tracked names.

    Best-effort: a missing DB / table yields ``[]`` so the digest degrades to an
    empty-state rather than raising. Read-only.
    """
    if db_path is None or not Path(db_path).exists():
        return []
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.Error:
        return []
    try:
        rows = conn.execute(
            """
            SELECT es.ticker, MAX(es.release_date) AS last_release
            FROM earnings_surprises es
            JOIN tracked_companies tc
              ON tc.ticker = es.ticker
             AND tc.archived_at IS NULL
             AND tc.list_type IN ('portfolio', 'evaluation')
            WHERE es.release_date IS NOT NULL
            GROUP BY es.ticker
            """
        ).fetchall()
    except sqlite3.Error:
        return []
    finally:
        conn.close()
    horizon_end = today + timedelta(days=horizon_days)
    out: list[tuple[str, date]] = []
    for ticker, last_release in rows:
        try:
            last = date.fromisoformat(str(last_release)[:10])
        except (ValueError, TypeError):
            continue
        est = last + timedelta(days=_NEXT_EARNINGS_GAP_DAYS)
        if today <= est <= horizon_end:
            out.append((str(ticker), est))
    out.sort(key=lambda t: (t[1], t[0]))
    return out


def _render_upcoming(body: StringIO, render_date: date, db_path: Path | None) -> None:
    """'Upcoming this week' — tracked names whose ESTIMATED next earnings (latest
    release + ~1 quarter) land within the next two weeks. Replaces the old
    no-calendar stub with a real, data-backed (if approximate) forward look."""
    upcoming = _upcoming_earnings(db_path, render_date)
    body.write('<section class="dash-section dash-upcoming">')
    body.write('<div class="dash-section-header">')
    body.write('<div class="dash-section-title">Upcoming this week</div>')
    body.write(f'<div class="dash-section-count">{len(upcoming)} est.</div>')
    body.write("</div>")
    if not upcoming:
        body.write(
            '<div class="empty-state">No estimated earnings in the next two weeks for '
            "tracked names.</div>"
        )
        body.write("</section>")
        return
    body.write('<ul class="upcoming-list">')
    for ticker, est in upcoming:
        body.write(
            '<li class="upcoming-item">'
            f'<span class="up-ticker">{_esc(ticker)}</span> '
            f'<span class="up-date">~{_esc(est.isoformat())}</span> '
            '<span class="up-note">est. next earnings</span></li>'
        )
    body.write("</ul>")
    body.write("</section>")


_LEDGER_KIND_LABELS: Mapping[str, str] = {
    "thesis_update": "Thesis update",
    "bear_append": "Bear case",
    "earnings_prep_append": "Earnings prep",
}


def _render_thesis_ledger(body: StringIO, user_id: str, db_path: Path | None) -> None:
    """Cross-holding 'recent thesis changes' — the append-only ledger of every
    accepted, alert-driven thesis edit. This previously had no reader on any
    surface (the section was a hard-coded 'deferred' stub), so the durable
    record of how the thesis moved over time was computed but never shown."""
    try:
        entries = list_recent_entries(user_id=user_id, limit=20, db_path=db_path)
    except (FileNotFoundError, RuntimeError, sqlite3.Error):
        entries = []

    body.write('<section class="dash-section dash-ledger">')
    body.write('<div class="dash-section-header">')
    body.write('<div class="dash-section-title">Recent thesis changes</div>')
    label = "entry" if len(entries) == 1 else "entries"
    body.write(f'<div class="dash-section-count">{len(entries)} {label}</div>')
    body.write("</div>")
    if not entries:
        body.write(
            '<div class="empty-state">'
            "No thesis-ledger entries yet — approving a queued action records one here."
            "</div></section>"
        )
        return
    body.write('<ul class="ledger-list">')
    for entry in entries:
        kind_label = _LEDGER_KIND_LABELS.get(entry.entry_kind, entry.entry_kind)
        when = _esc(entry.created_at.date().isoformat())
        body.write(
            '<li class="ledger-entry">'
            '<div class="ledger-meta">'
            f'<span class="ledger-ticker">{_esc(entry.ticker)}</span>'
            f'<span class="ledger-kind">{_esc(kind_label)}</span>'
            f'<span class="ledger-when">{when}</span>'
            "</div>"
            f'<div class="ledger-body">{_esc(entry.body)}</div>'
            "</li>"
        )
    body.write("</ul></section>")


def _render_footer(body: StringIO, render_date: date) -> None:
    generated_at = datetime.now(UTC).isoformat(timespec="seconds")
    body.write('<div class="l1-footer">')
    body.write(f"<span>Digest · {_esc(render_date.isoformat())}</span>")
    body.write(f"<span>generated {_esc(generated_at)}</span>")
    body.write("</div>")


# ----------------------------------------------------------------------------
# Internals
# ----------------------------------------------------------------------------


def _as_naive_utc(dt: datetime) -> datetime:
    """Coerce a timestamp to naive-UTC — the convention triggers persist
    ``fired_at`` in (``datetime.now(UTC).replace(tzinfo=None)``).

    Naive input is assumed to already be UTC (the trigger contract) and
    returned unchanged; aware input is converted to UTC then stripped. This
    keeps the window comparison total — mixing a naive ``fired_at`` with an
    aware bound raises ``TypeError`` — regardless of which shape a row was
    written in.
    """
    return dt.astimezone(UTC).replace(tzinfo=None) if dt.tzinfo is not None else dt


def _window_for_date(render_date: date) -> tuple[datetime, datetime]:
    """24h window straddling ``render_date``, as naive-UTC datetimes.

    Start: render_date − 1d at 00:00 UTC.
    End:   render_date + 1d at 00:00 UTC.

    Returned tz-naive to match how triggers persist ``fired_at`` (naive-UTC
    via ``datetime.now(UTC).replace(tzinfo=None)``); the store round-trips that
    naive, so the bounds must be naive too or the ``fired_at < window_end``
    comparison in ``render_morning_digest`` raises ``TypeError``.

    The wide window picks up after-close prints filed in yesterday's
    evening (any TZ) and morning-of-render fires alike. Filtering down
    to "alerts strictly before end" guards against a re-generated digest
    surfacing alerts from after the digest date.
    """
    start_dt = datetime.combine(render_date - timedelta(days=1), time.min)
    end_dt = datetime.combine(render_date + timedelta(days=1), time.min)
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
{FAVICON_LINK}
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
