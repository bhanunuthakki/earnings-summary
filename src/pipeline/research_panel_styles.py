"""Shared visual recipes for the research and ledger panel family.

Panel renderers own data, markup, and interaction hooks. This module owns the
family's visual vocabulary so a spacing, card, metadata, or state treatment is
changed once and inherited by every research surface.
"""

from __future__ import annotations

from ui.source_chip import SOURCE_CHIP_CSS

DIET_PANEL_STYLE = """<style>
.diet-sec { margin-top: var(--sp-5); }
.diet-sec.first { margin-top: var(--sp-3); }
.diet-sec-h { font-size: var(--fs-title); font-weight: 600; color: var(--fg);
  margin: 0 0 var(--sp-2); }
.diet-fresh { color: var(--muted); font-size: var(--fs-caption); font-weight: 400;
  white-space: nowrap; }
.diet-when { color: var(--muted); font-variant-numeric: tabular-nums; white-space: nowrap; }
.diet-sig a { color: var(--fg); text-decoration: none; }
.diet-sig a:hover { color: var(--accent); text-decoration: underline; }
.diet-firm { color: var(--muted); font-size: var(--fs-caption); }
.diet-date { font-family: var(--mono); font-weight: 600; color: var(--fg);
  font-variant-numeric: tabular-nums; white-space: nowrap; }
.diet-empty { color: var(--muted); font-style: italic; padding: var(--sp-3) 0; }
/* D3 group headers inside the stream: kind + a deterministic summary. */
.diet-group-h { font-size: var(--fs-body); font-weight: 600; color: var(--fg);
  margin: var(--sp-4) 0 var(--sp-1); }
.diet-group-sum { font-weight: 400; color: var(--muted); font-size: var(--fs-caption);
  margin-left: 6px; }
.diet-scaffold { margin-top: var(--sp-4); font-size: var(--fs-caption);
  color: var(--muted); }
</style>"""

RESEARCH_PANEL_STYLE = (
    """<style>
/* Research family foundation: controls are supplied by the shell's kit. */
.cc-peek-attest { margin-top: 10px; padding-top: 10px; border-top: 1px solid var(--hairline);
  display: flex; align-items: center; gap: 10px; }
.cc-peek-attest-done { color: var(--fg-soft); font-size: var(--fs-caption); }
.cc-attest-msg { font-size: var(--fs-caption); color: var(--fg-soft); }
.cc-review-peek .synthesis-body { font-size: var(--fs-body); }
.cc-review-foot { margin-top: 10px; padding-top: 10px; border-top: 1px solid var(--hairline);
  display: flex; flex-direction: column; gap: 8px; }
.cc-review-log { font-family: var(--mono); font-size: var(--fs-caption); color: var(--fg-soft);
  background: var(--paper); border: 1px solid var(--border); border-radius: var(--radius);
  padding: 8px 10px; margin: 0; max-height: 200px; overflow-y: auto;
  white-space: pre-wrap; word-break: break-word; }
.research-card, .jr-note, .dj-row, .vx-builder, .sc-note, .ledger-cap,
.ledger-stance, .ledger-packet { border:1px solid var(--border); border-radius:var(--radius);
  background:var(--surface); }
.research-card, .jr-note { padding:var(--sp-3) var(--sp-4); margin-bottom:var(--sp-2); }
.research-meta, .jr-when, .jr-src, .dj-when, .dj-advice, .jr-count, .dj-count,
.jr-hint, .vx-none, .vx-hint, .sc-skip-lo { color:var(--muted); font-size:var(--fs-caption); }
.research-title, .jr-h, .dj-h { color:var(--fg); font-size:var(--fs-title); font-weight:600;
  margin:var(--sp-4) 0 var(--sp-1); }
.research-body, .jr-body, .dj-note { color:var(--fg-soft); font-size:var(--fs-body); line-height:1.5; }
.research-empty, .jr-empty, .dj-empty { color:var(--muted); padding:var(--sp-4) 0; }
.research-actions, .jr-actions, .jr-filters, .dj-pq { display:flex; gap:var(--sp-2);
  align-items:center; flex-wrap:wrap; }
.jr-filters { margin:var(--sp-1) 0 var(--sp-4); }
.jr-count, .dj-count { margin-left:auto; }
.jr-note-new { margin:0 0 var(--sp-5); }
.jr-note-new textarea { width:100%; box-sizing:border-box; min-height:54px; }
.jr-head, .jr-rec-row { display:flex; gap:var(--sp-2); align-items:baseline; flex-wrap:wrap; }
.jr-head { margin-bottom:var(--sp-2); }
.jr-status, .ledger-chan, .ledger-unattr, .ledger-needs { font-size:var(--fs-caption);
  text-transform:uppercase; letter-spacing:.04em; }
.jr-status-open, .ledger-needs { color:var(--warn); }
.jr-status-resolved, .sc-skip-hi { color:var(--ok); }
.jr-status-superseded, .jr-status-archived { color:var(--muted); }
.jr-resolution, .jr-anchor, .jr-concl { color:var(--muted); font-size:var(--fs-caption); }
.jr-actions { margin-top:var(--sp-2); }
.jr-actions select { padding:var(--sp-half) var(--sp-2); }
.jr-empty { padding:var(--sp-5) 0; }
.jr-hint { margin-top:var(--sp-3); }
.jr-rec-sec { margin:0 0 var(--sp-4); }
.jr-rec { padding:var(--sp-2) var(--sp-3); margin-bottom:var(--sp-2); }
.jr-rec-concl { color:var(--warn); font-size:var(--fs-caption); margin:var(--sp-1) 0 var(--sp-2); }
.jr-synthesis { margin-top:var(--sp-4); border-top:1px solid var(--border); padding-top:var(--sp-3); }
.jr-synth-note { border-radius:var(--radius); background:var(--paper); padding:var(--sp-3) var(--sp-4);
  margin-bottom:var(--sp-2); }
.jr-edit-ta { width:100%; box-sizing:border-box; min-height:48px; resize:vertical;
  font-family:var(--sans); font-size:var(--fs-body); margin-top:var(--sp-2); }
.vx-builder { padding:var(--sp-3) var(--sp-4); margin:var(--sp-1) 0 var(--sp-4); }
.vx-row { display:flex; gap:var(--sp-2); align-items:center; flex-wrap:wrap; margin-bottom:var(--sp-3); }
.vx-row label, .vx-picker label, .vx-km-label { color:var(--muted); font-size:var(--fs-caption); }
.vx-pickers { display:grid; grid-template-columns:repeat(3,minmax(180px,1fr)); gap:var(--sp-3);
  margin-bottom:var(--sp-3); }
.vx-picker { display:flex; flex-direction:column; }
.vx-picker label { margin-bottom:var(--sp-half); text-transform:uppercase; letter-spacing:.04em; }
.vx-picker select { width:100%; }
.vx-workbench { border:1px solid var(--border); border-radius:var(--radius); background:var(--surface);
  padding:var(--sp-3) var(--sp-4); margin:var(--sp-1) 0 var(--sp-4); }
.vx-workbench-head { display:flex; justify-content:space-between; gap:var(--sp-2); align-items:baseline;
  color:var(--fg); font-size:var(--fs-body); margin-bottom:var(--sp-2); }
.vx-workbench-head span, .vx-workbench-meta, .vx-workbench-why, .vx-workbench-muted,
.vx-workbench-state span { color:var(--muted); font-size:var(--fs-caption); }
.vx-workbench-grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(180px,1fr)); gap:var(--sp-2); }
.vx-workbench-card { border:1px solid var(--hairline); border-radius:var(--radius); padding:var(--sp-2) var(--sp-3);
  display:flex; flex-direction:column; gap:var(--sp-half); }
.vx-workbench-kicker { color:var(--muted); font-size:var(--fs-caption); }
.vx-workbench-value { color:var(--fg); font-family:var(--mono); font-size:var(--fs-title); }
.vx-workbench-meta { display:flex; align-items:center; gap:var(--sp-1); flex-wrap:wrap; }
.vx-workbench-inspect { color:var(--muted); font-size:var(--fs-caption); }
.vx-workbench-inspect code { color:var(--fg-soft); font-family:var(--mono); }
.vx-workbench-state { display:flex; flex-direction:column; gap:var(--sp-half); }
.vx-error { color:var(--bad); font-size:var(--fs-body); margin:var(--sp-2) 0; }
.vx-inject-ok { color:var(--fg); font-size:var(--fs-body); line-height:1.5;
  border-left:var(--bw-thick) solid var(--ok); padding:var(--sp-1) 0 var(--sp-1) var(--sp-3); }
.ask-thread { display:flex; flex-direction:column; gap:var(--sp-3); margin:var(--sp-1) 0 var(--sp-4); }
.ask-hello { color:var(--muted); font-size:var(--fs-body); line-height:1.5; border:1px dashed var(--border);
  border-radius:var(--radius); padding:var(--sp-3) var(--sp-4); }
.ask-inputrow { display:flex; gap:var(--sp-2); align-items:center; margin-bottom:var(--sp-3); }
.ask-inputrow input { flex:1; padding:var(--sp-2) var(--sp-3); }
.ask-builder-pop { border:1px solid var(--border); border-radius:var(--radius); background:var(--surface);
  padding:0 var(--sp-4) var(--sp-3); margin-top:var(--sp-3); box-shadow:var(--shadow-pop); }
.ask-pop-head { display:flex; justify-content:space-between; align-items:center; padding:var(--sp-3) 0;
  font-size:var(--fs-body); font-weight:600; color:var(--fg); }
.ask-pop-close { background:transparent; border:0; color:var(--muted); font-size:var(--fs-display); cursor:pointer; }
.sc-note { margin-top:var(--sp-4); padding:var(--sp-3) var(--sp-4); font-size:var(--fs-body); line-height:1.55; }
.sc-note code { background:var(--surface); padding:var(--sp-half) var(--sp-1); border-radius:var(--radius); }
.au-strip { display:flex; flex-wrap:wrap; gap:var(--sp-2); align-items:center; }
.au-strip .k-pill { display:inline-flex; gap:var(--sp-1); align-items:baseline; }
.tl-table td.tk { white-space:nowrap; }
.tl-table td.when { color:var(--muted); white-space:nowrap; }
.tl-body { font-size:var(--fs-body); line-height:1.5; }
.tl-kpi-text { font-size:var(--fs-body); font-weight:600; margin:var(--sp-half) 0; color:var(--fg); }
\n/* migrated src/pipeline/peeks.py _SCORE_CSS */\n
.cc-score-head { display: flex; align-items: baseline; justify-content: space-between; gap: 12px; }
.cc-score-cap { color: var(--muted); font-size: var(--fs-caption); text-transform: uppercase;
  letter-spacing: 0.05em; }
.cc-score-big { font-family: var(--mono); font-size: var(--fs-display); font-weight: 600; }
.cc-score-big.score-hi { color: var(--ok); }
.cc-score-big.score-lo { color: var(--muted); }
.cc-score-big.score-warn { color: var(--warn); }
.cc-score-rows { display: flex; flex-direction: column; margin: 10px 0 6px; }
.cc-score-row { display: grid; grid-template-columns: 104px 1fr 84px 52px; gap: 10px;
  align-items: center; padding: 6px 2px; border-bottom: 1px solid var(--hairline);
  font-size: var(--fs-body); }
.cc-score-row:last-child { border-bottom: none; }
.cc-score-label { color: var(--muted); font-size: var(--fs-caption); text-transform: uppercase;
  letter-spacing: 0.05em; }
.cc-score-detail { font-variant-numeric: tabular-nums; }
.cc-score-detail.muted { color: var(--muted); }
.cc-score-bar { display: block; height: 6px; background: var(--paper);
  border-radius: var(--radius-full); overflow: hidden; }
.cc-score-fill { display: block; height: 100%; border-radius: var(--radius-full); }
.cc-score-fill.bar-pos { background: var(--ok); }
.cc-score-fill.bar-neg { background: var(--bad); }
.cc-score-fill.bar-mid { background: var(--muted); }
.cc-score-mult { font-family: var(--mono); font-weight: 600; text-align: right; }
.cc-score-mult.mult-pos { color: var(--ok); }
.cc-score-mult.mult-neg { color: var(--bad); }
.cc-score-mult.mult-mid { color: var(--muted); }
.cc-score-formula { font-family: var(--mono); font-size: var(--fs-caption); color: var(--fg-soft);
  margin-top: 4px; }
.cc-score-legend { color: var(--muted); font-size: var(--fs-caption); margin-top: 6px; }
.cc-fit-degraded { color: var(--warn); font-size: var(--fs-caption); margin-top: 6px; }
.cc-fit-group { color: var(--muted); font-size: var(--fs-caption); text-transform: uppercase;
  letter-spacing: 0.05em; margin-top: 10px; }
.cc-fit-strip { font-family: var(--mono); font-size: var(--fs-caption); color: var(--fg-soft);
  margin-top: 8px; }
.cc-wi-weights { display: flex; gap: 4px; }
.cc-wi-w-on { background: var(--accent-soft); }
.cc-wi-row { display: grid; grid-template-columns: 104px 1fr 24px 1fr; gap: 10px;
  align-items: center; padding: 6px 2px; border-bottom: 1px solid var(--hairline);
  font-size: var(--fs-body); }
.cc-wi-row:last-child { border-bottom: none; }
.cc-wi-val { font-family: var(--mono); font-variant-numeric: tabular-nums; }
.cc-wi-arrow { color: var(--muted); text-align: center; }
.cc-wi-corrs { display: flex; flex-wrap: wrap; gap: 4px; margin-top: 4px; }
\n/* migrated src/pipeline/peeks.py _PROV_CSS */\n
.cc-prov-rows { display: flex; flex-direction: column; }
.cc-prov-row { display: flex; align-items: baseline; gap: 10px; padding: 6px 2px;
  border-bottom: 1px solid var(--hairline); font-size: var(--fs-body); }
.cc-prov-row:last-child { border-bottom: none; }
.cc-prov-src { flex: 0 0 128px; color: var(--muted); font-size: var(--fs-caption);
  text-transform: uppercase; letter-spacing: 0.05em; }
.cc-prov-when { flex: 1 1 auto; min-width: 0; }
.cc-prov-age { font-weight: 600; }
.cc-prov-note { color: var(--muted); font-size: var(--fs-caption); }
/* cron marker + refresh button compose the kit (.k-chip / .k-btn-quiet.k-btn-sm);
   only the flex-child layout stays local. */
.cc-prov-cron { flex: none; }
.cc-prov-btn { flex: none; }
.cc-prov-log { font-family: var(--mono); font-size: var(--fs-caption); color: var(--fg-soft);
  background: var(--paper); border: 1px solid var(--border); border-radius: var(--radius);
  padding: 8px 10px; margin: 10px 0 0; max-height: 200px; overflow-y: auto;
  white-space: pre-wrap; word-break: break-word; }
\n/* migrated src/pipeline/peeks.py _PREP_CSS */\n
.cc-prep { display: flex; flex-direction: column; gap: 8px; }
.prep-head { display: flex; align-items: baseline; gap: 8px; }
.prep-when { font-family: var(--mono, monospace); color: var(--fg); font-weight: 600; }
.prep-sec h4 { margin: 0 0 3px; font-size: var(--fs-caption); color: var(--muted);
  text-transform: uppercase; letter-spacing: 0.06em; }
.prep-sec ul { list-style: none; margin: 0; padding: 0; }
.prep-sec li { padding: 2px 0; font-size: var(--fs-body); }
.prep-sec p { margin: 0; font-size: var(--fs-body); }
.prep-ask { background: transparent; border: 0; padding: 0; cursor: pointer;
  color: var(--fg); font: inherit; text-align: left; }
.prep-ask:hover { color: var(--accent); }
\n/* migrated src/pipeline/ledger_panel.py _PANEL_STYLE */
/* ONE card shape (visual conformance pass, requirement E): every card-like
   block on the Ledger tab — capture box, musing, stance, coach card — shares
   the SAME background/radius/padding/margin-bottom. Before this pass
   .ledger-cap alone carried a smaller sp-2/sp-3 padding and a sp-3 (not
   sp-2) margin-bottom, a visible size/gap mismatch against every card below
   it; it now shares the one card treatment via this grouped selector. */
.ledger-cap, .ledger-musing, .ledger-stance, .ledger-coach-card {
  background: var(--surface); border-radius: var(--radius);
  padding: var(--sp-3) var(--sp-4); margin-bottom: var(--sp-2);
}
.ledger-stance, .ledger-coach-card { border-left: 3px solid var(--border-2); }
.ledger-coach-card { position: relative; }
.ledger-cap textarea { width: 100%; min-height: 44px; resize: vertical; font-family: var(--sans); font-size: var(--fs-body); }
.ledger-cap-row { display: flex; align-items: center; gap: var(--sp-2); margin-top: var(--sp-2); }
.ledger-cap-status { font-size: var(--fs-caption); color: var(--muted); }
.ledger-musing-head, .ledger-stance-head { display: flex; align-items: baseline; gap: var(--sp-2); margin-bottom: var(--sp-1); }
.ledger-when { color: var(--muted); font-family: var(--mono); font-size: var(--fs-caption); margin-left: auto; white-space: nowrap; }
/* ONE micro-tag treatment (requirement E): every uppercase muted label chip
   — channel tag, unattributed marker — shares this rule instead of three
   near-identical copies. */
.ledger-chan, .ledger-unattr { font-size: var(--fs-caption); color: var(--muted); text-transform: uppercase; letter-spacing: 0.05em; }
.ledger-needs { color: var(--warn); font-size: var(--fs-caption); font-weight: 600; text-transform: uppercase; letter-spacing: 0.05em; }
.ledger-body { font-size: var(--fs-body); line-height: 1.55; color: var(--fg-soft); overflow-wrap: anywhere; }
.ledger-body > :first-child { margin-top: 0; }
.ledger-body > :last-child { margin-bottom: 0; }
.ledger-empty { color: var(--muted); font-style: italic; padding: var(--sp-3) 0; }
/* ONE heading scale for every section/sub-heading on the tab (requirement E)
   — .ledger-armed-h used to be its own smaller, uppercase, letter-spaced
   treatment (a step below every other section heading), reading as a
   different heading LEVEL for no reason; it now shares .ledger-sec-h. */
.ledger-sec-h, .ledger-armed-h { font-size: var(--fs-title); font-weight: 600; color: var(--fg); margin: var(--sp-4) 0 var(--sp-1); }
.ledger-sec-sub { font-size: var(--fs-caption); color: var(--muted); margin: 0 0 var(--sp-3); }
.ledger-stance-meta { color: var(--muted); font-size: var(--fs-caption); margin-left: auto; }
.ledger-coach-body { font-size: var(--fs-body); line-height: 1.55; color: var(--fg-soft); white-space: normal; }
.ledger-coach-row { display: flex; align-items: center; gap: var(--sp-2); margin-top: var(--sp-2); }
.ledger-coach-row input { flex: 1; font-family: var(--sans); font-size: var(--fs-body); }
.ledger-coach-x { position: absolute; top: var(--sp-2); right: var(--sp-2); }
.ledger-coach-receipt { color: var(--fg-soft); font-size: var(--fs-caption); }
/* Ratify receipt (consequence receipts PR) — a transient one-line notice
   above the Reconcile list; a sibling of #ledger-reconcile so the fragment
   reload's outerHTML swap never clobbers it before it's read. */
.ledger-receipt { color: var(--fg-soft); font-size: var(--fs-caption);
  padding: var(--sp-2) 0; }
.ledger-armed-table { width: 100%; border-collapse: collapse; font-size: var(--fs-caption); }
.ledger-armed-table th { text-align: left; color: var(--muted); font-weight: 600;
  padding: var(--sp-1) var(--sp-2); border-bottom: 1px solid var(--border); }
.ledger-armed-table td { padding: var(--sp-1) var(--sp-2); border-bottom: 1px solid var(--hairline); }
.ledger-armed-ticker { font-family: var(--mono); font-weight: 600; }
.ledger-armed-since { color: var(--muted); white-space: nowrap; }
.ledger-armed-num a { color: var(--muted); text-decoration: none; }
.ledger-armed-num a:hover { color: var(--accent); }
/* Jump-chip toolbar (PR9) — mirrors the Provenance console's anchor-nav band;
   one operating row above the sections, wraps on narrow widths. */
.ledger-jump-toolbar { display: flex; flex-wrap: wrap; gap: var(--sp-2); margin-bottom: var(--sp-4); }
/* Set-ticker chips (PR9) — the needs_ticker musing card's one-tap attribution
   row; reuses .ledger-cap-row's flex layout via the extra class below. */
.ledger-set-ticker { flex-wrap: wrap; }
/* In-card Rewrite / Steer textareas (PR9, replaces window.prompt) — same
   sizing family as the capture box's own textarea. */
.ledger-rewrite-ta, .ledger-steer-ta { width: 100%; min-height: 56px; resize: vertical;
  font-family: var(--sans); font-size: var(--fs-body); margin-bottom: var(--sp-2); }
/* Queues (overhaul P4): the four machinery sections (research / reconcile /
   worldview / stances) collapse into ONE block below the feed, so the tab reads
   conversation-first. Closed by default; a count on the summary surfaces pending
   work without re-inflating the wall of sections. Token-only. */
.ledger-queues { margin-top: var(--sp-4); border-top: 1px solid var(--border); }
.ledger-queues-sum { cursor: pointer; padding: var(--sp-3) 0; font-size: var(--fs-body); font-weight: 600; color: var(--fg); list-style: none; display: flex; align-items: baseline; gap: var(--sp-2); }
.ledger-queues-sum::-webkit-details-marker { display: none; }
.ledger-queues-sum::before { content: "\\25B8"; color: var(--muted); font-size: var(--fs-caption); }
.ledger-queues[open] .ledger-queues-sum::before { content: "\\25BE"; }
.ledger-queues-hint { font-size: var(--fs-caption); font-weight: 400; color: var(--muted); }
.ledger-queues-count { font-size: var(--fs-caption); font-weight: 600; color: var(--fg); }
.ledger-queues-body { padding-top: var(--sp-2); }
/* migrated src/pipeline/ledger_panel.py _ONMYMIND_STYLE */
/* Reuses the panel's own micro-tag / warn-tag treatment (.ledger-chan,
   .ledger-needs — see _PANEL_STYLE) rather than a second copy of the same
   rule under a card-local name; only the accent ladder badge is genuinely
   new here. */
.om-ladder { font-size: var(--fs-caption); font-weight: 600; color: var(--muted); text-transform: uppercase; letter-spacing: 0.05em; }
.om-ladder:empty { display: none; }
.om-actions { flex-wrap: wrap; }
/* Backlink from a research item to its source note + the brief highlight the
   scroll applies on arrival (owner feedback 2026-07-14). */
.ledger-backlink { margin-left: auto; }
.om-flash { animation: om-flash-kf 1.6s ease-out; }
@keyframes om-flash-kf {
  0%, 100% { background: transparent; }
  20% { background: color-mix(in srgb, var(--accent) 16%, transparent); }
}
.om-body a { overflow-wrap: anywhere; }
.om-brief { margin-top: var(--sp-2); }
.om-brief summary { font-size: var(--fs-caption); font-weight: 600; color: var(--accent); cursor: pointer; }
.om-brief-body { margin-top: var(--sp-2); padding: var(--sp-2) var(--sp-3); border-left: 3px solid var(--border-2); font-size: var(--fs-caption); color: var(--fg-soft); }
.om-brief-takeaways { margin: 0 0 var(--sp-2); padding-left: var(--sp-4); }
.om-brief-line { margin: var(--sp-1) 0; }
.om-brief-src { margin: var(--sp-2) 0 0; color: var(--muted); font-size: var(--fs-caption); }
/* The inline answer (overhaul): the Ledger's response to a question-shaped
   capture, generated once at capture time and stored on the note. A quiet
   accent-bordered block under the captured thought — distinct from the thought
   itself, token-only. */
.om-answer { margin-top: var(--sp-2); font-size: var(--fs-caption); line-height: 1.55; color: var(--fg-soft); }
.om-answer > :first-child { margin-top: 0; }
.om-answer > :last-child { margin-bottom: 0; }
.om-answer-label { display: block; font-size: var(--fs-caption); font-weight: 600; color: var(--fg); text-transform: uppercase; letter-spacing: 0.05em; margin-bottom: var(--sp-1); }
/* The answer is being generated on a background thread — an honest quiet
   in-progress state the poll swaps for the real answer when it lands. */
.om-answer-pending { color: var(--muted); font-style: italic; }
/* Inline chat (overhaul P3): "Ask more" / "Discuss" expands a real thread with
   the Ask brain right inside the card — no navigation, no popup. Token-only. */
.om-chat { margin-top: var(--sp-2); border-top: 1px solid var(--hairline); padding-top: var(--sp-2); }
.om-chat-thread { display: flex; flex-direction: column; gap: var(--sp-2); margin-bottom: var(--sp-2); }
.om-chat-msg { font-size: var(--fs-caption); line-height: 1.5; padding: var(--sp-2) var(--sp-3); border-radius: var(--radius); max-width: 90%; overflow-wrap: anywhere; }
.om-chat-user { align-self: flex-end; background: var(--accent-soft); color: var(--fg); }
.om-chat-assistant { align-self: flex-start; background: var(--surface); color: var(--fg-soft); }
.om-chat-pending { color: var(--muted); }
.om-chat-input { flex: 1; font-family: var(--sans); font-size: var(--fs-body); }
/* The universal reply box (Phase B) — one input per card, routed by the
   reply-intent classifier; the receipt bubble is the acted-path acknowledgement. */
.om-reply-input { flex: 1; font-family: var(--sans); font-size: var(--fs-body); }
.om-chat-receipt { color: var(--ok); font-weight: 600; }
#onmymind-more { margin-top: var(--sp-2); }
/* migrated src/pipeline/ledger_panel.py _PACKET_STYLE */
.ledger-packet { background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius); padding: var(--sp-3) var(--sp-4); margin-bottom: var(--sp-4); }
.pk-band { display: flex; align-items: baseline; gap: var(--sp-3); flex-wrap: wrap; }
.pk-count { font-size: var(--fs-body); font-weight: 600; color: var(--fg); }
.pk-hint, .pk-payoff { font-size: var(--fs-caption); color: var(--muted); }
.pk-payoff { flex-basis: 100%; }
.pk-progress { display: flex; align-items: baseline; gap: var(--sp-2); font-size: var(--fs-caption); color: var(--muted); margin: var(--sp-2) 0; flex-wrap: wrap; }
.pk-tally { color: var(--fg); font-weight: 600; }
.pk-item > .ledger-musing, .pk-item > .ledger-stance { margin-bottom: 0; }
.pk-class-header { font-size: var(--fs-caption); font-weight: 600; color: var(--muted); text-transform: uppercase; letter-spacing: 0.05em; margin: 0 0 var(--sp-1); }
.pk-class-why { font-size: var(--fs-caption); color: var(--muted); font-weight: 400; text-transform: none; letter-spacing: normal; margin-left: var(--sp-2); }
.pk-clear { color: var(--ok); font-weight: 600; padding: var(--sp-3) 0; }
.pk-receipt { color: var(--ok); font-weight: 600; font-size: var(--fs-body); padding: var(--sp-3) var(--sp-4); }
.pk-fail { color: var(--bad); font-size: var(--fs-caption); margin-top: var(--sp-2); }
.ledger-consequence { font-size: var(--fs-caption); color: var(--muted); margin: var(--sp-1) 0 0; }
.ledger-profile-update-form { margin-top: var(--sp-2); }
/* Homogeneous bulk-affirm group card (requirement C): the same .ledger-musing
   shape, with the individual narratives listed inside so nothing hides. */
.ledger-group-list { margin: var(--sp-2) 0; padding-left: var(--sp-4); font-size: var(--fs-caption); color: var(--fg-soft); }
.ledger-group-list li { margin: var(--sp-1) 0; }
/* migrated source_viewers standalone page and fragment rules */

.sv-title { font-size: var(--fs-title); font-weight: 600; }
.sv-meta { color: var(--muted); font-size: var(--fs-caption); font-family: var(--mono); }
.sv-lines { list-style: none; margin: 0; padding: 0; counter-reset: ln; }
.sv-lines li { counter-increment: ln; padding: 1px 8px 1px 0; display: flex; gap: 14px; }
.sv-lines li::before { content: counter(ln); color: var(--muted); width: 42px;
  flex: none; text-align: right; font-family: var(--mono); font-size: var(--fs-caption);
  padding-top: 2px; user-select: none; }
.sv-lines li:target { background: color-mix(in srgb, var(--warn) 14%, transparent);
  outline: 1px solid var(--warn); border-radius: var(--radius); }
.sv-lines .ln-text { white-space: pre-wrap; word-break: break-word; }
.sv-lines .ln-speaker { font-weight: 600; color: var(--fg); }
.sv-secnav { display: flex; flex-wrap: wrap; gap: 6px; margin: 0 0 18px; }
.sv-secnav a { text-decoration: none; }
.sv-sec-row { padding: 4px 0; border-bottom: 1px solid var(--hairline); }
.sv-sec-key { color: var(--muted); font-size: var(--fs-caption); }
.sv-sec-val { white-space: pre-wrap; word-break: break-word; }
.sv-frag-head { display: flex; gap: 12px; align-items: baseline; flex-wrap: wrap;
  margin-bottom: 12px; padding-bottom: 8px; border-bottom: 1px solid var(--border); }
.sv-stmt-wrap { overflow-x: auto; }
.sv-stmt-table { border-collapse: collapse;
  font-size: var(--fs-caption); white-space: nowrap; }
.sv-stmt-table th, .sv-stmt-table td { padding: 4px 10px; text-align: right;
  border-bottom: 1px solid var(--hairline); }
/* Canonical table rule (viewspec/render.py's .vx-matrix): numbers mono,
   labels/headers sans. Only the VALUE cells (<td>, not the first-child period
   column) carry mono; the sticky label column and every <th> inherit body's
   sans instead of the whole table pinning mono across the label too. */
.sv-stmt-table td:not(:first-child) { font-family: var(--mono); }
.sv-stmt-table th:first-child, .sv-stmt-table td:first-child {
  text-align: left; color: var(--muted); position: sticky; left: 0; background: var(--bg); }
.sv-stmt-table th { color: var(--muted); font-weight: 600; }
.sv-cell-hit { background: color-mix(in srgb, var(--warn) 16%, transparent);
  outline: 1px solid var(--warn); border-radius: var(--radius); }
 .sv-stmt-foot { margin-top: 12px; font-size: var(--fs-caption); }
 .sv-original-link { font-size:var(--fs-caption); }
/* PDF page-image view (pdf_slide locators — provenance click-through Phase B).
   The stage is position:relative so the bbox highlight overlays the rendered
   page image in percentage coordinates (layout only; highlight tones reuse the
   same warn color-mix treatment as .sv-cell-hit / .sv-lines li:target). */
.sv-pdf-stage { position: relative; display: inline-block; max-width: 100%; }
.sv-pdf-stage img { display: block; max-width: 100%; height: auto;
  border: 1px solid var(--border); border-radius: var(--radius); }
.sv-pdf-hit { position: absolute; pointer-events: none;
  outline: 2px solid var(--warn); border-radius: var(--radius);
  background: color-mix(in srgb, var(--warn) 18%, transparent); }
.sv-pdf-pager { display: flex; gap: 10px; align-items: baseline; flex-wrap: wrap;
  margin: 10px 0; font-size: var(--fs-caption); }
.sv-pdf-pager .sv-pdf-page-n { color: var(--muted); font-family: var(--mono); }
.sv-pdf-snippet { margin-top: 10px; }

.sv-page { margin: 0; font-family: var(--sans); background: var(--bg); color: var(--fg);
  font-size: var(--fs-body); line-height: 1.55; }
.sv-page a { color: var(--accent); }
.sv-head { padding: 14px 22px; border-bottom: 1px solid var(--border);
  display: flex; gap: 14px; align-items: baseline; flex-wrap: wrap;
  position: sticky; top: 0; background: var(--bg); z-index: 5; }
.sv-body { max-width: 980px; margin: 0 auto; padding: 18px 22px 80px; }
.sv-fallback { max-width: 720px; margin: 60px auto; padding: 22px;
  border: 1px solid var(--border); border-radius: var(--radius); background: var(--surface); }

"""  # nosec B608 -- static CSS family recipe; never constructed or executed as SQL.
    + SOURCE_CHIP_CSS
    + """
</style>"""
)


def research_panel_style() -> str:
    """Return the closed family recipe used by embedded panel fragments."""

    return RESEARCH_PANEL_STYLE
