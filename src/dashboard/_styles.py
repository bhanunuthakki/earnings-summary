"""Shared inline CSS for the Personal CIO dashboard surfaces.

Two-tone editorial palette (echoing the workspace renderer's tokens
without depending on it — the dashboard is intentionally a separate
render surface, not a workspace tab). Single self-contained string
inlined under ``<style>`` so the deliverable HTML files open straight
from the filesystem with no asset fetches.
"""

from __future__ import annotations

CSS = r"""
:root {
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
  --neu: #888b94;

  --sans: 'Inter', -apple-system, BlinkMacSystemFont, system-ui, sans-serif;
  --serif: 'Source Serif 4', Georgia, serif;
  --mono: 'JetBrains Mono', ui-monospace, SFMono-Regular, Menlo, monospace;

  --pad-x: 22px;
  --pad-y: 14px;
  --gap: 14px;
  --gap-lg: 22px;
  --card-pad: 14px;
}

* { box-sizing: border-box; }
html, body { margin: 0; padding: 0; background: var(--bg); color: var(--fg); }
body { font-family: var(--sans); font-size: 14px; line-height: 1.55; }

a { color: var(--accent); text-decoration: none; }
a:hover { text-decoration: underline; }

.muted { color: var(--muted); }
.mono { font-family: var(--mono); font-size: 12.5px; }
.num { font-variant-numeric: tabular-nums; }

.l1-shell {
  max-width: 1180px;
  margin: 0 auto;
  padding: 28px var(--pad-x) 64px var(--pad-x);
}

/* Header */
.l1-header { padding-bottom: 18px; border-bottom: 1px solid var(--hairline); margin-bottom: var(--gap-lg); }
.l1-header h1 { margin: 0 0 4px 0; font-size: 26px; font-weight: 700; letter-spacing: -0.01em; }
.l1-header .l1-subtitle { color: var(--muted); font-size: 13px; }

/* Section */
.dash-section { margin-bottom: var(--gap-lg); }
.dash-section-header { display: flex; align-items: baseline; gap: 12px; margin-bottom: 10px; padding-bottom: 6px; border-bottom: 1px solid var(--hairline); }
.dash-section-title { font-size: 15px; font-weight: 600; letter-spacing: 0.01em; }
.dash-section-count { color: var(--muted); font-size: 12.5px; font-family: var(--mono); }

/* Filter strip — feed only */
.dash-filters { display: flex; flex-wrap: wrap; gap: 10px; margin-bottom: var(--gap); padding: 10px 12px; background: var(--paper); border: 1px solid var(--border); border-radius: 8px; font-size: 12.5px; }
.dash-filters .filter-label { color: var(--muted); margin-right: 4px; }
.dash-filters .filter-value { font-family: var(--mono); color: var(--fg); }

/* Alert cards */
.alert-card {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 10px;
  padding: var(--card-pad);
  margin-bottom: var(--gap);
}
.alert-card-head { display: flex; flex-wrap: wrap; align-items: center; gap: 10px; margin-bottom: 8px; }
.ticker-badge {
  display: inline-flex; align-items: center;
  padding: 4px 10px;
  border: 1px solid var(--accent);
  background: var(--accent-soft);
  color: var(--accent);
  border-radius: 6px;
  font-family: var(--mono); font-weight: 600; font-size: 12px;
  letter-spacing: 0.05em;
}
.trigger-badge {
  display: inline-flex; align-items: center;
  padding: 2px 8px;
  border: 1px solid var(--border-2);
  border-radius: 4px;
  color: var(--fg-soft);
  font-family: var(--mono); font-size: 10.5px;
  letter-spacing: 0.04em;
  text-transform: uppercase;
}
.status-badge {
  display: inline-flex; align-items: center;
  padding: 2px 8px;
  border-radius: 4px;
  font-family: var(--mono); font-size: 10.5px;
  letter-spacing: 0.04em;
  text-transform: uppercase;
  border: 1px solid var(--border-2);
}
.status-pending  { color: var(--warn); border-color: var(--warn); }
.status-approved { color: var(--ok);   border-color: var(--ok); }
.status-dismissed { color: var(--muted); border-color: var(--muted); }
.status-expired  { color: var(--muted-2); border-color: var(--muted-2); }
.fired-at { color: var(--muted); font-family: var(--mono); font-size: 11.5px; margin-left: auto; }

.alert-memo {
  margin: 6px 0 10px 0;
  padding: 10px 12px;
  background: var(--paper);
  border-left: 3px solid var(--accent);
  border-radius: 0 6px 6px 0;
  font-family: var(--serif);
  font-size: 14px;
  color: var(--fg-soft);
}
.alert-memo-pending { color: var(--muted); font-style: italic; }

/* Queued actions */
.queued-actions { margin: 10px 0 0 0; }
.queued-actions h4 { font-size: 12.5px; font-weight: 600; margin: 0 0 6px 0; color: var(--muted); letter-spacing: 0.04em; text-transform: uppercase; }
.queued-action {
  display: flex; flex-wrap: wrap; gap: 10px; align-items: flex-start;
  padding: 8px 10px;
  background: var(--paper);
  border: 1px solid var(--hairline);
  border-radius: 6px;
  margin-bottom: 6px;
}
.qa-kind {
  display: inline-flex; align-items: center;
  padding: 2px 6px;
  border: 1px solid var(--border-2);
  background: var(--surface);
  color: var(--fg-soft);
  border-radius: 3px;
  font-family: var(--mono); font-size: 10.5px;
  letter-spacing: 0.04em;
}
.qa-body { flex: 1; min-width: 200px; color: var(--fg-soft); font-size: 13px; }
.qa-actions {
  display: flex; gap: 6px; align-items: center;
  font-family: var(--mono); font-size: 11px;
}
.qa-actions a {
  display: inline-flex;
  padding: 3px 8px;
  border: 1px solid var(--border-2);
  border-radius: 4px;
  color: var(--accent);
}
.qa-actions .qa-cli { color: var(--muted); }
.qa-status-applied { color: var(--ok); }
.qa-status-cancelled { color: var(--muted); }

/* Evidence drawer */
.evidence-drawer {
  margin-top: 10px;
  background: var(--paper);
  border: 1px solid var(--hairline);
  border-radius: 6px;
}
.evidence-drawer > summary {
  cursor: pointer;
  padding: 8px 12px;
  color: var(--muted);
  font-family: var(--mono); font-size: 11.5px;
  letter-spacing: 0.04em;
  text-transform: uppercase;
  user-select: none;
}
.evidence-drawer[open] > summary { border-bottom: 1px solid var(--hairline); }
.evidence-body { padding: 10px 12px; }
.evidence-section { margin-bottom: 10px; }
.evidence-section:last-child { margin-bottom: 0; }
.evidence-section-title {
  font-size: 11px;
  text-transform: uppercase;
  letter-spacing: 0.06em;
  color: var(--muted);
  margin-bottom: 4px;
}
.evidence-summary-text { color: var(--fg-soft); font-family: var(--serif); font-size: 13.5px; }
.evidence-malformed {
  padding: 10px;
  background: rgba(240, 138, 138, 0.08);
  border: 1px solid var(--bad);
  border-radius: 4px;
  color: var(--bad);
  margin-bottom: 10px;
}
.evidence-citations-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 12.5px;
}
.evidence-citations-table th {
  text-align: left;
  padding: 4px 8px 4px 0;
  color: var(--muted);
  font-weight: 500;
  font-size: 10.5px;
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
.cite-prov { color: var(--muted); font-family: var(--mono); font-size: 11.5px; white-space: nowrap; }
.prov-source { color: var(--accent); }
.prov-fetched { color: var(--muted); }
.evidence-raw { margin-top: 4px; }
.evidence-raw > summary {
  cursor: pointer;
  color: var(--muted);
  font-family: var(--mono); font-size: 11px;
  letter-spacing: 0.04em;
}
.evidence-raw-pre {
  margin: 6px 0 0 0;
  padding: 8px 10px;
  background: var(--bg);
  border: 1px solid var(--hairline);
  border-radius: 4px;
  font-family: var(--mono); font-size: 11.5px;
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
  border-radius: 8px;
  color: var(--muted);
  font-size: 13.5px;
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
  font-size: 11px;
  letter-spacing: 0.04em;
}
"""
