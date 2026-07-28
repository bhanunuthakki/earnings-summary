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
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite
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


# Priority tiers for the strip (Wave 2, surface_density_jit_redesign.md
# application map #2): the owner's book first, then names he is ACTIVELY
# valuing, then the rest of the evaluation list. "Active" is DERIVED — an
# unexpired research hot-flag, or owner activity (a decision / an authored
# note) inside the window — never a new flag to maintain.
_TIER_ORDER: tuple[str, ...] = ("portfolio", "active", "evaluation")
_TIER_LABELS: dict[str, str] = {
    "portfolio": "Portfolio",
    "active": "Active valuation",
    "evaluation": "Evaluation",
}
_ACTIVE_WINDOW_DAYS = 30


def _tier_by_ticker(conn: sqlite3.Connection, tickers: set[str], today: date) -> dict[str, str]:
    """Resolve each upcoming name to its strip tier. Best-effort per source —
    a missing table just can't promote anyone."""
    if not tickers:
        return {}
    marks = ",".join("?" for _ in tickers)
    args = tuple(sorted(tickers))
    tiers: dict[str, str] = dict.fromkeys(args, "evaluation")
    try:
        for t, lt in conn.execute(
            f"SELECT ticker, list_type FROM tracked_companies "
            f"WHERE archived_at IS NULL AND ticker IN ({marks})",
            args,
        ):
            if lt == "portfolio":
                tiers[str(t)] = "portfolio"
    except sqlite3.Error:
        pass
    cutoff = (today - timedelta(days=_ACTIVE_WINDOW_DAYS)).isoformat()
    active_probes = (
        (
            "SELECT DISTINCT ticker FROM research_hot_flags "
            f"WHERE ticker IN ({marks}) AND (expires_at IS NULL OR expires_at >= ?)",
            (*args, today.isoformat()),
        ),
        (
            f"SELECT DISTINCT ticker FROM decisions WHERE ticker IN ({marks}) AND made_at >= ?",
            (*args, cutoff),
        ),
        (
            f"SELECT DISTINCT ticker FROM analyst_notes "
            f"WHERE ticker IN ({marks}) AND created_at >= ?",
            (*args, cutoff),
        ),
    )
    for sql, params in active_probes:
        try:
            for (t,) in conn.execute(sql, params):
                if tiers.get(str(t)) == "evaluation":
                    tiers[str(t)] = "active"
        except sqlite3.Error:
            continue
    return tiers


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
        conn = connect_sqlite(db_path, role=SQLiteConnectionRole.READ_ONLY)
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

    Wave 2 (walkthrough #2): the doorways sit IN the row, in the horizontal
    space right of the ticker — not stacked beneath it. Each open watch item /
    question (lead-kinds first, capped) is a ``data-ask-q`` button the shell's
    global delegate (``goAsk``) opens in Ask scoped to the name; overflow
    collapses to a "+n" count. Returns "" when the name has no open notes
    (hide-don't-stub — the row still renders ticker + prep chip + date)."""
    notes = _open_notes(db_path, user_id, ticker=ticker)
    if not notes:
        return ""
    out = StringIO()
    for n in notes[:_PREP_NOTES_PER_TICKER]:
        body = n.body.strip()
        label = body if len(body) <= _WATCH_LABEL_MAX else body[: _WATCH_LABEL_MAX - 1] + "…"
        ask_q = f"{body} ({ticker})"
        full = f"{n.kind}: {body}"
        out.write(
            '<button type="button" class="up-watch-item" '
            f'data-ask-q="{_esc(ask_q)}" title="{_esc(full)}">'
            f'<span class="up-watch-kind k-chip">{_esc(n.kind)}</span>'
            f'<span class="up-watch-body">{_esc(label)}</span>'
            "</button>"
        )
    overflow = len(notes) - _PREP_NOTES_PER_TICKER
    if overflow > 0:
        out.write(f'<span class="up-watch-more muted">+{overflow}</span>')
    return out.getvalue()


def _prep_chip(ticker: str) -> str:
    """The on-demand earnings-prep memo doorway (walkthrough #4: "a chip that
    happens to be created on demand", never a pre-generated artifact). Opens
    the peek drawer on ``/api/peek/earnings-prep`` — assembled at click time
    from what the platform already knows about the name."""
    return (
        '<button type="button" class="up-prep k-chip k-chip-btn" '
        f'data-peek-url="/api/peek/earnings-prep?ticker={_esc(ticker)}" '
        f'data-peek-title="Earnings prep — {_esc(ticker)}" '
        'title="One-page earnings prep, assembled on demand">prep</button>'
    )


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

    # Tier resolution (Wave 2): portfolio > active valuation (derived) > rest.
    tiers: dict[str, str] = {}
    if db_path is not None and Path(db_path).exists():
        try:
            conn = connect_sqlite(db_path, role=SQLiteConnectionRole.READ_ONLY)
        except sqlite3.Error:
            conn = None
        if conn is not None:
            try:
                tiers = _tier_by_ticker(conn, {t for t, _, _ in upcoming}, today)
            finally:
                conn.close()

    out = StringIO()
    # data-up-summary: the one-line informative summary the shell hoists into
    # its collapsed <details> header ("Upcoming earnings · N in 14d — next TICK
    # MM-DD") so the closed section still answers "anything soon?" at a glance.
    # Rides the strip root as a data attribute so the builder seam stays a
    # single HTML string (command_center_shell reads it back; wave B B2).
    next_ticker, next_when, _ = upcoming[0]
    summary = (
        f"Upcoming earnings · {len(upcoming)} in {horizon_days}d"
        f" — next {next_ticker} {next_when.strftime('%m-%d')}"
    )
    out.write(f'<div class="up-strip" data-up-summary="{_esc(summary)}">')
    out.write(
        '<div class="up-strip-head">Upcoming earnings'
        f'<span class="up-strip-sub">next {horizon_days}d</span></div>'
    )
    out.write('<ul class="up-strip-list">')
    for tier in _TIER_ORDER:
        tier_rows = [
            (t, when, est) for t, when, est in upcoming if tiers.get(t, "evaluation") == tier
        ]
        if not tier_rows:
            continue
        out.write(
            f'<li class="up-tier"><span class="up-tier-label">{_TIER_LABELS[tier]}'
            f"</span><span class='up-tier-n'>{len(tier_rows)}</span></li>"
        )
        for ticker, when, is_estimate in tier_rows:
            base = "est. next earnings" if is_estimate else "next earnings"
            date_txt = f"~{when.isoformat()}" if is_estimate else when.isoformat()
            out.write("<li>")
            out.write(f'<div class="up-row" title="{_esc(base)}">')
            out.write(
                f'<span class="up-ticker" data-peek-ticker="{_esc(ticker)}">{_esc(ticker)}</span>'
            )
            if is_estimate:
                out.write('<span class="up-est">est.</span>')
            # Walkthrough #2: the chips live IN the horizontal space right of
            # the ticker — prep doorway first, then the owner's open items.
            out.write('<span class="up-chips">')
            out.write(_prep_chip(ticker))
            out.write(_watch_doorways(db_path, user_id, ticker))
            out.write("</span>")
            out.write(f'<span class="up-date">{_esc(date_txt)}</span>')
            out.write("</div>")
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
  color: var(--muted); font-size: var(--fs-caption); font-weight: 600;
  text-transform: uppercase; letter-spacing: 0.06em; margin-bottom: 3px; }
.up-strip-sub { font-weight: 400; letter-spacing: 0; text-transform: none;
  font-family: var(--mono, monospace); }
.up-strip-list { list-style: none; margin: 0; padding: 0; }
.up-strip-list li { padding: 3px 0; font-size: var(--fs-caption); }
/* Tier bands (Wave 2): Portfolio > Active valuation > Evaluation. */
.up-tier { display: flex; align-items: baseline; gap: 6px; padding-top: 7px; }
.up-tier-label { color: var(--muted); font-size: var(--fs-caption); font-weight: 600;
  text-transform: uppercase; letter-spacing: 0.06em; }
.up-tier-n { color: var(--muted); font-family: var(--mono, monospace);
  font-size: var(--fs-caption); }
.up-row { display: flex; align-items: baseline; gap: 8px; min-width: 0; }
.up-ticker { font-family: var(--mono, monospace); font-weight: 600;
  color: var(--fg); flex: none; }
.up-est { color: var(--muted); font-size: var(--fs-caption); font-weight: 600;
  text-transform: uppercase; letter-spacing: 0.05em; flex: none; }
.up-date { margin-left: auto; font-family: var(--mono, monospace);
  color: var(--muted); white-space: nowrap; flex: none; }
/* The row's chip lane (Wave 2, walkthrough #2): prep doorway + the owner's
   open watch items, IN the horizontal space right of the ticker. Overflow
   ellipsizes inside the lane; the date never wraps. */
.up-chips { display: flex; align-items: baseline; gap: 6px; min-width: 0;
  overflow: hidden; flex: 1 1 auto; }
.up-prep { flex: none; }
.up-watch-item { display: inline-flex; align-items: baseline; gap: 5px;
  background: transparent; border: 0; padding: 0; cursor: pointer;
  color: var(--muted); font: inherit; font-size: var(--fs-caption); min-width: 0;
  overflow: hidden; }
.up-watch-item:hover { color: var(--accent); }
.up-watch-item:hover .up-watch-body { text-decoration: underline; }
.up-watch-kind { flex: none; }
.up-watch-body { overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  max-width: 200px; }
.up-watch-more { font-size: var(--fs-caption); color: var(--muted); flex: none; }
""".strip()
