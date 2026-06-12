"""Data-cache effectiveness panel for the command-center shell.

Surfaces what the external-fetch cache actually saved — the metric that was
previously only reachable from the ``execution/show_source_calls.py`` CLID (v6
re-grade, Smart caching: "cache-hit-rate is not surfaced in any user-facing
route"). Reads ``source_calls`` via ``sources.registry.cache_effectiveness_overview``:
a KPI strip (overall cache-skip rate · network calls avoided · [dollars saved]) +
a per-(source, kind) table of volume / skip% / error% / latency.

``cost_saved`` is shown only when ``SOURCE_COST_PER_CALL_USD`` is configured —
FMP/SEC are flat-rate, so the honest headline is the *count* of network calls the
cache avoided. Reuses the shell's dark panel / kpi-strip / table CSS vocabulary.
"""

from __future__ import annotations

from html import escape
from pathlib import Path

from sources.registry import (
    CacheEffectivenessOverview,
    SourceCallSummary,
    cache_effectiveness_overview,
)

_PANEL_STYLE = """<style>
.sc-table { width:100%; border-collapse:collapse; font-size:var(--fs-body); }
.sc-table th, .sc-table td {
  padding:6px 10px; border-bottom:1px solid var(--border); text-align:left; }
.sc-table td.num, .sc-table th.num { text-align:right; font-variant-numeric:tabular-nums; }
.sc-table td.src { font-weight:600; }
.sc-skip-hi { color:var(--ok); }
.sc-skip-lo { color:var(--muted); }
.sc-err { color:var(--bad); }
.sc-note { margin-top:14px; padding:10px 13px; background:var(--paper);
  border:1px solid var(--border); border-radius:var(--radius); font-size:var(--fs-body);
  line-height:1.55; }
.sc-note code { background:var(--surface); padding:1px 5px; border-radius:4px; }
</style>"""


def render_source_calls_panel(db_path: Path) -> str:
    """The Data Cache tab fragment: a cache-effectiveness KPI strip + a
    per-(source, kind) table, followed by the shell's panel-latency readout
    (S14). Degrades to an empty-state when nothing is logged."""
    ov = cache_effectiveness_overview(db_path=db_path)
    if ov.total_calls == 0:
        return (
            _PANEL_STYLE + '<section class="panel"><h2>Data fetch cache</h2>'
            '<p class="muted">No source-call rows yet. Adapters in <code>src/sources/</code> '
            "log to <code>source_calls</code> on every fetch attempt; the table fills as the "
            "daily jobs run.</p></section>" + _PANEL_LATENCY_SECTION
        )
    return "".join(
        [
            _PANEL_STYLE,
            '<section class="panel"><h2>Data fetch cache</h2>',
            '<p class="sub">How much external-fetch work the cache avoided across every '
            "FMP / SEC / market-data adapter. A high <strong>skip rate</strong> means most "
            "requests were served from cache instead of hitting the network.</p>",
            _kpi_strip(ov),
            _source_table(ov.by_source),
            _note(ov),
            "</section>",
            _PANEL_LATENCY_SECTION,
        ]
    )


# Shell panel-latency readout (S14): the loader in command_center_shell.py
# POSTs one sample per panel activation/refresh to /api/metrics/panel; this
# section reads the GET aggregate back. Client-rendered by the fragment's own
# script (re-executed on every injection — the shell's injectHtml recreates
# script tags), so the section needs no server-side plumbing beyond the route.
_PANEL_LATENCY_SECTION = """<section class="panel" id="sc-panel-latency">
<h2>Panel latency</h2>
<p class="sub">What tab activations actually cost, as measured by the shell loader:
<code>cold</code> = first build over the network, <code>swr</code> /
<code>prefetch</code> = painted from the session cache, <code>revalidate</code> =
the background refresh behind a cached paint (304 = unchanged). In-memory ring —
resets with the server.</p>
<div id="sc-lat-body"><div class="cc-loading">Loading…</div></div>
<script>
(function () {
  var holder = document.getElementById('sc-lat-body');
  if (!holder) return;
  function esc(s) {
    return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  }
  function ms(x) { return x == null ? '—' : x + ' ms'; }
  fetch('/api/metrics/panel').then(function (r) { return r.json(); }).then(function (j) {
    var rows = (j && j.rows) || [];
    if (!rows.length) {
      holder.innerHTML = '<p class="muted">No samples yet this server run — ' +
        'switch a few tabs, then reopen this panel.</p>';
      return;
    }
    var head = '<p class="sub">Perceived activation p50 <strong>' + esc(ms(j.perceived_p50_ms)) +
      '</strong> · p95 <strong>' + esc(ms(j.perceived_p95_ms)) + '</strong> over ' +
      esc(j.samples) + ' samples (revalidations excluded).</p>';
    var html = '<table class="sc-table"><thead><tr><th>Panel</th><th>Path</th>' +
      '<th class="num">Loads</th><th class="num">p50 ms</th><th class="num">p95 ms</th>' +
      '</tr></thead><tbody>';
    rows.forEach(function (r) {
      html += '<tr><td class="src">' + esc(r.panel) + '</td><td>' + esc(r.cache) + '</td>' +
        '<td class="num">' + esc(r.n) + '</td><td class="num">' + esc(r.p50_ms) + '</td>' +
        '<td class="num">' + esc(r.p95_ms) + '</td></tr>';
    });
    holder.innerHTML = head + html + '</tbody></table>';
  }).catch(function () {
    holder.innerHTML = '<p class="muted">Metrics endpoint unavailable.</p>';
  });
})();
</script>
</section>"""


def _pct(x: float) -> str:
    return f"{x * 100:.1f}%"


def _kpi_strip(ov: CacheEffectivenessOverview) -> str:
    skip_tone = "tone-good" if ov.cache_skip_rate >= 0.5 else "tone-warn"
    err_tone = "tone-bad" if ov.error_rate > 0.05 else "tone-good"
    cards = [
        f'<div class="kpi-card {skip_tone}"><div class="kpi-label">Cache skip rate</div>'
        f'<div class="kpi-value">{_pct(ov.cache_skip_rate)}</div>'
        '<div class="kpi-sub">served from cache</div></div>',
        '<div class="kpi-card"><div class="kpi-label">Calls avoided</div>'
        f'<div class="kpi-value">{ov.calls_saved:,}</div>'
        f'<div class="kpi-sub">of {ov.total_calls:,} attempts</div></div>',
        f'<div class="kpi-card {err_tone}"><div class="kpi-label">Error rate</div>'
        f'<div class="kpi-value">{_pct(ov.error_rate)}</div>'
        '<div class="kpi-sub">failed / blocked</div></div>',
    ]
    if ov.cost_saved_usd > 0:
        cards.append(
            '<div class="kpi-card tone-good"><div class="kpi-label">Cost avoided</div>'
            f'<div class="kpi-value">${ov.cost_saved_usd:,.2f}</div>'
            '<div class="kpi-sub">at configured $/call</div></div>'
        )
    return '<div class="kpi-strip">' + "".join(cards) + "</div>"


def _source_table(rows: list[SourceCallSummary]) -> str:
    body = "".join(_row(r) for r in rows)
    return (
        '<table class="sc-table"><thead><tr>'
        "<th>Source</th><th>Kind</th>"
        '<th class="num">Calls</th><th class="num">Skip%</th><th class="num">Err%</th>'
        '<th class="num">Saved</th><th class="num">p50 ms</th><th class="num">Records</th>'
        "</tr></thead><tbody>"
        f"{body}</tbody></table>"
    )


def _row(r: SourceCallSummary) -> str:
    skip_cls = "sc-skip-hi" if r.cache_skip_rate >= 0.5 else "sc-skip-lo"
    err_cls = "sc-err" if r.error_rate > 0.05 else ""
    p50 = "—" if r.p50_latency_ms is None else f"{r.p50_latency_ms:,}"
    return (
        "<tr>"
        f'<td class="src">{escape(r.source_name)}</td>'
        f"<td>{escape(r.kind)}</td>"
        f'<td class="num">{r.total:,}</td>'
        f'<td class="num {skip_cls}">{_pct(r.cache_skip_rate)}</td>'
        f'<td class="num {err_cls}">{_pct(r.error_rate)}</td>'
        f'<td class="num">{r.calls_saved:,}</td>'
        f'<td class="num">{p50}</td>'
        f'<td class="num">{r.total_records:,}</td>'
        "</tr>"
    )


def _note(ov: CacheEffectivenessOverview) -> str:
    cost = (
        f" At the configured marginal cost, that is <strong>${ov.cost_saved_usd:,.2f}</strong> "
        "of fetch spend avoided."
        if ov.cost_saved_usd > 0
        else " Set <code>SOURCE_COST_PER_CALL_USD</code> to also value the avoided calls in "
        "dollars (FMP/SEC are flat-rate, so the headline metric is the call count)."
    )
    return (
        '<div class="sc-note">'
        f"The cache avoided <strong>{ov.calls_saved:,}</strong> of "
        f"<strong>{ov.total_calls:,}</strong> external requests "
        f"(<strong>{_pct(ov.cache_skip_rate)}</strong> skip rate).{cost} "
        "Detail per source / endpoint is also available from "
        "<code>python execution/show_source_calls.py</code> and "
        "<code>GET /api/source-calls</code>."
        "</div>"
    )
