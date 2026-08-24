"""Shared inline CSS for the Personal CIO dashboard surfaces.

Palette comes from the shared token source (``src/ui/tokens.py`` — master
build P0.1) so this surface can never drift from the workspace again;
layout tokens stay local. Type sizes come from the semantic scale
(``--fs-*``: importance, not surface — see ui/tokens.py), chrome from the
shared radius/transition tokens. Single self-contained string inlined under
``<style>`` so the deliverable HTML files open straight from the
filesystem with no asset fetches.
"""

from __future__ import annotations

from ui.controls import controls_css
from ui.tokens import palette_css

BASE_CSS = (
    "\n/* Shared palette (single source: src/ui/tokens.py) + local layout tokens. */\n"
    + palette_css("dark")
    + controls_css("dark")
    + r"""
* { box-sizing: border-box; }
html, body { margin: 0; padding: 0; background: var(--bg); color: var(--fg); }
body { font-family: var(--sans); font-size: var(--fs-body); line-height: 1.55; }

a { color: var(--accent); text-decoration: none; transition: color var(--transition); }
a:hover { text-decoration: underline; }
button { transition: color var(--transition), border-color var(--transition),
  background var(--transition); }

.muted { color: var(--muted); }
.mono { font-family: var(--mono); font-size: var(--fs-caption); }
.num { font-variant-numeric: tabular-nums; }

.l1-shell {
  max-width: 1180px;
  margin: 0 auto;
  padding: var(--sp-4) var(--sp-5) 40px var(--sp-5);
}

/* Header */
.l1-header { padding-bottom: var(--sp-4); border-bottom: 1px solid var(--hairline); margin-bottom: var(--sp-5); }
.l1-header h1 { margin: 0 0 4px 0; font-size: var(--fs-display); font-weight: 600; letter-spacing: -0.01em; }
.l1-header .l1-subtitle { color: var(--muted); font-size: var(--fs-body); }

/* Section */
.dash-section { margin-bottom: var(--sp-5); }

/* Active-filter chips — feed only. Only the constraints actually narrowing the
   view render here, each a removable chip linking back to the unfiltered feed
   (no "ALL · ALL" band when nothing is filtered). The chip itself is the shared
   kit (.k-chip + .k-chip-btn, controls.py), emitted by src/dashboard/feed.py;
   only the layout row + the mono value treatment stay local. */
.dash-filters { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 10px; }
.dash-filters .filter-value { font-family: var(--mono); }

/* Alert cards compose the canonical .k-card + .k-card-stack geometry; this
   namespace preserves the inbox JS hook and adds page-flow layout only. */
.alert-card { margin-bottom: var(--sp-3); }
.alert-card-head { display: flex; flex-wrap: wrap; align-items: center; gap: var(--sp-2); }
/* Alert-card identity marks ride the shared kit (controls.py), emitted by
   src/dashboard/_card.py: the ticker is a .k-tick-sym (mono 600, no box), the
   trigger a .k-chip, the status a .k-pill + tone. Only timestamp layout is local. */
.fired-at { color: var(--muted); font-family: var(--mono); font-size: var(--fs-caption); margin-left: auto; }

.alert-memo {
  margin: 0;
  padding: 10px 12px;
  background: var(--paper);
  border-left: 3px solid var(--border-2);
  border-radius: 0 var(--radius) var(--radius) 0;
  font-size: var(--fs-body);
  color: var(--fg-soft);
}
.alert-memo-pending { color: var(--muted); }

/* Queued actions */
.queued-actions { margin: 0; }
.queued-actions h4 { font-size: var(--fs-caption); font-weight: 600; margin: 0 0 6px 0; color: var(--muted); letter-spacing: 0.05em; text-transform: uppercase; }
.queued-action {
  display: flex; flex-wrap: wrap; gap: 10px; align-items: flex-start;
  padding: 8px 10px;
  background: var(--paper);
  border-radius: var(--radius);
  margin-bottom: 6px;
}
.qa-body { flex: 1; min-width: 200px; color: var(--fg-soft); font-size: var(--fs-body); }
/* approve/dismiss are kit buttons (.k-btn .k-btn-sm) — no local link skin. */
.qa-actions {
  display: flex; gap: 6px; align-items: center;
  font-size: var(--fs-caption);
}
.qa-status-applied { color: var(--ok); }
.qa-status-cancelled { color: var(--muted); }

/* Evidence drawer */
.evidence-drawer {
  margin: 0;
  background: var(--paper);
  border-radius: var(--radius);
}
.evidence-drawer > summary {
  cursor: pointer;
  padding: 8px 12px;
  color: var(--muted);
  font-size: var(--fs-caption); font-weight: 600;
  letter-spacing: 0.05em;
  text-transform: uppercase;
  user-select: none;
  transition: color var(--transition);
}
.evidence-drawer > summary:hover { color: var(--fg-soft); }
.evidence-drawer[open] > summary { border-bottom: 1px solid var(--hairline); }
.evidence-body { padding: 10px 12px; }
.evidence-section { margin-bottom: 10px; }
.evidence-section:last-child { margin-bottom: 0; }
.evidence-section-title {
  font-size: var(--fs-caption);
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.06em;
  color: var(--muted);
  margin-bottom: 4px;
}
.evidence-summary-text { color: var(--fg-soft); font-size: var(--fs-body); }
/* P4.4 — the owner's open notes attached to the alert's evidence. */
.evidence-notes-list { list-style: none; margin: 0; padding: 0; }
.evidence-notes-list li { padding: 3px 0; font-size: var(--fs-body); color: var(--fg-soft); }
/* evidence-note-kind → the shared .k-chip (controls.py); emitted by
   src/dashboard/evidence_drawer.py. The report's .oi-kind is its own (the §2
   report-category exception) and lives in workspace_styles.py. */
.evidence-malformed {
  padding: 10px;
  background: color-mix(in srgb, var(--bad) 12%, transparent);
  border: 1px solid var(--bad);
  border-radius: var(--radius);
  color: var(--bad);
  margin-bottom: 10px;
}
.evidence-citations-table {
  width: 100%;
  border-collapse: collapse;
  font-size: var(--fs-body);
  font-variant-numeric: tabular-nums;
}
.evidence-citations-table th {
  text-align: left;
  padding: 4px 8px 4px 0;
  color: var(--muted);
  font-weight: 600;
  font-size: var(--fs-caption);
  text-transform: uppercase;
  letter-spacing: 0.05em;
  border-bottom: 1px solid var(--hairline);
}
.evidence-citations-table td {
  padding: 6px 8px 6px 0;
  vertical-align: top;
  border-bottom: 1px solid var(--hairline);
}
.evidence-citations-table tr:last-child td { border-bottom: none; }
.cite-kind { color: var(--fg-soft); white-space: nowrap; }
.cite-locator { color: var(--muted); word-break: break-all; }
.cite-excerpt { color: var(--fg-soft); }
.cite-prov { color: var(--muted); font-family: var(--mono); font-size: var(--fs-caption); white-space: nowrap; }
.prov-source { color: var(--fg-soft); }
.prov-fetched { color: var(--muted); }
.evidence-raw { margin-top: 4px; }
.evidence-raw > summary {
  cursor: pointer;
  color: var(--muted);
  font-size: var(--fs-caption); font-weight: 600;
  letter-spacing: 0.05em;
}
.evidence-raw-pre {
  margin: 6px 0 0 0;
  padding: 8px 10px;
  background: var(--bg);
  border-radius: var(--radius);
  font-family: var(--mono); font-size: var(--fs-caption);
  color: var(--fg-soft);
  white-space: pre-wrap;
  word-break: break-all;
  max-height: 320px;
  overflow: auto;
}

.empty-state {
  padding: 20px;
  text-align: center;
  background: var(--paper);
  border: 1px dashed var(--border);
  border-radius: var(--radius);
  color: var(--muted);
  font-size: var(--fs-body);
}

.l1-footer {
  margin-top: 32px;
  padding-top: 14px;
  border-top: 1px solid var(--hairline);
  display: flex;
  justify-content: space-between;
  align-items: center;
  color: var(--muted);
  font-size: var(--fs-caption);
  letter-spacing: 0.04em;
}
"""
)

# The remaining dashboard surfaces deliberately consume these same semantic
# classes.  Keeping their visual rules here makes fragment renders (feed,
# cockpit, analytical export, and action panels) share one layout vocabulary.
INBOX_CSS = r"""
.ix-stream { display: flex; flex-direction: column; gap: var(--sp-2); }
.ix-card { border-radius: var(--radius); background: var(--surface); padding: 9px 12px; }
.ix-head { display: flex; align-items: baseline; gap: var(--sp-2); }
.ix-ticker { font-family: var(--mono); font-weight: 600; font-size: var(--fs-caption); color: var(--fg); text-decoration: none; }
.ix-ticker:hover { color: var(--accent); }
.ix-when { margin-left: auto; color: var(--muted); font-size: var(--fs-caption); font-family: var(--mono); white-space: nowrap; }
.ix-body { margin-top: var(--sp-1); font-size: var(--fs-body); line-height: 1.45; color: var(--fg); display: -webkit-box; -webkit-line-clamp: 3; -webkit-box-orient: vertical; overflow: hidden; }
.ix-compact .ix-body { -webkit-line-clamp: 2; }
.ix-card:hover .ix-body { -webkit-line-clamp: unset; }
.ix-foot { display: flex; flex-wrap: wrap; align-items: baseline; gap: var(--sp-4); margin-top: var(--sp-2); }
.ix-foot a { font-size: var(--fs-caption); text-decoration: none; }
.ix-foot-act { color: var(--accent); }
.ix-foot-act:hover { text-decoration: underline; }
.ix-foot-dismiss { color: var(--muted); }
.ix-foot-dismiss:hover { color: var(--bad); text-decoration: none; }
.ix-foot-link { color: var(--muted); }
.ix-foot-link:hover { color: var(--accent); }
.ix-memo-acts { display: flex; gap: var(--sp-2); margin-top: var(--sp-2); }
.ix-memo-open { text-decoration: none; }
.ix-note-dismiss:hover { color: var(--bad); border-color: var(--bad); }
.ix-empty { color: var(--muted); font-size: var(--fs-body); padding: var(--sp-4) var(--sp-1); }
.ix-degraded { color: var(--fg); font-size: var(--fs-body); padding: var(--sp-3) var(--sp-4); border: 1px solid var(--bad); border-radius: var(--radius); background: color-mix(in srgb, var(--bad) 8%, transparent); }
.ix-degraded-why { color: var(--muted); font-size: var(--fs-caption); border-bottom: 1px dotted var(--border-2); cursor: help; }
.ix-more { color: var(--muted); font-size: var(--fs-caption); padding: var(--sp-3) var(--sp-1) var(--sp-1); border-top: 1px solid var(--border-2); margin-top: var(--sp-2); }
.ix-quick { margin-left: auto; display: inline-flex; gap: var(--sp-1); visibility: hidden; }
.ix-card:hover .ix-quick, .ix-quick:focus-within { visibility: visible; }
.ix-quick ~ .ix-when, .ix-acted ~ .ix-when { margin-left: 0; }
.ix-act-approve:hover { color: var(--ok); border-color: var(--ok); }
.ix-act-dismiss:hover { color: var(--bad); border-color: var(--bad); }
.ix-act[disabled] { opacity: 0.5; cursor: default; }
.ix-act-fail { color: var(--bad); border-color: var(--bad); }
.ix-acted { margin-left: auto; font-size: var(--fs-caption); font-weight: 600; white-space: nowrap; color: var(--muted); }
.ix-acted-applied { color: var(--ok); }
.ix-dismissed { opacity: 0.55; transition: opacity var(--transition); }
.ix-new { box-shadow: inset 2px 0 0 var(--accent); }
.ix-sev-bad { box-shadow: inset 2px 0 0 var(--bad); }
.ix-badge { display: inline-block; min-width: 14px; text-align: center; margin-left: var(--sp-1); padding: 1px var(--sp-1); border-radius: var(--radius-full); background: var(--accent); color: var(--accent-contrast); font-family: var(--mono); font-size: var(--fs-caption); font-weight: 600; line-height: 1.4; vertical-align: 2px; }
.ix-badge[hidden] { display: none; }
.ix-cats { display: flex; flex-wrap: wrap; gap: var(--sp-2); margin: 0 0 var(--sp-2); }
.ix-cat span { opacity: 0.7; margin-left: var(--sp-1); }
.ix-hide { display: none !important; }
.ix-kind[title] { cursor: help; }
.ix-acted-detail { font-weight: 400; opacity: 0.85; }
.ix-dismiss-why { margin-left: var(--sp-1); }
.ix-why-toggle { font-weight: 400; opacity: 0.7; }
.ix-why-toggle:hover { opacity: 1; }
.ix-why-input { margin-left: var(--sp-1); width: 7em; font-size: var(--fs-caption); font-family: inherit; background: var(--surface); color: var(--fg); border: 1px solid var(--border); border-radius: var(--radius); padding: 1px var(--sp-1); }
""".strip()

UPCOMING_CSS = r"""
.up-strip { margin-bottom: var(--sp-2); }
.up-strip-head { display: flex; justify-content: space-between; align-items: baseline; margin-bottom: var(--sp-1); }
.up-strip-sub { font-weight: 400; }
.up-strip-list { list-style: none; margin: 0; padding: 0; }
.up-strip-list li { padding: var(--sp-1) 0; font-size: var(--fs-caption); }
.up-tier { display: flex; align-items: baseline; gap: var(--sp-2); padding-top: var(--sp-2); }
.up-tier-label { color: var(--muted); font-size: var(--fs-caption); font-weight: 600; text-transform: uppercase; letter-spacing: 0.06em; }
.up-tier-n { color: var(--muted); font-family: var(--mono); font-size: var(--fs-caption); }
.up-row { display: flex; align-items: baseline; gap: var(--sp-2); min-width: 0; }
.up-ticker { font-family: var(--mono); font-weight: 600; color: var(--fg); flex: none; }
.up-est { color: var(--muted); font-size: var(--fs-caption); font-weight: 600; text-transform: uppercase; letter-spacing: 0.05em; flex: none; }
.up-date { margin-left: auto; font-family: var(--mono); color: var(--muted); white-space: nowrap; flex: none; }
.up-chips { display: flex; align-items: baseline; gap: var(--sp-2); min-width: 0; overflow: hidden; flex: 1 1 auto; }
.up-prep { flex: none; }
.up-watch-item { display: inline-flex; align-items: baseline; gap: var(--sp-1); background: transparent; border: 0; padding: 0; cursor: pointer; color: var(--muted); font: inherit; font-size: var(--fs-caption); min-width: 0; overflow: hidden; }
.up-watch-item:hover { color: var(--accent); }
.up-watch-item:hover .up-watch-body { text-decoration: underline; }
.up-watch-kind { flex: none; }
.up-watch-body { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; max-width: 200px; }
.up-watch-more { font-size: var(--fs-caption); color: var(--muted); flex: none; }
""".strip()

ACTIONS_CSS = r"""
.actions-section { margin: 0 0 var(--sp-4); padding: var(--sp-3) var(--sp-4); background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius); }
.actions-section h2 { margin: 0 0 var(--sp-1); }
.actions-help { font-size: var(--fs-caption); color: var(--muted); margin: 0 0 var(--sp-3); max-width: 760px; }
.actions-help code { font-family: var(--mono); font-size: var(--fs-caption); }
.actions-form { display: flex; align-items: center; gap: var(--sp-3); flex-wrap: wrap; }
input.ir-ticker { width: 170px; text-transform: uppercase; font-family: var(--mono); }
.ir-quarters-label { font-size: var(--fs-caption); color: var(--muted); display: inline-flex; align-items: center; gap: var(--sp-2); }
.ir-quarters { width: 60px; }
.actions-status { font-size: var(--fs-caption); font-weight: 500; }
.actions-status[data-tone="running"] { color: var(--warn); }
.actions-status[data-tone="ok"] { color: var(--ok); }
.actions-status[data-tone="error"] { color: var(--bad); }
.actions-output { margin: var(--sp-3) 0 0; padding: var(--sp-3) var(--sp-3); max-height: 320px; overflow-y: auto; background: var(--bg); color: var(--fg-soft); border-radius: var(--radius); font-family: var(--mono); font-size: var(--fs-caption); line-height: 1.5; white-space: pre-wrap; word-break: break-word; }
.maint-sep { color: var(--muted); margin: 0 var(--sp-1); }
.maint-export { margin: var(--sp-3) 0 0; }
""".strip()

ANALYTICAL_CSS = r"""
.analytical-dashboard { margin: 0; padding: var(--sp-5); font-family: var(--sans); background: var(--bg); color: var(--fg); line-height: 1.5; font-size: var(--fs-body); }
.analytical-dashboard h1 { font-size: var(--fs-display); margin: 0 0 var(--sp-1); font-weight: 600; }
.analytical-dashboard h2 { font-size: var(--fs-title); margin: 0 0 var(--sp-1); font-weight: 600; }
.analytical-dashboard .stamp { color: var(--muted); font-size: var(--fs-caption); font-family: var(--mono); margin-bottom: var(--sp-3); }
.panel { margin-bottom: var(--sp-4); background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius); overflow: hidden; }
.panel-head { padding: var(--sp-3) var(--sp-4); border-bottom: 1px solid var(--hairline); }
.panel-head h2 { margin: 0; }
.panel-head .sub { margin: var(--sp-1) 0 0; }
.panel-body { padding: var(--sp-4); }
.panel-foot { padding: var(--sp-3) var(--sp-4); border-top: 1px solid var(--hairline); background: var(--paper); }
.panel .sub { color: var(--muted); font-size: var(--fs-caption); margin: 0 0 var(--sp-3); }
.analytical-dashboard table { width: 100%; border-collapse: collapse; font-size: var(--fs-body); font-variant-numeric: tabular-nums; }
.analytical-dashboard th { text-align: left; padding: var(--sp-2) var(--sp-3); border-bottom: 1px solid var(--border); font-size: var(--fs-caption); text-transform: uppercase; letter-spacing: 0.06em; color: var(--muted); font-weight: 600; }
.analytical-dashboard td { padding: var(--sp-2) var(--sp-3); border-bottom: 1px solid var(--hairline); vertical-align: top; }
.analytical-dashboard tbody tr:hover td { background: var(--paper); }
.analytical-dashboard td.num { text-align: right; }
.analytical-dashboard td.muted { color: var(--muted); }
tr.tone-sell { background: color-mix(in srgb, var(--bad) 6%, transparent); }
tr.tone-trim { background: color-mix(in srgb, var(--warn) 4%, transparent); }
tr.tone-init { background: color-mix(in srgb, var(--ok) 6%, transparent); }
tr.tx-buy { background: color-mix(in srgb, var(--ok) 4%, transparent); }
tr.tx-sell { background: color-mix(in srgb, var(--bad) 2%, transparent); }
td.trigger-cell { font-family: var(--sans); font-size: var(--fs-caption); text-transform: uppercase; }
tr.tl-group td { color: var(--muted); font-size: var(--fs-caption); font-weight: 600; text-transform: uppercase; letter-spacing: 0.06em; padding-top: var(--sp-3); border-bottom: 0; }
tr.tone-sell .trigger-cell { color: var(--bad); }
tr.tone-trim .trigger-cell { color: var(--warn); }
tr.tone-init .trigger-cell { color: var(--ok); }
td.signal-strong { color: var(--ok); font-weight: 600; }
td.signal-medium { color: var(--warn); }
td.signal-weak { color: var(--muted); }
.synthesis-body { font-size: var(--fs-body); line-height: 1.65; }
.synthesis-body h2, .synthesis-body h3, .synthesis-body h4, .synthesis-body h5, .synthesis-body h6 { color: var(--fg); margin-top: 1.2em; margin-bottom: var(--sp-1); }
.synthesis-body h2, .synthesis-body h3 { font-size: var(--fs-title); }
.synthesis-body h4, .synthesis-body h5, .synthesis-body h6 { font-size: var(--fs-body); }
.synthesis-body strong { color: var(--fg); }
.synthesis-body code { background: var(--paper); padding: 1px var(--sp-1); border-radius: var(--radius); font-family: var(--mono); font-size: var(--fs-caption); }
.synthesis-body ul { padding-left: var(--sp-5); }
.synthesis-body li { margin-bottom: var(--sp-1); }
.synthesis-body hr { border: none; border-top: 1px solid var(--border); margin: var(--sp-4) 0; }
.reread-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(380px, 1fr)); gap: var(--sp-3); margin-top: var(--sp-2); }
.reread-card { background: var(--surface); border-radius: var(--radius); padding: var(--sp-3) var(--sp-4); }
.reread-card summary { cursor: pointer; list-style: none; display: flex; justify-content: space-between; align-items: baseline; font-size: var(--fs-title); font-weight: 600; }
.reread-card summary::-webkit-details-marker { display: none; }
.reread-card summary::before { content: '▸ '; color: var(--muted); font-family: var(--mono); }
.reread-card[open] summary::before { content: '▾ '; }
.reread-stamp { color: var(--muted); font-size: var(--fs-caption); font-family: var(--mono); font-weight: 400; }
.reread-body { font-size: var(--fs-body); line-height: 1.55; margin-top: var(--sp-3); }
.reread-body h2, .reread-body h3, .reread-body h4 { color: var(--fg); margin: var(--sp-3) 0 var(--sp-1); }
.reread-body h2 { font-size: var(--fs-title); }
.reread-body h3 { font-size: var(--fs-body); }
.reread-body strong { color: var(--fg); }
.reread-body ul { padding-left: var(--sp-4); }
.reread-body hr { border: none; border-top: 1px solid var(--border); margin: var(--sp-3) 0; }
.cli-hint { font-family: var(--mono); font-size: var(--fs-caption); padding: var(--sp-3) var(--sp-3); background: var(--paper); border-radius: var(--radius); color: var(--fg-soft); overflow-x: auto; margin: var(--sp-2) 0 0; }
.panel-h3 { font-size: var(--fs-title); margin: var(--sp-5) 0 var(--sp-2); font-weight: 600; color: var(--fg); }
.kpi-strip { display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: var(--sp-2); margin: var(--sp-2) 0 var(--sp-3); }
.kpi-card { background: var(--paper); border-radius: var(--radius); padding: var(--sp-3) var(--sp-3); text-align: center; }
.kpi-card.tone-good { border-left: 3px solid var(--ok); }
.kpi-card.tone-warn { border-left: 3px solid var(--warn); }
.kpi-card.tone-bad { border-left: 3px solid var(--bad); }
.kpi-card.tone-muted { border-left: 3px solid var(--muted); }
.kpi-label { font-size: var(--fs-caption); color: var(--muted); letter-spacing: 0.06em; text-transform: uppercase; }
.kpi-value { font-size: var(--fs-display); font-weight: 600; margin: var(--sp-1) 0; color: var(--fg); font-variant-numeric: tabular-nums; }
.kpi-sub { font-size: var(--fs-caption); color: var(--muted); font-family: var(--sans); }
.calib-strip { display: flex; flex-direction: column; gap: var(--sp-2); margin: var(--sp-2) 0 var(--sp-5); }
.calib-row { display: grid; grid-template-columns: 80px 1fr 110px; gap: var(--sp-3); align-items: center; font-size: var(--fs-caption); }
.calib-label { color: var(--muted); text-transform: uppercase; letter-spacing: 0.06em; }
.calib-bar { background: var(--paper); border-radius: var(--radius-full); min-block-size: var(--sp-3); overflow: hidden; }
.calib-fill { display: block; inline-size: 100%; block-size: var(--sp-3); accent-color: var(--ok); }
.calib-value { font-family: var(--mono); color: var(--fg-soft); text-align: right; }
.decisions-table td.outcome-correct { color: var(--ok); }
.decisions-table td.outcome-wrong { color: var(--bad); }
.decisions-table td.outcome-mixed { color: var(--warn); }
.decisions-table td.outcome-pending { color: var(--muted); }
.budget-table td code { font-family: var(--mono); font-size: var(--fs-caption); color: var(--fg); background: transparent; padding: 0; }
.budget-table .budget-cap { width: 80px; }
.burn-cell { width: 200px; padding: var(--sp-2) var(--sp-3); }
.burn-bar { width: 100%; min-block-size: var(--sp-2); background: var(--paper); border-radius: var(--radius-full); overflow: hidden; }
.burn-fill { display: block; inline-size: 100%; block-size: var(--sp-2); accent-color: var(--ok); }
.burn-ok { accent-color: var(--ok); }
.burn-warn { accent-color: var(--warn); }
.burn-over { accent-color: var(--bad); }
.block-hard { font-family: var(--mono); font-size: var(--fs-caption); color: var(--bad); font-weight: 600; }
.block-soft { font-family: var(--mono); font-size: var(--fs-caption); color: var(--muted); }
.budget-footer { margin-top: var(--sp-3); font-size: var(--fs-body); color: var(--fg-soft); }
.budget-footer strong { color: var(--fg); }
.tier-strip { background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius); padding: var(--sp-2) var(--sp-4); margin-bottom: var(--sp-4); font-size: var(--fs-body); display: flex; align-items: center; flex-wrap: wrap; gap: var(--sp-2); }
.tier-strip-label { color: var(--muted); font-size: var(--fs-caption); text-transform: uppercase; letter-spacing: 0.06em; margin-right: var(--sp-2); }
.tier-stale-count { color: var(--bad); font-weight: 600; }
.tier-stale-count-muted { color: var(--muted); font-weight: 400; }
""".strip()

COCKPIT_CSS = r"""
.cockpit-section h2 { display: flex; align-items: baseline; gap: var(--sp-2); }
.cockpit-degraded { font-size: var(--fs-caption); color: var(--warn); margin: var(--sp-1) 0 var(--sp-2); display: flex; align-items: center; gap: var(--sp-2); }
.cockpit-table sup { font-size: var(--fs-caption); color: var(--muted); margin-left: var(--sp-1); }
.cockpit-table td { white-space: nowrap; }
.cockpit-table td.kpi-moves { white-space: normal; }
.cockpit-thin td, .cockpit-thin th { padding: var(--sp-1) var(--sp-3); }
a.k-chip { text-decoration: none; }
a.k-chip:hover { color: var(--fg); border-color: var(--border-2); }
.chip-partial { border-style: dashed; }
.kpi-move { margin: var(--sp-1) var(--sp-1) var(--sp-1) 0; }
.cell-pills { display: inline-flex; gap: var(--sp-1); flex-wrap: wrap; }
.er-soon { color: var(--warn); font-weight: 600; }
.stale-dot { cursor: help; }
a.stale-dot { text-decoration: none; cursor: pointer; }
.dot-col { text-align: center; width: 28px; }
""".strip()

CSS = BASE_CSS + INBOX_CSS + UPCOMING_CSS + ACTIONS_CSS + ANALYTICAL_CSS + COCKPIT_CSS
