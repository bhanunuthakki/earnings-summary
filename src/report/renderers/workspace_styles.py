"""CSS for the workspace HTML renderer.

Adapted from the Anthropic design bundle's ``workspace.css`` (paper-on-mono
editorial palette, three themes, two densities). Carries small additions for
the three tabs that the design itself doesn't cover (Company / Position /
Sources), so the rest of the brief's content has somewhere to render without
falling back to the legacy HTML doc.

The report now consumes the same Work OS contracts as the dashboards: the
four visible type roles, persistent three-layer sidebar, shared control kit,
and spacing ladder. Hierarchy comes from weight, color, and placement rather
than a report-only ramp of intermediate sizes. Mono remains limited to
tickers, numbers, code, and locators; Source Serif remains an exported-
artifact voice.

Single string constant. The renderer inlines it inside a ``<style>`` tag so
the deliverable stays a single self-contained HTML file (matches the existing
``html.py`` renderer's contract).
"""

from __future__ import annotations

from ui.cite_marks import CITE_MARKS_CSS
from ui.controls import controls_css
from ui.tokens import palette_css

_BASE_CSS = (
    "\n/* ============================================================\n"
    "   Tokens — shared palette (single source: src/ui/tokens.py)\n"
    "   + the shared control kit (src/ui/controls.py)\n"
    "   + workspace-local semantic aliases resolved from the shared spacing ladder.\n"
    "   3 themes (paper · white · dark) · 2 densities.\n"
    "   ============================================================ */\n"
    + palette_css("paper")
    + controls_css("paper")
    + r"""
/* ============================================================
   Report spacing rhythm — ONE system, three tiers (owner audit,
   2026-08-02: "spacing is shit... too much space between vertical
   sections and too little between horizontal sometimes"). Every gap in
   this file should read off one of these three, not a freehand px value:
     --section-gap  between STRUCTURAL BANDS (identity / kpi-strip /
                    news-strip / tabs-wrap / peer-row / footer) — the
                    biggest gap, used sparingly, at the page-chrome level.
     --gap-lg       between PANELS inside one tab. `.tab-body`'s own flex
                    `gap` is the ONE place this is spent per tab — a
                    panel/section class must NOT also carry its own
                    margin-top/-bottom to a sibling panel, or the two
                    stack (a panel used to get gap-lg PLUS a local 14-18px
                    margin — the doubled "too much space" the audit named;
                    fixed by deleting the redundant local margins, not by
                    inventing a fourth token).
     --row-pad-y    inside ONE panel, between repeated ROW-shaped items
                    (a Q&A question, a decision card, an IR document, a
                    valuation row) — the tightest tier. Table cells use
                    the sibling --table-pad-y instead (a table row is
                    denser than a card row by design).
   All three collapse under [data-density="compact"] below, so honoring
   them (instead of a literal px) is also what makes the density toggle
   actually reach a given block. */
:root {
  --pad-x: var(--sp-5);
  --panel-pad-x: var(--sp-4);
  --panel-pad-y: var(--sp-3);
  --row-pad-y: var(--sp-3);
  --gap: var(--sp-4);
  --gap-lg: var(--sp-5);
  --section-gap: var(--sp-6);
  --kpi-pad: var(--sp-5);
  --table-pad-y: var(--sp-2);
}

:root[data-density="compact"] {
  --pad-x: var(--sp-4);
  --panel-pad-x: var(--sp-3);
  --panel-pad-y: var(--sp-2);
  --row-pad-y: var(--sp-2);
  --gap: var(--sp-3);
  --gap-lg: var(--sp-4);
  --section-gap: var(--sp-5);
  --kpi-pad: var(--sp-4);
  --table-pad-y: var(--sp-1);
}

* { box-sizing: border-box; }
html, body { margin: 0; padding: 0; background: var(--bg); color: var(--fg); height: 100%; }
body { font-family: var(--sans); font-size: var(--fs-body); line-height: 1.5; display: flex; flex-direction: row; align-items: stretch; overflow: hidden; }

.pos { color: var(--ok); }
.neg { color: var(--bad); }
.muted { color: var(--muted); }
.accent { color: var(--accent); }
.num { font-variant-numeric: tabular-nums; font-feature-settings: 'tnum'; }
.mono { font-family: var(--mono); }

/* ============================================================
   Primitives
   ============================================================ */
.badge {
  display: inline-flex; align-items: center; gap: var(--sp-2);
  padding: var(--sp-1) var(--sp-2);
  border: var(--bw-thin) solid var(--border-2);
  border-radius: var(--radius-full);
  font-size: var(--fs-caption); font-weight: 500;
  color: var(--fg-soft);
  background: var(--surface);
  letter-spacing: 0.01em;
  white-space: nowrap;
}
.badge .dot { width: var(--dot-size); height: var(--dot-size); border-radius: 50%; }
.badge .dot-ok { background: var(--ok); }
.badge .dot-warn { background: var(--warn); }
.badge .dot-bad { background: var(--bad); }
.badge .dot-muted { background: var(--muted); }
.forgone-strip { margin: var(--sp-2) 0; }
.comp-alignment-verdict { margin-top: var(--sp-2); }
.out-of-scope-flags { border-top: var(--bw-thin) solid var(--hairline); }
.out-of-scope-list { padding-inline-start: var(--sp-5); margin-top: var(--sp-2); }

/* The report's parallel .pill / .pill-ok/-neutral/-warn/-bad/-muted badge
   system was removed (2026-06-14): an outline mono micro chip that duplicated
   the kit. Every consumer migrated to .k-chip.k-chip-mono (+ k-chip-ok/-warn/
   -bad tones) from src/ui/controls.py. (.pill-warn-cell further below is a
   separate table-cell text color, not part of that system, and stays.) */

/* P3.3 per-number source chips: <details class="src-pop"> wrapping a tiny
   tier-colored <summary class="src-chip"> badge; the open panel is an
   absolutely-positioned popover with the document identity + source link. */
.src-pop { display: inline-block; position: relative; vertical-align: baseline; }
.src-pop > summary { list-style: none; cursor: pointer; }
.src-pop > summary::-webkit-details-marker { display: none; }
.src-chip {
  display: inline-block; font-size: var(--fs-caption); font-weight: 600;
  letter-spacing: 0.04em; line-height: 1.4; padding: 0 var(--sp-1);
  border: var(--bw-thin) solid var(--border-2); border-radius: var(--radius);
  color: var(--muted); background: transparent;
  opacity: 0.65; user-select: none;
}
.src-chip:hover, .src-pop[open] .src-chip { opacity: 1; }
.src-sec-official { color: var(--ok); border-color: var(--ok); }
.src-fmp-normalized { color: var(--accent); border-color: var(--accent); }
.src-llm-extracted { color: var(--warn); border-color: var(--warn); }
.src-yfinance-fallback, .src-s1-provisional { color: var(--muted); }
/* Scored confidence below LOW_CONFIDENCE_THRESHOLD: the subtle cell
   affordance — warn-tinted dashed border, overriding the tier color. */
.src-chip.src-lowconf { color: var(--warn); border-color: var(--warn); border-style: dashed; }
.src-pop-body {
  position: absolute; z-index: 40; top: calc(100% + var(--sp-1)); left: 0;
  min-width: 220px; max-width: 340px; padding: var(--sp-2) var(--sp-2);
  background: var(--surface); border: var(--bw-thin) solid var(--border-2);
  border-radius: var(--radius); box-shadow: var(--shadow-pop);
  font-size: var(--fs-caption); text-align: left; white-space: normal;
}
.src-pop-row { padding: var(--bw-thin) 0; color: var(--fg); }
.src-pop-row.mono { font-family: var(--mono); font-size: var(--fs-caption); color: var(--muted); }
.src-pop-locator { word-break: break-all; }
.src-pop-row a { color: var(--accent); }
/* S2 PR3: unresolved validation issues + derived-from input rows. */
.src-pop-warn { color: var(--warn); }
.src-pop-input { display: flex; align-items: center; gap: var(--sp-1); }
.src-pop-input .src-chip { opacity: 1; }
.src-pop-input a.src-chip { text-decoration: none; }

.ic-btn {
  appearance: none;
  border: var(--bw-thin) solid var(--border);
  background: var(--surface);
  width: 30px; height: 30px;
  border-radius: var(--radius);
  display: inline-flex; align-items: center; justify-content: center;
  cursor: pointer;
  color: var(--fg-soft);
  font-size: var(--fs-body);
}
.ic-btn:hover { color: var(--fg); border-color: var(--border-2); }

/* P4.1 empty-state anatomy: empty sections collapse to a single muted
   summary line (<details class="panel panel-empty">) that expands to an
   analyst-language explanation. Replaces the old full-height .stub blocks. */
.panel-empty .panel-title { color: var(--muted); font-weight: 500; }
.panel-empty-body {
  padding: var(--sp-3) var(--panel-pad-x);
  font-size: var(--fs-body); color: var(--muted); line-height: 1.55;
  background: var(--paper);
}
.panel-budget { border-left: 3px solid var(--warn); }

/* ============================================================
   Workspace shell
   ============================================================ */
.report-sidebar {
  flex: none; height: 100vh; height: 100dvh;
  display: flex; flex-direction: column; overflow: hidden;
  padding: var(--sp-3); gap: var(--sp-3);
}
.report-sidebar-brand {
  display: flex; flex-direction: column; gap: var(--sp-1);
  padding: var(--sp-2); border-bottom: var(--bw-thin) solid var(--border);
}
.report-sidebar-ticker {
  font-family: var(--mono); font-size: var(--fs-title); font-weight: 600;
  color: var(--fg);
}
.report-sidebar-product { font-size: var(--fs-caption); color: var(--muted); }
.report-nav-scroll { min-height: 0; overflow-y: auto; }
.report-nav-layer { display: flex; flex-direction: column; gap: var(--sp-1); margin-bottom: var(--sp-3); }
.report-nav-label {
  padding: 0 var(--sp-2); font-size: var(--fs-caption); font-weight: 600;
  color: var(--muted); letter-spacing: 0.06em; text-transform: uppercase;
}
.report-sidebar .tab-count { margin-left: auto; }
/* BHA-71: sidebar collapse toggle button */
.report-sidebar-toggle {
  flex: none; display: flex; align-items: center; justify-content: center;
  width: var(--icon-button-size); height: var(--icon-button-size); cursor: pointer;
  margin-top: auto; align-self: flex-start;
  color: var(--muted); transition: color var(--transition), background var(--transition);
}
.report-sidebar-toggle:hover { color: var(--fg); background: var(--surface); }
.report-sidebar-toggle svg { transition: transform var(--transition-fluid); }
/* Collapsed state: rail shrinks to icon-only width */
.report-sidebar.sidebar-collapsed {
  width: var(--sidebar-collapsed-width);
  padding: var(--sp-2);
  transition: width var(--transition-fluid), padding var(--transition-fluid);
}
.report-sidebar { transition: width var(--transition-fluid), padding var(--transition-fluid); }
.report-sidebar.sidebar-collapsed .report-sidebar-product,
.report-sidebar.sidebar-collapsed .report-nav-label,
.report-sidebar.sidebar-collapsed .tab-label,
.report-sidebar.sidebar-collapsed .tab-count { display: none; }
.report-sidebar.sidebar-collapsed .report-sidebar-brand { align-items: center; padding-inline: 0; }
.report-sidebar.sidebar-collapsed .k-nav-item { justify-content: center; padding-inline: 0; }
.report-sidebar.sidebar-collapsed .report-sidebar-toggle svg { transform: rotate(180deg); }
/* Keyboard focus ring on sidebar nav items */
.report-sidebar .tab:focus-visible {
  outline: var(--bw-thick) solid var(--accent); outline-offset: var(--bw-thin);
}
/* Touch-friendly hit targets on coarse-pointer devices */
@media (pointer: coarse) {
  .report-sidebar .k-nav-item { min-height: var(--touch-target-size); }
}


.l1-root {
  flex: 1;
  min-width: 0;
  height: 100vh;
  height: 100dvh;
  overflow-y: auto; overflow-x: hidden;
  background: var(--bg);
  color: var(--fg);
  font-family: var(--sans);
  display: flex; flex-direction: column;
  padding-bottom: var(--sp-6);
}

.l1-chrome {
  display: flex; align-items: center; justify-content: space-between;
  padding: var(--sp-3) var(--pad-x);
  border-bottom: var(--bw-thin) solid var(--border);
  background: var(--surface);
}
.l1-chrome-left { display: flex; align-items: center; gap: var(--sp-6); }
.logo {
  font-family: var(--mono); font-weight: 600; font-size: var(--fs-body);
  letter-spacing: -0.01em; color: var(--fg);
}
.logo-sm { color: var(--muted); margin: 0 var(--sp-half); }
.crumb {
  font-size: var(--fs-body); display: flex; align-items: center; gap: var(--sp-2);
  color: var(--fg);
}
.crumb-sep { color: var(--muted); }
.crumb-current { font-weight: 500; color: var(--fg); }
.l1-chrome-right { display: flex; align-items: center; gap: var(--sp-2); }

.l1-identity {
  display: flex; align-items: flex-end; justify-content: space-between;
  padding: var(--sp-6) var(--pad-x) var(--sp-5);
  border-bottom: var(--bw-thin) solid var(--border);
  gap: var(--sp-6);
  flex-wrap: wrap;
}
.ticker-large {
  font-family: var(--mono); font-weight: 600; font-size: var(--fs-display);
  letter-spacing: -0.045em; line-height: 0.95;
  color: var(--fg);
}
.company-row {
  display: flex; align-items: center; gap: var(--sp-3); margin-top: var(--sp-3);
}
.company-name { font-size: var(--fs-display); font-weight: 500; }
.company-meta {
  font-size: var(--fs-body); color: var(--muted); margin-top: var(--sp-2);
  display: flex; align-items: center; gap: var(--sp-2); flex-wrap: wrap;
}
.meta-pip { color: var(--muted); }

.identity-right {
  display: flex; align-items: stretch; gap: var(--sp-5);
  flex-wrap: nowrap;
}
.val-stat { display: flex; flex-direction: column; gap: var(--sp-2); min-width: 92px; }
.val-stat-label {
  font-size: var(--fs-caption); color: var(--muted);
  text-transform: uppercase; letter-spacing: 0.08em;
}
.val-stat-value {
  font-family: var(--mono); font-size: var(--fs-display); font-weight: 500;
  font-variant-numeric: tabular-nums; letter-spacing: -0.015em;
  color: var(--fg);
}
.val-stat-value.mono-sm { font-size: var(--fs-display); }
.val-divider { width: var(--bw-thin); background: var(--border); align-self: stretch; }

.l1-thesis {
  display: flex; gap: var(--sp-5);
  padding: var(--sp-4) var(--pad-x);
  border-bottom: var(--bw-thin) solid var(--border);
  background: var(--paper);
}
.thesis-label {
  font-size: var(--fs-caption); font-weight: 600;
  letter-spacing: 0.1em; text-transform: uppercase;
  color: var(--muted); flex-shrink: 0; padding-top: var(--sp-1);
}
.l1-thesis p {
  margin: 0; font-size: var(--fs-body); line-height: 1.65;
  max-width: 1180px; color: var(--fg-soft);
}

.kpi-strip {
  display: grid; grid-template-columns: repeat(4, 1fr);
  gap: var(--bw-thin);
  background: var(--border);
  border-bottom: var(--bw-thin) solid var(--border);
}
.kpi-tile {
  background: var(--surface);
  padding: var(--kpi-pad) calc(var(--kpi-pad) + var(--sp-half));
}
.kpi-name {
  font-size: var(--fs-caption); color: var(--muted); margin-bottom: var(--sp-2);
  min-height: var(--sp-6); line-height: 1.35;
}
.kpi-row { display: flex; align-items: baseline; gap: var(--sp-2); }
.kpi-value {
  font-family: var(--mono); font-size: var(--fs-display); font-weight: 500;
  font-variant-numeric: tabular-nums; letter-spacing: -0.015em;
}
.kpi-delta {
  font-family: var(--mono); font-size: var(--fs-caption); font-weight: 500;
}
.kpi-delta.pos { color: var(--accent); }
.kpi-delta.neg { color: var(--muted); }
.kpi-spark { margin-top: var(--sp-2); color: var(--accent); }
.ws-spark { overflow: visible; display: block; }
.ws-spark-kpi { inline-size: 230px; block-size: 36px; }
.ws-spark-compact { inline-size: 120px; block-size: 24px; }
.ws-spark-micro { inline-size: 84px; block-size: 22px; }
.ws-spark-valuation { inline-size: 560px; block-size: 60px; }
.ws-spark-area { fill: currentColor; opacity: 0.18; }
.ws-spark-line { fill: none; stroke: currentColor; stroke-width: 1.25; stroke-linejoin: round; stroke-linecap: round; }
.ws-spark-dot { fill: currentColor; }
.ws-verdict-bar { display: flex; gap: var(--sp-half); align-items: center; }
.ws-verdict-cell { inline-size: var(--sp-3); block-size: var(--sp-5); border-radius: var(--radius-sm); opacity: 0.55; background: var(--border); }
.ws-verdict-exceeded { background: var(--accent); opacity: 1; }
.ws-verdict-met { background: var(--fg); }
.ws-verdict-mixed { background: var(--warn); }
.ws-verdict-missed { background: var(--muted); }
.kpi-axis {
  display: flex; justify-content: space-between; align-items: baseline;
  font-family: var(--mono); font-size: var(--fs-caption); color: var(--muted);
  margin-top: var(--sp-2);
}
.kpi-trail { color: var(--muted); }

.news-strip {
  padding: var(--sp-5) var(--pad-x) var(--sp-5);
  border-bottom: var(--bw-thin) solid var(--border);
  background: var(--bg);
}
.news-h { display: flex; align-items: baseline; gap: var(--sp-3); margin-bottom: var(--sp-3); }
.news-eyebrow {
  font-size: var(--fs-caption); font-weight: 600;
  letter-spacing: 0.1em; text-transform: uppercase; color: var(--muted);
}
.news-sub { font-size: var(--fs-caption); color: var(--muted); }
.news-grid {
  display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
  gap: var(--sp-3);
}
.news-item {
  border: var(--bw-thin) solid var(--border);
  background: var(--surface);
  padding: var(--sp-3);
  border-radius: var(--radius);
  position: relative;
}
.news-item::before {
  content: ''; position: absolute; left: 0; top: 0; bottom: 0;
  width: 3px; border-radius: var(--radius) 0 0 var(--radius-sm);
}
.news-item.tone-pos::before { background: var(--accent); }
.news-item.tone-opt::before { background: var(--warn); }
.news-item.tone-neg::before { background: var(--bad); }
.news-item.tone-neu::before { background: var(--muted); }
.news-meta {
  display: flex; align-items: center; gap: var(--sp-2); flex-wrap: wrap;
  font-family: var(--mono); font-size: var(--fs-caption);
  margin-bottom: var(--sp-2);
}
.news-tag {
  font-weight: 600; letter-spacing: 0.06em;
  color: var(--accent);
}
.news-item.tone-opt .news-tag { color: var(--warn); }
.news-item.tone-neg .news-tag { color: var(--bad); }
.news-item.tone-neu .news-tag { color: var(--muted); }
.news-date { color: var(--muted); }
.news-src { color: var(--muted); font-family: var(--sans); font-size: var(--fs-caption); }
.news-headline {
  font-family: var(--sans); font-size: var(--fs-body);
  font-weight: 500; line-height: 1.4; margin: 0 0 var(--sp-2);
  color: var(--fg);
}
.news-headline a { color: inherit; text-decoration: none; }
.news-headline a:hover { text-decoration: underline; }
.news-gloss {
  font-family: var(--sans); font-size: var(--fs-body);
  line-height: 1.5; color: var(--fg-soft); margin: 0;
}

[data-show-news="0"] .news-strip { display: none; }

/* ============================================================
   Tabs
   ============================================================ */
.l1-tabs-wrap { padding: var(--section-gap) var(--pad-x) 0; }
.tabs {
  display: flex;
}
.tab {
  flex: none;
}
.tab:hover { color: var(--fg); }
.tab.active { color: var(--fg); }
.tab-count {
  font-family: var(--mono); font-size: var(--fs-caption); font-weight: 500;
  padding: var(--sp-half) var(--sp-1); background: var(--paper);
  border-radius: var(--radius-full); color: var(--muted);
}
.tab.active .tab-count { background: var(--accent-soft); color: var(--accent); }
.tabs-meta { font-size: var(--fs-caption); padding-right: var(--sp-1); color: var(--muted); }
.tab-pane { padding-top: var(--section-gap); display: none; }
.tab-pane.active { display: block; }
.tab-body { display: flex; flex-direction: column; gap: var(--gap-lg); }

/* UX9 grouped tabs: the top bar swaps .tab-group-pane wrappers; inside a
   multi-section group a slim pill row swaps the section panes. Section
   panes stay .tab-pane (display + print + comment-anchor scoping reuse the
   rules above). */
.tab-group-pane { display: none; }
.tab-group-pane.active { display: block; }
.subtabs {
  display: flex; align-items: center; gap: var(--sp-2); flex-wrap: wrap;
  padding-top: var(--section-gap);
}
.subtab {
  appearance: none; border: var(--bw-thin) solid var(--border-2);
  background: var(--surface);
  padding: var(--sp-1) var(--sp-3) var(--sp-2);
  font-family: var(--sans); font-size: var(--fs-caption); font-weight: 500;
  color: var(--muted); cursor: pointer; border-radius: var(--radius-full);
  display: flex; align-items: center; gap: var(--sp-2);
  letter-spacing: 0.005em;
}
.subtab:hover { color: var(--fg); border-color: var(--fg); }
.subtab.active { color: var(--bg); background: var(--fg); border-color: var(--fg); }
.subtab-count {
  font-family: var(--mono); font-size: var(--fs-caption); font-weight: 500;
  padding: 0 var(--sp-1); border-radius: var(--radius-full);
  background: var(--paper); color: var(--muted);
}
.subtab.active .subtab-count { background: var(--bg); color: var(--fg); }

.eyebrow {
  font-size: var(--fs-caption); font-weight: 500;
  letter-spacing: 0.1em; text-transform: uppercase;
  color: var(--muted); margin-bottom: var(--sp-2);
}
.section-title {
  font-family: var(--sans);
  font-size: var(--fs-display); font-weight: 500; letter-spacing: -0.025em;
  margin: 0 0 var(--sp-3); line-height: 1.2;
  max-width: 820px;
  color: var(--fg);
}
.lede {
  font-size: var(--fs-title); color: var(--fg-soft); margin: 0;
  line-height: 1.6;
  /* No max-width — the thesis lede should fill the tab's content area so
     dense, structured theses don't wrap at awkwardly narrow column widths.
     Originally capped at 760px for legibility on wide monitors; that turned
     out to be too narrow for theses that name multiple break rules + KPI
     tiers inline. The parent panel still provides outer padding. */
}
.row-split {
  display: flex; justify-content: space-between; align-items: flex-start;
  gap: var(--sp-5); flex-wrap: wrap;
}

/* Hero quote (Earnings tab) */
.hero-quote {
  border: var(--bw-thin) solid var(--border-2);
  background: linear-gradient(180deg, var(--paper), var(--surface));
  padding: var(--sp-5) var(--sp-6) var(--sp-5);
  border-radius: var(--radius);
  display: flex; gap: var(--sp-5);
  position: relative;
}
.hero-quote-mark {
  font-family: var(--sans); font-size: var(--fs-display); font-weight: 600;
  line-height: 0.7; color: var(--accent);
  flex-shrink: 0;
}
.hero-quote blockquote { margin: 0; flex: 1; }
.hero-quote blockquote p {
  font-family: var(--sans); font-size: var(--fs-display); line-height: 1.4;
  font-weight: 400; margin: 0 0 var(--sp-3);
  letter-spacing: -0.005em;
  color: var(--fg);
}
.hero-quote footer {
  font-size: var(--fs-body); color: var(--muted);
  display: flex; gap: var(--sp-2);
}
.hero-speaker { color: var(--fg); font-weight: 500; }
.hero-role { color: var(--muted); }

.quarter-select { display: flex; flex-direction: column; gap: var(--sp-2); flex-shrink: 0; padding-top: var(--sp-1); }
.quarter-select-label {
  font-size: var(--fs-caption); font-weight: 500;
  letter-spacing: 0.08em; text-transform: uppercase;
  color: var(--muted); text-align: right;
}
.quarter-select-btns { display: flex; gap: var(--sp-1); flex-wrap: wrap; justify-content: flex-end; }
.qbtn {
  appearance: none; border: var(--bw-thin) solid var(--border-2);
  background: var(--surface);
  padding: var(--sp-2) var(--sp-3);
  font-family: var(--mono); font-size: var(--fs-caption); font-weight: 500;
  color: var(--muted); cursor: pointer; border-radius: var(--radius);
}
.qbtn:hover { color: var(--fg); border-color: var(--fg); }
.qbtn.active { color: var(--bg); background: var(--fg); border-color: var(--fg); }

/* ============================================================
   Panels
   ============================================================ */
.panel {
  border: var(--bw-thin) solid var(--border);
  border-radius: var(--radius);
  background: var(--surface);
  overflow: hidden;
}
.panel-head {
  display: flex; align-items: baseline; justify-content: space-between;
  padding: var(--panel-pad-y) var(--panel-pad-x);
  border-bottom: var(--bw-thin) solid var(--hairline);
  gap: var(--sp-3);
}
.panel-title {
  font-size: var(--fs-body); font-weight: 600; letter-spacing: 0.005em;
  color: var(--fg);
}
.panel-sub { font-size: var(--fs-caption); color: var(--muted); }

/* ---- §6.3 document form. Inside a .k-doc the report's panels stop being
   boxes and become sections: hairline rules and whitespace instead of cards,
   because a research page that grows boxes reads as a dashboard, and that is
   where a research surface loses the seriousness it needs to be trusted.

   The MARKUP is untouched. Every _panel_head() call site, every cross-link
   target, comment anchor and panel_id keeps working — only presentation
   changes. That is why this is a scoped stylesheet block and not a rewrite of
   twenty renderers: the alternative was hand-editing every call site, which
   would have churned the goldens structurally for a purely visual change.

   --panel-pad-x is the one lever. Heads, .val-rows, .tbl cells and the strip
   lists all take their horizontal padding from it, so zeroing it here flushes
   the whole section to the document's left edge in one declaration. ---- */
.k-doc .panel {
--panel-pad-x: var(--indent-0);
  border: 0; border-radius: 0; background: none; overflow: visible;
  border-top: var(--bw-thin) solid var(--border);
  padding-top: var(--sp-2);
}
.k-doc .panel:first-child { border-top: 0; padding-top: 0; }
.k-doc .panel-head { border-bottom: 0; padding-bottom: var(--sp-2); }
/* The section label: the kit's caption shape in the editorial mark (§2). */
.k-doc .panel-title {
  font-size: var(--fs-caption); font-weight: 600; color: var(--mark);
  text-transform: uppercase; letter-spacing: 0.16em;
}
/* A document's own prose keeps the reading measure even though the report
   itself is fluid — .k-doc-fluid drops the outer clamp, not this one. */
.k-doc .lede { max-width: var(--reading-measure); }

/* P4.1 canonical header anatomy: title (left) · as-of · source chip · sub
   (right edge, grouped in .panel-meta). Built by workspace_html._panel_head —
   hand-rolled heads should not exist outside that helper. */
.panel-meta {
  display: flex; align-items: baseline; gap: var(--sp-2);
  min-width: 0; text-align: right;
}
.panel-asof {
  font-family: var(--mono); font-size: var(--fs-caption); color: var(--muted);
  white-space: nowrap;
}
/* P4.1 canonical collapse idiom: <details class="panel"> with
   <summary class="panel-head">. The ▸/▾ affordance rides on .panel-title.
   (The financials line-item drill-down stays a JS row toggle — <tr> can't
   nest inside <details>.) */
details.panel > summary { cursor: pointer; user-select: none; list-style: none; }
details.panel > summary::-webkit-details-marker { display: none; }
details.panel > summary .panel-title::before {
  content: '\25B8 ';
  font-family: var(--mono); font-size: var(--fs-caption); color: var(--muted);
}
details.panel[open] > summary .panel-title::before { content: '\25BE '; }
details.panel:not([open]) > summary.panel-head { border-bottom: 0; }
details.panel > summary:hover { background: var(--paper); }
/* P4.3 cross-tab links: small accent links in the panel-meta that jump to a
   related panel on another tab (data-xtab handler in workspace_script.JS).
   The landing panel flashes briefly so the eye finds it after the switch. */
.panel-xlink {
  font-size: var(--fs-caption); color: var(--accent); text-decoration: none;
  white-space: nowrap;
}
.panel-xlink:hover { text-decoration: underline; }
.xlink-flash { outline: var(--bw-thick) solid var(--accent); outline-offset: var(--bw-thick); }

/* P4.4 — the open watch-items strip under the thesis lede: every build
   leads with what the owner already said to watch for. */
.l1-open-items { margin: 0 var(--pad-x) var(--sp-2); }
.oi-strip-list { list-style: none; margin: 0; padding: var(--sp-2) var(--panel-pad-x) var(--sp-2); }
.oi-strip-list li {
  display: flex; gap: var(--sp-2); align-items: baseline;
  padding: var(--sp-1) 0; font-size: var(--fs-body);
  border-bottom: var(--bw-thin) solid var(--hairline);
}
.oi-strip-list li:last-child { border-bottom: 0; }
.oi-kind {
  font-size: var(--fs-caption); font-weight: 600;
  text-transform: uppercase; letter-spacing: 0.05em;
  color: var(--accent); border: var(--bw-thin) solid var(--accent);
  border-radius: var(--radius-full); padding: 0 var(--sp-1); flex: none;
}
.oi-body { flex: 1; color: var(--fg-soft); }
.oi-when { color: var(--muted); font-family: var(--mono); font-size: var(--fs-caption); flex: none; }
.panel-foot {
  padding: var(--sp-3) var(--panel-pad-x);
  background: var(--paper);
  border-top: var(--bw-thin) solid var(--hairline);
  font-size: var(--fs-caption); color: var(--fg-soft);
  display: flex; gap: var(--sp-2); align-items: flex-start; line-height: 1.5;
}
.flag { color: var(--warn); font-size: var(--fs-body); }
.signals-fires {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(260px, 1fr));
  gap: var(--sp-2);
  padding: var(--sp-3) var(--panel-pad-x);
}
/* §3.5 signal cards — severity via token-backed classes (P6.1; these
   previously carried hardcoded inline rgba colors + a literal font stack). */
.signal-card {
  border: var(--bw-thin) solid var(--hairline);
  border-left: 3px solid var(--hairline);
  border-radius: var(--radius); padding: var(--sp-2) var(--sp-3); font-size: var(--fs-body);
}
.signal-card.sig-red { border-left-color: var(--bad); background: color-mix(in srgb, var(--bad) 16%, transparent); }
.signal-card.sig-yellow { border-left-color: var(--warn); background: color-mix(in srgb, var(--warn) 16%, transparent); }
.signal-card.sig-green { border-left-color: var(--ok); background: color-mix(in srgb, var(--ok) 16%, transparent); }
.signal-card-head {
  display: flex; justify-content: space-between;
  gap: var(--sp-2); align-items: baseline; margin-bottom: var(--sp-1);
}
.signal-card-metric { font-weight: 600; }
.signal-card-type {
  font-size: var(--fs-caption); text-transform: uppercase;
  letter-spacing: 0.4px; color: var(--muted);
}
.signal-card-narrative { line-height: 1.45; margin: var(--sp-1) 0 var(--sp-2); }
.signal-card-stat { font-family: var(--mono); font-size: var(--fs-caption); color: var(--muted); }
.signal-sev { font-weight: 600; color: var(--muted); }
.signal-sev.sig-red { color: var(--bad); }
.signal-sev.sig-yellow { color: var(--warn); }
.signal-sev.sig-green { color: var(--ok); }

.signals-all { padding: 0 var(--panel-pad-x) var(--sp-3); }
.signals-all > summary {
  cursor: pointer; font-size: var(--fs-body); color: var(--muted);
  padding: var(--sp-2) 0; list-style: none;
}
.signals-all > summary::-webkit-details-marker { display: none; }
.signals-all > summary::before { content: '▸ '; }
.signals-all[open] > summary::before { content: '▾ '; }
.prose-pad {
  padding: var(--sp-4) var(--panel-pad-x) var(--sp-4);
  font-size: var(--fs-body); line-height: 1.6; color: var(--fg-soft);
}
.prose-pad p { margin: 0 0 var(--sp-2); }
.prose-pad p:last-child { margin-bottom: 0; }
.prose-pad ul, .prose-pad ol { margin: 0 0 var(--sp-2); padding-left: var(--sp-5); }
.prose-pad li { margin-bottom: var(--sp-1); }

.grid-2col { display: grid; grid-template-columns: 1.05fr 1fr; gap: var(--gap); }
.grid-fin { display: grid; grid-template-columns: 1.1fr 1fr; gap: var(--gap); }
.grid-thesis-top { display: grid; grid-template-columns: 0.65fr 1fr; gap: var(--gap); }
.grid-thesis-bottom { display: grid; grid-template-columns: 0.55fr 1fr; gap: var(--gap); }
@media (max-width: 1080px) {
  .grid-2col, .grid-fin, .grid-thesis-top, .grid-thesis-bottom {
    grid-template-columns: 1fr;
  }
}

/* P4.1 canonical table — the ONE data-table class. Variants are modifier
   classes alongside it (.tbl-nowrap for dense numeric grids; semantic
   modifiers like .coverage-table / .insider-table / .kpi-ledger-table keep
   only their table-specific rules below). Explicitly fluid: width AND
   max-width at 100% (never a fixed px), table-layout left at its `auto`
   default so column widths still follow content within that 100% — a table
   should fill whatever column/panel it renders in, never sit locked at its
   own intrinsic width regardless of a wider container. */
.tbl { width: 100%; max-width: 100%; border-collapse: collapse; font-size: var(--fs-body); }
.tbl th {
  text-align: left; font-size: var(--fs-caption); font-weight: 500;
  color: var(--muted); padding: var(--sp-2) var(--panel-pad-x);
  text-transform: uppercase; letter-spacing: 0.06em;
  border-bottom: var(--bw-thin) solid var(--hairline);
  white-space: nowrap;
}
.tbl th.num, .tbl td.num {
  text-align: right;
  font-family: var(--mono); font-variant-numeric: tabular-nums;
}
.tbl td {
  padding: var(--table-pad-y) var(--panel-pad-x);
  border-bottom: var(--bw-thin) solid var(--hairline);
  vertical-align: top;
}
.tbl tr:last-child td { border-bottom: 0; }
.tbl tr:hover td { background: var(--paper); }
.tbl tr.emph td { background: var(--paper); font-weight: 500; }
.tbl-nowrap td { white-space: nowrap; }
.tbl .table-footer {
  text-align: center;
  padding: var(--sp-2) var(--panel-pad-x);
  background: var(--paper);
  color: var(--muted);
  font-size: var(--fs-caption);
  font-style: italic;
}

/* Earnings tab: the per-quarter caption line inside the ONE shared
   "Analyst Q&A" panel (workspace_sections/earnings.py::_qa_roster_panel —
   design_language §6.2, the quarter is the per-item label under a constant
   title stated once, not a restated panel-head per quarter). */
.qa-quarter-sub { padding: var(--row-pad-y) var(--panel-pad-x) 0; }
/* Q&A rows: <details class="qa-row"> per question (P4.1 — the canonical
   <details> collapse idiom; the +/- chevron is CSS-driven). */
.qa-list { display: flex; flex-direction: column; }
.qa-row { border-top: var(--bw-thin) solid var(--hairline); }
.qa-row:first-child { border-top: 0; }
.qa-row[open] { background: var(--paper); }
.qa-head {
  width: 100%;
  display: flex; align-items: center; gap: var(--sp-3);
  padding: var(--row-pad-y) var(--panel-pad-x);
  text-align: left; cursor: pointer; user-select: none; list-style: none;
}
.qa-head::-webkit-details-marker { display: none; }
.qa-head:hover { background: var(--paper); }
.qa-chev {
  font-family: var(--mono); width: 14px; color: var(--muted);
  font-size: var(--fs-title); font-weight: 400;
}
.qa-chev::before { content: '+'; }
.qa-row[open] .qa-chev::before { content: '-'; }
.qa-tag {
  font-size: var(--fs-caption); font-weight: 600;
  padding: var(--sp-1) var(--sp-2); border: var(--bw-thin) solid var(--accent);
  color: var(--accent); background: var(--accent-soft);
  border-radius: var(--radius-full);
  letter-spacing: 0.06em; min-width: 70px; text-align: center;
}
.qa-topic { font-size: var(--fs-body); font-weight: 500; flex: 1; color: var(--fg); }
.qa-analysts { font-size: var(--fs-caption); color: var(--muted); font-style: italic; max-width: 280px; text-align: right; }
.qa-ref { font-family: var(--mono); font-size: var(--fs-caption); min-width: 38px; text-align: right; color: var(--muted); }
.qa-body {
  padding: 0 var(--panel-pad-x) var(--sp-4) calc(var(--sp-6) + var(--sp-4));
  display: flex;
  flex-direction: column; gap: var(--sp-3);
}
.qa-q, .qa-a, .qa-followup { display: flex; gap: var(--sp-3); font-size: var(--fs-body); line-height: 1.55; }
.qa-q { color: var(--fg-soft); }
.qa-a { color: var(--fg); }
.qa-followup {
  font-size: var(--fs-body); color: var(--muted);
  padding-top: var(--sp-2); border-top: var(--bw-thin) dashed var(--border);
}
.qa-label {
  font-size: var(--fs-caption); font-weight: 600;
  color: var(--accent); flex-shrink: 0; width: 14px; padding-top: var(--sp-half);
}
.qa-followup-label {
  font-size: var(--fs-caption); font-weight: 600;
  letter-spacing: 0.08em; color: var(--muted);
  text-transform: uppercase; flex-shrink: 0; padding-top: var(--sp-1);
  width: 64px;
}

/* Say-Do */
.saydo-meta { display: flex; flex-direction: column; gap: var(--sp-2); align-items: flex-end; padding-top: var(--sp-1); }
.saydo-meta-label {
  font-size: var(--fs-caption); color: var(--muted);
  text-transform: uppercase; letter-spacing: 0.06em;
}
.saydo-history {
  display: flex; flex-direction: column; gap: var(--sp-2);
  align-items: flex-end;
}
.saydo-metric { font-weight: 500; }
.saydo-guide { font-family: var(--sans); font-style: italic; font-size: var(--fs-body); color: var(--muted); }
.saydo-actual { font-family: var(--mono); font-size: var(--fs-body); }

/* Financials */
.chart-panel { padding-bottom: var(--panel-pad-y); }
.chart-wrap {
  padding: var(--sp-2) var(--panel-pad-x);
  color: var(--fg-soft);
  overflow-x: auto;
}
.legend {
  display: flex; flex-wrap: wrap; gap: var(--sp-3);
  padding: var(--sp-2) var(--panel-pad-x) var(--sp-1);
}
.legend-item {
  display: flex; align-items: center; gap: var(--sp-2);
  font-size: var(--fs-caption); color: var(--fg-soft);
}
.legend-swatch { width: 10px; height: 10px; border-radius: var(--bw-thick); }
.overlay-stat {
  position: absolute; top: 14px; right: 30px;
  display: flex; flex-direction: column; align-items: flex-end;
}
.overlay-stat span {
  font-size: var(--fs-caption); color: var(--muted);
  text-transform: uppercase; letter-spacing: 0.06em;
}
.overlay-stat strong {
  font-family: var(--mono); font-size: var(--fs-display);
  font-weight: 500; color: var(--fg); margin-top: var(--sp-half);
}
.overlay-stat .pos { font-family: var(--mono); font-size: var(--fs-caption); color: var(--accent); }

.table-scroll { overflow-x: auto; }

/* Thesis */
.val-stack { display: flex; flex-direction: column; }
.val-row {
  display: flex; justify-content: space-between; align-items: baseline;
  padding: var(--row-pad-y) var(--panel-pad-x);
  border-bottom: var(--bw-thin) solid var(--hairline);
  font-size: var(--fs-body);
}
.val-row:last-child { border-bottom: 0; }
.val-row strong { font-family: var(--mono); font-weight: 500; font-size: var(--fs-body); }
.val-row.emph { background: var(--paper); }
.val-row.emph strong { font-size: var(--fs-title); font-weight: 600; }
.val-row.muted { color: var(--muted); }
.val-row.muted strong { color: var(--muted); }

/* S6 — Bear · Base · Bull scenario range on the valuation card */
.scenario-range { display: flex; border-bottom: var(--bw-thin) solid var(--hairline); }
.scenario-cell {
  flex: 1; display: flex; flex-direction: column; gap: var(--sp-half);
  padding: var(--row-pad-y) var(--panel-pad-x);
}
.scenario-cell + .scenario-cell { border-left: var(--bw-thin) solid var(--hairline); }
.scenario-label {
  font-size: var(--fs-caption); text-transform: uppercase;
  letter-spacing: 0.06em; color: var(--muted);
}
.scenario-cell.bear .scenario-label { color: var(--bad); }
.scenario-cell.bull .scenario-label { color: var(--ok); }
.scenario-cell strong { font-family: var(--mono); font-size: var(--fs-title); font-weight: 500; }
.scenario-cell.base strong { font-size: var(--fs-title); font-weight: 600; }
.scenario-upside { font-family: var(--mono); font-size: var(--fs-caption); }
.scenario-upside.pos { color: var(--ok); }
.scenario-upside.neg { color: var(--bad); }
.scenario-upside.muted { color: var(--muted); }
.scenario-bar { padding: var(--sp-2) var(--panel-pad-x) var(--sp-3); border-bottom: var(--bw-thin) solid var(--hairline); }
.scenario-bar-track {
  position: relative; height: 6px; border-radius: var(--radius-full);
  background: linear-gradient(90deg, color-mix(in srgb, var(--bad) 16%, transparent), color-mix(in srgb, var(--ok) 16%, transparent));
}
.scenario-bar-price {
  position: absolute; top: -3px; bottom: -3px; width: var(--bw-thick);
  border-radius: var(--radius-sm); background: var(--fg);
}
.scenario-bar-base { position: absolute; top: 0; bottom: 0; width: var(--bw-thick); background: var(--muted); }
.scenario-bar-legend {
  display: flex; justify-content: space-between; margin-top: var(--sp-1);
  font-family: var(--mono); font-size: var(--fs-caption); color: var(--muted);
}

/* Break-rule status is STATUS color (design_language §2): green=good like
   every other surface — the old accent-blue "ok" predated the green
   unification and left the report disagreeing with the dashboard's pills. */
.break-status-ok { color: var(--ok); }
.break-status-warn { color: var(--warn); }
.break-status-breach { color: var(--bad); }
.break-status-unresolved { color: var(--muted); }

.failure { display: flex; gap: var(--sp-3); padding: var(--row-pad-y) var(--panel-pad-x); border-bottom: var(--bw-thin) solid var(--hairline); }
.failure:last-child { border-bottom: 0; }
.failure-num {
  font-family: var(--mono); font-size: var(--fs-caption); font-weight: 600;
  color: var(--accent); width: 22px; flex-shrink: 0; padding-top: var(--sp-half);
}
.failure-body { flex: 1; }
.failure-title {
  font-size: var(--fs-body); font-weight: 500; line-height: 1.5;
  margin-bottom: var(--sp-2); color: var(--fg);
}
.failure-meta {
  display: grid; grid-template-columns: 110px 1fr; gap: var(--sp-2) var(--sp-3);
  font-size: var(--fs-caption); color: var(--fg-soft); line-height: 1.55;
  margin-top: var(--sp-2);
}
.failure-label {
  font-size: var(--fs-caption); font-weight: 600;
  letter-spacing: 0.06em; color: var(--accent);
  text-transform: uppercase;
  padding-top: var(--bw-thin);
}

/* ============================================================
   Company tab
   ============================================================ */
.elevator-block {
  border: var(--bw-thin) solid var(--border-2);
  background: linear-gradient(180deg, var(--paper), var(--surface));
  padding: var(--sp-5) var(--sp-5);
  border-radius: var(--radius);
  font-family: var(--sans);
  font-size: var(--fs-title);
  line-height: 1.55;
  color: var(--fg);
}
.elevator-block::before {
  content: 'Elevator pitch';
  display: block;
  font-size: var(--fs-caption); font-weight: 600;
  letter-spacing: 0.1em; text-transform: uppercase;
  color: var(--muted); margin-bottom: var(--sp-2);
}
.seg-desc { color: var(--fg-soft); font-size: var(--fs-body); }
.seg-bar {
  display: inline-block; height: 6px; vertical-align: middle;
  background: var(--accent); border-radius: var(--bw-thick); min-width: var(--bw-thin);
}

.ir-card {
  padding: var(--row-pad-y) var(--panel-pad-x);
  border-bottom: var(--bw-thin) solid var(--hairline);
}
.ir-card:last-child { border-bottom: 0; }
.ir-card-head {
  display: flex; align-items: center; gap: var(--sp-2);
  margin-bottom: var(--sp-2);
}
.ir-type {
  font-size: var(--fs-caption); font-weight: 600;
  padding: var(--sp-1) var(--sp-2); border-radius: var(--radius-full);
  background: var(--accent-soft); color: var(--accent);
  letter-spacing: 0.06em; text-transform: uppercase;
}
.ir-quarter {
  font-family: var(--mono); font-size: var(--fs-caption); color: var(--fg);
}
.ir-link {
  font-size: var(--fs-caption); color: var(--accent); text-decoration: none;
}
.ir-link:hover { text-decoration: underline; }
.ir-summary {
  font-size: var(--fs-body); color: var(--fg-soft); line-height: 1.5;
  margin: 0;
}

/* ============================================================
   Position tab
   ============================================================ */
.position-yousaid {
  margin: 0 0 var(--gap); font-size: var(--fs-body);
}
.position-yousaid .ys-line {
  display: flex; flex-wrap: wrap; align-items: baseline; gap: var(--sp-2);
}
.position-yousaid .k-empty { padding: 0; }
.position-coaching {
  display: flex; align-items: center; gap: var(--sp-3); flex-wrap: wrap;
  padding: 0 0 var(--gap);
}
.position-coaching .coaching-line { margin: 0; }
.position-stats {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
  gap: var(--bw-thin);
  background: var(--border);
  border-radius: var(--radius);
  overflow: hidden;
  border: var(--bw-thin) solid var(--border);
}
.position-stat {
  background: var(--surface);
  padding: var(--sp-4) var(--sp-4);
}
.position-stat-label {
  font-size: var(--fs-caption); color: var(--muted);
  text-transform: uppercase; letter-spacing: 0.08em;
  margin-bottom: var(--sp-2);
}
.position-stat-value {
  font-family: var(--mono); font-size: var(--fs-display); font-weight: 500;
  font-variant-numeric: tabular-nums; letter-spacing: -0.01em;
}
.position-stat-sub {
  font-family: var(--mono); font-size: var(--fs-caption);
  margin-top: var(--sp-1); color: var(--muted);
}
.decision-card {
  padding: var(--row-pad-y) var(--panel-pad-x);
  border-bottom: var(--bw-thin) solid var(--hairline);
}
.decision-card:last-child { border-bottom: 0; }
.decision-head {
  display: flex; align-items: baseline; gap: var(--sp-2);
  margin-bottom: var(--sp-2); flex-wrap: wrap;
}
.decision-date {
  font-family: var(--mono); font-size: var(--fs-caption); color: var(--muted);
}
.decision-action {
  font-size: var(--fs-caption); font-weight: 600;
  padding: var(--sp-half) var(--sp-2); border-radius: var(--radius-full);
  background: var(--accent-soft); color: var(--accent);
  letter-spacing: 0.06em; text-transform: uppercase;
}
.decision-confidence {
  font-size: var(--fs-caption); color: var(--muted); font-style: italic;
}
.decision-outcome {
  font-size: var(--fs-caption); font-weight: 600;
  padding: var(--sp-half) var(--sp-2); border-radius: var(--radius-full);
  letter-spacing: 0.06em; text-transform: uppercase;
}
/* Outcomes are STATUS: green=good (the accent "validated" predated the
   green unification), soft color-mix fills per the chip convention. */
.decision-outcome.validated { color: var(--ok);
  background: color-mix(in srgb, var(--ok) 12%, transparent); }
.decision-outcome.invalidated { color: var(--bad);
  background: color-mix(in srgb, var(--bad) 12%, transparent); }
.decision-outcome.partial { color: var(--warn);
  background: color-mix(in srgb, var(--warn) 12%, transparent); }
.decision-thesis {
  font-size: var(--fs-body); color: var(--fg-soft); line-height: 1.55;
  margin: 0;
}

/* ============================================================
   Sources tab
   ============================================================ */
/* Coverage matrix modifiers on .tbl: mono cells + centered dot columns. */
.coverage-table th.cov-cell { text-align: center; }
.coverage-table td { font-family: var(--mono); font-variant-numeric: tabular-nums; }
.cov-cell { text-align: center; }
.cov-yes { color: var(--accent); }
.cov-no { color: var(--border-2); }

.transcript-block {
  border-top: var(--bw-thin) solid var(--hairline);
}
.transcript-block:first-child { border-top: 0; }
.transcript-block summary {
  padding: var(--row-pad-y) var(--panel-pad-x);
  cursor: pointer; user-select: none;
  font-size: var(--fs-body); font-weight: 500;
  display: flex; align-items: center; gap: var(--sp-2);
}
.transcript-block summary::-webkit-details-marker { display: none; }
.transcript-block summary::marker { content: ''; }
.transcript-block summary::before {
  content: '▸';
  font-family: var(--mono); font-size: var(--fs-caption); color: var(--muted);
  display: inline-block;
  transition: transform var(--transition);
}
.transcript-block[open] summary::before { transform: rotate(90deg); }
.transcript-block summary:hover { background: var(--paper); }
.transcript-text {
  padding: var(--sp-3) var(--panel-pad-x) var(--sp-5);
  font-family: var(--mono); font-size: var(--fs-caption); line-height: 1.55;
  color: var(--fg-soft);
  white-space: pre-wrap; word-wrap: break-word;
  max-height: 600px; overflow-y: auto;
  background: var(--paper);
}

/* ============================================================
   Peer row
   ============================================================ */
.peer-row {
  margin: var(--section-gap) var(--pad-x) 0;
  padding-top: var(--sp-5);
  border-top: var(--bw-thin) solid var(--border);
}
.peer-head {
  display: flex; justify-content: space-between; align-items: baseline;
  padding-bottom: var(--sp-3);
  font-size: var(--fs-caption); color: var(--muted);
  flex-wrap: wrap; gap: var(--sp-2);
}
.peer-eyebrow {
  font-size: var(--fs-caption); font-weight: 600;
  letter-spacing: 0.1em; text-transform: uppercase;
  color: var(--muted); margin-right: var(--sp-3);
}
.peer-eyebrow-sub { color: var(--muted); }
.peer-grid {
  display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
  gap: var(--bw-thin); background: var(--border);
  border: var(--bw-thin) solid var(--border);
  border-radius: var(--radius);
  overflow: hidden;
}
.peer {
  appearance: none; border: 0;
  background: var(--surface);
  padding: var(--sp-3) var(--sp-3);
  display: flex; flex-direction: column; gap: var(--sp-2);
  text-align: left; font: inherit;
  color: inherit; text-decoration: none;
}
.peer:hover { background: var(--paper); }
.peer.active { background: var(--fg); color: var(--bg); }
.peer-top {
  display: flex; justify-content: space-between; align-items: center;
}
.peer-ticker {
  font-family: var(--mono); font-weight: 600; font-size: var(--fs-body);
  letter-spacing: -0.005em;
}
.peer-name { font-size: var(--fs-caption); color: var(--muted); }
.peer.active .peer-name { color: var(--muted); }

/* ============================================================
   Footer
   ============================================================ */
.l1-footer {
  display: flex; justify-content: space-between; gap: var(--sp-4);
  padding: var(--sp-5) var(--pad-x) 0;
  margin-top: var(--sp-6);
  border-top: var(--bw-thin) solid var(--border);
  font-size: var(--fs-caption); color: var(--fg-soft);
  align-items: baseline;
  flex-wrap: wrap;
}

/* ============================================================
   Tweaks panel
   ============================================================ */
.twk-toggle-btn {
  appearance: none;
  border: var(--bw-thin) solid var(--border);
  background: var(--surface);
  width: 30px; height: 30px;
  border-radius: var(--radius);
  cursor: pointer;
  color: var(--fg-soft);
  font-size: var(--fs-body);
}
.twk-toggle-btn:hover { color: var(--fg); border-color: var(--border-2); }

.twk-surface {
  position: fixed; right: var(--sp-4); bottom: var(--sp-4);
  bottom: calc(var(--sp-4) + env(safe-area-inset-bottom, 0px));
  z-index: 2147483646;
  width: 280px;
  display: none;
  flex-direction: column;
  background: var(--surface);
  color: var(--fg);
  border: var(--bw-thin) solid var(--border-2);
  border-radius: var(--radius);
  box-shadow: var(--shadow-pop);
  font-family: var(--sans); font-size: var(--fs-body);
  overflow: hidden;
}
.twk-surface.open { display: flex; }
.twk-hd {
  display: flex; align-items: center; justify-content: space-between;
  padding: var(--sp-2) var(--sp-2) var(--sp-2) var(--sp-3);
}
.twk-hd b { font-size: var(--fs-caption); font-weight: 600; letter-spacing: .01em; }
.twk-x {
  appearance: none; border: 0; background: transparent;
  color: var(--muted); width: var(--sp-5); height: var(--sp-5);
  border-radius: var(--radius); cursor: pointer; font-size: var(--fs-body);
}
.twk-x:hover { background: var(--paper); color: var(--fg); }
.twk-body {
  padding: var(--sp-1) var(--sp-3) var(--sp-3);
  display: flex; flex-direction: column; gap: var(--sp-3);
}
.twk-sect {
  font-size: var(--fs-caption); font-weight: 600;
  letter-spacing: .08em; text-transform: uppercase;
  color: var(--muted); padding-top: var(--sp-2);
}
.twk-sect:first-child { padding-top: 0; }
.twk-row { display: flex; flex-direction: column; gap: var(--sp-2); }
.twk-row-h {
  flex-direction: row; align-items: center; justify-content: space-between;
}
.twk-lbl { color: var(--fg-soft); font-weight: 500; }
.twk-seg {
  display: flex; padding: var(--sp-half); border-radius: var(--radius);
  background: var(--paper);
  border: var(--bw-thin) solid var(--border);
}
.twk-seg button {
  appearance: none; border: 0; background: transparent;
  flex: 1; padding: var(--sp-1) var(--sp-2); border-radius: calc(var(--radius) - var(--bw-thick));
  font: inherit; font-weight: 500; color: var(--muted);
  cursor: pointer;
}
.twk-seg button.active {
  background: var(--surface); color: var(--fg);
  /* --scrim is the palette's one black-wash primitive (a neutral 50%-alpha
     black in both themes, per tokens.py) — color-mix at 12% reproduces the
     exact alpha this micro shadow needs, without a raw function-color literal. */
  box-shadow: 0 var(--bw-thin) var(--bw-thick) color-mix(in srgb, var(--scrim) 12%, transparent);
}
.twk-toggle {
  position: relative; width: var(--sp-6); height: 18px;
  border: 0; border-radius: var(--radius-full);
  background: var(--border-2); cursor: pointer; padding: 0;
}
.twk-toggle[data-on="1"] { background: var(--accent); }
.twk-toggle::after {
  content: ''; position: absolute; top: var(--bw-thick); left: var(--bw-thick);
  width: 14px; height: 14px; border-radius: 50%;
  background: var(--surface);
  /* Same --scrim color-mix idiom as .twk-seg button.active above, at a
     stronger mix for this knob's slightly deeper shadow. */
  box-shadow: 0 var(--bw-thin) var(--bw-thick) color-mix(in srgb, var(--scrim) 50%, transparent);
  transition: transform var(--transition);
}
.twk-toggle[data-on="1"]::after { transform: translateX(14px); }

/* ============================================================
   Financials line-item drill-down + charts_v2 dark overrides
   ============================================================ */
.fin-row.drillable { cursor: pointer; }
.fin-row.drillable:hover td { background: var(--paper); }
.fin-chev {
  display: inline-block; width: 14px;
  font-family: var(--mono); font-size: var(--fs-caption);
  color: var(--muted);
}
.fin-drill td.fin-drill-cell {
  padding: 0 !important;
  background: var(--paper);
}
.fin-drill-table {
  width: 100%;
  max-width: 100%;
  margin: 0;
  background: var(--paper);
}
.fin-drill-table th, .fin-drill-table td {
  border: 0 !important;
  border-bottom: var(--bw-thin) solid var(--hairline) !important;
  padding: var(--sp-2) var(--sp-3) !important;
}
.fin-sum-row td {
  background: var(--surface) !important;
  font-family: var(--mono);
}

/* Dark-mode override for the charts_v2 YoY matrix — the upstream CSS is
   light-themed (white/grey backgrounds, dark text). Override the colors
   to match the workspace dark palette so the matrix doesn't stand out. */
:root[data-theme="dark"] .cv2-matrix-wrap { color: var(--fg-soft); }
:root[data-theme="dark"] .cv2-matrix-title { color: var(--fg); }
:root[data-theme="dark"] .cv2-matrix { color: var(--fg-soft); }
:root[data-theme="dark"] .cv2-matrix th,
:root[data-theme="dark"] .cv2-matrix td {
  border-color: var(--border) !important;
}
:root[data-theme="dark"] .cv2-matrix-label,
:root[data-theme="dark"] .cv2-matrix-q,
:root[data-theme="dark"] .cv2-matrix-cagr {
  color: var(--fg) !important;
  background: var(--paper) !important;
}
:root[data-theme="dark"] .cv2-matrix-noisy {
  color: var(--muted) !important;
  background: var(--paper) !important;
}
:root[data-theme="dark"] .cv2-matrix-footnote { color: var(--muted) !important; }

/* Pillbox warn variant — used by the Validation panel chip. */
.pill-warn-cell { color: var(--warn); }

/* ============================================================
   Thesis tab: hygiene panels + ledger collapsible + underweighted
   ============================================================ */
.grid-thesis-hygiene {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
  gap: var(--gap);
}
.thesis-list {
  margin: 0;
  padding: var(--sp-3) var(--panel-pad-x) var(--sp-3) var(--sp-6);
  font-size: var(--fs-body);
  line-height: 1.55;
  color: var(--fg-soft);
}
.thesis-list li { margin-bottom: var(--sp-2); }
.thesis-list li:last-child { margin-bottom: 0; }

/* Enriched §2 KPI ledger rows: a clean name with the qualifier demoted to a
   muted definition line, a sparkline + YoY/QoQ delta trend cell, a staleness
   flag, and the "tracked, no data yet" footnote. */
.kpi-ledger-table td:first-child { white-space: normal; max-width: 300px; }
.kpi-ledger-row td { vertical-align: top; }
.ledger-def { margin-top: var(--sp-1); font-weight: 400; white-space: normal; max-width: 300px; }
.ledger-trend { white-space: nowrap; }
.ledger-spark { color: var(--accent); display: inline-block; vertical-align: middle; }
.ledger-spark svg { display: inline-block; vertical-align: middle; }
.ledger-delta {
  font-family: var(--mono); font-size: var(--fs-caption); color: var(--muted); white-space: nowrap;
}
.ledger-stale {
  margin-left: var(--sp-2); font-size: var(--fs-caption); font-weight: 600; text-transform: uppercase;
  letter-spacing: 0.04em; color: var(--warn);
  border: var(--bw-thin) solid var(--warn); border-radius: var(--radius-full); padding: 0 var(--sp-1);
}
/* Dim a stale row's data but keep the flag itself at full strength. */
.ledger-stale-row td { opacity: 0.55; }
.ledger-stale-row .ledger-stale { opacity: 1; }
/* Doorway: a KPI name that opens the exact series in Ask (Law 2). Looks like
   the bold label it replaces; reveals a dashed accent underline on
   hover/keyboard focus so the affordance reads without crowding the table. */
.fact-doorway {
  font: inherit; font-weight: 600; color: var(--fg);
  background: none; border: 0; padding: 0; cursor: pointer; text-align: left;
  border-bottom: var(--bw-thin) dashed transparent;
}
.fact-doorway:hover, .fact-doorway:focus-visible {
  color: var(--accent); border-bottom-color: var(--accent); outline: none;
}
.ledger-tracked-only {
  padding: var(--sp-2) var(--panel-pad-x) var(--sp-3);
  border-top: var(--bw-thin) dashed var(--hairline);
}
.ledger-tracked-only strong { color: var(--fg); }

.underweighted-panel { border-color: var(--warn); }
.underweighted-panel .panel-title { color: var(--warn); }

/* News tab: per-section sub-panels use a slightly tighter grid than the
   page-level news strip used to. */
.news-grid-tab {
  padding: var(--sp-3) var(--panel-pad-x) var(--sp-3);
  grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
}

/* Peer-row tile variants by list type — quiet borders that hint at
   portfolio / watchlist / evaluation membership without shouting. */
.peer-portfolio { border-left: 3px solid var(--border-2); }
.peer-watchlist { border-left: 3px solid var(--muted); }
.peer-evaluation { border-left: 3px solid var(--border-2); }
.peer-grid {
  grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
}
a.peer { color: inherit; text-decoration: none; }
a.peer:hover { background: var(--paper); }

/* Misc tiny helpers used by the higher-density panels. */
.xsmall { font-size: var(--fs-caption); line-height: 1.4; }
.stub-warning {
  background: color-mix(in srgb, var(--bad) 16%, transparent);
  border-left: 3px solid var(--bad);
  padding: var(--sp-2) var(--sp-3);
  border-radius: var(--radius);
  font-size: var(--fs-body);
  color: var(--fg);
}
/* Break-rules detail row — appears under each main rule row when narrative /
   detail / observations are present. Quieter background so the parent row
   reads first. */
.break-detail td {
  padding: var(--sp-1) var(--panel-pad-x) var(--sp-2) !important;
  border-top: 0 !important;
  background: var(--paper);
  color: var(--muted);
}
.break-row td {
  vertical-align: top;
}
.break-row .muted.xsmall {
  display: block;
  margin-top: var(--sp-1);
  font-size: var(--fs-caption);
  font-weight: 400;
  line-height: 1.4;
}

/* Position tab — decision brief link + outcome notes block. */
.decision-brief-link {
  font-size: var(--fs-caption);
  color: var(--accent);
  text-decoration: none;
  font-family: var(--mono);
  padding: var(--bw-thin) var(--sp-2);
  border: var(--bw-thin) solid var(--accent);
  border-radius: var(--radius-full);
}
.decision-brief-link:hover { background: var(--accent-soft); }
.decision-outcome-block {
  margin-top: var(--sp-2);
  padding: var(--sp-2) var(--sp-2);
  background: var(--paper);
  border-radius: var(--radius);
  line-height: 1.45;
}

/* ============================================================
   Valuation tab
   ============================================================ */
.valuation-headline { padding: var(--sp-3) var(--sp-4); }
.valuation-headline-row {
  display: flex; align-items: baseline; gap: var(--sp-5); padding: var(--sp-2) 0 var(--sp-3);
  border-bottom: var(--bw-thin) dashed var(--hairline); flex-wrap: wrap;
}
.valuation-current { display: flex; flex-direction: column; gap: var(--sp-half); }
.valuation-current-value {
  font-family: var(--mono); font-size: var(--fs-display); font-weight: 600;
  letter-spacing: -0.02em; color: var(--fg);
}
.valuation-current-label { font-size: var(--fs-caption); color: var(--muted); text-transform: uppercase; letter-spacing: 0.06em; }
.valuation-band { display: flex; flex-direction: column; gap: var(--sp-half); font-size: var(--fs-body); color: var(--muted); }
.valuation-band-row { display: flex; gap: var(--sp-2); }
.valuation-band-row .mono { font-family: var(--mono); color: var(--fg); }
.valuation-peg { display: flex; flex-direction: column; gap: var(--sp-half); }
.valuation-peg-value {
  font-family: var(--mono); font-size: var(--fs-display); font-weight: 600;
  letter-spacing: -0.01em; color: var(--fg);
}
.valuation-peg-label { font-size: var(--fs-caption); color: var(--muted); text-transform: uppercase; letter-spacing: 0.06em; }
.valuation-peg-sub { font-size: var(--fs-caption); color: var(--muted); font-family: var(--mono); }
.valuation-verdict {
  margin-left: auto; padding: var(--sp-2) var(--sp-2);
  background: var(--paper); border: var(--bw-thin) solid var(--hairline); border-radius: var(--radius);
  font-size: var(--fs-caption); color: var(--fg); font-style: italic;
}
.valuation-spark { padding: var(--sp-3) 0 var(--sp-1); }
.valuation-spark-axis {
  display: flex; justify-content: space-between; font-size: var(--fs-caption);
  color: var(--muted); padding-bottom: var(--sp-2);
}

/* ============================================================
   Thesis lede (top of report, below identity)
   ============================================================ */
.l1-thesis {
  /* Keep the lede in the document flow instead of presenting it as a
     colored status card. */
  margin: 0 var(--pad-x) var(--sp-2);
  padding: var(--sp-3) 0 var(--sp-4);
  background: transparent;
  border-bottom: var(--bw-thin) solid var(--border);
  border-radius: 0;
}
.l1-thesis .thesis-label {
  font-size: var(--fs-caption); text-transform: uppercase; letter-spacing: 0.08em;
  color: var(--muted); font-weight: 600; margin-right: var(--sp-2);
}
.l1-thesis p {
  margin: var(--sp-1) 0 0; font-family: var(--sans);
  font-size: var(--fs-body); line-height: 1.55; color: var(--fg);
  /* Override the 1180px cap from the earlier .l1-thesis p rule — the thesis
     paragraph should span the full available width of the workspace shell
     so dense theses don't break across short, awkward lines. */
  max-width: none;
}

/* ============================================================
   "What changed" hoist (five_min_reread lens lede)
   ============================================================ */
/* Mirror the thesis lede's neutral document treatment: the standing read
   and the latest delta are separate registers, not competing alerts. */
.l1-reread {
  display: flex; gap: var(--sp-5); align-items: baseline;
  margin: 0 var(--pad-x) var(--sp-2);
  padding: var(--sp-3) 0 var(--sp-4);
  background: transparent;
  border-bottom: var(--bw-thin) solid var(--border);
  border-radius: 0;
}
.l1-reread .reread-label {
  font-size: var(--fs-caption); text-transform: uppercase; letter-spacing: 0.08em;
  color: var(--muted); font-weight: 600; flex-shrink: 0;
}
.l1-reread p {
  margin: 0; flex: 1; font-size: var(--fs-body); line-height: 1.55; color: var(--fg-soft);
}
.l1-reread .panel-xlink { flex-shrink: 0; white-space: nowrap; }

/* ============================================================
   Investment Decision Card strip (PRD §8.1, P1.1)
   ============================================================ */
.l1-decision-card {
  margin: 0 var(--pad-x) var(--sp-2);
  padding: var(--panel-pad-y) var(--panel-pad-x);
  background: var(--surface);
  border: var(--bw-thin) solid var(--border);
  border-radius: var(--radius);
}
.l1-decision-card .dc-head {
  display: flex; align-items: center; gap: var(--gap);
  margin-bottom: var(--sp-3);
}
.l1-decision-card .dc-label {
  font-size: var(--fs-caption); text-transform: uppercase; letter-spacing: 0.08em;
  color: var(--muted); font-weight: 600;
}
.l1-decision-card .dc-suggested {
  font-size: var(--fs-caption); color: var(--fg-soft); margin-left: auto;
}
.l1-decision-card .dc-grid {
  display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
  gap: var(--gap-lg);
}
.l1-decision-card .dc-cell h4 {
  margin: 0 0 var(--sp-1); font-size: var(--fs-caption); text-transform: uppercase;
  letter-spacing: 0.04em; color: var(--muted);
}
.l1-decision-card .dc-cell p {
  margin: 0; font-size: var(--fs-body); line-height: 1.5; color: var(--fg);
}
.l1-decision-card .dc-blockers {
  margin-top: var(--sp-3); font-size: var(--fs-caption); color: var(--warn);
}
.l1-decision-card .dc-blockers ul { margin: var(--sp-1) 0 0; padding-left: var(--sp-4); }
.l1-decision-card .dc-uncertainty {
  margin-top: var(--sp-3); font-size: var(--fs-caption); color: var(--fg-soft);
}
.l1-decision-card .dc-actions {
  display: flex; align-items: center; flex-wrap: wrap; gap: var(--gap);
  margin-top: var(--sp-3); padding-top: var(--sp-3); border-top: var(--bw-thin) solid var(--hairline);
}
.l1-decision-card .dc-status {
  font-size: var(--fs-caption); color: var(--fg-soft); margin-top: var(--sp-1);
}

/* ============================================================
   Print
   ============================================================ */
@media print {
  .l1-chrome, .twk-toggle-btn, .twk-surface { display: none; }
  .tab-group-pane { display: block !important; }
  .tab-pane { display: block !important; padding-top: var(--sp-4); break-before: page; }
  .tabs, .subtabs { display: none; }
  .news-strip, .kpi-strip, .l1-identity { break-after: avoid; }
}

/* ============================================================
   Phase 5 — Executive Compensation tab
   ============================================================ */
/* the CEO role tag rides the kit's .k-chip .k-chip-mono (outline mono) now —
   accent fill is interactive-only (§2), a role tag stays quiet. */
.ceo-pill { margin-left: var(--sp-2); vertical-align: middle; }
.kpi-match { color: var(--ok); font-weight: 500; }

.insider-table tr.tx-buy { background: color-mix(in srgb, var(--ok) 6%, transparent); }
.insider-table tr.tx-sell { background: color-mix(in srgb, var(--bad) 4%, transparent); }
.insider-table td.signal-strong { color: var(--ok); font-weight: 600; }
.insider-table td.signal-medium { color: var(--accent); font-weight: 500; }
.insider-table td.signal-weak { color: var(--muted); }

ul.flag-list {
  list-style: none;
  padding: var(--sp-3);
  margin: 0;
  display: flex;
  flex-direction: column;
  gap: var(--sp-2);
}
ul.flag-list li {
  padding: var(--sp-2) var(--sp-3);
  border-left: 3px solid;
  background: var(--paper);
  font-size: var(--fs-body);
  line-height: 1.5;
}
ul.flag-list li.flag-warn { border-left-color: var(--warn); }
ul.flag-list li.flag-positive {
  border-left-color: var(--ok);
  background: color-mix(in srgb, var(--ok) 6%, transparent);
}

/* ============================================================
   Synthesis tab — lens artifact panels
   ============================================================ */
/* No local margin: .lens-panel is a direct child of .tab-body, whose own
   `gap: var(--gap-lg)` already spaces every panel in a tab (the rhythm
   tokens block at the top of this file). A local margin here used to
   STACK on top of that gap (var(--sp-5) + var(--sp-4) = var(--sp-6) between two lens panels)
   — the "too much space between vertical sections" this pass fixes. */
.lens-panel .panel-head {
  display: flex;
  flex-direction: column;
  gap: var(--sp-half);
  padding-bottom: var(--sp-1);
}
.lens-panel .lens-body { padding: var(--sp-3) var(--sp-4) var(--sp-4); }
/* .lens-warn / .lens-stale migrated to the control kit's .k-pill (+ k-pill-warn
   for DIRTY; neutral bare for STALE) — see workspace_sections/synthesis.py. */
.lens-five_min_reread .lens-body h2 {
  color: var(--accent);
  font-size: var(--fs-title);
  margin-top: 1em;
}
.lens-five_min_reread .lens-body strong { color: var(--fg); }

/* ============================================================
   Decision history — last 3 LLM recommendations from the
   audit ledger (decisions table, migration 0046). Rendered in
   the Thesis tab between the valuation/break-rule grid and the
   thesis-hygiene panels.
   ============================================================ */
/* No local margin-top: a direct .tab-body child already gets the shared
   panel-to-panel gap (see the .lens-panel note above — same fix). */
.decision-list {
  display: flex;
  flex-direction: column;
  gap: var(--sp-2);
  padding: var(--sp-2) var(--sp-3);
}
.decision-badge {
  display: grid;
  grid-template-columns: 72px 1fr auto;
  align-items: center;
  gap: var(--sp-2);
  padding: var(--sp-2) var(--sp-2);
  border-radius: var(--radius);
  background: var(--surface);
  border: var(--bw-thin) solid var(--hairline);
  font-size: var(--fs-body);
}
.decision-badge .decision-date { color: var(--muted); }
.decision-badge .decision-kind {
  font-weight: 600;
  letter-spacing: 0.4px;
  color: var(--fg);
}
/* The outcome chip now rides the control kit's .k-pill (shape + tone fill); this
   selector adds only its uppercase-micro typographic refinement. Tone is routed
   in Python (_OUTCOME_PILL_TONE in workspace_sections/thesis_risk.py): correct ->
   k-pill-ok, wrong -> k-pill-bad, mixed -> k-pill-warn, pending/unfalsifiable ->
   neutral bare .k-pill. */
.decision-badge .decision-outcome {
  font-size: var(--fs-caption);
  text-transform: uppercase;
  letter-spacing: 0.06em;
}

/* ============================================================
   Cross-quarter themes — prepared remarks vs Q&A rollup (§5 split)
   ============================================================ */
.theme-bucket { margin-top: var(--sp-3); }
.theme-bucket:first-of-type { margin-top: var(--sp-1); }
.theme-bucket-title {
  margin: var(--sp-2) 0 var(--sp-2);
  font-size: var(--fs-body);
  text-transform: uppercase;
  letter-spacing: 0.06em;
  color: var(--muted);
}
.theme-rollup-list { list-style: none; padding: 0; margin: 0; }
.theme-row {
  padding: var(--sp-2) 0 var(--sp-2);
  border-top: var(--bw-thin) solid var(--hairline);
}
.theme-row:first-child { border-top: 0; }
.theme-head { margin-bottom: var(--sp-1); font-size: var(--fs-body); }
.theme-spark {
  display: flex;
  flex-wrap: wrap;
  gap: var(--sp-2);
  margin: var(--sp-1) 0 var(--sp-2);
}
.theme-spark-cell {
  display: inline-flex;
  align-items: center;
  gap: var(--sp-1);
  padding: var(--sp-half) var(--sp-2);
  background: var(--paper);
  border-radius: var(--radius-full);
  font-size: var(--fs-caption);
  color: var(--muted);
}
.theme-spark-n {
  color: var(--accent);
  font-weight: 600;
  font-variant-numeric: tabular-nums;
}
.theme-evidence {
  list-style: none;
  padding: 0 0 0 var(--sp-3);
  margin: var(--sp-1) 0 0;
  border-left: var(--bw-thick) solid var(--border);
}
.theme-evidence li {
  font-size: var(--fs-caption);
  margin: var(--sp-1) 0;
  color: var(--fg-soft);
  line-height: 1.45;
}
.theme-evidence em { font-style: normal; }
.theme-note {
  font-size: var(--fs-caption);
  margin: 0 0 var(--sp-2);
}

/* ============================================================
   P3 panels (P4-A1) — macro sensitivities, strategic targets,
   customer concentrations, lease ladder, decision history full
   ledger, say-do verdicts, peer comp. Most reuse the canonical .tbl
   class; the styles below are for the Decisions tab's
   summary-chip ribbon + small layout tweaks.
   ============================================================ */
/* No local margin-top on these seven: each renders as a direct .tab-body
   child, which already spaces every panel via its own flex gap (see the
   .lens-panel note above) — var(--gap) here used to add a second, smaller
   gap on top of that shared one instead of matching it, so two P3 panels
   back to back were spaced inconsistently with every other pair of panels
   in the same tab. */
/* Phase 3 (comparable_sets_bottoms_up.md §11) — Sector context card: the
   industry/sector/benchmark-proxy chip row. Layout only (flex/gap/padding);
   the .k-well/.k-chip fills are the kit's, not re-skinned here. */
.comp-set-benchmark-row {
  display: flex;
  flex-wrap: wrap;
  gap: var(--sp-3);
  padding: var(--sp-3) var(--panel-pad-x);
}
.comp-set-benchmark-row .k-well {
  display: inline-flex;
  align-items: center;
  gap: var(--sp-2);
}
.decision-chips {
  display: flex;
  flex-wrap: wrap;
  gap: var(--sp-2);
  padding: var(--sp-3) var(--panel-pad-x) var(--sp-1);
}
.decision-chips-sub {
  padding-top: 0;
  padding-bottom: var(--sp-3);
}
.decision-chip {
  display: inline-flex;
  align-items: center;
  gap: var(--sp-2);
  padding: var(--sp-1) var(--sp-2);
  background: var(--paper);
  border: var(--bw-thin) solid var(--hairline);
  border-radius: var(--radius-full);
  font-size: var(--fs-caption);
}
.decision-chip-label {
  
  font-size: var(--fs-caption);
  font-weight: 600;
  letter-spacing: 0.05em;
  color: var(--fg);
}
.decision-chip-n {
  font-family: var(--mono);
  font-variant-numeric: tabular-nums;
  color: var(--accent);
  font-weight: 600;
}
.decision-chip.decision-chip-muted .decision-chip-label {
  color: var(--muted);
  text-transform: uppercase;
}
.decision-chip.decision-chip-muted .decision-chip-n {
  color: var(--fg-soft);
}

/* ============================================================
   Responsive overrides (S16 PR1 + L13 PR2)
   ============================================================ */

/* Horizontal table scroll at small-laptop/tablet widths. */
@media (max-width: 1024px) {
  .tbl { display: block; overflow-x: auto; -webkit-overflow-scrolling: touch; }
}

/* Narrow/tablet: KPI strip drops from 4 to 2 columns; multi-column grids
   collapse; identity cluster wraps. */
@media (max-width: 900px) {
  .kpi-strip { grid-template-columns: repeat(2, 1fr); }
  .grid-2col, .grid-fin, .grid-thesis-top, .grid-thesis-bottom {
    grid-template-columns: 1fr;
  }
  .identity-right { flex-wrap: wrap; }
}

/* Tablet portrait: JavaScript defaults the persistent sidebar to the canonical
   icon rail while leaving the toggle operable for keyboard, touch, and pointer. */
@media (max-width: 768px) {
  :root { --pad-x: var(--sp-3); }
  .kpi-strip { grid-template-columns: 1fr; }
  .kpi-tile { min-width: 0; }
  .kpi-spark svg { width: 100%; height: auto; }
  .subtabs { overflow-x: auto; }
}

@media (prefers-reduced-motion: reduce) {
  .report-sidebar,
  .report-sidebar-toggle,
  .report-sidebar-toggle svg { transition: none; }
}

/* Keyboard-shortcut help overlay (JS-injected by workspace_script; ? toggles,
   Esc / click closes). Token-only so it tracks the active theme. */
.ws-kbd-help { position: fixed; inset: 0; z-index: 60; display: none;
  align-items: center; justify-content: center;
  background: color-mix(in srgb, var(--bg) 72%, transparent); }
.ws-kbd-help.is-open { display: flex; }
.ws-kbd-surface { background: var(--surface); border: var(--bw-thin) solid var(--border);
  border-radius: var(--radius); box-shadow: var(--shadow-pop); padding: var(--sp-4) var(--sp-5);
  min-width: 280px; }
.ws-kbd-title { font-size: var(--fs-body); font-weight: 600; margin-bottom: var(--sp-2); }
.ws-kbd-list { display: grid; grid-template-columns: auto 1fr; gap: var(--sp-2) var(--sp-4);
  margin: 0; font-size: var(--fs-caption); }
.ws-kbd-list dt { font-family: var(--mono); color: var(--accent); }
.ws-kbd-list dd { margin: 0; color: var(--muted); }
.ws-kbd-hint { margin-top: var(--sp-3); font-size: var(--fs-caption); color: var(--muted); }
"""
)


# Component CSS is composed here so the workspace has one visual owner.
# Modules re-export their slice for backwards-compatible tests/imports.
CHAT_CSS = (
    CITE_MARKS_CSS
    + r"""
.chat-drawer {
  position: fixed; bottom: var(--sp-4);
  right: calc(var(--sidebar-open-width, 0) + var(--sp-4));
  z-index: 95;
}
.chat-toggle { border-radius: var(--radius-full); box-shadow: var(--shadow-pop); }
.chat-toggle.open { background: var(--muted); }
.chat-toggle-icon { font-family: var(--mono); }
.chat-sidebar {
  flex-shrink: 0; width: 0; height: 100dvh; overflow: hidden;
  background: var(--surface); border-left: 0 solid var(--hairline);
  display: flex; flex-direction: column; position: sticky; top: 0;
}
.chat-sidebar.open {
  width: var(--sidebar-width); border-left-width: var(--bw-thin);
  box-shadow: var(--shadow-pop);
}
.chat-head {
  display: flex; align-items: flex-start; justify-content: space-between;
  gap: var(--sp-2); padding: var(--sp-3); border-bottom: var(--bw-thin) solid var(--hairline);
}
.chat-title { font-size: var(--fs-body); font-weight: 600; color: var(--fg); }
.chat-sub { font-size: var(--fs-caption); color: var(--muted); margin-top: var(--sp-1); font-family: var(--mono); }
.chat-close { margin-inline-start: auto; }
.chat-handoff {
  display: flex; flex-direction: column; align-items: flex-start;
  gap: var(--sp-3); margin: var(--sp-3);
}
@media (max-width: 47.5rem) {
  .chat-sidebar.open { width: 100%; }
  .chat-sidebar .k-btn { min-block-size: var(--touch-target-size); }
}
@media (prefers-reduced-motion: reduce) {
  .chat-sidebar, .chat-drawer { transition: none; animation: none; }
}
"""
)
COMMENTS_CSS = r"""
/* ============================================================
   Inline comments — pin markers + sidebar + form
   ============================================================ */
[data-commentable="true"] { position: relative; }
.cmt-pin-host {
  position: absolute; top: var(--sp-1); right: var(--sp-1);
  pointer-events: none; z-index: 5;
}
.cmt-pin {
  pointer-events: auto;
  display: inline-flex; align-items: center; justify-content: center;
  min-width: 20px; height: 20px; padding: 0 6px;
  font-family: var(--mono); font-size: var(--fs-caption); font-weight: 600;
  background: color-mix(in srgb, var(--fg) 4%, transparent);
  border: var(--bw-thin) solid var(--hairline);
  border-radius: 10px;
  color: var(--muted); cursor: pointer; opacity: 0.45;
  transition: opacity 0.15s, background 0.15s, border-color 0.15s;
}
[data-commentable="true"]:hover .cmt-pin { opacity: 1; }
.cmt-pin:hover { background: color-mix(in srgb, var(--fg) 10%, transparent); border-color: var(--muted); color: var(--fg); }
.cmt-pin.has-open { background: color-mix(in srgb, var(--warn) 18%, transparent); border-color: color-mix(in srgb, var(--warn) 55%, transparent); color: var(--warn); opacity: 1; }
.cmt-pin.all-addressed { background: color-mix(in srgb, var(--ok) 12%, transparent); border-color: color-mix(in srgb, var(--ok) 40%, transparent); color: var(--ok); opacity: 0.9; }

.cmt-sidebar {
  /* True push-sidebar: flex sibling to .l1-root */
  flex-shrink: 0;
  width: 0;
  height: 100vh;
  overflow: hidden;
  background: var(--surface);
  border-left: 0 solid var(--hairline);
  transition: width 0.2s ease, border-left-width 0s 0.2s;
  display: flex; flex-direction: column;
  position: sticky; top: 0;
}
.cmt-sidebar.open {
  width: 380px;
  border-left-width: var(--bw-thin);
  transition: width 0.2s ease, border-left-width 0s;
  box-shadow: var(--shadow-pop);
}
.cmt-sidebar-head {
  display: flex; align-items: flex-start; justify-content: space-between;
  padding: 14px var(--sp-4); border-bottom: var(--bw-thin) solid var(--hairline);
}
.cmt-sidebar-title { font-size: var(--fs-title); font-weight: 600; color: var(--fg); }
.cmt-sidebar-sub {
  font-size: var(--fs-caption); color: var(--muted); margin-top: var(--bw-thick);
  font-family: var(--mono);
}
.cmt-close {
  background: transparent; border: none; color: var(--muted);
  font-size: 20px; line-height: 1; padding: 0 6px; cursor: pointer;
}
.cmt-close:hover { color: var(--fg); }

/* Outbox status badge — shown in the sidebar header when the local queue is
   non-empty. Rides the control kit's .k-pill (k-pill-warn = "pending recovery",
   set in JS); this rule adds only layout (right-aligned) + its mono-micro refine.
   The warn tone fill lives on .k-pill-warn now, not here. */
.cmt-outbox-badge {
  margin: var(--bw-thick) var(--sp-2) 0 auto;
  font-size: var(--fs-caption);
  font-family: var(--mono);
  text-transform: uppercase; letter-spacing: 0.04em;
}

/* Health pill — server reachability indicator in the sidebar header. Rides the
   kit's .k-pill (+ k-pill-ok online / k-pill-bad offline / neutral bare while the
   first poll is in flight, tone set in renderHealthPill); layout + mono-micro
   refine only here. */
.cmt-health-pill {
  margin: var(--bw-thick) 6px 0 6px;
  font-size: var(--fs-caption);
  font-family: var(--mono);
  text-transform: uppercase; letter-spacing: 0.04em;
}

/* Offline banner inside the form — telegraphs the failure mode BEFORE
   the user submits, instead of after the click. */
.cmt-offline-banner {
  margin-bottom: var(--sp-2);
  padding: 6px 10px;
  font-size: var(--fs-caption);
  color: var(--warn);
  background: color-mix(in srgb, var(--warn) 10%, transparent);
  border: var(--bw-thin) solid color-mix(in srgb, var(--warn) 40%, transparent);
  border-radius: var(--radius);
  line-height: 1.4;
}
:root.chat-sidebar-open { --sidebar-open-width: var(--sidebar-width); }
:root.comments-sidebar-open { --sidebar-open-width: var(--comments-drawer-width); }

.cmt-list { flex: 1; overflow-y: auto; padding: var(--sp-3) 14px; }
.cmt-empty { color: var(--muted); font-size: var(--fs-caption); padding: var(--sp-2) 0; }
.cmt-card {
  background: var(--paper);
  border: var(--bw-thin) solid var(--hairline);
  border-radius: var(--radius);
  padding: 10px var(--sp-3);
  margin-bottom: 10px;
  font-size: var(--fs-body);
}
.cmt-card-head {
  display: flex; align-items: center; gap: 6px;
  font-size: var(--fs-caption); color: var(--muted);
  margin-bottom: 6px;
  text-transform: uppercase; letter-spacing: 0.04em;
}
.cmt-status { font-weight: 600; }
.cmt-status-open { color: var(--warn); }
.cmt-status-addressed { color: var(--ok); }
.cmt-status-dismissed { color: var(--muted); }
.cmt-intent { background: color-mix(in srgb, var(--fg) 5%, transparent); padding: var(--bw-thin) 6px; border-radius: 3px; }
.cmt-time { margin-left: auto; font-family: var(--mono); }
.cmt-body { color: var(--fg); line-height: 1.5; white-space: pre-wrap; }
.cmt-resolution {
  margin-top: var(--sp-2); padding: var(--sp-2) 10px;
  background: color-mix(in srgb, var(--ok) 8%, transparent); border-left: var(--bw-thick) solid var(--ok);
  border-radius: 3px; font-size: var(--fs-caption); color: var(--muted);
}
.cmt-thread { margin-top: var(--sp-2); padding-top: 6px; border-top: var(--bw-thin) dashed var(--hairline); }
.cmt-thread-turn { display: flex; gap: var(--sp-2); padding: var(--sp-1) 0; font-size: var(--fs-caption); }
.cmt-thread-role { font-family: var(--sans); color: var(--muted); width: 60px; flex-shrink: 0; }
.cmt-thread-text { color: var(--fg); }
.cmt-role-assistant .cmt-thread-role { color: var(--accent); }
/* Action buttons are the kit's quiet small button (.k-btn.k-btn-quiet.k-btn-sm,
   added in the JS markup); this layout-only rule keeps the row spacing. */
.cmt-actions { margin-top: var(--sp-2); display: flex; gap: 6px; }

.cmt-form { padding: var(--sp-3) 14px; border-top: var(--bw-thin) solid var(--hairline); background: var(--bg); }
/* Textarea/select: skinned by the shared control kit (ui/controls.py). */
.cmt-form textarea { width: 100%; box-sizing: border-box; resize: vertical; }
.cmt-form-row { display: flex; gap: var(--sp-2); margin-top: var(--sp-2); align-items: center; }
.cmt-form select { flex: 1; font-size: var(--fs-caption); }
/* The Post submit + Save-to-journal buttons are the kit's .k-btn primary/quiet
   (added in the boot.py markup) — no freehand button skin here. */
.cmt-form-hint { font-size: var(--fs-caption); color: var(--muted); margin-top: 6px; min-height: 14px; }

/* Floating "+ Comment" button on text selection (Google-Docs style). The
   button itself is the kit's small primary (.k-btn.k-btn-primary.k-btn-sm,
   added in the JS markup); only the floater's drop shadow + pill radius are
   surface-specific. */
.cmt-floater { position: absolute; z-index: 110; pointer-events: auto; }
.cmt-floater-btn {
  border-radius: var(--radius-full);
  box-shadow: var(--shadow-pop);
}

/* Free-text highlight (the underlined excerpt the user commented on). Open =
   warn-tinted, addressed = ok-tinted, both routed through the status tokens
   via color-mix (no raw rgba). */
mark.cmt-highlight {
  background: color-mix(in srgb, var(--warn) 18%, transparent);
  border-bottom: var(--bw-thick) solid var(--warn);
  color: inherit;
  cursor: pointer;
  padding: 0 var(--bw-thin);
  border-radius: var(--radius-sm);
  transition: background 0.12s;
}
mark.cmt-highlight:hover { background: color-mix(in srgb, var(--warn) 32%, transparent); }
mark.cmt-highlight.addressed {
  background: color-mix(in srgb, var(--ok) 14%, transparent);
  border-bottom-color: var(--ok);
}
mark.cmt-highlight.addressed:hover { background: color-mix(in srgb, var(--ok) 24%, transparent); }
"""
DCF_CSS = """
.dcf-edit { margin-top: var(--sp-3); border: var(--bw-thin) solid var(--border);
  border-radius: var(--radius); padding: var(--sp-3); background: var(--surface); }
.dcf-edit-toggle { font-size: var(--fs-caption); }
.dcf-edit-body { margin-top: var(--sp-3); }
.dcf-edit-status { font-size: var(--fs-caption); color: var(--muted);
  min-height: 1.2em; margin-bottom: var(--sp-2); }
.dcf-edit-status[data-tone="bad"] { color: var(--bad); }
.dcf-edit-status[data-tone="ok"] { color: var(--ok); }
.dcf-edit-status[data-tone="warn"] { color: var(--warn); }
.dcf-edit-cols { display: grid; grid-template-columns: minmax(0, 1.1fr) minmax(0, 1fr);
  gap: var(--sp-4); align-items: start; }
@media (max-width: 720px) { .dcf-edit-cols { grid-template-columns: 1fr; } }
.dcf-edit-group { margin-bottom: var(--sp-3); }
.dcf-edit-group-title { font-size: var(--fs-caption); font-weight: 600; color: var(--muted);
  text-transform: uppercase; letter-spacing: 0.05em; margin-bottom: var(--sp-2); }
.dcf-edit-fields { display: grid; grid-template-columns: repeat(auto-fill, minmax(132px, 1fr));
  gap: var(--sp-2); }
.dcf-edit-field { display: flex; flex-direction: column; gap: var(--bw-thick); }
.dcf-edit-field label { font-size: var(--fs-caption); color: var(--muted); }
.dcf-edit-field input, .dcf-edit-field select { font-size: var(--fs-caption);
  padding: 3px 6px; font-variant-numeric: tabular-nums; }
.dcf-edit-field.is-derived input { color: var(--muted); font-style: italic; }
.dcf-seg-grid { display: grid; grid-template-columns: minmax(0, 1fr) auto auto; gap: var(--sp-2);
  align-items: center; }
.dcf-seg-grid .dcf-seg-name { font-size: var(--fs-caption); color: var(--fg-soft);
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.dcf-seg-grid input { width: 5.5em; font-size: var(--fs-caption); padding: 3px 6px;
  font-variant-numeric: tabular-nums; }
.dcf-seg-head { font-size: var(--fs-caption); color: var(--muted); text-transform: uppercase;
  letter-spacing: 0.04em; text-align: center; }
.dcf-edit-scenarios { display: flex; gap: var(--sp-2); margin-bottom: var(--sp-3); }
.dcf-scn { flex: 1; border: var(--bw-thin) solid var(--border); border-radius: var(--radius);
  padding: var(--sp-2); text-align: center; }
.dcf-scn-label { font-size: var(--fs-caption); color: var(--muted); text-transform: uppercase;
  letter-spacing: 0.05em; }
.dcf-scn-val { font-size: var(--fs-body); font-weight: 600; color: var(--fg);
  font-variant-numeric: tabular-nums; }
.dcf-scn-up { font-size: var(--fs-caption); font-variant-numeric: tabular-nums; }
.dcf-scn-up[data-tone="positive"] { color: var(--ok); }
.dcf-scn-up[data-tone="negative"] { color: var(--bad); }
.dcf-scn-up[data-tone="muted"] { color: var(--muted); }
.dcf-scn[data-scenario="base"] { border-color: var(--accent); }
.dcf-heatmap { overflow-x: auto; }
.dcf-hm-cap { font-size: var(--fs-caption); color: var(--muted); margin-bottom: var(--sp-1); }
.dcf-hm-table { border-collapse: collapse; font-size: var(--fs-caption);
  font-variant-numeric: tabular-nums; }
.dcf-hm-table th, .dcf-hm-table td { padding: var(--sp-1) var(--sp-2); text-align: right;
  border: var(--bw-thin) solid var(--hairline); }
.dcf-hm-table th { color: var(--muted); font-weight: 600; }
.dcf-hm-table td.base { outline: var(--bw-thick) solid var(--accent); outline-offset: -2px; font-weight: 600; }
.dcf-hm-table td[data-tone="positive"][data-intensity="0"] { background: color-mix(in srgb, var(--ok) 8%, transparent); }
.dcf-hm-table td[data-tone="positive"][data-intensity="1"] { background: color-mix(in srgb, var(--ok) 18%, transparent); }
.dcf-hm-table td[data-tone="positive"][data-intensity="2"] { background: color-mix(in srgb, var(--ok) 28%, transparent); }
.dcf-hm-table td[data-tone="positive"][data-intensity="3"] { background: color-mix(in srgb, var(--ok) 38%, transparent); }
.dcf-hm-table td[data-tone="negative"][data-intensity="0"] { background: color-mix(in srgb, var(--bad) 8%, transparent); }
.dcf-hm-table td[data-tone="negative"][data-intensity="1"] { background: color-mix(in srgb, var(--bad) 18%, transparent); }
.dcf-hm-table td[data-tone="negative"][data-intensity="2"] { background: color-mix(in srgb, var(--bad) 28%, transparent); }
.dcf-hm-table td[data-tone="negative"][data-intensity="3"] { background: color-mix(in srgb, var(--bad) 38%, transparent); }
.dcf-hm-axis { font-size: var(--fs-caption); color: var(--muted); }
/* Wave 5: a captured report value injected into a driver flashes; the report's
   "-> DCF" affordance is a quiet accent micro-button. */
.dcf-edit-field input.dcf-injected { outline: var(--bw-thick) solid var(--accent); outline-offset: var(--bw-thin);
  background: color-mix(in srgb, var(--accent) 12%, transparent); }
.dcf-inject-btn { color: var(--accent); margin-left: var(--sp-1); white-space: nowrap; }
"""
READER_OVERRIDE_CSS = """
:host {
  display: block;
  min-height: 100%;
  color: var(--fg);
  background: var(--bg);
}
.work-os-report-content.k-doc {
  inline-size: 100%;
  max-inline-size: var(--main-max-width);
  margin-inline: auto;
  padding: var(--sp-5);
}
.l1-root {
  height: auto !important;
  min-height: 100%;
  overflow: visible !important;
  padding-bottom: var(--sp-6);
  background: transparent !important;
}
.tab-group-pane,
.tab-pane,
.subtab-pane {
  display: block !important;
}
.work-os-report-content [data-reader-group-active="false"],
.work-os-report-content [data-reader-section-active="false"] {
  display: none !important;
}
.tab-group-pane + .tab-group-pane {
  margin-top: var(--sp-6);
}
.work-os-report-content.k-doc .tab-group-pane,
.work-os-report-content.k-doc .tab-pane {
  max-inline-size: 100%;
}
.work-os-report-content.k-doc .tab-pane {
  padding-block: var(--sp-4);
  border-bottom: var(--bw-thin) solid var(--border);
}
.reader-group-title {
  margin: 0 0 var(--sp-4);
  padding-bottom: var(--sp-2);
  border-bottom: var(--bw-thin) solid var(--border);
  color: var(--fg);
  font-size: var(--fs-display);
}
.cmt-sidebar,
.chat-drawer,
.scrim {
  /* The transient Work OS reader owns comments/Ask in its outer toolbar. */
  display: none !important;
}
"""
# Keep the public base bundle focused on the document shell.  Component slices
# remain defined here (and are imported by their legacy modules) so every
# workspace visual decision has one owner without making the static scanner
# silently truncate the shell bundle.
CSS = _BASE_CSS
