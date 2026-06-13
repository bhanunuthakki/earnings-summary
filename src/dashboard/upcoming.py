"""Upcoming-earnings look-ahead — the compact Home-rail strip.

The one piece of the retired standalone morning digest worth keeping
(2026-06-11): tracked names whose next earnings land within the horizon,
rendered as a compact strip the shell mounts ABOVE the Inbox rail. Real
dates come from the canonical ``expected_earnings`` calendar (0082); names
the calendar has no future row for fall back to the old estimate — latest
``earnings_surprises`` release + one quarter, marked "~"/est. Each row
carries the owner's open watch items / questions for that name as a hover
tooltip (P4.4: earnings prep starts from what the owner already said to
look for).
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

# Lead kinds for the per-ticker prep tooltips (P4.4): the owner's watch items
# and unanswered questions come first.
_OPEN_ITEM_KIND_RANK = {"watch": 0, "question": 1}
_PREP_NOTES_PER_TICKER = 3


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


def _prep_tooltip(db_path: Path | None, user_id: str, ticker: str, base: str) -> str:
    """The row's title text: what the date is, then the owner's open prep
    items for the name (capped) — the compact carrier of the old digest's
    per-ticker prep-notes lists."""
    notes = _open_notes(db_path, user_id, ticker=ticker)
    if not notes:
        return base
    shown = [f"{n.kind}: {n.body}" for n in notes[:_PREP_NOTES_PER_TICKER]]
    overflow = len(notes) - _PREP_NOTES_PER_TICKER
    if overflow > 0:
        shown.append(f"+{overflow} more open item(s)")
    return base + " — " + " · ".join(shown)


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
    fallback path), open prep items in the row tooltip. Returns ``""`` when
    nothing is upcoming (a quiet strip renders as no strip at all)."""
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
        tooltip = _prep_tooltip(db_path, user_id, ticker, base)
        date_txt = f"~{when.isoformat()}" if is_estimate else when.isoformat()
        out.write(f'<li title="{_esc(tooltip)}">')
        out.write(
            f'<span class="up-ticker" data-peek-ticker="{_esc(ticker)}">{_esc(ticker)}</span>'
        )
        if is_estimate:
            out.write('<span class="up-est">est.</span>')
        out.write(f'<span class="up-date">{_esc(date_txt)}</span>')
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
.up-strip-list li { display: flex; align-items: baseline; gap: 8px; padding: 2px 0;
  font-size: var(--fs-caption); cursor: help; }
.up-ticker { font-family: var(--mono, monospace); font-weight: 700;
  color: var(--fg); }
.up-est { color: var(--muted); font-size: var(--fs-micro); font-weight: 600;
  text-transform: uppercase; letter-spacing: 0.05em; }
.up-date { margin-left: auto; font-family: var(--mono, monospace);
  color: var(--muted); white-space: nowrap; }
""".strip()
