"""Peek fragments (UX9) — the small HTML payloads behind the shell's quick-look.

The command-center shell's peek primitive (``command_center_shell.SHELL_JS``)
fetches one of these head/foot-less fragments and injects it into a positioned
popover, so reviewing an alert, reading a source excerpt, or glancing at a
ticker no longer means navigating away from the panel you were on:

* :func:`render_alert_peek` / :func:`render_alerts_list_peek` — full alert
  card(s) (evidence drawer, queued actions with their live approve/dismiss
  links) for the inbox "review →" links and the cockpit's pending-alert pills.
* :func:`render_ticker_peek` — the hover mini-card for ticker links: price +
  day move, thesis verdict, DCF gap, next ER, unreviewed count, and the
  open-the-holding link. Reuses the cockpit's own per-ticker readers so the
  card can never disagree with the cockpit row it annotates.
* :func:`render_memo_peek` — the latest advisor memo of a kind, for the
  portfolio insights "full memo →" link.

Source-document peeks are NOT here — ``/source/<doc_id>?fragment=1`` serves
those straight from ``pipeline.source_viewers``.

All builders are read-only and degrade on missing tables (``None`` → the
route 404s; the peek shows its failed-to-load empty state).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from datetime import UTC, datetime
from html import escape
from io import StringIO
from pathlib import Path

from alerts import AlertRow, get_alert, list_alerts, list_queued_actions_for_alert
from dashboard._card import render_alert_card
from dashboard.evidence_drawer import load_brief_provenance
from identity import DEFAULT_USER_ID
from pipeline.analytical_dashboard_html import light_markdown_to_html
from pipeline.research_cockpit import latest_dcf_runs, next_earnings, profile_quote
from report.renderers.numfmt import fmt_date, fmt_pct
from ui.time import stamp_html

__all__ = [
    "render_alert_peek",
    "render_alerts_list_peek",
    "render_memo_peek",
    "render_ticker_peek",
]

# Same worst-wins tone vocabulary the cockpit's verdict badge uses.
_STATUS_TONE: dict[str, str] = {
    "breach": "bad",
    "broken": "bad",
    "warn": "warn",
    "watch": "warn",
    "ok": "ok",
    "intact": "ok",
}

# Mirrors ck_advisor_memos_kind (alembic 0077).
_MEMO_KINDS = frozenset({"next_dollar", "swap_check", "socratic"})


# ----------------------------------------------------------------------------
# Alert peeks
# ----------------------------------------------------------------------------


def render_alert_peek(db_path: Path, alert_id: int) -> str | None:
    """One full alert card — evidence drawer open, queued actions with their
    approve/dismiss links — for the inbox cards' "review →" peek. None when
    no such alert exists (the route 404s)."""
    try:
        alert = get_alert(alert_id, db_path=db_path)
    except (LookupError, sqlite3.Error):
        return None
    return _alert_cards_html([alert], db_path)


def render_alerts_list_peek(
    db_path: Path,
    *,
    user_id: str = DEFAULT_USER_ID,
    ticker: str | None = None,
    status: str | None = None,
    limit: int = 8,
) -> str:
    """Up to ``limit`` full alert cards (drawers collapsed — it's a scan list)
    for the cockpit's pending-alert pills, with the full feed as the overflow
    path. Always renders — an empty slice is a valid answer."""
    try:
        alerts = list_alerts(
            user_id=user_id, ticker=ticker, status=status, limit=limit, db_path=db_path
        )
    except sqlite3.Error:
        alerts = []
    if not alerts:
        return '<div class="cc-empty">No matching alerts.</div>'
    params = [("ticker", ticker), ("status", status)]
    qs = "&".join(f"{k}={escape(v)}" for k, v in params if v)
    feed_href = f"/feed?{qs}" if qs else "/feed"
    footer = f'<div class="cc-peek-foot"><a href="{feed_href}">open in the full feed →</a></div>'
    return _alert_cards_html(alerts, db_path) + footer


def _alert_cards_html(alerts: list[AlertRow], db_path: Path) -> str:
    """Shared card-list body: per-ticker brief provenance looked up once, the
    drawer expanded only when a single alert is being reviewed."""
    out = StringIO()
    out.write('<div class="cc-peek-alerts">')
    prov_cache: dict[str, Mapping[str, object] | None] = {}
    for alert in alerts:
        t = alert.ticker.upper()
        if t not in prov_cache:
            prov_cache[t] = load_brief_provenance(t, db_path=db_path)
        try:
            actions = list_queued_actions_for_alert(alert.id, db_path=db_path)
        except sqlite3.Error:
            actions = []
        render_alert_card(
            out,
            alert,
            actions=actions,
            show_status_badge=True,
            brief_provenance=prov_cache[t],
            drawer_open=len(alerts) == 1,
        )
    out.write("</div>")
    return out.getvalue()


# ----------------------------------------------------------------------------
# Ticker hover mini-card
# ----------------------------------------------------------------------------


def render_ticker_peek(
    conn: sqlite3.Connection,
    repo_root: Path,
    ticker: str,
    *,
    now: datetime | None = None,
) -> str | None:
    """The hover mini-card for a tracked ticker; None when the ticker isn't
    tracked (the route 404s and the hover card simply doesn't show).

    Reuses the cockpit's per-ticker readers (`profile_quote`,
    `next_earnings`, `latest_dcf_runs`) so the card and the cockpit row
    can't drift apart. Rows render hide-don't-stub: a missing price/DCF/ER
    drops its row rather than showing an em-dash pile.
    """
    t = ticker.strip().upper()
    if not t:
        return None
    try:
        row = conn.execute(
            "SELECT name FROM tracked_companies WHERE UPPER(ticker) = ? AND archived_at IS NULL",
            (t,),
        ).fetchone()
    except sqlite3.Error:
        return None
    if row is None:
        return None
    name = str(row[0]) if row[0] else None

    ref = now or datetime.now(UTC)
    price, day_move, _asof = profile_quote(repo_root, t)
    verdict = _latest_overall_status(conn, t)
    next_er = next_earnings(repo_root, t, ref)
    fv_gap = latest_dcf_runs(conn).get(t, (None, None, None, None))[0]
    pending = _pending_alert_count(conn, t)

    badge = ""
    if verdict:
        tone = _STATUS_TONE.get(verdict.lower(), "muted")
        badge = f'<span class="cockpit-badge b-{tone}">{escape(verdict)}</span>'
    head = f'<div class="cc-mini-head"><span class="cc-mini-ticker">{escape(t)}</span>{badge}</div>'
    name_html = f'<div class="cc-mini-name">{escape(name)}</div>' if name else ""

    rows: list[tuple[str, str]] = []
    if price is not None:
        move = ""
        if day_move is not None:
            mtone = "pos" if day_move >= 0 else "neg"
            move = f' <span class="{mtone}">{escape(fmt_pct(day_move, signed=True))}</span>'
        rows.append(("Price", f"${price:,.2f}{move}"))
    if fv_gap is not None:
        # > 0 — price above fair value (rich); < 0 — below (cheap).
        gtone = "neg" if fv_gap > 0 else "pos"
        rows.append(
            ("vs DCF FV", f'<span class="{gtone}">{escape(fmt_pct(fv_gap, signed=True))}</span>')
        )
    if next_er:
        days = (datetime.fromisoformat(next_er).date() - ref.date()).days
        rel = "today" if days == 0 else f"in {days}d"
        rows.append(("Next ER", f"{escape(fmt_date(next_er, include_year=False))} · {escape(rel)}"))
    if pending:
        rows.append(("Unreviewed", f"{pending} alert{'s' if pending != 1 else ''}"))
    rows_html = "".join(
        f'<div class="cc-mini-row"><span>{label}</span><b>{value}</b></div>'
        for label, value in rows
    )
    return (
        f'<div class="cc-mini">{head}{name_html}{rows_html}'
        f'<div class="cc-mini-open"><a href="/#holding={escape(t)}">open holding →</a></div>'
        "</div>"
    )


def _latest_overall_status(conn: sqlite3.Connection, ticker: str) -> str | None:
    try:
        row = conn.execute(
            "SELECT overall_status FROM thesis_evaluations "
            "WHERE ticker = ? ORDER BY evaluated_at DESC LIMIT 1",
            (ticker,),
        ).fetchone()
    except sqlite3.Error:
        return None
    if row is None or row[0] is None:
        return None
    return str(row[0])


def _pending_alert_count(conn: sqlite3.Connection, ticker: str) -> int:
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM alerts WHERE status = 'pending' AND ticker = ?",
            (ticker,),
        ).fetchone()
    except sqlite3.Error:
        return 0
    return int(row[0]) if row else 0


# ----------------------------------------------------------------------------
# Advisor memo peek
# ----------------------------------------------------------------------------


def render_memo_peek(db_path: Path, kind: str) -> str | None:
    """The latest advisor memo of ``kind``, markdown-rendered, for the
    portfolio insights' "full memo →" peek. None on an unknown kind or when
    no memo of that kind exists yet."""
    if kind not in _MEMO_KINDS or not db_path.exists():
        return None
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.Error:
        return None
    try:
        row = conn.execute(
            "SELECT title, body_md, created_at FROM advisor_memos "
            "WHERE kind = ? ORDER BY created_at DESC LIMIT 1",
            (kind,),
        ).fetchone()
    except sqlite3.Error:
        return None
    finally:
        conn.close()
    if row is None:
        return None
    title, body_md, created_at = str(row[0]), str(row[1]), str(row[2])
    return (
        '<div class="cc-peek-memo">'
        f'<div class="cc-peek-memo-head"><h2>{escape(title)}</h2>'
        f"{stamp_html(created_at, mode='date')}</div>"
        f'<div class="synthesis-body">{light_markdown_to_html(body_md[:20000])}</div>'
        "</div>"
    )
