"""Unified tabbed command-center shell.

One page, served at ``GET /`` by ``execution/comments_server.py``, that folds the
former three vertically-scrolling pages (``/``, ``/analytical``, ``/ticker/<t>``)
into a single dark-themed app with a horizontal tab bar.

Design — thin shell + lazy panels:

* **Overview is server-inlined** (instant first paint): the tier-coverage strip,
  the IR-KPI + maintenance action blocks, and the portfolio/evaluation status
  tables — all reused verbatim from ``dashboard_html`` / ``analytical_dashboard_html``.
* **Every other tab lazy-loads its HTML on first activation** via
  ``GET /api/panel/<name>`` (the head/foot-less fragments from PR 5), then caches
  it in the DOM. Inline ``<script>`` blocks inside a fragment (e.g. the budget
  Save-button wiring) are re-executed after injection — ``innerHTML`` alone does
  not run them.
* **Pre-reads + Insiders are dropdown-driven** (``cc-picker``): the panel ships an
  "All holdings" view and re-fetches ``endpoint?ticker=<T>`` server-side on change
  (rereads are ~8 KB each and the insider scan is heavy, so client-side filtering
  would defeat the lazy shell). The same pattern serves the per-ticker Holding tab.
* **Deep-linkable** via ``location.hash`` — ``#holdings``, ``#prereads=NU``,
  ``#holding=NU`` — with ``hashchange`` driving back/forward.

The standalone ``/analytical`` and ``/ticker/<t>`` pages remain as working
deep-link targets (zero rewrite); this shell is purely additive.

All HTML is server-side f-strings + vanilla JS (no template engine), matching the
rest of the dashboard. ``SHELL_CSS`` / ``SHELL_JS`` are plain string constants so
their braces pass through untouched; only the small assembly bits interpolate.
"""

from __future__ import annotations

from datetime import UTC, datetime
from html import escape

from pipeline.dashboard_status import DashboardRow

# Tab order. Each entry: (panel_id, label, endpoint, is_picker, picker_required).
# ``overview`` is inlined (no endpoint). The picker tabs re-fetch ``endpoint?ticker=``
# on dropdown change; ``picker_required`` panels (none yet — the Holding tab in a
# follow-up) show a "pick a holding" prompt until one is chosen, while filter
# pickers (prereads, insiders) default to the cross-ticker "All" view.
_TABS: tuple[tuple[str, str, str | None, bool, bool], ...] = (
    ("overview", "Overview", None, False, False),
    ("portfolio", "Portfolio", "/api/panel/portfolio", False, False),
    ("holdings", "Holdings", "/api/panel/holdings", False, False),
    ("prereads", "Pre-reads", "/api/panel/prereads", True, False),
    ("insiders", "Insiders", "/api/panel/insiders", True, False),
    ("predictions", "Predictions", "/api/panel/predictions", False, False),
    ("decisions", "Decisions", "/api/panel/decisions", False, False),
    ("budget", "LLM Spend", "/api/panel/budget", False, False),
)


def render_overview_panel(
    rows_by_list: dict[str, list[DashboardRow]],
    coverage: dict[str, dict[str, int]] | None,
) -> str:
    """The inlined Overview tab: tier-coverage strip + IR-KPI/maintenance actions
    + the portfolio & evaluation status tables. Reuses the existing public seams
    so there is no second code path for any of this content."""
    from pipeline.analytical_dashboard_html import render_tier_coverage_strip
    from pipeline.dashboard_html import render_status_overview

    return render_tier_coverage_strip(coverage or {}) + render_status_overview(rows_by_list)


def render_shell(
    *,
    overview_html: str,
    generated_at: datetime | None = None,
    tabs: tuple[tuple[str, str, str | None, bool, bool], ...] = _TABS,
) -> str:
    """Render the full command-center document.

    ``overview_html`` is the pre-built Overview panel (see ``render_overview_panel``)
    inlined for first paint; every other tab is an empty placeholder that
    lazy-loads from its ``/api/panel/<name>`` endpoint on first activation.
    """
    stamp = (generated_at or datetime.now(UTC)).isoformat(timespec="seconds")
    return "".join(
        [
            _DOC_HEAD,
            f'<div class="cc-topbar"><div class="cc-brand">Command Center</div>'
            f'<span class="cc-stamp">generated {escape(stamp)}</span></div>',
            _render_tab_bar(tabs),
            '<main class="cc-panels">',
            _render_panels(tabs, overview_html),
            "</main>",
            f"<script>{SHELL_JS}</script>",
            _DOC_FOOT,
        ]
    )


def _render_tab_bar(tabs: tuple[tuple[str, str, str | None, bool, bool], ...]) -> str:
    out = ['<nav class="cc-tabs" role="tablist">']
    for pid, label, _endpoint, _picker, _required in tabs:
        out.append(
            f'<button class="cc-tab" type="button" role="tab" '
            f'data-tab-target="{escape(pid)}">{escape(label)}</button>'
        )
    out.append("</nav>")
    return "".join(out)


def _render_panels(
    tabs: tuple[tuple[str, str, str | None, bool, bool], ...], overview_html: str
) -> str:
    out: list[str] = []
    for pid, _label, endpoint, picker, required in tabs:
        if pid == "overview":
            out.append(
                f'<section class="cc-panel" data-panel="{escape(pid)}" data-loaded="1">'
                f'<div class="cc-panel-body">{overview_html}</div></section>'
            )
            continue
        ep = f' data-endpoint="{escape(endpoint)}"' if endpoint else ""
        pk = ' data-picker="1"' if picker else ""
        rq = ' data-picker-required="1"' if required else ""
        picker_html = (
            '<div class="cc-picker-wrap">'
            '<select class="cc-picker" aria-label="Filter by ticker"></select>'
            "</div>"
            if picker
            else ""
        )
        out.append(
            f'<section class="cc-panel" data-panel="{escape(pid)}"{ep}{pk}{rq} '
            f'data-loaded="0" data-current-ticker="" hidden>'
            f"{picker_html}"
            '<div class="cc-panel-body"><div class="cc-loading">Loading…</div></div>'
            "</section>"
        )
    return "".join(out)


# The workspace `:root` vars (--accent/--ink/--panel/--hairline/...) are defined
# here too so any later-inlined comment/chat CSS resolves against the dark theme.
SHELL_CSS = """
:root {
  --bg: #0c0d10;
  --panel: #16171a;
  --panel-alt: #1f2125;
  --bg-card: #16171a;
  --bg-elev: #1f2125;
  --border: #2a2c30;
  --hairline: #2a2c30;
  --row-hover: #1f2125;
  --ink: #e5e5e2;
  --fg: #e5e5e2;
  --ink-muted: #aaa;
  --fg-muted: #888;
  --muted: #888;
  --link: #6ea8fe;
  --accent: #6db3ff;
  --ok: #4ade80;
  --warn: #fbbf24;
  --bad: #f87171;
  --font-mono: 'JetBrains Mono', ui-monospace, SFMono-Regular, Menlo, monospace;
  --font-body: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
}
* { box-sizing: border-box; }
body { margin: 0; padding: 0; font-family: var(--font-body); background: var(--bg);
  color: var(--ink); line-height: 1.5; font-size: 14px; }
a { color: var(--link); }

/* Top bar + tabs */
.cc-topbar { display: flex; justify-content: space-between; align-items: baseline;
  padding: 16px 24px 10px; }
.cc-brand { font-size: 18px; font-weight: 700; letter-spacing: 0.2px; }
.cc-stamp { color: var(--muted); font-size: 12px; font-family: var(--font-mono); }
.cc-tabs { display: flex; gap: 2px; padding: 0 16px; border-bottom: 1px solid var(--border);
  overflow-x: auto; position: sticky; top: 0; background: var(--bg); z-index: 20; }
.cc-tab { background: transparent; border: none; border-bottom: 2px solid transparent;
  color: var(--muted); padding: 10px 16px; font-size: 13px; font-weight: 600;
  cursor: pointer; white-space: nowrap; font-family: var(--font-body); }
.cc-tab:hover { color: var(--ink); }
.cc-tab.active { color: var(--ink); border-bottom-color: var(--accent); }

.cc-panels { padding: 22px 24px 64px; max-width: 1280px; margin: 0 auto; }
.cc-panel[hidden] { display: none; }
.cc-loading, .cc-empty { color: var(--muted); font-size: 13px; padding: 24px 4px; }

/* Ticker picker (Pre-reads / Insiders / Holding) */
.cc-picker-wrap { margin-bottom: 16px; }
.cc-picker { background: var(--panel-alt); color: var(--ink); border: 1px solid var(--border);
  border-radius: 6px; padding: 7px 10px; font-size: 13px; min-width: 260px;
  font-family: var(--font-body); }

/* ============================================================
   Analytical / command-center panel vocabulary (dark)
   Lifted from analytical_dashboard_html + ticker_command_center so the
   lazy fragments + the inlined overview render identically here.
   ============================================================ */
h1 { font-size: 24px; margin: 0 0 8px; font-weight: 600; }
h2 { font-size: 18px; margin: 0 0 6px; font-weight: 600; }
h3 { font-size: 15px; margin: 0; font-weight: 600; }
.panel { margin-bottom: 28px; background: var(--panel); border: 1px solid var(--border);
  border-radius: 8px; padding: 18px 20px; }
.panel .sub { color: #999; font-size: 12px; margin: 0 0 16px; }
.muted { color: var(--muted); }
table { width: 100%; border-collapse: collapse; font-size: 13px; }
th { text-align: left; padding: 8px 10px; border-bottom: 2px solid var(--border);
  font-family: var(--font-mono); font-size: 11px; text-transform: uppercase;
  letter-spacing: 0.5px; color: var(--muted); font-weight: 600; }
td { padding: 8px 10px; border-bottom: 1px solid #1f2125; vertical-align: top; }
td.num { text-align: right; font-variant-numeric: tabular-nums; }
td.muted { color: #666; }
td.pos { color: var(--ok); }
td.neg { color: var(--bad); }
.ticker-link { color: #f5f5f0; text-decoration: none; font-weight: 600; }
.ticker-link:hover { color: #aaa; }
tr.tone-sell { background: rgba(248, 113, 113, 0.06); }
tr.tone-trim { background: rgba(251, 191, 36, 0.04); }
tr.tone-init { background: rgba(74, 222, 128, 0.06); }
tr.tx-buy { background: rgba(74, 222, 128, 0.04); }
tr.tx-sell { background: rgba(248, 113, 113, 0.02); }
td.trigger-cell { font-family: var(--font-mono); font-size: 11px; text-transform: uppercase; }
tr.tone-sell .trigger-cell { color: var(--bad); }
tr.tone-trim .trigger-cell { color: var(--warn); }
tr.tone-init .trigger-cell { color: var(--ok); }
td.signal-strong { color: var(--ok); font-weight: 600; }
td.signal-medium { color: var(--warn); }
td.signal-weak { color: var(--muted); }
code { font-family: var(--font-mono); font-size: 12px; color: #b8b8b0; }
.cli-hint { font-family: var(--font-mono); font-size: 12px; padding: 10px 12px;
  background: #1f2125; border-radius: 4px; color: var(--ok); overflow-x: auto; margin: 6px 0 0; }
.panel-h3 { font-size: 14px; margin: 18px 0 8px; font-weight: 600; color: #f5f5f0;
  font-family: var(--font-mono); text-transform: uppercase; letter-spacing: 0.4px; }
/* Synthesis */
.synthesis-panel { border-left: 3px solid var(--ok); }
.synthesis-body { font-size: 14px; line-height: 1.65; }
.synthesis-body h2, .synthesis-body h3, .synthesis-body h4 { color: #f5f5f0; margin-top: 1.2em; margin-bottom: 6px; }
.synthesis-body h2 { font-size: 18px; }
.synthesis-body h3 { font-size: 15px; }
.synthesis-body h4 { font-size: 13px; color: var(--ok); }
.synthesis-body strong { color: #f5f5f0; }
.synthesis-body code { background: #1f2125; padding: 1px 5px; border-radius: 3px; }
.synthesis-body ul { padding-left: 22px; }
.synthesis-body li { margin-bottom: 4px; }
.synthesis-body hr { border: none; border-top: 1px solid var(--border); margin: 16px 0; }
/* Reread grid + cards */
.reread-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(380px, 1fr)); gap: 12px; margin-top: 8px; }
.reread-card { background: var(--panel); border: 1px solid var(--border); border-radius: 6px; padding: 12px 14px; }
.reread-card summary { cursor: pointer; list-style: none; display: flex; justify-content: space-between; align-items: baseline; font-size: 16px; font-weight: 600; }
.reread-card summary::-webkit-details-marker { display: none; }
.reread-card summary::before { content: '▸ '; color: var(--muted); font-family: var(--font-mono); }
.reread-card[open] summary::before { content: '▾ '; }
.reread-stamp { color: var(--muted); font-size: 11px; font-family: var(--font-mono); font-weight: 400; }
.reread-body { font-size: 13px; line-height: 1.55; margin-top: 10px; }
.reread-body h2, .reread-body h3, .reread-body h4 { color: #f5f5f0; margin: 10px 0 4px; }
.reread-body h2 { font-size: 14px; color: var(--ok); }
.reread-body h3 { font-size: 13px; }
.reread-body strong { color: #f5f5f0; }
.reread-body ul { padding-left: 18px; }
.reread-body hr { border: none; border-top: 1px solid var(--border); margin: 10px 0; }
/* KPI strip + decisions calibration */
.kpi-strip { display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 10px; margin: 8px 0 12px; }
.kpi-card { background: #1f2125; border: 1px solid var(--border); border-radius: 6px; padding: 10px 12px; text-align: center; }
.kpi-card.tone-good { border-left: 3px solid var(--ok); }
.kpi-card.tone-warn { border-left: 3px solid var(--warn); }
.kpi-card.tone-bad { border-left: 3px solid var(--bad); }
.kpi-card.tone-muted { border-left: 3px solid #555; }
.kpi-card.pos .kpi-value { color: var(--ok); }
.kpi-card.neg .kpi-value { color: var(--bad); }
.kpi-label { font-family: var(--font-mono); font-size: 11px; color: var(--muted); letter-spacing: 0.5px; }
.kpi-value { font-size: 22px; font-weight: 700; margin: 2px 0; color: #f5f5f0; }
.kpi-sub { font-size: 10px; color: #777; font-family: var(--font-mono); }
.calib-strip { display: flex; flex-direction: column; gap: 6px; margin: 8px 0 18px; }
.calib-row { display: grid; grid-template-columns: 80px 1fr 110px; gap: 12px; align-items: center; font-size: 12px; }
.calib-label { font-family: var(--font-mono); color: #aaa; text-transform: uppercase; }
.calib-bar { background: #1f2125; border-radius: 3px; height: 14px; overflow: hidden; }
.calib-fill { background: linear-gradient(90deg, #f87171 0%, #fbbf24 50%, #4ade80 100%); height: 100%; }
.calib-value { font-family: var(--font-mono); color: #ccc; text-align: right; }
.decisions-table td.outcome-correct { color: var(--ok); }
.decisions-table td.outcome-wrong { color: var(--bad); }
.decisions-table td.outcome-mixed { color: var(--warn); }
.decisions-table td.outcome-pending { color: var(--muted); }
/* LLM budget */
.budget-table td code { font-family: var(--font-mono); font-size: 12px; color: #f5f5f0; background: transparent; padding: 0; }
.burn-cell { width: 200px; padding: 6px 10px; }
.burn-bar { width: 100%; height: 8px; background: #1f2125; border-radius: 4px; overflow: hidden; }
.burn-fill { height: 100%; transition: width 0.2s; }
.burn-ok { background: var(--ok); }
.burn-warn { background: var(--warn); }
.burn-over { background: var(--bad); }
.budget-footer { margin-top: 12px; font-size: 13px; color: #ccc; }
.budget-footer strong { color: #f5f5f0; }
.budget-table input, .budget-table select { background: var(--panel-alt); color: var(--ink);
  border: 1px solid var(--border); border-radius: 4px; padding: 4px 6px; font-size: 12px; }
.budget-save { background: var(--accent); color: #0d1117; border: none; padding: 5px 12px;
  border-radius: 4px; font-weight: 600; font-size: 12px; cursor: pointer; }
/* Tier coverage strip */
.tier-strip { background: var(--panel); border: 1px solid var(--border); border-radius: 6px;
  padding: 10px 14px; margin-bottom: 22px; font-size: 13px; display: flex; align-items: center;
  flex-wrap: wrap; gap: 4px; }
.tier-strip-label { color: var(--muted); font-family: var(--font-mono); font-size: 11px;
  text-transform: uppercase; letter-spacing: 0.5px; margin-right: 8px; }
.tier-chip { font-family: var(--font-mono); font-size: 12px; padding: 2px 6px; border-radius: 3px; cursor: help; }
.tier-ok { color: var(--ok); }
.tier-stale { color: var(--warn); }
.tier-stale-count { color: var(--bad); font-weight: 600; }
.tier-empty { color: #666; }

/* ============================================================
   Overview status tables + action blocks (re-themed dark)
   ============================================================ */
.list-section { margin-bottom: 24px; }
.list-section h2 { font-size: 15px; text-transform: uppercase; letter-spacing: 0.5px; margin: 18px 0 10px; }
.list-section h2 .count { font-weight: 400; color: var(--muted); margin-left: 4px; }
.list-section table { border: 1px solid var(--border); border-radius: 8px; overflow: hidden; }
td.ticker { font-family: var(--font-mono); font-weight: 600; }
td.ticker a { color: var(--link); text-decoration: none; }
td.ticker a:hover { text-decoration: underline; }
.qa-yes, .qa-no { font-size: 10px; padding: 1px 5px; border-radius: 3px; letter-spacing: 0.3px; }
.qa-yes { background: #14361f; color: #6ee7a0; }
.qa-no { background: #3a1f1f; color: #f0a0a0; }
.comments-open { color: var(--warn); font-weight: 500; }
.breach-badge { display: inline-block; padding: 2px 8px; border-radius: 3px; font-size: 11px;
  color: white; text-transform: uppercase; letter-spacing: 0.4px; font-weight: 500; }
.open-link { color: var(--link); text-decoration: none; }
.open-link:hover { text-decoration: underline; }
.empty { color: var(--muted); font-style: italic; padding: 12px; }
""".strip()


# Tab switching + lazy panel loading + cc-picker + hash deep-linking. Vanilla JS,
# no build step. Plain string (not an f-string / .format template) so its braces
# are literal.
SHELL_JS = r"""
(function () {
  var TICKERS = null;  // cached /api/tickers payload
  var tabs = Array.prototype.slice.call(document.querySelectorAll('.cc-tab'));
  var panels = Array.prototype.slice.call(document.querySelectorAll('.cc-panel'));

  function panelById(id) {
    for (var i = 0; i < panels.length; i++) {
      if (panels[i].getAttribute('data-panel') === id) return panels[i];
    }
    return null;
  }

  // innerHTML does NOT execute <script> tags — re-create them so panel-local
  // wiring (e.g. the budget Save buttons) runs after a lazy fragment is injected.
  function injectHtml(container, html) {
    container.innerHTML = html;
    var scripts = container.querySelectorAll('script');
    for (var i = 0; i < scripts.length; i++) {
      var old = scripts[i];
      var s = document.createElement('script');
      if (old.src) s.src = old.src; else s.textContent = old.textContent;
      old.parentNode.replaceChild(s, old);
    }
  }

  function fetchTickers() {
    if (TICKERS) return Promise.resolve(TICKERS);
    return fetch('/api/tickers').then(function (r) { return r.json(); })
      .then(function (j) { TICKERS = (j && j.tickers) || []; return TICKERS; })
      .catch(function () { TICKERS = []; return TICKERS; });
  }

  function populatePicker(panel) {
    var sel = panel.querySelector('.cc-picker');
    if (!sel || sel.getAttribute('data-filled') === '1') return Promise.resolve();
    var required = panel.getAttribute('data-picker-required') === '1';
    return fetchTickers().then(function (list) {
      var opts = required
        ? '<option value="">— select a holding —</option>'
        : '<option value="">All holdings</option>';
      list.forEach(function (t) {
        var label = t.ticker + (t.name ? ' · ' + t.name : '');
        opts += '<option value="' + t.ticker + '">' + label + '</option>';
      });
      sel.innerHTML = opts;
      sel.setAttribute('data-filled', '1');
    });
  }

  function loadBody(panel, ticker) {
    var ep = panel.getAttribute('data-endpoint');
    if (!ep) return;
    var body = panel.querySelector('.cc-panel-body');
    var required = panel.getAttribute('data-picker-required') === '1';
    if (required && !ticker) {
      body.innerHTML = '<div class="cc-empty">Pick a holding from the dropdown above.</div>';
      panel.setAttribute('data-current-ticker', '');
      return;
    }
    var url = ep + (ticker ? ('?ticker=' + encodeURIComponent(ticker)) : '');
    body.innerHTML = '<div class="cc-loading">Loading…</div>';
    fetch(url).then(function (r) {
      if (!r.ok) throw new Error('HTTP ' + r.status);
      return r.text();
    }).then(function (html) {
      injectHtml(body, html);
      panel.setAttribute('data-current-ticker', ticker || '');
    }).catch(function (e) {
      body.innerHTML = '<div class="cc-empty">Failed to load (' + e.message + ').</div>';
    });
  }

  function activate(panelId, ticker) {
    var panel = panelById(panelId);
    if (!panel) { panel = panelById('overview'); panelId = 'overview'; ticker = null; }
    panels.forEach(function (p) { p.hidden = (p !== panel); });
    tabs.forEach(function (t) {
      t.classList.toggle('active', t.getAttribute('data-tab-target') === panelId);
    });
    var isPicker = panel.getAttribute('data-picker') === '1';
    var loaded = panel.getAttribute('data-loaded') === '1';
    if (isPicker) {
      populatePicker(panel).then(function () {
        var sel = panel.querySelector('.cc-picker');
        if (sel) sel.value = ticker || '';
        var cur = panel.getAttribute('data-current-ticker') || '';
        if (!loaded || cur !== (ticker || '')) {
          loadBody(panel, ticker || null);
          panel.setAttribute('data-loaded', '1');
        }
      });
    } else if (!loaded && panel.getAttribute('data-endpoint')) {
      loadBody(panel, null);
      panel.setAttribute('data-loaded', '1');
    }
  }

  function parseHash() {
    var h = (location.hash || '').replace(/^#/, '');
    if (!h) return { panel: 'overview', ticker: null };
    var eq = h.indexOf('=');
    if (eq === -1) return { panel: h, ticker: null };
    return { panel: h.substring(0, eq), ticker: decodeURIComponent(h.substring(eq + 1)) };
  }

  function onHashChange() {
    var p = parseHash();
    activate(p.panel, p.ticker);
  }

  tabs.forEach(function (t) {
    t.addEventListener('click', function () {
      location.hash = '#' + t.getAttribute('data-tab-target');
    });
  });

  panels.forEach(function (panel) {
    var sel = panel.querySelector('.cc-picker');
    if (!sel) return;
    sel.addEventListener('change', function () {
      var pid = panel.getAttribute('data-panel');
      var t = sel.value;
      location.hash = t ? ('#' + pid + '=' + encodeURIComponent(t)) : ('#' + pid);
    });
  });

  window.addEventListener('hashchange', onHashChange);
  onHashChange();  // initial activation from the current hash (or overview)
})();
""".strip()


_DOC_HEAD = """<!doctype html>
<html lang="en" data-theme="dark">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Portfolio · command center</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500;600&display=swap" rel="stylesheet">
<style>{css}</style>
</head>
<body>
""".replace("{css}", SHELL_CSS)

_DOC_FOOT = "</body></html>"
