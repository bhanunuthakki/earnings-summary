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
* :func:`render_provenance_peek` — per-source data freshness (brief build,
  FMP pulls, IR docs, news, the earnings calendar) with inline refresh
  buttons that POST the existing ``/actions/*`` endpoints and stream the job
  log into the peek — the click-through behind the freshness dots and the
  Home tier strip (UX9d).

Source-document peeks are NOT here — ``/source/<doc_id>?fragment=1`` serves
those straight from ``pipeline.source_viewers``.

All builders are read-only and degrade on missing tables (``None`` → the
route 404s; the peek shows its failed-to-load empty state). The provenance
peek goes further: it always renders, with missing sources as em-dash ages.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from datetime import UTC, datetime
from html import escape
from io import StringIO
from pathlib import Path
from typing import NamedTuple

from alerts import AlertRow, get_alert, list_alerts, list_queued_actions_for_alert
from dashboard._card import render_alert_card
from dashboard.evidence_drawer import load_brief_provenance
from identity import DEFAULT_USER_ID
from pipeline.analytical_dashboard_html import light_markdown_to_html
from pipeline.research_cockpit import latest_dcf_runs, next_earnings, profile_quote
from report.renderers.numfmt import fmt_date, fmt_pct, fmt_reltime
from ui.time import stamp_html

__all__ = [
    "render_alert_peek",
    "render_alerts_list_peek",
    "render_memo_peek",
    "render_provenance_peek",
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


# ----------------------------------------------------------------------------
# Data-provenance peek (UX9d) — freshness rows + in-place refresh
# ----------------------------------------------------------------------------


class _ProvAction(NamedTuple):
    """One inline refresh control: POSTs an EXISTING /actions/* endpoint (the
    same job kinds the System actions console starts — no new job types)."""

    post_url: str
    body: dict[str, object]
    label: str
    title: str


class _ProvRow(NamedTuple):
    label: str
    stamp: str | None  # ISO timestamp rendered relative (ui.time rules)
    prefix: str  # rides inside the stamp span ("oldest ")
    note: str | None  # plain-text aside ("12 endpoints · oldest 3w ago")
    action: _ProvAction | None
    cron_hint: str | None  # no on-demand action — name the cron that owns it


def render_provenance_peek(db_path: Path, ticker: str | None) -> str:
    """Per-source data freshness with in-place refresh — the click-through
    behind the freshness dots (cockpit rows, the holding-header dot) and the
    Home tier strip.

    ``ticker`` scopes to one holding; ``None`` renders portfolio-wide
    aggregates. Each row is one provenance source — report brief, FMP
    endpoint pulls, IR documents, news, the earnings calendar — with a
    relative age (exact UTC in the tooltip) and, where a registered
    ``/actions/*`` endpoint can refresh that source, a button that streams
    the job log into the peek over the standard ``/actions/stream/<job_id>``
    SSE channel (button disabled while its job runs). Sources with no
    on-demand action show which cron owns them instead. The System →
    Actions console stays the deep path (the peek footer links it).

    Always renders: a missing DB / table / column / row degrades that row to
    an em-dash age, never an error.
    """
    t = (ticker or "").strip().upper() or None
    conn: sqlite3.Connection | None = None
    if db_path.exists():
        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        except sqlite3.Error:
            conn = None
    try:
        rows = _prov_ticker_rows(conn, t) if t else _prov_portfolio_rows(conn)
    finally:
        if conn is not None:
            conn.close()
    scope = escape(t or "portfolio", quote=True)
    return (
        f'<div class="cc-prov" data-prov-scope="{scope}">'
        f'<div class="cc-prov-rows">{"".join(_prov_row_html(r) for r in rows)}</div>'
        '<pre class="cc-prov-log" hidden></pre>'
        '<div class="cc-peek-foot"><a href="/#actions">open the Actions console →</a></div>'
        f"<style>{_PROV_CSS}</style><script>{_PROV_JS}</script>"
        "</div>"
    )


def _prov_ticker_rows(conn: sqlite3.Connection | None, t: str) -> list[_ProvRow]:
    rows: list[_ProvRow] = []

    r = _prov_first(
        conn,
        "SELECT last_built_at FROM tracked_companies "
        "WHERE UPPER(ticker) = ? AND archived_at IS NULL",
        (t,),
    )
    rows.append(
        _ProvRow(
            "Report brief",
            _ts(r, 0),
            "",
            None,
            _ProvAction(
                "/actions/refresh",
                {"ticker": t, "mode": "stale"},
                "Refresh",
                "Run the stale refresh chain (FMP → transcripts → IR docs → KPIs → report rebuild)",
            ),
            None,
        )
    )

    r = _prov_first(
        conn,
        "SELECT MAX(last_pulled), MIN(last_pulled), COUNT(*) "
        "FROM fmp_endpoint_status WHERE UPPER(ticker) = ?",
        (t,),
    )
    newest, oldest, n = _ts(r, 0), _ts(r, 1), _count(r, 2)
    note = None
    if n:
        note = f"{n} endpoint{'s' if n != 1 else ''}"
        if oldest and oldest != newest:
            note += f" · oldest {fmt_reltime(oldest)}"
    rows.append(
        _ProvRow(
            "FMP endpoints",
            newest,
            "",
            note,
            _ProvAction(
                "/actions/refresh",
                {"ticker": t, "mode": "stale", "steps": ["fmp"]},
                "Refresh",
                "Re-pull this ticker's FMP endpoints (cadence-aware — fresh ones are skipped)",
            ),
            None,
        )
    )

    r = _prov_first(
        conn,
        "SELECT MAX(fetched_at), COUNT(*) FROM documents "
        "WHERE UPPER(ticker) = ? AND doc_type LIKE 'ir_%'",
        (t,),
    )
    n = _count(r, 1)
    rows.append(
        _ProvRow(
            "IR documents",
            _ts(r, 0),
            "",
            f"{n} doc{'s' if n != 1 else ''}" if n else None,
            _ProvAction(
                "/actions/refresh-ir",
                {"ticker": t},
                "Refresh",
                "Discover + ingest the issuer's IR historical-data spreadsheet (headless browser)",
            ),
            None,
        )
    )

    r = _prov_first(
        conn,
        "SELECT MAX(fetched_at), COUNT(*) FROM news WHERE UPPER(ticker) = ?",
        (t,),
    )
    n = _count(r, 1)
    rows.append(
        _ProvRow(
            "News",
            _ts(r, 0),
            "",
            f"{n} stor{'ies' if n != 1 else 'y'}" if n else None,
            _ProvAction(
                "/actions/refresh",
                {"ticker": t, "mode": "stale", "steps": ["news"]},
                "Refresh",
                "Re-fetch this ticker's news feeds",
            ),
            None,
        )
    )

    r = _prov_first(
        conn,
        "SELECT MAX(last_seen_at), "
        "MIN(CASE WHEN expected_date >= date('now') THEN expected_date END) "
        "FROM expected_earnings WHERE UPPER(ticker) = ?",
        (t,),
    )
    nxt = _ts(r, 1)
    rows.append(
        _ProvRow(
            "Earnings calendar",
            _ts(r, 0),
            "",
            f"next ER {fmt_date(nxt)}" if nxt else None,
            None,
            "the daily calendar refresher",
        )
    )
    return rows


def _prov_portfolio_rows(conn: sqlite3.Connection | None) -> list[_ProvRow]:
    rows: list[_ProvRow] = []

    r = _prov_first(
        conn,
        "SELECT COUNT(*), COUNT(last_built_at), MIN(last_built_at) "
        "FROM tracked_companies WHERE archived_at IS NULL",
    )
    total, built = _count(r, 0), _count(r, 1)
    rows.append(
        _ProvRow(
            "Report briefs",
            _ts(r, 2),
            "oldest ",
            f"{built} of {total} built" if total else None,
            None,
            "the tier crons (P1 daily · P2 weekly · P3 monthly)",
        )
    )

    r = _prov_first(
        conn,
        "SELECT MAX(last_pulled), COUNT(DISTINCT ticker), COUNT(*) FROM fmp_endpoint_status",
    )
    n_tickers, n_rows = _count(r, 1), _count(r, 2)
    rows.append(
        _ProvRow(
            "FMP endpoints",
            _ts(r, 0),
            "",
            f"{n_tickers} tickers · {n_rows} endpoint rows" if n_rows else None,
            None,
            "the daily fetch cron",
        )
    )

    r = _prov_first(
        conn,
        "SELECT MAX(fetched_at), COUNT(*) FROM documents WHERE doc_type LIKE 'ir_%'",
    )
    n = _count(r, 1)
    rows.append(
        _ProvRow(
            "IR documents",
            _ts(r, 0),
            "",
            f"{n} doc{'s' if n != 1 else ''}" if n else None,
            _ProvAction(
                "/actions/maintenance",
                {"action": "process_inbox"},
                "Process inbox",
                "Register documents dropped into the inbox folder "
                "(register_dropped_documents --all); the weekly IR cron does the fetching",
            ),
            None,
        )
    )

    r = _prov_first(conn, "SELECT MAX(fetched_at), COUNT(*) FROM news")
    n = _count(r, 1)
    rows.append(
        _ProvRow(
            "News",
            _ts(r, 0),
            "",
            f"{n} stor{'ies' if n != 1 else 'y'}" if n else None,
            None,
            "the daily news fetch",
        )
    )

    r = _prov_first(
        conn,
        "SELECT MAX(last_seen_at), COUNT(DISTINCT ticker) FROM expected_earnings",
    )
    n = _count(r, 1)
    rows.append(
        _ProvRow(
            "Earnings calendar",
            _ts(r, 0),
            "",
            f"{n} tickers dated" if n else None,
            None,
            "the daily calendar refresher",
        )
    )
    return rows


def _prov_first(
    conn: sqlite3.Connection | None, sql: str, params: tuple[object, ...] = ()
) -> tuple[object, ...] | None:
    """First row or None — a missing table/column (pre-migration DB, the
    minimal test substrates) degrades the row, never the peek."""
    if conn is None:
        return None
    try:
        return conn.execute(sql, params).fetchone()
    except sqlite3.Error:
        return None


def _ts(row: tuple[object, ...] | None, idx: int) -> str | None:
    if row is None or idx >= len(row) or not row[idx]:
        return None
    return str(row[idx])


def _count(row: tuple[object, ...] | None, idx: int) -> int:
    if row is None or idx >= len(row):
        return 0
    v = row[idx]
    return int(v) if isinstance(v, int | float) else 0


def _prov_row_html(row: _ProvRow) -> str:
    note = f' <span class="cc-prov-note">{escape(row.note)}</span>' if row.note else ""
    if row.action is not None:
        control = (
            '<button type="button" class="cc-prov-btn" '
            f'data-prov-post="{escape(row.action.post_url, quote=True)}" '
            f'data-prov-body="{escape(json.dumps(row.action.body), quote=True)}" '
            f'title="{escape(row.action.title, quote=True)}">{escape(row.action.label)}</button>'
        )
    elif row.cron_hint:
        control = (
            '<span class="cc-prov-cron" title="No on-demand action — refreshed by '
            f'{escape(row.cron_hint, quote=True)}">cron</span>'
        )
    else:
        control = ""
    stamp = stamp_html(row.stamp, css="cc-prov-age", prefix=row.prefix)
    return (
        '<div class="cc-prov-row">'
        f'<span class="cc-prov-src">{escape(row.label)}</span>'
        f'<span class="cc-prov-when">{stamp}{note}</span>'
        f"{control}</div>"
    )


_PROV_CSS = """
.cc-prov-rows { display: flex; flex-direction: column; }
.cc-prov-row { display: flex; align-items: baseline; gap: 10px; padding: 6px 2px;
  border-bottom: 1px solid var(--hairline); font-size: var(--fs-body); }
.cc-prov-row:last-child { border-bottom: none; }
.cc-prov-src { flex: 0 0 128px; color: var(--muted); font-size: var(--fs-caption);
  text-transform: uppercase; letter-spacing: 0.05em; }
.cc-prov-when { flex: 1 1 auto; min-width: 0; }
.cc-prov-age { font-weight: 600; }
.cc-prov-note { color: var(--muted); font-size: var(--fs-caption); }
.cc-prov-cron { flex: none; color: var(--muted); font-size: var(--fs-micro);
  border: 1px solid var(--border); border-radius: var(--radius-full);
  padding: 1px 8px; cursor: default; }
.cc-prov-btn { flex: none; font: inherit; font-size: var(--fs-caption);
  color: var(--accent); background: var(--paper); border: 1px solid var(--border);
  border-radius: var(--radius-full); padding: 1px 10px; cursor: pointer;
  transition: border-color var(--transition); }
.cc-prov-btn:hover { border-color: var(--accent); }
.cc-prov-btn:disabled { color: var(--muted); cursor: progress; border-color: var(--border); }
.cc-prov-log { font-family: var(--mono); font-size: var(--fs-micro); color: var(--fg-soft);
  background: var(--paper); border: 1px solid var(--border); border-radius: var(--radius);
  padding: 8px 10px; margin: 10px 0 0; max-height: 200px; overflow-y: auto;
  white-space: pre-wrap; word-break: break-word; }
""".strip()

# In-peek refresh wiring: POST the row's /actions/* endpoint, then stream the
# job's SSE frames ({event: start|log|done}) into the shared .cc-prov-log —
# the same contract the System actions console uses (dashboard_html). One
# document-level delegated listener (guarded) so re-injected fragments never
# double-wire; the clicked button alone is disabled while ITS job runs (other
# rows stay actionable — the registry single-flights per (ticker, kind)). A
# peek closed mid-stream leaves the EventSource draining into detached nodes
# until its done frame — harmless on this single-user localhost tool.
_PROV_JS = """
(function () {
  if (window.__ccProvWired) return;
  window.__ccProvWired = true;
  document.addEventListener('click', function (ev) {
    var btn = ev.target && ev.target.closest ? ev.target.closest('button[data-prov-post]') : null;
    if (!btn || btn.disabled) return;
    var wrap = btn.closest('.cc-prov');
    var log = wrap ? wrap.querySelector('.cc-prov-log') : null;
    if (!log) return;
    function line(t) { log.hidden = false; log.textContent += t + '\\n'; log.scrollTop = log.scrollHeight; }
    function release() { btn.disabled = false; btn.textContent = btn.getAttribute('data-prov-label') || 'Refresh'; }
    btn.setAttribute('data-prov-label', btn.textContent);
    btn.disabled = true;
    btn.textContent = 'Running…';
    fetch(btn.getAttribute('data-prov-post'), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: btn.getAttribute('data-prov-body') || '{}'
    }).then(function (resp) {
      return resp.json().then(function (j) { return { ok: resp.ok, status: resp.status, body: j }; });
    }).then(function (r) {
      if (!r.ok) {
        line('! ' + ((r.body && r.body.error) || ('HTTP ' + r.status)));
        release();
        return;
      }
      line('> ' + r.body.kind + ' started (job ' + r.body.job_id + ')');
      var es = new EventSource(r.body.stream_url);
      var finished = false;
      es.onmessage = function (e) {
        var m;
        try { m = JSON.parse(e.data); } catch (_) { return; }
        if (m.event === 'log') { line(m.line); }
        else if (m.event === 'done') {
          finished = true;
          line('# exit code ' + m.exit_code);
          es.close();
          release();
        }
      };
      es.onerror = function () {
        if (finished) return;
        line('! stream interrupted — is the server still running?');
        es.close();
        release();
      };
    }).catch(function (e) { line('! ' + e.message); release(); });
  });
})();
""".strip()
