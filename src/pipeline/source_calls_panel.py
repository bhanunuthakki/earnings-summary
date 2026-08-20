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

import sqlite3
from dataclasses import dataclass
from html import escape
from pathlib import Path

from pipeline.research_panel_styles import RESEARCH_PANEL_STYLE
from sources.registry import (
    CacheEffectivenessOverview,
    SourceCallSummary,
    cache_effectiveness_overview,
)
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite
from ui import living_grid as lg

_PANEL_STYLE = RESEARCH_PANEL_STYLE


def render_source_calls_panel(db_path: Path) -> str:
    """The Data Cache tab fragment: a cache-effectiveness KPI strip + a
    per-(source, kind) table, followed by the shell's panel-latency readout
    (S14). Degrades to an empty-state when nothing is logged."""
    ov = cache_effectiveness_overview(db_path=db_path)
    action_usage = render_action_usage_section(db_path)
    if ov.total_calls == 0:
        return (
            _PANEL_STYLE + '<section class="panel"><h2>Data fetch cache</h2>'
            '<p class="muted">No source-call rows yet. Adapters in <code>src/sources/</code> '
            "log to <code>source_calls</code> on every fetch attempt; the table fills as the "
            "daily jobs run.</p></section>" + _PANEL_LATENCY_SECTION + action_usage
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
            action_usage,
        ]
    )


# Operational panel-latency readout (S14): the panel loader
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
    var html = '<table class="p-table sc-table"><thead><tr><th>Panel</th><th>Path</th>' +
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
        lg.grid_open()
        + lg.filter_bar(len(rows), noun="sources", placeholder="Filter by source / kind…")
        + '<table class="p-table sc-table"><thead><tr>'
        + lg.th("Source", "source", "text", num=False)
        + lg.th("Kind", "kind", "text", num=False)
        + lg.th("Calls", "calls", "num")
        + lg.th("Skip%", "skip", "num")
        + lg.th("Err%", "err", "num")
        + lg.th("Saved", "saved", "num")
        + lg.th("p50 ms", "p50", "num")
        + lg.th("Records", "records", "num")
        + "</tr></thead><tbody>"
        + f"{body}</tbody></table>"
        + lg.grid_close()
    )


def _row(r: SourceCallSummary) -> str:
    skip_cls = "sc-skip-hi" if r.cache_skip_rate >= 0.5 else "sc-skip-lo"
    err_cls = "sc-err" if r.error_rate > 0.05 else ""
    p50 = "—" if r.p50_latency_ms is None else f"{r.p50_latency_ms:,}"
    data = (
        lg.data_text(f"{r.source_name} {r.kind}")
        + lg.data_text_key("source", r.source_name)
        + lg.data_text_key("kind", r.kind)
        + lg.data_num("calls", r.total)
        + lg.data_num("skip", r.cache_skip_rate)
        + lg.data_num("err", r.error_rate)
        + lg.data_num("saved", r.calls_saved)
        + lg.data_num("p50", float(r.p50_latency_ms) if r.p50_latency_ms is not None else None)
        + lg.data_num("records", r.total_records)
    )
    return (
        f"<tr{data}>"
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


# ---------------------------------------------------------------------------
# Ledger action usage (30d) — the read-side of Phase C's instrument-first ruling
# ("does the owner actually USE each Ledger action?"). The comments-server routes
# UPSERT durable ``act:<family>[:<action>]`` counters into
# ``panel_activation_counts`` on every feed action; this section is the only
# surface that reads them back, grouped into families so the strongest-used
# actions read first. Server-rendered (the counts live in the DB), unlike the
# in-memory panel-latency ring next to it.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ActionUsageRow:
    """One ``act:*`` counter, split into its family + action for grouping."""

    panel_id: str
    family: str
    action: str
    count: int


def _split_family_action(panel_id: str) -> tuple[str, str]:
    """Map an ``act:*`` panel_id to ``(family, action)``.

    The family is the segment immediately after ``act:``; ``research_run`` /
    ``research_reject`` collapse into one ``research`` family (task grouping),
    and a single-segment id (e.g. ``act:capture``) is its own family with the
    same word as its lone action.
    """
    rest = panel_id[4:] if panel_id.startswith("act:") else panel_id
    if rest.startswith("research_"):
        return ("research", rest[len("research_") :] or "run")
    if ":" in rest:
        family, action = rest.split(":", 1)
        return (family, action)
    return (rest, rest)


def _read_action_usage(db_path: Path) -> list[ActionUsageRow]:
    """Read the 30-day ``act:*`` activation totals.

    Read-only and best-effort: a missing DB / table (fresh install before the
    first feed action creates it) yields ``[]`` rather than raising, so the
    section degrades to a clean empty-state. Mirrors the window filter the
    ``GET /api/metrics/panel`` aggregate uses.
    """
    if not Path(db_path).exists():
        return []
    sql = (
        "SELECT panel_id, SUM(count) AS n FROM panel_activation_counts"
        " WHERE panel_id LIKE 'act:%' AND day >= date('now', '-30 days')"
        " GROUP BY panel_id"
    )
    try:
        conn = connect_sqlite(db_path, role=SQLiteConnectionRole.READ_ONLY)
        try:
            rows = conn.execute(sql).fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        return []
    out: list[ActionUsageRow] = []
    for panel_id, n in rows:
        family, action = _split_family_action(str(panel_id))
        out.append(
            ActionUsageRow(panel_id=str(panel_id), family=family, action=action, count=int(n or 0))
        )
    return out


_ACTION_USAGE_EMPTY = (
    '<section class="panel"><h2>Ledger action usage (30d)</h2>'
    '<div class="k-well"><p class="muted">No Ledger actions recorded in the last 30 days — '
    "the counters fill as you work the feed.</p></div></section>"
)


def render_action_usage_section(db_path: Path) -> str:
    """Server-rendered ``act:*`` usage: a family-total strip + a per-action
    table, families ordered by total desc. Degrades to a clean empty-state when
    no Ledger actions were recorded in the window."""
    rows = _read_action_usage(db_path)
    if not rows:
        return _ACTION_USAGE_EMPTY

    families: dict[str, list[ActionUsageRow]] = {}
    for r in rows:
        families.setdefault(r.family, []).append(r)
    fam_totals = {fam: sum(r.count for r in rs) for fam, rs in families.items()}
    ordered = sorted(families, key=lambda f: (-fam_totals[f], f))
    grand_total = sum(fam_totals.values())

    return "".join(
        [
            '<section class="panel"><h2>Ledger action usage (30d)</h2>',
            '<p class="sub">How often each Ledger action was actually fired over the last '
            "30 days — the read-side of the instrument-first ruling. "
            f"<strong>{grand_total:,}</strong> actions across "
            f"<strong>{len(families)}</strong> families.</p>",
            _family_strip(ordered, fam_totals),
            _action_table(ordered, families, len(rows)),
            "</section>",
        ]
    )


def _family_strip(ordered: list[str], fam_totals: dict[str, int]) -> str:
    chips = "".join(
        f'<span class="k-pill">{escape(fam)} <strong>{fam_totals[fam]:,}</strong></span>'
        for fam in ordered
    )
    return f'<div class="k-well"><div class="au-strip">{chips}</div></div>'


def _action_table(
    ordered: list[str], families: dict[str, list[ActionUsageRow]], n_rows: int
) -> str:
    body_parts: list[str] = []
    for fam in ordered:
        for r in sorted(families[fam], key=lambda x: (-x.count, x.action)):
            data = (
                lg.data_text(f"{fam} {r.action}")
                + lg.data_text_key("family", fam)
                + lg.data_text_key("action", r.action)
                + lg.data_num("count", r.count)
            )
            body_parts.append(
                f"<tr{data}>"
                f'<td><span class="k-chip k-chip-mono">{escape(fam)}</span></td>'
                f'<td><span class="k-chip">{escape(r.action)}</span></td>'
                f'<td class="num">{r.count:,}</td>'
                "</tr>"
            )
    return (
        lg.grid_open()
        + lg.filter_bar(n_rows, noun="actions", placeholder="Filter by family / action…")
        + '<table class="p-table sc-table"><thead><tr>'
        + lg.th("Family", "family", "text", num=False)
        + lg.th("Action", "action", "text", num=False)
        + lg.th("Activations", "count", "num")
        + "</tr></thead><tbody>"
        + "".join(body_parts)
        + "</tbody></table>"
        + lg.grid_close()
    )
