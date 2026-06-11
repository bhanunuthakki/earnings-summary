"""CSS for the workspace HTML renderer.

Adapted from the Anthropic design bundle's ``workspace.css`` (paper-on-mono
editorial palette, three themes, two densities). Carries small additions for
the three tabs that the design itself doesn't cover (Company / Position /
Sources), so the rest of the brief's content has somewhere to render without
falling back to the legacy HTML doc.

Type/chrome (UI polish, 2026-06-11): this surface consumes the shared semantic
scale from ``src/ui/tokens.py`` for its **chrome tiers** — labels, captions,
eyebrows, tags, table headers, and other annotations ride ``--fs-caption`` /
``--fs-micro``; box corners ride the one ``--radius`` (pills ``--radius-full``);
overlays use ``--transition``. General section LABELS were de-mono'd to the
sans body (mono is reserved for tickers + numbers + code + locators), so the
eyebrows read as refined tracked-caps rather than terminal-ish.

Unlike the dashboards, this is an editorial *reading* surface, so its larger
type is deliberately surface-specific and is **left literal**, not forced onto
the dashboard scale: the reading-body ramp (12.5-14px prose / table cells /
panel titles) and the display ramp (15px lede -> 28px section title -> the 60px
identity ticker / 100px hero mark / big numeric readouts). One sanctioned
escape below the ``--fs-micro`` floor: the 8.5px per-number provenance chip
(``.src-chip`` — intentionally the smallest mark so it never crowds the number
it annotates).

Single string constant. The renderer inlines it inside a ``<style>`` tag so
the deliverable stays a single self-contained HTML file (matches the existing
``html.py`` renderer's contract).
"""

from __future__ import annotations

from ui.tokens import palette_css

CSS = (
    "\n/* ============================================================\n"
    "   Tokens — shared palette (single source: src/ui/tokens.py)\n"
    "   + workspace-local layout/density tokens.\n"
    "   3 themes (paper · white · dark) · 2 densities.\n"
    "   ============================================================ */\n"
    + palette_css("paper")
    + r"""
:root {
  --pad-x: 28px;
  --pad-y: 18px;
  --panel-pad-x: 18px;
  --panel-pad-y: 14px;
  --row-pad-y: 11px;
  --gap: 18px;
  --gap-lg: 24px;
  --section-gap: 28px;
  --kpi-pad: 22px;
  --table-pad-y: 10px;

  /* Aliases used by chat/comments modules */
  --bg-elev: var(--surface);
  --panel: var(--surface);
  --panel-alt: var(--paper);
  --ink: var(--fg);
  --ink-muted: var(--muted);
  --font-mono: var(--mono);
  --font-body: var(--sans);
}

:root[data-density="compact"] {
  --pad-x: 22px;
  --pad-y: 13px;
  --panel-pad-x: 14px;
  --panel-pad-y: 10px;
  --row-pad-y: 7px;
  --gap: 12px;
  --gap-lg: 16px;
  --section-gap: 20px;
  --kpi-pad: 16px;
  --table-pad-y: 6.5px;
}

* { box-sizing: border-box; }
html, body { margin: 0; padding: 0; background: var(--bg); color: var(--fg); height: 100%; }
body { font-family: var(--sans); font-size: 14px; line-height: 1.5; display: flex; flex-direction: row; align-items: stretch; overflow: hidden; }

.pos { color: var(--pos); }
.neg { color: var(--neg); }
.muted { color: var(--muted); }
.accent { color: var(--accent); }
.num { font-variant-numeric: tabular-nums; font-feature-settings: 'tnum'; }
.mono { font-family: var(--mono); }

/* ============================================================
   Primitives
   ============================================================ */
.badge {
  display: inline-flex; align-items: center; gap: 6px;
  padding: 3px 9px;
  border: 1px solid var(--border-2);
  border-radius: 999px;
  font-size: var(--fs-caption); font-weight: 500;
  color: var(--fg-soft);
  background: var(--surface);
  letter-spacing: 0.01em;
  white-space: nowrap;
}
.badge .dot { width: 6px; height: 6px; border-radius: 50%; }

.pill {
  display: inline-flex; align-items: center;
  padding: 2px 8px; border-radius: 3px;
  font-family: var(--mono); font-size: var(--fs-micro); font-weight: 500;
  letter-spacing: 0.04em;
  border: 1px solid var(--border-2);
  white-space: nowrap;
}
.pill-ok { color: var(--accent); border-color: var(--accent); background: var(--accent-soft); }
.pill-neutral { color: var(--fg); border-color: var(--fg); }
.pill-warn { color: var(--warn); border-color: var(--warn); background: rgba(185,124,0,0.08); }
.pill-bad { color: var(--bad); border-color: var(--bad); background: var(--tone-neg); }
.pill-muted { color: var(--muted-2); border-color: var(--border); }

/* P3.3 per-number source chips: <details class="src-pop"> wrapping a tiny
   tier-colored <summary class="src-chip"> badge; the open panel is an
   absolutely-positioned popover with the document identity + source link. */
.src-pop { display: inline-block; position: relative; vertical-align: baseline; }
.src-pop > summary { list-style: none; cursor: pointer; }
.src-pop > summary::-webkit-details-marker { display: none; }
.src-chip {
  display: inline-block; font-size: 8.5px; font-weight: 700;
  letter-spacing: 0.04em; line-height: 1.4; padding: 0 3px;
  border: 1px solid var(--border-2); border-radius: 3px;
  color: var(--muted-2); background: transparent;
  opacity: 0.65; user-select: none;
}
.src-chip:hover, .src-pop[open] .src-chip { opacity: 1; }
.src-sec-official { color: var(--ok); border-color: var(--ok); }
.src-fmp-normalized { color: var(--accent); border-color: var(--accent); }
.src-llm-extracted { color: var(--warn); border-color: var(--warn); }
.src-yfinance-fallback, .src-s1-provisional { color: var(--muted-2); }
.src-pop-body {
  position: absolute; z-index: 40; top: calc(100% + 4px); left: 0;
  min-width: 220px; max-width: 340px; padding: 8px 10px;
  background: var(--surface); border: 1px solid var(--border-2);
  border-radius: var(--radius); box-shadow: 0 6px 18px rgba(0,0,0,0.18);
  font-size: var(--fs-caption); text-align: left; white-space: normal;
}
.src-pop-row { padding: 1px 0; color: var(--fg); }
.src-pop-row.mono { font-family: var(--mono); font-size: var(--fs-micro); color: var(--muted); }
.src-pop-locator { word-break: break-all; }
.src-pop-row a { color: var(--accent); }

.ic-btn {
  appearance: none;
  border: 1px solid var(--border);
  background: var(--surface);
  width: 30px; height: 30px;
  border-radius: var(--radius);
  display: inline-flex; align-items: center; justify-content: center;
  cursor: pointer;
  color: var(--fg-soft);
  font-size: 14px;
}
.ic-btn:hover { color: var(--fg); border-color: var(--border-2); }

/* P4.1 empty-state anatomy: empty sections collapse to a single muted
   summary line (<details class="panel panel-empty">) that expands to an
   analyst-language explanation. Replaces the old full-height .stub blocks. */
.panel-empty .panel-title { color: var(--muted); font-weight: 500; }
.panel-empty-body {
  padding: 12px var(--panel-pad-x);
  font-size: 12.5px; color: var(--muted); line-height: 1.55;
  background: var(--paper);
}
.panel-budget { border-left: 3px solid var(--warn); }

/* ============================================================
   Workspace shell
   ============================================================ */
.l1-root {
  flex: 1;
  min-width: 0;
  height: 100vh;
  overflow-y: auto;
  background: var(--bg);
  color: var(--fg);
  font-family: var(--sans);
  display: flex; flex-direction: column;
  padding-bottom: 32px;
}

.l1-chrome {
  display: flex; align-items: center; justify-content: space-between;
  padding: 14px var(--pad-x);
  border-bottom: 1px solid var(--border);
  background: var(--surface);
}
.l1-chrome-left { display: flex; align-items: center; gap: 32px; }
.logo {
  font-family: var(--mono); font-weight: 600; font-size: 13.5px;
  letter-spacing: -0.01em; color: var(--fg);
}
.logo-sm { color: var(--muted-2); margin: 0 2px; }
.crumb {
  font-size: 12.5px; display: flex; align-items: center; gap: 8px;
  color: var(--fg);
}
.crumb-sep { color: var(--muted-2); }
.crumb-current { font-weight: 500; color: var(--fg); }
.l1-chrome-right { display: flex; align-items: center; gap: 10px; }

.l1-identity {
  display: flex; align-items: flex-end; justify-content: space-between;
  padding: 32px var(--pad-x) 24px;
  border-bottom: 1px solid var(--border);
  gap: 32px;
  flex-wrap: wrap;
}
.ticker-large {
  font-family: var(--mono); font-weight: 600; font-size: 60px;
  letter-spacing: -0.045em; line-height: 0.95;
  color: var(--fg);
}
.company-row {
  display: flex; align-items: center; gap: 12px; margin-top: 12px;
}
.company-name { font-size: 20px; font-weight: 500; }
.company-meta {
  font-size: 12.5px; color: var(--muted); margin-top: 6px;
  display: flex; align-items: center; gap: 8px; flex-wrap: wrap;
}
.meta-pip { color: var(--muted-2); }

.identity-right {
  display: flex; align-items: stretch; gap: 22px;
  flex-wrap: nowrap;
}
.val-stat { display: flex; flex-direction: column; gap: 8px; min-width: 92px; }
.val-stat-label {
  font-size: var(--fs-micro); color: var(--muted);
  text-transform: uppercase; letter-spacing: 0.08em;
}
.val-stat-value {
  font-family: var(--mono); font-size: 24px; font-weight: 500;
  font-variant-numeric: tabular-nums; letter-spacing: -0.015em;
  color: var(--fg);
}
.val-stat-value.mono-sm { font-size: 18px; }
.val-divider { width: 1px; background: var(--border); align-self: stretch; }

.l1-thesis {
  display: flex; gap: 22px;
  padding: 18px var(--pad-x);
  border-bottom: 1px solid var(--border);
  background: var(--paper);
}
.thesis-label {
  font-size: var(--fs-micro); font-weight: 600;
  letter-spacing: 0.1em; text-transform: uppercase;
  color: var(--muted); flex-shrink: 0; padding-top: 3px;
}
.l1-thesis p {
  margin: 0; font-size: 13.5px; line-height: 1.65;
  max-width: 1180px; color: var(--fg-soft);
}

.kpi-strip {
  display: grid; grid-template-columns: repeat(4, 1fr);
  gap: 1px;
  background: var(--border);
  border-bottom: 1px solid var(--border);
}
.kpi-tile {
  background: var(--surface);
  padding: var(--kpi-pad) calc(var(--kpi-pad) + 2px);
}
.kpi-name {
  font-size: var(--fs-caption); color: var(--muted); margin-bottom: 10px;
  min-height: 32px; line-height: 1.35;
}
.kpi-row { display: flex; align-items: baseline; gap: 10px; }
.kpi-value {
  font-family: var(--mono); font-size: 26px; font-weight: 500;
  font-variant-numeric: tabular-nums; letter-spacing: -0.015em;
}
.kpi-delta {
  font-family: var(--mono); font-size: var(--fs-caption); font-weight: 500;
}
.kpi-delta.pos { color: var(--accent); }
.kpi-delta.neg { color: var(--muted); }
.kpi-spark { margin-top: 10px; color: var(--accent); }
.kpi-axis {
  display: flex; justify-content: space-between; align-items: baseline;
  font-family: var(--mono); font-size: var(--fs-micro); color: var(--muted-2);
  margin-top: 6px;
}
.kpi-trail { color: var(--muted); }

.news-strip {
  padding: 22px var(--pad-x) 24px;
  border-bottom: 1px solid var(--border);
  background: var(--bg);
}
.news-h { display: flex; align-items: baseline; gap: 14px; margin-bottom: 14px; }
.news-eyebrow {
  font-size: var(--fs-micro); font-weight: 600;
  letter-spacing: 0.1em; text-transform: uppercase; color: var(--muted);
}
.news-sub { font-size: var(--fs-caption); color: var(--muted-2); }
.news-grid {
  display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
  gap: 14px;
}
.news-item {
  border: 1px solid var(--border);
  background: var(--surface);
  padding: 14px;
  border-radius: var(--radius);
  position: relative;
}
.news-item::before {
  content: ''; position: absolute; left: 0; top: 0; bottom: 0;
  width: 3px; border-radius: var(--radius) 0 0 6px;
}
.news-item.tone-pos::before { background: var(--accent); }
.news-item.tone-opt::before { background: var(--warn); }
.news-item.tone-neg::before { background: var(--bad); }
.news-item.tone-neu::before { background: var(--muted-2); }
.news-meta {
  display: flex; align-items: center; gap: 8px; flex-wrap: wrap;
  font-family: var(--mono); font-size: var(--fs-micro);
  margin-bottom: 8px;
}
.news-tag {
  font-weight: 600; letter-spacing: 0.06em;
  color: var(--accent);
}
.news-item.tone-opt .news-tag { color: var(--warn); }
.news-item.tone-neg .news-tag { color: var(--bad); }
.news-item.tone-neu .news-tag { color: var(--muted); }
.news-date { color: var(--muted-2); }
.news-src { color: var(--muted); font-family: var(--sans); font-size: var(--fs-caption); }
.news-headline {
  font-family: var(--sans); font-size: 13.5px;
  font-weight: 500; line-height: 1.4; margin: 0 0 8px;
  color: var(--fg);
}
.news-headline a { color: inherit; text-decoration: none; }
.news-headline a:hover { text-decoration: underline; }
.news-gloss {
  font-family: var(--serif); font-size: 12.5px;
  line-height: 1.5; color: var(--fg-soft); margin: 0;
}

[data-show-news="0"] .news-strip { display: none; }

/* ============================================================
   Tabs
   ============================================================ */
.l1-tabs-wrap { padding: var(--section-gap) var(--pad-x) 0; }
.tabs {
  display: flex; align-items: center; gap: 2px;
  border-bottom: 1px solid var(--border);
  flex-wrap: wrap;
}
.tab {
  appearance: none; border: 0; background: transparent;
  padding: 11px 16px 13px;
  font-family: var(--sans); font-size: 13.5px; font-weight: 500;
  color: var(--muted);
  cursor: pointer;
  border-bottom: 2px solid transparent;
  margin-bottom: -1px;
  display: flex; align-items: center; gap: 8px;
  letter-spacing: 0.005em;
}
.tab:hover { color: var(--fg-soft); }
.tab.active { color: var(--fg); border-bottom-color: var(--fg); }
.tab-count {
  font-family: var(--mono); font-size: var(--fs-micro); font-weight: 500;
  padding: 1px 6px; background: var(--paper);
  border-radius: 3px; color: var(--muted);
}
.tab.active .tab-count { background: var(--fg); color: var(--bg); }
.tabs-spacer { flex: 1; }
.tabs-meta { font-size: var(--fs-caption); padding-right: 4px; color: var(--muted); }
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
  display: flex; align-items: center; gap: 6px; flex-wrap: wrap;
  padding-top: var(--section-gap);
}
.subtab {
  appearance: none; border: 1px solid var(--border-2);
  background: var(--surface);
  padding: 5px 12px 6px;
  font-family: var(--sans); font-size: var(--fs-caption); font-weight: 500;
  color: var(--muted); cursor: pointer; border-radius: 999px;
  display: flex; align-items: center; gap: 7px;
  letter-spacing: 0.005em;
}
.subtab:hover { color: var(--fg); border-color: var(--fg); }
.subtab.active { color: var(--bg); background: var(--fg); border-color: var(--fg); }
.subtab-count {
  font-family: var(--mono); font-size: var(--fs-micro); font-weight: 500;
  padding: 0 5px; border-radius: 3px;
  background: var(--paper); color: var(--muted);
}
.subtab.active .subtab-count { background: var(--bg); color: var(--fg); }

.eyebrow {
  font-size: var(--fs-caption); font-weight: 500;
  letter-spacing: 0.1em; text-transform: uppercase;
  color: var(--muted); margin-bottom: 10px;
}
.section-title {
  font-family: var(--sans);
  font-size: 28px; font-weight: 500; letter-spacing: -0.025em;
  margin: 0 0 12px; line-height: 1.2;
  max-width: 820px;
  color: var(--fg);
}
.lede {
  font-size: 15px; color: var(--fg-soft); margin: 0;
  line-height: 1.6;
  /* No max-width — the thesis lede should fill the tab's content area so
     dense, structured theses don't wrap at awkwardly narrow column widths.
     Originally capped at 760px for legibility on wide monitors; that turned
     out to be too narrow for theses that name multiple break rules + KPI
     tiers inline. The parent panel still provides outer padding. */
}
.row-split {
  display: flex; justify-content: space-between; align-items: flex-start;
  gap: 24px; flex-wrap: wrap;
}

/* Hero quote (Earnings tab) */
.hero-quote {
  border: 1px solid var(--border-2);
  background: linear-gradient(180deg, var(--paper), var(--surface));
  padding: 28px 32px 26px;
  border-radius: var(--radius);
  display: flex; gap: 22px;
  position: relative;
}
.hero-quote-mark {
  font-family: var(--serif); font-size: 100px; font-weight: 600;
  line-height: 0.7; color: var(--accent);
  flex-shrink: 0;
}
.hero-quote blockquote { margin: 0; flex: 1; }
.hero-quote blockquote p {
  font-family: var(--serif); font-size: 22px; line-height: 1.4;
  font-weight: 400; margin: 0 0 12px;
  letter-spacing: -0.005em;
  color: var(--fg);
}
.hero-quote footer {
  font-size: 12.5px; color: var(--muted);
  display: flex; gap: 8px;
}
.hero-speaker { color: var(--fg); font-weight: 500; }
.hero-role { color: var(--muted); }

.quarter-select { display: flex; flex-direction: column; gap: 8px; flex-shrink: 0; padding-top: 4px; }
.quarter-select-label {
  font-size: var(--fs-micro); font-weight: 500;
  letter-spacing: 0.08em; text-transform: uppercase;
  color: var(--muted); text-align: right;
}
.quarter-select-btns { display: flex; gap: 4px; flex-wrap: wrap; justify-content: flex-end; }
.qbtn {
  appearance: none; border: 1px solid var(--border-2);
  background: var(--surface);
  padding: 7px 13px;
  font-family: var(--mono); font-size: var(--fs-caption); font-weight: 500;
  color: var(--muted); cursor: pointer; border-radius: var(--radius);
}
.qbtn:hover { color: var(--fg); border-color: var(--fg); }
.qbtn.active { color: var(--bg); background: var(--fg); border-color: var(--fg); }

/* ============================================================
   Panels
   ============================================================ */
.panel {
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: var(--surface);
  overflow: hidden;
}
.panel-head {
  display: flex; align-items: baseline; justify-content: space-between;
  padding: var(--panel-pad-y) var(--panel-pad-x);
  border-bottom: 1px solid var(--hairline);
  gap: 12px;
}
.panel-title {
  font-size: 13.5px; font-weight: 600; letter-spacing: 0.005em;
  color: var(--fg);
}
.panel-sub { font-size: var(--fs-caption); color: var(--muted); }
/* P4.1 canonical header anatomy: title (left) · as-of · source chip · sub
   (right edge, grouped in .panel-meta). Built by workspace_html._panel_head —
   hand-rolled heads should not exist outside that helper. */
.panel-meta {
  display: flex; align-items: baseline; gap: 10px;
  min-width: 0; text-align: right;
}
.panel-asof {
  font-family: var(--mono); font-size: var(--fs-micro); color: var(--muted-2);
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
.xlink-flash { outline: 2px solid var(--accent); outline-offset: 2px; }

/* P4.4 — the open watch-items strip under the thesis lede: every build
   leads with what the owner already said to watch for. */
.l1-open-items { margin: 0 var(--pad-x) 8px; }
.oi-strip-list { list-style: none; margin: 0; padding: 6px var(--panel-pad-x) 10px; }
.oi-strip-list li {
  display: flex; gap: 8px; align-items: baseline;
  padding: 4px 0; font-size: 12.5px;
  border-bottom: 1px solid var(--hairline);
}
.oi-strip-list li:last-child { border-bottom: 0; }
.oi-kind {
  font-size: var(--fs-micro); font-weight: 600;
  text-transform: uppercase; letter-spacing: 0.05em;
  color: var(--accent); border: 1px solid var(--accent);
  border-radius: 3px; padding: 0 5px; flex: none;
}
.oi-body { flex: 1; color: var(--fg-soft); }
.oi-when { color: var(--muted); font-family: var(--mono); font-size: var(--fs-micro); flex: none; }
.panel-foot {
  padding: 12px var(--panel-pad-x);
  background: var(--paper);
  border-top: 1px solid var(--hairline);
  font-size: var(--fs-caption); color: var(--fg-soft);
  display: flex; gap: 10px; align-items: flex-start; line-height: 1.5;
}
.flag { color: var(--warn); font-size: 14px; }
.signals-fires {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(260px, 1fr));
  gap: 10px;
  padding: 14px var(--panel-pad-x);
}
/* §3.5 signal cards — severity via token-backed classes (P6.1; these
   previously carried hardcoded inline rgba colors + a literal font stack). */
.signal-card {
  border: 1px solid var(--hairline);
  border-left: 3px solid var(--hairline);
  border-radius: var(--radius); padding: 10px 12px; font-size: 12.5px;
}
.signal-card.sig-red { border-left-color: var(--bad); background: var(--tone-neg); }
.signal-card.sig-yellow { border-left-color: var(--warn); background: var(--tone-opt); }
.signal-card.sig-green { border-left-color: var(--ok); background: var(--tone-pos); }
.signal-card-head {
  display: flex; justify-content: space-between;
  gap: 8px; align-items: baseline; margin-bottom: 4px;
}
.signal-card-metric { font-weight: 600; }
.signal-card-type {
  font-size: var(--fs-micro); text-transform: uppercase;
  letter-spacing: 0.4px; color: var(--muted);
}
.signal-card-narrative { line-height: 1.45; margin: 4px 0 6px; }
.signal-card-stat { font-family: var(--mono); font-size: var(--fs-caption); color: var(--muted); }
.signal-sev { font-weight: 600; color: var(--muted); }
.signal-sev.sig-red { color: var(--bad); }
.signal-sev.sig-yellow { color: var(--warn); }
.signal-sev.sig-green { color: var(--ok); }

.signals-all { padding: 0 var(--panel-pad-x) 14px; }
.signals-all > summary {
  cursor: pointer; font-size: 12.5px; color: var(--muted);
  padding: 8px 0; list-style: none;
}
.signals-all > summary::-webkit-details-marker { display: none; }
.signals-all > summary::before { content: '▸ '; }
.signals-all[open] > summary::before { content: '▾ '; }
.prose-pad {
  padding: 16px var(--panel-pad-x) 18px;
  font-size: 13.5px; line-height: 1.6; color: var(--fg-soft);
}
.prose-pad p { margin: 0 0 10px; }
.prose-pad p:last-child { margin-bottom: 0; }
.prose-pad ul, .prose-pad ol { margin: 0 0 10px; padding-left: 22px; }
.prose-pad li { margin-bottom: 4px; }

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
   only their table-specific rules below). */
.tbl { width: 100%; border-collapse: collapse; font-size: 13px; }
.tbl th {
  text-align: left; font-size: var(--fs-micro); font-weight: 500;
  color: var(--muted); padding: 10px var(--panel-pad-x);
  text-transform: uppercase; letter-spacing: 0.06em;
  border-bottom: 1px solid var(--hairline);
  white-space: nowrap;
}
.tbl th.num, .tbl td.num {
  text-align: right;
  font-family: var(--mono); font-variant-numeric: tabular-nums;
}
.tbl td {
  padding: var(--table-pad-y) var(--panel-pad-x);
  border-bottom: 1px solid var(--hairline);
  vertical-align: top;
}
.tbl tr:last-child td { border-bottom: 0; }
.tbl tr:hover td { background: var(--paper); }
.tbl tr.emph td { background: var(--paper); font-weight: 500; }
.tbl-nowrap td { white-space: nowrap; }
.tbl .table-footer {
  text-align: center;
  padding: 8px var(--panel-pad-x);
  background: var(--paper);
  color: var(--muted);
  font-size: var(--fs-caption);
  font-style: italic;
}

/* Q&A rows: <details class="qa-row"> per question (P4.1 — the canonical
   <details> collapse idiom; the +/- chevron is CSS-driven). */
.qa-list { display: flex; flex-direction: column; }
.qa-row { border-top: 1px solid var(--hairline); }
.qa-row:first-child { border-top: 0; }
.qa-row[open] { background: var(--paper); }
.qa-head {
  width: 100%;
  display: flex; align-items: center; gap: 14px;
  padding: 13px var(--panel-pad-x);
  text-align: left; cursor: pointer; user-select: none; list-style: none;
}
.qa-head::-webkit-details-marker { display: none; }
.qa-head:hover { background: var(--paper); }
.qa-chev {
  font-family: var(--mono); width: 14px; color: var(--muted);
  font-size: 16px; font-weight: 400;
}
.qa-chev::before { content: '+'; }
.qa-row[open] .qa-chev::before { content: '-'; }
.qa-tag {
  font-size: var(--fs-micro); font-weight: 600;
  padding: 3px 7px; border: 1px solid var(--accent);
  color: var(--accent); background: var(--accent-soft);
  border-radius: 3px;
  letter-spacing: 0.06em; min-width: 70px; text-align: center;
}
.qa-topic { font-size: 13.5px; font-weight: 500; flex: 1; color: var(--fg); }
.qa-analysts { font-size: var(--fs-caption); color: var(--muted); font-style: italic; max-width: 280px; text-align: right; }
.qa-ref { font-family: var(--mono); font-size: var(--fs-micro); min-width: 38px; text-align: right; color: var(--muted-2); }
.qa-body {
  padding: 0 var(--panel-pad-x) 18px 50px;
  display: flex;
  flex-direction: column; gap: 12px;
}
.qa-q, .qa-a, .qa-followup { display: flex; gap: 12px; font-size: 13.5px; line-height: 1.55; }
.qa-q { color: var(--fg-soft); }
.qa-a { color: var(--fg); }
.qa-followup {
  font-size: 12.5px; color: var(--muted);
  padding-top: 8px; border-top: 1px dashed var(--border);
}
.qa-label {
  font-size: var(--fs-micro); font-weight: 600;
  color: var(--accent); flex-shrink: 0; width: 14px; padding-top: 2px;
}
.qa-followup-label {
  font-size: var(--fs-micro); font-weight: 600;
  letter-spacing: 0.08em; color: var(--muted);
  text-transform: uppercase; flex-shrink: 0; padding-top: 3px;
  width: 64px;
}

/* Say-Do */
.saydo-meta { display: flex; flex-direction: column; gap: 10px; align-items: flex-end; padding-top: 4px; }
.saydo-meta-label {
  font-size: var(--fs-caption); color: var(--muted);
  text-transform: uppercase; letter-spacing: 0.06em;
}
.saydo-history {
  display: flex; flex-direction: column; gap: 6px;
  align-items: flex-end;
}
.saydo-metric { font-weight: 500; }
.saydo-guide { font-family: var(--serif); font-style: italic; font-size: 13px; color: var(--muted); }
.saydo-actual { font-family: var(--mono); font-size: 12.5px; }

/* Financials */
.chart-panel { padding-bottom: var(--panel-pad-y); }
.chart-wrap {
  padding: 6px var(--panel-pad-x);
  color: var(--fg-soft);
  overflow-x: auto;
}
.legend {
  display: flex; flex-wrap: wrap; gap: 14px;
  padding: 10px var(--panel-pad-x) 4px;
}
.legend-item {
  display: flex; align-items: center; gap: 7px;
  font-size: var(--fs-caption); color: var(--fg-soft);
}
.legend-swatch { width: 10px; height: 10px; border-radius: 2px; }
.overlay-stat {
  position: absolute; top: 14px; right: 30px;
  display: flex; flex-direction: column; align-items: flex-end;
}
.overlay-stat span {
  font-size: var(--fs-micro); color: var(--muted);
  text-transform: uppercase; letter-spacing: 0.06em;
}
.overlay-stat strong {
  font-family: var(--mono); font-size: 22px;
  font-weight: 500; color: var(--fg); margin-top: 2px;
}
.overlay-stat .pos { font-family: var(--mono); font-size: var(--fs-caption); color: var(--accent); }

.table-scroll { overflow-x: auto; }

/* Thesis */
.val-stack { display: flex; flex-direction: column; }
.val-row {
  display: flex; justify-content: space-between; align-items: baseline;
  padding: 12px var(--panel-pad-x);
  border-bottom: 1px solid var(--hairline);
  font-size: 13px;
}
.val-row:last-child { border-bottom: 0; }
.val-row strong { font-family: var(--mono); font-weight: 500; font-size: 14px; }
.val-row.emph { background: var(--paper); }
.val-row.emph strong { font-size: 16px; font-weight: 600; }
.val-row.muted { color: var(--muted); }
.val-row.muted strong { color: var(--muted); }

.break-status-ok { color: var(--accent); }
.break-status-warn { color: var(--warn); }
.break-status-breach { color: var(--bad); }
.break-status-unresolved { color: var(--muted); }

.failure { display: flex; gap: 14px; padding: 14px var(--panel-pad-x); border-bottom: 1px solid var(--hairline); }
.failure:last-child { border-bottom: 0; }
.failure-num {
  font-family: var(--mono); font-size: var(--fs-caption); font-weight: 600;
  color: var(--accent); width: 22px; flex-shrink: 0; padding-top: 2px;
}
.failure-body { flex: 1; }
.failure-title {
  font-size: 13.5px; font-weight: 500; line-height: 1.5;
  margin-bottom: 8px; color: var(--fg);
}
.failure-meta {
  display: grid; grid-template-columns: 110px 1fr; gap: 6px 12px;
  font-size: var(--fs-caption); color: var(--fg-soft); line-height: 1.55;
  margin-top: 6px;
}
.failure-label {
  font-size: var(--fs-micro); font-weight: 600;
  letter-spacing: 0.06em; color: var(--accent);
  text-transform: uppercase;
  padding-top: 1px;
}

/* ============================================================
   Company tab
   ============================================================ */
.elevator-block {
  border: 1px solid var(--border-2);
  background: linear-gradient(180deg, var(--paper), var(--surface));
  padding: 22px 26px;
  border-radius: var(--radius);
  font-family: var(--serif);
  font-size: 17px;
  line-height: 1.55;
  color: var(--fg);
}
.elevator-block::before {
  content: 'Elevator pitch';
  display: block;
  font-size: var(--fs-micro); font-weight: 600;
  letter-spacing: 0.1em; text-transform: uppercase;
  color: var(--muted); margin-bottom: 10px;
}
.seg-desc { color: var(--fg-soft); font-size: 12.5px; }
.seg-bar {
  display: inline-block; height: 6px; vertical-align: middle;
  background: var(--accent); border-radius: 2px; min-width: 1px;
}

.ir-card {
  padding: 12px var(--panel-pad-x);
  border-bottom: 1px solid var(--hairline);
}
.ir-card:last-child { border-bottom: 0; }
.ir-card-head {
  display: flex; align-items: center; gap: 10px;
  margin-bottom: 6px;
}
.ir-type {
  font-size: var(--fs-micro); font-weight: 600;
  padding: 3px 7px; border-radius: 3px;
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
  font-size: 12.5px; color: var(--fg-soft); line-height: 1.5;
  margin: 0;
}

/* ============================================================
   Position tab
   ============================================================ */
.position-stats {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
  gap: 1px;
  background: var(--border);
  border-radius: var(--radius);
  overflow: hidden;
  border: 1px solid var(--border);
}
.position-stat {
  background: var(--surface);
  padding: 16px 18px;
}
.position-stat-label {
  font-size: var(--fs-micro); color: var(--muted);
  text-transform: uppercase; letter-spacing: 0.08em;
  margin-bottom: 8px;
}
.position-stat-value {
  font-family: var(--mono); font-size: 20px; font-weight: 500;
  font-variant-numeric: tabular-nums; letter-spacing: -0.01em;
}
.position-stat-sub {
  font-family: var(--mono); font-size: var(--fs-caption);
  margin-top: 4px; color: var(--muted);
}
.decision-card {
  padding: 14px var(--panel-pad-x);
  border-bottom: 1px solid var(--hairline);
}
.decision-card:last-child { border-bottom: 0; }
.decision-head {
  display: flex; align-items: baseline; gap: 10px;
  margin-bottom: 6px; flex-wrap: wrap;
}
.decision-date {
  font-family: var(--mono); font-size: var(--fs-caption); color: var(--muted);
}
.decision-action {
  font-size: var(--fs-caption); font-weight: 600;
  padding: 2px 7px; border-radius: 3px;
  background: var(--accent-soft); color: var(--accent);
  letter-spacing: 0.06em; text-transform: uppercase;
}
.decision-confidence {
  font-size: var(--fs-caption); color: var(--muted); font-style: italic;
}
.decision-outcome {
  font-size: var(--fs-micro); font-weight: 600;
  padding: 2px 6px; border-radius: 3px;
  letter-spacing: 0.06em; text-transform: uppercase;
}
.decision-outcome.validated { color: var(--accent); background: var(--accent-soft); }
.decision-outcome.invalidated { color: var(--bad); background: var(--tone-neg); }
.decision-outcome.partial { color: var(--warn); background: var(--tone-opt); }
.decision-thesis {
  font-size: 12.5px; color: var(--fg-soft); line-height: 1.55;
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
  border-top: 1px solid var(--hairline);
}
.transcript-block:first-child { border-top: 0; }
.transcript-block summary {
  padding: 14px var(--panel-pad-x);
  cursor: pointer; user-select: none;
  font-size: 13px; font-weight: 500;
  display: flex; align-items: center; gap: 10px;
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
  padding: 12px var(--panel-pad-x) 22px;
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
  padding-top: 22px;
  border-top: 1px solid var(--border);
}
.peer-head {
  display: flex; justify-content: space-between; align-items: baseline;
  padding-bottom: 14px;
  font-size: var(--fs-caption); color: var(--muted);
  flex-wrap: wrap; gap: 10px;
}
.peer-eyebrow {
  font-size: var(--fs-caption); font-weight: 600;
  letter-spacing: 0.1em; text-transform: uppercase;
  color: var(--muted); margin-right: 14px;
}
.peer-eyebrow-sub { color: var(--muted-2); }
.peer-grid {
  display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
  gap: 1px; background: var(--border);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  overflow: hidden;
}
.peer {
  appearance: none; border: 0;
  background: var(--surface);
  padding: 14px 14px;
  display: flex; flex-direction: column; gap: 8px;
  text-align: left; font: inherit;
  color: inherit; text-decoration: none;
}
.peer:hover { background: var(--paper); }
.peer.active { background: var(--fg); color: var(--bg); }
.peer-top {
  display: flex; justify-content: space-between; align-items: center;
}
.peer-ticker {
  font-family: var(--mono); font-weight: 700; font-size: 14px;
  letter-spacing: -0.005em;
}
.peer-name { font-size: var(--fs-caption); color: var(--muted); }
.peer.active .peer-name { color: var(--muted-2); }

/* ============================================================
   Footer
   ============================================================ */
.l1-footer {
  display: flex; justify-content: space-between; gap: 16px;
  padding: 28px var(--pad-x) 0;
  margin-top: 32px;
  border-top: 1px solid var(--border);
  font-size: var(--fs-caption); color: var(--fg-soft);
  align-items: baseline;
  flex-wrap: wrap;
}

/* ============================================================
   Tweaks panel
   ============================================================ */
.twk-toggle-btn {
  appearance: none;
  border: 1px solid var(--border);
  background: var(--surface);
  width: 30px; height: 30px;
  border-radius: var(--radius);
  cursor: pointer;
  color: var(--fg-soft);
  font-size: 14px;
}
.twk-toggle-btn:hover { color: var(--fg); border-color: var(--border-2); }

.twk-panel {
  position: fixed; right: 16px; bottom: 16px; z-index: 2147483646;
  width: 280px;
  display: none;
  flex-direction: column;
  background: var(--surface);
  color: var(--fg);
  border: 1px solid var(--border-2);
  border-radius: var(--radius-full);
  box-shadow: 0 12px 40px rgba(0,0,0,.16);
  font-family: var(--sans); font-size: 12.5px;
  overflow: hidden;
}
.twk-panel.open { display: flex; }
.twk-hd {
  display: flex; align-items: center; justify-content: space-between;
  padding: 10px 8px 10px 14px;
}
.twk-hd b { font-size: var(--fs-caption); font-weight: 600; letter-spacing: .01em; }
.twk-x {
  appearance: none; border: 0; background: transparent;
  color: var(--muted); width: 24px; height: 24px;
  border-radius: var(--radius); cursor: pointer; font-size: 13px;
}
.twk-x:hover { background: var(--paper); color: var(--fg); }
.twk-body {
  padding: 4px 14px 14px;
  display: flex; flex-direction: column; gap: 12px;
}
.twk-sect {
  font-size: var(--fs-micro); font-weight: 600;
  letter-spacing: .08em; text-transform: uppercase;
  color: var(--muted); padding-top: 6px;
}
.twk-sect:first-child { padding-top: 0; }
.twk-row { display: flex; flex-direction: column; gap: 6px; }
.twk-row-h {
  flex-direction: row; align-items: center; justify-content: space-between;
}
.twk-lbl { color: var(--fg-soft); font-weight: 500; }
.twk-seg {
  display: flex; padding: 2px; border-radius: 7px;
  background: var(--paper);
  border: 1px solid var(--border);
}
.twk-seg button {
  appearance: none; border: 0; background: transparent;
  flex: 1; padding: 5px 8px; border-radius: 5px;
  font: inherit; font-weight: 500; color: var(--muted);
  cursor: pointer;
}
.twk-seg button.active {
  background: var(--surface); color: var(--fg);
  box-shadow: 0 1px 2px rgba(0,0,0,.06);
}
.twk-toggle {
  position: relative; width: 32px; height: 18px;
  border: 0; border-radius: 999px;
  background: var(--border-2); cursor: pointer; padding: 0;
}
.twk-toggle[data-on="1"] { background: var(--accent); }
.twk-toggle::after {
  content: ''; position: absolute; top: 2px; left: 2px;
  width: 14px; height: 14px; border-radius: 50%;
  background: var(--surface);
  box-shadow: 0 1px 2px rgba(0,0,0,.25);
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
  font-family: var(--mono); font-size: var(--fs-micro);
  color: var(--muted);
}
.fin-drill td.fin-drill-cell {
  padding: 0 !important;
  background: var(--paper);
}
.fin-drill-table {
  width: 100%;
  margin: 0;
  background: var(--paper);
}
.fin-drill-table th, .fin-drill-table td {
  border: 0 !important;
  border-bottom: 1px solid var(--hairline) !important;
  padding: 8px 14px !important;
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
  color: var(--muted-2) !important;
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
  padding: 12px var(--panel-pad-x) 14px 36px;
  font-size: 12.5px;
  line-height: 1.55;
  color: var(--fg-soft);
}
.thesis-list li { margin-bottom: 6px; }
.thesis-list li:last-child { margin-bottom: 0; }

/* Enriched §2 KPI ledger rows: a clean name with the qualifier demoted to a
   muted definition line, a sparkline + YoY/QoQ delta trend cell, a staleness
   flag, and the "tracked, no data yet" footnote. */
.kpi-ledger-table td:first-child { white-space: normal; max-width: 300px; }
.kpi-ledger-row td { vertical-align: top; }
.ledger-def { margin-top: 3px; font-weight: 400; white-space: normal; max-width: 300px; }
.ledger-trend { white-space: nowrap; }
.ledger-spark { color: var(--accent); display: inline-block; vertical-align: middle; }
.ledger-spark svg { display: inline-block; vertical-align: middle; }
.ledger-delta {
  font-family: var(--mono); font-size: var(--fs-micro); color: var(--muted); white-space: nowrap;
}
.ledger-stale {
  margin-left: 6px; font-size: var(--fs-micro); font-weight: 600; text-transform: uppercase;
  letter-spacing: 0.04em; color: var(--warn);
  border: 1px solid var(--warn); border-radius: 3px; padding: 0 4px;
}
/* Dim a stale row's data but keep the flag itself at full strength. */
.ledger-stale-row td { opacity: 0.55; }
.ledger-stale-row .ledger-stale { opacity: 1; }
.ledger-tracked-only {
  padding: 9px var(--panel-pad-x) 12px;
  border-top: 1px dashed var(--hairline);
}
.ledger-tracked-only strong { color: var(--fg); }

.underweighted-panel { border-color: var(--warn); }
.underweighted-panel .panel-title { color: var(--warn); }

/* News tab: per-section sub-panels use a slightly tighter grid than the
   page-level news strip used to. */
.news-grid-tab {
  padding: 12px var(--panel-pad-x) 14px;
  grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
}

/* Peer-row tile variants by list type — quiet borders that hint at
   portfolio / watchlist / evaluation membership without shouting. */
.peer-portfolio { border-left: 3px solid var(--accent); }
.peer-watchlist { border-left: 3px solid var(--muted-2); }
.peer-evaluation { border-left: 3px solid var(--warn); }
.peer-grid {
  grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
}
a.peer { color: inherit; text-decoration: none; }
a.peer:hover { background: var(--paper); }

/* Misc tiny helpers used by the higher-density panels. */
.xsmall { font-size: var(--fs-micro); line-height: 1.4; }
.stub-warning {
  background: var(--tone-neg);
  border-left: 3px solid var(--bad);
  padding: 10px 14px;
  border-radius: var(--radius);
  font-size: 12.5px;
  color: var(--fg);
}
/* Break-rules detail row — appears under each main rule row when narrative /
   detail / observations are present. Quieter background so the parent row
   reads first. */
.break-detail td {
  padding: 4px var(--panel-pad-x) 10px !important;
  border-top: 0 !important;
  background: var(--paper);
  color: var(--muted);
}
.break-row td {
  vertical-align: top;
}
.break-row .muted.xsmall {
  display: block;
  margin-top: 4px;
  font-size: var(--fs-micro);
  font-weight: 400;
  line-height: 1.4;
}

/* Position tab — decision brief link + outcome notes block. */
.decision-brief-link {
  font-size: var(--fs-caption);
  color: var(--accent);
  text-decoration: none;
  font-family: var(--mono);
  padding: 1px 6px;
  border: 1px solid var(--accent);
  border-radius: 3px;
}
.decision-brief-link:hover { background: var(--accent-soft); }
.decision-outcome-block {
  margin-top: 6px;
  padding: 6px 8px;
  background: var(--paper);
  border-radius: var(--radius);
  line-height: 1.45;
}

/* ============================================================
   Valuation tab
   ============================================================ */
.valuation-headline { padding: 14px 16px; }
.valuation-headline-row {
  display: flex; align-items: baseline; gap: 28px; padding: 8px 0 14px;
  border-bottom: 1px dashed var(--hairline); flex-wrap: wrap;
}
.valuation-current { display: flex; flex-direction: column; gap: 2px; }
.valuation-current-value {
  font-family: var(--font-mono); font-size: 36px; font-weight: 600;
  letter-spacing: -0.02em; color: var(--ink);
}
.valuation-current-label { font-size: var(--fs-caption); color: var(--muted); text-transform: uppercase; letter-spacing: 0.06em; }
.valuation-band { display: flex; flex-direction: column; gap: 2px; font-size: 12.5px; color: var(--ink-muted); }
.valuation-band-row { display: flex; gap: 6px; }
.valuation-band-row .mono { font-family: var(--font-mono); color: var(--ink); }
.valuation-peg { display: flex; flex-direction: column; gap: 2px; }
.valuation-peg-value {
  font-family: var(--font-mono); font-size: 22px; font-weight: 600;
  letter-spacing: -0.01em; color: var(--ink);
}
.valuation-peg-label { font-size: var(--fs-caption); color: var(--muted); text-transform: uppercase; letter-spacing: 0.06em; }
.valuation-peg-sub { font-size: var(--fs-caption); color: var(--ink-muted); font-family: var(--font-mono); }
.valuation-verdict {
  margin-left: auto; padding: 6px 10px;
  background: var(--panel-alt); border: 1px solid var(--hairline); border-radius: var(--radius);
  font-size: var(--fs-caption); color: var(--ink); font-style: italic;
}
.valuation-spark { padding: 12px 0 4px; }
.valuation-spark-axis {
  display: flex; justify-content: space-between; font-size: var(--fs-caption);
  color: var(--muted); padding-bottom: 6px;
}

/* ============================================================
   Thesis lede prominence (top of report, below identity)
   ============================================================ */
.l1-thesis {
  /* Align side margins to the shell's standard inset so the thesis card
     spans the full content width (matched edges with the KPI strip / tabs
     below it) instead of inset further. */
  margin: 0 var(--pad-x) 8px;
  padding: 12px 16px;
  background: var(--panel);
  border-left: 3px solid var(--accent, var(--ok));
  border-radius: var(--radius);
}
.l1-thesis .thesis-label {
  font-size: var(--fs-micro); text-transform: uppercase; letter-spacing: 0.08em;
  color: var(--accent, var(--ok)); font-weight: 600; margin-right: 10px;
}
.l1-thesis p {
  margin: 4px 0 0; font-family: var(--font-serif, var(--font-body));
  font-size: 14px; line-height: 1.55; color: var(--ink);
  /* Override the 1180px cap from the earlier .l1-thesis p rule — the thesis
     paragraph should span the full available width of the workspace shell
     so dense theses don't break across short, awkward lines. */
  max-width: none;
}

/* ============================================================
   Print
   ============================================================ */
@media print {
  .l1-chrome, .twk-toggle-btn, .twk-panel { display: none; }
  .tab-group-pane { display: block !important; }
  .tab-pane { display: block !important; padding-top: 16px; break-before: page; }
  .tabs, .subtabs { display: none; }
  .news-strip, .kpi-strip, .l1-identity { break-after: avoid; }
}

/* ============================================================
   Phase 5 — Executive Compensation tab
   ============================================================ */
.ceo-pill {
  display: inline-block;
  font-size: var(--fs-micro);
  font-family: var(--mono);
  background: var(--accent);
  color: var(--surface);
  padding: 1px 5px;
  border-radius: 3px;
  margin-left: 6px;
  vertical-align: middle;
  letter-spacing: 0.5px;
}
.kpi-match { color: var(--ok); font-weight: 500; }

.insider-table tr.tx-buy { background: rgba(58, 138, 58, 0.06); }
.insider-table tr.tx-sell { background: rgba(176, 64, 64, 0.04); }
.insider-table td.signal-strong { color: var(--ok); font-weight: 600; }
.insider-table td.signal-medium { color: var(--accent); font-weight: 500; }
.insider-table td.signal-weak { color: var(--muted); }

ul.flag-list {
  list-style: none;
  padding: 12px;
  margin: 0;
  display: flex;
  flex-direction: column;
  gap: 8px;
}
ul.flag-list li {
  padding: 8px 12px;
  border-left: 3px solid;
  background: var(--paper);
  font-size: 13px;
  line-height: 1.5;
}
ul.flag-list li.flag-warn { border-left-color: var(--warn); }
ul.flag-list li.flag-positive {
  border-left-color: var(--ok);
  background: rgba(58, 138, 58, 0.06);
}

/* ============================================================
   Synthesis tab — lens artifact panels
   ============================================================ */
.lens-panel { margin-bottom: 16px; }
.lens-panel .panel-head {
  display: flex;
  flex-direction: column;
  gap: 2px;
  padding-bottom: 4px;
}
.lens-panel .lens-body { padding: 14px 18px 18px; }
.lens-warn {
  display: inline-block;
  font-size: var(--fs-micro);
  font-family: var(--mono);
  background: var(--warn);
  color: var(--surface);
  padding: 1px 5px;
  border-radius: 3px;
  margin-left: 6px;
  letter-spacing: 0.5px;
}
.lens-stale {
  display: inline-block;
  font-size: var(--fs-micro);
  font-family: var(--mono);
  background: var(--muted);
  color: var(--surface);
  padding: 1px 5px;
  border-radius: 3px;
  margin-left: 6px;
  letter-spacing: 0.5px;
}
.lens-five_min_reread .lens-body h2 {
  color: var(--accent);
  font-size: 16px;
  margin-top: 1em;
}
.lens-five_min_reread .lens-body strong { color: var(--fg); }

/* ============================================================
   Decision history — last 3 LLM recommendations from the
   audit ledger (decisions table, migration 0046). Rendered in
   the Thesis tab between the valuation/break-rule grid and the
   thesis-hygiene panels.
   ============================================================ */
.decision-history-panel { margin-top: 14px; }
.decision-list {
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding: 10px 12px;
}
.decision-badge {
  display: grid;
  grid-template-columns: 72px 1fr auto;
  align-items: center;
  gap: 10px;
  padding: 6px 10px;
  border-radius: var(--radius);
  background: var(--surface);
  border: 1px solid var(--hairline);
  font-size: 12.5px;
}
.decision-badge .decision-date { color: var(--muted); }
.decision-badge .decision-kind {
  font-weight: 600;
  letter-spacing: 0.4px;
  color: var(--fg);
}
.decision-badge .decision-outcome {
  font-size: var(--fs-micro);
  padding: 2px 8px;
  border-radius: 999px;
  text-transform: uppercase;
  letter-spacing: 0.4px;
  font-weight: 600;
}
.decision-badge.outcome-correct .decision-outcome {
  background: var(--accent-soft);
  color: var(--ok);
}
.decision-badge.outcome-wrong .decision-outcome {
  background: #fdecec;
  color: var(--bad);
}
.decision-badge.outcome-mixed .decision-outcome {
  background: var(--tone-opt);
  color: var(--warn);
}
.decision-badge.outcome-pending .decision-outcome {
  background: var(--paper);
  color: var(--muted);
}
.decision-badge.outcome-unfalsifiable .decision-outcome {
  background: var(--paper);
  color: var(--muted);
  font-style: italic;
}

/* ============================================================
   Cross-quarter themes — prepared remarks vs Q&A rollup (§5 split)
   ============================================================ */
.theme-bucket { margin-top: 14px; }
.theme-bucket:first-of-type { margin-top: 4px; }
.theme-bucket-title {
  margin: 8px 0 6px;
  font-size: 13px;
  text-transform: uppercase;
  letter-spacing: 0.06em;
  color: var(--muted);
}
.theme-rollup-list { list-style: none; padding: 0; margin: 0; }
.theme-row {
  padding: 8px 0 10px;
  border-top: 1px solid var(--hairline);
}
.theme-row:first-child { border-top: 0; }
.theme-head { margin-bottom: 4px; font-size: 14px; }
.theme-spark {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin: 4px 0 6px;
}
.theme-spark-cell {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 2px 7px;
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
  padding: 0 0 0 12px;
  margin: 4px 0 0;
  border-left: 2px solid var(--border);
}
.theme-evidence li {
  font-size: var(--fs-caption);
  margin: 4px 0;
  color: var(--fg-soft);
  line-height: 1.45;
}
.theme-evidence em { font-style: normal; }
.theme-note {
  font-size: var(--fs-caption);
  margin: 0 0 8px;
}

/* ============================================================
   P3 panels (P4-A1) — macro sensitivities, strategic targets,
   customer concentrations, lease ladder, decision history full
   ledger, say-do verdicts, peer comp. Most reuse the canonical .tbl
   class; the styles below are for the Decisions tab's
   summary-chip ribbon + small layout tweaks.
   ============================================================ */
.macro-sens-panel,
.strategic-targets-panel,
.customer-concentration-panel,
.lease-ladder-panel,
.saydo-verdicts-panel,
.peer-comp-panel {
  margin-top: var(--gap);
}
.decision-chips {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  padding: 12px var(--panel-pad-x) 4px;
}
.decision-chips-sub {
  padding-top: 0;
  padding-bottom: 12px;
}
.decision-chip {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 3px 10px;
  background: var(--paper);
  border: 1px solid var(--hairline);
  border-radius: 999px;
  font-size: var(--fs-caption);
}
.decision-chip-label {
  
  font-size: var(--fs-micro);
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
"""
)
