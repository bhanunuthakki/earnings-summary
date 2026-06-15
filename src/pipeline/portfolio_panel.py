"""Portfolio section page renderers for the command-center shell.

Master build P2.1 — the advisor's data foundation — split across two sub-tabs
(UX round 4: the synthesis layer stopped being a buried strip at the bottom of
Performance):

* **Performance** (``render_portfolio_panel``): the tracker's analytics (TWR
  vs SPY / QQQ / policy with the policy mix, risk stats vs SPY, allocation +
  concentration cuts, per-position dollar alpha), then the live positions /
  % of book / taxable breakdown / latest transactions. Every number in the
  analytics sections comes from the tracker's API verbatim — benchmark math is
  never rebuilt here (directive architecture rule); the only client-side
  arithmetic is display formatting and the portfolio-minus-benchmark readout
  of two API values.
* **Synthesis** (``render_portfolio_synthesis_panel``): the portfolio-level
  reading layer — thesis-health rollup + sector exposure in a grid up top, the
  quantitative next-dollar allocation distribution (src/allocation) full-width
  with its factor waterfall, the cached ``cross_portfolio_synthesis`` lens
  memo below.

Degrades gracefully: tracker fully offline → Performance shows ONE "tracker
offline" note (with the start hint); tracker up but an analytics endpoint
failing → the other sections still render and the failed ones are named in a
footnote. The Synthesis tab never shows the offline card — its panels fall
back to equal-weighted readings and say so in their sub-lines.

Reuses the dark panel/table/kpi-strip CSS vocabulary the shell already defines;
the fragment-local additions (legend chips, allocation bars, the benchmark
chart on Performance; the insights grid + next-dollar rows on Synthesis) ship
as per-fragment ``<style>`` blocks keyed off the shared token variables, and
the chart's series colors come from ``ui.tokens.CHART_SERIES``.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from html import escape
from pathlib import Path

from allocation import FACTOR_LABELS, NextDollarModel, build_next_dollar_model
from attribution import PositionAttribution
from integrations.portfolio_tracker_client import (
    TAX_BUCKETS,
    AllocationBucket,
    BetaStats,
    LivePortfolio,
    PerformancePoint,
    PerformanceSeries,
    PolicyMix,
    PortfolioAnalytics,
    PositionAlpha,
    PositionCorrelationRow,
    Positioning,
    fetch_live_portfolio,
    fetch_portfolio_analytics,
)
from portfolio_risk import (
    CrowdedName,
    DrawdownPoint,
    DrawdownStats,
    FactorRollup,
    compute_drawdown,
    factor_exposure_rollup,
)
from portfolio_risk_snapshot_store import (
    RiskSnapshot,
    read_latest_snapshot,
    write_snapshot,
)
from risk_reward import RiskRewardGap, RiskRewardGapRow, build_risk_reward_gap
from ui.controls import ticker_label
from ui.time import stamp_html
from ui.tokens import CHART_SERIES

_TAX_LABELS: dict[str, str] = {
    "taxable": "Taxable",
    "tax_deferred": "Tax-deferred",
    "tax_free": "Tax-free",
    "unknown": "Unknown",
}

# Greek letters for the stat labels, via chr() so the source stays ASCII
# (RUF001 ambiguous-unicode) — same idiom as workspace_charts._RSQUO.
_ALPHA = chr(0x03B1)
_SIGMA = chr(0x03C3)


@dataclass(frozen=True, slots=True)
class WindowSelection:
    """The analytics window applied to the tracker fetch, echoed back into the
    page's window bar so the controls reflect what is actually shown.
    ``None`` dates mean the tracker's own defaults (snapshot-derived window
    for /performance, trailing 365d elsewhere)."""

    start_date: str | None = None
    end_date: str | None = None
    include_backfill: bool = False


_DEFAULT_WINDOW = WindowSelection()


def _valid_iso(s: str | None) -> str | None:
    """``YYYY-MM-DD`` normalized, or None for absent/garbage input."""
    if not s:
        return None
    try:
        return date.fromisoformat(s).isoformat()
    except ValueError:
        return None


def validated_window(
    start_date: str | None, end_date: str | None, include_backfill: bool
) -> WindowSelection:
    """Sanitize the query-string window: ISO dates only; an inverted range
    falls back to the tracker defaults entirely (the bar echoes what was
    actually applied, so the fallback is visible, not silent)."""
    s, e = _valid_iso(start_date), _valid_iso(end_date)
    if s and e and s > e:
        s = e = None
    return WindowSelection(start_date=s, end_date=e, include_backfill=include_backfill)


def render_portfolio_panel(
    *,
    api_url: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    include_backfill: bool = False,
    db_path: Path | None = None,
) -> str:
    """The Portfolio → Performance tab fragment: tracker analytics (performance /
    risk / positioning / alpha) over the requested window, then the live
    positions/taxable view. The window args come from the page's own window bar
    (via ``/api/panel/portfolio`` query params) and pass through to the tracker
    verbatim after validation. ``db_path`` (S15) joins the per-position
    attribution narratives onto the alpha numbers — None keeps the page
    tracker-pure (the pre-S15 shape). The synthesis layer lives on its own
    sub-tab — ``render_portfolio_synthesis_panel``."""
    window = validated_window(start_date, end_date, include_backfill)
    analytics = fetch_portfolio_analytics(
        api_url=api_url,
        start_date=window.start_date,
        end_date=window.end_date,
        include_backfill=window.include_backfill,
    )
    live = fetch_live_portfolio(api_url=api_url)
    attributions: list[PositionAttribution] | None = None
    if db_path is not None and db_path.exists():
        from attribution import attributions_for_book

        attributions = attributions_for_book(db_path=db_path, alpha=analytics.position_alpha)
    # Keep the risk snapshot fresh on a successful read; fall back to it (stamped)
    # when the analytics are down so the page still carries a risk read (L5 PR2).
    snapshot: RiskSnapshot | None = None
    if db_path is not None:
        if analytics.available:
            _persist_risk_snapshot(analytics, db_path)
        else:
            snapshot = read_latest_snapshot(db_path=db_path)
    return compose_portfolio_page(
        analytics, live, window=window, attributions=attributions, snapshot=snapshot
    )


def compose_portfolio_page(
    analytics: PortfolioAnalytics,
    live: LivePortfolio,
    window: WindowSelection | None = None,
    attributions: list[PositionAttribution] | None = None,
    snapshot: RiskSnapshot | None = None,
) -> str:
    """Pure page assembly (testable without network or DB).

    The window bar always leads the page — it doubles as a retry/refresh
    control when the tracker is down. Tracker fully down → the live section's
    single offline note carries the start hint for the whole page (no duplicate
    per-section offline panels). Tracker up but ALL analytics endpoints failing
    (e.g. an older tracker build) → one quiet note instead of five dead
    sections. ``attributions`` (S15) renders the per-position narrative block
    after the analytics sections; None/empty hides it. ``snapshot`` (L5 PR2)
    renders the last-known cached risk read when the analytics are unavailable.
    """
    w = window or _DEFAULT_WINDOW
    parts: list[str] = [_ANALYTICS_CSS, _window_bar(w)]
    if analytics.available:
        parts.append(render_portfolio_analytics_sections(analytics))
    else:
        if snapshot is not None:
            parts.append(_cached_risk_section(snapshot))
        if live.available:
            first_error = next(iter(analytics.errors.values()), "no analytics payloads")
            parts.append(
                '<section class="panel"><h2>Portfolio analytics</h2>'
                '<p class="muted">The tracker is reachable but its analytics endpoints aren\'t — '
                f"{escape(first_error)}.</p></section>"
            )
    if attributions:
        from pipeline.attribution_panel import render_book_attribution_section

        parts.append(render_book_attribution_section(attributions))
    parts.append(render_live_portfolio_section(live))
    return "".join(parts)


# ---------------------------------------------------------------------------
# Tracker analytics sections (master build P2.1). Pure HTML assembly over the
# already-parsed PortfolioAnalytics — no network, no benchmark math.
# ---------------------------------------------------------------------------

# Styling only the Performance fragment needs; everything else reuses the
# shell's panel/kpi/table vocabulary. Colors key off the shared token variables
# so a palette change in ui/tokens.py propagates here untouched. (The Synthesis
# fragment carries its own block — _INSIGHTS_CSS.)
_ANALYTICS_CSS = """<style>
.pf-legend { display: flex; gap: 18px; flex-wrap: wrap; margin: 2px 0 10px; font-size: var(--fs-body); }
.pf-chip { display: inline-flex; align-items: center; gap: 6px; color: var(--muted); }
.pf-chip strong { color: var(--fg); font-variant-numeric: tabular-nums; }
.pf-swatch { width: 10px; height: 10px; border-radius: var(--radius); display: inline-block; }
.pf-chart { width: 100%; height: auto; display: block; }
.pf-policy { font-size: var(--fs-caption); margin: 10px 0 0; }
.pf-warn { color: var(--warn); }
.pf-alloc-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr));
  gap: 10px 32px; margin-top: 4px; }
.pf-alloc-row { display: grid; grid-template-columns: minmax(110px, 1.3fr) 2fr 52px 76px;
  gap: 10px; align-items: center; font-size: var(--fs-body); padding: 3px 0; }
.pf-alloc-label { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.pf-bar { background: var(--hairline); border-radius: var(--radius); height: 10px; overflow: hidden; }
.pf-bar-fill { background: var(--accent); opacity: 0.75; height: 100%; display: block; }
.pf-alloc-pct { text-align: right; font-variant-numeric: tabular-nums; }
.pf-alloc-val { text-align: right; font-variant-numeric: tabular-nums; font-size: var(--fs-caption); }
.pf-flag { color: var(--warn); margin-left: 4px; cursor: help; }
.pf-total td { font-weight: 600; border-top: 2px solid var(--border); }
.pf-degraded { font-size: var(--fs-caption); }
.pf-window { display: flex; align-items: center; gap: 8px; flex-wrap: wrap;
  background: var(--surface); border-radius: var(--radius);
  padding: 10px 14px; margin-bottom: 18px; font-size: var(--fs-body); }
.pf-window-label { font-size: var(--fs-caption); text-transform: uppercase;
  letter-spacing: 0.06em; color: var(--muted); margin-right: 4px; }
.pf-btn { background: var(--paper); color: var(--fg-soft); border: 1px solid var(--border);
  border-radius: var(--radius); padding: 4px 10px; font-size: var(--fs-caption); cursor: pointer;
  font-family: var(--sans); transition: color var(--transition), border-color var(--transition); }
.pf-btn:hover { border-color: var(--border-2); color: var(--fg); }
.pf-btn-apply { background: var(--accent); color: var(--accent-contrast); border: none;
  font-weight: 600; }
.pf-window input[type="date"] { padding: 3px 6px; font-size: var(--fs-caption);
  font-family: var(--mono); }
.pf-backfill-label { color: var(--muted); display: inline-flex; align-items: center;
  gap: 5px; margin-left: 6px; cursor: help; }
</style>"""

# Styling for the Synthesis fragment: the rollup/exposure insights grid and
# the next-dollar distribution rows. Same token-variable discipline as
# _ANALYTICS_CSS; a separate block so each tab ships only the rules it renders.
_INSIGHTS_CSS = """<style>
.pf-insights { display: grid; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr));
  gap: 0 18px; align-items: start; }
.pf-th-chips { display: flex; gap: 8px; flex-wrap: wrap; }
.pf-th-chip { font-family: var(--sans); font-size: var(--fs-caption); text-decoration: none;
  border: 1px solid var(--border); border-radius: var(--radius); padding: 3px 9px; }
.pf-th-warn { color: var(--warn); border-color: var(--warn); }
.pf-th-bad { color: var(--bad); border-color: var(--bad); }
.pf-exp-row { display: grid; grid-template-columns: minmax(110px, 1fr) 2fr 44px; gap: 10px;
  align-items: center; font-size: var(--fs-body); padding: 3px 0; }
.pf-exp-label { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.pf-exp-bar { background: var(--paper); border-radius: var(--radius); height: 9px; overflow: hidden; }
.pf-exp-bar span { display: block; height: 100%; background: var(--accent); border-radius: var(--radius); }
.pf-exp-pct { text-align: right; font-variant-numeric: tabular-nums; color: var(--muted); }
.pf-nd-excerpt { font-size: var(--fs-body); line-height: 1.55; }
.pf-nd-item { border-radius: var(--radius); }
.pf-nd-item:hover, .pf-nd-item:focus-within { background: var(--surface); }
.pf-nd-row { display: grid; grid-template-columns: 56px 1fr 56px 70px; gap: 10px;
  align-items: center; font-size: var(--fs-body); padding: 3px 0; }
.pf-nd-ticker { font-family: var(--mono); }
.pf-nd-bar { background: var(--paper); border-radius: var(--radius); height: 9px; overflow: hidden; }
.pf-nd-bar span { display: block; height: 100%; background: var(--accent); border-radius: var(--radius); }
.pf-nd-alloc { text-align: right; font-variant-numeric: tabular-nums; }
.pf-nd-now { text-align: right; font-variant-numeric: tabular-nums; font-size: var(--fs-caption); }
.pf-nd-wf { display: none; gap: 6px; flex-wrap: wrap; padding: 1px 0 6px; }
.pf-nd-item:hover .pf-nd-wf, .pf-nd-item:focus-within .pf-nd-wf { display: flex; }
.pf-nd-chip { font-family: var(--sans); font-size: var(--fs-caption); border: 1px solid var(--border);
  border-radius: var(--radius); padding: 2px 7px; cursor: help; color: var(--muted); }
.pf-nd-chip.pos { color: var(--ok); border-color: var(--ok); }
.pf-nd-chip.neg { color: var(--bad); border-color: var(--bad); }
.pf-nd-note { font-size: var(--fs-caption); }
.pf-nd-hint { font-size: var(--fs-caption); }
.pf-nd-memo-h { margin-top: 12px; }
</style>"""

_SECTION_LABELS: dict[str, str] = {
    "performance": "Performance vs benchmarks",
    "beta": "Risk vs benchmark",
    "positioning": "Positioning",
    "position_alpha": "Per-position alpha",
    "policy": "Policy mix",
}

# Chart/legend series: label, stroke, stroke-width, value extractor. The book's
# line is the bright foreground token; benchmarks use the shared categorical
# chart palette (Okabe-Ito) so they read apart without semantic green/red.
_CHART_SPECS: tuple[tuple[str, str, float, Callable[[PerformancePoint], float | None]], ...] = (
    ("Portfolio", "var(--fg)", 2.4, lambda p: p.portfolio_return_pct),
    ("SPY", CHART_SERIES[1], 1.3, lambda p: p.spy_return_pct),
    ("QQQ", CHART_SERIES[3], 1.3, lambda p: p.qqq_return_pct),
    ("Policy", CHART_SERIES[5], 1.3, lambda p: p.policy_return_pct),
)


# Vanilla JS for the window bar: preset / custom-date / backfill refetch of
# this panel fragment, re-executing fragment scripts on inject (the same idiom
# as SHELL_JS's injectHtml — innerHTML alone does not run <script> tags).
# Plain string, not an f-string: the braces are literal JS.
_WINDOW_JS = r"""
(function () {
  var bar = document.getElementById('pf-window-bar');
  if (!bar) return;
  function pad(n) { return (n < 10 ? '0' : '') + n; }
  function iso(d) { return d.getFullYear() + '-' + pad(d.getMonth() + 1) + '-' + pad(d.getDate()); }
  function backfillChecked() {
    var cb = document.getElementById('pf-backfill');
    return !!(cb && cb.checked);
  }
  function refetch(start, end, backfill) {
    var target = bar.closest('.cc-panel-body') || bar.parentElement || document.body;
    var qs = [];
    if (start) qs.push('start_date=' + encodeURIComponent(start));
    if (end) qs.push('end_date=' + encodeURIComponent(end));
    if (backfill) qs.push('include_backfill=1');
    target.innerHTML = '<div class="cc-loading">Loading…</div>';
    fetch('/api/panel/portfolio' + (qs.length ? '?' + qs.join('&') : ''))
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
      .catch(function (e) {
        target.innerHTML = '<div class="cc-empty">Failed to load (' + e.message + ').</div>';
      });
  }
  bar.addEventListener('click', function (ev) {
    var btn = ev.target && ev.target.closest ? ev.target.closest('button[data-preset]') : null;
    if (!btn) return;
    var preset = btn.getAttribute('data-preset');
    if (preset === 'default') { refetch(null, null, backfillChecked()); return; }
    var end = new Date();
    var start;
    if (preset === 'ytd') {
      start = new Date(end.getFullYear(), 0, 1);
    } else {
      start = new Date(end.getTime());
      start.setMonth(start.getMonth() - parseInt(preset, 10));
    }
    refetch(iso(start), iso(end), backfillChecked());
  });
  document.getElementById('pf-apply').addEventListener('click', function () {
    refetch(
      document.getElementById('pf-start').value || null,
      document.getElementById('pf-end').value || null,
      backfillChecked()
    );
  });
})();
"""

# One-click tracker start (PR6). Plain string — braces are literal JS.
_START_TRACKER_JS = """
(function () {
  var btn = document.getElementById('pf-start-tracker');
  if (!btn || btn.dataset.wired) return;
  btn.dataset.wired = '1';
  var msg = document.getElementById('pf-start-msg');
  var log = document.getElementById('pf-start-log');
  function reinject(target, html) {
    target.innerHTML = html;
    var scripts = target.querySelectorAll('script');
    for (var i = 0; i < scripts.length; i++) {
      var old = scripts[i];
      var s = document.createElement('script');
      if (old.src) s.src = old.src; else s.textContent = old.textContent;
      old.parentNode.replaceChild(s, old);
    }
  }
  function pollPanel(tries) {
    if (tries <= 0) {
      msg.textContent = 'tracker still not reachable — check the log below';
      btn.disabled = false;
      return;
    }
    fetch('/api/panel/portfolio').then(function (r) { return r.text(); }).then(function (html) {
      if (html.indexOf('pf-live-offline') === -1) {
        var card = document.getElementById('pf-live-offline');
        var target = card ? card.closest('.cc-panel-body') : null;
        if (target) { reinject(target, html); } else { location.reload(); }
      } else {
        setTimeout(function () { pollPanel(tries - 1); }, 3000);
      }
    }).catch(function () { setTimeout(function () { pollPanel(tries - 1); }, 3000); });
  }
  btn.addEventListener('click', function () {
    btn.disabled = true;
    msg.textContent = 'starting…';
    fetch('/actions/start-tracker', {
      method: 'POST', headers: {'Content-Type': 'application/json'}, body: '{}'
    }).then(function (r) { return r.json().then(function (j) { return {ok: r.ok, j: j}; }); })
      .then(function (res) {
        if (!res.ok) {
          msg.textContent = 'error: ' + (res.j.error || 'failed');
          btn.disabled = false;
          return;
        }
        msg.textContent = 'starting — waiting for :8000 to answer…';
        log.style.display = 'block';
        try {
          var es = new EventSource(res.j.stream_url);
          es.onmessage = function (ev) {
            try {
              var f = JSON.parse(ev.data);
              if (f.line) { log.textContent += f.line + '\\n'; log.scrollTop = log.scrollHeight; }
              if (f.event === 'done') { es.close(); }
            } catch (e) {}
          };
        } catch (e) {}
        pollPanel(30);
      }).catch(function () { msg.textContent = 'network error'; btn.disabled = false; });
  });
})();
"""

# Preset keys are months-back (parsed by the JS), plus ytd/default specials.
_WINDOW_PRESETS: tuple[tuple[str, str], ...] = (
    ("1", "1M"),
    ("3", "3M"),
    ("6", "6M"),
    ("ytd", "YTD"),
    ("12", "1Y"),
    ("24", "2Y"),
    ("default", "Default"),
)


def _window_bar(w: WindowSelection) -> str:
    """The window selector: preset buttons, custom date inputs (echoing the
    applied window), the modeled-backfill toggle, and the refetch script.
    Always rendered — when the tracker is down it doubles as a retry control."""
    buttons = "".join(
        f'<button type="button" class="pf-btn" data-preset="{key}">{label}</button>'
        for key, label in _WINDOW_PRESETS
    )
    checked = " checked" if w.include_backfill else ""
    backfill_tip = (
        "Extend /performance backward through the tracker's transaction walk-back "
        "(up to ~24 months). Backfilled values are MODELED, not observed — they can "
        "drift on incomplete transactions or unrecorded transfers."
    )
    return (
        '<div class="pf-window" id="pf-window-bar">'
        '<span class="pf-window-label">Window</span>'
        f"{buttons}"
        f'<input type="date" id="pf-start" value="{escape(w.start_date or "")}" '
        'aria-label="window start date">'
        '<span class="muted">→</span>'
        f'<input type="date" id="pf-end" value="{escape(w.end_date or "")}" '
        'aria-label="window end date">'
        '<button type="button" class="pf-btn pf-btn-apply" id="pf-apply">Apply</button>'
        f'<label class="pf-backfill-label" title="{escape(backfill_tip)}">'
        f'<input type="checkbox" id="pf-backfill"{checked}> modeled backfill</label>'
        "</div>"
        f"<script>{_WINDOW_JS}</script>"
    )


def render_portfolio_analytics_sections(a: PortfolioAnalytics) -> str:
    """Every analytics section that loaded, in page order, plus one footnote
    naming the sections that didn't (instead of five dead panels). The shared
    ``<style>`` block is emitted by ``compose_portfolio_page`` (the bar needs
    it even when no section loaded)."""
    out: list[str] = []
    if a.performance is not None:
        out.append(_performance_section(a.performance, a.policy))
    if a.beta is not None:
        out.append(_risk_section(a.beta))
    if a.positioning is not None:
        out.append(_positioning_section(a.positioning))
    if a.position_alpha is not None:
        out.append(_alpha_section(a.position_alpha))
    failed = [label for key, label in _SECTION_LABELS.items() if key in a.errors]
    if failed:
        out.append(
            '<p class="muted pf-degraded">Unavailable from the tracker right now: '
            f"{escape(', '.join(failed))}.</p>"
        )
    return "".join(out)


def _performance_section(perf: PerformanceSeries, policy: PolicyMix | None) -> str:
    window = f"{perf.start_date or '?'} → {perf.end_date or '?'}"
    head = (
        '<section class="panel"><h2>Performance vs benchmarks</h2>'
        '<p class="sub">Time-weighted return (Modified Dietz) from the tracker · each '
        "benchmark is a synthetic book receiving the same external cashflows · net external "
        f"inflow {_money(perf.net_external_cashflow_in)} over the window.</p>"
    )
    if not perf.points:
        return (
            f"{head}"
            '<p class="muted">Tracker returned no performance history for the window.</p>'
            f"{_policy_line(policy)}</section>"
        )

    finals: dict[str, float | None] = {
        label: next((v for p in reversed(perf.points) if (v := get(p)) is not None), None)
        for label, _color, _sw, get in _CHART_SPECS
    }
    cards: list[str] = [
        _kpi_card(
            "Portfolio TWR",
            _pct(finals["Portfolio"], signed=True),
            sub=window,
            tone=_tone(finals["Portfolio"]),
        )
    ]
    # Display delta of two tracker-computed returns (the dollar/regression alpha
    # readouts come from /position-alpha and /beta — never recomputed here).
    excess = (
        finals["Portfolio"] - finals["SPY"]
        if finals["Portfolio"] is not None and finals["SPY"] is not None
        else None
    )
    cards.append(_kpi_card("vs SPY", _pp(excess), sub="excess return", tone=_tone(excess)))
    for bench in ("SPY", "QQQ", "Policy"):
        if finals[bench] is not None:
            cards.append(_kpi_card(bench, _pct(finals[bench], signed=True), sub="cashflow-matched"))
    warn = (
        '<p class="muted">⚠ The window start value looks incomplete (backfill unreliable) — '
        "early benchmark gaps may overstate or understate relative performance.</p>"
        if perf.backfill_start_unreliable
        else ""
    )
    return (
        f"{head}"
        f'<div class="kpi-strip">{"".join(cards)}</div>'
        f"{_chart_legend(perf.points)}"
        f"{_benchmark_chart(perf.points)}"
        f"{_policy_line(policy)}"
        f"{warn}</section>"
    )


def _chart_legend(points: list[PerformancePoint]) -> str:
    chips: list[str] = []
    for label, color, _sw, get in _CHART_SPECS:
        final = next((v for p in reversed(points) if (v := get(p)) is not None), None)
        if final is None:
            continue
        chips.append(
            f'<span class="pf-chip"><span class="pf-swatch" style="background:{color}"></span>'
            f"{escape(label)} <strong>{_pct(final, signed=True)}</strong></span>"
        )
    return f'<div class="pf-legend">{"".join(chips)}</div>' if chips else ""


def _benchmark_chart(points: list[PerformancePoint]) -> str:
    """Static multi-series SVG of cumulative window return %. Presentation only:
    the values are plotted exactly as the tracker returned them (a light stride
    keeps the fragment small on year-long daily series; endpoints always kept)."""
    if len(points) > 240:
        stride = -(-len(points) // 240)  # ceil division
        sampled = points[::stride]
        if sampled[-1] is not points[-1]:
            sampled.append(points[-1])
        points = sampled
    series: list[tuple[str, str, float, list[tuple[int, float]]]] = []
    for label, color, sw, get in _CHART_SPECS:
        coords = [(i, v) for i, p in enumerate(points) if (v := get(p)) is not None]
        if len(coords) >= 2:
            series.append((label, color, sw, coords))
    if not series:
        return ""

    all_vals = [v for _label, _color, _sw, coords in series for _i, v in coords]
    lo = min(min(all_vals), 0.0)  # keep the 0% line in frame
    hi = max(max(all_vals), 0.0)
    pad = (hi - lo or 1.0) * 0.08
    y0, y1 = lo - pad, hi + pad
    width, height = 860.0, 240.0
    pad_t, pad_r, pad_b, pad_l = 10.0, 14.0, 22.0, 46.0
    plot_w, plot_h = width - pad_l - pad_r, height - pad_t - pad_b
    n = len(points)

    def x_of(i: int) -> float:
        return pad_l + (i / max(n - 1, 1)) * plot_w

    def y_of(v: float) -> float:
        return pad_t + plot_h - ((v - y0) / (y1 - y0)) * plot_h

    parts: list[str] = [
        f'<svg class="pf-chart" viewBox="0 0 {width:.0f} {height:.0f}" role="img" '
        'aria-label="Cumulative time-weighted return vs SPY, QQQ, and policy benchmarks">'
    ]
    for frac in (0.0, 1 / 3, 2 / 3, 1.0):
        tick = y0 + frac * (y1 - y0)
        ty = y_of(tick)
        parts.append(
            f'<line x1="{pad_l:.1f}" x2="{pad_l + plot_w:.1f}" y1="{ty:.1f}" y2="{ty:.1f}" '
            'stroke="var(--border)" stroke-width="0.5" stroke-dasharray="2 3" />'
        )
        parts.append(
            f'<text x="{pad_l - 6:.1f}" y="{ty + 3:.1f}" text-anchor="end" font-size="9.5" '
            f'fill="var(--muted)" font-family="var(--mono)">{tick:.0f}%</text>'
        )
    if y0 < 0.0 < y1:
        zy = y_of(0.0)
        parts.append(
            f'<line x1="{pad_l:.1f}" x2="{pad_l + plot_w:.1f}" y1="{zy:.1f}" y2="{zy:.1f}" '
            'stroke="var(--border-2)" stroke-width="0.8" />'
        )
    anchors = {0: "start", n // 2: "middle", n - 1: "end"}
    for i, anchor in anchors.items():
        parts.append(
            f'<text x="{x_of(i):.1f}" y="{height - 6:.1f}" text-anchor="{anchor}" '
            'font-size="9.5" fill="var(--muted)" font-family="var(--mono)">'
            f"{escape(points[i].date)}</text>"
        )
    for label, color, sw, coords in series:
        d = " ".join(
            ("M" if j == 0 else "L") + f"{x_of(i):.1f},{y_of(v):.1f}"
            for j, (i, v) in enumerate(coords)
        )
        parts.append(
            f'<path d="{d}" fill="none" stroke="{color}" stroke-width="{sw}" '
            f'stroke-linejoin="round" stroke-linecap="round"><title>{escape(label)}</title>'
            "</path>"
        )
    parts.append("</svg>")
    return "".join(parts)


def _policy_line(policy: PolicyMix | None) -> str:
    """The policy benchmark's target mix, as context for the policy line/cards."""
    if policy is None or not policy.weights:
        return ""
    chips = " · ".join(
        f"{escape(w.ticker)} {_pct(w.weight_pct, decimals=0)}" for w in policy.weights
    )
    warn = ""
    if not policy.is_balanced:
        warn = (
            f' <span class="pf-warn">(weights sum to {_pct(policy.total_pct, decimals=0)} '
            "— unbalanced)</span>"
        )
    return f'<p class="pf-policy muted">Policy mix: {chips}{warn}</p>'


def _risk_section(b: BetaStats) -> str:
    bench = b.benchmark or "SPY"
    rf = f" · risk-free {_pct_frac(b.risk_free_annual)}" if b.risk_free_annual is not None else ""
    samples = f" · {b.sample_size} daily samples" if b.sample_size is not None else ""
    cards = [
        _kpi_card(f"Beta vs {bench}", _ratio(b.beta)),
        _kpi_card(
            "Alpha (ann.)",
            _pct(b.alpha_annualized_pct, signed=True),
            tone=_tone(b.alpha_annualized_pct),
        ),
        _kpi_card("Sharpe", _ratio(b.sharpe)),
        _kpi_card("Sortino", _ratio(b.sortino)),
        _kpi_card("Info ratio", _ratio(b.information_ratio)),
        _kpi_card("Tracking error", _pct_frac(b.tracking_error_annualized), sub="annualized"),
        _kpi_card(
            f"Portfolio {_SIGMA}",
            _pct_frac(b.portfolio_volatility_annualized),
            sub=f"{bench} {_SIGMA} {_pct_frac(b.benchmark_volatility_annualized)}",
        ),
        _kpi_card("R²", _ratio(b.r_squared)),
    ]
    notes = f'<p class="muted">{escape("; ".join(b.notes))}</p>' if b.notes else ""
    return (
        '<section class="panel"><h2>Risk &amp; efficiency</h2>'
        f'<p class="sub">Daily-return regression vs {escape(bench)} from the tracker · '
        f"{escape(b.start_date or '?')} → {escape(b.end_date or '?')}{samples}{rf}.</p>"
        f'<div class="kpi-strip">{"".join(cards)}</div>'
        f"{notes}</section>"
    )


def _positioning_section(pos: Positioning) -> str:
    cards: list[str] = []
    conc = pos.concentration
    if conc is not None:
        if conc.num_positions is not None:
            cards.append(_kpi_card("Positions", str(conc.num_positions)))
        cards.append(_kpi_card("Top 1", _pct(conc.top1_weight_pct), sub="of book"))
        cards.append(_kpi_card("Top 5", _pct(conc.top5_weight_pct), sub="of book"))
        cards.append(_kpi_card("Top 10", _pct(conc.top10_weight_pct), sub="of book"))
        if conc.hhi is not None:
            cards.append(_kpi_card("HHI", f"{conc.hhi:,.0f}", sub="of 10,000"))
        if conc.effective_holdings is not None:
            cards.append(
                _kpi_card(
                    "Effective holdings",
                    _ratio(conc.effective_holdings, decimals=1),
                    sub="equal-position equivalent",
                )
            )
    if pos.weighted_avg_correlation_spy is not None:
        cards.append(
            _kpi_card(
                "Avg corr vs SPY",
                _ratio(pos.weighted_avg_correlation_spy),
                sub="value-weighted",
            )
        )
    blocks = "".join(
        _alloc_block(title, buckets)
        for title, buckets in (
            ("By asset type", pos.by_asset_type),
            ("By sector", pos.by_sector),
            ("By region", pos.by_region),
            ("By account type", pos.by_account_type),
        )
    )
    strip = f'<div class="kpi-strip">{"".join(cards)}</div>' if cards else ""
    grid = f'<div class="pf-alloc-grid">{blocks}</div>' if blocks else ""
    return (
        '<section class="panel"><h2>Positioning &amp; concentration</h2>'
        f'<p class="sub">Snapshot {escape(pos.snapshot_date or "?")} · '
        f"book {_money(pos.total_value)} · weights from the tracker's classification.</p>"
        f"{strip}{grid}</section>"
    )


def _alloc_block(title: str, buckets: list[AllocationBucket]) -> str:
    if not buckets:
        return ""
    rows: list[str] = []
    for b in sorted(buckets, key=lambda x: -(x.weight_pct or 0.0)):
        width = max(0.0, min(100.0, b.weight_pct or 0.0))
        tip = f"{b.label} · {b.count} name(s)" if b.count is not None else b.label
        rows.append(
            '<div class="pf-alloc-row">'
            f'<span class="pf-alloc-label" title="{escape(tip)}">{escape(b.label)}</span>'
            f'<span class="pf-bar"><span class="pf-bar-fill" style="width:{width:.1f}%">'
            "</span></span>"
            f'<span class="pf-alloc-pct">{_pct(b.weight_pct)}</span>'
            f'<span class="pf-alloc-val muted">{_money(b.value)}</span>'
            "</div>"
        )
    return f'<div class="pf-alloc-block"><h3 class="panel-h3">{escape(title)}</h3>{"".join(rows)}</div>'


def _alpha_section(pa: PositionAlpha) -> str:
    head = (
        '<section class="panel"><h2>Per-position alpha</h2>'
        f'<p class="sub">{escape(pa.start_date or "?")} → {escape(pa.end_date or "?")} · '
        "dollar alpha vs a counterfactual that routes each position's exact buys/sells into "
        f"the benchmark on the same days ({_ALPHA} = actual P&amp;L - benchmark P&amp;L).</p>"
    )
    if not pa.rows:
        return f'{head}<p class="muted">Tracker returned no positions for the window.</p></section>'
    show_policy = pa.has_policy
    policy_th = f'<th class="num">{_ALPHA} vs policy</th>' if show_policy else ""
    rows: list[str] = []
    for r in sorted(pa.rows, key=lambda x: (x.alpha is None, -(x.alpha or 0.0))):
        ticker = r.ticker or "—"
        ticker_cell = (
            f'<a href="../research/{escape(ticker)}/" class="ticker-link">{escape(ticker)}</a>'
            if r.ticker
            else "—"
        )
        if r.incomplete:
            ticker_cell += (
                '<span class="pf-flag" title="window start could not be fully reconstructed '
                '— row is approximate">⚠</span>'
            )
        policy_td = _money_cell(r.alpha_vs_policy, colored=True) if show_policy else ""
        rows.append(
            "<tr>"
            f"<td>{ticker_cell}</td>"
            f"<td>{escape(r.name or '—')}</td>"
            f"{_money_cell(r.value_at_end)}"
            f"{_money_cell(r.actual_pl, colored=True)}"
            f"{_money_cell(r.spy_counterfactual_pl)}"
            f"{_money_cell(r.alpha, colored=True)}"
            f"{_money_cell(r.alpha_vs_qqq, colored=True)}"
            f"{policy_td}"
            "</tr>"
        )
    policy_total = _money_cell(pa.total_alpha_vs_policy, colored=True) if show_policy else ""
    totals = (
        '<tr class="pf-total"><td>Total</td><td></td><td class="num"></td>'
        f"{_money_cell(pa.total_actual_pl, colored=True)}"
        f"{_money_cell(pa.total_spy_pl)}"
        f"{_money_cell(pa.total_alpha, colored=True)}"
        f"{_money_cell(pa.total_alpha_vs_qqq, colored=True)}"
        f"{policy_total}</tr>"
    )
    return (
        f"{head}"
        '<table class="alpha-table"><thead><tr>'
        "<th>Ticker</th><th>Name</th>"
        '<th class="num">Value</th><th class="num">P&amp;L</th>'
        f'<th class="num">SPY P&amp;L</th><th class="num">{_ALPHA} vs SPY</th>'
        f'<th class="num">{_ALPHA} vs QQQ</th>{policy_th}'
        "</tr></thead><tbody>"
        f"{''.join(rows)}"
        f"</tbody><tfoot>{totals}</tfoot></table></section>"
    )


def _kpi_card(label: str, value: str, *, sub: str = "", tone: str = "") -> str:
    sub_html = f'<div class="kpi-sub">{escape(sub)}</div>' if sub else ""
    cls = f" {tone}" if tone else ""
    return (
        f'<div class="kpi-card{cls}"><div class="kpi-label">{escape(label)}</div>'
        f'<div class="kpi-value">{value}</div>{sub_html}</div>'
    )


def _tone(v: float | None) -> str:
    """kpi-card / table-cell modifier: green when favorable, red when not."""
    if v is None:
        return ""
    return "pos" if v >= 0 else "neg"


def _money_cell(v: float | None, *, colored: bool = False) -> str:
    if v is None:
        return '<td class="num muted">—</td>'
    cls = "num" + (f" {_tone(v)}" if colored else "")
    return f'<td class="{cls}">{_money(v)}</td>'


def _pct(v: float | None, *, signed: bool = False, decimals: int = 1) -> str:
    """A value already in PERCENT units (the tracker's ``*_pct`` fields)."""
    if v is None:
        return "—"
    return f"{v:+.{decimals}f}%" if signed else f"{v:.{decimals}f}%"


def _pp(v: float | None) -> str:
    """A spread of two percent values, in signed percentage points."""
    return "—" if v is None else f"{v:+.1f}pp"


def _pct_frac(v: float | None, decimals: int = 1) -> str:
    """A FRACTION (0.18 = 18%) rendered as percent — the tracker's volatility /
    tracking-error / risk-free fields, unlike its ``*_pct`` fields."""
    return "—" if v is None else f"{v * 100.0:.{decimals}f}%"


def _ratio(v: float | None, decimals: int = 2) -> str:
    return "—" if v is None else f"{v:.{decimals}f}"


def _offline_reason(error: str | None) -> str:
    """One humane sentence for the offline card; the raw error stays in the
    details element for whoever actually wants the stack-shaped version."""
    low = (error or "").lower()
    if "timed out" in low or "timeout" in low:
        return "The tracker API didn't respond — it looks like the server isn't up."
    if "refused" in low or "failed to establish" in low:
        return "Nothing is listening on the tracker port — the server isn't running."
    if not error:
        return "It wasn't reachable on the last check."
    return "The tracker API request failed on the last check."


# ---------------------------------------------------------------------------
# Portfolio → Synthesis tab (UX round 4; grew out of the PR6 insights strip
# that used to ride the bottom of Performance): thesis-health rollup + sector
# exposure in a grid up top, the next-dollar distribution full-width as the
# centerpiece, the cross-portfolio lens memo below. Each panel hides itself
# when its substrate is absent (hide-don't-stub).
# ---------------------------------------------------------------------------


def render_portfolio_synthesis_panel(db_path: Path, *, api_url: str | None = None) -> str:
    """The Portfolio → Synthesis tab fragment. Fetches the live book once (the
    exposure weighting and the next-dollar model prefer live position weights
    and fall back to equal-weight when the tracker is down — no offline card
    here, Performance carries it) plus the cached ``cross_portfolio_synthesis``
    lens memo, then assembles the page."""
    # Lazy imports keep the analytical builder out of this module's import graph
    # until the panel is actually requested.
    from pipeline.analytical_dashboard import build_analytical_dashboard
    from pipeline.analytical_dashboard_html import render_panel_fragment

    live = fetch_live_portfolio(api_url=api_url)
    dash = build_analytical_dashboard(db_path, sections={"portfolio_synthesis"})
    memo = render_panel_fragment(dash, "portfolio") or ""
    return compose_synthesis_page(db_path, live, memo)


def compose_synthesis_page(db_path: Path, live: LivePortfolio, synthesis: str) -> str:
    """Page assembly over an already-fetched live book + lens-memo fragment
    (testable without network; the insight panels read the DB themselves):
    the rollup/exposure grid, then the next-dollar distribution full-width,
    then the memo."""
    grid = "".join(p for p in (_thesis_rollup_panel(db_path), _exposure_panel(db_path, live)) if p)
    parts: list[str] = [_INSIGHTS_CSS]
    if grid:
        parts.append(f'<div class="pf-insights">{grid}</div>')
    parts.append(render_next_dollar_panel(db_path, live))
    parts.append(synthesis)
    return "".join(parts)


def _portfolio_tickers(conn: sqlite3.Connection) -> list[str]:
    try:
        rows = conn.execute(
            "SELECT ticker FROM tracked_companies "
            "WHERE list_type = 'portfolio' AND archived_at IS NULL ORDER BY ticker"
        ).fetchall()
    except sqlite3.Error:
        return []
    return [str(r[0]).upper() for r in rows]


def _thesis_rollup_panel(db_path: Path) -> str:
    """One line of portfolio-level thesis health: OK / WARN / BREACH counts,
    with the non-OK names as chips deep-linking into their Holding tab."""
    if not db_path.exists():
        return ""
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.Error:
        return ""
    try:
        tickers = _portfolio_tickers(conn)
        if not tickers:
            return ""
        latest: dict[str, str] = {}
        try:
            rows = conn.execute(
                "SELECT ticker, overall_status FROM thesis_evaluations "
                "ORDER BY ticker, evaluated_at DESC"
            ).fetchall()
        except sqlite3.Error:
            return ""
        for r in rows:
            t = str(r[0]).upper()
            if t in tickers and t not in latest and r[1]:
                latest[t] = str(r[1]).lower()
    finally:
        conn.close()
    if not latest:
        return ""
    tones = {"ok": "ok", "intact": "ok", "watch": "warn", "warn": "warn"}
    flagged = [(t, s) for t, s in sorted(latest.items()) if tones.get(s, "bad") != "ok"]
    ok_n = len(latest) - len(flagged)
    chips = "".join(
        # data-peek-ticker: the chip text carries " · status", so the hover
        # mini-card needs the bare symbol spelled out (UX9).
        f'<a class="pf-th-chip pf-th-{tones.get(s, "bad")}" href="#holding={escape(t)}" '
        f'data-peek-ticker="{escape(t)}">'
        f"{escape(t)} · {escape(s)}</a>"
        for t, s in flagged
    )
    summary = f"{ok_n} OK · {len(flagged)} flagged" if flagged else f"all {ok_n} OK"
    return (
        '<section class="panel"><h2>Thesis health</h2>'
        f'<p class="sub">Latest evaluation across the portfolio — {escape(summary)}.</p>'
        + (f'<div class="pf-th-chips">{chips}</div>' if chips else "")
        + "</section>"
    )


def _exposure_panel(db_path: Path, live: LivePortfolio) -> str:
    """Sector exposure across the book: position-weighted when the tracker is
    up, name counts otherwise. Sectors come from the cached FMP profiles."""
    if not db_path.exists():
        return ""
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.Error:
        return ""
    try:
        tickers = _portfolio_tickers(conn)
    finally:
        conn.close()
    if not tickers:
        return ""
    repo_root = db_path.parent.parent
    weights: dict[str, float] = {}
    if live.available and live.positions:
        total = sum(p.market_value or 0.0 for p in live.positions) or 0.0
        if total > 0:
            for p in live.positions:
                if p.ticker:
                    weights[p.ticker.upper()] = (p.market_value or 0.0) / total
    by_sector: dict[str, float] = {}
    for t in tickers:
        profile = repo_root / "data" / "historical" / "fmp" / f"{t}_profile.json"
        sector = "Unclassified"
        if profile.exists():
            try:
                payload: object = json.loads(profile.read_text(encoding="utf-8"))
                rec: object = payload[0] if isinstance(payload, list) and payload else payload
                if isinstance(rec, dict):
                    raw = rec.get("sector")  # pyright: ignore[reportUnknownMemberType]
                    if isinstance(raw, str) and raw.strip():
                        sector = raw.strip()
            except (OSError, ValueError):
                pass
        by_sector[sector] = by_sector.get(sector, 0.0) + weights.get(t, 1.0 / len(tickers))
    if not by_sector:
        return ""
    mode = "weighted by live position" if weights else "equal-weighted (tracker offline)"
    top = sorted(by_sector.items(), key=lambda kv: kv[1], reverse=True)
    rows = "".join(
        '<div class="pf-exp-row">'
        f'<span class="pf-exp-label">{escape(sector)}</span>'
        f'<span class="pf-exp-bar"><span style="width:{share * 100:.0f}%"></span></span>'
        f'<span class="pf-exp-pct">{share * 100:.0f}%</span>'
        "</div>"
        for sector, share in top
    )
    return (
        '<section class="panel"><h2>Exposure</h2>'
        f'<p class="sub">By FMP sector · {escape(mode)}.</p>'
        f'<div class="pf-exp">{rows}</div></section>'
    )


def render_next_dollar_panel(db_path: Path, live: LivePortfolio | None = None) -> str:
    """Quantitative next-dollar allocation distribution over the holdings
    (src/allocation: DCF upside / diversification / macro tilt, z-scored,
    blended by visible weights, softmaxed — directives/next_dollar_model.md),
    with the latest advisor memo excerpted below as the narrative layer.
    Falls back to the memo alone when the model has nothing to score; hides
    entirely when neither exists. Public for the markup-contract tests
    (UX9 peeks); ``live`` is optional so those callers don't need a tracker
    snapshot (None means no live weights — the model goes equal-weight)."""
    if not db_path.exists():
        return ""
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.Error:
        return ""
    memo: tuple[str, str, str] | None = None
    try:
        tickers = _portfolio_tickers(conn)
        try:
            row = conn.execute(
                "SELECT title, body_md, created_at FROM advisor_memos "
                "WHERE kind = 'next_dollar' ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
        except sqlite3.Error:
            row = None
        if row is not None:
            memo = (str(row[0]), str(row[1]), str(row[2]))
    finally:
        conn.close()

    model: NextDollarModel | None = None
    if tickers:
        live_values: dict[str, float | None] | None = None
        if live is not None and live.available and live.positions:
            live_values = {p.ticker.upper(): p.market_value for p in live.positions if p.ticker}
        model = build_next_dollar_model(db_path, db_path.parent.parent, tickers, live_values)

    model_html = _next_dollar_distribution(model) if model is not None else ""
    memo_html = _next_dollar_memo(memo, with_heading=bool(model_html))
    if not model_html and not memo_html:
        return ""
    return (
        '<section class="panel"><h2>Where the next dollar goes</h2>'
        f"{model_html}{memo_html}</section>"
    )


def _next_dollar_distribution(model: NextDollarModel) -> str:
    """The allocation bars + per-holding factor waterfall (hover/focus) +
    the model's provenance sub-line. Pure HTML over an already-built model."""
    # round(..., 6) first: 0.30/0.80 floats to 37.4999…, which ':.0f' alone
    # would render as 37% (and the blend line would sum to 99%).
    blend = " / ".join(f"{FACTOR_LABELS[k]} {round(w * 100.0, 6):.0f}%" for k, w in model.blend)
    bits = [f"blend {blend}"]
    bits.append("tracker-weighted" if model.weights_source == "tracker" else "equal-weighted")
    if model.prices_through is not None:
        bits.append(f"daily returns through {model.prices_through.isoformat()}")
    if model.cov_obs:
        bits.append(f"{model.cov_obs}d window")
    if model.portfolio_vol_ann is not None:
        bits.append(f"book vol {model.portfolio_vol_ann * 100.0:.0f}%/yr")
    if model.shrinkage is not None:
        bits.append(f"LW shrink {model.shrinkage:.2f}")
    sub = f'<p class="sub">Softmax over blended z-scores · {escape(" · ".join(bits))}.</p>'

    warn_lines = [
        f"{FACTOR_LABELS[k]} hidden — {reason}" for k, reason in model.hidden_factors.items()
    ]
    warn_lines.extend(f"{t} not scored — {reason}" for t, reason in sorted(model.excluded.items()))
    warn_lines.extend(model.notes)
    warns = "".join(f'<p class="muted pf-nd-note">{escape(w)}</p>' for w in warn_lines)

    active = {k for k, _w in model.blend}
    max_alloc = max((r.allocation_pct for r in model.rows), default=0.0) or 1.0
    items: list[str] = []
    for r in model.rows:
        width = max(0.0, min(100.0, r.allocation_pct / max_alloc * 100.0))
        chips: list[str] = []
        for key in ("ret", "div", "macro"):
            if key not in active:
                continue
            label = FACTOR_LABELS[key]
            reading = r.reading(key)
            if reading is None:
                chips.append(
                    f'<span class="pf-nd-chip" title="no {escape(label)} data for this '
                    f'holding — its blend renormalizes over the rest">{escape(label)} —</span>'
                )
                continue
            tone = " pos" if reading.contribution >= 0 else " neg"
            tip = (
                f"z {reading.z:+.2f} · weight {round(reading.weight * 100.0, 6):.0f}% · "
                f"raw {reading.raw * 100.0:+.1f}% · {reading.detail}"
            )
            chips.append(
                f'<span class="pf-nd-chip{tone}" title="{escape(tip)}">'
                f"{escape(label)} {reading.contribution:+.2f}</span>"
            )
        ticker = escape(r.ticker)
        items.append(
            '<div class="pf-nd-item" tabindex="0">'
            '<div class="pf-nd-row">'
            f'<a class="pf-nd-ticker ticker-link" href="../research/{ticker}/">{ticker}</a>'
            f'<span class="pf-nd-bar"><span style="width:{width:.1f}%"></span></span>'
            f'<span class="pf-nd-alloc">{r.allocation_pct:.1f}%</span>'
            f'<span class="pf-nd-now muted">now {r.current_weight_pct:.1f}%</span>'
            "</div>"
            f'<div class="pf-nd-wf">{"".join(chips)}</div>'
            "</div>"
        )
    hint = (
        '<p class="muted pf-nd-hint">Hover or focus a row for the factor waterfall '
        "(blend weight x z per factor; raw values in the tooltip).</p>"
    )
    return f"{sub}{warns}{''.join(items)}{hint}"


def _next_dollar_memo(memo: tuple[str, str, str] | None, *, with_heading: bool) -> str:
    """The latest next-dollar advisor memo, excerpted, deep-linking into the
    Memos tab. Gets its own sub-heading when it sits under the distribution."""
    if memo is None:
        return ""
    title, body_md, created_at = memo
    text = " ".join(body_md.replace("#", " ").replace("*", " ").split())
    excerpt = text[:420] + ("…" if len(text) > 420 else "")
    meta = (
        f'<p class="sub">{escape(title)} · {stamp_html(created_at, mode="date")} · '
        # Peeks the rendered memo in place (UX9); the hash href still lands on
        # the Memos tab for middle-click / non-shell surfaces.
        '<a href="#advisor_memos" data-peek-url="/api/peek/memo/next_dollar" '
        'data-peek-title="Next-dollar memo">full memo →</a></p>'
    )
    body = f'<p class="pf-nd-excerpt">{escape(excerpt)}</p>'
    heading = '<h3 class="panel-h3 pf-nd-memo-h">Advisor memo</h3>' if with_heading else ""
    return f"{heading}{meta}{body}"


# ---------------------------------------------------------------------------
# Portfolio → Risk tab (L5 — the whole-book risk cockpit). The
# performance/risk pillar's high-severity gaps in one panel:
#   * book DRAWDOWN (max DD + underwater curve + time-to-recovery) computed
#     client-side from the tracker's daily TWR series (no drawdown endpoint),
#   * FACTOR/STYLE exposure rolled up from the per-ticker correlation/beta rows
#     the client used to discard (+ a 10Y rate leg from macro_sensitivities),
#   * RISK vs REWARD vs CONVICTION (L7 — the risk-budget allocator): each name's
#     share of book risk (marginal contribution off the shrunk covariance) set
#     against its share of the book's asymmetry-aware expected DCF reward and its
#     recorded conviction, flagging the parity gaps,
#   * the whole-book MACRO STRESS lens (CLI-only before) surfaced with a
#     scenario picker that fires execution/run_scenario.py over an SSE job.
# Macro stress reads the local cache, so it renders even with the tracker down;
# drawdown + factor + the risk/reward gap degrade to an offline note (the
# cached-snapshot path lands alongside, L5 PR2).
# ---------------------------------------------------------------------------

_RISK_CSS = """<style>
.pfr-uw { width: 100%; height: auto; display: block; margin-top: 6px; }
.pfr-top { font-size: var(--fs-caption); color: var(--muted); margin: 6px 0 0; }
.pfr-tops { margin-top: 8px; }
.pfr-run { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; margin: 4px 0 12px; }
.pfr-run select { font-size: var(--fs-body); }
.pfr-log { display: none; max-height: 160px; overflow: auto; margin: 0 0 12px; }
.rrg-table { margin-top: 4px; }
.rrg-mismatch { max-width: 460px; }
.rrg-chips { display: inline-flex; gap: 4px; flex-wrap: wrap; vertical-align: middle; }
.rrg-score { margin-right: 8px; }
</style>"""

# Fires the portfolio macro-stress lens (execution/run_scenario.py --portfolio)
# over the standard /actions/stream SSE channel, then re-fetches the Risk panel
# so the freshly-cached digest paints. Plain string — braces are literal JS.
# (Mirrors _START_TRACKER_JS's reinject-on-done idiom.)
_RUN_SCENARIO_JS = """
(function () {
  var btn = document.getElementById('pfr-run-scenario');
  if (!btn || btn.dataset.wired) return;
  btn.dataset.wired = '1';
  var sel = document.getElementById('pfr-scenario');
  var msg = document.getElementById('pfr-run-msg');
  var log = document.getElementById('pfr-run-log');
  function reinject(target, html) {
    target.innerHTML = html;
    var scripts = target.querySelectorAll('script');
    for (var i = 0; i < scripts.length; i++) {
      var old = scripts[i];
      var s = document.createElement('script');
      if (old.src) s.src = old.src; else s.textContent = old.textContent;
      old.parentNode.replaceChild(s, old);
    }
  }
  function reloadPanel() {
    fetch('/api/panel/portfolio_risk').then(function (r) { return r.text(); }).then(function (html) {
      var root = document.getElementById('pfr-root');
      var target = root ? root.closest('.cc-panel-body') : null;
      if (target) { reinject(target, html); } else { location.reload(); }
    }).catch(function () { btn.disabled = false; });
  }
  btn.addEventListener('click', function () {
    var scenario = sel ? sel.value : '';
    if (!scenario) { msg.textContent = 'pick a scenario'; return; }
    btn.disabled = true;
    msg.textContent = 'running… (LLM digest, ~10-40s)';
    log.style.display = 'block';
    fetch('/actions/run-scenario', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({scenario: scenario})
    }).then(function (r) { return r.json().then(function (j) { return {ok: r.ok, j: j}; }); })
      .then(function (res) {
        if (!res.ok) { msg.textContent = 'error: ' + (res.j.error || 'failed'); btn.disabled = false; return; }
        try {
          var es = new EventSource(res.j.stream_url);
          es.onmessage = function (ev) {
            try {
              var f = JSON.parse(ev.data);
              if (f.line) { log.textContent += f.line + '\\n'; log.scrollTop = log.scrollHeight; }
              if (f.event === 'done') { es.close(); msg.textContent = 'done — refreshing digest…'; reloadPanel(); }
            } catch (e) {}
          };
          es.onerror = function () { es.close(); btn.disabled = false; };
        } catch (e) { btn.disabled = false; }
      }).catch(function () { msg.textContent = 'network error'; btn.disabled = false; });
  });
})();
"""


def render_portfolio_risk_panel(
    *,
    api_url: str | None = None,
    db_path: Path | None = None,
) -> str:
    """The Portfolio → Risk tab fragment: book drawdown + factor/style exposure
    (from the live tracker) and the whole-book macro-stress lens (from the local
    artifact cache, with a scenario picker). The tracker-fed sections degrade to
    an offline note; the macro-stress section renders regardless."""
    analytics = fetch_portfolio_analytics(
        api_url=api_url, only={"performance", "positioning", "beta"}
    )
    drawdown = (
        compute_drawdown(analytics.performance.points)
        if analytics.performance is not None
        else None
    )
    factor: FactorRollup | None = None
    gap: RiskRewardGap | None = None
    if analytics.positioning is not None:
        rate_betas = _rate_betas(analytics.positioning.correlations, db_path)
        factor = factor_exposure_rollup(analytics.positioning.correlations, rate_betas)
        if db_path is not None:
            gap = _build_risk_reward_gap(analytics.positioning, db_path)
    scenarios = _scenario_options()
    digest = _cached_macro_digest_html(db_path) if db_path is not None else ""
    # On a successful read, refresh the last-known snapshot; when the tracker is
    # down, fall back to it so the surface degrades to stamped cached values.
    snapshot: RiskSnapshot | None = None
    if db_path is not None:
        if analytics.available:
            _persist_risk_snapshot(analytics, db_path, drawdown=drawdown, factor=factor)
        else:
            snapshot = read_latest_snapshot(db_path=db_path)
    return compose_risk_page(
        analytics,
        drawdown=drawdown,
        factor=factor,
        gap=gap,
        scenarios=scenarios,
        digest=digest,
        snapshot=snapshot,
    )


def _build_risk_reward_gap(pos: Positioning, db_path: Path) -> RiskRewardGap | None:
    """Assemble the risk-parity-gap table from the live book weights (the
    positioning endpoint's per-name weight_pct) — None when there are no
    weighted names to model. repo_root is the price cache's parent of the DB."""
    weights = {
        r.ticker.upper(): (r.weight_pct or 0.0) / 100.0
        for r in pos.correlations
        if r.ticker and r.weight_pct is not None
    }
    if not weights:
        return None
    return build_risk_reward_gap(db_path, db_path.parent.parent, weights, weights_source="tracker")


def compose_risk_page(
    analytics: PortfolioAnalytics,
    *,
    drawdown: DrawdownStats | None,
    factor: FactorRollup | None,
    scenarios: list[tuple[str, str]],
    digest: str,
    snapshot: RiskSnapshot | None = None,
    gap: RiskRewardGap | None = None,
) -> str:
    """Pure assembly of the Risk page (testable without network or DB). The
    ``#pfr-root`` wrapper is the re-inject target the run-scenario script swaps
    after a digest regenerates. When the tracker is offline, ``snapshot`` (the
    last-known cached read) renders stamped values instead of a bare note.
    ``gap`` (L7) is the risk-budget allocator's risk-vs-reward-vs-conviction
    ranking; None hides the section (tracker offline / no weighted book)."""
    parts: list[str] = [_RISK_CSS, '<div id="pfr-root">']
    if analytics.available:
        if analytics.beta is not None:
            parts.append(_risk_section(analytics.beta))
        parts.append(_drawdown_section(drawdown))
        if factor is not None:
            parts.append(_factor_exposure_section(factor))
        if gap is not None:
            parts.append(_risk_reward_gap_section(gap))
    elif snapshot is not None:
        parts.append(_cached_risk_section(snapshot))
    else:
        parts.append(_risk_offline_note(analytics))
    parts.append(_macro_stress_section(scenarios, digest))
    parts.append("</div>")
    return "".join(parts)


def _build_risk_snapshot(
    analytics: PortfolioAnalytics,
    drawdown: DrawdownStats | None,
    factor: FactorRollup | None,
) -> RiskSnapshot:
    """Flatten the live analytics + derived drawdown/factor into the cache row."""
    snap = RiskSnapshot()
    b = analytics.beta
    if b is not None:
        snap.window_start = b.start_date
        snap.window_end = b.end_date
        snap.benchmark = b.benchmark
        snap.beta = b.beta
        snap.alpha_annualized_pct = b.alpha_annualized_pct
        snap.sharpe = b.sharpe
        snap.sortino = b.sortino
        snap.information_ratio = b.information_ratio
        snap.tracking_error_annualized = b.tracking_error_annualized
        snap.portfolio_volatility_annualized = b.portfolio_volatility_annualized
        snap.r_squared = b.r_squared
    pos = analytics.positioning
    if pos is not None:
        snap.weighted_avg_correlation_spy = pos.weighted_avg_correlation_spy
        c = pos.concentration
        if c is not None:
            snap.num_positions = c.num_positions
            snap.top1_weight_pct = c.top1_weight_pct
            snap.top5_weight_pct = c.top5_weight_pct
            snap.top10_weight_pct = c.top10_weight_pct
            snap.hhi = c.hhi
            snap.effective_holdings = c.effective_holdings
    if drawdown is not None:
        snap.max_drawdown_pct = drawdown.max_drawdown_pct
        snap.current_drawdown_pct = drawdown.current_drawdown_pct
        snap.drawdown_recovered = 1 if drawdown.recovered else 0
        snap.days_to_recovery = drawdown.days_to_recovery
    if factor is not None:
        snap.spy_beta = factor.spy_beta
        snap.qqq_beta = factor.qqq_beta
        snap.growth_tilt = factor.growth_tilt
        snap.avg_correlation_spy = factor.avg_correlation_spy
        snap.rate_beta_10y = factor.rate_beta_10y
        snap.names_priced = factor.names_priced
        snap.names_total = factor.names_total
    return snap


def _persist_risk_snapshot(
    analytics: PortfolioAnalytics,
    db_path: Path,
    *,
    drawdown: DrawdownStats | None = None,
    factor: FactorRollup | None = None,
) -> None:
    """Refresh the last-known risk snapshot after a successful tracker fetch.

    No-op when the tracker is unavailable. ``drawdown`` / ``factor`` are passed
    through from the Risk panel (which already computed them, with the rate leg);
    the Performance panel leaves them None and this derives them inline — without
    the per-name macro DB read, so its hot render path stays cheap (the rate leg
    then rides whatever the Risk panel last wrote)."""
    if not analytics.available:
        return
    if drawdown is None and analytics.performance is not None:
        drawdown = compute_drawdown(analytics.performance.points)
    if factor is None and analytics.positioning is not None:
        factor = factor_exposure_rollup(analytics.positioning.correlations)
    write_snapshot(_build_risk_snapshot(analytics, drawdown, factor), db_path=db_path)


def _cached_risk_section(snap: RiskSnapshot) -> str:
    """The Risk panel's offline fallback: the last-known benchmark-risk, drawdown,
    and factor cards, every value clearly stamped as cached and dated."""
    stamp = (
        stamp_html(snap.captured_at, mode="date", prefix="as of ")
        if snap.captured_at
        else "last-known"
    )
    cards: list[str] = []
    if snap.beta is not None:
        cards.append(_kpi_card(f"Beta vs {snap.benchmark or 'SPY'}", _ratio(snap.beta)))
    if snap.sharpe is not None:
        cards.append(_kpi_card("Sharpe", _ratio(snap.sharpe)))
    if snap.portfolio_volatility_annualized is not None:
        cards.append(
            _kpi_card("Portfolio vol", _pct_frac(snap.portfolio_volatility_annualized), sub="ann.")
        )
    if snap.max_drawdown_pct is not None:
        cards.append(
            _kpi_card(
                "Max drawdown",
                _pct(snap.max_drawdown_pct, signed=True),
                tone=_tone(snap.max_drawdown_pct),
            )
        )
    if snap.spy_beta is not None:
        cards.append(_kpi_card("Market β (SPY)", _ratio(snap.spy_beta), sub="value-weighted"))
    if snap.qqq_beta is not None:
        cards.append(_kpi_card("Growth β (QQQ)", _ratio(snap.qqq_beta), sub="value-weighted"))
    if snap.rate_beta_10y is not None:
        cards.append(_kpi_card("Rate β (10Y)", _ratio(snap.rate_beta_10y), sub="vs 10Y yield"))
    if snap.top5_weight_pct is not None:
        cards.append(_kpi_card("Top 5", _pct(snap.top5_weight_pct), sub="of book"))
    strip = f'<div class="kpi-strip">{"".join(cards)}</div>' if cards else ""
    return (
        '<section class="panel"><h2>Risk &amp; drawdown</h2>'
        '<p class="sub">Live risk analytics are unavailable right now — showing the last-known '
        f"snapshot ({stamp}). These are cached values, not live; reconnect the tracker for live "
        "drawdown, factor, and benchmark-risk reads.</p>"
        f"{strip}</section>"
    )


def _rate_betas(rows: list[PositionCorrelationRow], db_path: Path | None) -> dict[str, float]:
    """Per-ticker 10Y-yield beta from the local ``macro_sensitivities`` table —
    the rate-sensitivity leg of the factor roll-up. ``{}`` when the table or DB
    is absent (the leg then hides itself)."""
    if db_path is None or not db_path.exists():
        return {}
    try:
        from macro_store import fetch_sensitivities
    except ImportError:
        return {}
    out: dict[str, float] = {}
    for r in rows:
        if not r.ticker:
            continue
        try:
            sens = fetch_sensitivities(ticker=r.ticker, db_path=db_path)
        except Exception:  # defensive on a render path; absence is fine
            continue
        for s in sens:
            if s.series_id == "us_10y":
                out[r.ticker.upper()] = s.beta
                break
    return out


def _scenario_options() -> list[tuple[str, str]]:
    """(id, title) pairs for the scenario picker, in registry order."""
    from macro_scenarios import SCENARIOS, all_scenario_ids

    return [(sid, SCENARIOS[sid].title) for sid in all_scenario_ids()]


def _cached_macro_digest_html(db_path: Path) -> str:
    """The most recent cached portfolio macro-stress digest as rendered HTML
    (heading + scenario title + 'as of' stamp + prose), or '' when none exists."""
    if not db_path.exists():
        return ""
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.Error:
        return ""
    try:
        row = conn.execute(
            "SELECT purpose, content_md, generated_at FROM llm_artifacts "
            "WHERE scope = 'portfolio' AND purpose LIKE 'lens:portfolio_macro_stress:%' "
            "AND superseded_by_id IS NULL AND content_md IS NOT NULL "
            "ORDER BY generated_at DESC LIMIT 1"
        ).fetchone()
    except sqlite3.Error:
        return ""
    finally:
        conn.close()
    if row is None:
        return ""
    purpose, content_md, generated_at = str(row[0]), str(row[1]), str(row[2])
    scenario_id = purpose.rsplit(":", 1)[-1]
    title = scenario_id
    try:
        from macro_scenarios import get as get_scenario

        sc = get_scenario(scenario_id)
        if sc is not None:
            title = sc.title
    except ImportError:
        pass
    from pipeline.analytical_dashboard_html import light_markdown_to_html

    meta = f'<p class="sub">Latest: {escape(title)} · {stamp_html(generated_at, mode="date")}</p>'
    return f'<h3 class="panel-h3">Stress digest</h3>{meta}{light_markdown_to_html(content_md)}'


def _risk_offline_note(analytics: PortfolioAnalytics) -> str:
    """Tracker down → drawdown / benchmark-risk / factor exposure can't be read
    live. (L5 PR2 swaps this for the last-known cached snapshot, stamped.)"""
    reason = _offline_reason(next(iter(analytics.errors.values()), None))
    return (
        '<section class="panel"><h2>Risk &amp; drawdown</h2>'
        '<p class="sub">Drawdown, benchmark-risk stats, and factor exposure come from the '
        "live portfolio tracker.</p>"
        f'<p class="muted">{escape(reason)}</p></section>'
    )


def _dd_window(dd: DrawdownStats) -> str:
    if dd.peak_date and dd.trough_date:
        return f"{dd.peak_date} → {dd.trough_date}"
    return "peak → trough"


def _drawdown_section(dd: DrawdownStats | None) -> str:
    head = (
        '<section class="panel"><h2>Drawdown</h2>'
        '<p class="sub">Peak-to-trough decline of the book\'s time-weighted return over the '
        "tracker's window — the worst loss ridden, and whether it has recovered.</p>"
    )
    if dd is None:
        return f'{head}<p class="muted">No daily return series available for a drawdown read.</p></section>'
    never_fell = dd.trough_date is None
    cards = [
        _kpi_card(
            "Max drawdown",
            _pct(dd.max_drawdown_pct, signed=True),
            sub="no drawdown in window" if never_fell else _dd_window(dd),
            tone="" if never_fell else _tone(dd.max_drawdown_pct),
        ),
        _kpi_card(
            "Current drawdown",
            _pct(dd.current_drawdown_pct, signed=True),
            sub="at a high" if dd.current_drawdown_pct >= -0.05 else "below peak",
            tone="" if dd.current_drawdown_pct >= -0.05 else "neg",
        ),
    ]
    if never_fell:
        cards.append(_kpi_card("Recovery", "none needed", sub="never below peak", tone="pos"))
    elif dd.recovered:
        rec = f"{dd.days_to_recovery}d" if dd.days_to_recovery is not None else "recovered"
        cards.append(_kpi_card("Time to recovery", rec, sub="trough → new high", tone="pos"))
    else:
        cards.append(_kpi_card("Recovery", "underwater", sub="not yet recovered", tone="neg"))
    return (
        f"{head}"
        f'<div class="kpi-strip">{"".join(cards)}</div>'
        f"{_underwater_chart(dd.underwater)}</section>"
    )


def _underwater_chart(points: list[DrawdownPoint]) -> str:
    """A filled underwater (drawdown) area chart: 0% at the top, the trough at
    the bottom. Presentation only — the values are plotted as computed."""
    coords = [(p.date, p.drawdown_pct) for p in points if p.date]
    if len(coords) < 2:
        return ""
    if len(coords) > 240:
        stride = -(-len(coords) // 240)  # ceil division
        sampled = coords[::stride]
        if sampled[-1] != coords[-1]:
            sampled.append(coords[-1])
        coords = sampled
    vals = [v for _d, v in coords]
    lo = min(min(vals), 0.0)
    pad = (-lo or 1.0) * 0.08
    y0, y1 = lo - pad, pad  # y1 just above 0 (the surface); y0 below the trough
    width, height = 860.0, 180.0
    pad_t, pad_r, pad_b, pad_l = 10.0, 14.0, 22.0, 46.0
    plot_w, plot_h = width - pad_l - pad_r, height - pad_t - pad_b
    n = len(coords)

    def x_of(i: int) -> float:
        return pad_l + (i / max(n - 1, 1)) * plot_w

    def y_of(v: float) -> float:
        return pad_t + plot_h - ((v - y0) / (y1 - y0)) * plot_h

    line = " ".join(
        ("M" if i == 0 else "L") + f"{x_of(i):.1f},{y_of(v):.1f}"
        for i, (_d, v) in enumerate(coords)
    )
    base_y = y_of(0.0)
    area = (
        f"M{x_of(0):.1f},{base_y:.1f} "
        + " ".join(f"L{x_of(i):.1f},{y_of(v):.1f}" for i, (_d, v) in enumerate(coords))
        + f" L{x_of(n - 1):.1f},{base_y:.1f} Z"
    )
    parts: list[str] = [
        f'<svg class="pfr-uw" viewBox="0 0 {width:.0f} {height:.0f}" role="img" '
        'aria-label="Underwater drawdown curve: percent below the running peak over the window">'
    ]
    # 0% surface line + the trough gridline with labels.
    for tick in (0.0, lo):
        ty = y_of(tick)
        parts.append(
            f'<line x1="{pad_l:.1f}" x2="{pad_l + plot_w:.1f}" y1="{ty:.1f}" y2="{ty:.1f}" '
            'stroke="var(--border)" stroke-width="0.5" stroke-dasharray="2 3" />'
        )
        parts.append(
            f'<text x="{pad_l - 6:.1f}" y="{ty + 3:.1f}" text-anchor="end" font-size="9.5" '
            f'fill="var(--muted)" font-family="var(--mono)">{tick:.1f}%</text>'
        )
    anchors = {0: "start", n // 2: "middle", n - 1: "end"}
    for i, anchor in anchors.items():
        parts.append(
            f'<text x="{x_of(i):.1f}" y="{height - 6:.1f}" text-anchor="{anchor}" '
            'font-size="9.5" fill="var(--muted)" font-family="var(--mono)">'
            f"{escape(coords[i][0])}</text>"
        )
    parts.append(f'<path d="{area}" fill="var(--bad)" fill-opacity="0.16" stroke="none" />')
    parts.append(
        f'<path d="{line}" fill="none" stroke="var(--bad)" stroke-width="1.6" '
        'stroke-linejoin="round" stroke-linecap="round" />'
    )
    parts.append("</svg>")
    return "".join(parts)


def _factor_exposure_section(factor: FactorRollup) -> str:
    head = (
        '<section class="panel"><h2>Factor &amp; style exposure</h2>'
        '<p class="sub">Book value-weighted loadings from the per-holding correlation/beta '
        "table — where the book crowds into market, growth, and rate sensitivity.</p>"
    )
    cards: list[str] = []
    if factor.spy_beta is not None:
        cards.append(_kpi_card("Market β (SPY)", _ratio(factor.spy_beta), sub="value-weighted"))
    if factor.qqq_beta is not None:
        cards.append(_kpi_card("Growth β (QQQ)", _ratio(factor.qqq_beta), sub="value-weighted"))
    if factor.growth_tilt is not None:
        cards.append(_kpi_card("Growth tilt", f"{factor.growth_tilt:+.2f}", sub="QQQ - SPY beta"))
    if factor.avg_correlation_spy is not None:
        cards.append(
            _kpi_card("Crowding", _ratio(factor.avg_correlation_spy), sub="avg corr to SPY")
        )
    if factor.rate_beta_10y is not None:
        cards.append(_kpi_card("Rate β (10Y)", _ratio(factor.rate_beta_10y), sub="vs 10Y yield"))
    cov = f"{factor.names_priced} of {factor.names_total} names priced"
    note = (
        f'<p class="muted pfr-top">{escape(cov)}. Value / size / momentum loadings need a '
        "factor-return series the tracker doesn't expose — not shown.</p>"
    )
    tops = (
        '<div class="pfr-tops">'
        f"{_factor_top_line('Most market-sensitive', factor.top_market)}"
        f"{_factor_top_line('Most growth-leaning', factor.top_growth)}"
        f"{_factor_top_line('Most crowded (corr SPY)', factor.top_crowding)}"
        "</div>"
    )
    return f'{head}<div class="kpi-strip">{"".join(cards)}</div>{note}{tops}</section>'


def _factor_top_line(label: str, names: list[CrowdedName]) -> str:
    if not names:
        return ""
    chips = ", ".join(
        f"{ticker_label(c.ticker, href=f'../research/{escape(c.ticker)}/')} "
        f'<span class="muted">({c.loading:+.2f})</span>'
        for c in names
    )
    return f'<p class="pfr-top"><strong>{escape(label)}:</strong> {chips}</p>'


def _risk_reward_gap_section(gap: RiskRewardGap) -> str:
    """L7 risk-budget allocator: the risk-vs-reward-vs-conviction parity table.
    Uses the S1 control kit (.p-table / .k-chip / .k-pill) — no raw hex."""
    head = (
        '<section class="panel"><h2>Risk vs reward vs conviction</h2>'
        '<p class="sub">Each position\'s share of total book risk (marginal contribution off the '
        "Ledoit-Wolf covariance) set against its share of the book's expected reward "
        "(probability-weighted bull/base/bear DCF on the live price) and your recorded "
        "conviction. A positive gap means a name eats more of the book's risk than the "
        "reward it supplies; where the DCF is stale or missing the reward leg is marked "
        "low-confidence and shown but not scored.</p>"
    )
    if gap.hidden_reason is not None:
        return (
            f'{head}<p class="muted">Risk-parity gap unavailable — '
            f"{escape(gap.hidden_reason)}.</p></section>"
        )
    if not gap.rows:
        return (
            f'{head}<p class="muted">No positions with the daily price history needed to model '
            "risk contribution.</p></section>"
        )
    bits = [f"{gap.weights_source}-weighted"]
    if gap.portfolio_vol_ann is not None:
        bits.append(f"book vol {gap.portfolio_vol_ann * 100.0:.0f}%/yr")
    if gap.cov_obs:
        bits.append(f"{gap.cov_obs}d window")
    if gap.shrinkage is not None:
        bits.append(f"LW shrink {gap.shrinkage:.2f}")
    if gap.prices_through is not None:
        bits.append(f"prices through {gap.prices_through.isoformat()}")
    bits.append(f"{gap.valued_names}/{len(gap.rows)} priced by a current DCF")
    sub = f'<p class="sub">{escape(" · ".join(bits))}.</p>'
    rows_html = "".join(_rrg_row(r) for r in gap.rows)
    notes = "".join(f'<p class="muted pfr-top">{escape(n)}</p>' for n in gap.notes)
    table = (
        '<table class="p-table rrg-table"><thead><tr>'
        "<th>Ticker</th>"
        '<th class="num">Weight</th><th class="num">Risk share</th>'
        '<th class="num">Reward share</th><th class="num">Gap</th>'
        '<th class="num">Exp. return</th><th class="num">Conviction</th>'
        "<th>Mismatch</th>"
        f"</tr></thead><tbody>{rows_html}</tbody></table>"
    )
    return f"{head}{sub}{table}{notes}</section>"


def _rrg_row(r: RiskRewardGapRow) -> str:
    ticker = ticker_label(r.ticker, href=f"../research/{escape(r.ticker)}/")
    if r.gap_pct is not None:
        gap_tone = "neg" if r.gap_pct > 0 else "pos"
        gap_cell = f'<span class="{gap_tone}">{r.gap_pct:+.0f}pp</span>'
    else:
        gap_cell = '<span class="muted">&mdash;</span>'
    if r.expected_return_pct is not None:
        e_tone = "pos" if r.expected_return_pct >= 0 else "neg"
        warn = (
            f' <span class="muted" title="{escape(r.confidence_reason)}">&#9888;</span>'
            if r.low_confidence and r.confidence_reason
            else ""
        )
        exp_cell = f'<span class="{e_tone}">{r.expected_return_pct:+.0f}%</span>{warn}'
    else:
        exp_cell = '<span class="muted">&mdash;</span>'
    reward_cell = (
        f"{r.reward_share_pct:.0f}%"
        if r.reward_share_pct is not None
        else '<span class="muted">&mdash;</span>'
    )
    conv = (
        f"{r.conviction:g}/5" if r.conviction is not None else '<span class="muted">&mdash;</span>'
    )
    chips = "".join(
        f'<span class="k-chip k-chip-mono">{escape(c)}</span>' for c in r.mismatch_reasons
    )
    if r.mismatch_score > 0:
        mismatch = (
            f'<span class="k-pill k-pill-warn rrg-score">{r.mismatch_score:g}</span>'
            f'<span class="rrg-chips">{chips}</span>'
        )
    elif chips:
        mismatch = f'<span class="rrg-chips">{chips}</span>'
    elif r.low_confidence:
        mismatch = '<span class="muted">low-confidence</span>'
    else:
        mismatch = '<span class="muted">aligned</span>'
    return (
        "<tr>"
        f"<td>{ticker}</td>"
        f'<td class="num">{r.weight_pct:.1f}%</td>'
        f'<td class="num">{r.risk_share_pct:.0f}%</td>'
        f'<td class="num">{reward_cell}</td>'
        f'<td class="num">{gap_cell}</td>'
        f'<td class="num">{exp_cell}</td>'
        f'<td class="num">{conv}</td>'
        f'<td class="rrg-mismatch">{mismatch}</td>'
        "</tr>"
    )


def _macro_stress_section(scenarios: list[tuple[str, str]], digest: str) -> str:
    options = "".join(
        f'<option value="{escape(sid)}">{escape(title)}</option>' for sid, title in scenarios
    )
    body = (
        digest
        or '<p class="muted">No stress digest cached yet — pick a scenario and run it to '
        "generate the per-holding beta x shock read-through.</p>"
    )
    return (
        '<section class="panel"><h2>Whole-book macro stress</h2>'
        "<p class=\"sub\">Apply a named scenario's shocks to each holding's betas — the "
        "cross-name read-through, hedge clusters, and capital-allocation actions. Cached; "
        "re-running a scenario is free.</p>"
        '<div class="pfr-run">'
        '<label class="k-label" for="pfr-scenario">Scenario</label>'
        f'<select id="pfr-scenario">{options}</select>'
        '<button type="button" class="k-btn k-btn-primary" id="pfr-run-scenario">'
        "Run scenario</button>"
        '<span class="muted" id="pfr-run-msg"></span>'
        "</div>"
        '<pre id="pfr-run-log" class="cli-hint pfr-log"></pre>'
        f"{body}"
        f"<script>{_RUN_SCENARIO_JS}</script>"
        "</section>"
    )


def render_live_portfolio_section(live: LivePortfolio) -> str:
    """The live-positions panel: total + taxable-bucket KPI strip, a positions
    table with % of portfolio, and the latest transactions. Renders an offline
    note (with the start hint) when the tracker is unreachable."""
    if not live.available:
        # Humane offline card (PR1) + one-click start (PR6): the button runs
        # /actions/start-tracker (the tracker's own venv, from its checkout)
        # and the panel re-fetches itself until :8000 answers. The raw
        # requests repr stays in the collapsed details.
        return (
            '<section class="panel" id="pf-live-offline"><h2>Live portfolio</h2>'
            '<p class="sub">The portfolio tracker isn\'t running, so live positions, '
            "% of book, and the taxable breakdown aren't shown.</p>"
            f'<p class="muted">{escape(_offline_reason(live.error))}</p>'
            '<p><button type="button" class="pf-btn pf-btn-apply" id="pf-start-tracker">'
            "▶ Start tracker</button> "
            '<span class="muted" id="pf-start-msg"></span></p>'
            '<pre id="pf-start-log" class="cli-hint" '
            'style="display:none; max-height:180px; overflow:auto"></pre>'
            '<details class="offline-tech"><summary>Start it manually · technical detail</summary>'
            '<pre class="cli-hint">cd ../portfolio-tracker &amp;&amp; '
            "uvicorn portfolio_tracker.api.main:app --port 8000</pre>"
            f'<p class="muted">API endpoint: <code>{escape(live.api_url)}</code>'
            f"{f' — {escape(live.error)}' if live.error else ''}</p>"
            "</details>"
            f"<script>{_START_TRACKER_JS}</script>"
            "</section>"
        )
    if not live.positions:
        return (
            '<section class="panel"><h2>Live portfolio</h2>'
            '<p class="muted">Tracker reachable, but it reports no current holdings.</p></section>'
        )

    out: list[str] = [
        '<section class="panel"><h2>Live portfolio</h2>',
        '<p class="sub">Live positions from the companion portfolio-tracker · '
        "% of book and taxable status derived per account.</p>",
        _summary_strip(live),
        _positions_table(live),
    ]
    out.append("</section>")
    out.append(_transactions_section(live))
    return "".join(out)


def _summary_strip(live: LivePortfolio) -> str:
    cards = [
        '<div class="kpi-card"><div class="kpi-label">Total market value</div>'
        f'<div class="kpi-value">{_money(live.total_market_value)}</div></div>'
    ]
    total = live.total_market_value
    for bucket in TAX_BUCKETS:
        val = live.by_tax_treatment.get(bucket, 0.0)
        if val <= 0:
            continue
        pct = f"{100.0 * val / total:.0f}%" if total > 0 else "—"
        cards.append(
            f'<div class="kpi-card"><div class="kpi-label">{escape(_TAX_LABELS[bucket])}</div>'
            f'<div class="kpi-value">{_money(val)}</div>'
            f'<div class="kpi-sub">{pct} of book</div></div>'
        )
    return f'<div class="kpi-strip">{"".join(cards)}</div>'


def _positions_table(live: LivePortfolio) -> str:
    rows: list[str] = []
    # Largest position first.
    for p in sorted(live.positions, key=lambda x: -(x.market_value or 0.0)):
        treatments = sorted({lot.tax_treatment for lot in p.accounts})
        treat_str = ", ".join(_TAX_LABELS.get(t, t) for t in treatments) or "—"
        pnl = p.unrealized_pnl
        pnl_cell = (
            f'<td class="num {"pos" if pnl >= 0 else "neg"}">{_money(pnl)}</td>'
            if pnl is not None
            else '<td class="num muted">—</td>'
        )
        pct = f"{p.percent_of_portfolio:.1f}%" if p.percent_of_portfolio is not None else "—"
        ticker = p.ticker or "—"
        ticker_cell = (
            f'<a href="../research/{escape(ticker)}/" class="ticker-link">{escape(ticker)}</a>'
            if p.ticker
            else "—"
        )
        rows.append(
            "<tr>"
            f"<td>{ticker_cell}</td>"
            f"<td>{escape(p.name or '—')}</td>"
            f'<td class="num">{p.quantity:,.2f}</td>'
            f'<td class="num">{_money(p.market_value)}</td>'
            f'<td class="num">{pct}</td>'
            f"{pnl_cell}"
            f"<td>{escape(treat_str)}</td>"
            "</tr>"
        )
    return (
        '<table class="positions-table"><thead><tr>'
        "<th>Ticker</th><th>Name</th>"
        '<th class="num">Shares</th><th class="num">Market value</th>'
        '<th class="num">% of book</th><th class="num">Unrealized P&amp;L</th>'
        "<th>Tax treatment</th>"
        "</tr></thead><tbody>"
        f"{''.join(rows)}"
        "</tbody></table>"
    )


def _transactions_section(live: LivePortfolio) -> str:
    if not live.transactions:
        return ""
    rows: list[str] = []
    for t in live.transactions:
        kind = t.type + (f" · {t.subtype}" if t.subtype else "")
        qty = f"{t.quantity:,.2f}" if t.quantity is not None else "—"
        rows.append(
            "<tr>"
            f"<td>{escape(t.date[:10])}</td>"
            f"<td>{escape(t.ticker or '—')}</td>"
            f"<td>{escape(kind)}</td>"
            f'<td class="num">{qty}</td>'
            f'<td class="num">{_money(t.amount)}</td>'
            f"<td>{escape(t.account_name)}</td>"
            "</tr>"
        )
    return (
        '<section class="panel"><h2>Latest transactions</h2>'
        '<p class="sub">Most recent trades + cashflows across all linked accounts.</p>'
        '<table class="txn-table"><thead><tr>'
        "<th>Date</th><th>Ticker</th><th>Type</th>"
        '<th class="num">Shares</th><th class="num">Amount</th><th>Account</th>'
        "</tr></thead><tbody>"
        f"{''.join(rows)}"
        "</tbody></table></section>"
    )


def _money(v: float | None) -> str:
    if v is None:
        return "—"
    if abs(v) >= 1000:
        return f"${v:,.0f}"
    return f"${v:,.2f}"
