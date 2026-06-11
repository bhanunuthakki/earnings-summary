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
from dashboard.evidence_drawer import load_brief_provenance
from identity import DEFAULT_USER_ID
from report.renderers.numfmt import fmt_date
from ui.time import stamp_html
from ui.tokens import FAVICON_LINK
from user_state.ledger import list_recent_entries
from user_state.notes import AnalystNoteRow, list_notes


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
    _render_whats_new(body, pending_alerts, actions_per_alert, db_path)
    _render_open_items(body, user_id, db_path)
    _render_outstanding(body, outstanding_actions)
    _render_upcoming(body, date, db_path, user_id=user_id)
    _render_thesis_ledger(body, user_id, db_path)
    _render_footer(body, date)
    body.write("</div>")

    return _document(date, body.getvalue())


# ----------------------------------------------------------------------------
# Sections
# ----------------------------------------------------------------------------


def _render_header(body: StringIO, render_date: date) -> None:
    body.write('<header class="l1-header">')
    body.write(f"<h1>Morning digest · {_esc(fmt_date(render_date.isoformat()))}</h1>")
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
    db_path: Path | None = None,
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

    # One brief-provenance lookup per ticker so fact_id citations in the
    # evidence drawer resolve (P3.3); alerts routinely share tickers.
    prov_cache: dict[str, Mapping[str, object] | None] = {}
    for alert in alerts:
        if alert.ticker not in prov_cache:
            prov_cache[alert.ticker] = (
                load_brief_provenance(alert.ticker, db_path=db_path)
                if db_path is not None
                else None
            )
        render_alert_card(
            body,
            alert,
            actions=list(actions_per_alert.get(alert.id, [])),
            show_status_badge=False,
            brief_provenance=prov_cache[alert.ticker],
        )
    body.write("</section>")


# Lead kinds for the open-items panel + the per-ticker earnings-prep lists
# (P4.4): the owner's watch items and unanswered questions come first.
_OPEN_ITEM_KIND_RANK = {"watch": 0, "question": 1}
_OPEN_ITEMS_LIMIT = 30
_PREP_NOTES_PER_TICKER = 3


def _open_notes(
    db_path: Path | None, user_id: str, *, ticker: str | None = None
) -> list[AnalystNoteRow]:
    """Open analyst notes, lead-kinds first — best-effort ([] on any miss)."""
    if db_path is None or not Path(db_path).exists():
        return []
    try:
        rows = list_notes(user_id=user_id, ticker=ticker, status="open", db_path=db_path)
    except sqlite3.Error:
        return []
    return sorted(rows, key=lambda n: _OPEN_ITEM_KIND_RANK.get(n.kind, 9))


def _render_open_items(body: StringIO, user_id: str, db_path: Path | None) -> None:
    """'Open items' (P4.4) — the owner's standing watch items, questions, and
    other open notes from the analyst journal, so the digest carries what the
    owner already said to look for alongside what's new."""
    notes = _open_notes(db_path, user_id)[:_OPEN_ITEMS_LIMIT]
    body.write('<section class="dash-section dash-open-items">')
    body.write('<div class="dash-section-header">')
    body.write('<div class="dash-section-title">Open items</div>')
    body.write(f'<div class="dash-section-count">{len(notes)} open</div>')
    body.write("</div>")
    if not notes:
        body.write(
            '<div class="empty-state">No open items in the analyst journal — '
            "notes arrive from report comments, chat, and alert reviews.</div>"
        )
        body.write("</section>")
        return
    body.write('<ul class="open-items-list">')
    for n in notes:
        ticker_html = (
            f'<span class="oi-ticker">{_esc(n.ticker)}</span>'
            if n.ticker
            else '<span class="oi-ticker oi-portfolio">PORTFOLIO</span>'
        )
        body.write(
            '<li class="open-item">'
            f'<span class="oi-kind">{_esc(n.kind)}</span>'
            f"{ticker_html}"
            f'<span class="oi-body">{_esc(n.body)}</span>'
            f"{stamp_html(n.created_at, mode='date', css='oi-when')}"
            "</li>"
        )
    body.write("</ul></section>")


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


def _render_upcoming(
    body: StringIO,
    render_date: date,
    db_path: Path | None,
    *,
    user_id: str = DEFAULT_USER_ID,
) -> None:
    """'Upcoming this week' — tracked names whose ESTIMATED next earnings (latest
    release + ~1 quarter) land within the next two weeks. Each name leads with
    the owner's open watch items / questions (P4.4) so earnings prep starts
    from what the owner already said to look for."""
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
            '<span class="up-note">est. next earnings</span>'
        )
        prep_notes = _open_notes(db_path, user_id, ticker=ticker)
        if prep_notes:
            shown = prep_notes[:_PREP_NOTES_PER_TICKER]
            body.write('<ul class="prep-notes">')
            for n in shown:
                body.write(f'<li><span class="oi-kind">{_esc(n.kind)}</span> {_esc(n.body)}</li>')
            overflow = len(prep_notes) - len(shown)
            if overflow > 0:
                body.write(f'<li class="muted">+{overflow} more open item(s)</li>')
            body.write("</ul>")
        body.write("</li>")
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
        body.write(
            '<li class="ledger-entry">'
            '<div class="ledger-meta">'
            f'<span class="ledger-ticker">{_esc(entry.ticker)}</span>'
            f'<span class="ledger-kind">{_esc(kind_label)}</span>'
            f"{stamp_html(entry.created_at, mode='date', css='ledger-when')}"
            "</div>"
            f'<div class="ledger-body">{_esc(entry.body)}</div>'
            "</li>"
        )
    body.write("</ul></section>")


def _render_footer(body: StringIO, render_date: date) -> None:
    generated_at = datetime.now(UTC).replace(tzinfo=None)
    body.write('<div class="l1-footer">')
    body.write(f"<span>Digest · {_esc(fmt_date(render_date.isoformat()))}</span>")
    body.write(stamp_html(generated_at, prefix="generated "))
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
