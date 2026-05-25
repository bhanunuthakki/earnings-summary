"""CSS for the workspace HTML renderer.

Adapted from the Anthropic design bundle's ``workspace.css`` (paper-on-mono
editorial palette, three themes, two densities). Carries small additions for
the three tabs that the design itself doesn't cover (Company / Position /
Sources), so the rest of the brief's content has somewhere to render without
falling back to the legacy HTML doc.

Single string constant. The renderer inlines it inside a ``<style>`` tag so
the deliverable stays a single self-contained HTML file (matches the existing
``html.py`` renderer's contract).
"""

from __future__ import annotations

CSS = r"""
/* ============================================================
   Tokens — paper-on-monochrome editorial palette.
   Single accent. 3 themes (paper · white · dark) · 2 densities.
   ============================================================ */
:root {
  --bg: #fafaf7;
  --surface: #ffffff;
  --paper: #f4f3ef;
  --fg: #0c0d10;
  --fg-soft: #2a2c33;
  --muted: #6c6f78;
  --muted-2: #9a9da6;
  --border: #e4e3dd;
  --border-2: #d1cfc7;
  --hairline: #ecebe5;

  --accent: #1d4ed8;
  --accent-soft: #eef2ff;

  --ok: #1d4ed8;
  --warn: #b97c00;
  --bad: #b91c1c;
  --pos: #1d4ed8;
  --neg: #6c6f78;

  --seg-1: #0c0d10;
  --seg-2: #43464e;
  --seg-3: #7a7d86;
  --seg-4: #b6b8be;
  --seg-5: #dcdcd7;

  --tone-pos: #eef2ff;
  --tone-neu: #f4f3ef;
  --tone-opt: #fff8e6;
  --tone-neg: #fdf2f2;

  --sans: 'Inter', -apple-system, BlinkMacSystemFont, system-ui, sans-serif;
  --serif: 'Source Serif 4', 'Source Serif Pro', Georgia, serif;
  --mono: 'JetBrains Mono', ui-monospace, SFMono-Regular, Menlo, monospace;

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

:root[data-theme="white"] {
  --bg: #ffffff;
  --paper: #fafaf7;
  --hairline: #efeeea;
}

:root[data-theme="dark"] {
  --bg: #0c0d10;
  --surface: #14161b;
  --paper: #1a1d23;
  --fg: #f4f3ef;
  --fg-soft: #d5d6d2;
  --muted: #888b94;
  --muted-2: #5b5e66;
  --border: #2a2d35;
  --border-2: #383b44;
  --hairline: #1f2127;

  --accent: #8aa8ff;
  --accent-soft: #1c2138;

  --ok: #8aa8ff;
  --warn: #f5c66a;
  --bad: #f08a8a;
  --pos: #8aa8ff;
  --neg: #888b94;

  --seg-1: #f4f3ef;
  --seg-2: #b6b8be;
  --seg-3: #7a7d86;
  --seg-4: #43464e;
  --seg-5: #25282f;

  --tone-pos: #1a2238;
  --tone-neu: #1a1d23;
  --tone-opt: #2b2418;
  --tone-neg: #2b1a1a;
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
  font-size: 11.5px; font-weight: 500;
  color: var(--fg-soft);
  background: var(--surface);
  letter-spacing: 0.01em;
  white-space: nowrap;
}
.badge .dot { width: 6px; height: 6px; border-radius: 50%; }

.pill {
  display: inline-flex; align-items: center;
  padding: 2px 8px; border-radius: 3px;
  font-family: var(--mono); font-size: 10.5px; font-weight: 500;
  letter-spacing: 0.04em;
  border: 1px solid var(--border-2);
  white-space: nowrap;
}
.pill-ok { color: var(--accent); border-color: var(--accent); background: var(--accent-soft); }
.pill-neutral { color: var(--fg); border-color: var(--fg); }
.pill-warn { color: var(--warn); border-color: var(--warn); background: rgba(185,124,0,0.08); }
.pill-bad { color: var(--bad); border-color: var(--bad); background: var(--tone-neg); }
.pill-muted { color: var(--muted-2); border-color: var(--border); }

.ic-btn {
  appearance: none;
  border: 1px solid var(--border);
  background: var(--surface);
  width: 30px; height: 30px;
  border-radius: 6px;
  display: inline-flex; align-items: center; justify-content: center;
  cursor: pointer;
  color: var(--fg-soft);
  font-size: 14px;
}
.ic-btn:hover { color: var(--fg); border-color: var(--border-2); }

.stub {
  padding: 14px var(--panel-pad-x);
  font-size: 12.5px; color: var(--muted);
  background: var(--paper);
  border-top: 1px solid var(--hairline);
}
.stub-label {
  font-family: var(--mono); font-size: 10px; font-weight: 600;
  letter-spacing: 0.08em; text-transform: uppercase;
  color: var(--warn); margin-right: 8px;
}

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
  font-size: 10.5px; color: var(--muted);
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
  font-family: var(--mono); font-size: 10.5px; font-weight: 600;
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
  font-size: 12px; color: var(--muted); margin-bottom: 10px;
  min-height: 32px; line-height: 1.35;
}
.kpi-row { display: flex; align-items: baseline; gap: 10px; }
.kpi-value {
  font-family: var(--mono); font-size: 26px; font-weight: 500;
  font-variant-numeric: tabular-nums; letter-spacing: -0.015em;
}
.kpi-delta {
  font-family: var(--mono); font-size: 12px; font-weight: 500;
}
.kpi-delta.pos { color: var(--accent); }
.kpi-delta.neg { color: var(--muted); }
.kpi-spark { margin-top: 10px; color: var(--accent); }
.kpi-axis {
  display: flex; justify-content: space-between; align-items: baseline;
  font-family: var(--mono); font-size: 10px; color: var(--muted-2);
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
  font-family: var(--mono); font-size: 10.5px; font-weight: 600;
  letter-spacing: 0.1em; text-transform: uppercase; color: var(--muted);
}
.news-sub { font-size: 11.5px; color: var(--muted-2); }
.news-grid {
  display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
  gap: 14px;
}
.news-item {
  border: 1px solid var(--border);
  background: var(--surface);
  padding: 14px;
  border-radius: 6px;
  position: relative;
}
.news-item::before {
  content: ''; position: absolute; left: 0; top: 0; bottom: 0;
  width: 3px; border-radius: 6px 0 0 6px;
}
.news-item.tone-pos::before { background: var(--accent); }
.news-item.tone-opt::before { background: var(--warn); }
.news-item.tone-neg::before { background: var(--bad); }
.news-item.tone-neu::before { background: var(--muted-2); }
.news-meta {
  display: flex; align-items: center; gap: 8px; flex-wrap: wrap;
  font-family: var(--mono); font-size: 10.5px;
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
.news-src { color: var(--muted); font-family: var(--sans); font-size: 11px; }
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
  font-family: var(--mono); font-size: 10.5px; font-weight: 500;
  padding: 1px 6px; background: var(--paper);
  border-radius: 3px; color: var(--muted);
}
.tab.active .tab-count { background: var(--fg); color: var(--bg); }
.tabs-spacer { flex: 1; }
.tabs-meta { font-size: 11.5px; padding-right: 4px; color: var(--muted); }
.tab-pane { padding-top: var(--section-gap); display: none; }
.tab-pane.active { display: block; }
.tab-body { display: flex; flex-direction: column; gap: var(--gap-lg); }

.eyebrow {
  font-family: var(--mono); font-size: 11px; font-weight: 500;
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
  border-radius: 8px;
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
  font-family: var(--mono); font-size: 10.5px; font-weight: 500;
  letter-spacing: 0.08em; text-transform: uppercase;
  color: var(--muted); text-align: right;
}
.quarter-select-btns { display: flex; gap: 4px; flex-wrap: wrap; justify-content: flex-end; }
.qbtn {
  appearance: none; border: 1px solid var(--border-2);
  background: var(--surface);
  padding: 7px 13px;
  font-family: var(--mono); font-size: 11.5px; font-weight: 500;
  color: var(--muted); cursor: pointer; border-radius: 4px;
}
.qbtn:hover { color: var(--fg); border-color: var(--fg); }
.qbtn.active { color: var(--bg); background: var(--fg); border-color: var(--fg); }

/* ============================================================
   Panels
   ============================================================ */
.panel {
  border: 1px solid var(--border);
  border-radius: 6px;
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
.panel-sub { font-size: 11.5px; color: var(--muted); }
.panel-foot {
  padding: 12px var(--panel-pad-x);
  background: var(--paper);
  border-top: 1px solid var(--hairline);
  font-size: 12px; color: var(--fg-soft);
  display: flex; gap: 10px; align-items: flex-start; line-height: 1.5;
}
.flag { color: var(--warn); font-size: 14px; }
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

.metrics-table { width: 100%; border-collapse: collapse; font-size: 13px; }
.metrics-table th {
  text-align: left; font-size: 10.5px; font-weight: 500;
  color: var(--muted); padding: 10px var(--panel-pad-x);
  text-transform: uppercase; letter-spacing: 0.06em;
  border-bottom: 1px solid var(--hairline);
}
.metrics-table th.num, .metrics-table td.num { text-align: right; }
.metrics-table td {
  padding: var(--table-pad-y) var(--panel-pad-x);
  border-bottom: 1px solid var(--hairline);
}
.metrics-table tr:last-child td { border-bottom: 0; }
.metrics-table tr.emph td {
  background: var(--paper); font-weight: 500;
}
.metrics-table td.num { font-family: var(--mono); font-variant-numeric: tabular-nums; }

.quotes {
  padding: 6px var(--panel-pad-x) var(--panel-pad-y);
  display: flex; flex-direction: column;
}
.quote {
  padding: 12px 0;
  border-top: 1px solid var(--hairline);
}
.quote:first-child { border-top: 0; padding-top: 8px; }
.quote-tag {
  font-family: var(--mono); font-size: 10px; font-weight: 600;
  letter-spacing: 0.1em; color: var(--accent);
  text-transform: uppercase; margin-bottom: 7px;
}
.quote-body {
  font-family: var(--serif); font-size: 15px; line-height: 1.45;
  color: var(--fg);
}
.quote-speaker {
  margin-top: 6px; font-size: 11.5px; color: var(--muted);
  font-family: var(--mono);
}

.qa-list { display: flex; flex-direction: column; }
.qa-row { border-top: 1px solid var(--hairline); }
.qa-row:first-child { border-top: 0; }
.qa-row.open { background: var(--paper); }
.qa-head {
  width: 100%; appearance: none; border: 0; background: transparent;
  display: flex; align-items: center; gap: 14px;
  padding: 13px var(--panel-pad-x);
  text-align: left; cursor: pointer; font: inherit;
}
.qa-head:hover { background: var(--paper); }
.qa-chev {
  font-family: var(--mono); width: 14px; color: var(--muted);
  font-size: 16px; font-weight: 400;
}
.qa-tag {
  font-family: var(--mono); font-size: 10px; font-weight: 600;
  padding: 3px 7px; border: 1px solid var(--accent);
  color: var(--accent); background: var(--accent-soft);
  border-radius: 3px;
  letter-spacing: 0.06em; min-width: 70px; text-align: center;
}
.qa-topic { font-size: 13.5px; font-weight: 500; flex: 1; color: var(--fg); }
.qa-analysts { font-size: 11.5px; color: var(--muted); font-style: italic; max-width: 280px; text-align: right; }
.qa-ref { font-family: var(--mono); font-size: 10.5px; min-width: 38px; text-align: right; color: var(--muted-2); }
.qa-body {
  padding: 0 var(--panel-pad-x) 18px 50px;
  display: none;
  flex-direction: column; gap: 12px;
}
.qa-row.open .qa-body { display: flex; }
.qa-q, .qa-a, .qa-followup { display: flex; gap: 12px; font-size: 13.5px; line-height: 1.55; }
.qa-q { color: var(--fg-soft); }
.qa-a { color: var(--fg); }
.qa-followup {
  font-size: 12.5px; color: var(--muted);
  padding-top: 8px; border-top: 1px dashed var(--border);
}
.qa-label {
  font-family: var(--mono); font-size: 10.5px; font-weight: 600;
  color: var(--accent); flex-shrink: 0; width: 14px; padding-top: 2px;
}
.qa-followup-label {
  font-family: var(--mono); font-size: 9.5px; font-weight: 600;
  letter-spacing: 0.08em; color: var(--muted);
  text-transform: uppercase; flex-shrink: 0; padding-top: 3px;
  width: 64px;
}

/* Say-Do */
.saydo-meta { display: flex; flex-direction: column; gap: 10px; align-items: flex-end; padding-top: 4px; }
.saydo-meta-label {
  font-size: 11px; color: var(--muted);
  text-transform: uppercase; letter-spacing: 0.06em;
}
.saydo-history {
  display: flex; flex-direction: column; gap: 6px;
  align-items: flex-end;
}
.saydo-table { width: 100%; border-collapse: collapse; font-size: 13px; }
.saydo-table th {
  text-align: left; font-size: 10.5px; font-weight: 500;
  color: var(--muted); padding: 10px var(--panel-pad-x);
  text-transform: uppercase; letter-spacing: 0.06em;
  border-bottom: 1px solid var(--hairline);
}
.saydo-table td {
  padding: var(--row-pad-y) var(--panel-pad-x);
  border-bottom: 1px solid var(--hairline);
  vertical-align: top;
}
.saydo-table tr:last-child td { border-bottom: 0; }
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
  font-size: 11.5px; color: var(--fg-soft);
}
.legend-swatch { width: 10px; height: 10px; border-radius: 2px; }
.overlay-stat {
  position: absolute; top: 14px; right: 30px;
  display: flex; flex-direction: column; align-items: flex-end;
}
.overlay-stat span {
  font-size: 10.5px; color: var(--muted);
  text-transform: uppercase; letter-spacing: 0.06em;
}
.overlay-stat strong {
  font-family: var(--mono); font-size: 22px;
  font-weight: 500; color: var(--fg); margin-top: 2px;
}
.overlay-stat .pos { font-family: var(--mono); font-size: 11px; color: var(--accent); }

.table-scroll { overflow-x: auto; }
.fin-table { width: 100%; border-collapse: collapse; font-size: 12.5px; }
.fin-table th {
  text-align: left; font-size: 10.5px; font-weight: 500;
  color: var(--muted); padding: 11px 14px;
  text-transform: uppercase; letter-spacing: 0.05em;
  border-bottom: 1px solid var(--hairline);
  white-space: nowrap;
}
.fin-table th.num, .fin-table td.num {
  text-align: right;
  font-family: var(--mono); font-variant-numeric: tabular-nums;
}
.fin-table td { padding: 9px 14px; border-bottom: 1px solid var(--hairline); white-space: nowrap; }
.fin-table tr:last-child td { border-bottom: 0; }
.fin-table tr:hover td { background: var(--paper); }

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

.sens-wrap { padding: 12px var(--panel-pad-x); }
.sens-table { width: 100%; border-collapse: collapse; font-family: var(--mono); font-size: 11.5px; }
.sens-table th {
  font-weight: 500; padding: 6px 4px;
  color: var(--muted); font-size: 10px; letter-spacing: 0.04em;
  text-align: center;
  background: transparent;
}
.sens-table th.col-base { color: var(--accent); font-weight: 600; }
.sens-table tr.row-base th { color: var(--accent); font-weight: 600; }
.sens-row-h { text-align: left !important; padding-right: 12px !important; }
.sens-cell {
  padding: 7px 4px; text-align: center;
  font-variant-numeric: tabular-nums;
  border-radius: 3px;
}
.sens-cell.above { color: var(--accent); background: var(--accent-soft); }
.sens-cell.below { color: var(--muted); background: var(--paper); }
.sens-cell.base {
  outline: 2px solid var(--accent);
  outline-offset: -2px;
  font-weight: 700;
}
.sens-legend {
  display: flex; gap: 18px; padding-top: 12px;
  font-size: 11px; color: var(--muted);
  flex-wrap: wrap;
}
.sens-sw {
  display: inline-block; width: 12px; height: 12px;
  border-radius: 2px; vertical-align: -2px; margin-right: 6px;
}
.sens-sw.above { background: var(--accent-soft); border: 1px solid var(--accent); }
.sens-sw.below { background: var(--paper); border: 1px solid var(--border-2); }
.sens-sw.base { background: var(--accent); }

.break-table { width: 100%; border-collapse: collapse; font-size: 12.5px; }
.break-table th {
  text-align: left; font-size: 10.5px; font-weight: 500;
  color: var(--muted); padding: 10px var(--panel-pad-x);
  text-transform: uppercase; letter-spacing: 0.05em;
  border-bottom: 1px solid var(--hairline);
}
.break-table th.num, .break-table td.num {
  text-align: right;
  font-family: var(--mono); font-variant-numeric: tabular-nums;
}
.break-table td { padding: var(--table-pad-y) var(--panel-pad-x); border-bottom: 1px solid var(--hairline); }
.break-table tr:last-child td { border-bottom: 0; }
.break-status-ok { color: var(--accent); }
.break-status-warn { color: var(--warn); }
.break-status-breach { color: var(--bad); }

.failure { display: flex; gap: 14px; padding: 14px var(--panel-pad-x); border-bottom: 1px solid var(--hairline); }
.failure:last-child { border-bottom: 0; }
.failure-num {
  font-family: var(--mono); font-size: 11px; font-weight: 600;
  color: var(--accent); width: 22px; flex-shrink: 0; padding-top: 2px;
}
.failure-body { flex: 1; }
.failure-title {
  font-size: 13.5px; font-weight: 500; line-height: 1.5;
  margin-bottom: 8px; color: var(--fg);
}
.failure-meta {
  display: grid; grid-template-columns: 110px 1fr; gap: 6px 12px;
  font-size: 12px; color: var(--fg-soft); line-height: 1.55;
  margin-top: 6px;
}
.failure-label {
  font-family: var(--mono); font-size: 9.5px; font-weight: 600;
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
  border-radius: 8px;
  font-family: var(--serif);
  font-size: 17px;
  line-height: 1.55;
  color: var(--fg);
}
.elevator-block::before {
  content: 'Elevator pitch';
  display: block;
  font-family: var(--mono); font-size: 10.5px; font-weight: 600;
  letter-spacing: 0.1em; text-transform: uppercase;
  color: var(--muted); margin-bottom: 10px;
}
.seg-list { width: 100%; border-collapse: collapse; font-size: 13px; }
.seg-list th {
  text-align: left; font-size: 10.5px; font-weight: 500;
  color: var(--muted); padding: 10px var(--panel-pad-x);
  text-transform: uppercase; letter-spacing: 0.06em;
  border-bottom: 1px solid var(--hairline);
}
.seg-list th.num { text-align: right; }
.seg-list td {
  padding: var(--row-pad-y) var(--panel-pad-x);
  border-bottom: 1px solid var(--hairline);
  vertical-align: top;
}
.seg-list tr:last-child td { border-bottom: 0; }
.seg-list .num {
  text-align: right;
  font-family: var(--mono); font-variant-numeric: tabular-nums;
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
  font-family: var(--mono); font-size: 10px; font-weight: 600;
  padding: 3px 7px; border-radius: 3px;
  background: var(--accent-soft); color: var(--accent);
  letter-spacing: 0.06em; text-transform: uppercase;
}
.ir-quarter {
  font-family: var(--mono); font-size: 12px; color: var(--fg);
}
.ir-link {
  font-size: 11.5px; color: var(--accent); text-decoration: none;
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
  border-radius: 6px;
  overflow: hidden;
  border: 1px solid var(--border);
}
.position-stat {
  background: var(--surface);
  padding: 16px 18px;
}
.position-stat-label {
  font-size: 10.5px; color: var(--muted);
  text-transform: uppercase; letter-spacing: 0.08em;
  margin-bottom: 8px;
}
.position-stat-value {
  font-family: var(--mono); font-size: 20px; font-weight: 500;
  font-variant-numeric: tabular-nums; letter-spacing: -0.01em;
}
.position-stat-sub {
  font-family: var(--mono); font-size: 11px;
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
  font-family: var(--mono); font-size: 11.5px; color: var(--muted);
}
.decision-action {
  font-family: var(--mono); font-size: 11px; font-weight: 600;
  padding: 2px 7px; border-radius: 3px;
  background: var(--accent-soft); color: var(--accent);
  letter-spacing: 0.06em; text-transform: uppercase;
}
.decision-confidence {
  font-size: 11.5px; color: var(--muted); font-style: italic;
}
.decision-outcome {
  font-family: var(--mono); font-size: 10px; font-weight: 600;
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
.coverage-table { width: 100%; border-collapse: collapse; font-size: 12.5px; }
.coverage-table th {
  text-align: left; font-size: 10.5px; font-weight: 500;
  color: var(--muted); padding: 10px 12px;
  text-transform: uppercase; letter-spacing: 0.06em;
  border-bottom: 1px solid var(--hairline);
}
.coverage-table th.cov-cell { text-align: center; }
.coverage-table td {
  padding: 7px 12px;
  border-bottom: 1px solid var(--hairline);
  font-family: var(--mono); font-variant-numeric: tabular-nums;
}
.coverage-table tr:last-child td { border-bottom: 0; }
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
  font-family: var(--mono); font-size: 12px; color: var(--muted);
  display: inline-block;
  transition: transform .15s;
}
.transcript-block[open] summary::before { transform: rotate(90deg); }
.transcript-block summary:hover { background: var(--paper); }
.transcript-text {
  padding: 12px var(--panel-pad-x) 22px;
  font-family: var(--mono); font-size: 11.5px; line-height: 1.55;
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
  font-size: 12px; color: var(--muted);
  flex-wrap: wrap; gap: 10px;
}
.peer-eyebrow {
  font-family: var(--mono); font-size: 11px; font-weight: 600;
  letter-spacing: 0.1em; text-transform: uppercase;
  color: var(--muted); margin-right: 14px;
}
.peer-eyebrow-sub { color: var(--muted-2); }
.peer-grid {
  display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
  gap: 1px; background: var(--border);
  border: 1px solid var(--border);
  border-radius: 6px;
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
.peer-name { font-size: 11.5px; color: var(--muted); }
.peer.active .peer-name { color: var(--muted-2); }

/* ============================================================
   Footer
   ============================================================ */
.l1-footer {
  display: flex; justify-content: space-between; gap: 16px;
  padding: 28px var(--pad-x) 0;
  margin-top: 32px;
  border-top: 1px solid var(--border);
  font-size: 11.5px; color: var(--fg-soft);
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
  border-radius: 6px;
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
  border-radius: 12px;
  box-shadow: 0 12px 40px rgba(0,0,0,.16);
  font-family: var(--sans); font-size: 12.5px;
  overflow: hidden;
}
.twk-panel.open { display: flex; }
.twk-hd {
  display: flex; align-items: center; justify-content: space-between;
  padding: 10px 8px 10px 14px;
}
.twk-hd b { font-size: 12px; font-weight: 600; letter-spacing: .01em; }
.twk-x {
  appearance: none; border: 0; background: transparent;
  color: var(--muted); width: 24px; height: 24px;
  border-radius: 6px; cursor: pointer; font-size: 13px;
}
.twk-x:hover { background: var(--paper); color: var(--fg); }
.twk-body {
  padding: 4px 14px 14px;
  display: flex; flex-direction: column; gap: 12px;
}
.twk-sect {
  font-family: var(--mono); font-size: 9.5px; font-weight: 600;
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
  transition: transform .15s;
}
.twk-toggle[data-on="1"]::after { transform: translateX(14px); }

/* ============================================================
   Financials line-item drill-down + charts_v2 dark overrides
   ============================================================ */
.fin-row.drillable { cursor: pointer; }
.fin-row.drillable:hover td { background: var(--paper); }
.fin-chev {
  display: inline-block; width: 14px;
  font-family: var(--mono); font-size: 10px;
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

.thesis-ledger-details {
  border: 1px solid var(--border);
  border-radius: 6px;
  background: var(--surface);
  overflow: hidden;
}
.thesis-ledger-details summary {
  padding: 12px var(--panel-pad-x);
  cursor: pointer; user-select: none;
  font-size: 13px; font-weight: 500;
  list-style: none;
}
.thesis-ledger-details summary::-webkit-details-marker { display: none; }
.thesis-ledger-details summary::before {
  content: '\25B8';
  font-family: var(--mono); font-size: 11px; color: var(--muted);
  margin-right: 8px;
  display: inline-block;
  transition: transform .15s;
}
.thesis-ledger-details[open] summary::before { transform: rotate(90deg); }
.thesis-ledger-details summary:hover { background: var(--paper); }

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
.xsmall { font-size: 10.5px; line-height: 1.4; }
.stub-warning {
  background: var(--tone-neg);
  border-left: 3px solid var(--bad);
  padding: 10px 14px;
  border-radius: 4px;
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
  font-size: 10.5px;
  font-weight: 400;
  line-height: 1.4;
}

/* Position tab — decision brief link + outcome notes block. */
.decision-brief-link {
  font-size: 11px;
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
  border-radius: 4px;
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
.valuation-current-label { font-size: 11px; color: var(--muted); text-transform: uppercase; letter-spacing: 0.06em; }
.valuation-band { display: flex; flex-direction: column; gap: 2px; font-size: 12.5px; color: var(--ink-muted); }
.valuation-band-row { display: flex; gap: 6px; }
.valuation-band-row .mono { font-family: var(--font-mono); color: var(--ink); }
.valuation-verdict {
  margin-left: auto; padding: 6px 10px;
  background: var(--panel-alt); border: 1px solid var(--hairline); border-radius: 6px;
  font-size: 12px; color: var(--ink); font-style: italic;
}
.valuation-spark { padding: 12px 0 4px; }
.valuation-spark-axis {
  display: flex; justify-content: space-between; font-size: 11px;
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
  border-radius: 4px;
}
.l1-thesis .thesis-label {
  font-size: 10.5px; text-transform: uppercase; letter-spacing: 0.08em;
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
  .tab-pane { display: block !important; padding-top: 16px; break-before: page; }
  .tabs { display: none; }
  .news-strip, .kpi-strip, .l1-identity { break-after: avoid; }
}
"""
