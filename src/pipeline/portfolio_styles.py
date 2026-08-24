"""Visual master for the Portfolio panel family.

Consumers in this family own data, markup, and JavaScript hooks.  All visual
CSS is composed here so palette, type, spacing, geometry, and control choices
remain reviewable in one place.
"""

from __future__ import annotations

from ui.controls import controls_css
from ui.tokens import palette_css


def _block(css: str) -> str:
    return f"<style>{css.strip()}</style>"


_PORTFOLIO = r"""
.pf-excluded-note { margin-top:var(--sp-2); }
.pf-start-log { display:block; max-block-size:var(--grid-card-sm); overflow:auto; }
.pf-start-log[hidden] { display:none; }
.pf-tracker-banner { border-left: var(--bw-thick) solid var(--warn); }
.pf-tracker-actions { display:flex; align-items:center; gap:var(--sp-3); flex-wrap:wrap; margin:var(--sp-3) 0 0; }
.pf-legend { display:flex; gap:var(--sp-5); flex-wrap:wrap; margin:var(--sp-half) 0 var(--sp-3); font-size:var(--fs-body); }
.pf-chip { display:inline-flex; align-items:center; gap:var(--sp-1); color:var(--muted); }
.pf-chip strong { color:var(--fg); font-variant-numeric:tabular-nums; }
.pf-swatch { width:var(--sp-2); height:var(--sp-2); border-radius:var(--radius); display:inline-block; }
.pf-swatch-portfolio { background:var(--fg); }.pf-swatch-spy { background:var(--series-spy); }.pf-swatch-qqq { background:var(--series-qqq); }.pf-swatch-policy { background:var(--series-policy); }
.pf-chart { width:100%; height:auto; display:block; }
.pf-chart .pf-grid { fill:none; stroke:var(--border); stroke-width:var(--bw-thin); stroke-dasharray:2 3; }
.pf-chart .pf-grid-zero { fill:none; stroke:var(--border-2); stroke-width:var(--bw-thin); }
.pf-chart .pf-axis-label { fill:var(--muted); font-family:var(--mono); font-size:var(--fs-caption); }
.pf-chart .pf-axis-start { text-anchor:start; }.pf-chart .pf-axis-middle { text-anchor:middle; }.pf-chart .pf-axis-end { text-anchor:end; }
.pf-chart .pf-series { fill:none; stroke-linejoin:round; stroke-linecap:round; }
.pf-chart .pf-series-portfolio { stroke:var(--fg); stroke-width:var(--bw-thick); }.pf-chart .pf-series-spy { stroke:var(--series-spy); stroke-width:var(--bw-thin); }.pf-chart .pf-series-qqq { stroke:var(--series-qqq); stroke-width:var(--bw-thin); }.pf-chart .pf-series-policy { stroke:var(--series-policy); stroke-width:var(--bw-thin); }
.pf-policy { font-size:var(--fs-caption); margin:var(--sp-3) 0 0; }
.pf-warn { color:var(--warn); }
.pf-alloc-grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(var(--grid-card-lg),1fr)); gap:var(--sp-1) var(--sp-6); margin-top:var(--sp-1); }
.pf-alloc-row { display:grid; grid-template-columns:minmax(var(--grid-card-sm),1.3fr) 2fr var(--grid-card-sm) var(--grid-card-sm); gap:var(--sp-1); align-items:center; font-size:var(--fs-body); padding:var(--sp-half) 0; }
.pf-alloc-label { overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.pf-bar,.pf-exp-bar,.pf-nd-bar { background:var(--hairline); border-radius:var(--radius-full); height:var(--bar-track-height); overflow:hidden; }
.pf-bar-fill,.pf-exp-bar span,.pf-nd-bar span { background:var(--accent); height:100%; display:block; border-radius:var(--radius-full); }
.pf-bar-fill { opacity:.75; }
.pf-alloc-pct,.pf-alloc-val,.pf-exp-pct,.pf-nd-alloc,.pf-nd-now { text-align:right; font-variant-numeric:tabular-nums; }
.pf-alloc-val,.pf-nd-now { font-size:var(--fs-caption); }
.pf-flag { color:var(--warn); margin-left:var(--sp-1); cursor:help; }
.pf-total td { font-weight:600; border-top:var(--bw-thick) solid var(--border); }
.pf-degraded,.pf-nd-note,.pf-nd-hint { font-size:var(--fs-caption); }
.pf-alpha-details { margin-top:var(--sp-2); }
.pf-alpha-details>summary { cursor:pointer; color:var(--fg); font-weight:600; padding:var(--sp-2) 0; }
.performance-risk-panel { display:grid; gap:var(--sp-4); }
.pr-secondary-grid { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:var(--sp-4); }
.pr-secondary-grid > .panel { margin:0; }
.pr-allocation-rows { display:grid; gap:var(--sp-2); }
.pr-allocation-row { display:grid; grid-template-columns:minmax(var(--grid-card-sm),1fr) minmax(var(--grid-card-sm),2fr) var(--grid-card-sm); gap:var(--sp-2); align-items:center; }
.pr-allocation-row strong { text-align:right; font-family:var(--mono); font-variant-numeric:tabular-nums; }
.pr-allocation-track { block-size:var(--bar-track-height); background:var(--hairline); border-radius:var(--radius-full); overflow:hidden; }
.pr-allocation-track span { display:block; block-size:100%; border-radius:inherit; background:var(--accent); }
.pr-risk [role=tabpanel] { margin-top:var(--sp-3); }
.pr-policy-editor { display:grid; gap:var(--sp-2); }.pr-policy-editor h2 { margin:0; }.pr-policy-editor .sub { margin:0; }.pr-policy-grid { display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:var(--sp-2); }.pr-policy-row { display:grid; gap:var(--sp-half); color:var(--muted); font-size:var(--fs-caption); }.pr-policy-row input { box-sizing:border-box; width:100%; min-block-size:var(--touch-target-size); padding:var(--sp-half) var(--sp-1); font-family:var(--mono); font-size:var(--fs-body); }.pr-policy-actions { display:flex; flex-wrap:wrap; align-items:center; gap:var(--sp-2); }.pr-policy-status { margin:0; color:var(--muted); font-size:var(--fs-caption); }.pr-policy-status[data-tone=pending] { color:var(--warn); }.pr-policy-status[data-tone=error] { color:var(--bad); }.pr-policy-status[data-tone=success] { color:var(--ok); }
@media (max-width:700px) { .pr-secondary-grid { grid-template-columns:1fr; }.pr-allocation-row { grid-template-columns:minmax(0,1fr) minmax(var(--grid-card-sm),1.4fr) var(--grid-card-sm); }.pf-window input[type=date],.pr-policy-row input { font-size:var(--mobile-control-font-size); }.pr-policy-grid { grid-template-columns:repeat(2,minmax(0,1fr)); } }
.pf-perf-head { display:flex; align-items:flex-start; justify-content:space-between; gap:var(--sp-1) var(--sp-4); flex-wrap:wrap; margin-bottom:var(--sp-4); }
.pf-perf-head h2 { margin:0; }
.pf-window { display:flex; align-items:center; gap:var(--sp-1); flex-wrap:wrap; font-size:var(--fs-caption); }
.pf-window-standalone { margin-bottom:var(--sp-4); }
.pf-window-label { font-size:var(--fs-caption); text-transform:uppercase; letter-spacing:.06em; color:var(--muted); margin-right:var(--sp-half); }
.pf-window input[type=date] { padding:var(--sp-half) var(--sp-1); font-size:var(--fs-caption); font-family:var(--mono); }
.pf-backfill-label { color:var(--muted); display:inline-flex; align-items:center; gap:var(--sp-half); margin-left:var(--sp-1); cursor:help; }
.pf-info { position:relative; display:inline-flex; align-items:center; justify-content:center; width:var(--icon-size); height:var(--icon-size); border-radius:var(--radius-full); border:var(--bw-thin) solid var(--border); color:var(--muted); font-size:var(--fs-caption); font-weight:600; cursor:help; margin-left:var(--sp-1); vertical-align:middle; transition:color var(--transition),border-color var(--transition); }
.pf-info:hover,.pf-info:focus { color:var(--fg); border-color:var(--border-2); outline:none; }
.pf-info-pop { position:absolute; top:calc(100% + var(--sp-1)); left:0; z-index:5; width:var(--grid-card-lg); background:var(--surface); border:var(--bw-thin) solid var(--border); border-radius:var(--radius); box-shadow:var(--shadow-pop); padding:var(--sp-2) var(--sp-3); font-size:var(--fs-caption); font-style:normal; font-weight:400; line-height:1.5; color:var(--muted); white-space:normal; display:none; }
.pf-info:hover .pf-info-pop,.pf-info:focus .pf-info-pop,.pf-info:focus-within .pf-info-pop { display:block; }
.pf-insights { display:grid; grid-template-columns:repeat(auto-fit,minmax(var(--grid-card-lg),1fr)); gap:0 var(--sp-4); align-items:start; }
.pf-th-chips { display:flex; gap:var(--sp-2); flex-wrap:wrap; }
.pf-exp-row { display:grid; grid-template-columns:minmax(var(--grid-card-sm),1fr) 2fr var(--grid-card-sm); gap:var(--sp-1); align-items:center; font-size:var(--fs-body); padding:var(--sp-half) 0; }
.pf-exp-label { overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.pf-exp-pct { color:var(--muted); }
.pf-nd-excerpt { font-size:var(--fs-body); line-height:1.55; }
.pf-nd-item { border-radius:var(--radius); }
.pf-nd-item:hover,.pf-nd-item:focus-within { background:var(--surface); }
.pf-nd-row { display:grid; grid-template-columns:var(--grid-card-sm) 1fr var(--grid-card-sm) var(--grid-card-sm); gap:var(--sp-1); align-items:center; font-size:var(--fs-body); padding:var(--sp-half) 0; }
.pf-nd-ticker { font-family:var(--mono); }
.pf-nd-wf { display:none; gap:var(--sp-1); flex-wrap:wrap; padding:var(--sp-half) 0 var(--sp-2); }
.pf-nd-item:hover .pf-nd-wf,.pf-nd-item:focus-within .pf-nd-wf { display:flex; }
.pf-nd-memo-h { margin-top:var(--sp-3); }
.pfr-bets ol { margin:var(--sp-1) 0 0 var(--sp-5); padding:0; }
.pfr-bets li { margin:0 0 var(--sp-2); font-size:var(--fs-body); line-height:1.5; }
.pfr-bets .pfr-bet-nums,.pfr-top,.pts-excluded,.pfc-diag,.ptc-finding-rationale { color:var(--muted); font-size:var(--fs-caption); }
.pfr-uw { width:100%; height:auto; display:block; margin-top:var(--sp-1); }
.pfr-uw .pfr-grid { fill:none; stroke:var(--border); stroke-width:var(--bw-thin); stroke-dasharray:2 3; }.pfr-uw .pfr-axis-label { fill:var(--muted); font-family:var(--mono); font-size:var(--fs-caption); }.pfr-uw .pfr-axis-start { text-anchor:start; }.pfr-uw .pfr-axis-middle { text-anchor:middle; }.pfr-uw .pfr-axis-end { text-anchor:end; }.pfr-uw .pfr-area { fill:var(--bad); fill-opacity:.16; stroke:none; }.pfr-uw .pfr-line { fill:none; stroke:var(--bad); stroke-width:var(--bw-thick); stroke-linejoin:round; stroke-linecap:round; }
.pfr-tops { margin-top:var(--sp-2); }
.pfr-run { display:flex; align-items:center; gap:var(--sp-1); flex-wrap:wrap; margin:var(--sp-1) 0 var(--sp-3); }
.pfr-run select { font-size:var(--fs-body); }
.pfr-log { display:none; max-height:var(--grid-card-sm); overflow:auto; margin:0 0 var(--sp-3); }
.pfc-scroll { overflow-x:auto; margin-top:var(--sp-2); }
.pfc-table { border-collapse:collapse; font-family:var(--mono); font-size:var(--fs-caption); }
.pfc-table th { font-weight:600; color:var(--muted); padding:var(--sp-half) var(--sp-1); text-align:center; }
.pfc-table th.pfc-row-h { text-align:right; }
.pfc-cell { padding:var(--sp-half) var(--sp-1); text-align:center; font-variant-numeric:tabular-nums; min-width:var(--ticker-width); }
.pfc-c1 { background:color-mix(in srgb,var(--warn) 10%,transparent); }.pfc-c2 { background:color-mix(in srgb,var(--warn) 24%,transparent); }.pfc-c3 { background:color-mix(in srgb,var(--bad) 30%,transparent); }.pfc-neg { background:color-mix(in srgb,var(--ok) 14%,transparent); }
.pfc-clusters { display:flex; flex-direction:column; gap:var(--sp-1); margin:var(--sp-2) 0 0; }.pts-table,.pfm-table,.rrg-table { margin-top:var(--sp-1); }.pfm-table { max-width:var(--grid-card-lg); }
.pfr-coverage-warn { font-size:var(--fs-body); margin:var(--sp-2) 0; }
.ptc-findings { display:flex; flex-direction:column; gap:var(--sp-2); margin-top:var(--sp-2); }.ptc-finding { border-left:var(--bw-thick) solid var(--warn); padding-left:var(--sp-2); }.ptc-finding-bad { border-left-color:var(--bad); }.ptc-finding-head { font-size:var(--fs-body); }
.rrg-mismatch { max-width:var(--grid-card-lg); }.rrg-chips { display:inline-flex; gap:var(--sp-1); flex-wrap:wrap; vertical-align:middle; }.rrg-score { margin-right:var(--sp-2); }.pfr-naked-chips { display:flex; flex-wrap:wrap; gap:var(--sp-1); margin:var(--sp-2) 0 var(--sp-3); }
"""

_CONSOLE = r"""
.console-grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(var(--grid-card-lg),1fr)); gap:var(--sp-2); align-items:start; }
.console-grid .console-sec,.hc-card { min-width:0; }.console-grid .console-sec.csec-wide { grid-column:1/-1; }
.console-sec>.panel { margin:0; padding:0; background:transparent; border:0; border-radius:0; box-shadow:none; }
.console-brief .cb-sub { color:var(--muted); font-size:var(--fs-caption); margin:0 0 var(--sp-1); }.console-brief p.cb-line { margin:0 0 var(--sp-half); font-size:var(--fs-body); line-height:1.5; }.console-brief .cb-links { display:flex; flex-wrap:wrap; gap:var(--sp-1); margin-top:var(--sp-2); }.hc-h { margin:0; }.hc-tabs { display:flex; flex-wrap:wrap; gap:var(--sp-1); margin:0 0 var(--sp-2); }.hc-pane[hidden] { display:none; }
@media (max-width:47.5rem) { .console-grid { grid-template-columns:minmax(0,1fr); }.console-sec { min-width:0; max-width:100%; } }
"""

_POSITIONING = r"""
.pos-intent-quote { margin-top:var(--sp-2); }
.pos-grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(var(--grid-card-lg),1fr)); gap:var(--sp-4); align-items:start; }.pos-grid .pos-span { grid-column:1/-1; }.pos-dim { display:flex; gap:var(--sp-1); align-items:baseline; padding:var(--sp-1) 0; border-bottom:var(--bw-thin) dashed var(--border); font-size:var(--fs-body); }.pos-dim:last-child { border-bottom:none; }.pos-dim .lbl { min-width:var(--grid-card-sm); color:var(--muted); }.pos-dim .val { font-family:var(--mono); }.pos-hist { font-size:var(--fs-body); }.pos-hist td { padding:var(--sp-1) var(--sp-2) var(--sp-1) 0; vertical-align:top; }.pos-chat-log { max-height:var(--grid-card-lg); overflow-y:auto; display:flex; flex-direction:column; gap:var(--sp-2); padding:var(--sp-1) 0; }.pos-msg { padding:var(--sp-2) var(--sp-3); border-radius:var(--radius); font-size:var(--fs-body); line-height:1.5; white-space:pre-wrap; }.pos-msg.user { background:var(--surface); border:var(--bw-thin) solid var(--border); align-self:flex-end; max-width:85%; }.pos-msg.coach { background:var(--paper); border:var(--bw-thin) solid var(--border); max-width:95%; }.pos-chat-form { display:flex; gap:var(--sp-2); margin-top:var(--sp-2); }.pos-chat-form textarea,.pos-form-grid input,.pos-form-grid select,.pos-narrative { background:var(--paper); color:var(--fg); border:var(--bw-thin) solid var(--border); border-radius:var(--radius); padding:var(--sp-1) var(--sp-2); font-size:var(--fs-body); box-sizing:border-box; }.pos-chat-form textarea { flex:1; min-height:var(--grid-card-sm); resize:vertical; font-family:inherit; }.pos-form-grid { display:grid; grid-template-columns:var(--grid-card-sm) 1fr 1fr; gap:var(--sp-1) var(--sp-2); align-items:center; font-size:var(--fs-body); }.pos-form-grid .hdr { color:var(--muted); font-size:var(--fs-caption); }.pos-form-grid input,.pos-form-grid select,.pos-narrative { width:100%; font-family:var(--mono); }.pos-narrative { font-family:inherit; min-height:var(--grid-card-sm); margin-top:var(--sp-1); }.pos-diff { font-size:var(--fs-caption); color:var(--muted); font-family:var(--mono); }.pos-degraded { margin-top:var(--sp-2); font-size:var(--fs-caption); color:var(--muted); display:flex; flex-wrap:wrap; align-items:baseline; gap:var(--sp-1); }.pos-degraded details { flex-basis:100%; }.pos-degraded summary { cursor:pointer; }.pos-degraded ul { margin:var(--sp-1) 0 0 var(--sp-4); font-family:var(--mono); }.pos-error { color:var(--bad); font-size:var(--fs-body); margin-top:var(--sp-2); white-space:pre-wrap; }.pos-actions { display:flex; gap:var(--sp-2); margin-top:var(--sp-3); align-items:center; }
"""

_LIFECYCLE = r"""
.plc-timeline { list-style:none; margin:var(--sp-2) 0 0; padding:0; }.plc-timeline li { position:relative; padding:0 0 var(--sp-4) var(--sp-5); border-left:var(--bw-thick) solid var(--border); margin-left:var(--sp-1); }.plc-timeline li:last-child { padding-bottom:var(--sp-half); }.plc-timeline li::before { content:""; position:absolute; left:-6px; top:var(--sp-1); width:var(--sp-2); height:var(--sp-2); border-radius:var(--radius-full); background:var(--muted); border:var(--bw-thick) solid var(--surface); }.plc-timeline li.open::before { background:var(--ok); }.plc-head { display:flex; align-items:baseline; gap:var(--sp-1); flex-wrap:wrap; }.plc-dates { font-family:var(--mono); font-weight:600; font-size:var(--fs-body); }.plc-price,.plc-meta,.plc-note { color:var(--muted); font-size:var(--fs-caption); }.plc-price { font-family:var(--mono); }.plc-meta { margin-top:var(--sp-half); }.plc-thesis { font-size:var(--fs-caption); line-height:1.5; margin:var(--sp-1) 0 0; color:var(--muted); }.plc-conds { margin:var(--sp-1) 0 0; padding-left:var(--sp-4); font-size:var(--fs-caption); color:var(--muted); }.plc-conds li { padding:var(--sp-half) 0; border:none; margin:0; }.plc-conds li::before { display:none; }.plc-grade { margin-top:var(--sp-2); display:grid; gap:var(--sp-1); max-width:var(--grid-card-lg); }.plc-grade textarea,.plc-grade select { width:100%; box-sizing:border-box; }.plc-grade .plc-grade-row { display:flex; gap:var(--sp-2); align-items:center; }.plc-lessons { font-size:var(--fs-caption); line-height:1.5; margin:var(--sp-1) 0 0; }
"""

_ALLOCATION = r"""
.alloc-grid { display:flex; flex-direction:column; gap:var(--sp-3); }.alloc-cash-form,.alloc-actions,.posture-actions { display:flex; flex-wrap:wrap; gap:var(--sp-2); align-items:center; }.alloc-cash-form { margin-bottom:var(--sp-2); }.alloc-cash-form input[type=number] { width:var(--grid-card-sm); }.alloc-cash-form input[type=text] { width:var(--grid-card-md); }.alloc-error { color:var(--bad); font-size:var(--fs-body); margin-top:var(--sp-1); white-space:pre-wrap; }.alloc-plan-row { display:flex; flex-wrap:wrap; align-items:baseline; gap:var(--sp-2); padding:var(--sp-1) 0; border-bottom:var(--bw-thin) dashed var(--border); }.alloc-plan-row:last-child { border-bottom:none; }.alloc-alt-grid,.risk-cat-grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(var(--grid-card-lg),1fr)); gap:var(--sp-3); align-items:start; }.alloc-actions,.posture-actions { margin-top:var(--sp-3); }.cr-receipt { color:var(--muted); font-size:var(--fs-caption); margin:var(--sp-2) 0 0; }.alloc-compare-out { margin-top:var(--sp-2); font-size:var(--fs-body); }.alloc-compare-table { width:100%; border-collapse:collapse; font-size:var(--fs-body); }.alloc-compare-table th,.alloc-compare-table td { padding:var(--sp-1) var(--sp-2); text-align:left; border-bottom:var(--bw-thin) solid var(--border); }.alloc-fallback-banner { margin-bottom:var(--sp-2); }.alloc-rationale { margin-top:var(--sp-2); }.alloc-rationale>summary { cursor:pointer; color:var(--fg); font-weight:600; padding:var(--sp-2) 0; }.risk-cat { padding:var(--sp-1) 0; }.risk-cat h4 { margin:0 0 var(--sp-1); font-size:var(--fs-body); }.risk-row { display:flex; justify-content:space-between; gap:var(--sp-2); padding:var(--sp-half) 0; font-size:var(--fs-body); }.cc-alloc-today { display:flex; align-items:baseline; gap:var(--sp-2); margin:0 0 var(--sp-3); font-size:var(--fs-body); }
"""

_DECISIONS = r"""
.ad-table td,.ad-timeline td { vertical-align:middle; }.ad-note { font-size:var(--fs-caption); margin:0 0 var(--sp-2); }.ad-score { color:var(--warn); font-variant-numeric:tabular-nums; margin-right:var(--sp-2); }.ad-chip { margin:var(--sp-half) var(--sp-1) var(--sp-half) 0; }.ad-mismatch { max-width:var(--grid-card-lg); }.ad-aligned { font-size:var(--fs-caption); }.ad-editor { display:flex; align-items:center; gap:var(--sp-2); flex-wrap:wrap; font-size:var(--fs-caption); padding:var(--sp-1) 0; }.ad-editor span,.ad-timeline td.when,.adc-kpis,.adc-line,.adc-sub,.sk-lbl,.cpnl-line { color:var(--muted); }.ad-editor select,.ad-editor input { padding:var(--sp-half) var(--sp-1); font-size:var(--fs-caption); }.ad-editor input.ad-target { width:var(--grid-card-sm); }.ad-editor input.ad-note-input { flex:1; min-width:var(--grid-card-sm); }.ad-timeline td.tk { font-weight:600; white-space:nowrap; font-family:var(--mono); }.ad-timeline td.when { white-space:nowrap; }.adc-kpis { display:flex; gap:var(--sp-5); flex-wrap:wrap; margin:var(--sp-half) 0 var(--sp-3); font-size:var(--fs-body); }.adc-kpi b { color:var(--fg); font-variant-numeric:tabular-nums; margin-right:var(--sp-1); }.adc-table { margin-bottom:var(--sp-3); }.adc-line { font-size:var(--fs-caption); margin:var(--sp-1) 0; }.adc-sub { font-size:var(--fs-body); font-weight:600; text-transform:uppercase; letter-spacing:.05em; margin:var(--sp-3) 0 var(--sp-1); }.ad-body { font-size:var(--fs-body); line-height:1.5; }.adc-spark { width:100%; max-width:var(--grid-card-lg); height:var(--grid-card-sm); display:block; margin:var(--sp-half) 0 var(--sp-2); }.adc-trend th,.adc-trend td { padding-top:var(--sp-half); padding-bottom:var(--sp-half); }.adc-trend sup { color:var(--muted); font-size:var(--fs-caption); }.sk-kpis .adc-kpi { display:flex; flex-direction:column; gap:var(--sp-half); }.sk-val { font-variant-numeric:tabular-nums; font-size:var(--fs-body); }.sk-lbl { font-size:var(--fs-caption); text-transform:uppercase; letter-spacing:.04em; }.sk-read { font-size:var(--fs-body); color:var(--fg); margin:var(--sp-half) 0 var(--sp-3); }.cpnl-hoist { margin:0 0 var(--sp-3); }.cpnl-hoist .adc-kpis { margin:0; }.cpnl-line { font-size:var(--fs-caption); margin:var(--sp-1) 0; }.cpnl-line b { color:var(--fg); font-variant-numeric:tabular-nums; }.cpnl-list { display:flex; flex-direction:column; gap:var(--sp-half); }.cpnl-unmute-btn { margin-left:var(--sp-1); }
.adc-spark .adc-trend-line { fill:none; stroke:var(--accent); stroke-width:var(--bw-thick); }.adc-spark .adc-trend-midline { fill:none; stroke:var(--border); stroke-width:var(--bw-thin); stroke-dasharray:3 3; }.adc-spark .adc-dot { stroke:var(--accent); }.adc-spark .adc-dot-confident { fill:var(--accent); stroke-width:var(--bw-thin); }.adc-spark .adc-dot-thin { fill:var(--bg); stroke-width:var(--bw-thin); }
"""

_MEMOS = r"""
.soc-record-link { color:var(--accent); }
.am-runbar { display:flex; align-items:center; gap:var(--sp-1); flex-wrap:wrap; background:var(--surface); border:var(--bw-thin) solid var(--border); border-radius:var(--radius); padding:var(--sp-2) var(--sp-4); margin-bottom:var(--sp-5); font-size:var(--fs-body); }.am-note,.soc-status { font-size:var(--fs-caption); }.am-log { width:100%; margin:var(--sp-2) 0 0; padding:var(--sp-2) var(--sp-3); background:var(--paper); border:var(--bw-thin) solid var(--border); border-radius:var(--radius); font-family:var(--mono); font-size:var(--fs-caption); max-height:var(--grid-card-sm); overflow-y:auto; white-space:pre-wrap; }.am-screen td { vertical-align:middle; }.am-cleared { color:var(--warn); font-weight:600; }.am-card { background:var(--surface); border:var(--bw-thin) solid var(--border); border-radius:var(--radius); padding:var(--sp-2) var(--sp-4); margin-bottom:var(--sp-2); }.am-card summary { cursor:pointer; list-style:none; display:flex; align-items:baseline; gap:var(--sp-1); flex-wrap:wrap; }.am-card summary::-webkit-details-marker { display:none; }.am-card summary::before { content:'\25B8  '; color:var(--muted); font-family:var(--mono); }.am-card[open] summary::before { content:'\25BE  '; }.am-scope { font-family:var(--mono); font-weight:600; }.am-title { color:var(--fg-soft); font-size:var(--fs-body); }.am-stamp { margin-left:auto; color:var(--muted); font-size:var(--fs-caption); font-family:var(--mono); }.am-body { font-size:var(--fs-body); line-height:1.6; margin-top:var(--sp-3); }.am-body h2,.am-body h3,.am-body h4 { color:var(--fg); margin:var(--sp-3) 0 var(--sp-1); }.am-body h3 { font-size:var(--fs-title); }.am-body ul { padding-left:var(--sp-5); }.am-sep { width:var(--bw-thin); height:var(--sp-5); background:var(--border); display:inline-block; }.am-runbar select,.soc-controls select { padding:var(--sp-1) var(--sp-5) var(--sp-1) var(--sp-2); font-size:var(--fs-caption); font-family:var(--mono); }.am-stance { text-transform:uppercase; letter-spacing:.06em; font-size:var(--fs-caption); cursor:help; }.soc-q { margin:var(--sp-3) 0; }.soc-q label { display:block; font-size:var(--fs-body); color:var(--fg); margin-bottom:var(--sp-1); }.soc-q textarea { width:100%; resize:vertical; }.soc-controls { display:flex; align-items:center; gap:var(--sp-1); flex-wrap:wrap; margin-top:var(--sp-4); }.soc-saved { color:var(--ok); font-size:var(--fs-body); }.am-track { font-size:var(--fs-body); margin:0 0 var(--sp-3); font-variant-numeric:tabular-nums; }
* { box-sizing:border-box; }.soc-page-body { margin:0; padding:var(--sp-4) var(--sp-5) var(--sp-6); font-family:var(--sans); background:var(--bg); color:var(--fg); line-height:1.55; font-size:var(--fs-body); }.soc-page-main { max-width:var(--grid-card-lg); margin:0 auto; }.soc-page-main h1 { font-size:var(--fs-display); margin:0 0 var(--sp-1); }.soc-page-main h2 { font-size:var(--fs-title); margin:0 0 var(--sp-1); }.soc-page-panel { background:var(--surface); border:var(--bw-thin) solid var(--border); border-radius:var(--radius); padding:var(--sp-3) var(--sp-4); margin-bottom:var(--sp-4); }.soc-page-panel .sub { color:var(--muted); font-size:var(--fs-caption); margin:0 0 var(--sp-2); }.soc-page-muted { color:var(--muted); }
"""


def portfolio_css() -> str:
    return _block(_PORTFOLIO)


def console_css() -> str:
    return _block(_CONSOLE)


def positioning_css() -> str:
    return _block(_POSITIONING)


def lifecycle_css() -> str:
    return _block(_LIFECYCLE)


def allocation_css() -> str:
    return _block(_ALLOCATION)


def decisions_css() -> str:
    return _block(_DECISIONS)


def memos_css() -> str:
    return _block(_MEMOS)


def page_css() -> str:
    return palette_css("dark") + controls_css("dark") + memos_css()
