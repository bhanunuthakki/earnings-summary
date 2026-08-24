"""Shared token-only stylesheet for the analysis and brief visual family."""

from __future__ import annotations

ANALYSIS_STYLE = """<style>
.etfw { display: flex; flex-direction: column; gap: 12px; }
.etfw h4 { margin: 0 0 4px; font-size: var(--fs-caption); color: var(--muted); text-transform: uppercase; letter-spacing: 0.05em; }
.etfw-strip { display: flex; flex-wrap: wrap; gap: 6px; }
.etfw-row { display: grid; grid-template-columns: 120px 1fr; gap: 8px; padding: 4px 0; border-bottom: 1px solid var(--hairline); font-size: var(--fs-body); }
.etfw-row:last-child { border-bottom: none; }
.etfw-row .v { font-family: var(--mono); font-variant-numeric: tabular-nums; }
.etfw-table { width: 100%; border-collapse: collapse; font-size: var(--fs-body); }
.etfw-table td, .etfw-table th { padding: 4px 8px 4px 0; text-align: left; }
.etfw-table td.num { font-family: var(--mono); }
.etfw-miss { color: var(--muted); font-size: var(--fs-body); }
.etfw-src { color: var(--muted); font-size: var(--fs-caption); }
.etfw-verdict { margin-bottom: 6px; }
.rtp-rows { display: flex; flex-direction: column; gap: var(--sp-2); margin: var(--sp-2) 0; }
.rtp-row { display: flex; align-items: center; gap: var(--sp-2); flex-wrap: wrap; font-size: var(--fs-body); padding: var(--sp-1) 0; border-bottom: 1px solid var(--border); }
.rtp-note { color: var(--muted); font-size: var(--fs-caption); flex-basis: 100%; margin: 0; }
.rtp-due { font-size: var(--fs-caption); color: var(--muted); margin: var(--sp-1) 0; }
.rtp-scorecard { display: flex; flex-direction: column; gap: var(--sp-2); margin-top: var(--sp-3); }
.rtp-sc-row { display: flex; align-items: baseline; gap: var(--sp-2); font-size: var(--fs-body); }
.rtp-sc-label { font-weight: 600; }
.rtp-sc-detail { color: var(--muted); font-size: var(--fs-caption); flex-basis: 100%; margin: 0; }
.cc-open-loops { display: flex; flex-wrap: wrap; align-items: baseline; gap: 6px 18px; margin: 0 0 10px; font-size: var(--fs-body); }
.cc-ol-head { color: var(--fg); font-weight: 600; }
.cc-ol-line { color: var(--fg); text-decoration: none; display: inline-flex; align-items: baseline; gap: 6px; }
.cc-ol-line:hover { color: var(--accent); }
.cc-ol-line:hover .cc-ol-count { color: var(--accent); }
.cc-ol-clear { color: var(--muted); }
.cc-ol-escalation { margin: 0 0 8px; }
.cc-ol-escalation a { color: inherit; text-decoration: underline; }
.atr-card { margin-bottom:var(--sp-2); }
.atr-head { display:flex; align-items:baseline; gap:10px; flex-wrap:wrap; margin-bottom:4px; }
.atr-ticker { font-family:var(--mono); font-weight:600; }
.atr-alpha { font-family:var(--mono); font-variant-numeric:tabular-nums; font-size:var(--fs-caption); }
.atr-window { color:var(--muted); font-size:var(--fs-caption); font-family:var(--mono); }
.atr-narrative { font-size:var(--fs-body); line-height:1.55; color:var(--fg-soft); margin:0; }
.atr-sub { color:var(--muted); font-size:var(--fs-caption); margin:2px 0 10px; }
.cc-spb-today { display: flex; align-items: baseline; gap: var(--sp-2); flex-wrap: wrap; margin: 0 0 var(--sp-2); }
.cc-spb-today .muted { color: var(--muted); font-size: var(--fs-caption); }
.wv-add { background: var(--surface); border-radius: var(--radius); padding: var(--sp-3) var(--sp-4); margin-bottom: var(--sp-3); }
.wv-add textarea { width: 100%; min-height: 48px; resize: vertical; font-family: var(--sans); font-size: var(--fs-body); }
.wv-add input { width: 100%; margin-top: var(--sp-2); font-family: var(--sans); font-size: var(--fs-caption); }
.wv-add-row { display: flex; align-items: center; gap: var(--sp-2); margin-top: var(--sp-2); flex-wrap: wrap; }
.wv-status { font-size: var(--fs-caption); color: var(--muted); }
.wv-scope { font-family: var(--mono); font-size: var(--fs-caption); color: var(--muted); text-transform: lowercase; }
.wv-prov { font-size: var(--fs-caption); color: var(--muted); text-transform: uppercase; letter-spacing: 0.05em; }
.wv-proposed-badge { font-size: var(--fs-caption); font-weight: 600; color: var(--warn); text-transform: uppercase; letter-spacing: 0.05em; }
.wv-tension { font-size: var(--fs-caption); font-weight: 600; color: var(--warn); text-transform: uppercase; letter-spacing: 0.05em; }
.wv-tension-note { font-size: var(--fs-caption); color: var(--warn); margin-top: var(--sp-1); }
.rt-brief { display: flex; flex-direction: column; gap: var(--sp-3); }
.rt-empty { color: var(--muted); font-size: var(--fs-body); padding: var(--sp-4) 0; }
.rt-group-title { font-size: var(--fs-caption); font-weight: 600; color: var(--muted); text-transform: uppercase; letter-spacing: 0.06em; margin: var(--sp-2) 0 0; }
.rt-item { display: flex; flex-direction: column; gap: var(--sp-2); }
.rt-item-head { display: flex; align-items: center; gap: var(--sp-2); flex-wrap: wrap; }
.rt-item-cross { color: var(--fg-soft); font-size: var(--fs-body); font-weight: 600; }
.rt-item .prose { margin-top: var(--sp-1); }
.rt-item .prose p { margin: 0 0 var(--sp-2); }
.rt-actions { display: flex; gap: var(--sp-2); margin-top: var(--sp-1); flex-wrap: wrap; }
.rt-refute-box { flex-basis: 100%; margin-top: var(--sp-1); }
.rt-refute-ta { width: 100%; box-sizing: border-box; min-height: 56px; resize: vertical; font-family: var(--sans); font-size: var(--fs-body); }
.rt-refute-row { display: flex; gap: var(--sp-2); margin-top: var(--sp-2); }
</style>"""


# Kept body-only for allocation_decisions_panel, which wraps this export.
REDTEAM_PNL_CSS = """.rtp-rows { display: flex; flex-direction: column; gap: var(--sp-2); margin: var(--sp-2) 0; }
.rtp-row { display: flex; align-items: center; gap: var(--sp-2); flex-wrap: wrap; font-size: var(--fs-body); padding: var(--sp-1) 0; border-bottom: 1px solid var(--border); }
.rtp-note { color: var(--muted); font-size: var(--fs-caption); flex-basis: 100%; margin: 0; }
.rtp-due { font-size: var(--fs-caption); color: var(--muted); margin: var(--sp-1) 0; }
.rtp-scorecard { display: flex; flex-direction: column; gap: var(--sp-2); margin-top: var(--sp-3); }
.rtp-sc-row { display: flex; align-items: baseline; gap: var(--sp-2); font-size: var(--fs-body); }
.rtp-sc-label { font-weight: 600; }
.rtp-sc-detail { color: var(--muted); font-size: var(--fs-caption); flex-basis: 100%; margin: 0; }
"""

__all__ = ["ANALYSIS_STYLE", "REDTEAM_PNL_CSS"]
