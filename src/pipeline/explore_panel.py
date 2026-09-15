"""Narrative-first Explore with an on-demand analytical Work Bench.

Explore renders the governed Ask stream inline. A compact ``Work with data``
doorway expands the current question or ViewSpec into a full-canvas,
deterministic analytics workspace. Modeling remains owned by the DCF.
"""

from __future__ import annotations

import json
import sqlite3
from html import escape
from pathlib import Path

from identity import DEFAULT_USER_ID
from pipeline.explore_panel_runtime import EXPLORE_PANEL_JS
from pipeline.research_panel_styles import RESEARCH_PANEL_STYLE
from report.models import CellSource
from report.renderers.charts_v2 import fmt_compact, fmt_pct
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite
from ui.cite_marks import CITE_MARKS_SNIPPET
from ui.source_chip import source_chip_html
from user_state.saved_views import SavedViewRow, list_views
from viewspec.engine import metric_catalog
from viewspec.spec import CADENCES, TRANSFORMS
from viewspec.workbench import RankedMetricWorkbench

_PANEL_STYLE = RESEARCH_PANEL_STYLE


def _saved_chip(view: SavedViewRow) -> str:
    spec_attr = escape(json.dumps(view.spec))
    return (
        f'<span class="vx-saved" data-view-id="{view.id}" '
        f'data-view-name="{escape(view.name)}" data-spec="{spec_attr}">'
        f'<button type="button" class="k-btn k-btn-sm k-btn-quiet" data-act="load" '
        f'title="load + run">{escape(view.name)}</button>'
        '<button type="button" class="k-btn k-btn-sm k-btn-quiet" data-act="del" '
        'title="delete">&times;</button></span>'
    )


def render_saved_views_list(db_path: Path, *, user_id: str = DEFAULT_USER_ID) -> str:
    """Render the saved ViewSpec strip used by the Explore fragment route."""
    try:
        views = list_views(user_id=user_id, db_path=db_path)
    except (sqlite3.Error, FileNotFoundError, RuntimeError):
        views = []
    if not views:
        return '<span class="vx-none">No saved analyses yet.</span>'
    return "".join(_saved_chip(view) for view in views)


def _default_tickers(db_path: Path, user_id: str) -> list[str]:
    """Return the tracked portfolio universe without inventing a local store."""
    if not db_path.exists():
        return []
    try:
        conn = connect_sqlite(db_path, role=SQLiteConnectionRole.READ_ONLY)
    except sqlite3.Error:
        return []
    try:
        rows = conn.execute(
            "SELECT ticker FROM tracked_companies "
            "WHERE user_id = ? AND list_type = 'portfolio' ORDER BY ticker",
            (user_id,),
        ).fetchall()
        return [str(row[0]) for row in rows]
    except sqlite3.Error:
        return []
    finally:
        conn.close()


def render_keymetrics_fragment(db_path: Path, tickers: list[str]) -> str:
    """Render the ranked suggestion chips for the requested company universe."""
    from pipeline.key_metrics import key_metric_bubbles, render_key_metrics_inner

    symbols = [ticker.strip().upper() for ticker in tickers if ticker.strip()]
    catalog: dict[str, list[dict[str, object]]] = (
        metric_catalog(db_path, symbols)
        if symbols
        else {"fin": [], "kpi": [], "seg": [], "detail": []}
    )
    return render_key_metrics_inner(key_metric_bubbles(db_path, symbols, catalog), symbols)


def _workbench_value(value: float, unit: str | None) -> str:
    if (unit or "").lower() in {"%", "percent"}:
        return f"{value:.1f}%"
    if (unit or "").lower() == "bps":
        return f"{value:.0f}bps"
    return fmt_compact(value)


def render_ranked_workbench(workbench: RankedMetricWorkbench) -> str:
    """Render governed ranked metrics for consumers that still embed the recipe."""
    if workbench.state == "unavailable":
        return '<section class="k-well vx-workbench vx-workbench-state"><strong>Metrics unavailable</strong><span>Local fact data is unavailable.</span></section>'
    if workbench.state == "empty":
        return '<section class="k-well vx-workbench vx-workbench-state"><strong>No ranked metrics yet</strong><span>Search the governed fact catalog.</span></section>'
    if workbench.state == "stale":
        return '<section class="k-well vx-workbench vx-workbench-state"><strong>Ranked metrics need fresh facts</strong><span>The ranked list has no current matching observations.</span></section>'
    cards: list[str] = []
    for row in workbench.rows:
        trend = fmt_pct(row.change_pct) if row.change_pct is not None else "—"
        cell_source = row.source if isinstance(row.source, CellSource) else None
        source = (
            source_chip_html(cell_source)
            if cell_source is not None
            else '<span class="vx-workbench-muted">source unavailable</span>'
        )
        rank = "Thesis tier" if row.rank_source == "tier" else "Cached research rank"
        cards.append(
            '<article class="vx-workbench-card">'
            f'<div class="vx-workbench-kicker">{escape(row.ticker)} · {escape(rank)}</div>'
            f'<strong title="{escape(row.token)}">{escape(row.label)}</strong>'
            f'<div class="vx-workbench-value">{escape(_workbench_value(row.value, row.unit))}</div>'
            f'<div class="vx-workbench-meta">Trend {escape(trend)} · as of {escape(row.as_of)} {source}</div>'
            f'<div class="vx-workbench-why">Why it matters: {escape(row.why)}</div>'
            f'<details class="vx-workbench-inspect"><summary>Inspect metric</summary><code>{escape(row.token)}</code></details>'
            "</article>"
        )
    return (
        '<section class="vx-workbench" aria-label="Ranked metrics">'
        '<div class="vx-workbench-head"><strong>Ranked metrics</strong><span>Current governed facts</span></div>'
        f'<div class="vx-workbench-grid">{"".join(cards)}</div></section>'
    )


def render_explore_panel(
    db_path: Path,
    *,
    user_id: str = DEFAULT_USER_ID,
    initial_tickers: list[str] | None = None,
    include_runtime: bool = True,
) -> str:
    """Render the consolidated Explore thread and its on-demand Work Bench."""
    tickers = (
        [ticker.strip().upper() for ticker in initial_tickers if ticker.strip()]
        if initial_tickers is not None
        else _default_tickers(db_path, user_id)
    )
    ticker = tickers[0] if tickers else ""
    catalog: dict[str, list[dict[str, object]]] = (
        metric_catalog(db_path, [ticker])
        if ticker
        else {"fin": [], "kpi": [], "seg": [], "detail": []}
    )
    from pipeline.key_metrics import key_metric_bubbles

    bubbles = key_metric_bubbles(db_path, [ticker] if ticker else [], catalog)
    suggestion_chips = "".join(
        f'<button type="button" class="k-chip k-chip-btn" '
        f'data-ask-q="Tell me about {escape(b.label)} for {escape(ticker)}" '
        f'title="{escape(b.title)}">{escape(b.label)}</button>'
        for b in bubbles[:6]
    )
    if not suggestion_chips:
        company = ticker or "this company"
        suggestion_chips = (
            f'<button type="button" class="k-chip k-chip-btn" data-ask-q="What changed most recently for {escape(company)}?">What changed?</button>'
            f'<button type="button" class="k-chip k-chip-btn" data-ask-q="Tell me about growth for {escape(company)}">Growth</button>'
            f'<button type="button" class="k-chip k-chip-btn" data-ask-q="What are the key risks for {escape(company)}?">Key risks</button>'
        )
    transform_options = "".join(
        f'<option value="{escape(value)}"{" selected" if value == "level" else ""}>{escape(value.title())}</option>'
        for value in TRANSFORMS
    )
    cadence_options = "".join(
        f'<option value="{escape(value)}">{escape(value.title())}</option>' for value in CADENCES
    )
    company_options = (
        "".join(
            f'<option value="{escape(symbol)}"{" selected" if symbol == ticker else ""}>{escape(symbol)}</option>'
            for symbol in tickers
        )
        or '<option value="">Company</option>'
    )
    runtime = f"<script>{EXPLORE_PANEL_JS}</script>" if include_runtime else ""
    cite_assets = CITE_MARKS_SNIPPET if include_runtime else ""
    return f"""{_PANEL_STYLE}
{cite_assets}
<div id="vx-root" class="explore-root">
  <section class="explore-conversation" aria-label="Explore company research">
    <div class="ask-thread" id="ask-thread"><div class="explore-empty" id="explore-empty"><p>Ask a company question. Explore answers in narrative, grounds material claims in governed data, and offers deeper analysis only when you want it.</p><div class="explore-suggestions" aria-label="Suggested questions">{suggestion_chips}</div></div></div>
    <div class="explore-compose"><input id="ask-q" aria-label="Ask Explore" autocomplete="off" placeholder="Ask about growth, margins, segment mix, management claims, or what changed…"><button type="button" id="ask-go" class="k-btn k-btn-primary">Ask</button></div>
    <div class="explore-compose-meta"><span id="explore-status">Grounded in reported facts, source documents, and clearly labeled inference.</span><button type="button" id="vx-open-empty" class="k-chip k-chip-btn">Work with data ↗</button></div>
  </section>

  <dialog class="vx-workbench-page" id="vx-workbench" aria-label="Analytics Work Bench">
    <header class="vx-workbench-topbar"><button type="button" class="k-btn k-btn-quiet" id="vx-back">← Back to Explore</button><div class="vx-workbench-heading"><div class="k-label">Explore / Analytics</div><h1 id="vx-workbench-title">{escape(ticker or "Company")} analysis</h1><p id="vx-active-prompt">New company-data analysis</p></div><div class="vx-workbench-actions"><label class="vx-company-select"><span class="k-label">Company</span><select class="k-select" id="vx-workbench-company" aria-label="Analytics company" data-k-select-default="true">{company_options}</select></label><input id="vx-view-name" placeholder="Analysis name" aria-label="Analysis name"><button type="button" class="k-btn k-btn-quiet" id="vx-save">Save analysis</button><span class="vx-save-status" id="vx-save-status" role="status"></span><div class="vx-saved-wrap"><button type="button" class="k-btn k-btn-quiet" id="vx-saved-toggle" aria-expanded="false" aria-controls="vx-saved-panel">Saved</button><div class="k-menu vx-saved-panel" id="vx-saved-panel" hidden><div class="k-label">Saved analyses</div><div id="vx-saved-list"><span class="vx-none">Loading…</span></div></div></div></div></header>
    <div class="vx-workbench-grid">
      <aside class="vx-fields-rail" id="vx-fields-rail" hidden><div class="vx-rail-head"><div><span class="k-label">All fields</span><p>Queryable statement, KPI, and segment facts, plus source-backed annual customer-concentration and lease-commitment series. Definition-pending detail stays explicitly labeled.</p></div><button type="button" class="k-btn k-btn-sm k-btn-quiet" id="vx-fields-minimize">Minimize</button></div><div class="vx-field-catalog" id="vx-field-catalog"></div></aside>
      <div class="vx-resizer vx-fields-resizer" id="vx-fields-resizer" role="separator" aria-label="Resize fields rail" aria-orientation="vertical" aria-valuemin="240" aria-valuemax="520" aria-valuenow="280" tabindex="0"></div>
      <main class="vx-analysis-canvas">
        <section class="vx-operating-band" aria-label="Shape and fields"><div class="vx-shape-row"><span class="k-label">Shape</span><label>Transform<select id="vx-transform">{transform_options}</select></label><label>Cadence<select id="vx-cadence">{cadence_options}</select></label><label>Periods<input id="vx-periods" type="number" min="1" max="40" value="8"></label><label>CAGR years<input id="vx-cagr-years" type="number" min="1" max="10" value="3"></label><button type="button" class="k-btn k-btn-primary" id="vx-run">Run analysis</button></div><div class="vx-fields-band"><div class="vx-fields-label"><span class="k-label">Fields</span><span id="vx-selected-count">0 fields</span></div><div class="vx-selected-fields" id="vx-selected-fields"></div><div class="vx-field-search-wrap"><input type="search" id="vx-field-search" autocomplete="off" placeholder="Search cached company facts…" aria-label="Search cached company facts"><div class="k-menu vx-field-suggestions" id="vx-field-suggestions" hidden></div></div><button type="button" class="k-chip k-chip-btn" id="vx-browse-fields">Browse all</button></div></section>
        <div class="vx-result-toolbar" role="group" aria-label="Result view"><button type="button" class="k-chip k-chip-tab is-on" data-result-view="both" aria-pressed="true">Table + chart</button><button type="button" class="k-chip k-chip-tab" data-result-view="table" aria-pressed="false">Table</button><button type="button" class="k-chip k-chip-tab" data-result-view="chart" aria-pressed="false">Chart</button></div>
        <div id="vx-result" class="vx-analysis-result"><div class="vx-none">Choose fields, shape the window, and run. Canonical facts retain period, unit, definition, and source provenance; definition-pending detail is visibly marked and restricted to supported shapes.</div></div>
      </main>
      <div class="vx-resizer vx-inspector-resizer" id="vx-inspector-resizer" role="separator" aria-label="Resize metric inspector" aria-orientation="vertical" aria-valuemin="260" aria-valuemax="560" aria-valuenow="340" tabindex="0"></div>
      <aside class="vx-inspector" id="vx-inspector" hidden><div class="vx-rail-head"><div><span class="k-label">Metric definition</span><h2 id="vx-inspector-title">Metric</h2></div><button type="button" class="k-btn k-btn-sm k-btn-quiet" id="vx-inspector-close">Minimize</button></div><p id="vx-inspector-definition"></p><dl class="vx-inspector-facts"><div><dt>Canonical field</dt><dd id="vx-inspector-token"></dd></div><div><dt>Origin</dt><dd id="vx-inspector-origin"></dd></div></dl></aside>
    </div>
    <footer class="vx-workbench-compose"><div><span class="k-label">Refine this analysis</span><p>Describe a comparison or add a metric; Explore will reshape the governed ViewSpec.</p></div><input id="vx-workbench-q" autocomplete="off" aria-label="Refine analysis" placeholder="e.g. compare segment growth over 8 complete quarters"><button type="button" class="k-btn k-btn-primary" id="vx-workbench-go">Apply</button></footer>
  </dialog>
  <input id="vx-tickers" type="hidden" value="{escape(ticker)}">
</div>
{runtime}"""


__all__ = [
    "EXPLORE_PANEL_JS",
    "render_explore_panel",
    "render_keymetrics_fragment",
    "render_ranked_workbench",
    "render_saved_views_list",
]
