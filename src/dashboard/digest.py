"""Daily morning HTML digest renderer.

UX redesign PR3: the digest is the *since-yesterday view of the unified
inbox* (dashboard/inbox.py). One self-contained HTML page per day with three
sections:

  1. Header                  — date + "what changed since yesterday" label
  2. What changed (last 24h) — ONE deduped stream: alerts (any status),
                                queued-action drafts, thesis-ledger entries,
                                and journal items, newest first. Replaces the
                                old What's-new / Open-items / Outstanding /
                                Recent-thesis-changes quartet that showed the
                                same queued action up to three different ways.
  3. Upcoming this week      — estimated next-earnings for tracked names
                                (labelled "est." — earnings_surprises holds
                                only past releases), each led by the owner's
                                open watch items for that name.

Empty-state path: section 2 collapses to one "nothing changed" line rather
than emitting empty divs, so the morning open of a quiet day still looks
intentional.
"""

from __future__ import annotations

import html
import sqlite3
from datetime import UTC, date, datetime, time, timedelta
from io import StringIO
from pathlib import Path

from dashboard._styles import CSS
from dashboard.inbox import INBOX_CSS, INBOX_JS, collect_inbox, render_inbox_stream
from identity import DEFAULT_USER_ID
from report.renderers.numfmt import fmt_date
from ui.time import stamp_html
from ui.tokens import FAVICON_LINK
from user_state.notes import AnalystNoteRow, list_notes


def render_morning_digest(
    date: date,
    user_id: str = DEFAULT_USER_ID,
    db_path: Path | None = None,
) -> str:
    """Return the full HTML string for the morning digest of ``date``.

    UX redesign PR3: the digest is the *since-yesterday view of the unified
    inbox* — ONE deduped "What changed" stream (alerts of any status, drafts,
    thesis-ledger entries, journal items, newest first) instead of the old
    four sections that showed the same queued action up to three ways —
    followed by the estimated-earnings look-ahead.

    The 24h window is ``[date - 1d, date + 1d)`` in UTC — wide enough that
    the morning of ``date`` always picks up "yesterday after-close" prints
    regardless of the user's local timezone; the upper bound keeps a digest
    re-generated for a historical date honest.
    """
    window_start, window_end = _window_for_date(date)
    items = collect_inbox(db_path, user_id=user_id, since=window_start, until=window_end, limit=60)

    body = StringIO()
    body.write('<div class="l1-shell">')
    _render_header(body, date)
    body.write('<section class="dash-section dash-whats-new">')
    body.write('<div class="dash-section-header">')
    body.write('<div class="dash-section-title">What changed (last 24h)</div>')
    body.write(f'<div class="dash-section-count">{len(items)} item(s)</div>')
    body.write("</div>")
    # Bucket labels (Today / Yesterday) are relative to the digest's own day,
    # not the +1d window bound — an item from the render date reads "Today".
    body.write(
        render_inbox_stream(
            items,
            db_path=db_path,
            now=datetime.combine(date, time.max),
            surface="digest",
            empty_text=(
                "Nothing changed in the last 24h — no alerts, drafts, thesis "
                "edits, or new watch items. ✓ Your portfolio looks quiet."
            ),
        )
    )
    body.write("</section>")
    _render_upcoming(body, date, db_path, user_id=user_id)
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
        "Since yesterday, in one stream — alerts, drafts awaiting review, "
        "thesis changes, and your open watch items, deduped."
        "</div>"
    )
    body.write("</header>")


# Lead kinds for the per-ticker earnings-prep lists
# (P4.4): the owner's watch items and unanswered questions come first.
_OPEN_ITEM_KIND_RANK = {"watch": 0, "question": 1}
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
<style>{CSS}
{INBOX_CSS}</style>
</head>
<body>
{body}
<script>{INBOX_JS}</script>
</body>
</html>
"""


def _esc(text: str) -> str:
    return html.escape(text, quote=True)
