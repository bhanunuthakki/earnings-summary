"""Upcoming-earnings look-ahead — the compact Home-rail strip.

The one piece of the retired standalone morning digest worth keeping
(2026-06-11): tracked names whose next earnings land within the horizon,
rendered as a compact strip the shell mounts ABOVE the Inbox rail. Real
dates come from the canonical ``expected_earnings`` calendar (0082); names
the calendar has no future row for fall back to the old estimate — latest
``earnings_surprises`` release + one quarter, marked "~"/est. Each row
surfaces the owner's open watch items / questions for that name INLINE as
clickable ``data-ask-q`` doorways (P4.4 made actionable: earnings prep
starts from what the owner already said to look for, and one click opens
Ask scoped to that watch item). This is the link between the upcoming-
earnings lane and the "things to watch out for" the owner recorded — derived
at render time from ``analyst_notes``, with no new data branch of its own.
"""

from __future__ import annotations

import html
import sqlite3
from datetime import date, timedelta
from io import StringIO
from pathlib import Path

from expected_earnings import upcoming_by_ticker
from identity import DEFAULT_USER_ID
from user_state.notes import AnalystNoteRow, list_notes

__all__ = ["UPCOMING_CSS", "render_upcoming_strip", "upcoming_earnings"]

# Real next-earnings dates come from the expected_earnings table (0082) — the
# canonical calendar materialized daily from the FMP-cache → yfinance stack by
# execution/refresh_expected_earnings.py. Tracked names the calendar has no
# future row for fall back to the old estimate: latest known release + one
# quarter (labelled "est."), built from the earnings_surprises history.
_NEXT_EARNINGS_GAP_DAYS = 91
_UPCOMING_HORIZON_DAYS = 14

# Lead kinds for the per-ticker prep doorways (P4.4): the owner's watch items
# and unanswered questions come first.
_OPEN_ITEM_KIND_RANK = {"watch": 0, "question": 1}
_PREP_NOTES_PER_TICKER = 3
# Visible-label cap for an inline watch doorway (the Home rail is narrow); the
# full note text always rides in the button's ``title``.
_WATCH_LABEL_MAX = 64


def _tracked_tickers(conn: sqlite3.Connection) -> set[str]:
    """Non-archived portfolio + evaluation tickers ([] on a partial schema)."""
    try:
        rows = conn.execute(
            "SELECT ticker FROM tracked_companies WHERE archived_at IS NULL "
            "AND list_type IN ('portfolio', 'evaluation')"
        ).fetchall()
    except sqlite3.Error:
        return set()
    return {str(t) for (t,) in rows}


def _last_release_by_ticker(conn: sqlite3.Connection) -> list[tuple[str, object]]:
    """Latest earnings_surprises release per tracked name, for the est. path."""
    try:
        return conn.execute(
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


def upcoming_earnings(
    db_path: Path | None, today: date, *, horizon_days: int = _UPCOMING_HORIZON_DAYS
) -> list[tuple[str, date, bool]]:
    """``(ticker, date, is_estimate)`` within the horizon for tracked names.

    Calendar rows win: a ticker with ANY future ``expected_earnings`` row is
    calendar-owned — it appears iff that real date lands in the horizon, and
    is never re-estimated. Only tickers without a future calendar row fall
    back to the +91d estimate. Best-effort: a missing DB / table degrades to
    the fallback (or ``[]``) rather than raising. Read-only.
    """
    if db_path is None or not Path(db_path).exists():
        return []
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.Error:
        return []
    try:
        tracked = _tracked_tickers(conn)
        calendar = {t: d for t, d in upcoming_by_ticker(conn, today).items() if t in tracked}
        last_releases = _last_release_by_ticker(conn)
    finally:
        conn.close()
    horizon_end = today + timedelta(days=horizon_days)
    out: list[tuple[str, date, bool]] = []
    for ticker, nxt in calendar.items():
        if today <= nxt <= horizon_end:
            out.append((ticker, nxt, False))
    for ticker, last_release in last_releases:
        if str(ticker) in calendar:
            continue
        try:
            last = date.fromisoformat(str(last_release)[:10])
        except (ValueError, TypeError):
            continue
        est = last + timedelta(days=_NEXT_EARNINGS_GAP_DAYS)
        if today <= est <= horizon_end:
            out.append((str(ticker), est, True))
    out.sort(key=lambda t: (t[1], t[0]))
    return out


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


def _watch_doorways(db_path: Path | None, user_id: str, ticker: str) -> str:
    """Inline, clickable "things to watch out for" for one upcoming name.

    The owner's open watch items / questions (lead-kinds first, capped) render
    as Law-2 doorways: each is a ``data-ask-q`` button the shell's global
    delegate (``goAsk``) opens in Ask, scoped to the name — so the row goes from
    "reports soon" to "here's what I said to watch, click to dig in" in one move.
    Returns "" when the name has no open notes (hide-don't-stub — the row still
    renders its ticker + date)."""
    notes = _open_notes(db_path, user_id, ticker=ticker)
    if not notes:
        return ""
    out = StringIO()
    out.write('<ul class="up-watch">')
    for n in notes[:_PREP_NOTES_PER_TICKER]:
        body = n.body.strip()
        label = body if len(body) <= _WATCH_LABEL_MAX else body[: _WATCH_LABEL_MAX - 1] + "…"
        ask_q = f"{body} ({ticker})"
        full = f"{n.kind}: {body}"
        out.write(
            '<li><button type="button" class="up-watch-item" '
            f'data-ask-q="{_esc(ask_q)}" title="{_esc(full)}">'
            f'<span class="up-watch-kind">{_esc(n.kind)}</span>'
            f'<span class="up-watch-body">{_esc(label)}</span>'
            "</button></li>"
        )
    overflow = len(notes) - _PREP_NOTES_PER_TICKER
    if overflow > 0:
        out.write(f'<li class="up-watch-more muted">+{overflow} more open item(s)</li>')
    out.write("</ul>")
    return out.getvalue()


def render_upcoming_strip(
    db_path: Path | None,
    today: date,
    *,
    user_id: str = DEFAULT_USER_ID,
    horizon_days: int = _UPCOMING_HORIZON_DAYS,
) -> str:
    """The compact "Upcoming earnings" strip for the Home rail: one row per
    tracked name reporting within the horizon — ticker (shell hover mini-card
    via ``data-peek-ticker``), date (``~``-prefixed + "est." chip on the
    fallback path), and the owner's open watch items / questions rendered INLINE
    beneath as clickable ``data-ask-q`` doorways. Returns ``""`` when nothing is
    upcoming (a quiet strip renders as no strip at all)."""
    upcoming = upcoming_earnings(db_path, today, horizon_days=horizon_days)
    if not upcoming:
        return ""
    out = StringIO()
    out.write('<div class="up-strip">')
    out.write(
        '<div class="up-strip-head">Upcoming earnings'
        f'<span class="up-strip-sub">next {horizon_days}d</span></div>'
    )
    out.write('<ul class="up-strip-list">')
    for ticker, when, is_estimate in upcoming:
        base = "est. next earnings" if is_estimate else "next earnings"
        date_txt = f"~{when.isoformat()}" if is_estimate else when.isoformat()
        out.write("<li>")
        out.write(f'<div class="up-row" title="{_esc(base)}">')
        out.write(
            f'<span class="up-ticker" data-peek-ticker="{_esc(ticker)}">{_esc(ticker)}</span>'
        )
        if is_estimate:
            out.write('<span class="up-est">est.</span>')
        out.write(f'<span class="up-date">{_esc(date_txt)}</span>')
        out.write("</div>")
        out.write(_watch_doorways(db_path, user_id, ticker))
        out.write("</li>")
    out.write("</ul></div>")
    return out.getvalue()


def _esc(text: str) -> str:
    return html.escape(str(text), quote=True)


UPCOMING_CSS = """
/* "Upcoming earnings" — the compact Home-rail strip above the Inbox. */
.up-strip { background: var(--surface); border-radius: var(--radius);
  padding: 9px 12px; margin-bottom: var(--sp-2); }
.up-strip-head { display: flex; justify-content: space-between; align-items: baseline;
  color: var(--muted); font-size: var(--fs-micro); font-weight: 600;
  text-transform: uppercase; letter-spacing: 0.06em; margin-bottom: 3px; }
.up-strip-sub { font-weight: 400; letter-spacing: 0; text-transform: none;
  font-family: var(--mono, monospace); }
.up-strip-list { list-style: none; margin: 0; padding: 0; }
.up-strip-list li { padding: 3px 0; font-size: var(--fs-caption); }
.up-row { display: flex; align-items: baseline; gap: 8px; }
.up-ticker { font-family: var(--mono, monospace); font-weight: 600;
  color: var(--fg); }
.up-est { color: var(--muted); font-size: var(--fs-micro); font-weight: 600;
  text-transform: uppercase; letter-spacing: 0.05em; }
.up-date { margin-left: auto; font-family: var(--mono, monospace);
  color: var(--muted); white-space: nowrap; }
/* "Things to watch out for" — the owner's open notes for the name, inline as
   clickable Ask doorways (the actionable link off the earnings lane). */
.up-watch { list-style: none; margin: 3px 0 0; padding: 0 0 0 2px;
  display: flex; flex-direction: column; gap: 1px; }
.up-watch-item { display: flex; align-items: baseline; gap: 6px; width: 100%;
  text-align: left; background: transparent; border: 0; padding: 1px 0;
  cursor: pointer; color: var(--muted); font: inherit; font-size: var(--fs-micro);
  min-width: 0; }
.up-watch-item:hover { color: var(--accent); }
.up-watch-item:hover .up-watch-body { text-decoration: underline; }
.up-watch-kind { flex: none; color: var(--muted); font-size: var(--fs-micro);
  text-transform: uppercase; letter-spacing: 0.05em;
  border: 1px solid var(--hairline); border-radius: var(--radius); padding: 0 3px; }
.up-watch-body { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.up-watch-more { font-size: var(--fs-micro); color: var(--muted); padding-left: 2px; }
""".strip()
