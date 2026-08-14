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

Degrades gracefully: tracker fully offline → a single prominent start-tracker
banner LEADS the page (it auto-starts on open, since the whole page reads from
the tracker); tracker up but an analytics endpoint failing → the other sections
still render and the failed ones are named in a footnote. The window controls
ride in the Performance panel header (the chart they drive), not a standalone
top bar. The Synthesis tab — the Portfolio section's landing tab since the
navigation_ia.md reorder — also leads with the offline banner when the tracker
is down; its panels still fall back to equal-weighted readings below it.

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
from typing import cast

from allocation import FACTOR_LABELS, NextDollarModel, build_next_dollar_model
from bear_lint import (
    SHALLOW_BEAR_FLOOR_PCT,
    STATUS_MISSING,
    STATUS_NOT_A_BEAR,
    STATUS_SHALLOW,
    BearLintFinding,
    BearLintReport,
    build_bear_lint,
)
from compute.thesis_evaluation_episodes import episode_history_source
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
    probe_tracker,
)
from portfolio_correlation import (
    CLUSTER_CORR,
    CorrelationRead,
    build_holdings_correlation_from_disk,
)
from portfolio_montecarlo import (
    DEFAULT_N_PATHS,
    DEFAULT_T_DF,
    DRAWDOWN_LABELS,
    WEALTHPLAN_CMA_ASSUMED_VOL_PCT,
    DistributionRead,
    EventStressResult,
    MonteCarloRead,
    build_book_monte_carlo,
    build_joint_latam_stress,
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
    METRIC_VERSION,
    RebaseBasis,
    RiskSnapshot,
    read_latest_snapshot,
    write_snapshot,
)
from portfolio_style_factors import StyleFactorRollup, build_style_rollup_from_disk
from portfolio_tail_stress import (
    COVERAGE_BAD_PCT,
    COVERAGE_WARN_PCT,
    TailStress,
    TailStressRow,
    build_tail_stress,
)
from portfolio_weights import read_materialized_weights
from position_guard import (
    CHECK_ADD,
    CHECK_BEAR,
    CHECK_DOWNSIDE,
    CHECK_THESIS,
    THESIS_FRESHNESS_DAYS,
)
from position_guard_cache import (
    PositionGuardCacheModel,
    PositionGuardCheckModel,
    PositionGuardRowModel,
    read_position_guard_cache,
)
from risk_factors import BookFactorVector, book_factor_vector
from risk_reward import RiskRewardGap, RiskRewardGapRow, build_risk_reward_gap
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite
from thesis_collision import CachedReport, read_cached_report
from ui import living_grid as lg
from ui.controls import chip_tone_class, thesis_status_tone, ticker_label
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

# The degraded-marker reason stamped when the B4b liveness probe says the
# tracker host is down and the data fetchers are skipped entirely.
_PROBE_DOWN_ERROR = "tracker liveness probe failed (connection refused or timed out)"


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
    """The Portfolio → Performance fragment: the benchmark scorecard plus
    expandable position drivers.

    Risk, positioning, and live-holdings detail have first-class destinations
    elsewhere in the Portfolio console; fetching and rendering them again here
    made this one fragment a seven-screen duplicate. The window args come from
    the page's own window bar and pass through to the tracker verbatim after
    validation. ``db_path`` remains in the signature for the route contract."""
    window = validated_window(start_date, end_date, include_backfill)
    # ONE cheap liveness probe first (wave B B4b): a down tracker used to cost
    # a serial walk of every data GET's failure path before the offline banner
    # painted. Down → skip the fetchers entirely and render the banner now.
    alive, base = probe_tracker(api_url)
    if alive:
        analytics = fetch_portfolio_analytics(
            api_url=api_url,
            start_date=window.start_date,
            end_date=window.end_date,
            include_backfill=window.include_backfill,
            only={"performance", "position_alpha", "policy"},
        )
        # The liveness probe already established availability. This compact
        # surface does not need a second holdings/transactions walk.
        live = LivePortfolio(available=True, api_url=base)
    else:
        analytics = PortfolioAnalytics(
            available=False, api_url=base, errors={"performance": _PROBE_DOWN_ERROR}
        )
        live = LivePortfolio(available=False, api_url=base, error=_PROBE_DOWN_ERROR)
    return compose_portfolio_page(analytics, live, window=window, include_live=False)


def compose_portfolio_page(
    analytics: PortfolioAnalytics,
    live: LivePortfolio,
    window: WindowSelection | None = None,
    snapshot: RiskSnapshot | None = None,
    *,
    include_live: bool = True,
) -> str:
    """Pure page assembly (testable without network or DB).

    Tracker down → the whole page reads from the tracker, so a single prominent
    start-tracker banner LEADS the page (it auto-starts on open) rather than a
    buried bottom card. The window controls live with the chart they drive (the
    Performance panel header), not as a standalone top bar. Tracker up but ALL
    analytics endpoints failing (e.g. an older tracker build) → one quiet note
    instead of five dead sections. ``snapshot`` (L5 PR2) renders the last-known
    cached risk read when the analytics are unavailable.
    """
    w = window or _DEFAULT_WINDOW
    parts: list[str] = [_ANALYTICS_CSS]
    # Tracker offline → lead with the start banner (it is the page's gate).
    if not live.available:
        parts.append(_tracker_offline_banner(live))
    if analytics.available:
        parts.append(render_portfolio_analytics_sections(analytics, w))
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
    # Live positions render at the bottom only when the tracker answered (the
    # offline state is the top banner, not a second panel down here).
    if live.available and include_live:
        parts.append(render_live_portfolio_section(live))
    return "".join(parts)


# ---------------------------------------------------------------------------
# Tracker analytics sections (master build P2.1). Pure HTML assembly over the
# already-parsed PortfolioAnalytics — no network, no benchmark math.
# ---------------------------------------------------------------------------

# Tracker-offline banner rules, shared by Performance (always) and Synthesis
# (when the tracker is down — navigation_ia.md §2.1: a landing page must
# self-report its degradation, not silently fall back to equal-weight). Its own
# <style> block so each fragment ships it independently of the big sheets.
_TRACKER_BANNER_CSS = """<style>
/* Tracker-offline banner: the page's data source is down, so this LEADS the
   page (the start control is prominent), never a buried bottom card. */
.pf-tracker-banner { border-left: 3px solid var(--warn); }
.pf-tracker-actions { display: flex; align-items: center; gap: 12px; flex-wrap: wrap;
  margin: 12px 0 0; }
</style>"""

# Styling only the Performance fragment needs; everything else reuses the
# shell's panel/kpi/table vocabulary. Colors key off the shared token variables
# so a palette change in ui/tokens.py propagates here untouched. (The Synthesis
# fragment carries its own block — _INSIGHTS_CSS.)
_ANALYTICS_CSS = (
    """<style>
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
.pf-alpha-details { margin-top: var(--sp-2); }
.pf-alpha-details > summary { cursor: pointer; color: var(--fg);
  font-weight: 600; padding: 8px 0; }
/* Performance panel header: title (+ hover note) on the left, the window
   controls on the right — ONE operating band, not a separate top bar
   (design_language §6.1). The window cluster dropped its card chrome: in-panel
   it reads as the panel's own control, not a competing surface above it. */
.pf-perf-head { display: flex; align-items: flex-start; justify-content: space-between;
  gap: 10px 18px; flex-wrap: wrap; margin-bottom: 14px; }
.pf-perf-head h2 { margin: 0; }
.pf-window { display: flex; align-items: center; gap: 6px; flex-wrap: wrap;
  font-size: var(--fs-caption); }
.pf-window-standalone { margin-bottom: 18px; }
.pf-window-label { font-size: var(--fs-caption); text-transform: uppercase;
  letter-spacing: 0.06em; color: var(--muted); margin-right: 2px; }
.pf-window input[type="date"] { padding: 3px 6px; font-size: var(--fs-caption);
  font-family: var(--mono); }
.pf-backfill-label { color: var(--muted); display: inline-flex; align-items: center;
  gap: 5px; margin-left: 4px; cursor: help; }
/* Methodology note: a hover affordance on the title, not permanent prose — it
   is reference detail, surfaced on demand (the heading carries it on hover). */
.pf-info { position: relative; display: inline-flex; align-items: center;
  justify-content: center; width: 15px; height: 15px; border-radius: var(--radius-full);
  border: 1px solid var(--border); color: var(--muted); font-size: var(--fs-caption);
  font-weight: 600; cursor: help; margin-left: 7px;
  vertical-align: middle; transition: color var(--transition), border-color var(--transition); }
.pf-info:hover, .pf-info:focus { color: var(--fg); border-color: var(--border-2); outline: none; }
.pf-info-pop { position: absolute; top: calc(100% + 6px); left: 0; z-index: 5; width: 300px;
  background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius);
  box-shadow: var(--shadow-pop); padding: 9px 11px; font-size: var(--fs-caption);
  font-style: normal; font-weight: 400; line-height: 1.5; color: var(--muted);
  white-space: normal; display: none; }
.pf-info:hover .pf-info-pop, .pf-info:focus .pf-info-pop,
.pf-info:focus-within .pf-info-pop { display: block; }
</style>"""
    + _TRACKER_BANNER_CSS
)

# Styling for the Synthesis fragment: the rollup/exposure insights grid and
# the next-dollar distribution rows. Same token-variable discipline as
# _ANALYTICS_CSS; a separate block so each tab ships only the rules it renders.
_INSIGHTS_CSS = """<style>
.pf-insights { display: grid; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr));
  gap: 0 18px; align-items: start; }
.pf-th-chips { display: flex; gap: 8px; flex-wrap: wrap; }
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
# Class-scoped, not id-scoped (Phase-5 verifier): the banner is emitted by BOTH
# the Synthesis section (Health console) and the Performance section
# (Allocation console), and with duplicate ids getElementById always resolved
# the FIRST instance — whichever composite loaded second had a dead Start
# button. Each run wires every still-unwired banner in the document, each
# scoped to its own subtree.
_START_TRACKER_JS = """
(function () {
  var banners = document.querySelectorAll('.pf-live-offline');
  Array.prototype.forEach.call(banners, function (banner) { wireBanner(banner); });
  function wireBanner(banner) {
  var btn = banner.querySelector('.pf-start-tracker');
  if (!btn || btn.dataset.wired) return;
  btn.dataset.wired = '1';
  var msg = banner.querySelector('.pf-start-msg');
  var log = banner.querySelector('.pf-start-log');
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
      CCAction.release(btn);
      return;
    }
    var endpoint = banner.getAttribute('data-refresh-endpoint') || '/api/panel/portfolio';
    fetch(endpoint).then(function (r) { return r.text(); }).then(function (html) {
      if (html.indexOf('pf-live-offline') === -1) {
        // A tracker gate can live inside one section of a composite console.
        // Refresh only that section; replacing .cc-panel-body destroys the
        // entire Health/Allocation page around it.
        var target = banner.closest('.console-sec') || banner.closest('.cc-panel-body');
        if (target) { reinject(target, html); } else { location.reload(); }
      } else {
        setTimeout(function () { pollPanel(tries - 1); }, 3000);
      }
    }).catch(function () { setTimeout(function () { pollPanel(tries - 1); }, 3000); });
  }
  function startTracker(auto) {
    CCAction.busy(btn, auto ? undefined : 'Starting…');
    if (!auto) { msg.textContent = 'starting…'; }
    fetch('/actions/start-tracker', {
      method: 'POST', headers: {'Content-Type': 'application/json'}, body: '{}'
    }).then(function (r) { return r.json().then(function (j) { return {ok: r.ok, status: r.status, j: j}; }); })
      .then(function (res) {
        // 409 = a tracker job is ALREADY starting (e.g. a prior page open
        // kicked it off) — not an error, just begin polling for :8000.
        if (!res.ok && res.status !== 409) {
          msg.textContent = 'error: ' + (res.j.error || 'failed to start tracker');
          CCAction.release(btn);
          return;
        }
        msg.textContent = 'starting — waiting for :8000 to answer…';
        log.style.display = 'block';
        if (res.ok && res.j.stream_url) {
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
        }
        pollPanel(30);
      }).catch(function () { msg.textContent = 'network error — start-tracker request failed'; CCAction.release(btn); });
  }
  btn.addEventListener('click', function () { startTracker(false); });
  // Auto-start when the page opens — the tracker powers the WHOLE portfolio
  // page, so don't make the user hunt for a button. Guarded to fire once per
  // page load so re-injecting this banner (or a second banner instance) can't
  // spawn a start loop; a hard failure leaves the manual button to retry.
  if (!window.__pfTrackerAutostart) {
    window.__pfTrackerAutostart = true;
    startTracker(true);
  } else {
    // Another mounted banner already started the shared tracker. This banner
    // still refreshes its own section when the service becomes reachable.
    CCAction.busy(btn);
    msg.textContent = 'starting — waiting for :8000 to answer…';
    pollPanel(30);
  }
  }
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
        f'<button type="button" class="k-btn k-btn-quiet k-btn-sm" data-preset="{key}">{label}</button>'
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
        '<button type="button" class="k-btn k-btn-primary k-btn-sm" id="pf-apply">Apply</button>'
        f'<label class="pf-backfill-label" title="{escape(backfill_tip)}">'
        f'<input type="checkbox" id="pf-backfill"{checked}> modeled backfill</label>'
        "</div>"
        f"<script>{_WINDOW_JS}</script>"
    )


def render_portfolio_analytics_sections(
    a: PortfolioAnalytics, window: WindowSelection | None = None
) -> str:
    """Every analytics section that loaded, in page order, plus one footnote
    naming the sections that didn't (instead of five dead panels). The shared
    ``<style>`` block is emitted by ``compose_portfolio_page``. The window
    controls ride in the Performance panel header (the chart they drive); if
    Performance itself failed but other analytics loaded, a slim standalone bar
    keeps re-windowing reachable."""
    w = window or _DEFAULT_WINDOW
    out: list[str] = []
    if a.performance is not None:
        out.append(_performance_section(a.performance, a.policy, w, a.position_alpha))
    elif any(x is not None for x in (a.beta, a.positioning, a.position_alpha)):
        out.append(f'<div class="pf-window-standalone">{_window_bar(w)}</div>')
    if a.position_alpha is not None:
        out.append(
            '<details class="pf-alpha-details"><summary>'
            f"Position drivers ({len(a.position_alpha.rows)})"
            "</summary>"
            f"{_alpha_section(a.position_alpha)}</details>"
        )
    if a.beta is not None:
        out.append(_risk_section(a.beta))
    if a.positioning is not None:
        out.append(_positioning_section(a.positioning))
    failed = [label for key, label in _SECTION_LABELS.items() if key in a.errors]
    if failed:
        out.append(
            '<p class="muted pf-degraded">Unavailable from the tracker right now: '
            f"{escape(', '.join(failed))}.</p>"
        )
    return "".join(out)


def _performance_section(
    perf: PerformanceSeries,
    policy: PolicyMix | None,
    window: WindowSelection,
    position_alpha: PositionAlpha | None = None,
) -> str:
    window_label = f"{perf.start_date or '?'} → {perf.end_date or '?'}"
    # The methodology note rides a hover affordance on the title, not permanent
    # prose; the window controls share the title's band (one operating band).
    note = (
        "Money-weighted return (Modified Dietz) from the tracker. Each benchmark "
        "is a synthetic book receiving the same external cashflows; net external "
        f"inflow {_money(perf.net_external_cashflow_in)} over the window."
    )
    head = (
        '<section class="panel"><div class="pf-perf-head">'
        "<h2>Performance vs benchmarks"
        f'<span class="pf-info" tabindex="0" role="note" aria-label="{escape(note)}">i'
        f'<span class="pf-info-pop">{escape(note)}</span></span></h2>'
        f"{_window_bar(window)}</div>"
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
    cards: list[str] = []
    if position_alpha is not None:
        alpha_window = f"{position_alpha.start_date or '?'} → {position_alpha.end_date or '?'}"
        cards.extend(
            [
                _kpi_card(
                    "Actual P&L",
                    _money(position_alpha.total_actual_pl),
                    sub=alpha_window,
                    tone=_tone(position_alpha.total_actual_pl),
                ),
                _kpi_card(
                    "Matched SPY P&L",
                    _money(position_alpha.total_spy_pl),
                    sub="same-day buys & sells",
                    tone=_tone(position_alpha.total_spy_pl),
                ),
                _kpi_card(
                    "Alpha vs SPY",
                    _money(position_alpha.total_alpha),
                    sub="actual minus matched SPY",
                    tone=_tone(position_alpha.total_alpha),
                ),
            ]
        )
    cards.append(
        _kpi_card(
            "Modified Dietz",
            _pct(finals["Portfolio"], signed=True),
            sub=window_label,
            tone=_tone(finals["Portfolio"]),
        )
    )
    warn = _backfill_warning(perf)
    return (
        f"{head}"
        f'<div class="kpi-strip">{"".join(cards)}</div>'
        f"{_chart_legend(perf.points)}"
        f"{_benchmark_chart(perf.points)}"
        f"{_policy_line(policy)}"
        f"{warn}</section>"
    )


def _backfill_warning(perf: PerformanceSeries) -> str:
    """The "this window is modeled, not measured" banner.

    Names the actual defect. Two earlier versions of this copy were wrong in
    different directions and both are worth recording.

    The original — "the window start value looks incomplete … may overstate or
    understate relative performance" — never rendered at all, because the
    tracker's guard only fired when the reconstructed start had collapsed below
    25% of the end, which never happened on a real window.

    The replacement over-corrected: it said the walk-back "can only see
    positions you still hold", implying survivorship bias. That is NOT what the
    walk-back does. It replays transactions backward from the anchor snapshot,
    reversing every buy, sell and transfer on file, so a position fully exited
    inside the covered span IS resurrected into the historical book. Measured
    on the live database at 2024-01-01 it reconstructed 33 positions against
    the 12 held today, including XLV, CPNG, AMZN, SOFI and FSLR — all long
    gone. Attributing the inflation to vanished losers was simply false.

    The real limit is COVERAGE, and it is per-account. Each account's
    transaction history begins when that account was linked, not when the
    window opens. Before its own feed starts, an account's positions freeze at
    their earliest reconstructed state and — the part that actually moves the
    number — the contributions that built it are invisible. Money deposited
    but unseen is arithmetically indistinguishable from investment gain, so the
    bias is one-directional and upward. On 2026-07-30 the recorded net flow was
    +$4,867 for all of 2024 and *negative* $8,326 for 2025 on a book that grew
    ~$150k that year; a 365-day window reporting +21.6% falls to ~7% if the
    true figure was $75k higher, and under water at $150k.
    """
    if not perf.backfill_start_unreliable:
        return ""
    observed = perf.earliest_observed_date
    span = (
        f"Only {escape(observed)} onward is backed by observed broker snapshots"
        if observed
        else "No part of this window is backed by observed broker snapshots"
    )
    return (
        '<p class="muted">⚠ <strong>This window is modeled, not measured.</strong> '
        f"{span}. Earlier values are rebuilt by replaying your transactions backward, which "
        "reconstructs trades and closed positions faithfully <em>as far back as each "
        "account's transaction history reaches</em> — and that begins when the account was "
        "linked, not when the window opens. Before then, deposits into that account are "
        "invisible, and money you added is indistinguishable from money you made, so the "
        "return is biased <em>upward</em>. Shorten the window to the observed range for a "
        "number you can act on.</p>"
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
        'aria-label="Cumulative Modified-Dietz return vs SPY, QQQ, and policy benchmarks">'
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
    policy_th = lg.th(f"{_ALPHA} vs policy", "policy", "num") if show_policy else ""
    rows: list[str] = []
    for r in sorted(pa.rows, key=lambda x: (x.alpha is None, -(x.alpha or 0.0))):
        ticker = r.ticker or "—"
        ticker_cell = (
            ticker_label(ticker, href="../research/" + escape(ticker) + "/") if r.ticker else "—"
        )
        if r.incomplete:
            ticker_cell += (
                '<span class="pf-flag" title="window start could not be fully reconstructed '
                '— row is approximate">⚠</span>'
            )
        policy_td = _money_cell(r.alpha_vs_policy, colored=True) if show_policy else ""
        # Living-grid hooks: data-text drives the filter box; the per-column
        # data-* carry the RAW sort keys so the client re-orders without
        # re-parsing the formatted cells (progressive enhancement — the rows
        # already arrive alpha-sorted from the server above).
        data = (
            lg.data_text(f"{r.ticker or ''} {r.name or ''}")
            + lg.data_text_key("ticker", r.ticker)
            + lg.data_text_key("name", r.name)
            + lg.data_num("value", r.value_at_end)
            + lg.data_num("pl", r.actual_pl)
            + lg.data_num("spy", r.spy_counterfactual_pl)
            + lg.data_num("alpha", r.alpha)
            + lg.data_num("qqq", r.alpha_vs_qqq)
            + (lg.data_num("policy", r.alpha_vs_policy) if show_policy else "")
        )
        rows.append(
            f"<tr{data}>"
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
    headers = (
        lg.th("Ticker", "ticker", "text", num=False)
        + lg.th("Name", "name", "text", num=False)
        + lg.th("Value", "value", "num")
        + lg.th("P&amp;L", "pl", "num")
        + lg.th("SPY P&amp;L", "spy", "num")
        + lg.th(f"{_ALPHA} vs SPY", "alpha", "num")
        + lg.th(f"{_ALPHA} vs QQQ", "qqq", "num")
        + policy_th
    )
    return (
        head
        + lg.grid_open()
        + lg.filter_bar(len(pa.rows), noun="positions")
        + '<table class="alpha-table"><thead><tr>'
        + headers
        + "</tr></thead><tbody>"
        + "".join(rows)
        + f"</tbody><tfoot>{totals}</tfoot></table>"
        + lg.grid_close()
        + "</section>"
    )


def _kpi_card(label: str, value: str, *, sub: str = "", tone: str = "") -> str:
    sub_html = f'<div class="kpi-sub">{escape(sub)}</div>' if sub else ""
    # A pos/neg tone colors the VALUE (green/red number) via the kit .k-num-* on
    # the value's own span — a child rule beats .kpi-value's default color with
    # no shell compound. A tone-* rides the card as its border rail.
    if tone in ("pos", "neg"):
        value_html = f'<div class="kpi-value"><span class="{_NUM_CLS[tone]}">{value}</span></div>'
        card_cls = ""
    else:
        value_html = f'<div class="kpi-value">{value}</div>'
        card_cls = f" {tone}" if tone else ""
    return (
        f'<div class="kpi-card{card_cls}"><div class="kpi-label">{escape(label)}</div>'
        f"{value_html}{sub_html}</div>"
    )


# Semantic pos/neg → the kit's green/red number-text classes (design_language §4).
_NUM_CLS = {"pos": "k-num-pos", "neg": "k-num-neg", "": ""}


def _tone(v: float | None) -> str:
    """kpi-card / table-cell modifier: green when favorable, red when not."""
    if v is None:
        return ""
    return "pos" if v >= 0 else "neg"


def _money_cell(v: float | None, *, colored: bool = False) -> str:
    if v is None:
        return '<td class="num muted">—</td>'
    cls = "num" + (f" {_NUM_CLS[_tone(v)]}" if colored else "")
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


def render_portfolio_synthesis_panel(
    db_path: Path, *, api_url: str | None = None, cash_to_deploy_usd: float | None = None
) -> str:
    """The Portfolio → Synthesis tab fragment. Fetches the live book once (the
    exposure weighting and the next-dollar model prefer live position weights
    and fall back to equal-weight when the tracker is down) plus the cached
    ``cross_portfolio_synthesis`` lens memo, then assembles the page. As the
    section's LANDING tab (navigation_ia.md §2.1) it now leads with the
    tracker-offline banner when the live fetch failed — a front door must
    say its weights are degraded, not silently show equal-weight.

    ``cash_to_deploy_usd`` (tenet-2 Phase 2) opts the next-dollar panel into
    cash-aware mode — the route boundary (``execution/comments_server.py``)
    reads it from a ``?cash_to_deploy=`` query param; omitted, the panel
    behaves exactly as before Phase 2."""
    # Lazy imports keep the analytical builder out of this module's import graph
    # until the panel is actually requested.
    from pipeline.analytical_dashboard import build_analytical_dashboard
    from pipeline.analytical_dashboard_html import render_panel_fragment

    # Liveness probe first (B4b): tracker down → equal-weight degrade + the
    # offline banner immediately, without walking the live fetch's timeouts.
    alive, base = probe_tracker(api_url)
    live = (
        fetch_live_portfolio(api_url=api_url)
        if alive
        else LivePortfolio(available=False, api_url=base, error=_PROBE_DOWN_ERROR)
    )
    dash = build_analytical_dashboard(db_path, sections={"portfolio_synthesis"})
    memo = _synthesis_memo_doorway(dash.portfolio_synthesis_md) or (
        render_panel_fragment(dash, "portfolio") or ""
    )
    return compose_synthesis_page(db_path, live, memo, cash_to_deploy_usd=cash_to_deploy_usd)


def _synthesis_memo_headline(content_md: str, cap: int = 220) -> str:
    """The memo's first substantive prose line (headings/bullets skipped) —
    the one-line conclusion the Health console shows instead of the body."""
    for raw in content_md.splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", "-", "*", ">", "|", "```")):
            continue
        return line[:cap]
    return content_md.strip().replace("\n", " ")[:cap]


def _synthesis_memo_doorway(content_md: str | None) -> str:
    """Wave B (B5): the Health console's Synthesis section no longer embeds the
    FULL cross-portfolio lens memo — the same memo already renders in Record's
    Memos section, so Health showed it twice. Render headline + a "full memo →"
    doorway (``#advisor_memos`` — the shell's ANCHORS redirect lands it on
    Record's memos section). "" when nothing is cached, so the caller falls
    back to the existing run-hint stub."""
    if not content_md:
        return ""
    headline = _synthesis_memo_headline(content_md)
    return (
        '<section class="panel synthesis-panel"><div class="panel-head">'
        "<h2>Portfolio synthesis</h2>"
        '<p class="sub">Cross-ticker patterns · the full memo lives in Record → Memos.</p>'
        '</div><div class="panel-body">'
        f'<p class="pf-syn-headline">{escape(headline)}</p>'
        '<p><a class="k-chip k-chip-btn" href="#advisor_memos">full memo →</a></p>'
        "</div></section>"
    )


def compose_synthesis_page(
    db_path: Path,
    live: LivePortfolio,
    synthesis: str,
    *,
    cash_to_deploy_usd: float | None = None,
) -> str:
    """Page assembly over an already-fetched live book + lens-memo fragment
    (testable without network; the insight panels read the DB themselves):
    the tracker-offline banner when the live book is unavailable (landing-tab
    honesty — the exposure panel below is equal-weighted then), the
    rollup/exposure grid, a one-line pointer to the primary next-dollar
    answer, then the memo.

    P0.4b (PRD §6/§7.4): Health no longer owns the primary next-dollar
    answer — the full ``render_next_dollar_panel`` distribution moved to
    Portfolio → Allocation as the governed Incremental Dollar Recommendation
    (``pipeline.allocation_recommendation_panel``). This page now shows only
    a doorway line; ``render_next_dollar_panel`` itself is unchanged and
    still public (peek/markup-contract tests call it directly).
    ``cash_to_deploy_usd`` is accepted for backward-compatible call sites but
    no longer affects this page's output (the cash-aware mode lives on the
    Allocation console's cash form now)."""
    del cash_to_deploy_usd  # kept for callers; no longer threaded here (see docstring)
    grid = "".join(p for p in (_thesis_rollup_panel(db_path), _exposure_panel(db_path, live)) if p)
    parts: list[str] = [_INSIGHTS_CSS]
    if not live.available:
        parts.append(_TRACKER_BANNER_CSS)
        parts.append(
            _tracker_offline_banner(live, refresh_endpoint="/api/panel/portfolio_synthesis")
        )
    if grid:
        parts.append(f'<div class="pf-insights">{grid}</div>')
    parts.append(
        '<section class="panel"><h2>Next dollar</h2>'
        '<p class="sub">The Incremental Dollar Recommendation now lives on '
        '<a class="k-chip k-chip-btn" href="/#portfolio_allocation">Portfolio &rarr; '
        "Allocation</a> &mdash; a governed plan for new cash, with Risk Budget impact "
        "and owner actions.</p></section>"
    )
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
        conn = connect_sqlite(db_path, role=SQLiteConnectionRole.READ_ONLY)
    except sqlite3.Error:
        return ""
    try:
        tickers = _portfolio_tickers(conn)
        if not tickers:
            return ""
        latest: dict[str, str] = {}
        try:
            source = episode_history_source(conn)
            rows = conn.execute(
                f"SELECT ticker, overall_status FROM {source.relation} "
                f"ORDER BY ticker, {source.latest_checked_column} DESC"  # nosec B608 -- trusted closed relation
            ).fetchall()
        except (sqlite3.Error, ValueError):
            return ""
        for r in rows:
            t = str(r[0]).upper()
            if t in tickers and t not in latest and r[1]:
                latest[t] = str(r[1]).lower()
    finally:
        conn.close()
    if not latest:
        return ""
    # Tone via the shared kit resolver: the local map here defaulted every
    # unknown status to RED (an `unresolved` evaluation rendered as a breach);
    # now breach/broken are red, warn/watch amber, and unknown vocabulary
    # stays a neutral chip — still flagged, never mis-colored.
    flagged = [(t, s) for t, s in sorted(latest.items()) if thesis_status_tone(s) != "ok"]
    ok_n = len(latest) - len(flagged)
    chips = "".join(
        # data-peek-ticker: the chip text carries " · status", so the hover
        # mini-card needs the bare symbol spelled out (UX9).
        f'<a class="k-chip{chip_tone_class(thesis_status_tone(s))}" href="#holding={escape(t)}" '
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
        conn = connect_sqlite(db_path, role=SQLiteConnectionRole.READ_ONLY)
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
                if isinstance(payload, list):
                    records = cast("list[object]", payload)
                    rec: object = records[0] if records else {}
                else:
                    rec = payload
                if isinstance(rec, dict):
                    raw = cast("dict[str, object]", rec).get("sector")
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


def render_next_dollar_panel(
    db_path: Path,
    live: LivePortfolio | None = None,
    *,
    cash_to_deploy_usd: float | None = None,
) -> str:
    """Quantitative next-dollar allocation distribution over the holdings
    (src/allocation: DCF upside / diversification / macro tilt, z-scored,
    blended by visible weights, softmaxed — directives/next_dollar_model.md),
    with the latest advisor memo excerpted below as the narrative layer.
    Falls back to the memo alone when the model has nothing to score; hides
    entirely when neither exists. Public for the markup-contract tests
    (UX9 peeks); ``live`` is optional so those callers don't need a tracker
    snapshot (None means no live weights — the model goes equal-weight).

    ``cash_to_deploy_usd`` (tenet-2 Phase 2 cash-aware mode) opts into a
    concrete per-holding dollar plan for that cash — omitted, the panel
    renders the distribution only, exactly as before Phase 2."""
    if not db_path.exists():
        return ""
    try:
        conn = connect_sqlite(db_path, role=SQLiteConnectionRole.READ_ONLY)
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
        model = build_next_dollar_model(
            db_path,
            db_path.parent.parent,
            tickers,
            live_values,
            cash_to_deploy_usd=cash_to_deploy_usd,
        )

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
    blend_source = (
        "your affirmed profile"
        if model.blend_weights_source == "owner_profile"
        else "default house view"
    )
    bits = [f"blend {blend} ({blend_source})"]
    bits.append("tracker-weighted" if model.weights_source == "tracker" else "equal-weighted")
    if model.prices_through is not None:
        bits.append(f"daily returns through {model.prices_through.isoformat()}")
    if model.cov_obs:
        bits.append(f"{model.cov_obs}d window")
    if model.portfolio_vol_ann is not None:
        bits.append(f"book vol {model.portfolio_vol_ann * 100.0:.0f}%/yr")
    if model.shrinkage is not None:
        bits.append(f"LW shrink {model.shrinkage:.2f}")
    if model.cash_to_deploy_usd is not None:
        bits.append(f"deploying ${model.cash_to_deploy_usd:,.0f}")
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
                    f'<span class="k-chip" title="no {escape(label)} data for this '
                    f'holding — its blend renormalizes over the rest">{escape(label)} —</span>'
                )
                continue
            tone = " k-chip-ok" if reading.contribution >= 0 else " k-chip-bad"
            tip = (
                f"z {reading.z:+.2f} · weight {round(reading.weight * 100.0, 6):.0f}% · "
                f"raw {reading.raw * 100.0:+.1f}% · {reading.detail}"
            )
            chips.append(
                f'<span class="k-chip{tone}" title="{escape(tip)}">'
                f"{escape(label)} {reading.contribution:+.2f}</span>"
            )
        ticker = escape(r.ticker)
        # Cash-aware mode (tenet-2 Phase 2): fold the dollar amount into the
        # existing "now X%" cell rather than adding a 5th grid column — the
        # 4-column .pf-nd-row grid is otherwise fixed-width across every row.
        now_text = f"now {r.current_weight_pct:.1f}%"
        if r.cash_allocation_usd is not None:
            now_text += f" · +${r.cash_allocation_usd:,.0f}"
        items.append(
            '<div class="pf-nd-item" tabindex="0">'
            '<div class="pf-nd-row">'
            f"{ticker_label(r.ticker, href=f'../research/{ticker}/', classes='pf-nd-ticker')}"
            f'<span class="pf-nd-bar"><span style="width:{width:.1f}%"></span></span>'
            f'<span class="pf-nd-alloc">{r.allocation_pct:.1f}%</span>'
            f'<span class="pf-nd-now muted">{now_text}</span>'
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
/* Implicit bets (Wave 3, surface_density_jit_redesign.md #3): the ranked
   prose statement of what the book is positioned for. */
.pfr-bets ol { margin: 4px 0 0 20px; padding: 0; }
.pfr-bets li { margin: 0 0 6px; font-size: var(--fs-body); line-height: 1.5; }
.pfr-bets .pfr-bet-nums { color: var(--muted); font-size: var(--fs-caption); }
.pfr-uw { width: 100%; height: auto; display: block; margin-top: 6px; }
.pfr-top { font-size: var(--fs-caption); color: var(--muted); margin: 6px 0 0; }
.pfr-tops { margin-top: 8px; }
.pfr-run { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; margin: 4px 0 12px; }
.pfr-run select { font-size: var(--fs-body); }
.pfr-log { display: none; max-height: 160px; overflow: auto; margin: 0 0 12px; }
/* Pairwise correlation heat table: warm tint scales with co-movement (the
   crowding signal); negative correlation (a true diversifier) reads cool.
   color-mix over the shared tone tokens — the analytical-dashboard idiom. */
.pfc-scroll { overflow-x: auto; margin-top: 8px; }
.pfc-table { border-collapse: collapse; font-family: var(--mono); font-size: var(--fs-caption); }
.pfc-table th { font-weight: 600; color: var(--muted); padding: 2px 5px; text-align: center; }
.pfc-table th.pfc-row-h { text-align: right; }
.pfc-cell { padding: 2px 5px; text-align: center; font-variant-numeric: tabular-nums;
  min-width: 34px; }
.pfc-diag { color: var(--muted); }
.pfc-c1 { background: color-mix(in srgb, var(--warn) 10%, transparent); }
.pfc-c2 { background: color-mix(in srgb, var(--warn) 24%, transparent); }
.pfc-c3 { background: color-mix(in srgb, var(--bad) 30%, transparent); }
.pfc-neg { background: color-mix(in srgb, var(--ok) 14%, transparent); }
.pfc-clusters { display: flex; flex-direction: column; gap: 4px; margin: 8px 0 0; }
.pts-table { margin-top: 4px; }
.pts-excluded { color: var(--muted); }
.pfm-table { margin-top: 4px; max-width: 360px; }
/* Coverage-gate warning (Monthly Red Team Phase 1 guard 1): a leading pill
   line the aggregate headline sits BELOW, never a quiet footnote — the
   book-level scenario/reward rollups (tail stress, risk-vs-reward) share it. */
.pfr-coverage-warn { font-size: var(--fs-body); margin: 8px 0; }
.ptc-findings { display: flex; flex-direction: column; gap: 8px; margin-top: 8px; }
.ptc-finding { border-left: 2px solid var(--warn); padding-left: 10px; }
.ptc-finding-bad { border-left-color: var(--bad); }
.ptc-finding-head { font-size: var(--fs-body); }
.ptc-finding-rationale { font-size: var(--fs-caption); color: var(--muted); margin: 2px 0 0; }
.rrg-table { margin-top: 4px; }
.rrg-mismatch { max-width: 460px; }
.rrg-chips { display: inline-flex; gap: 4px; flex-wrap: wrap; vertical-align: middle; }
.rrg-score { margin-right: 8px; }
/* Naked-position gate violation chips (Monthly Red Team Phase 1 guard 7):
   one dense standing chip per violation, wrapping — the .k-chip kit base
   (controls.py), only the layout (flex-wrap + gaps) is local. */
.pfr-naked-chips { display: flex; flex-wrap: wrap; gap: 7px; margin: 8px 0 12px; }
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
    }).catch(function () { CCAction.release(btn); });
  }
  btn.addEventListener('click', function () {
    var scenario = sel ? sel.value : '';
    if (!scenario) { msg.textContent = 'pick a scenario'; return; }
    CCAction.busy(btn, 'Running…');
    msg.textContent = 'running… (LLM digest, ~10-40s)';
    log.style.display = 'block';
    fetch('/actions/run-scenario', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({scenario: scenario})
    }).then(function (r) { return r.json().then(function (j) { return {ok: r.ok, j: j}; }); })
      .then(function (res) {
        if (!res.ok) {
          msg.textContent = 'error: ' + (res.j.error || 'scenario run failed to start');
          CCAction.release(btn);
          return;
        }
        try {
          var es = new EventSource(res.j.stream_url);
          es.onmessage = function (ev) {
            try {
              var f = JSON.parse(ev.data);
              if (f.line) { log.textContent += f.line + '\\n'; log.scrollTop = log.scrollHeight; }
              if (f.event === 'done') {
                es.close();
                if (f.exit_code === 0) {
                  msg.textContent = 'done — refreshing digest…';
                  CCAction.receipt(btn, '✓ Digest refreshed');
                  reloadPanel();
                } else {
                  msg.textContent = 'scenario run failed — exit code ' + f.exit_code + ', see log above';
                  CCAction.release(btn);
                }
              }
            } catch (e) {}
          };
          es.onerror = function () {
            es.close();
            msg.textContent = 'stream interrupted — scenario run may not have completed';
            CCAction.release(btn);
          };
        } catch (e) { CCAction.release(btn); }
      }).catch(function () { msg.textContent = 'network error'; CCAction.release(btn); });
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
    # Liveness probe first (B4b): a down tracker renders the offline/cached
    # branch immediately instead of walking three data GETs' failure paths.
    alive, base = probe_tracker(api_url)
    analytics = (
        fetch_portfolio_analytics(api_url=api_url, only={"performance", "positioning", "beta"})
        if alive
        else PortfolioAnalytics(
            available=False, api_url=base, errors={"positioning": _PROBE_DOWN_ERROR}
        )
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
    style = _build_style_rollup(analytics.positioning, db_path)
    correlation = _build_correlation_read(analytics.positioning, db_path)
    tail_stress = _build_tail_stress(analytics.positioning, db_path)
    monte_carlo = _build_monte_carlo(analytics.positioning, db_path)
    joint_latam = _build_joint_latam_stress(analytics.positioning, db_path)
    bear_lint = _build_bear_lint(db_path)
    position_guard = _read_position_guard(db_path)
    collision = _read_thesis_collision(analytics.positioning, db_path)
    factors = _read_business_factor_vector(db_path)
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
    # The implicit-bets frame reads the LATEST snapshot regardless of branch
    # (tracker up: the row _persist_risk_snapshot just refreshed) + the local
    # weights — so the page's organizing statement renders online and offline.
    bets_snapshot = snapshot
    if bets_snapshot is None and db_path is not None:
        bets_snapshot = read_latest_snapshot(db_path=db_path)
    bets_weights = (
        _local_book_weights(analytics.positioning, db_path.parent.parent)
        if db_path is not None
        else {}
    )
    bets = _implicit_bets_section(bets_snapshot, bets_weights, factors)
    return compose_risk_page(
        analytics,
        drawdown=drawdown,
        factor=factor,
        gap=gap,
        style=style,
        correlation=correlation,
        tail_stress=tail_stress,
        monte_carlo=monte_carlo,
        joint_latam=joint_latam,
        bear_lint=bear_lint,
        position_guard=position_guard,
        collision=collision,
        factors=factors,
        scenarios=scenarios,
        digest=digest,
        snapshot=snapshot,
        bets=bets,
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


def _local_book_weights(pos: Positioning | None, repo_root: Path) -> dict[str, float]:
    """Ticker -> fraction-of-book for the local-substrate risk sections.

    Prefers the live positioning rows; offline it falls back to the
    materialized weights cache (stage 0c), whose keys double as the holdings
    list. ``{}`` when neither source has weights."""
    weights: dict[str, float] = {}
    if pos is not None:
        weights = {
            r.ticker.upper(): (r.weight_pct or 0.0) / 100.0
            for r in pos.correlations
            if r.ticker and r.weight_pct is not None
        }
    if not weights:
        weights = read_materialized_weights(repo_root)
    return weights


def _build_style_rollup(pos: Positioning | None, db_path: Path | None) -> StyleFactorRollup | None:
    """The value/size/momentum loadings, computed entirely from local disk
    (FMP price cache + factor_proxies store) — so the section renders with the
    tracker DOWN."""
    if db_path is None:
        return None
    repo_root = db_path.parent.parent
    weights = _local_book_weights(pos, repo_root)
    if not weights:
        return None
    return build_style_rollup_from_disk(repo_root, list(weights), weights)


def _build_correlation_read(
    pos: Positioning | None, db_path: Path | None
) -> CorrelationRead | None:
    """The holdings pairwise correlation matrix + crowding clusters, computed
    entirely from the local price cache — renders with the tracker DOWN (same
    weights degrade as the style section)."""
    if db_path is None:
        return None
    repo_root = db_path.parent.parent
    weights = _local_book_weights(pos, repo_root)
    if not weights:
        return None
    return build_holdings_correlation_from_disk(repo_root, list(weights), weights)


def _build_tail_stress(pos: Positioning | None, db_path: Path | None) -> TailStress | None:
    """The all-bears book stress, from local ``dcf_runs`` bear scenarios —
    renders with the tracker DOWN (same weights degrade as the other local
    sections; the DB read itself needs no tracker)."""
    if db_path is None:
        return None
    weights = _local_book_weights(pos, db_path.parent.parent)
    if not weights:
        return None
    return build_tail_stress(db_path, weights)


def _build_monte_carlo(pos: Positioning | None, db_path: Path | None) -> MonteCarloRead | None:
    """The fat-tailed book Monte Carlo (PR4) — computed entirely from the
    local price cache, same weights degrade as the other local sections."""
    if db_path is None:
        return None
    weights = _local_book_weights(pos, db_path.parent.parent)
    if not weights:
        return None
    return build_book_monte_carlo(db_path.parent.parent, weights)


def _build_joint_latam_stress(
    pos: Positioning | None, db_path: Path | None
) -> EventStressResult | None:
    """The joint-LatAm event-correlation stress (PR4) — reads local
    ``dcf_runs`` bear scenarios (same substrate as ``_build_tail_stress``);
    renders with the tracker DOWN."""
    if db_path is None:
        return None
    weights = _local_book_weights(pos, db_path.parent.parent)
    if not weights:
        return None
    return build_joint_latam_stress(db_path, weights)


def _build_bear_lint(db_path: Path | None) -> BearLintReport | None:
    """The bear-realism lint (Monthly Red Team Phase 1 guard 2) — from the
    materialized weights cache, so it renders with the tracker DOWN like the
    other local Risk-tab sections. ``None`` when there is no DB or no weighted
    holdings (the caller renders the empty state)."""
    if db_path is None:
        return None
    weights = read_materialized_weights(db_path.parent.parent)
    if not weights:
        return None
    return build_bear_lint(db_path, db_path.parent.parent)


def _read_position_guard(db_path: Path | None) -> PositionGuardCacheModel | None:
    """The naked-position gate (Monthly Red Team Phase 1 guard 7) — READS the
    nightly-materialized cache (``data/dashboard/position_guard.json``,
    ``execution/refresh_position_guard.py``, morning-pipeline stage 0h) rather
    than recomputing: the whole point of materializing it is that a render
    never pays for the DB reads. ``None`` when there is no DB or no cache on
    file yet (the caller renders the empty state, distinct from "zero
    violations")."""
    if db_path is None:
        return None
    return read_position_guard_cache(db_path.parent.parent)


def _read_thesis_collision(pos: Positioning | None, db_path: Path | None) -> CachedReport | None:
    """The cached thesis-collision audit, filtered against the CURRENT
    holding set: a cached finding naming a name sold since the audit ran must
    not render as if it's still describing the live book (composition drift
    — see ``read_cached_report``'s ``current_tickers``). Falls back to the
    unfiltered cached read when the current holding set can't be derived
    (still safe: no LLM call, no crash), matching every other local section's
    degrade-gracefully discipline."""
    if db_path is None:
        return None
    weights = _local_book_weights(pos, db_path.parent.parent)
    if not weights:
        return read_cached_report(db_path)
    return read_cached_report(db_path, list(weights))


def _read_business_factor_vector(db_path: Path | None) -> BookFactorVector | None:
    """The C3 business-factor book vector (weights x persisted ``is_latest``
    loadings) — a pure local DB + materialized-weights-cache read, zero LLM,
    so it renders with the tracker DOWN like the other local Risk-tab
    sections. ``None`` when there is no DB (the caller renders the empty
    state that names the refresh command)."""
    if db_path is None:
        return None
    return book_factor_vector(db_path, db_path.parent.parent)


def _implicit_bets_section(
    snapshot: RiskSnapshot | None,
    weights: dict[str, float],
    factors: BookFactorVector | None,
) -> str:
    """The ranked implicit-bets statement — the risk page's organizing frame
    (Wave 3, surface_density_jit_redesign.md application map #3; the owner's
    own words chose this over a spider: the screen should state "what am I
    currently positioned for from a timing of different cycles").

    Every bet is DERIVED from data already on disk (risk snapshot, materialized
    weights, the C3 factor vector) and states its numbers inline; the sections
    below carry the evidence. Deterministic prose — no render-path LLM. Ranked
    by salience (weight/magnitude); an unpopulated source drops its bet."""
    bets: list[tuple[float, str]] = []

    if weights:
        top_t, top_w = max(weights.items(), key=lambda kv: kv[1])
        if top_w >= 0.10:
            bets.append(
                (
                    top_w * 4.0,
                    f"<strong>Single-name execution at {escape(top_t)}</strong> — "
                    f"{top_w * 100.0:.1f}% of the book rides one name's outcome. "
                    '<span class="pfr-bet-nums">Concentration zones + the collision '
                    "audit below carry the evidence.</span>",
                )
            )
        latam_w = sum(weights.get(t, 0.0) for t in ("NU", "MELI", "STNE"))
        if latam_w >= 0.10:
            bets.append(
                (
                    latam_w * 3.0,
                    "<strong>The LatAm credit/FX cycle</strong> — "
                    f"{latam_w * 100.0:.1f}% of the book moves on Brazil-credit "
                    "conditions hitting NU/MELI together. "
                    '<span class="pfr-bet-nums">The joint-LatAm event stress below '
                    "prices that exact scenario.</span>",
                )
            )

    if snapshot is not None and snapshot.growth_tilt is not None:
        g = snapshot.growth_tilt
        if abs(g) > 0.1:
            direction = "growth leadership over value" if g > 0 else "value leadership over growth"
            rate_txt = ""
            if snapshot.rate_beta_10y is not None:
                rate_txt = (
                    f" Rate sensitivity is near-neutral (10y β {snapshot.rate_beta_10y:+.2f})"
                    if abs(snapshot.rate_beta_10y) < 0.1
                    else f" 10y-rate β {snapshot.rate_beta_10y:+.2f}"
                )
            bets.append(
                (
                    abs(g),
                    f"<strong>The style cycle: {direction}</strong> "
                    f'<span class="pfr-bet-nums">(QQQ-SPY tilt {g:+.2f}).{rate_txt}</span>',
                )
            )

    if snapshot is not None and snapshot.beta is not None:
        b = snapshot.beta
        r2 = snapshot.r_squared
        idio = (
            f" — but R² {r2:.2f} means outcomes here are mostly stock-specific, not an index ride"
            if r2 is not None and r2 < 0.3
            else ""
        )
        bets.append(
            (
                min(abs(b - 1.0), 0.9),
                f"<strong>The market itself</strong> "
                f'<span class="pfr-bet-nums">(β {b:.2f}{idio}).</span>',
            )
        )

    if factors is not None and factors.vector:
        for name, loading in sorted(
            factors.vector.items(), key=lambda kv: abs(kv[1]), reverse=True
        )[:2]:
            if abs(loading) < 0.05:
                continue
            tops = factors.top_contributors.get(name, ())
            names = ", ".join(t for t, _ in tops[:3])
            bets.append(
                (
                    abs(loading),
                    f"<strong>{escape(name.replace('_', ' ').title())}</strong> "
                    f'<span class="pfr-bet-nums">(book loading {loading:+.2f}'
                    f"{' · via ' + escape(names) if names else ''}).</span>",
                )
            )

    head = (
        '<section class="panel pfr-bets"><h2>What am I positioned for?</h2>'
        '<p class="sub">The statement your holdings make about the world — the book\'s '
        "implicit bets, ranked by how much rides on each. The sections below carry "
        "the evidence.</p>"
    )
    if not bets:
        # D4: nothing derivable yet → one line naming what unlocks the read.
        return (
            head + '<p class="muted">Not derivable yet — a risk snapshot (morning '
            "pipeline) and materialized weights unlock this read.</p></section>"
        )
    items = "".join(f"<li>{line}</li>" for _, line in sorted(bets, key=lambda b: -b[0]))
    return f"{head}<ol>{items}</ol></section>"


# --------------------------------------------------------------------------- #
# Portfolio → Health chip fragments (Health console redesign, 2026-07-30).
# The Health console is two chip-tab cards — Theses (thesis / exposure /
# collisions) and Book risk (bets / drawdown / crowding / tail) — each pane
# fetched on first activation via /api/panel/portfolio_health?fragment=<key>.
# Every pane composes the SAME section renderers the standalone Synthesis and
# Risk builders use (no second code path); the sections cut from the console
# (Red Team, whole-book macro stress, style/business factors, the guards)
# stay reachable through the still-live /api/panel/portfolio_risk and
# /api/panel/red_team routes and the Ask doorways on the console brief.
# --------------------------------------------------------------------------- #

HEALTH_FRAGMENTS: tuple[str, ...] = (
    "thesis",
    "exposure",
    "collisions",
    "bets",
    "drawdown",
    "crowding",
    "tail",
)


def _quiet_note(text: str) -> str:
    return f'<section class="panel"><p class="muted">{escape(text)}</p></section>'


def render_health_fragment(db_path: Path, fragment: str) -> str:
    """One Health-console chip pane as a standalone HTML fragment. Each pane
    carries the CSS block its sections need (the same block the standalone
    builder emits), so a directly-fetched fragment styles itself."""
    if fragment == "thesis":
        from pipeline.analytical_dashboard import build_analytical_dashboard
        from pipeline.analytical_dashboard_html import render_panel_fragment

        dash = build_analytical_dashboard(db_path, sections={"portfolio_synthesis"})
        memo = _synthesis_memo_doorway(dash.portfolio_synthesis_md) or (
            render_panel_fragment(dash, "portfolio") or ""
        )
        rollup = _thesis_rollup_panel(db_path) or _quiet_note("No evaluated theses yet.")
        return _INSIGHTS_CSS + rollup + memo
    if fragment == "exposure":
        alive, base = probe_tracker(None)
        live = (
            fetch_live_portfolio()
            if alive
            else LivePortfolio(available=False, api_url=base, error=_PROBE_DOWN_ERROR)
        )
        exposure = _exposure_panel(db_path, live) or _quiet_note("No holdings to weight yet.")
        return _INSIGHTS_CSS + exposure
    if fragment == "collisions":
        return _RISK_CSS + _thesis_collision_section(_read_thesis_collision(None, db_path))
    if fragment == "bets":
        snapshot = read_latest_snapshot(db_path=db_path)
        weights = _local_book_weights(None, db_path.parent.parent)
        bets = _implicit_bets_section(snapshot, weights, _read_business_factor_vector(db_path))
        return _RISK_CSS + (
            bets or _quiet_note("Not enough on-disk data to state the book's bets yet.")
        )
    if fragment == "drawdown":
        alive, base = probe_tracker(None)
        analytics = (
            fetch_portfolio_analytics(only={"performance", "beta"})
            if alive
            else PortfolioAnalytics(
                available=False, api_url=base, errors={"performance": _PROBE_DOWN_ERROR}
            )
        )
        if analytics.available:
            parts: list[str] = []
            if analytics.beta is not None:
                parts.append(_risk_section(analytics.beta))
            dd = (
                compute_drawdown(analytics.performance.points)
                if analytics.performance is not None
                else None
            )
            parts.append(_drawdown_section(dd))
            return _RISK_CSS + "".join(parts)
        snap = read_latest_snapshot(db_path=db_path)
        if snap is not None:
            return _RISK_CSS + _cached_risk_section(snap)
        return _RISK_CSS + _risk_offline_note(analytics)
    if fragment == "crowding":
        return _RISK_CSS + _correlation_section(_build_correlation_read(None, db_path))
    if fragment == "tail":
        return _RISK_CSS + (
            _tail_stress_section(_build_tail_stress(None, db_path))
            + _monte_carlo_section(
                _build_monte_carlo(None, db_path), _build_joint_latam_stress(None, db_path)
            )
        )
    return _quiet_note(f"Unknown Health fragment: {fragment}")


def compose_risk_page(
    analytics: PortfolioAnalytics,
    *,
    drawdown: DrawdownStats | None,
    factor: FactorRollup | None,
    scenarios: list[tuple[str, str]],
    digest: str,
    snapshot: RiskSnapshot | None = None,
    gap: RiskRewardGap | None = None,
    style: StyleFactorRollup | None = None,
    correlation: CorrelationRead | None = None,
    tail_stress: TailStress | None = None,
    monte_carlo: MonteCarloRead | None = None,
    joint_latam: EventStressResult | None = None,
    bear_lint: BearLintReport | None = None,
    position_guard: PositionGuardCacheModel | None = None,
    collision: CachedReport | None = None,
    factors: BookFactorVector | None = None,
    bets: str = "",
) -> str:
    """Pure assembly of the Risk page (testable without network or DB). The
    ``#pfr-root`` wrapper is the re-inject target the run-scenario script swaps
    after a digest regenerates. When the tracker is offline, ``snapshot`` (the
    last-known cached read) renders stamped values instead of a bare note.
    ``gap`` (L7) is the risk-budget allocator's risk-vs-reward-vs-conviction
    ranking; None hides the section (tracker offline / no weighted book).
    ``style`` (the value/size/momentum ETF-proxy loadings), ``correlation``
    (the holdings pairwise matrix + crowding clusters), ``tail_stress`` (the
    all-bears book drawdown), ``monte_carlo`` (the fat-tailed book simulation)
    + ``joint_latam`` (its companion event-correlation stress), ``bear_lint``
    (the bear-realism lint, Monthly Red Team Phase 1 guard 2 — rides right
    alongside tail stress), ``position_guard`` (the naked-position gate,
    guard 7 — the nightly-materialized cache, never recomputed here),
    ``collision`` (the cached thesis-collision audit), and ``factors`` (the
    C3 business-factor book vector — persisted loadings x book weights) are
    computed from local disk/DB, so they render in BOTH branches —
    tracker up or down."""
    parts: list[str] = [_RISK_CSS, '<div id="pfr-root">']
    # The implicit-bets statement leads (Wave 3): the page's organizing frame,
    # rendered in BOTH branches — "" only when the caller didn't build it
    # (pure-assembly tests).
    if bets:
        parts.append(bets)
    if analytics.available:
        if analytics.beta is not None:
            parts.append(_risk_section(analytics.beta))
        parts.append(_drawdown_section(drawdown))
        if factor is not None:
            parts.append(_factor_exposure_section(factor))
        parts.append(_style_factor_section(style))
        parts.append(_correlation_section(correlation))
        parts.append(_tail_stress_section(tail_stress))
        parts.append(_monte_carlo_section(monte_carlo, joint_latam))
        parts.append(_bear_lint_section(bear_lint))
        parts.append(_position_guard_section(position_guard))
        parts.append(_thesis_collision_section(collision))
        parts.append(_business_factor_section(factors))
        if gap is not None:
            parts.append(_risk_reward_gap_section(gap))
    else:
        if snapshot is not None:
            parts.append(_cached_risk_section(snapshot))
        else:
            parts.append(_risk_offline_note(analytics))
        parts.append(_style_factor_section(style))
        parts.append(_correlation_section(correlation))
        parts.append(_tail_stress_section(tail_stress))
        parts.append(_monte_carlo_section(monte_carlo, joint_latam))
        parts.append(_bear_lint_section(bear_lint))
        parts.append(_position_guard_section(position_guard))
        parts.append(_thesis_collision_section(collision))
        parts.append(_business_factor_section(factors))
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
    # A degenerate tracker response (only one section loaded) used to CLOBBER
    # the last good snapshot with NULLs — `available` is True when ANY section
    # loads, and the sole prod row was all-NULL (2026-07-19 review, G5). A
    # capture must carry the sections the snapshot's substance comes from.
    if analytics.performance is None or analytics.positioning is None:
        return
    if drawdown is None:
        drawdown = compute_drawdown(analytics.performance.points)
    if factor is None:
        factor = factor_exposure_rollup(analytics.positioning.correlations)
    # §7.1.9 provenance: this opportunistic render-path write derives
    # rebase_basis the identical way the authoritative scheduled writer does
    # (execution/refresh_portfolio_risk_snapshot.py) — from the tracker's own
    # backfill_start_unreliable signal, never guessed — so a page-load-
    # triggered capture is just as comparable as a scheduled one, rather than
    # permanently reading as "unknown provenance" against every future delta.
    rebase_basis: RebaseBasis = (
        "modeled_backfill" if analytics.performance.backfill_start_unreliable else "observed"
    )
    write_snapshot(
        _build_risk_snapshot(analytics, drawdown, factor),
        db_path=db_path,
        metric_version=METRIC_VERSION,
        rebase_basis=rebase_basis,
    )


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
        conn = connect_sqlite(db_path, role=SQLiteConnectionRole.READ_ONLY)
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
        f'<p class="muted pfr-top">{escape(cov)}. Value / size / momentum load from local '
        "ETF-proxy spreads — the style-factor section below.</p>"
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


def _style_factor_section(style: StyleFactorRollup | None) -> str:
    """Value / size / momentum loadings from the local ETF-proxy spreads.

    Renders in the offline branch too — the substrate is entirely on-disk
    (FMP price cache + factor_proxies store). ``None`` (no proxy series
    fetched yet / no weights cache) gets the empty-state with the refresh
    command instead of a hidden section, mirroring ``_macro_stress_section``'s
    always-render pattern so the surface is discoverable before its first
    data arrives."""
    head = (
        '<section class="panel"><h2>Style factor loadings</h2>'
        '<p class="sub">Value / size / momentum betas of the book — each holding\'s daily '
        "returns regressed on a free ETF-proxy return spread (univariate OLS, the "
        "macro-sensitivity idiom), value-weighted over the names with an estimate. "
        "Local data only; renders with the tracker down.</p>"
    )
    if style is None:
        return (
            f"{head}"
            '<p class="muted">No proxy series (or weights cache) on file yet — run '
            "<code>python execution/fetch_factor_proxies.py</code> (morning-pipeline "
            "stage 0g keeps it fresh).</p></section>"
        )
    cards: list[str] = []
    for leg in style.legs:
        if leg.book_beta is None:
            continue
        cards.append(_kpi_card(f"{leg.label} β", f"{leg.book_beta:+.2f}", sub=leg.spread_label))
    bits = [
        f"{max((leg.names_priced for leg in style.legs), default=0)}"
        f" of {style.names_total} names priced"
    ]
    if style.proxies_through is not None:
        bits.append(f"proxies through {style.proxies_through.isoformat()}")
    bits.append(f"{style.lookback_obs}d window")
    note = f'<p class="muted pfr-top">{escape(" · ".join(bits))}.</p>'
    missing = (
        '<p class="muted pfr-top">Missing proxy series: '
        f"{escape(', '.join(style.missing_proxies))} — re-run "
        "<code>execution/fetch_factor_proxies.py</code>.</p>"
        if style.missing_proxies
        else ""
    )
    tops = "".join(
        _factor_top_line(f"Largest {leg.label.lower()} tilt", leg.top) for leg in style.legs
    )
    strip = f'<div class="kpi-strip">{"".join(cards)}</div>' if cards else ""
    return f'{head}{strip}{note}{missing}<div class="pfr-tops">{tops}</div></section>'


def _corr_cell_class(v: float, *, diagonal: bool) -> str:
    """Heat tone for one pairwise correlation cell."""
    if diagonal:
        return "pfc-cell pfc-diag"
    if v >= 0.8:
        return "pfc-cell pfc-c3"
    if v >= CLUSTER_CORR:
        return "pfc-cell pfc-c2"
    if v >= 0.5:
        return "pfc-cell pfc-c1"
    if v < 0.0:
        return "pfc-cell pfc-neg"
    return "pfc-cell"


def _correlation_section(read: CorrelationRead | None) -> str:
    """Holdings pairwise correlation + crowding clusters, from the local price
    cache. Renders in the offline branch too (same always-render pattern as the
    style-factor section); ``None`` gets the empty state, not a hidden section."""
    head = (
        '<section class="panel"><h2>Holdings correlation &amp; crowding</h2>'
        '<p class="sub">Pairwise correlation of the holdings\' daily returns over their common '
        "trading window (local price cache — renders with the tracker down), and the clusters "
        "that trade as one bet: names linked at corr &ge; "
        f"{CLUSTER_CORR:.2f} grouped, with their combined share of book.</p>"
    )
    if read is None:
        return (
            f"{head}"
            '<p class="muted">Not enough daily price history across the holdings for a pairwise '
            "read (needs two or more names with an overlapping window).</p></section>"
        )
    cards: list[str] = []
    if read.avg_pairwise_corr is not None:
        cards.append(
            _kpi_card("Avg pairwise corr", _ratio(read.avg_pairwise_corr), sub="off-diagonal mean")
        )
    if read.clusters:
        biggest = read.clusters[0]
        cards.append(
            _kpi_card(
                "Largest cluster",
                f"{biggest.combined_weight_pct:.0f}%",
                sub=f"{len(biggest.tickers)} names as one bet",
                tone="neg" if biggest.combined_weight_pct >= 25.0 else "",
            )
        )
    else:
        cards.append(_kpi_card("Clusters", "none", sub=f"no links at ≥{CLUSTER_CORR:.2f}"))
    strip = f'<div class="kpi-strip">{"".join(cards)}</div>' if cards else ""

    clusters_html = ""
    if read.clusters:
        lines = "".join(
            '<p class="pfr-top"><strong>'
            + " + ".join(ticker_label(t, href=f"../research/{escape(t)}/") for t in c.tickers)
            + "</strong>"
            f" — {c.combined_weight_pct:.0f}% of book moves as one bet "
            f'<span class="muted">(avg corr {c.avg_corr:.2f}, weakest link '
            f"{c.min_corr:.2f})</span></p>"
            for c in read.clusters
        )
        clusters_html = f'<div class="pfc-clusters">{lines}</div>'

    header_cells = "".join(f"<th>{escape(t)}</th>" for t in read.tickers)
    body_rows: list[str] = []
    for i, t in enumerate(read.tickers):
        cells: list[str] = []
        for j, u in enumerate(read.tickers):
            v = read.matrix[i][j]
            cls = _corr_cell_class(v, diagonal=i == j)
            text = "—" if i == j else f"{v:+.2f}"
            cells.append(
                f'<td class="{cls}" title="{escape(t)} x {escape(u)} · {v:+.2f}">{text}</td>'
            )
        body_rows.append(f'<tr><th class="pfc-row-h">{escape(t)}</th>{"".join(cells)}</tr>')
    table = (
        '<div class="pfc-scroll"><table class="pfc-table">'
        f"<thead><tr><th></th>{header_cells}</tr></thead>"
        f"<tbody>{''.join(body_rows)}</tbody></table></div>"
    )

    bits = [f"{len(read.tickers)} names", f"{read.n_obs} common trading days"]
    if read.prices_through is not None:
        bits.append(f"prices through {read.prices_through.isoformat()}")
    note = f'<p class="muted pfr-top">{escape(" · ".join(bits))}.</p>'
    dropped = (
        '<p class="muted pfr-top">Not modeled: '
        + escape("; ".join(f"{t} — {reason}" for t, reason in sorted(read.dropped.items())))
        + ".</p>"
        if read.dropped
        else ""
    )
    return f"{head}{strip}{clusters_html}{table}{note}{dropped}</section>"


def _tail_stress_row(r: TailStressRow) -> str:
    ticker = ticker_label(r.ticker, href=f"../research/{escape(r.ticker)}/")
    if r.excluded_reason is not None:
        return (
            "<tr>"
            f"<td>{ticker}</td>"
            f'<td class="num">{r.weight_pct:.1f}%</td>'
            f'<td class="num pts-excluded" colspan="3">not modeled — {escape(r.excluded_reason)}</td>'
            "</tr>"
        )
    bear_cell = (
        f'<span class="{_NUM_CLS[_tone(r.bear_return_pct)]}">{r.bear_return_pct:+.0f}%</span>'
        if r.bear_return_pct is not None
        else '<span class="muted">&mdash;</span>'
    )
    contrib_cell = (
        f'<span class="{_NUM_CLS[_tone(r.contribution_pct)]}">{r.contribution_pct:+.1f}pp</span>'
        if r.contribution_pct is not None
        else '<span class="muted">&mdash;</span>'
    )
    warn = (
        f' <span class="muted" title="{escape(r.confidence_reason)}">&#9888;</span>'
        if r.low_confidence and r.confidence_reason
        else ""
    )
    return (
        "<tr>"
        f"<td>{ticker}</td>"
        f'<td class="num">{r.weight_pct:.1f}%</td>'
        f'<td class="num">{_money(r.live_price) if r.live_price is not None else "—"}</td>'
        f'<td class="num">{_money(r.bear_fv) if r.bear_fv is not None else "—"}</td>'
        f'<td class="num">{bear_cell}</td>'
        f'<td class="num">{contrib_cell}{warn}</td>'
        "</tr>"
    )


def _coverage_warning_html(modeled_pct: float, *, noun: str = "book") -> str:
    """A leading UNMODELED warning pill when ``modeled_pct`` of the ``noun``
    sits below :data:`COVERAGE_WARN_PCT` — Monthly Red Team Phase 1 guard 1: a
    book-level scenario/reward rollup must never render its aggregate as a
    clean, healthy-looking number while the majority of the book carries no
    modeled read. ``""`` when coverage clears the bar (no warning to show)."""
    if modeled_pct >= COVERAGE_WARN_PCT:
        return ""
    tone = "k-pill-bad" if modeled_pct < COVERAGE_BAD_PCT else "k-pill-warn"
    unmodeled = 100.0 - modeled_pct
    return (
        '<p class="pfr-coverage-warn">'
        f'<span class="k-pill {tone}">{unmodeled:.0f}% OF {escape(noun.upper())} UNMODELED</span> '
        f"— only {modeled_pct:.0f}% of the {escape(noun)} carries a modeled read; the number "
        f"below is NOT a whole-{escape(noun)} figure.</p>"
    )


def _tail_stress_section(stress: TailStress | None) -> str:
    """The all-bears book drawdown, from local ``dcf_runs`` bear scenarios.
    Renders in both branches (local DB, no tracker dependency); ``None`` gets
    the empty state rather than a hidden section. Coverage gate (Monthly Red
    Team Phase 1 guard 1): below :data:`COVERAGE_WARN_PCT` modeled, a leading
    UNMODELED pill replaces the plain "% of book modeled" sub-line and the
    headline number's tone is stripped (never colored green/red as if it were
    a confident, whole-book read) — the design principle the 2026-07
    adversarial review named: 42%-modeled must not render healthy."""
    head = (
        '<section class="panel"><h2>Scenario-tail stress</h2>'
        '<p class="sub">If every holding fell to its DCF bear-case fair value tomorrow: the '
        "book-level drawdown implied by summing each name's weight &times; bear-case return "
        "(not a probability-weighted expectation — the joint-tail floor the scenario ranges "
        "imply). Local <code>dcf_runs</code> only; renders with the tracker down.</p>"
    )
    if stress is None:
        return (
            f"{head}"
            '<p class="muted">No weighted holdings to stress yet (needs the tracker or a '
            "materialized weights cache).</p></section>"
        )
    coverage_warn = _coverage_warning_html(stress.modeled_weight_pct)
    drawdown_sub = (
        f"over the {stress.modeled_weight_pct:.0f}% modeled — NOT the whole book"
        if coverage_warn
        else f"{stress.modeled_weight_pct:.0f}% of book modeled"
    )
    drawdown_tone = "" if coverage_warn else _tone(stress.book_drawdown_pct)
    cards = [
        _kpi_card(
            "All-bears book drawdown",
            f"{stress.book_drawdown_pct:+.1f}%",
            sub=drawdown_sub,
            tone=drawdown_tone,
        ),
        _kpi_card(
            "Coverage",
            f"{stress.names_with_bear} of {stress.names_total}",
            sub="names with a bear scenario",
        ),
    ]
    if stress.stale_weight_pct > 0:
        cards.append(
            _kpi_card(
                "Stale weight",
                f"{stress.stale_weight_pct:.0f}%",
                sub="fair value / price aging out",
                tone="neg",
            )
        )
    strip = f'<div class="kpi-strip">{"".join(cards)}</div>'
    rows_html = "".join(_tail_stress_row(r) for r in stress.rows)
    table = (
        '<table class="p-table pts-table"><thead><tr>'
        "<th>Ticker</th>"
        '<th class="num">Weight</th><th class="num">Live price</th>'
        '<th class="num">Bear FV</th><th class="num">Bear return</th>'
        '<th class="num">Contribution</th>'
        f"</tr></thead><tbody>{rows_html}</tbody></table>"
    )
    notes = "".join(f'<p class="muted pfr-top">{escape(n)}</p>' for n in stress.notes)
    return f"{head}{coverage_warn}{strip}{table}{notes}</section>"


_BEAR_LINT_STATUS_LABEL: dict[str, str] = {
    STATUS_NOT_A_BEAR: "NOT A BEAR",
    STATUS_MISSING: "MISSING",
    STATUS_SHALLOW: "SHALLOW",
    "ok": "OK",
}
_BEAR_LINT_STATUS_TONE: dict[str, str] = {
    STATUS_NOT_A_BEAR: "bad",
    STATUS_MISSING: "bad",
    STATUS_SHALLOW: "warn",
    "ok": "ok",
}


def _bear_lint_row(f: BearLintFinding) -> str:
    ticker = ticker_label(f.ticker, href=f"../research/{escape(f.ticker)}/")
    tone = _BEAR_LINT_STATUS_TONE[f.status]
    label = _BEAR_LINT_STATUS_LABEL[f.status]
    status_cell = f'<span class="k-pill k-pill-{tone}">{escape(label)}</span>'
    prov_cell = (
        f'<span class="k-chip k-chip-mono">{escape(f.provenance)}</span>'
        if f.provenance is not None
        else '<span class="muted">&mdash;</span>'
    )
    bear_cell = (
        f"{_money(f.bear_fv)} ({f.bear_return_pct:+.0f}%)"
        if f.bear_fv is not None and f.bear_return_pct is not None
        else '<span class="muted">&mdash;</span>'
    )
    return (
        "<tr>"
        f"<td>{ticker}</td>"
        f'<td class="num">{f.weight_pct:.1f}%</td>'
        f"<td>{status_cell}</td>"
        f'<td class="num">{_money(f.live_price) if f.live_price is not None else "—"}</td>'
        f'<td class="num">{bear_cell}</td>'
        f"<td>{prov_cell}</td>"
        f"<td>{escape(f.reason)}</td>"
        "</tr>"
    )


def _bear_lint_section(report: BearLintReport | None) -> str:
    """Bear-realism lint (Monthly Red Team Phase 1 guard 2): every held name's
    latest top-level DCF bear scenario, classified missing / not-a-bear /
    shallow / ok. Rides the same Risk-tab area as tail stress — the 2026-07
    adversarial review's #3 failure mode: fake ``BEAR_SEED`` bears sitting AT/
    ABOVE the live price for several names meant every scenario-reward
    consumer silently read "no downside" for half the book."""
    head = (
        '<section class="panel"><h2>Bear-realism lint</h2>'
        '<p class="sub">Every held name\'s latest top-level DCF bear scenario, checked against '
        "the live price: a bear case AT or ABOVE price isn't downside at all, and one less than "
        f"{SHALLOW_BEAR_FLOOR_PCT:.0f}% below price reads as ordinary volatility, not a "
        "thesis-break bear. Provenance names whether the bear came from the generic "
        "<code>BEAR_SEED</code> offset, a thesis-calibrated holdings override, or an owner "
        "workbook edit. Local <code>dcf_runs</code> only; renders with the tracker down.</p>"
    )
    if report is None or not report.findings:
        return (
            f"{head}"
            '<p class="muted">No weighted holdings to lint yet (needs the tracker or a '
            "materialized weights cache).</p></section>"
        )
    flagged = report.flagged
    counts: dict[str, int] = {}
    for f in report.findings:
        counts[f.status] = counts.get(f.status, 0) + 1
    cards = [
        _kpi_card(
            "Flagged",
            str(len(flagged)),
            sub=f"of {len(report.findings)} held names",
            tone="neg" if flagged else "",
        ),
    ]
    for status in (STATUS_NOT_A_BEAR, STATUS_MISSING, STATUS_SHALLOW):
        n = counts.get(status, 0)
        if n:
            cards.append(_kpi_card(_BEAR_LINT_STATUS_LABEL[status].title(), str(n)))
    strip = f'<div class="kpi-strip">{"".join(cards)}</div>'
    if not flagged:
        return (
            f'{head}{strip}<p class="muted pfr-top">Every held name clears the lint.</p></section>'
        )
    rows_html = "".join(_bear_lint_row(f) for f in flagged)
    table = (
        '<table class="p-table pts-table"><thead><tr>'
        "<th>Ticker</th>"
        '<th class="num">Weight</th><th>Status</th>'
        '<th class="num">Live price</th><th class="num">Bear FV (return)</th>'
        "<th>Provenance</th><th>Reason</th>"
        f"</tr></thead><tbody>{rows_html}</tbody></table>"
    )
    return f"{head}{strip}{table}</section>"


# ---------------------------------------------------------------------------
# Naked-position gate (Monthly Red Team Phase 1 guard 7). Reads the nightly-
# materialized cache (position_guard_cache.read_position_guard_cache) —
# NEVER recomputes ``position_guard.build_position_guard`` on the render
# path, matching the directive's "renders never recompute" contract for this
# guard specifically.
# ---------------------------------------------------------------------------

_PG_CHECK_ORDER: tuple[str, ...] = (CHECK_DOWNSIDE, CHECK_BEAR, CHECK_THESIS)
_PG_CHECK_LABEL: dict[str, str] = {
    CHECK_DOWNSIDE: "Downside trigger",
    CHECK_BEAR: "Realistic bear",
    CHECK_THESIS: "Thesis freshness",
}
_PG_CHECK_WHATS_WRONG: dict[str, str] = {
    CHECK_DOWNSIDE: "no downside rule",
    CHECK_BEAR: "no realistic bear",
    CHECK_THESIS: "thesis stale",
}
_PG_CHECK_FIX: dict[str, str] = {
    CHECK_DOWNSIDE: "encode an exit ladder (sizing intent or break_rules)",
    CHECK_BEAR: "persist a realistic DCF bear case",
    CHECK_THESIS: "refresh the thesis",
}


def _pg_field(row: PositionGuardRowModel, check: str) -> PositionGuardCheckModel:
    return {
        CHECK_DOWNSIDE: row.downside_trigger,
        CHECK_BEAR: row.realistic_bear,
        CHECK_THESIS: row.thesis_fresh,
    }[check]


def _naked_position_summary_pill(cache: PositionGuardCacheModel | None) -> str:
    """The compact Risk-panel summary row: ``NAKED POSITIONS: N``, k-pill-bad
    when N>0, k-pill-ok when the book is clean (or there's nothing to gate
    yet — an empty gate is not itself a violation)."""
    n = len(cache.violations) if cache is not None else 0
    tone = "k-pill-bad" if n > 0 else "k-pill-ok"
    return f'<p class="pfr-top"><span class="k-pill {tone}">NAKED POSITIONS: {n}</span></p>'


def _violation_chip(row: PositionGuardRowModel) -> str:
    """One dense standing chip per violation: ticker + which check(s) failed
    + the one-line fix — e.g. "FLKR: no downside rule — encode an exit
    ladder". The full per-check reasons ride the hover tooltip; the table
    below carries them inline for anyone who wants the detail without
    hovering."""
    failing = [c for c in _PG_CHECK_ORDER if c in row.failed_checks]
    whats_wrong = " + ".join(_PG_CHECK_WHATS_WRONG[c] for c in failing)
    fix = _PG_CHECK_FIX[failing[0]] if failing else ""
    tooltip = " · ".join(f"{_PG_CHECK_LABEL[c]}: {_pg_field(row, c).reason}" for c in failing)
    return (
        f'<a class="k-chip k-chip-bad" href="#holding={escape(row.ticker)}" '
        f'data-peek-ticker="{escape(row.ticker)}" title="{escape(tooltip)}">'
        f"{escape(row.ticker)}: {escape(whats_wrong)} — {escape(fix)}</a>"
    )


def _pg_check_cell(check: PositionGuardCheckModel) -> str:
    tone = "ok" if check.passed else "bad"
    label = "OK" if check.passed else "FAIL"
    return f'<td><span class="k-pill k-pill-{tone}">{label}</span> {escape(check.reason)}</td>'


def _position_guard_table_row(row: PositionGuardRowModel) -> str:
    ticker = ticker_label(row.ticker, href=f"../research/{escape(row.ticker)}/")
    return (
        "<tr>"
        f"<td>{ticker}</td>"
        f'<td class="num">{row.weight_pct:.1f}%</td>'
        f"{_pg_check_cell(row.downside_trigger)}"
        f"{_pg_check_cell(row.realistic_bear)}"
        f"{_pg_check_cell(row.thesis_fresh)}"
        "</tr>"
    )


def _add_trigger_advisories(cache: PositionGuardCacheModel) -> list[PositionGuardRowModel]:
    """High-conviction held names (module docstring "Bull-side symmetry",
    ``position_guard.CHECK_ADD``) missing an add-rung — the ADVISORY nudge
    list. Deliberately NOT ``cache.violations``: a row can clear all three
    violation-grade checks and still land here, and a row already in
    ``violations`` can land here too — the two lists are independent."""
    return [r for r in cache.rows if r.add_trigger is not None and not r.add_trigger.passed]


def _add_trigger_chip(row: PositionGuardRowModel) -> str:
    """One quiet chip — plain ``.k-chip`` (no ok/warn/bad tone), the
    "neutral nudge" rendering the advisory severity calls for (module
    docstring: missing upside is a nudge, never a violation-grade color)."""
    check = row.add_trigger
    assert check is not None  # caller filters via _add_trigger_advisories
    return (
        f'<a class="k-chip" href="#holding={escape(row.ticker)}" '
        f'data-peek-ticker="{escape(row.ticker)}" title="{escape(check.reason)}" '
        f'data-check="{CHECK_ADD}">'
        f"{escape(row.ticker)}: high conviction, no add-rung encoded</a>"
    )


def _add_trigger_advisory_block(cache: PositionGuardCacheModel) -> str:
    advisories = _add_trigger_advisories(cache)
    if not advisories:
        return ""
    chips = "".join(_add_trigger_chip(r) for r in advisories)
    return (
        '<p class="sub pfr-top"><strong>Add-rung advisory</strong> — high-conviction names '
        "with no encoded buy pre-commitment (sell rules but no buy rules). A nudge, not a "
        "violation: never counted in NAKED POSITIONS or the monthly-close gate.</p>"
        f'<div class="pfr-naked-chips">{chips}</div>'
    )


def _position_guard_section(cache: PositionGuardCacheModel | None) -> str:
    """Naked-position gate (Monthly Red Team Phase 1 guard 7): every held name
    above 0.5% needs a downside exit rule the platform can enforce, a
    realistic persisted bear case (bear-realism lint clears ok/shallow), and
    a thesis updated within the freshness window on file. Violations render
    as standing chips (one dense card per violation, ticker + failing
    check(s) + the one-line fix) plus a full per-check table; a summary
    k-pill leads the section either way. Reads the nightly-materialized
    cache — never recomputes on the render path.

    A fourth, ADVISORY row (``_add_trigger_advisory_block``) renders
    separately below, for high-conviction names with no encoded add-rung —
    never folded into the violations chips/table/pill above (Bull-side
    symmetry, PR9: missing downside is a violation, missing upside is a
    nudge)."""
    head = (
        '<section class="panel"><h2>Naked-position gate</h2>'
        '<p class="sub">Every held name above 0.5% of book needs all three on file: a '
        "downside exit rule the platform can enforce (a sizing-intent price rung or "
        "holdings-JSON break rules), a realistic persisted DCF bear case (the bear-realism "
        f"lint above must clear ok/shallow), and a thesis updated within "
        f"{THESIS_FRESHNESS_DAYS} days. Nightly-materialized "
        "(<code>data/dashboard/position_guard.json</code>) — violations block the monthly "
        "close (<code>directives/monthly_red_team.md</code> Phase 1).</p>"
    )
    if cache is None or not cache.rows:
        return (
            f"{head}{_naked_position_summary_pill(None)}"
            '<p class="muted pfr-top">No weighted holdings to gate yet (needs the morning '
            "pipeline to have run stage 0h at least once).</p></section>"
        )
    violations = cache.violations
    pill = _naked_position_summary_pill(cache)
    advisory = _add_trigger_advisory_block(cache)
    if not violations:
        return (
            f"{head}{pill}"
            '<p class="muted pfr-top">Every held name clears the naked-position gate.'
            f"</p>{advisory}</section>"
        )
    chips = "".join(_violation_chip(r) for r in violations)
    rows_html = "".join(_position_guard_table_row(r) for r in violations)
    table = (
        '<table class="p-table pts-table"><thead><tr>'
        "<th>Ticker</th>"
        '<th class="num">Weight</th><th>Downside trigger</th>'
        "<th>Realistic bear</th><th>Thesis freshness</th>"
        f"</tr></thead><tbody>{rows_html}</tbody></table>"
    )
    return f'{head}{pill}<div class="pfr-naked-chips">{chips}</div>{table}{advisory}</section>'


def _mc_prob_row(label: str, normal: DistributionRead, student_t: DistributionRead) -> str:
    return (
        f"<tr><td>&lt;{escape(label)}</td>"
        f'<td class="num">{normal.prob_below.get(label, 0.0) * 100.0:.1f}%</td>'
        f'<td class="num">{student_t.prob_below.get(label, 0.0) * 100.0:.1f}%</td></tr>'
    )


def _joint_latam_block(stress: EventStressResult | None) -> str:
    if stress is None:
        return (
            '<div class="k-well pfr-top"><strong>Joint-LatAm stress</strong> '
            '<span class="muted">— not enough weighted holdings to stress.</span></div>'
        )
    bad = stress.book_return_pct <= -15.0
    pill_tone = "k-pill-bad" if bad else "k-pill-warn"
    well_tone = "k-well-bad" if bad else "k-well-warn"
    top_legs = sorted(stress.legs, key=lambda leg: leg.return_pct)[:5]
    legs_html = ", ".join(
        f"{ticker_label(leg.ticker, href=f'../research/{escape(leg.ticker)}/')} "
        f'<span class="{_NUM_CLS[_tone(leg.return_pct)]}" title="{escape(leg.label)}">'
        f"{leg.return_pct:+.0f}%</span>"
        for leg in top_legs
    )
    notes = (
        f'<p class="muted pfr-top">{escape("; ".join(stress.notes))}</p>' if stress.notes else ""
    )
    return (
        f'<div class="k-well {well_tone} pfr-top">'
        f"<strong>{escape(stress.title)}</strong> "
        f'<span class="k-pill {pill_tone}">{stress.book_return_pct:+.1f}% book</span>'
        f'<p class="muted pfr-top">{escape(stress.description)}</p>'
        f'<p class="pfr-top">{legs_html}</p>{notes}</div>'
    )


def _monte_carlo_section(mc: MonteCarloRead | None, joint_latam: EventStressResult | None) -> str:
    """Fat-tailed book Monte Carlo (PR4, directives/monthly_red_team.md Phase
    3): a multivariate-normal AND multivariate Student-t (df=4) annual-horizon
    simulation from the local price cache's aligned daily covariance (one
    shared crash-mixing chi-square draw per path, so the tails co-move),
    beside its companion joint-LatAm event-correlation stress. Local price
    cache + local ``dcf_runs`` only; renders with the tracker down, same as
    the correlation/style/tail-stress sections above it."""
    head = (
        '<section class="panel"><h2>Tail risk (Monte Carlo)</h2>'
        f'<p class="sub">{DEFAULT_N_PATHS:,}-path simulation of the book\'s ANNUAL return '
        "from the local price cache's aligned daily covariance (normal vs Student-t, "
        f"df={DEFAULT_T_DF} &mdash; one shared crash-mixing chi-square draw per path so the "
        "tails co-move, not simulated day-by-day since that would CLT-average the fat tail "
        "away), plus a deterministic joint-LatAm event stress. Local data only; renders "
        "with the tracker down.</p>"
    )
    if mc is None:
        return (
            f"{head}"
            '<p class="muted">Not enough daily price history across two or more modeled '
            "holdings for a book simulation yet.</p></section>"
        )
    # The normal model's vol is the well-behaved covariance-implied number (the
    # CMA comparison point — directives/monthly_red_team.md Phase 3's "~22-27%
    # book" is this figure); the t-model's arithmetic vol is NOT shown as a
    # headline stat — expm1() of a heavy-tailed log-shock makes the simple-
    # return std dev wildly sensitive to a handful of extreme upside draws
    # (the same convexity that makes lognormal mean != median), so the
    # t-distribution's honest contribution here is its PERCENTILES/
    # probabilities (robust to a few outlier paths), not its variance.
    cma_note = (
        f"wealthplan CMA assumes {WEALTHPLAN_CMA_ASSUMED_VOL_PCT:.0f}% vol &middot; this book "
        f"simulates &sim;{mc.normal.vol_pct:.0f}%"
    )
    p30 = mc.student_t.prob_below.get("-30%", 0.0)
    cards = [
        _kpi_card(
            "t-dist 1st pctile (annual)",
            f"{mc.student_t.pct_1st:+.0f}%",
            sub=f"normal {mc.normal.pct_1st:+.0f}%",
            tone=_tone(mc.student_t.pct_1st),
        ),
        _kpi_card(
            "P(book &lt; -30%)",
            f"{p30 * 100.0:.1f}%",
            sub=f"normal {mc.normal.prob_below.get('-30%', 0.0) * 100.0:.1f}%",
            tone="neg" if p30 > 0.02 else "",
        ),
        _kpi_card(
            "Book vol",
            f"{mc.normal.vol_pct:.1f}%",
            sub="annualized · normal/covariance basis",
        ),
        _kpi_card(
            "Coverage",
            f"{mc.modeled_weight_pct:.0f}%",
            sub=f"of book modeled ({len(mc.tickers)} names + cash-likes)",
            tone="neg" if mc.modeled_weight_pct < 90.0 else "",
        ),
    ]
    strip = f'<div class="kpi-strip">{"".join(cards)}</div>'
    cma_chip = f'<p class="pfr-top"><span class="k-chip k-chip-warn">{cma_note}</span></p>'
    prob_rows = "".join(_mc_prob_row(label, mc.normal, mc.student_t) for label in DRAWDOWN_LABELS)
    table = (
        '<table class="p-table pfm-table"><thead><tr><th>Book return</th>'
        '<th class="num">P (normal)</th><th class="num">P (t-dist)</th>'
        f"</tr></thead><tbody>{prob_rows}</tbody></table>"
    )
    bits = [f"{mc.n_paths:,} paths", f"seed {mc.seed}", f"{mc.n_obs} common trading days"]
    if mc.prices_through is not None:
        bits.append(f"prices through {mc.prices_through.isoformat()}")
    note = f'<p class="muted pfr-top">{escape(" · ".join(bits))}. {escape(mc.drift_source)}.</p>'
    dropped = (
        '<p class="muted pfr-top">Not modeled: '
        + escape("; ".join(f"{t} — {reason}" for t, reason in sorted(mc.dropped.items())))
        + ".</p>"
        if mc.dropped
        else ""
    )
    latam_html = _joint_latam_block(joint_latam)
    return f"{head}{strip}{cma_chip}{table}{note}{dropped}{latam_html}</section>"


def _thesis_collision_section(cached: CachedReport | None) -> str:
    """Whole-book thesis-collision audit: shared-driver clusters + contradictory
    theses, from the cached LLM finding (never generated on the render path).
    ``None`` gets the empty state naming the refresh command."""
    head = (
        '<section class="panel"><h2>Thesis collisions</h2>'
        '<p class="sub">Names that look independent on price stats but really share '
        "one underlying driver, and theses that make directly contradictory bets — a "
        "governed LLM read over every holding's thesis + tier-1 break-rule drivers. "
        "Cached; regenerated on demand, not on every page load.</p>"
    )
    if cached is None:
        return (
            f"{head}"
            '<p class="muted">No audit on file yet — run '
            "<code>python execution/run_thesis_collision.py</code> "
            "(re-running is free once the thesis set is unchanged).</p></section>"
        )
    report = cached.report
    stamp = stamp_html(cached.generated_at, mode="date", prefix="as of ")
    n = len(report.tickers_analyzed)
    stale_note = (
        '<p class="muted pfr-top ptc-stale">Portfolio changed since this audit — findings '
        "naming " + escape(", ".join(cached.stale_tickers)) + " (no longer held) were "
        "dropped. Re-run the audit to cover the current book.</p>"
        if cached.portfolio_changed
        else ""
    )
    if not report.clusters and not report.contradictions:
        return (
            f"{head}{stale_note}"
            f'<p class="muted">No shared-driver clusters or contradictions found across '
            f"{n} names ({stamp}).</p></section>"
        )
    findings: list[str] = []
    for c in report.clusters:
        chips = " + ".join(ticker_label(t, href=f"../research/{escape(t)}/") for t in c.tickers)
        findings.append(
            '<div class="ptc-finding">'
            f'<p class="ptc-finding-head"><strong>{chips}</strong> — {escape(c.driver)}</p>'
            f'<p class="ptc-finding-rationale">{escape(c.rationale)}</p></div>'
        )
    for c in report.contradictions:
        chips = " vs ".join(ticker_label(t, href=f"../research/{escape(t)}/") for t in c.tickers)
        findings.append(
            '<div class="ptc-finding ptc-finding-bad">'
            f'<p class="ptc-finding-head"><strong>{chips}</strong> — {escape(c.contradiction)}</p>'
            f'<p class="ptc-finding-rationale">{escape(c.rationale)}</p></div>'
        )
    note = (
        f'<p class="muted pfr-top">{n} names analyzed · {len(report.clusters)} shared-driver '
        f"clusters · {len(report.contradictions)} contradictions · {stamp}.</p>"
    )
    return f'{head}{stale_note}<div class="ptc-findings">{"".join(findings)}</div>{note}</section>'


def _business_factor_section(factors: BookFactorVector | None) -> str:
    """C3: the book's business-factor exposure vector — book weight x
    persisted loading, summed per taxonomy factor, with each factor's top-3
    contributing tickers. Reuses the ``.pf-exp-*`` bar vocabulary the sector
    Exposure section already established (same visual need: a labeled bar +
    a percent). Empty/None gets the empty state naming the refresh command,
    matching the thesis-collision section's convention."""
    head = (
        '<section class="panel"><h2>Business-factor exposure</h2>'
        '<p class="sub">What the book is actually a bet on, independent of ticker or '
        "sector — LLM loadings onto a small controlled taxonomy, grounded in each "
        "holding's disclosed revenue mix or thesis. Cached; regenerated on demand, "
        "not on every page load.</p>"
    )
    if factors is None or not factors.vector:
        return (
            f"{head}"
            '<p class="muted">No business-factor exposures on file yet — run '
            "<code>python execution/refresh_business_factors.py</code> "
            "(re-running is free once no holding's mix/thesis has changed).</p></section>"
        )
    top = sorted(factors.vector.items(), key=lambda kv: kv[1], reverse=True)
    rows: list[str] = []
    for factor, share in top:
        contributors = factors.top_contributors.get(factor, ())
        chips = " ".join(
            f'<span class="k-chip k-chip-mono">{escape(t)} {c * 100:.0f}%</span>'
            for t, c in contributors
        )
        rows.append(
            '<div class="pf-exp-row">'
            f'<span class="pf-exp-label">{escape(factor)}</span>'
            f'<span class="pf-exp-bar"><span style="width:{min(share, 1.0) * 100:.0f}%"></span></span>'
            f'<span class="pf-exp-pct">{share * 100:.0f}%</span>'
            f"</div>"
            f'<p class="muted pfr-top ptc-finding-rationale">{chips}</p>'
        )
    return f'{head}<div class="pf-exp">{"".join(rows)}</div></section>'


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
    # Coverage gate (Monthly Red Team Phase 1 guard 1): the reward leg is a
    # book-level scenario-reward rollup too — a majority-unscored reward share
    # must not read as a quiet footnote fraction, same bar as tail stress.
    valued_pct = gap.valued_names / len(gap.rows) * 100.0
    coverage_warn = _coverage_warning_html(valued_pct, noun="reward")
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
    return f"{head}{coverage_warn}{sub}{table}{notes}</section>"


def _rrg_row(r: RiskRewardGapRow) -> str:
    ticker = ticker_label(r.ticker, href=f"../research/{escape(r.ticker)}/")
    if r.gap_pct is not None:
        gap_tone = "neg" if r.gap_pct > 0 else "pos"
        gap_cell = f'<span class="{_NUM_CLS[gap_tone]}">{r.gap_pct:+.0f}pp</span>'
    else:
        gap_cell = '<span class="muted">&mdash;</span>'
    if r.expected_return_pct is not None:
        e_tone = "pos" if r.expected_return_pct >= 0 else "neg"
        warn = (
            f' <span class="muted" title="{escape(r.confidence_reason)}">&#9888;</span>'
            if r.low_confidence and r.confidence_reason
            else ""
        )
        exp_cell = f'<span class="{_NUM_CLS[e_tone]}">{r.expected_return_pct:+.0f}%</span>{warn}'
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


def _tracker_offline_banner(
    live: LivePortfolio, *, refresh_endpoint: str = "/api/panel/portfolio"
) -> str:
    """The page's gate: the whole Portfolio page reads from the companion
    tracker, so when it is down this LEADS the page (prominent, not buried) and
    auto-starts on open. One-click start runs ``/actions/start-tracker`` (the
    tracker's own venv, from its checkout) and the panel re-fetches itself until
    :8000 answers; the raw requests repr stays in the collapsed details."""
    return (
        # Class hooks, not ids (Phase-5 verifier): this banner renders in BOTH
        # the Health console (Synthesis) and the Allocation console
        # (Performance); duplicate ids left the second instance's Start button
        # dead. _START_TRACKER_JS wires every unwired .pf-live-offline subtree.
        '<section class="panel pf-tracker-banner pf-live-offline" '
        f'data-refresh-endpoint="{escape(refresh_endpoint, quote=True)}">'
        "<h2>Portfolio tracker</h2>"
        '<p class="sub">This whole page reads from the companion portfolio-tracker — '
        "live positions, performance vs benchmarks, risk, and allocation. It isn't "
        "running yet, so there's nothing to show until it starts.</p>"
        f'<p class="muted">{escape(_offline_reason(live.error))}</p>'
        '<div class="pf-tracker-actions">'
        '<button type="button" class="pf-start-tracker k-btn k-btn-primary">'
        "Start tracker</button>"
        '<span class="pf-start-msg muted">starting automatically…</span>'
        "</div>"
        '<pre class="pf-start-log cli-hint" '
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


def render_live_portfolio_section(live: LivePortfolio) -> str:
    """The live-positions panel: total + taxable-bucket KPI strip, a positions
    table with % of portfolio, and the latest transactions. When the tracker is
    unreachable it returns the prominent start-tracker banner (which the page
    composer also floats to the top)."""
    if not live.available:
        return _tracker_offline_banner(live)
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
    # Largest position first (the client can re-sort; this is the default view).
    for p in sorted(live.positions, key=lambda x: -(x.market_value or 0.0)):
        treatments = sorted({lot.tax_treatment for lot in p.accounts})
        treat_str = ", ".join(_TAX_LABELS.get(t, t) for t in treatments) or "—"
        pnl = p.unrealized_pnl
        pnl_cell = (
            f'<td class="num {"k-num-pos" if pnl >= 0 else "k-num-neg"}">{_money(pnl)}</td>'
            if pnl is not None
            else '<td class="num muted">—</td>'
        )
        pct = f"{p.percent_of_portfolio:.1f}%" if p.percent_of_portfolio is not None else "—"
        ticker = p.ticker or "—"
        ticker_cell = (
            ticker_label(ticker, href="../research/" + escape(ticker) + "/") if p.ticker else "—"
        )
        data = (
            lg.data_text(f"{p.ticker or ''} {p.name or ''} {treat_str}")
            + lg.data_text_key("ticker", p.ticker)
            + lg.data_text_key("name", p.name)
            + lg.data_text_key("treat", treat_str)
            + lg.data_num("shares", p.quantity)
            + lg.data_num("mv", p.market_value)
            + lg.data_num("pct", p.percent_of_portfolio)
            + lg.data_num("pnl", p.unrealized_pnl)
        )
        rows.append(
            f"<tr{data}>"
            f"<td>{ticker_cell}</td>"
            f"<td>{escape(p.name or '—')}</td>"
            f'<td class="num">{p.quantity:,.2f}</td>'
            f'<td class="num">{_money(p.market_value)}</td>'
            f'<td class="num">{pct}</td>'
            f"{pnl_cell}"
            f"<td>{escape(treat_str)}</td>"
            "</tr>"
        )
    headers = (
        lg.th("Ticker", "ticker", "text", num=False)
        + lg.th("Name", "name", "text", num=False)
        + lg.th("Shares", "shares", "num")
        + lg.th("Market value", "mv", "num")
        + lg.th("% of book", "pct", "num")
        + lg.th("Unrealized P&amp;L", "pnl", "num")
        + lg.th("Tax treatment", "treat", "text", num=False)
    )
    return (
        lg.grid_open()
        + lg.filter_bar(len(live.positions), noun="positions")
        + '<table class="positions-table"><thead><tr>'
        + headers
        + "</tr></thead><tbody>"
        + "".join(rows)
        + "</tbody></table>"
        + lg.grid_close()
    )


def _transactions_section(live: LivePortfolio) -> str:
    if not live.transactions:
        return ""
    rows: list[str] = []
    for t in live.transactions:
        kind = t.type + (f" · {t.subtype}" if t.subtype else "")
        qty = f"{t.quantity:,.2f}" if t.quantity is not None else "—"
        iso_date = t.date[:10]
        # data-date is the ISO date — text-sorts chronologically.
        data = (
            lg.data_text(f"{iso_date} {t.ticker or ''} {kind} {t.account_name}")
            + lg.data_text_key("date", iso_date)
            + lg.data_text_key("ticker", t.ticker)
            + lg.data_text_key("type", kind)
            + lg.data_text_key("account", t.account_name)
            + lg.data_num("qty", t.quantity)
            + lg.data_num("amount", t.amount)
        )
        rows.append(
            f"<tr{data}>"
            f"<td>{escape(iso_date)}</td>"
            f"<td>{escape(t.ticker or '—')}</td>"
            f"<td>{escape(kind)}</td>"
            f'<td class="num">{qty}</td>'
            f'<td class="num">{_money(t.amount)}</td>'
            f"<td>{escape(t.account_name)}</td>"
            "</tr>"
        )
    headers = (
        lg.th("Date", "date", "text", num=False)
        + lg.th("Ticker", "ticker", "text", num=False)
        + lg.th("Type", "type", "text", num=False)
        + lg.th("Shares", "qty", "num")
        + lg.th("Amount", "amount", "num")
        + lg.th("Account", "account", "text", num=False)
    )
    return (
        '<section class="panel"><h2>Latest transactions</h2>'
        '<p class="sub">Most recent trades + cashflows across all linked accounts.</p>'
        + lg.grid_open()
        + lg.filter_bar(len(live.transactions), noun="transactions")
        + '<table class="txn-table"><thead><tr>'
        + headers
        + "</tr></thead><tbody>"
        + "".join(rows)
        + "</tbody></table>"
        + lg.grid_close()
        + "</section>"
    )


def _money(v: float | None) -> str:
    if v is None:
        return "—"
    if abs(v) >= 1000:
        return f"${v:,.0f}"
    return f"${v:,.2f}"
