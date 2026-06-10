"""Advisor Memos panel — the Portfolio theme's Memos tab (master build P2.3).

Three stacked views:

* **Run bar** — "Generate next-dollar memo" and "Run swap checks" buttons.
  Each POSTs ``/actions/advisor-memo`` (the jobs registry runs
  ``execution/run_advisor_memos.py``) and streams the job's stdout into the
  panel via the existing ``/actions/stream/<job_id>`` SSE channel, then
  refetches the fragment so the new memo appears.
* **Swap-discipline screen** — the deterministic table (no LLM): each
  holding's DCF upside vs the best fresh external alternative, margin vs the
  clearing bar. Renders on every load whether or not any memo was generated,
  so the discipline check is always visible.
* **Memo record** — newest-first collapsible memos from ``advisor_memos``
  (bodies through the shared light-markdown renderer), each tagged with its
  kind, tickers, and memory backlinks (note / ledger entry ids).

Tracker/DCF degradation mirrors the Decisions tab: missing inputs dash out,
nothing 500s.
"""

from __future__ import annotations

import sqlite3
from html import escape
from pathlib import Path

from advisor.context import (
    IMPLAUSIBLE_UPSIDE_PCT,
    SwapCandidate,
    load_valuations,
    screen_swap_candidates,
)
from advisor.store import AdvisorMemoRow, list_memos
from identity import DEFAULT_USER_ID
from pipeline.analytical_dashboard_html import light_markdown_to_html

_KIND_LABELS: dict[str, str] = {
    "next_dollar": "Next dollar",
    "swap_check": "Swap check",
    "socratic": "Socratic",
}

DEFAULT_MARGIN_PP = 15.0


def render_advisor_memos_panel(
    db_path: Path,
    *,
    user_id: str = DEFAULT_USER_ID,
    margin_pp: float = DEFAULT_MARGIN_PP,
) -> str:
    """The Memos tab fragment. Pure DB reads — memo generation only ever
    happens through the run bar's explicit POST (LLM spend stays deliberate)."""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        holdings_val, candidates_val = load_valuations(conn)
    finally:
        conn.close()
    screen = screen_swap_candidates(holdings_val, candidates_val, margin_pp=margin_pp)
    implausible = sorted(
        t for t, v in candidates_val.items() if v.upside_pct > IMPLAUSIBLE_UPSIDE_PCT
    )
    try:
        memos = list_memos(user_id=user_id, limit=50, db_path=db_path)
    except (sqlite3.OperationalError, FileNotFoundError, RuntimeError):
        memos = []  # substrate predates 0077 — render the screen + run bar anyway
    return compose_memos_page(screen, memos, margin_pp=margin_pp, implausible=implausible)


def compose_memos_page(
    screen: list[SwapCandidate],
    memos: list[AdvisorMemoRow],
    *,
    margin_pp: float = DEFAULT_MARGIN_PP,
    implausible: list[str] | None = None,
) -> str:
    """Pure page assembly (testable without DB)."""
    return "".join(
        [
            _PANEL_CSS,
            _run_bar(),
            _screen_section(screen, margin_pp=margin_pp, implausible=implausible or []),
            _memos_section(memos),
            f"<script>{_RUN_JS}</script>",
        ]
    )


def _run_bar() -> str:
    return (
        '<div class="am-runbar" id="am-runbar">'
        '<span class="am-runbar-label">Advisor</span>'
        '<button type="button" class="am-btn" data-kind="next_dollar">'
        "Generate next-dollar memo</button>"
        '<button type="button" class="am-btn" data-kind="swap_checks">'
        "Run swap checks</button>"
        '<span class="muted am-note">Evidence + framing, never directives. '
        "Each run spends LLM budget (advisor_* purposes) and lands in the record below "
        "+ your notes.</span>"
        '<pre class="am-log" id="am-log" hidden></pre>'
        "</div>"
    )


def _screen_section(
    screen: list[SwapCandidate], *, margin_pp: float, implausible: list[str]
) -> str:
    head = (
        '<section class="panel"><h2>Swap-discipline screen</h2>'
        f'<p class="sub">Deterministic, LLM-free: each holding\'s DCF upside vs the best '
        f"fresh external alternative (watchlist + evaluation, breached theses excluded). "
        f"A swap memo is only considered when the margin clears {margin_pp:.0f}pp — wide "
        "enough to survive tax drag and DCF asymmetry.</p>"
    )
    if implausible:
        head += (
            '<p class="muted am-note">Excluded as implausible (DCF upside &gt; '
            f"+{IMPLAUSIBLE_UPSIDE_PCT:.0f}% — more likely a mis-modeled run than an "
            f"opportunity): {escape(', '.join(implausible))}.</p>"
        )
    if not screen:
        return (
            f"{head}"
            '<p class="muted">No screen rows — needs holdings and external names with '
            "usable DCF runs.</p></section>"
        )
    rows = "".join(
        "<tr>"
        f'<td class="ticker"><a href="/ticker/{escape(s.holding)}" class="ticker-link">'
        f"{escape(s.holding)}</a></td>"
        f'<td class="num">{s.holding_upside_pct:+.0f}%</td>'
        f"<td>{escape(s.candidate)} <span class='muted'>({escape(s.candidate_list)})</span></td>"
        f'<td class="num">{s.candidate_upside_pct:+.0f}%</td>'
        f'<td class="num {"am-cleared" if s.cleared else ""}">{s.margin_pp:+.0f}pp</td>'
        f"<td>{_bar_cell(s)}</td>"
        "</tr>"
        for s in screen
    )
    return (
        f"{head}"
        '<table class="am-screen"><thead><tr>'
        '<th>Holding</th><th class="num">Upside</th>'
        '<th>Best alternative</th><th class="num">Upside</th>'
        '<th class="num">Margin</th><th>Bar</th>'
        "</tr></thead><tbody>"
        f"{rows}</tbody></table></section>"
    )


def _bar_cell(s: SwapCandidate) -> str:
    if s.cleared:
        return '<span class="am-pill am-pill-cleared">clears the bar</span>'
    return '<span class="am-pill am-pill-held">discipline holds</span>'


def _memos_section(memos: list[AdvisorMemoRow]) -> str:
    head = (
        '<section class="panel"><h2>Memo record</h2>'
        '<p class="sub">Every advisor memo, newest first — each also wrote an analyst note '
        "(and, when ticker-scoped, a decisions-timeline entry). Scoring lands with the "
        "stance scorecard (P2.5).</p>"
    )
    if not memos:
        return (
            f"{head}"
            '<p class="muted">No memos yet — generate the first next-dollar memo above.</p>'
            "</section>"
        )
    cards = "".join(_memo_card(m) for m in memos)
    return f"{head}{cards}</section>"


def _memo_card(m: AdvisorMemoRow) -> str:
    kind = _KIND_LABELS.get(m.kind, m.kind)
    scope = m.ticker or "portfolio"
    if m.counter_ticker:
        scope += f" vs {m.counter_ticker}"
    body = light_markdown_to_html(m.body_md[:12000])
    links: list[str] = []
    if m.note_id is not None:
        links.append(f"note #{m.note_id}")
    if m.ledger_entry_id is not None:
        links.append(f"ledger #{m.ledger_entry_id}")
    link_str = f' · <span class="muted">{escape(" · ".join(links))}</span>' if links else ""
    return (
        f'<details class="am-card"><summary>'
        f'<span class="am-pill am-kind-{escape(m.kind)}">{escape(kind)}</span>'
        f'<span class="am-scope">{escape(scope)}</span>'
        f'<span class="am-title">{escape(m.title)}</span>'
        f'<span class="am-stamp">{escape(m.created_at.date().isoformat())}{link_str}</span>'
        f"</summary>"
        f'<div class="am-body">{body}</div>'
        "</details>"
    )


_PANEL_CSS = """<style>
.am-runbar { display: flex; align-items: center; gap: 10px; flex-wrap: wrap;
  background: var(--surface); border: 1px solid var(--border); border-radius: 8px;
  padding: 10px 14px; margin-bottom: 18px; font-size: 12.5px; }
.am-runbar-label { font-family: var(--mono); font-size: 11px; text-transform: uppercase;
  letter-spacing: 0.5px; color: var(--muted); }
.am-btn { background: var(--accent); color: #0d1117; border: none; border-radius: 4px;
  padding: 5px 12px; font-size: 12px; font-weight: 600; cursor: pointer; }
.am-btn[disabled] { opacity: 0.45; cursor: wait; }
.am-note { font-size: 11.5px; }
.am-log { width: 100%; margin: 8px 0 0; padding: 8px 10px; background: var(--paper);
  border: 1px solid var(--border); border-radius: 4px; font-family: var(--mono);
  font-size: 11px; max-height: 180px; overflow-y: auto; white-space: pre-wrap; }
.am-screen td { vertical-align: middle; }
.am-cleared { color: var(--warn); font-weight: 600; }
.am-pill { display: inline-block; padding: 1px 8px; border-radius: 10px; font-size: 11px;
  font-weight: 600; white-space: nowrap; }
.am-pill-cleared { background: #422006; color: var(--warn); }
.am-pill-held { background: #14361f; color: #6ee7a0; }
.am-card { background: var(--surface); border: 1px solid var(--border); border-radius: 8px;
  padding: 10px 14px; margin-bottom: 10px; }
.am-card summary { cursor: pointer; list-style: none; display: flex; align-items: baseline;
  gap: 10px; flex-wrap: wrap; }
.am-card summary::-webkit-details-marker { display: none; }
.am-card summary::before { content: '\\25B8  '; color: var(--muted); font-family: var(--mono); }
.am-card[open] summary::before { content: '\\25BE  '; }
.am-kind-next_dollar { background: #1f2b3a; color: #8fb6e6; }
.am-kind-swap_check { background: #2b2440; color: #c4b5fd; }
.am-kind-socratic { background: #103039; color: #7dd3fc; }
.am-scope { font-family: var(--mono); font-weight: 600; }
.am-title { color: var(--fg-soft, #ccc); font-size: 12.5px; }
.am-stamp { margin-left: auto; color: var(--muted); font-size: 11px; font-family: var(--mono); }
.am-body { font-size: 13px; line-height: 1.6; margin-top: 10px; }
.am-body h2, .am-body h3, .am-body h4 { color: #f5f5f0; margin: 12px 0 4px; }
.am-body h3 { font-size: 14px; }
.am-body ul { padding-left: 20px; }
</style>"""

# Run-bar wiring: POST the action, stream the job's SSE frames into the log,
# refetch the panel on done. Plain string — braces are literal JS.
_RUN_JS = r"""
(function () {
  var bar = document.getElementById('am-runbar');
  if (!bar) return;
  var logEl = document.getElementById('am-log');
  function append(line) {
    logEl.hidden = false;
    logEl.textContent += line + '\n';
    logEl.scrollTop = logEl.scrollHeight;
  }
  function refetch() {
    var target = bar.closest('.cc-panel-body') || bar.parentElement || document.body;
    fetch('/api/panel/advisor_memos')
      .then(function (r) { if (!r.ok) throw new Error('HTTP ' + r.status); return r.text(); })
      .then(function (html) {
        target.innerHTML = html;
        var scripts = target.querySelectorAll('script');
        for (var i = 0; i < scripts.length; i++) {
          var old = scripts[i];
          var s = document.createElement('script');
          if (old.src) s.src = old.src; else s.textContent = old.textContent;
          old.parentNode.replaceChild(s, old);
        }
      })
      .catch(function (e) { append('reload failed: ' + e.message); });
  }
  bar.addEventListener('click', function (ev) {
    var btn = ev.target && ev.target.closest ? ev.target.closest('button[data-kind]') : null;
    if (!btn) return;
    var kind = btn.getAttribute('data-kind');
    var buttons = bar.querySelectorAll('button');
    buttons.forEach(function (b) { b.disabled = true; });
    logEl.hidden = false;
    logEl.textContent = '';
    append('starting ' + kind + ' run…');
    fetch('/actions/advisor-memo', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ kind: kind })
    }).then(function (r) {
      return r.json().then(function (j) {
        if (!r.ok) throw new Error(j.error || ('HTTP ' + r.status));
        return j;
      });
    }).then(function (job) {
      var es = new EventSource(job.stream_url);
      var finished = false;
      es.onmessage = function (ev2) {
        var m;
        try { m = JSON.parse(ev2.data); } catch (_) { return; }
        if (m.event === 'start') {
          append('> job ' + m.job_id + ' started (' + m.kind + ')');
        } else if (m.event === 'log') {
          append(m.line);
        } else if (m.event === 'done') {
          finished = true;
          append('# exit code ' + m.exit_code);
          es.close();
          buttons.forEach(function (b) { b.disabled = false; });
          if (m.exit_code === 0) refetch();
        }
      };
      es.onerror = function () {
        if (!finished) append('stream closed');
        es.close();
        buttons.forEach(function (b) { b.disabled = false; });
      };
    }).catch(function (e) {
      append('failed: ' + e.message);
      buttons.forEach(function (b) { b.disabled = false; });
    });
  });
})();
""".strip()
