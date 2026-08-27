"""Single visual master for the Work OS and ticker command-center family.

Consumers own markup and behavior only.  Geometry, responsive rules, and the
family's transient-surface styling live here and are emitted into each HTML
document because some consumers render standalone documents.
"""

from __future__ import annotations

from ui.controls import controls_css
from ui.tokens import palette_css

WORK_OS_CSS = """
html { scrollbar-gutter: stable; }
html, body { min-height: 100dvh; }
body { padding-bottom: env(safe-area-inset-bottom); }
.work-os-threshold-note-title { font-weight:600; font-size:var(--fs-title); }
.work-os-threshold-note-body { font-size:var(--fs-body); color:var(--fg-soft); }
.work-os-portfolio-topline { display:grid; grid-template-columns:minmax(0, 0.75fr) minmax(0, 2.25fr); gap:var(--sp-3); align-items:stretch; }
.work-os-nav-card { min-inline-size:0; }
.work-os-nav-card-body { padding:var(--sp-3); display:grid; align-content:center; }
.work-os-allocation-list { display:grid; gap:var(--sp-half); margin-block-start:var(--sp-2); padding-block-start:var(--sp-2); border-block-start:var(--bw-thin) solid var(--hairline); }
.work-os-allocation-row { margin:0; }
.work-os-actions-rail { min-inline-size:0; }
.work-os-action-queue { display:grid; gap:var(--sp-1); }
.work-os-action-card { padding:var(--sp-1) var(--sp-2); overflow:hidden; }
.work-os-action-row { display:grid; grid-template-columns:minmax(0, 1fr) auto; align-items:center; gap:var(--sp-2); }
.work-os-action-copy { min-inline-size:0; }
.work-os-action-copy .research-actions { margin-block-start:var(--sp-1); }
.work-os-sort-button { inline-size:100%; justify-content:space-between; white-space:nowrap; }
.work-os-portfolio-table th[aria-sort="ascending"] .work-os-sort-button, .work-os-portfolio-table th[aria-sort="descending"] .work-os-sort-button { color:var(--accent); }
.work-os-threshold-link { display:inline-block; margin-block-start:var(--sp-1); color:var(--fg-soft); }
.work-os-evaluation-list { display:grid; grid-template-columns:minmax(0, 1fr) auto auto auto; gap:var(--sp-2) var(--sp-3); }
.work-os-evaluation-thread { display:grid; grid-template-columns:subgrid; grid-column:1 / -1; align-items:center; }
.work-os-evaluation-copy { display:grid; grid-template-columns:subgrid; grid-template-rows:auto auto; grid-column:1 / 4; align-items:center; row-gap:var(--sp-1); min-inline-size:0; }
.work-os-evaluation-title { grid-column:1; grid-row:1; min-inline-size:0; }
.work-os-evaluation-kind { grid-column:2; grid-row:1 / -1; align-self:center; }
.work-os-evaluation-readiness { grid-column:3; grid-row:1 / -1; align-self:center; }
.work-os-evaluation-meta { grid-column:1; grid-row:2; }
.work-os-evaluation-actions { grid-column:4; }
.work-os-section { display: flex; flex-direction: column; gap: var(--sp-2); }
.work-os-stat-head { padding: var(--sp-3); border-bottom: var(--bw-thin) solid var(--hairline); }
.k-stat-cell[data-work-os-stat-key] { position: relative; display: block; min-block-size: calc(var(--touch-target-size) + var(--sp-4)); color: inherit; text-decoration: none; }
a.k-stat-cell[data-work-os-stat-key] { cursor: pointer; }
a.k-stat-cell[data-work-os-stat-key]:hover { color: var(--fg); background: var(--paper); }
.k-stat-cell[data-work-os-stat-key]:focus-visible { z-index: 1; outline: var(--bw-thin) solid var(--accent); outline-offset: calc(var(--bw-thin) * -1); }
.k-stat-cell[data-work-os-stat-key] .stat-subtext { min-block-size: calc(var(--fs-caption) * 2); }
.ts-add-row { margin-top:var(--sp-3); display:flex; gap:var(--sp-2); align-items:center; flex-wrap:wrap; }
.ts-add-ticker { inline-size:calc(var(--grid-card-sm) / 2); }
.ts-add-label { font-size:var(--fs-body); }
.ts-add-note { font-size:var(--fs-caption); }
.k-scrim { position: fixed; inset: 0; background: var(--scrim); z-index: 250; }
.k-scrim[hidden], .drawer-scrim { display: none !important; }
.work-os-live-status { position: absolute; inline-size: var(--bw-thin); block-size: var(--bw-thin); overflow: hidden; clip: rect(0 0 0 0); }
.work-os-report-frame { width: 100%; min-height: calc(100dvh - var(--header-height) - var(--sp-6)); border: var(--bw-thin) solid var(--border); border-radius: var(--radius-card); background: var(--surface); }
.work-os-report-host { display: block; min-height: 100%; border: var(--bw-thin) solid var(--border); border-radius: var(--radius-card); background: var(--surface); overflow: hidden; }
.work-os-reader { position: fixed; inset: 0; z-index: var(--z-modal); display: flex; flex-direction: column; gap: var(--sp-3); min-height: 0; padding: var(--sp-4); background: var(--bg); overflow: hidden; }
.work-os-reader[hidden] { display: none !important; }
.work-os-reader-header { position: sticky; inset-block-start: 0; display: grid; grid-template-columns: auto minmax(0, 1fr) auto; align-items: center; gap: var(--sp-3); border-bottom: var(--bw-thin) solid var(--border); padding-bottom: var(--sp-3); background: var(--bg); }
.work-os-reader-masthead { min-width: 0; text-align: center; }
.work-os-reader-actions { display: flex; align-items: center; gap: var(--sp-2); }
.work-os-reader-decision { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: var(--sp-3); max-inline-size: var(--main-max-width); inline-size: 100%; margin-inline: auto; min-height: auto; height: auto; overflow: visible; flex-shrink: 0; }
.work-os-reader-decision > div { display: flex; flex-direction: column; gap: var(--sp-1); min-height: 0; }
.work-os-reader-layout { display: grid; grid-template-columns: var(--grid-card-sm) minmax(0, 1fr); gap: var(--sp-4); flex: 1 1 auto; min-height: 0; max-inline-size: var(--main-max-width); inline-size: 100%; margin-inline: auto; overflow: hidden; }
.work-os-reader-sections { display: flex; flex-direction: column; align-self: start; gap: var(--sp-1); max-block-size: 100%; overflow-y: auto; }
.work-os-reader-sections:empty { display: none; }
.work-os-reader-group { display: flex; flex-direction: column; gap: var(--sp-1); }
.work-os-reader-group-button { inline-size: 100%; justify-content: flex-start; }
.work-os-reader-group-sections { display: flex; flex-direction: column; gap: var(--sp-half); padding-inline-start: var(--sp-3); }
.work-os-reader-section-button { inline-size: 100%; justify-content: flex-start; }
.work-os-reader-body { flex: 1 1 auto; min-height: 0; overflow: auto; }
.work-os-detail-page { position: fixed; inset: 0; z-index: var(--z-modal); display: flex; flex-direction: column; gap: var(--sp-3); min-height: 0; padding: var(--sp-4); background: var(--bg); overflow: hidden; }
.work-os-detail-page[hidden] { display: none !important; }
.work-os-detail-page-header { position: sticky; inset-block-start: 0; display: grid; grid-template-columns: auto minmax(0, 1fr) auto; align-items: center; gap: var(--sp-3); padding-bottom: var(--sp-3); border-bottom: var(--bw-thin) solid var(--border); background: var(--bg); }
.work-os-detail-page-title { min-width: 0; overflow-wrap: anywhere; }
.work-os-detail-page-body { flex: 1 1 auto; min-height: 0; overflow: auto; max-inline-size: var(--main-max-width); inline-size: 100%; margin-inline: auto; }
.work-os-company-desk { display: flex; flex-direction: column; gap: var(--sp-3); min-height: 0; }
.work-os-company-toolbar { display: flex; justify-content: space-between; align-items: center; gap: var(--sp-3); flex-wrap: wrap; }
.work-os-company-picker { display: flex; align-items: center; gap: var(--sp-2); flex-wrap: wrap; }
.company-identity-switcher { position: relative; min-width: 0; border-radius: var(--radius); }
.company-identity-row { display: flex; align-items: center; gap: var(--sp-2); min-block-size: var(--touch-target-size); }
.company-picker-trigger { opacity: 1; transform: none; transition: opacity var(--transition), transform var(--transition); }
.company-identity-switcher:hover .company-picker-trigger, .company-identity-switcher:focus-within .company-picker-trigger { opacity: 1; transform: translateX(0); }
.company-picker-popover { position: absolute; inset-block-start: calc(100% + var(--sp-1)); inset-inline-start: 0; z-index: 220; inline-size: var(--grid-card-md); max-inline-size: calc(100vw - var(--sp-6)); box-shadow: var(--shadow-pop); }
.company-picker-popover[hidden] { display: none !important; }
.company-picker-popover input[type="search"] { inline-size: 100%; min-inline-size: 0; box-sizing: border-box; min-block-size: var(--touch-target-size); }
.company-picker-list { max-block-size: var(--grid-card-sm); overflow-y: auto; }
.company-picker-list [role="option"] { display: flex; align-items: baseline; justify-content: space-between; gap: var(--sp-3); }
.company-picker-list [aria-selected="true"] { background: var(--paper); color: var(--accent); }
.sidebar-home { min-block-size: var(--icon-button-size); }
.work-os-action-copy { display: flex; align-items: center; gap: var(--sp-3); flex: 1; }
.research-screen { display: flex; flex-direction: column; gap: var(--sp-3); min-height: 0; }
.research-toolbar, .research-actions { display: flex; flex-direction: row; justify-content: space-between; align-items: center; gap: var(--sp-3); flex-wrap: wrap; }
.research-toolbar.k-card { overflow: visible; }
.research-decision-band { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); }
.research-decision-band[data-units="8"] { grid-template-columns: repeat(auto-fit, minmax(var(--grid-card-sm), 1fr)); }
.research-grid { display: grid; grid-template-columns: minmax(0, 2fr) minmax(0, 1fr); gap: var(--sp-3); align-items: start; }
.research-list { display: flex; flex-direction: column; gap: var(--sp-2); margin-top: var(--sp-3); }
.research-question-capture { display: flex; flex-direction: column; gap: var(--sp-2); margin-top: var(--sp-3); }
.research-question-capture input { flex: 1 1 auto; min-inline-size: var(--grid-card-sm); }
.is-cited-location { background: color-mix(in srgb, var(--warn) 14%, transparent); }
.research-row { display: flex; justify-content: space-between; align-items: flex-start; gap: var(--sp-3); }
.research-library-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: var(--sp-3); }
#screen-workspace .research-grid.k-grid-split-rail-lg { grid-template-columns: minmax(0, 1fr) var(--rail-lg); }
.company-desk-approved-grid { max-inline-size: var(--main-max-width); inline-size: 100%; margin-inline: auto; }
.company-desk-topline { display: grid; grid-template-columns: minmax(0, 1fr) auto; align-items: center; gap: var(--sp-3); overflow: visible; }
.company-desk-actions { display: flex; align-items: center; justify-content: flex-end; gap: var(--sp-2); flex-wrap: wrap; }
.company-desk-facts { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); grid-column: 1 / -1; border-block-start: var(--bw-thin) solid var(--hairline); }
.company-desk-facts .k-stat-cell + .k-stat-cell { border-inline-start: var(--bw-thin) solid var(--hairline); }
.company-desk-decision-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: var(--sp-3); margin-block-end: var(--sp-3); }
.company-desk-decision-grid > div { min-inline-size: 0; }
.company-desk-tracking-grid { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: var(--sp-2); }
.tracking-band { display: flex; flex-direction: column; gap: var(--sp-1); min-inline-size: 0; border-block-start: calc(var(--bw-thin) * 2) solid var(--border); }
.tracking-band-buy, .tracking-band-add { border-block-start-color: var(--ok); }
.tracking-band-hold { border-block-start-color: var(--accent); }
.tracking-band-trim { border-block-start-color: var(--warn); }
.company-desk-tracking-note { grid-column: 1 / -1; }
.company-desk-summary-grid, .company-desk-exploration-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: var(--sp-3); align-items: start; }
.research-tabs { display: flex; gap: var(--sp-1); overflow-x: auto; border-block-end: var(--bw-thin) solid var(--hairline); margin-block-end: var(--sp-3); }
.research-tab { flex: 0 0 auto; }
.research-tab[aria-selected="true"] { color: var(--accent); box-shadow: inset 0 calc(var(--bw-thin) * -2) 0 var(--accent); }
@media (hover: none) { .company-picker-trigger { min-block-size: var(--touch-target-size); opacity: 1; transform: none; } }
@media (max-width: 47.5rem) {
  body { display: flex; min-width: 0; }
  .app-sidebar, .app-sidebar.is-collapsed { position: sticky; inset-block-start: 0; z-index: 200; width: var(--sidebar-collapsed-width); min-width: var(--sidebar-collapsed-width); height: 100dvh; min-height: 0; padding: var(--sp-2); overflow-x: hidden; overflow-y: auto; border-right: var(--bw-thin) solid var(--border); border-bottom: 0; }
  .app-sidebar > div:first-child { display: flex; flex-direction: column; align-items: stretch; gap: var(--sp-1); width: 100%; }
  .sidebar-brand { align-items: center; padding: var(--sp-2) 0; margin: 0; }
  .sidebar-collapse-toggle, .nav-layer-title, .sidebar-cmd-text, .nav-text { display: none !important; }
  .sidebar-home { width: 100%; min-block-size: var(--touch-target-size); min-inline-size: var(--touch-target-size); justify-content: center; padding: var(--sp-2); }
  .sidebar-logo { display: none; }
  .sidebar-cmd, .app-sidebar .nav-item, .app-sidebar.is-collapsed .nav-item { flex: 0 0 auto; justify-content: center; min-block-size: var(--touch-target-size); min-inline-size: var(--touch-target-size); width: 100%; margin: 0; padding: var(--sp-2); }
  .app-sidebar .nav-item::after { display: none; }
  .app-main { width: 100%; min-width: 0; padding-bottom: calc(var(--sp-4) + env(safe-area-inset-bottom)); }
  .app-header { padding-inline: var(--sp-3); }
  .main-content { width: 100%; min-width: 0; padding: var(--sp-3); }
  .screen-view, .k-card { min-width: 0; }
  .matrix-table { display: block; max-width: 100%; overflow-x: auto; }
  .work-os-portfolio-topline { grid-template-columns:minmax(0, 1fr); }
  .work-os-action-row { grid-template-columns:minmax(0, 1fr); align-items:stretch; }
  .work-os-evaluation-list { grid-template-columns:minmax(0, 1fr); }
  .work-os-evaluation-thread { grid-template-columns:minmax(0, 1fr); grid-column:1; align-items:stretch; gap:var(--sp-2); }
  .work-os-evaluation-copy { grid-template-columns:minmax(0, 1fr) auto; grid-column:1; column-gap:var(--sp-2); }
  .work-os-evaluation-title { grid-column:1; grid-row:1; }
  .work-os-evaluation-kind { grid-column:2; grid-row:1; justify-self:end; }
  .work-os-evaluation-readiness { grid-column:2; grid-row:2; justify-self:end; }
  .work-os-evaluation-meta { grid-column:1; grid-row:2; }
  .work-os-evaluation-actions { grid-column:1; }
  .k-action-row { flex-wrap: wrap; gap: var(--sp-2); }
  .screen-view [style*="grid-template-columns"] { grid-template-columns: 1fr !important; }
  .research-decision-band, .research-grid, .research-library-grid, .work-os-reader-decision, .company-desk-topline, .company-desk-facts, .company-desk-decision-grid, .company-desk-tracking-grid, .company-desk-summary-grid, .company-desk-exploration-grid { grid-template-columns: 1fr; }
  .research-decision-band .k-stat-cell, .work-os-reader-decision .k-stat-cell { border-inline-start: 0; border-block-start: var(--bw-thin) solid var(--hairline); }
  .research-decision-band .k-stat-cell:first-child, .work-os-reader-decision .k-stat-cell:first-child { border-block-start: 0; }
  .company-desk-actions { justify-content: flex-start; }
  .company-desk-facts .k-stat-cell + .k-stat-cell { border-inline-start: 0; border-block-start: var(--bw-thin) solid var(--hairline); }
  .company-desk-tracking-note { grid-column: auto; }
  .research-tab { min-block-size: var(--touch-target-size); }
  #screen-workspace .k-grid-split-rail-lg, #screen-execution-queue .k-grid-split-rail { display: block; }
  #screen-brief-library .research-toolbar { align-items: stretch; }
  .research-toolbar { flex-direction: column; align-items: stretch; }
  #screen-brief-library .research-actions { display: grid; grid-template-columns: auto minmax(0, 1fr); inline-size: 100%; }
  #screen-brief-library .research-actions .k-select { min-width: 0; inline-size: 100%; }
  .research-actions .k-chip, .research-actions .k-btn, .research-library-card .k-btn { min-block-size: var(--touch-target-size); }
  .work-os-reader { padding: var(--sp-3); }
  .work-os-detail-page { padding: var(--sp-3); }
  .work-os-detail-page-header { grid-template-columns: auto minmax(0, 1fr); }
  .work-os-detail-page-header .work-os-detail-page-close { grid-column: 1 / -1; justify-self: start; min-block-size: var(--touch-target-size); }
  .work-os-reader-layout { display: block; overflow: auto; }
  .work-os-reader-sections { display: flex; flex-direction: row; max-block-size: none; overflow-x: auto; overflow-y: visible; margin-block-end: var(--sp-3); }
  .work-os-reader-group { flex: 0 0 auto; }
  .work-os-reader-group-button, .work-os-reader-section-button { min-block-size: var(--touch-target-size); }
  .work-os-reader-body { overflow: visible; }
  .company-picker-trigger { min-block-size: var(--touch-target-size); opacity: 1; transform: none; }
  .company-picker-popover { position: fixed; inset: var(--sp-6) var(--sp-3) auto;
    inline-size: auto; max-inline-size: none; max-block-size: calc(100vh - var(--sp-6) - var(--sp-6));
    overflow-y: auto; }
  .drill-drawer { width: 100%; max-width: 100%; border-radius: 0; }
  input, select, textarea { font-size: var(--mobile-control-font-size) !important; }
}
.drill-drawer[hidden] { display: none !important; }
@media (prefers-reduced-motion: reduce) { *, *::before, *::after { animation-duration: 0s !important; transition-duration: 0s !important; scroll-behavior: auto !important; } }

"""

CC_ACTION_CSS = """
.cc-act-leave { transition: opacity var(--transition), transform var(--transition); opacity: 0; transform: translateY(var(--lift-md)); pointer-events: none; }
.cc-act-collapse { transition: height var(--transition), margin var(--transition), padding var(--transition), border-width var(--transition); height: 0 !important; min-height: 0 !important; margin: 0 !important; padding: 0 !important; border-width: 0 !important; overflow: hidden !important; }
@media (prefers-reduced-motion: reduce) { .cc-act-leave, .cc-act-collapse { transition-duration: 0.01ms; } }
"""

CC_OVERLAY_CSS = """
.cc-anim-out { transition: opacity var(--transition), transform var(--transition); opacity: 0; pointer-events: none; }
.cc-anim-out.cc-m-rise { transform: translateY(var(--sp-2)); }
.cc-anim-out.cc-m-slide-right { transform: translateX(var(--sp-3)); }
.cc-anim-out.cc-m-pop { transform: translateX(-50%) scale(0.985); }
.cc-scrim-out { transition: opacity var(--transition); opacity: 0; pointer-events: none; }
@media (prefers-reduced-motion: reduce) { .cc-anim-out, .cc-scrim-out { transition-duration: 0.01ms; } }
"""

WORK_OS_COPILOT_CSS = """
.work-os-copilot-launcher {
  position: fixed;
  inset-inline-end: calc(var(--sp-4) + env(safe-area-inset-right, 0));
  inset-block-end: calc(var(--sp-4) + env(safe-area-inset-bottom, 0));
  z-index: 230;
  gap: var(--sp-1);
  min-block-size: var(--icon-button-size);
  box-shadow: var(--shadow-pop);
  transition: opacity var(--transition);
}
.work-os-copilot-launcher .k-chip[hidden] { display: none; }
.work-os-copilot-launcher[aria-expanded="true"] { opacity: 0; pointer-events: none; }
.work-os-copilot {
  position: fixed; inset: 0 0 0 var(--sidebar-width); z-index: 240;
  display: grid; grid-template-columns: minmax(0, 0.7fr) minmax(0, 2.3fr);
  min-width: 0; min-height: 0; overflow: hidden;
  background: var(--bg); color: var(--fg);
  border-inline-start: var(--bw-thin) solid var(--border);
}
.work-os-copilot[hidden] { display: none; }
.work-os-copilot[data-mode="fullscreen"] { inset-inline-start: 0; }
.work-os-copilot-history {
  display: flex; flex-direction: column; min-width: 0; min-height: 0;
  overflow: hidden; background: var(--paper);
  border-inline-end: var(--bw-thin) solid var(--border);
}
.work-os-copilot-history-head,
.work-os-copilot-toolbar,
.work-os-copilot-filter-row,
.work-os-copilot-composer-actions,
.work-os-copilot-evidence-head,
.work-os-copilot-proposal-head {
  display: flex; align-items: center; gap: var(--sp-2);
}
.work-os-copilot-history-head,
.work-os-copilot-toolbar,
.work-os-copilot-evidence-head {
  justify-content: space-between; padding: var(--sp-3);
  border-bottom: var(--bw-thin) solid var(--border);
}
.work-os-copilot-history-head .k-btn { flex: 0 0 auto; }
.work-os-copilot-history-search { min-width: 0; flex: 1; }
.work-os-copilot-filter-stack {
  display: flex; flex-direction: column; gap: var(--sp-2); padding: var(--sp-2) var(--sp-3);
  border-bottom: var(--bw-thin) solid var(--border);
}
.work-os-copilot-filter-row { flex-wrap: wrap; }
.work-os-copilot-filter-row .k-select { min-width: 0; flex: 1; }
.work-os-copilot-filter-context {
  display: grid; grid-template-columns: auto minmax(0, 1fr) auto minmax(0, 1fr);
  align-items: center; gap: var(--sp-1);
}
.work-os-copilot-filter-context .k-label { white-space: nowrap; }
.work-os-copilot-filter-context .k-select { width: 100%; }
.work-os-copilot-sessions {
  display: flex; flex-direction: column; gap: var(--sp-1); min-width: 0;
  min-height: 0; overflow-x: hidden; overflow-y: auto; padding: var(--sp-2);
}
.work-os-copilot-session {
  display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: var(--sp-1);
  align-items: center; min-width: 0;
}
.work-os-copilot-session-main { justify-content: flex-start; min-width: 0; text-align: start; }
.work-os-copilot-session-copy { display: flex; flex-direction: column; min-width: 0; }
.work-os-copilot-session-title,
.work-os-copilot-session-meta { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.work-os-copilot-session-title { color: var(--fg); font-size: var(--fs-body); }
.work-os-copilot-session-meta { color: var(--muted); font-size: var(--fs-nano); }
.work-os-copilot-session-actions { display: flex; gap: var(--sp-half); }
.work-os-copilot-main {
  position: relative; display: grid; grid-template-rows: auto minmax(0, 1fr) auto;
  min-width: 0; min-height: 0; overflow: hidden;
}
.work-os-copilot-heading { min-width: 0; flex: 1; }
.work-os-copilot-title { color: var(--fg); font-size: var(--fs-title); font-weight: 600; }
.work-os-copilot-subtitle { color: var(--muted); font-size: var(--fs-caption); }
.work-os-copilot-toolbar-actions { display: flex; gap: var(--sp-1); }
.work-os-copilot-thread {
  display: flex; flex-direction: column; gap: var(--sp-3); min-width: 0;
  min-height: 0; overflow-x: hidden; overflow-y: auto; padding: var(--sp-4);
}
.work-os-copilot-turn { max-width: 100%; min-width: 0; }
.work-os-copilot-turn[data-role="user"] { align-self: flex-end; }
.work-os-copilot-turn[data-role="user"] .k-well { background: var(--accent-soft); }
.stat-subtext[data-tone="bad"] { color: var(--bad); }
.work-os-copilot-turn-copy { color: var(--fg-soft); white-space: pre-wrap; overflow-wrap: anywhere; }
.work-os-copilot-stage { color: var(--muted); font-size: var(--fs-caption); }
.work-os-copilot-fragment { max-width: 100%; overflow-x: auto; }
.work-os-copilot-citations { display: flex; flex-wrap: wrap; gap: var(--sp-1); margin-top: var(--sp-2); }
.work-os-copilot-suggestions { display: flex; flex-wrap: wrap; gap: var(--sp-1); }
.work-os-copilot-composer {
  display: flex; flex-direction: column; gap: var(--sp-2); padding: var(--sp-3) var(--sp-4);
  border-top: var(--bw-thin) solid var(--border); background: var(--paper);
}
.work-os-copilot-composer textarea { width: 100%; min-height: calc(var(--sp-6) + var(--sp-5)); resize: vertical; }
.work-os-copilot-composer-actions { justify-content: space-between; flex-wrap: wrap; }
.work-os-copilot-context { display: flex; align-items: center; gap: var(--sp-1); flex-wrap: wrap; }
.work-os-copilot-evidence {
  position: absolute; inset: 0 0 0 auto; z-index: 1; width: min(100%, calc(var(--sidebar-width) * 2));
  display: grid; grid-template-rows: auto minmax(0, 1fr); min-width: 0; min-height: 0;
  overflow: hidden; background: var(--surface); border-inline-start: var(--bw-thin) solid var(--border);
  box-shadow: var(--shadow-drawer);
}
.work-os-copilot-evidence[hidden] { display: none; }
.work-os-copilot-evidence-body {
  display: flex; flex-direction: column; gap: var(--sp-2); min-width: 0;
  overflow-x: hidden; overflow-y: auto; padding: var(--sp-3);
}
.work-os-copilot-source { display: flex; flex-direction: column; gap: var(--sp-1); }
.work-os-copilot-source-actions { display: flex; flex-wrap: wrap; gap: var(--sp-1); }
.work-os-copilot-proposal { display: flex; flex-direction: column; gap: var(--sp-2); }
.work-os-copilot-proposal-head { justify-content: space-between; flex-wrap: wrap; }
.work-os-copilot-proposal-action { margin-inline-start: auto; }
.work-os-copilot-proposal-actions { margin-inline-start: auto; display: flex; align-items: center; justify-content: flex-end; gap: var(--sp-1); flex-wrap: wrap; }
.work-os-copilot-proposal-status { flex: 1 1 auto; }
.work-os-copilot-proposal [data-proposal-body] { display: flex; flex-direction: column; gap: var(--sp-2); }
.work-os-copilot-proposal-grid {
  display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); gap: var(--sp-2);
}
.work-os-copilot-proposal-cell { min-width: 0; overflow-wrap: anywhere; }
.work-os-copilot-proposal-kv { display: grid; grid-template-columns: auto minmax(0, 1fr); gap: var(--sp-1) var(--sp-2); }
.work-os-copilot-kpi-changes { display: flex; flex-direction: column; min-width: 0; }
.work-os-copilot-kpi-row {
  display: grid; grid-template-columns: minmax(0, 1.2fr) minmax(0, 1fr) minmax(0, 1fr);
  gap: var(--sp-2); min-width: 0; padding: var(--sp-1) 0;
  border-top: var(--bw-thin) solid var(--border);
}
.work-os-copilot-kpi-row > * { min-width: 0; overflow-wrap: anywhere; }
.work-os-copilot-proposal-error { display: flex; flex-direction: column; gap: var(--sp-1); }
@media (max-width: 47.5rem) {
  .work-os-copilot,
  .work-os-copilot[data-mode="fullscreen"] {
    inset: 0; grid-template-columns: 1fr; grid-template-rows: minmax(0, 0.85fr) minmax(0, 2.15fr);
  }
  .work-os-copilot-history { border-inline-end: 0; border-bottom: var(--bw-thin) solid var(--border); }
  .work-os-copilot-history-head,
  .work-os-copilot-toolbar,
  .work-os-copilot-filter-stack,
  .work-os-copilot-thread,
  .work-os-copilot-composer { padding: var(--sp-2); }
  .work-os-copilot .k-btn,
  .work-os-copilot .k-chip,
  .work-os-copilot input,
  .work-os-copilot select,
  .work-os-copilot textarea { min-block-size: var(--touch-target-size); }
  .work-os-copilot input,
  .work-os-copilot select,
  .work-os-copilot textarea { font-size: var(--mobile-control-font-size); }
  #workOsCopilotFullscreen { display: none; }
  .work-os-copilot-evidence { width: 100%; }
}
@media (prefers-reduced-motion: reduce) {
  .work-os-copilot, .work-os-copilot * { scroll-behavior: auto; transition: none; animation: none; }
}

"""

TCC_QUICK_NOTE_STYLE = """<style>
.cc-quicknote .qn-row { display: flex; gap: var(--sp-2); align-items: center; margin-bottom: var(--sp-2); }
.cc-quicknote textarea { width: 100%; box-sizing: border-box; resize: vertical; margin-bottom: var(--sp-2); }
.cc-quicknote .qn-ticker { width: var(--ticker-width); text-transform: uppercase; }
.cc-quicknote .qn-msg, .cc-quicknote .qn-musing-label, .cc-notes-foot { font-size: var(--fs-caption); }
.cc-quicknote .qn-musing-label { display: flex; align-items: center; gap: var(--sp-1); color: var(--muted); cursor: pointer; }
</style>"""

TCC_COMBO_STYLE = """<style>
.cc-combo { position: relative; flex: 1 1 var(--grid-card-md); max-width: var(--grid-card-lg); min-width: var(--grid-card-sm); }
.cc-combo .cc-combo-input { width: 100%; box-sizing: border-box; padding: var(--sp-1) var(--sp-3); font-family: var(--mono); font-weight: 600; letter-spacing: 0.02em; }
.cc-combo .cc-combo-input::placeholder { font-family: var(--sans); font-weight: 400; letter-spacing: normal; }
.cc-combo-name { position: absolute; right: var(--sp-3); top: 50%; transform: translateY(-50%); color: var(--muted); font-size: var(--fs-caption); max-width: 58%; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; pointer-events: none; }
.cc-combo:focus-within .cc-combo-name { display: none; }
.cc-combo-list { position: absolute; z-index: 25; top: 100%; left: 0; right: 0; margin: 0; padding: var(--sp-1) 0; list-style: none; max-height: var(--grid-card-sm); overflow-y: auto; background: var(--surface); border: var(--bw-thin) solid var(--accent); border-top: none; border-radius: 0 0 var(--radius) var(--radius); }
.cc-combo-list[hidden] { display: none; }
.cc-combo .cc-combo-input[aria-expanded="true"] { border-bottom-left-radius: 0; border-bottom-right-radius: 0; }
.cc-combo-list li { display: flex; align-items: baseline; gap: var(--sp-2); padding: var(--sp-1) var(--sp-3); cursor: pointer; font-size: var(--fs-body); }
.cc-combo-list li.sel, .cc-combo-list li:hover { background: var(--paper); }
.cc-combo-tk { font-family: var(--mono); font-weight: 600; color: var(--fg); }
.cc-combo-nm, .cc-combo-none, .cc-holding-hint { color: var(--muted); font-size: var(--fs-caption); }
.cc-combo-nm { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.cc-holding-right { display: flex; align-items: center; gap: var(--sp-2); flex-wrap: wrap; }
</style>"""

TCC_DISCLOSURE_STYLE = """<style>
.disclosure-strip { margin: var(--sp-2) 0; }
.disclosure-head, .disclosure-row-head { display: flex; align-items: center; gap: var(--sp-2); flex-wrap: wrap; }
.disclosure-head { justify-content: space-between; }
.disclosure-head h2 { margin: 0; }
.disclosure-rows { display: grid; gap: var(--sp-2); margin-top: var(--sp-2); }
.disclosure-row { display: grid; gap: var(--sp-1); padding-top: var(--sp-2); border-top: var(--bw-thin) solid var(--border); }
.disclosure-row-head strong { margin-right: auto; }
.disclosure-receipt, .disclosure-gate, .disclosure-interpretation p { margin: 0; }
.disclosure-receipt { color: var(--fg); }
</style>"""

TCC_YOU_SAID_STYLE = """<style>
.tcc-yousaid { margin: var(--sp-1) 0 var(--sp-2); font-size: var(--fs-body); }
.tcc-yousaid .ys-line { display: flex; flex-wrap: wrap; align-items: baseline; gap: var(--sp-2); }
.tcc-yousaid .k-empty { padding: 0; }
</style>"""

TCC_DRAWER_STYLE = """<style>
.cc-holding-head { display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: var(--sp-3); min-height: var(--touch-target-size); margin-bottom: var(--sp-3); padding-bottom: var(--sp-2); border-bottom: var(--bw-thin) solid var(--border); }
.cc-fdot { cursor: help; margin-left: var(--sp-1); }
a.cc-fdot { text-decoration: none; cursor: pointer; }
.tcc-report-main .cc-report-frame { height: calc(100dvh - 200px); }
.tcc-drawer-scrim { position: fixed; inset: 0; background: var(--scrim); z-index: 34; animation: cc-fade-in var(--transition); }
.tcc-drawer { position: fixed; inset-block: 0; inset-inline-end: 0; width: min(var(--drawer-width), 94vw); background: var(--bg); border-left: var(--bw-thin) solid var(--border); z-index: 35; display: flex; flex-direction: column; box-shadow: var(--shadow-pop); animation: cc-slide-in-right var(--transition); }
.tcc-drawer[hidden], .tcc-drawer-scrim[hidden] { display: none; }
.tcc-drawer-close { background: transparent; border: none; color: var(--muted); font-size: var(--fs-display); cursor: pointer; line-height: 1; padding: var(--sp-half) var(--sp-1); transition: color var(--transition); }
.tcc-drawer-close:hover { color: var(--fg); }
.tcc-drawer-body { overflow-y: auto; padding: var(--sp-3) var(--sp-4) var(--sp-6); }
@keyframes cc-slide-in-right { from { transform: translateX(var(--sp-3)); opacity: 0; } to { transform: none; opacity: 1; } }
@keyframes cc-fade-in { from { opacity: 0; } to { opacity: 1; } }
</style>"""

TCC_PAGE_CSS = """<style>
body { margin: 0; padding: var(--sp-5); font-family: var(--sans); background: var(--bg); color: var(--fg); line-height: 1.5; font-size: var(--fs-body); }
.tcc-refresh-note { margin-top:var(--sp-2); }
.tcc-dcfsheets-open[hidden], .stamp[hidden] { display:none; }
.cc-report-embed { padding:var(--sp-1) var(--sp-1); }
header { margin-bottom: var(--sp-3); border-bottom: var(--bw-thin) solid var(--border); padding-bottom: var(--sp-2); display: flex; justify-content: space-between; align-items: flex-start; }
h1 { font-size: var(--fs-display); margin: 0 0 var(--sp-1); font-weight: 600; }
h1 .k-tick-name { font-size: var(--fs-body); }
h2 { font-size: var(--fs-title); margin: 0 0 var(--sp-2); font-weight: 600; }
a { transition: color var(--transition); }
.top-nav { font-size: var(--fs-caption); }
.top-nav a { color: var(--accent); text-decoration: none; }
.top-nav a:hover { text-decoration: underline; }
.badges { text-align: right; }
.panel { margin-bottom: var(--sp-4); background: var(--surface); border-radius: var(--radius); padding: var(--sp-3) var(--sp-4); }
.panel .sub { color: var(--muted); font-size: var(--fs-caption); margin: 0 0 var(--sp-3); }
.panel-h3 { font-size: var(--fs-title); margin: var(--sp-4) 0 var(--sp-2); color: var(--fg); font-weight: 600; }
.muted { color: var(--muted); }
table { width: 100%; border-collapse: collapse; font-size: var(--fs-body); font-variant-numeric: tabular-nums; }
th { text-align: left; padding: var(--sp-1) var(--sp-2); border-bottom: var(--bw-thin) solid var(--border); font-size: var(--fs-caption); text-transform: uppercase; letter-spacing: 0.06em; color: var(--muted); font-weight: 600; }
td { padding: var(--sp-1) var(--sp-2); border-bottom: var(--bw-thin) solid var(--hairline); vertical-align: top; }
tbody tr:hover td { background: var(--paper); }
td.num { text-align: right; }
code { font-family: var(--mono); font-size: var(--fs-body); color: var(--fg-soft); }
.fresh-strip { display: flex; gap: var(--sp-half); margin-bottom: var(--sp-4); background: var(--border); border-radius: var(--radius); overflow: hidden; }
.fresh-cell { background: var(--surface); padding: var(--sp-2) var(--sp-3); flex: 1; }
.fresh-label { font-size: var(--fs-caption); text-transform: uppercase; letter-spacing: 0.06em; color: var(--muted); }
.fresh-val { font-size: var(--fs-body); font-variant-numeric: tabular-nums; }
.kpi-strip { display: grid; grid-template-columns: repeat(auto-fit, minmax(var(--grid-card-sm), 1fr)); gap: var(--sp-half); margin-bottom: var(--sp-3); background: var(--border); border-radius: var(--radius); overflow: hidden; }
.kpi-card { background: var(--surface); padding: var(--sp-2) var(--sp-3); }
.kpi-label { font-size: var(--fs-caption); text-transform: uppercase; letter-spacing: 0.06em; color: var(--muted); }
.kpi-value { font-size: var(--fs-title); font-weight: 600; margin-top: var(--sp-half); font-variant-numeric: tabular-nums; }
ul { margin: var(--sp-1) 0; padding-left: var(--sp-5); }
li { margin-bottom: var(--sp-half); }
</style>"""


def work_os_style_block(*, theme: str = "dark") -> str:
    """Return the family style block, including canonical palette and kit."""
    return (
        '<style id="work-os-family-css">'
        + palette_css(theme)
        + controls_css(theme)
        + WORK_OS_CSS
        + "</style>"
    )


__all__ = [
    "CC_ACTION_CSS",
    "CC_OVERLAY_CSS",
    "TCC_COMBO_STYLE",
    "TCC_DISCLOSURE_STYLE",
    "TCC_DRAWER_STYLE",
    "TCC_PAGE_CSS",
    "TCC_QUICK_NOTE_STYLE",
    "TCC_YOU_SAID_STYLE",
    "WORK_OS_COPILOT_CSS",
    "WORK_OS_CSS",
    "work_os_style_block",
]
