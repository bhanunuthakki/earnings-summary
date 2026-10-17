"""Narrative-first Explore with an on-demand analytical Work Bench.

Explore renders the governed Ask stream inline. A compact ``Work with data``
doorway expands the current question or ViewSpec into a full-canvas,
deterministic analytics workspace. Modeling remains owned by the DCF.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from html import escape
from pathlib import Path
from typing import Literal, cast

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


# ---------------------------------------------------------------------------
# Render-side "key metrics" bubble row for the DIY picker (folded in from the
# former ``pipeline.key_metrics``; directives/key_metrics_picker.md).
#
# Builds the clickable preselect chips shown above the Explore picker's
# ``<select multiple>`` set. Two sources, merged here:
#
# * **Tier-graded baseline (always, NO LLM, NO cache)** — the tier-1/tier-2 KPI
#   grading in ``kpi_definitions.threshold_tier`` (the thesis breakers). Pulled
#   straight from the DB for the selected tickers.
# * **LLM picks (cache read only)** — ``data/key_metrics/{TICKER}.json``,
#   written on the ``--enable-llm`` build by ``src/compute/key_metrics.py``.
#   Read here as plain JSON — NO heavy import on the render path (mirrors how
#   ``report.sections.p3_data`` reads the peer_selection cache). Absent /
#   malformed cache → the tier-graded baseline alone.
#
# Every bubble token is validated against the live picker catalog so a click
# always maps to a real ``<option>``; the bubble's domain prefix (``fin:`` /
# ``kpi:``) routes it to the right ``<select>`` in the panel's inline JS.
# ---------------------------------------------------------------------------

# Combined ceiling on the rendered row — tier baseline first, then LLM picks
# fill the remainder. Keeps the row to one scannable line, not a wall of chips.
_MAX_BUBBLES = 12

_TIER_TITLE = {
    "tier_1_break": "Tier-1 thesis KPI (a breach breaks the thesis)",
    "tier_2_monitor": "Tier-2 monitored KPI",
}


@dataclass(frozen=True)
class KeyMetricBubble:
    token: str
    label: str
    source: Literal["tier", "llm"]
    title: str  # tooltip text


# ---------------------------------------------------------------------------
# data gathering
# ---------------------------------------------------------------------------


def _catalog_index(
    catalog: dict[str, list[dict[str, object]]],
) -> tuple[set[str], dict[str, str]]:
    """``(valid_tokens, label_by_token)`` from the picker catalog — the closed
    set of tokens a bubble may reference (so every chip maps to an option)."""
    valid: set[str] = set()
    labels: dict[str, str] = {}
    for entries in catalog.values():
        for entry in entries:
            token = entry.get("token")
            if not isinstance(token, str) or not token:
                continue
            valid.add(token)
            label = entry.get("label")
            if isinstance(label, str) and label:
                labels[token] = label
    return valid, labels


def tier_graded_baseline(db_path: Path, tickers: list[str]) -> list[tuple[str, str]]:
    """``(token, tier)`` for every tier-1/tier-2 graded KPI (with ≥1 fact) across
    the selected tickers, tier-1 first then by how many tickers carry it. Tokens
    are ``kpi:<name>``. Best-effort: a missing DB/table yields ``[]``.

    Reads only ``threshold_tier`` (a long-standing column) — safe on a DB that
    has not yet migrated the capture-program ``definition_origin`` column."""
    symbols = [t.strip().upper() for t in tickers if t.strip()]
    if not symbols or not db_path.exists():
        return []
    marks = ",".join("?" * len(symbols))
    try:
        conn = connect_sqlite(db_path, role=SQLiteConnectionRole.READ_ONLY)
    except sqlite3.Error:
        return []
    try:
        rows = conn.execute(
            f"""
            SELECT kd.name AS name, kd.threshold_tier AS tier,
                   COUNT(DISTINCT kf.ticker) AS n
            FROM kpi_definitions kd
            JOIN kpi_facts kf ON kf.kpi_definition_id = kd.id
            WHERE kd.threshold_tier IN ('tier_1_break', 'tier_2_monitor')
              AND kf.ticker IN ({marks})
            GROUP BY kd.name, kd.threshold_tier
            ORDER BY CASE kd.threshold_tier WHEN 'tier_1_break' THEN 0 ELSE 1 END,
                     n DESC, name ASC
            """,  # nosec B608 -- only '?' placeholders are interpolated; tickers remain bound
            tuple(symbols),
        ).fetchall()
    except sqlite3.Error:
        return []
    finally:
        conn.close()
    out: list[tuple[str, str]] = []
    for name, tier, _n in rows:
        if name:
            out.append((f"kpi:{name}", str(tier)))
    return out


def load_llm_picks(db_path: Path, ticker: str) -> list[tuple[str, str]]:
    """``(token, why)`` from the cached LLM picks for ``ticker``, read straight
    from ``data/key_metrics/<T>.json`` (no heavy import on the render path).
    ``[]`` on any miss / malformed cache (degrade to the tier-graded baseline)."""
    path = db_path.parent / "key_metrics" / f"{ticker.upper()}.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(raw, dict):
        return []
    metrics = cast("dict[str, object]", raw).get("metrics")
    if not isinstance(metrics, list):
        return []
    out: list[tuple[str, str]] = []
    for entry in cast("list[object]", metrics):
        if not isinstance(entry, dict):
            continue
        rec = cast("dict[str, object]", entry)
        token = rec.get("token")
        if not isinstance(token, str) or not token:
            continue
        why = rec.get("why")
        out.append((token, why.strip() if isinstance(why, str) else ""))
    return out


def key_metric_bubbles(
    db_path: Path,
    tickers: list[str],
    catalog: dict[str, list[dict[str, object]]],
    *,
    max_bubbles: int = _MAX_BUBBLES,
) -> list[KeyMetricBubble]:
    """Merge the tier-graded baseline with the cached LLM picks into the bubble
    row: tier-graded first (deterministic, always shown), then LLM picks that
    aren't already present. Deduped by token, every token validated against the
    live catalog, capped at ``max_bubbles``."""
    symbols = [t.strip().upper() for t in tickers if t.strip()]
    valid_tokens, label_by_token = _catalog_index(catalog)
    bubbles: list[KeyMetricBubble] = []
    used: set[str] = set()

    def _label(token: str) -> str:
        if token in label_by_token:
            return label_by_token[token]
        # Fall back to the token's leaf (after the domain prefix).
        return token.split(":", 1)[-1] if ":" in token else token

    for token, tier in tier_graded_baseline(db_path, symbols):
        if token in used or token not in valid_tokens:
            continue
        used.add(token)
        bubbles.append(
            KeyMetricBubble(
                token=token,
                label=_label(token),
                source="tier",
                title=_TIER_TITLE.get(tier, "Graded thesis KPI"),
            )
        )
        if len(bubbles) >= max_bubbles:
            return bubbles

    for ticker in symbols:
        for token, why in load_llm_picks(db_path, ticker):
            if token in used or token not in valid_tokens:
                continue
            used.add(token)
            bubbles.append(
                KeyMetricBubble(
                    token=token,
                    label=_label(token),
                    source="llm",
                    title=why or "Key metric for this business",
                )
            )
            if len(bubbles) >= max_bubbles:
                return bubbles
    return bubbles


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------


def render_key_metrics_inner(bubbles: list[KeyMetricBubble], tickers: list[str]) -> str:
    """The inner HTML of the ``#vx-keymetrics`` container: a label + a chip per
    bubble. Empty string when there are no bubbles (the container collapses via
    ``:empty``). Returned bare so the ``?fragment=keymetrics`` route can swap it
    in on a ticker change."""
    if not bubbles:
        return ""
    symbols = [t.strip().upper() for t in tickers if t.strip()]
    scope = ", ".join(symbols[:4]) + ("…" if len(symbols) > 4 else "")
    label = f"Key metrics &middot; {escape(scope)}" if scope else "Key metrics"
    chips: list[str] = []
    for b in bubbles:
        tone = " k-chip-accent" if b.source == "llm" else ""
        chips.append(
            f'<button type="button" class="k-chip km-chip{tone}" '
            f'data-km-token="{escape(b.token)}" title="{escape(b.title)}">'
            f"{escape(b.label)}</button>"
        )
    return (
        f'<span class="vx-km-label">{label}</span><span class="vx-km-chips">{"".join(chips)}</span>'
    )


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
    # Optional metric discovery belongs to the on-demand Work Bench. The
    # conversation must remain usable while the governed catalog is expensive
    # or unavailable; generic questions do not assert any financial facts.
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
