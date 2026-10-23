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
from advisor.store import AdvisorMemoRow, StanceScoreRow, list_memos, list_scores_for_memos
from dcf.input_evidence import SourceReadContext
from identity import DEFAULT_USER_ID
from pipeline.portfolio_styles import memos_css
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite
from ui import living_grid as lg
from ui.controls import ticker_label
from ui.panel import panel_section
from ui.prose import render_prose

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
    source_context: SourceReadContext | None = None,
) -> str:
    """The Memos tab fragment. Pure DB reads — memo generation only ever
    happens through the run bar's explicit POST (LLM spend stays deliberate)."""
    conn = connect_sqlite(db_path, role=SQLiteConnectionRole.READ_ONLY)
    conn.row_factory = sqlite3.Row
    try:
        holdings_val, candidates_val = load_valuations(conn, source_context=source_context)
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
    try:
        scores = list_scores_for_memos([m.id for m in memos], db_path=db_path)
    except (sqlite3.OperationalError, FileNotFoundError, RuntimeError):
        scores = {}  # substrate predates 0078 — pills fall back to "scoring pending"
    return compose_memos_page(
        screen,
        memos,
        margin_pp=margin_pp,
        implausible=implausible,
        scores=scores,
    )


def compose_memos_page(
    screen: list[SwapCandidate],
    memos: list[AdvisorMemoRow],
    *,
    margin_pp: float = DEFAULT_MARGIN_PP,
    implausible: list[str] | None = None,
    scores: dict[int, StanceScoreRow] | None = None,
) -> str:
    """Pure page assembly (testable without DB)."""
    return "".join(
        [
            _PANEL_CSS,
            _run_bar(),
            _screen_section(screen, margin_pp=margin_pp, implausible=implausible or []),
            _memos_section(memos, scores or {}),
            f"<script>{_RUN_JS}</script>",
        ]
    )


def _run_bar() -> str:
    return (
        '<div class="am-runbar" id="am-runbar">'
        '<span class="k-label">Advisor</span>'
        '<button type="button" class="k-btn k-btn-primary k-btn-sm" data-kind="next_dollar">'
        "Generate next-dollar memo</button>"
        '<button type="button" class="k-btn k-btn-quiet k-btn-sm" data-kind="swap_checks">'
        "Run swap checks</button>"
        '<span class="muted am-note">Evidence + framing, never directives. '
        "Each run spends LLM budget and lands in the record below + your notes.</span>"
        '<pre class="am-log" id="am-log" hidden></pre>'
        "</div>"
    )


def _screen_section(
    screen: list[SwapCandidate], *, margin_pp: float, implausible: list[str]
) -> str:
    sub = (
        f'<p class="sub">Deterministic, LLM-free: each holding\'s DCF upside vs the best '
        f"fresh external alternative (watchlist + evaluation, breached theses excluded). "
        f"A swap memo is only considered when the margin clears {margin_pp:.0f}pp — wide "
        "enough to survive tax drag and DCF asymmetry.</p>"
    )
    if implausible:
        sub += (
            '<p class="muted am-note">Excluded as implausible (DCF upside &gt; '
            f"+{IMPLAUSIBLE_UPSIDE_PCT:.0f}% — more likely a mis-modeled run than an "
            f"opportunity): {escape(', '.join(implausible))}.</p>"
        )
    if not screen:
        return panel_section(
            sub,
            '<p class="muted">No screen rows — needs holdings and external names with '
            "usable DCF runs.</p>",
            title="Swap-discipline screen",
        )
    # The "best alternative" is a single portfolio-wide winner (the highest-upside
    # eligible external name), so it is identical on every row — owner feedback
    # 2026-07-14: "why is the alternative the same for everything — remove the
    # column." Name it ONCE in a caption; the table then carries only per-holding
    # data (its own upside + the margin against that one alternative).
    best = screen[0]
    best_label = ticker_label(best.candidate, href=f"/ticker/{escape(best.candidate)}")
    caption = (
        f'<p class="sub am-swap-target">Best available alternative: '
        f"{best_label} "
        f'<span class="muted">({escape(best.candidate_list)})</span> '
        f"at {best.candidate_upside_pct:+.0f}% DCF upside. Each holding below is scored by the "
        "margin its own upside trails this alternative.</p>"
    )
    rows = "".join(
        f"<tr{_screen_data(s)}>"
        f'<td class="ticker">'
        f"{ticker_label(s.holding, href='/ticker/' + escape(s.holding))}"
        "</td>"
        f'<td class="num">{s.holding_upside_pct:+.0f}%</td>'
        f'<td class="num {"am-cleared" if s.cleared else ""}">{s.margin_pp:+.0f}pp</td>'
        f"<td>{_bar_cell(s)}</td>"
        "</tr>"
        for s in screen
    )
    return panel_section(
        sub,
        caption,
        lg.grid_open()
        + lg.filter_bar(len(screen), noun="rows", placeholder="Filter by holding…")
        + '<table class="am-screen"><thead><tr>'
        + lg.th("Holding", "holding", "text", num=False)
        + lg.th("Upside", "hupside", "num")
        + lg.th("Margin vs alt", "margin", "num")
        + "<th>Bar</th>"
        + "</tr></thead><tbody>"
        + f"{rows}</tbody></table>"
        + lg.grid_close(),
        title="Swap-discipline screen",
    )


def _screen_data(s: SwapCandidate) -> str:
    return (
        lg.data_text(f"{s.holding} {s.candidate}")
        + lg.data_text_key("holding", s.holding)
        + lg.data_num("hupside", s.holding_upside_pct)
        + lg.data_num("margin", s.margin_pp)
    )


def _bar_cell(s: SwapCandidate) -> str:
    if s.cleared:
        return '<span class="k-pill k-pill-warn">clears the bar</span>'
    return '<span class="k-pill k-pill-ok">discipline holds</span>'


def _memos_section(memos: list[AdvisorMemoRow], scores: dict[int, StanceScoreRow]) -> str:
    sub = (
        '<p class="sub">Every advisor memo, newest first — each also wrote an analyst note '
        "(and, when ticker-scoped, a decisions-timeline entry). Matured stances are graded "
        "weekly against subsequent price (SPY-relative when the tracker is up).</p>"
    )
    if not memos:
        return panel_section(
            sub,
            '<p class="muted">No memos yet — generate the first next-dollar memo above.</p>',
            title="Memo record",
        )
    cards = "".join(_memo_card(m, scores.get(m.id)) for m in memos)
    return panel_section(
        sub,
        _track_record_strip(scores),
        cards,
        title="Memo record",
    )


def _track_record_strip(scores: dict[int, StanceScoreRow]) -> str:
    """The aggregate scorecard: how the advisor's stances and screens have
    actually graded. Hidden until something has been scored."""
    graded = [s for s in scores.values() if s.verdict != "unscoreable"]
    if not graded:
        return ""
    stances = [s for s in graded if s.verdict in ("correct", "wrong", "mixed")]
    swaps = [s for s in graded if s.verdict in ("screen_validated", "screen_refuted")]
    bits: list[str] = []
    if stances:
        correct = sum(1 for s in stances if s.verdict == "correct")
        excesses = [s.excess_return_pct for s in stances if s.excess_return_pct is not None]
        avg = f" · avg excess {sum(excesses) / len(excesses):+.1f}pp" if excesses else ""
        bits.append(f"Stances: {correct}/{len(stances)} correct{avg}")
    if swaps:
        validated = sum(1 for s in swaps if s.verdict == "screen_validated")
        bits.append(f"Swap screens: {validated}/{len(swaps)} validated")
    return '<p class="am-track muted">Track record — ' + " · ".join(bits) + "</p>"


_VERDICT_TONE: dict[str, str] = {
    "correct": "ok",
    "screen_validated": "ok",
    "wrong": "bad",
    "screen_refuted": "bad",
    "mixed": "warn",
    "unscoreable": "muted",
}


def _score_pill(score: StanceScoreRow) -> str:
    """One memo's graded outcome, rendered beside its stance/kind."""
    tone = _VERDICT_TONE.get(score.verdict, "muted")
    if score.verdict in ("correct", "wrong", "mixed") and score.excess_return_pct is not None:
        detail = f" {score.excess_return_pct:+.1f}pp vs SPY"
    elif score.verdict in ("correct", "wrong", "mixed") and score.ticker_return_pct is not None:
        detail = f" {score.ticker_return_pct:+.1f}% abs"
    elif score.verdict in ("screen_validated", "screen_refuted") and score.detail:
        margin = score.detail.get("realized_margin_pp")
        detail = f" {float(margin):+.1f}pp realized" if isinstance(margin, (int, float)) else ""
    else:
        detail = ""
    tip = (
        f"graded {score.start_date or '?'} → {score.end_date or '?'} · "
        f"basis {score.benchmark_basis}"
    )
    # Filled status pill = the control kit's .k-pill (+ tone); muted → bare.
    suffix = f" k-pill-{tone}" if tone in ("ok", "warn", "bad") else ""
    return (
        f'<span class="k-pill{suffix}" title="{escape(tip)}">'
        f"{escape(score.verdict.replace('_', ' '))}{escape(detail)}</span>"
    )


def _memo_card(m: AdvisorMemoRow, score: StanceScoreRow | None = None) -> str:
    kind = _KIND_LABELS.get(m.kind, m.kind)
    scope = m.ticker or "portfolio"
    if m.counter_ticker:
        scope += f" vs {m.counter_ticker}"
    body = render_prose(m.body_md[:12000])
    stance = ""
    if m.stance:
        horizon = f" · {m.horizon_days}d" if m.horizon_days else ""
        # Every displayed stance carries its track record (directive): the
        # graded verdict once scored, the pending state until then.
        # Stance is the analyst's position, not a status — a neutral bare
        # .k-pill (the local .am-stance now carries only its typographic refine).
        stance = (
            f'<span class="k-pill am-stance" title="scoring {escape(m.score_status)}">'
            f"stance: {escape(m.stance)}{escape(horizon)}</span>"
        )
    if score is not None:
        stance += _score_pill(score)
    elif m.score_status == "pending" and m.kind == "swap_check":
        stance += '<span class="k-pill" title="screen grades at horizon">scoring pending</span>'
    links: list[str] = []
    if m.note_id is not None:
        links.append(f"note #{m.note_id}")
    if m.ledger_entry_id is not None:
        links.append(f"ledger #{m.ledger_entry_id}")
    link_str = f' · <span class="muted">{escape(" · ".join(links))}</span>' if links else ""
    return (
        f'<details class="am-card"><summary>'
        f'<span class="k-chip">{escape(kind)}</span>'
        f'<span class="am-scope">{escape(scope)}</span>'
        f"{stance}"
        f'<span class="am-title">{escape(m.title)}</span>'
        f'<span class="am-stamp">{escape(m.created_at.date().isoformat())}{link_str}</span>'
        f"</summary>"
        f'<div class="am-body">{body}</div>'
        "</details>"
    )


_PANEL_CSS = memos_css()

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
    (window.uiFetch || fetch)('/api/panel/advisor_memos')
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
    CCAction.busy(btn, 'Running…');
    buttons.forEach(function (b) { if (b !== btn) b.disabled = true; });
    logEl.hidden = false;
    logEl.textContent = '';
    append('starting ' + kind + ' run…');
    function releaseAll() {
      CCAction.release(btn);
      buttons.forEach(function (b) { if (b !== btn) b.disabled = false; });
    }
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
          buttons.forEach(function (b) { if (b !== btn) b.disabled = false; });
          if (m.exit_code === 0) {
            CCAction.receipt(btn, '✓ Run complete — recorded below');
            setTimeout(refetch, 900);
          } else {
            CCAction.release(btn);
            append('run failed — exit code ' + m.exit_code + ', see log above for detail');
          }
        }
      };
      es.onerror = function () {
        if (!finished) append('stream closed unexpectedly — run may not have completed');
        es.close();
        releaseAll();
      };
    }).catch(function (e) {
      append('failed: ' + e.message);
      releaseAll();
    });
  });
})();
""".strip()
