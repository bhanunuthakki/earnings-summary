"""Unified tabbed command-center shell.

One page, served at ``GET /`` by ``execution/comments_server.py``, that folds the
former three vertically-scrolling pages (``/``, ``/analytical``, ``/ticker/<t>``)
into a single dark-themed app with a horizontal tab bar.

Design — thin shell + lazy panels:

* **Overview is server-inlined** (instant first paint): the Research cockpit
  (one attention-ranked row per holding — thesis health · valuation · events,
  master build P1.2) followed by the tier-coverage strip. The IR-KPI +
  maintenance action blocks moved to the Governance theme's Actions tab
  (``/api/panel/actions``) — Overview is for reading, not operating.
* **Every other tab lazy-loads its HTML on first activation** via
  ``GET /api/panel/<name>`` (the head/foot-less fragments from PR 5), then caches
  it in the DOM. Inline ``<script>`` blocks inside a fragment (e.g. the budget
  Save-button wiring) are re-executed after injection — ``innerHTML`` alone does
  not run them.
* **Three-theme IA (master build P1.1)**: a primary theme row — Research /
  Portfolio / Governance — with a sub-tab row per theme. Pre-reads, Insiders,
  Predictions, and the Decisions tab were removed from the nav; their legacy
  hashes remap (see ``_LEGACY_PANEL_REDIRECTS``).
* **The Holding drill-down is dropdown-driven** (``cc-picker``): re-fetches
  ``endpoint?ticker=<T>`` server-side on change.
* **Deep-linkable** via ``location.hash`` — ``#holdings``, ``#holding=NU`` —
  with ``hashchange`` driving back/forward; killed-panel hashes redirect.

The standalone ``/analytical`` and ``/ticker/<t>`` pages remain as working
deep-link targets (zero rewrite); this shell is purely additive.

All HTML is server-side f-strings + vanilla JS (no template engine), matching the
rest of the dashboard. ``SHELL_CSS`` / ``SHELL_JS`` are plain string constants so
their braces pass through untouched; only the small assembly bits interpolate.
"""

from __future__ import annotations

from datetime import UTC, datetime
from html import escape

from pipeline.research_cockpit import CockpitRow
from ui.time import stamp_html
from ui.tokens import FAVICON_LINK, palette_css

# Three-theme information architecture (master build P1.1): a primary theme
# row (Research / Portfolio / Governance) with a sub-tab row per theme.
# Sub-tab entries keep the original shape — (panel_id, label, endpoint,
# is_picker, picker_required) — so the lazy-panel renderer and the JS
# activation contract are unchanged. ``overview`` is inlined (no endpoint);
# ``picker_required`` panels (the per-ticker Holding drill-down) show a
# "pick a holding" prompt until one is chosen. The trigger-ladder sub-tab
# keeps panel id ``holdings`` (matching ``/api/panel/holdings``) but is
# labelled "Triggers" so it reads distinctly from the singular "Holding".
#
# Killed as nav surfaces per the directive: Pre-reads, Insiders, Predictions,
# and the Decisions tab (their legacy deep-links remap — see
# _LEGACY_PANEL_REDIRECTS; insider signals remain inside the per-ticker
# report's Exec-Comp section, and the decisions record folds into the
# Portfolio theme in P2.2).
_SubTab = tuple[str, str, str | None, bool, bool]
_THEMES: tuple[tuple[str, str, tuple[_SubTab, ...]], ...] = (
    (
        "research",
        "Research",
        (
            ("overview", "Overview", None, False, False),
            ("holding", "Holding", "/api/panel/holding", True, True),
            # On-the-fly slice-and-dice (P5.1): the ViewSpec builder —
            # metrics x tickers x period x transform over the fact tables,
            # with saved views.
            ("explore", "Explore", "/api/panel/explore", False, False),
            # New-name discovery queue (P5.4): screened/adjacency candidates
            # with why-surfaced evidence; approval gates every eval build.
            ("discovery", "Discovery", "/api/panel/discovery", False, False),
            # The analyst journal's lifecycle home (P4.5): list / filter /
            # resolve / reclassify / supersede over analyst_notes.
            ("journal", "Journal", "/api/panel/journal", False, False),
        ),
    ),
    (
        "portfolio",
        "Portfolio",
        (
            ("portfolio", "Performance", "/api/panel/portfolio", False, False),
            # P2.2 — the allocation-decisions record: sizing audit + the merged
            # decisions timeline (thesis ledger + sizing intents + decision
            # notes). The standalone Thesis Ledger tab folded into it.
            ("decisions_record", "Decisions", "/api/panel/decisions_record", False, False),
            # P2.3 — advisor memos: next-dollar + swap-discipline runs, the
            # deterministic swap screen, and the durable memo record.
            ("advisor_memos", "Memos", "/api/panel/advisor_memos", False, False),
            ("holdings", "Triggers", "/api/panel/holdings", False, False),
        ),
    ),
    (
        "governance",
        "Governance",
        (
            # Per-ticker section coverage (P4.2): which report sections are
            # filled for which names — the visible counterpart of the
            # hide-don't-stub policy (reports no longer show cold stubs).
            ("section_coverage", "Coverage", "/api/panel/section_coverage", False, False),
            ("ir_coverage", "IR Docs", "/api/panel/ir_coverage", False, False),
            ("source_calls", "Data Cache", "/api/panel/source_calls", False, False),
            # Whole-book data-quality state over validation_issues (P3.4) —
            # previously reachable only per-ticker inside workspace reports.
            ("validation", "Validation", "/api/panel/validation", False, False),
            # "was X, now Y" over the supersede chains (P3.5), linking both
            # filings into the /source/<doc_id> viewers.
            ("restatements", "Restatements", "/api/panel/restatements", False, False),
        ),
    ),
)

# Old tab deep-links keep working: killed panels 302 (client-side) onto their
# new homes. Mirrored verbatim into SHELL_JS's REDIRECTS map — keep in sync.
# budget/actions became settings-drawer sections in P3.4: their hashes land on
# Governance and the JS auto-opens the drawer (see DRAWER_OPENERS in SHELL_JS).
_LEGACY_PANEL_REDIRECTS: dict[str, str] = {
    "prereads": "overview",
    "insiders": "overview",
    "predictions": "overview",
    "decisions": "decisions_record",
    "thesis_ledger": "decisions_record",
    "budget": "ir_coverage",
    "actions": "ir_coverage",
}


def render_overview_panel(
    rows_by_list: dict[str, list[CockpitRow]],
    coverage: dict[str, dict[str, int]] | None,
) -> str:
    """The inlined Overview tab: the Research cockpit (the landing answer to
    "which holding needs my attention today?") with the tier-coverage strip
    below it. Reuses the existing public seams so there is no second code path
    for any of this content."""
    from pipeline.analytical_dashboard_html import render_tier_coverage_strip
    from pipeline.research_cockpit import render_research_cockpit

    return render_research_cockpit(rows_by_list) + render_tier_coverage_strip(coverage or {})


def render_shell(
    *,
    overview_html: str,
    generated_at: datetime | None = None,
    themes: tuple[tuple[str, str, tuple[_SubTab, ...]], ...] = _THEMES,
) -> str:
    """Render the full command-center document.

    ``overview_html`` is the pre-built Overview panel (see ``render_overview_panel``)
    inlined for first paint; every other sub-tab is an empty placeholder that
    lazy-loads from its ``/api/panel/<name>`` endpoint on first activation.
    """
    stamp = stamp_html(generated_at or datetime.now(UTC), css="cc-stamp", prefix="updated ")
    flat_tabs = tuple(t for _tid, _tlabel, subs in themes for t in subs)
    return "".join(
        [
            _DOC_HEAD,
            f'<div class="cc-topbar"><div class="cc-brand">Command Center</div>'
            f'<nav class="cc-links">'
            f'<a href="/digest">Morning digest</a>'
            f'<a href="/feed">Alert feed</a>'
            f"</nav>"
            f'<button class="cc-settings-btn" id="cc-settings-toggle" type="button" '
            f'title="Budgets · ticker settings · maintenance">⚙ Settings</button>'
            f"{stamp}</div>",
            _render_tab_bar(themes),
            '<main class="cc-panels">',
            _render_panels(flat_tabs, overview_html),
            "</main>",
            _SETTINGS_DRAWER_HTML,
            f"<script>{SHELL_JS}</script>",
            _DOC_FOOT,
        ]
    )


# The settings drawer (P3.4): admin stops being a tab. Each <details> section
# lazy-loads an existing panel fragment on first open through the same
# injectHtml used for tabs, so the budget Save buttons and the maintenance
# blocks' SSE wiring keep working unchanged inside the drawer.
_SETTINGS_DRAWER_HTML = (
    '<div class="cc-drawer-scrim" id="cc-drawer-scrim" hidden></div>'
    '<aside class="cc-drawer" id="cc-drawer" hidden aria-label="Settings">'
    '<div class="cc-drawer-head"><span>Settings &amp; maintenance</span>'
    '<button class="cc-drawer-close" id="cc-drawer-close" type="button" '
    'aria-label="Close">&times;</button></div>'
    '<div class="cc-drawer-body">'
    '<details class="cc-drawer-sec" open data-endpoint="/api/panel/budget" data-loaded="0">'
    "<summary>LLM budgets</summary>"
    '<div class="cc-drawer-sec-body"><div class="cc-loading">Loading…</div></div></details>'
    '<details class="cc-drawer-sec" data-endpoint="/api/panel/ticker_settings" data-loaded="0">'
    "<summary>Ticker settings</summary>"
    '<div class="cc-drawer-sec-body"><div class="cc-loading">Loading…</div></div></details>'
    '<details class="cc-drawer-sec" data-endpoint="/api/panel/actions" data-loaded="0">'
    "<summary>Maintenance actions &amp; job streams</summary>"
    '<div class="cc-drawer-sec-body"><div class="cc-loading">Loading…</div></div></details>'
    "</div></aside>"
)


def _render_tab_bar(themes: tuple[tuple[str, str, tuple[_SubTab, ...]], ...]) -> str:
    """Primary theme row + one sub-tab row per theme (hidden until its theme is
    active; the JS toggles visibility). Sub-tab buttons keep the exact
    ``cc-tab``/``data-tab-target`` contract the activation JS has always used."""
    out = ['<nav class="cc-tabs cc-theme-row" role="tablist">']
    for tid, tlabel, _subs in themes:
        out.append(
            f'<button class="cc-tab cc-theme-tab" type="button" role="tab" '
            f'data-theme-target="{escape(tid)}">{escape(tlabel)}</button>'
        )
    out.append("</nav>")
    for tid, _tlabel, subs in themes:
        out.append(f'<nav class="cc-tabs cc-subtabs" data-cc-theme="{escape(tid)}" hidden>')
        for pid, label, _endpoint, _picker, _required in subs:
            out.append(
                f'<button class="cc-tab" type="button" role="tab" '
                f'data-tab-target="{escape(pid)}" data-cc-theme="{escape(tid)}">'
                f"{escape(label)}</button>"
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


# Palette comes from the shared token source (src/ui/tokens.py); the alias
# block below maps this surface's legacy var names onto the canonical ones so
# its rules — and any later-inlined comment/chat CSS — resolve unchanged.
SHELL_CSS = (
    palette_css("dark")
    + """
:root {
  --panel: var(--surface);
  --panel-alt: var(--paper);
  --bg-card: var(--surface);
  --bg-elev: var(--paper);
  --row-hover: var(--paper);
  --ink: var(--fg);
  --ink-muted: var(--muted);
  --fg-muted: var(--muted);
  --link: var(--accent);
  --font-mono: var(--mono);
  --font-body: var(--sans);
}
* { box-sizing: border-box; }
body { margin: 0; padding: 0; font-family: var(--font-body); background: var(--bg);
  color: var(--ink); line-height: 1.5; font-size: 14px; }
a { color: var(--link); }

/* Top bar + tabs */
.cc-topbar { display: flex; justify-content: space-between; align-items: baseline;
  padding: 16px 24px 10px; }
.cc-brand { font-size: 18px; font-weight: 700; letter-spacing: 0.2px; }
.cc-links { display: flex; gap: 16px; margin-left: 24px; margin-right: auto; }
.cc-links a { color: var(--link); text-decoration: none; font-size: 13px; }
.cc-links a:hover { text-decoration: underline; }
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
.tier-backfill { color: var(--muted); }
.tier-backfill .tier-stale-count { color: var(--muted); font-weight: 400; }
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

/* ============================================================
   Holding drill-down tab (ticker_command_center sections + embedded report)
   ============================================================ */
.cc-holding-head { display: flex; justify-content: space-between; align-items: flex-start;
  flex-wrap: wrap; gap: 8px; margin-bottom: 18px; padding-bottom: 12px;
  border-bottom: 1px solid var(--border); }
.cc-holding-ticker { font-size: 22px; font-weight: 700; font-family: var(--font-mono); }
.cc-holding-name { font-size: 16px; color: var(--ink-muted); }
.cc-holding-links { font-size: 13px; display: flex; gap: 14px; align-items: center; }
.cc-holding-links a { color: var(--link); text-decoration: none; white-space: nowrap; }
.cc-holding-links a:hover { text-decoration: underline; }
.badges { display: inline-flex; gap: 4px; margin-left: 8px; }
.badge { display: inline-block; padding: 2px 8px; border-radius: 3px; font-size: 11px;
  text-transform: uppercase; letter-spacing: 0.4px; background: #2a2c30; }
.badge.b-ok { background: #14532d; color: var(--ok); }
.badge.b-warn { background: #422006; color: var(--warn); }
.badge.b-bad { background: #450a0a; color: var(--bad); }
.badge.b-muted { background: #2a2c30; color: var(--muted); }
.fresh-strip { display: flex; gap: 10px; margin-bottom: 22px; flex-wrap: wrap; }
.fresh-cell { background: var(--panel); border: 1px solid var(--border); border-radius: 6px;
  padding: 8px 14px; flex: 1; min-width: 140px; }
.fresh-label { font-size: 10px; text-transform: uppercase; letter-spacing: 0.5px; color: var(--muted); }
.fresh-val { font-size: 15px; font-variant-numeric: tabular-nums; }
.ok-dot { color: var(--ok); }
.tcc-refresh, .tcc-refresh + .tcc-refresh { background: var(--accent); color: #0d1117; border: none;
  padding: 6px 12px; border-radius: 4px; font-weight: 600; font-size: 12px; cursor: pointer; margin-right: 4px; }
.artifact-table code { background: transparent; padding: 0; }
.cc-report-embed { padding-bottom: 8px; }
.cc-report-frame { width: 100%; height: calc(100vh - 220px); min-height: 560px;
  border: 1px solid var(--border); border-radius: 8px; background: var(--bg); margin-top: 6px; }

/* Report split (master build P1.3): the embedded report with the analyst's
   open notes + recent alerts in a rail beside it. The rail scrolls
   independently, capped to the report frame's height; below 1100px the rail
   stacks under the report. */
.cc-holding-split { display: grid; grid-template-columns: minmax(0, 1fr) 360px;
  gap: 18px; align-items: start; }
.cc-holding-main { min-width: 0; }
.cc-holding-rail { display: flex; flex-direction: column; gap: 18px;
  max-height: calc(100vh - 150px); overflow-y: auto; position: sticky; top: 88px; }
.cc-rail-panel { margin-bottom: 0; padding: 14px 16px; }
.cc-rail-panel h2 { font-size: 15px; }
@media (max-width: 1100px) {
  .cc-holding-split { grid-template-columns: 1fr; }
  .cc-holding-rail { position: static; max-height: none; overflow-y: visible; }
}

/* Rail note cards — one per open analyst_notes row, color-keyed by kind. */
.rail-note { background: var(--paper); border: 1px solid var(--hairline);
  border-left: 3px solid var(--muted); border-radius: 6px; padding: 8px 10px;
  margin-bottom: 8px; }
.rail-note.nk-question { border-left-color: var(--accent); }
.rail-note.nk-decision { border-left-color: var(--ok); }
.rail-note.nk-watch { border-left-color: var(--warn); }
.rail-note.nk-assumption { border-left-color: var(--fg-soft); }
.rail-note.nk-observation { border-left-color: var(--muted); }
.rail-note-head { display: flex; justify-content: space-between; align-items: baseline; gap: 8px; }
.rail-note-kind { font-family: var(--font-mono); font-size: 10.5px; text-transform: uppercase;
  letter-spacing: 0.05em; color: var(--muted); }
.rail-note.nk-question .rail-note-kind { color: var(--accent); }
.rail-note.nk-decision .rail-note-kind { color: var(--ok); }
.rail-note.nk-watch .rail-note-kind { color: var(--warn); }
.rail-note-when { font-family: var(--font-mono); font-size: 10.5px; color: var(--muted); }
.rail-note-body { font-size: 12.5px; line-height: 1.5; margin: 4px 0; color: var(--fg-soft);
  overflow-wrap: anywhere; }
.rail-note-meta { font-size: 10.5px; color: var(--muted); overflow-wrap: anywhere; }
.rail-note-meta code { font-size: 10.5px; }

/* Alert cards + evidence drawer inside the rail — same class vocabulary the
   digest/feed render (src/dashboard/_card.py + evidence_drawer.py); rules
   lifted from src/dashboard/_styles.py and compacted for the 360px column.
   Palette vars are shared via ui.tokens, so only layout is duplicated. */
.alert-card { background: var(--surface); border: 1px solid var(--border); border-radius: 8px;
  padding: 10px 12px; margin-bottom: 10px; }
.alert-card-head { display: flex; flex-wrap: wrap; align-items: center; gap: 6px; margin-bottom: 6px; }
.ticker-badge { display: inline-flex; align-items: center; padding: 2px 7px;
  border: 1px solid var(--accent); background: var(--accent-soft); color: var(--accent);
  border-radius: 5px; font-family: var(--font-mono); font-weight: 600; font-size: 11px;
  letter-spacing: 0.05em; }
.trigger-badge, .status-badge { display: inline-flex; align-items: center; padding: 1px 6px;
  border: 1px solid var(--border-2); border-radius: 4px; color: var(--fg-soft);
  font-family: var(--font-mono); font-size: 10px; letter-spacing: 0.04em; text-transform: uppercase; }
.status-pending { color: var(--warn); border-color: var(--warn); }
.status-approved { color: var(--ok); border-color: var(--ok); }
.status-dismissed { color: var(--muted); border-color: var(--muted); }
.status-expired { color: var(--muted-2); border-color: var(--muted-2); }
.fired-at { color: var(--muted); font-family: var(--font-mono); font-size: 10.5px; margin-left: auto; }
.alert-memo { margin: 4px 0 8px; padding: 7px 9px; background: var(--paper);
  border-left: 3px solid var(--accent); border-radius: 0 6px 6px 0; font-size: 12.5px;
  color: var(--fg-soft); }
.alert-memo-pending { color: var(--muted); font-style: italic; }
.queued-actions { margin-top: 8px; }
.queued-actions h4 { font-size: 11px; font-weight: 600; margin: 0 0 5px;
  color: var(--muted); letter-spacing: 0.04em; text-transform: uppercase; }
.queued-action { display: flex; flex-wrap: wrap; gap: 8px; align-items: flex-start;
  padding: 6px 8px; background: var(--paper); border: 1px solid var(--hairline);
  border-radius: 6px; margin-bottom: 5px; }
.qa-kind { display: inline-flex; align-items: center; padding: 1px 5px;
  border: 1px solid var(--border-2); background: var(--surface); color: var(--fg-soft);
  border-radius: 3px; font-family: var(--font-mono); font-size: 10px; letter-spacing: 0.04em; }
.qa-body { flex: 1; min-width: 140px; color: var(--fg-soft); font-size: 12px; }
.qa-actions { display: flex; gap: 6px; align-items: center; font-family: var(--font-mono);
  font-size: 10.5px; }
.qa-actions a { display: inline-flex; padding: 2px 7px; border: 1px solid var(--border-2);
  border-radius: 4px; color: var(--accent); text-decoration: none; }
.qa-actions .qa-cli { color: var(--muted); overflow-wrap: anywhere; }
.qa-status-applied { color: var(--ok); }
.qa-status-cancelled { color: var(--muted); }
.evidence-drawer { margin-top: 8px; background: var(--paper); border: 1px solid var(--hairline);
  border-radius: 6px; }
.evidence-drawer > summary { cursor: pointer; padding: 6px 10px; color: var(--muted);
  font-family: var(--font-mono); font-size: 10.5px; letter-spacing: 0.04em;
  text-transform: uppercase; user-select: none; }
.evidence-drawer[open] > summary { border-bottom: 1px solid var(--hairline); }
.evidence-body { padding: 8px 10px; }
.evidence-section { margin-bottom: 8px; }
.evidence-section:last-child { margin-bottom: 0; }
.evidence-section-title { font-size: 10px; text-transform: uppercase; letter-spacing: 0.06em;
  color: var(--muted); margin-bottom: 3px; }
.evidence-summary-text { color: var(--fg-soft); font-size: 12.5px; }
.evidence-malformed { padding: 8px; background: rgba(240, 138, 138, 0.08);
  border: 1px solid var(--bad); border-radius: 4px; color: var(--bad); margin-bottom: 8px;
  font-size: 12px; }
.evidence-citations-table { width: 100%; border-collapse: collapse; font-size: 11.5px; }
.evidence-citations-table th { text-align: left; padding: 3px 6px 3px 0; color: var(--muted);
  font-weight: 500; font-size: 9.5px; text-transform: uppercase; letter-spacing: 0.05em;
  border-bottom: 1px solid var(--hairline); font-family: var(--font-body); }
.evidence-citations-table td { padding: 4px 6px 4px 0; vertical-align: top;
  border-bottom: 1px solid var(--hairline); }
.evidence-citations-table tr:last-child td { border-bottom: none; }
.cite-kind { color: var(--fg-soft); white-space: nowrap; }
.cite-locator { color: var(--muted); word-break: break-all; font-family: var(--font-mono);
  font-size: 11px; }
.cite-excerpt { color: var(--fg-soft); }
.cite-prov { color: var(--muted); font-family: var(--font-mono); font-size: 10.5px; }
.prov-source { color: var(--accent); }
.evidence-raw { margin-top: 4px; }
.evidence-raw > summary { cursor: pointer; color: var(--muted); font-family: var(--font-mono);
  font-size: 10.5px; letter-spacing: 0.04em; }
.evidence-raw-pre { margin: 5px 0 0; padding: 7px 9px; background: var(--bg);
  border: 1px solid var(--hairline); border-radius: 4px; font-family: var(--font-mono);
  font-size: 10.5px; color: var(--fg-soft); white-space: pre-wrap; word-break: break-all;
  max-height: 260px; overflow: auto; }

/* Three-theme nav (master build P1.1): primary theme row + per-theme sub-tab
   rows. Sub-tab rows reuse .cc-tab styling at a smaller size. */
.cc-theme-row { padding-top: 2px; }
.cc-theme-tab { font-size: 14.5px; font-weight: 600; letter-spacing: 0.01em; }
.cc-subtabs { z-index: 19; }
.cc-subtabs .cc-tab { font-size: 12.5px; }

/* Settings drawer (P3.4): admin-as-drawer instead of admin-as-tab. */
.cc-settings-btn { background: var(--panel-alt); color: var(--ink); border: 1px solid var(--border);
  border-radius: 6px; padding: 5px 12px; font-size: 12.5px; font-weight: 600; cursor: pointer;
  font-family: var(--font-body); margin-right: 14px; }
.cc-settings-btn:hover { border-color: var(--accent); color: var(--accent); }
.cc-drawer-scrim { position: fixed; inset: 0; background: rgba(0,0,0,0.45); z-index: 38; }
.cc-drawer { position: fixed; top: 0; right: 0; bottom: 0; width: min(620px, 92vw);
  background: var(--bg); border-left: 1px solid var(--border); z-index: 39;
  display: flex; flex-direction: column; box-shadow: -12px 0 32px rgba(0,0,0,0.35); }
.cc-drawer[hidden], .cc-drawer-scrim[hidden] { display: none; }
.cc-drawer-head { display: flex; justify-content: space-between; align-items: center;
  padding: 14px 18px; border-bottom: 1px solid var(--border); font-weight: 700; }
.cc-drawer-close { background: transparent; border: none; color: var(--muted);
  font-size: 20px; cursor: pointer; line-height: 1; padding: 2px 6px; }
.cc-drawer-close:hover { color: var(--ink); }
.cc-drawer-body { overflow-y: auto; padding: 14px 18px 40px; }
.cc-drawer-sec { margin-bottom: 14px; border: 1px solid var(--border); border-radius: 8px;
  background: var(--panel); }
.cc-drawer-sec > summary { cursor: pointer; list-style: none; padding: 11px 14px;
  font-size: 13.5px; font-weight: 600; }
.cc-drawer-sec > summary::-webkit-details-marker { display: none; }
.cc-drawer-sec > summary::before { content: '▸ '; color: var(--muted); font-family: var(--font-mono); }
.cc-drawer-sec[open] > summary::before { content: '▾ '; }
.cc-drawer-sec-body { padding: 0 14px 12px; }
.cc-drawer-sec-body .panel { margin-bottom: 0; border: none; padding: 0; background: transparent; }
"""
).strip()


# Tab switching + lazy panel loading + cc-picker + hash deep-linking. Vanilla JS,
# no build step. Plain string (not an f-string / .format template) so its braces
# are literal.
SHELL_JS = r"""
(function () {
  var TICKERS = null;  // cached /api/tickers payload
  // Sub-tab buttons only — theme buttons share .cc-tab styling but carry
  // data-theme-target instead of data-tab-target.
  var tabs = Array.prototype.slice.call(document.querySelectorAll('.cc-tab[data-tab-target]'));
  var themeTabs = Array.prototype.slice.call(document.querySelectorAll('.cc-theme-tab'));
  var subnavs = Array.prototype.slice.call(document.querySelectorAll('.cc-subtabs'));
  var panels = Array.prototype.slice.call(document.querySelectorAll('.cc-panel'));
  var lastPanelByTheme = {};  // remember each theme's last-active sub-tab

  // Killed nav surfaces (P1.1) — legacy hashes land on their new homes.
  // Kept in sync with _LEGACY_PANEL_REDIRECTS in the Python module.
  var REDIRECTS = {
    prereads: 'overview',
    insiders: 'overview',
    predictions: 'overview',
    decisions: 'decisions_record',
    thesis_ledger: 'decisions_record',
    budget: 'ir_coverage',
    actions: 'ir_coverage'
  };
  // Legacy panels that became settings-drawer sections (P3.4): their old
  // deep-links also auto-open the drawer after landing on Governance.
  var DRAWER_OPENERS = { budget: 1, actions: 1 };

  function firstPanelOfTheme(tid) {
    for (var i = 0; i < tabs.length; i++) {
      if (tabs[i].getAttribute('data-cc-theme') === tid) {
        return tabs[i].getAttribute('data-tab-target');
      }
    }
    return null;
  }

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
    var activeTheme = null;
    tabs.forEach(function (t) {
      var on = t.getAttribute('data-tab-target') === panelId;
      t.classList.toggle('active', on);
      if (on) activeTheme = t.getAttribute('data-cc-theme');
    });
    if (activeTheme) {
      themeTabs.forEach(function (t) {
        t.classList.toggle('active', t.getAttribute('data-theme-target') === activeTheme);
      });
      subnavs.forEach(function (n) {
        n.hidden = (n.getAttribute('data-cc-theme') !== activeTheme);
      });
      lastPanelByTheme[activeTheme] = panelId;
    }
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
    var wasDrawerPanel = !!DRAWER_OPENERS[p.panel];
    if (REDIRECTS[p.panel]) { p = { panel: REDIRECTS[p.panel], ticker: null }; }
    activate(p.panel, p.ticker);
    if (wasDrawerPanel) openDrawer();
  }

  // ----- Settings drawer (P3.4) -----
  var drawer = document.getElementById('cc-drawer');
  var drawerScrim = document.getElementById('cc-drawer-scrim');

  function loadDrawerSection(sec) {
    if (!sec || sec.getAttribute('data-loaded') === '1') return;
    var ep = sec.getAttribute('data-endpoint');
    var body = sec.querySelector('.cc-drawer-sec-body');
    if (!ep || !body) return;
    sec.setAttribute('data-loaded', '1');
    fetch(ep).then(function (r) {
      if (!r.ok) throw new Error('HTTP ' + r.status);
      return r.text();
    }).then(function (html) {
      injectHtml(body, html);
    }).catch(function (e) {
      sec.setAttribute('data-loaded', '0');
      body.innerHTML = '<div class="cc-empty">Failed to load (' + e.message + ').</div>';
    });
  }

  function openDrawer() {
    if (!drawer) return;
    drawer.hidden = false;
    if (drawerScrim) drawerScrim.hidden = false;
    var secs = drawer.querySelectorAll('.cc-drawer-sec[open]');
    for (var i = 0; i < secs.length; i++) loadDrawerSection(secs[i]);
  }

  function closeDrawer() {
    if (drawer) drawer.hidden = true;
    if (drawerScrim) drawerScrim.hidden = true;
  }

  var settingsBtn = document.getElementById('cc-settings-toggle');
  if (settingsBtn) settingsBtn.addEventListener('click', function () {
    if (drawer && drawer.hidden) openDrawer(); else closeDrawer();
  });
  var drawerClose = document.getElementById('cc-drawer-close');
  if (drawerClose) drawerClose.addEventListener('click', closeDrawer);
  if (drawerScrim) drawerScrim.addEventListener('click', closeDrawer);
  document.addEventListener('keydown', function (ev) {
    if (ev.key === 'Escape') closeDrawer();
  });
  if (drawer) {
    var allSecs = drawer.querySelectorAll('.cc-drawer-sec');
    for (var di = 0; di < allSecs.length; di++) {
      allSecs[di].addEventListener('toggle', function (ev) {
        if (ev.target.open) loadDrawerSection(ev.target);
      });
    }
  }

  tabs.forEach(function (t) {
    t.addEventListener('click', function () {
      location.hash = '#' + t.getAttribute('data-tab-target');
    });
  });

  themeTabs.forEach(function (t) {
    t.addEventListener('click', function () {
      var tid = t.getAttribute('data-theme-target');
      var target = lastPanelByTheme[tid] || firstPanelOfTheme(tid);
      if (target) location.hash = '#' + target;
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

  // A ticker link anywhere in the shell (analytical panels' .ticker-link, the
  // Overview status tables' td.ticker links) opens the per-ticker Holding tab
  // instead of navigating away. Delegated on document so it also catches links
  // inside lazily-injected panels. (Links inside the embedded report iframe live
  // in a separate document and are unaffected.)
  document.addEventListener('click', function (ev) {
    if (!ev.target.closest) return;
    var a = ev.target.closest('a.ticker-link, td.ticker a');
    if (!a) return;
    var t = (a.textContent || '').trim().toUpperCase();
    if (!t) return;
    ev.preventDefault();
    location.hash = '#holding=' + encodeURIComponent(t);
  });

  window.addEventListener('hashchange', onHashChange);
  onHashChange();  // initial activation from the current hash (or overview)
})();
""".strip()


_DOC_HEAD = (
    """<!doctype html>
<html lang="en" data-theme="dark">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Portfolio · command center</title>
"""
    + FAVICON_LINK
    + """
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500;600&display=swap" rel="stylesheet">
<style>{css}</style>
</head>
<body>
""".replace("{css}", SHELL_CSS)
)

_DOC_FOOT = "</body></html>"
