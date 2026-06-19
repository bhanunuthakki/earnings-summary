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

CSS = (
    "\n/* Shared palette (single source: src/ui/tokens.py) + local layout tokens. */\n"
    + palette_css("dark")
    + controls_css("dark")
    + r"""
:root {
  --pad-x: 22px;
  --pad-y: 14px;
  --gap: 14px;
  --gap-lg: 22px;
  --card-pad: 14px;
}

* { box-sizing: border-box; }
html, body { margin: 0; padding: 0; background: var(--bg); color: var(--fg); }
body { font-family: var(--sans); font-size: var(--fs-body); line-height: 1.55; }

a { color: var(--accent); text-decoration: none; transition: color var(--transition); }
a:hover { text-decoration: underline; }
button { transition: color var(--transition), border-color var(--transition),
  background var(--transition); }

.muted { color: var(--muted); }
.mono { font-family: var(--mono); font-size: 0.93em; }
.num { font-variant-numeric: tabular-nums; }

.l1-shell {
  max-width: 1180px;
  margin: 0 auto;
  padding: 28px var(--pad-x) 64px var(--pad-x);
}

/* Header */
.l1-header { padding-bottom: 18px; border-bottom: 1px solid var(--hairline); margin-bottom: var(--gap-lg); }
.l1-header h1 { margin: 0 0 4px 0; font-size: var(--fs-display); font-weight: 600; letter-spacing: -0.01em; }
.l1-header .l1-subtitle { color: var(--muted); font-size: var(--fs-body); }

/* Section */
.dash-section { margin-bottom: var(--gap-lg); }

/* Active-filter chips — feed only. Only the constraints actually narrowing the
   view render here, each a removable chip linking back to the unfiltered feed
   (no "ALL · ALL" band when nothing is filtered). The chip itself is the shared
   kit (.k-chip + .k-chip-btn, controls.py), emitted by src/dashboard/feed.py;
   only the layout row + the mono value treatment stay local. */
.dash-filters { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 10px; }
.dash-filters .filter-value { font-family: var(--mono); }

/* Alert cards — elevation (surface on bg), not border boxes. */
.alert-card {
  background: var(--surface);
  border-radius: var(--radius);
  padding: var(--card-pad);
  margin-bottom: var(--gap);
}
.alert-card-head { display: flex; flex-wrap: wrap; align-items: center; gap: 10px; margin-bottom: 8px; }
/* Alert-card identity marks ride the shared kit (controls.py), emitted by
   src/dashboard/_card.py: the ticker is a .k-tick-sym (mono 600, no box), the
   trigger a .k-chip, the status a .k-pill + tone. Only timestamp layout is local. */
.fired-at { color: var(--muted); font-family: var(--mono); font-size: var(--fs-caption); margin-left: auto; }

.alert-memo {
  margin: 6px 0 10px 0;
  padding: 10px 12px;
  background: var(--paper);
  border-left: 3px solid var(--border-2);
  border-radius: 0 var(--radius) var(--radius) 0;
  font-family: var(--serif);
  font-size: var(--fs-section);
  color: var(--fg-soft);
}
.alert-memo-pending { color: var(--muted); }

/* Queued actions */
.queued-actions { margin: 10px 0 0 0; }
.queued-actions h4 { font-size: var(--fs-caption); font-weight: 600; margin: 0 0 6px 0; color: var(--muted); letter-spacing: 0.05em; text-transform: uppercase; }
.queued-action {
  display: flex; flex-wrap: wrap; gap: 10px; align-items: flex-start;
  padding: 8px 10px;
  background: var(--paper);
  border-radius: var(--radius);
  margin-bottom: 6px;
}
.qa-body { flex: 1; min-width: 200px; color: var(--fg-soft); font-size: var(--fs-body); }
.qa-actions {
  display: flex; gap: 6px; align-items: center;
  font-size: var(--fs-caption);
}
.qa-actions a {
  display: inline-flex;
  padding: 3px 8px;
  border: 1px solid var(--border-2);
  border-radius: var(--radius);
  color: var(--accent);
}
.qa-actions .qa-cli { color: var(--muted); font-family: var(--mono); font-size: 0.93em; }
.qa-status-applied { color: var(--ok); }
.qa-status-cancelled { color: var(--muted); }

/* Evidence drawer */
.evidence-drawer {
  margin-top: 10px;
  background: var(--paper);
  border-radius: var(--radius);
}
.evidence-drawer > summary {
  cursor: pointer;
  padding: 8px 12px;
  color: var(--muted);
  font-size: var(--fs-micro); font-weight: 600;
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
  font-size: var(--fs-micro);
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.06em;
  color: var(--muted);
  margin-bottom: 4px;
}
.evidence-summary-text { color: var(--fg-soft); font-family: var(--serif); font-size: var(--fs-section); }
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
  font-size: var(--fs-micro);
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
.cite-prov { color: var(--muted); font-family: var(--mono); font-size: var(--fs-micro); white-space: nowrap; }
.prov-source { color: var(--accent); }
.prov-fetched { color: var(--muted); }
.evidence-raw { margin-top: 4px; }
.evidence-raw > summary {
  cursor: pointer;
  color: var(--muted);
  font-size: var(--fs-micro); font-weight: 600;
  letter-spacing: 0.05em;
}
.evidence-raw-pre {
  margin: 6px 0 0 0;
  padding: 8px 10px;
  background: var(--bg);
  border-radius: var(--radius);
  font-family: var(--mono); font-size: var(--fs-micro);
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
  font-family: var(--mono);
  font-size: var(--fs-micro);
  letter-spacing: 0.04em;
}
"""
)
