"""Financials tab: line-item/segment YoY matrices, KPI series, signals, validation.

Split out of ``workspace_html.py`` (S13 renderer modularization). The
public entry point and output contract live in ``workspace_html``;
names here keep their original (underscore) spellings and are exported
via ``__all__`` for the package-internal imports and the back-compat
re-exports in ``workspace_html``."""

from __future__ import annotations

from io import StringIO

from report.models import (
    FinancialsSection,
    SectionStatus,
    SegmentSecondaryExpansion,
    SegmentSeries,
    SegmentsSection,
    SignalRow,
    SignalsSection,
)
from report.renderers.charts_v2 import MatrixRow, yoy_heatmap_table
from report.renderers.workspace_data import quarter_short
from report.renderers.workspace_sections._shared import (
    _empty_panel,
    _esc,
    _missing_panel,
    _panel_head,
    _source_chip_html,
    _source_hover_title,
    _xlink_html,
)

__all__ = [
    "_SIGNALS_SEVERITIES",
    "_annual_kpi_series_yoy_panel",
    "_financials_tab",
    "_growth_cell",
    "_kpi_series_yoy_panel",
    "_line_items_levels_panel",
    "_line_items_yoy_panel",
    "_segment_drill_table",
    "_segment_secondary_expansions_panel",
    "_segments_real",
    "_segments_yoy_panel",
    "_segments_yoy_panel_for_metric",
    "_signal_card_workspace",
    "_signal_severity_class",
    "_signals_panel",
    "_signals_table_workspace",
    "_sum_segments_at",
    "_validation_panel",
]


def _financials_tab(
    body: StringIO,
    fin: FinancialsSection,
    seg: SegmentsSection,
    signals: SignalsSection | None = None,
) -> None:
    """Financials tab — YoY% heatmap (line items first, then per-segment) with
    a validation badge showing whether segment revenue sums tie to the
    consolidated revenue line (catches dropped segments and unit mismatches).
    The per-line-item 12-quarter level table is also rendered with a
    click-to-expand drill-down showing the underlying segment breakdown for
    revenue rows. The §3.5 signals panel renders above the validation row
    when the time-series writer has materialized any current signals."""
    body.write('<div class="tab-body">')
    body.write(
        f'<div class="eyebrow">Financials · {len(fin.quarter_labels)} quarters · {fin.currency} millions</div>'
    )

    if fin.status != SectionStatus.OK and not fin.line_items:
        _missing_panel(body, fin.status, fin.missing, title="Financials")
        body.write("</div>")
        return

    # 0) Time-series signals — surfaced first so red/yellow fires read before
    # the heatmaps. Silently skipped when the section is absent or empty.
    if signals is not None:
        _signals_panel(body, signals)

    # 1) Validation: segments tie to total revenue?
    _validation_panel(body, fin, seg)

    # 2) YoY heatmap — line items.
    _line_items_yoy_panel(body, fin)

    # 3) YoY heatmap — segments (filtered to non-pseudo, real reporting units).
    _segments_yoy_panel(body, seg)

    # 3b) Same heatmap shape for the other two segment buckets (geography
    # mix, segment operating income) — separate panels because the row sets
    # often differ from the revenue-by-product set.
    _segments_yoy_panel_for_metric(
        body,
        title="YoY% — revenue by geography",
        rows=seg.revenue_by_geography,
        quarter_labels_full=seg.quarter_labels_full,
        quarter_labels=seg.quarter_labels,
        segment_definitions=seg.segment_definitions,
    )
    _segments_yoy_panel_for_metric(
        body,
        title="YoY% — segment operating income",
        rows=seg.operating_income,
        quarter_labels_full=seg.quarter_labels_full,
        quarter_labels=seg.quarter_labels,
        segment_definitions=seg.segment_definitions,
    )
    _segments_yoy_panel_for_metric(
        body,
        title="YoY% — capex by segment",
        rows=seg.capex_by_segment,
        quarter_labels_full=seg.quarter_labels_full,
        quarter_labels=seg.quarter_labels,
        segment_definitions=seg.segment_definitions,
    )
    _segments_yoy_panel_for_metric(
        body,
        title="YoY% — headcount by segment",
        rows=seg.headcount_by_segment,
        quarter_labels_full=seg.quarter_labels_full,
        quarter_labels=seg.quarter_labels,
        segment_definitions=seg.segment_definitions,
    )

    # 3c) KPI time-series matrices — analyst-tracked KPIs that aren't on
    # the line-items axis (ARPAC, GMV growth, NIM, etc.).
    _kpi_series_yoy_panel(body, fin)

    # 3c-annual) Annual-cadence KPIs (bank capital ratios, other 20-F/10-K-only
    # metrics) on a fiscal-year axis — kept off the quarterly heatmap so they
    # render as a clean annual series instead of a mostly-empty quarterly one.
    _annual_kpi_series_yoy_panel(body, fin)

    # 3d) Junction-driven secondary-dim expansions (geography / channel /
    # customer cross-tabs of segment data). Only renders when the section
    # builder found junction rows; otherwise the panel is silently skipped.
    _segment_secondary_expansions_panel(body, seg)

    # 4) 12-quarter levels with segment drill-down.
    _line_items_levels_panel(body, fin, seg)

    body.write("</div>")


def _segments_yoy_panel_for_metric(
    body: StringIO,
    *,
    title: str,
    rows: list[SegmentSeries],
    quarter_labels_full: list[str],
    quarter_labels: list[str],
    segment_definitions: dict[str, str],
) -> None:
    """Generic YoY heatmap for any segment bucket (geography, OI, ...).

    Filters pseudo-rows (Google Inc / Total / Consolidated) and rows with
    no positive data, then runs charts_v2.yoy_heatmap_table with the same
    12-quarter window the revenue-by-product panel uses. Skipped when the
    bucket is empty for this ticker.
    """
    filtered: list[SegmentSeries] = []
    for s in rows:
        name_l = s.segment_name.lower()
        if name_l.startswith("google inc") or name_l == "total" or "consolidat" in name_l:
            continue
        if not any(v for v in s.values if v is not None and v > 0):
            continue
        filtered.append(s)
    if not filtered:
        return
    filtered.sort(key=lambda s: -(s.values[-1] or 0))
    periods = quarter_labels_full or quarter_labels
    matrix_rows: list[MatrixRow] = []
    for s in filtered:
        levels = s.levels_full or s.values
        if not levels or all(v is None for v in levels):
            continue
        defn = segment_definitions.get(s.segment_name) if segment_definitions else None
        matrix_rows.append(
            MatrixRow(name=s.segment_name, levels=list(levels), unit=s.unit, tooltip=defn or "")
        )
    if not matrix_rows:
        return
    body.write(_panel_head(title, sub=f"{len(matrix_rows)} rows · heat shading"))
    body.write('<div class="prose-pad">')
    body.write(yoy_heatmap_table(matrix_rows, list(periods), title="", display_quarters=12))
    body.write("</div></div>")


def _segment_secondary_expansions_panel(body: StringIO, seg: SegmentsSection) -> None:
    """Render junction-derived secondary-dim breakdowns as expandable panels.

    One panel per expansion (one per dim_type). Each panel shows a YoY heatmap
    over the same 12-quarter axis the rest of the tab uses. Silently skipped
    when the section builder didn't surface any expansions for this ticker.
    """
    # getattr (not direct access) tolerates specs built before the field
    # existed; the annotation pins the type back down for strict checking.
    expansions: list[SegmentSecondaryExpansion] = getattr(seg, "secondary_expansions", None) or []
    if not expansions:
        return
    periods = seg.quarter_labels_full or seg.quarter_labels
    if not periods:
        return
    for exp in expansions:
        if not exp.rows:
            continue
        axis_label = exp.dim_type.replace("_", " ").title()
        matrix_rows: list[MatrixRow] = []
        for s in exp.rows:
            levels = s.levels_full or s.values
            if not levels or all(v is None for v in levels):
                continue
            matrix_rows.append(
                MatrixRow(name=s.segment_name, levels=list(levels), unit=s.unit, tooltip="")
            )
        if not matrix_rows:
            continue
        parent = f" — under {exp.parent_label}" if exp.parent_label else ""
        body.write(
            _panel_head(
                f"By {axis_label}{parent} · cross-tab",
                sub=f"{len(matrix_rows)} rows · junction data",
            )
        )
        body.write('<div class="prose-pad">')
        body.write(yoy_heatmap_table(matrix_rows, list(periods), title="", display_quarters=12))
        body.write("</div></div>")


def _kpi_series_yoy_panel(body: StringIO, fin: FinancialsSection) -> None:
    """YoY heatmap for the analyst-tracked KPIs surfaced as kpi_chart_series.

    These are KPIs the financials section flagged as worth charting that
    aren't already in the line_items axis (e.g. NDR, GMV, ARPAC). Same
    matrix shape so the visual treatment is consistent.
    """
    series = fin.kpi_chart_series
    if not series:
        return
    periods = fin.quarter_labels_full or fin.quarter_labels
    matrix_rows: list[MatrixRow] = []
    for s in series:
        levels = s.levels_full or s.values
        if not levels or all(v is None for v in levels):
            continue
        # KPI chip universality (S2 PR2): same anatomy as the line-items
        # panel — per-cell hover (tier + fetched-at + conf %), clickable chip
        # on the row label for the latest sourced quarter. sources_full
        # aligns to levels_full, so chips only attach on that axis.
        cell_titles: list[str | None] | None = None
        label_suffix = ""
        if s.levels_full and s.sources_full:
            cell_titles = [
                _source_hover_title(src) if src is not None else None for src in s.sources_full
            ]
            latest_src = next((src for src in reversed(s.sources_full) if src is not None), None)
            if latest_src is not None:
                label_suffix = _source_chip_html(latest_src)
        matrix_rows.append(
            MatrixRow(
                name=s.name,
                levels=list(levels),
                unit=s.unit,
                cell_titles=cell_titles,
                label_suffix_html=label_suffix,
            )
        )
    if not matrix_rows:
        return
    body.write(_panel_head("Tracked KPIs", sub=f"{len(matrix_rows)} analyst-tracked series"))
    body.write('<div class="prose-pad">')
    body.write(yoy_heatmap_table(matrix_rows, list(periods), title="", display_quarters=12))
    body.write("</div></div>")


def _annual_kpi_series_yoy_panel(body: StringIO, fin: FinancialsSection) -> None:
    """YoY heatmap for ANNUAL-cadence KPIs on a fiscal-year axis.

    These are metrics the issuer discloses only annually (bank Basel III capital
    ratios, other 20-F/10-K-only figures). Rendering them on the quarterly axis
    produced a mostly-empty 12-quarter row; here they get their own panel with a
    fiscal-year axis and year-over-year shading (``period_stride=1``). Skipped
    when the ticker tracks no annual KPIs.
    """
    series = fin.annual_kpi_chart_series
    years = fin.annual_kpi_years
    if not series or not years:
        return
    matrix_rows: list[MatrixRow] = []
    for s in series:
        if not s.values or all(v is None for v in s.values):
            continue
        # Chips on the annual axis (S2 PR2): sources_full aligns to years,
        # which IS this panel's level axis — no window translation needed.
        cell_titles: list[str | None] | None = None
        label_suffix = ""
        if s.sources_full:
            cell_titles = [
                _source_hover_title(src) if src is not None else None for src in s.sources_full
            ]
            latest_src = next((src for src in reversed(s.sources_full) if src is not None), None)
            if latest_src is not None:
                label_suffix = _source_chip_html(latest_src)
        matrix_rows.append(
            MatrixRow(
                name=s.name,
                levels=list(s.values),
                unit=s.unit,
                cell_titles=cell_titles,
                label_suffix_html=label_suffix,
            )
        )
    if not matrix_rows:
        return
    periods = [str(y) for y in years]
    # Trailing CAGR/Δ columns in YEARS, trimmed to the available span so a short
    # annual history (NU CAR = 3 years) doesn't advertise an empty 3y column.
    cagr_years = tuple(y for y in (1, 2, 3) if y < len(years))
    body.write(
        _panel_head(
            "Tracked KPIs — annual",
            sub=f"{len(matrix_rows)} annual-cadence series · fiscal-year axis",
        )
    )
    body.write('<div class="prose-pad">')
    body.write(
        yoy_heatmap_table(
            matrix_rows,
            periods,
            title="",
            display_quarters=len(periods),
            cagr_periods=cagr_years,
            period_stride=1,
            periods_per_year=1,
        )
    )
    body.write("</div></div>")


def _segments_real(seg: SegmentsSection) -> list[SegmentSeries]:
    """Filter out the consolidated 'Google Inc' pseudo-rows that FMP sometimes
    emits alongside the real reporting units. Keep only series with at least
    one positive value, sorted by latest-period revenue descending."""
    filtered: list[SegmentSeries] = []
    for s in seg.revenue_by_product:
        name_l = s.segment_name.lower()
        if name_l.startswith("google inc") or name_l == "total" or "consolidat" in name_l:
            continue
        if not any(v for v in s.values if v is not None and v > 0):
            continue
        filtered.append(s)
    # Latest-period revenue, treating None as 0 for sort stability.
    filtered.sort(key=lambda s: -(s.values[-1] or 0))
    return filtered


def _validation_panel(body: StringIO, fin: FinancialsSection, seg: SegmentsSection) -> None:
    """Compare consolidated revenue vs sum-of-real-segments per quarter.
    Emit a single status row: OK / off-by-X — the user wanted a visible tie-out."""
    revenue = next((li for li in fin.line_items if li.line_item.lower() == "revenue"), None)
    if revenue is None or not seg.revenue_by_product:
        return
    real = _segments_real(seg)
    if not real:
        return
    # Align segment series to financials quarter axis. Common case: both share
    # the same labels in the same order. Bail out cleanly when they don't.
    if seg.quarter_labels != fin.quarter_labels:
        _empty_panel(
            body,
            "Validation: segments ↔ revenue",
            "Segment quarter labels don't match the financials quarter labels — "
            "the segment series may be on a different fiscal calendar, so the "
            "tie-out can't be computed.",
            reason="cannot tie · labels misaligned",
        )
        return
    diffs: list[tuple[str, float | None]] = []
    for i, lbl in enumerate(fin.quarter_labels):
        total = revenue.values[i]
        if total is None:
            diffs.append((lbl, None))
            continue
        seg_sum = _sum_segments_at(real, i)
        if seg_sum == 0:
            diffs.append((lbl, None))
            continue
        diffs.append((lbl, (seg_sum - total) / total * 100))
    # Worst absolute deviation across quarters where we have a tie point.
    worst = max(
        (abs(d) for _, d in diffs if d is not None),
        default=None,
    )
    if worst is None:
        return
    if worst <= 0.5:
        status_cls = "k-chip-ok"
        status_text = f"ties to FMP (max drift {worst:.2f}%)"
    elif worst <= 2.0:
        status_cls = "k-chip-warn"
        status_text = f"minor drift (max {worst:.2f}%)"
    else:
        status_cls = "k-chip-bad"
        status_text = f"DRIFT — segments off by up to {worst:.1f}%"
    body.write(
        _panel_head(
            "Validation: segments ↔ consolidated revenue",
            sub_html=f'<span class="k-chip k-chip-mono {status_cls}">{_esc(status_text)}</span>',
        )
    )
    # Compact per-quarter detail table — only show drift > 0.1% to keep it scannable.
    body.write('<div class="table-scroll"><table class="tbl tbl-nowrap"><thead><tr>')
    body.write('<th>Quarter</th><th class="num">Revenue</th><th class="num">Σ segments</th>')
    body.write('<th class="num">Drift</th></tr></thead><tbody>')
    for i, lbl in enumerate(fin.quarter_labels):
        d = diffs[i][1]
        if d is None or abs(d) < 0.1:
            continue
        total = revenue.values[i]
        seg_sum = _sum_segments_at(real, i)
        cls = "num pos" if abs(d) < 1 else ("num neg" if d < 0 else "num warn")
        body.write(f"<tr><td>{_esc(quarter_short(lbl))}</td>")
        body.write(f'<td class="num">{(total or 0) / 1000:.2f}B</td>')
        body.write(f'<td class="num">{seg_sum / 1000:.2f}B</td>')
        body.write(f'<td class="{cls}">{d:+.2f}%</td></tr>')
    body.write("</tbody></table></div></div>")


def _signals_panel(body: StringIO, signals: SignalsSection) -> None:
    """§3.5 Signals — tier-bucketed cards + collapsible 'All signals' table.

    Silently omitted when no signal of any tier surfaced for this ticker —
    a §3.5 with nothing to say is worse than no §3.5 at all.
    """
    if not (signals.red_signals or signals.yellow_signals or signals.green_signals):
        return
    total = len(signals.red_signals) + len(signals.yellow_signals) + len(signals.green_signals)
    fires = list(signals.red_signals) + list(signals.yellow_signals)
    body.write(
        _panel_head(
            "§3.5 Signals",
            sub=f"{len(signals.red_signals)} red · "
            f"{len(signals.yellow_signals)} yellow · {len(signals.green_signals)} green",
            links=_xlink_html("news", "related news →"),
            panel_id="panel-signals",
        )
    )
    if fires:
        body.write('<div class="signals-fires">')
        for r in fires:
            _signal_card_workspace(body, r)
        body.write("</div>")
    body.write(f'<details class="signals-all"><summary>All signals ({total})</summary>')
    all_rows = (
        list(signals.red_signals) + list(signals.yellow_signals) + list(signals.green_signals)
    )
    _signals_table_workspace(body, all_rows)
    body.write("</details>")
    body.write("</div>")


# Severity is expressed through token-backed classes (P6.1 design-debt
# sweep — these cards previously hardcoded rgba colors + a font stack
# inline, bypassing the P0.1 palette).
_SIGNALS_SEVERITIES = frozenset({"red", "yellow", "green"})


def _signal_severity_class(severity: str) -> str:
    return f"sig-{severity}" if severity in _SIGNALS_SEVERITIES else "sig-unknown"


def _signal_card_workspace(body: StringIO, r: SignalRow) -> None:
    type_label = r.signal_type.replace("_", " ")
    body.write(f'<div class="signal-card {_signal_severity_class(r.severity)}">')
    body.write('<div class="signal-card-head">')
    body.write(
        f'<span class="signal-card-metric">{_esc(r.metric_name)}</span>'
        f'<span class="signal-card-type">{_esc(type_label)}</span>'
    )
    body.write("</div>")
    if r.narrative:
        body.write(f'<div class="signal-card-narrative">{_esc(r.narrative)}</div>')
    if r.value_summary:
        body.write(f'<div class="signal-card-stat">{_esc(r.value_summary)}</div>')
    body.write("</div>")


def _signals_table_workspace(body: StringIO, rows: list[SignalRow]) -> None:
    body.write('<div class="prose-pad"><div class="table-scroll">')
    body.write('<table class="tbl"><thead><tr>')
    body.write(
        "<th>Sev</th><th>Metric</th><th>Kind</th><th>Signal</th><th>Narrative</th><th>Stat</th>"
    )
    body.write("</tr></thead><tbody>")
    for r in rows:
        narrative = _esc(r.narrative) if r.narrative else "—"
        stat = _esc(r.value_summary) if r.value_summary else "—"
        body.write(
            f'<tr><td class="signal-sev {_signal_severity_class(r.severity)}">'
            f"{_esc(r.severity)}</td>"
            f"<td><strong>{_esc(r.metric_name)}</strong></td>"
            f"<td>{_esc(r.metric_kind)}</td>"
            f"<td>{_esc(r.signal_type.replace('_', ' '))}</td>"
            f"<td>{narrative}</td>"
            f'<td class="mono">{stat}</td></tr>'
        )
    body.write("</tbody></table></div></div>")


def _line_items_yoy_panel(body: StringIO, fin: FinancialsSection) -> None:
    """YoY% matrix for the headline line items, using charts_v2.yoy_heatmap_table."""
    if not fin.line_items:
        return
    # Prefer levels_full when available (16-q with YoY lookback baseline);
    # otherwise fall back to the 12-q values list.
    periods = fin.quarter_labels_full or fin.quarter_labels
    rows: list[MatrixRow] = []
    for li in fin.line_items:
        levels = li.levels_full or li.values
        if not levels or all(v is None for v in levels):
            continue
        # P3.3 source chips: hover per cell (tier + fetched-at of the
        # current-quarter fact), click via the row-label chip (latest
        # sourced quarter's document identity + open-source link).
        cell_titles: list[str | None] | None = None
        label_suffix = ""
        if li.sources_full:
            cell_titles = [
                _source_hover_title(s) if s is not None else None for s in li.sources_full
            ]
            latest_src = next((s for s in reversed(li.sources_full) if s is not None), None)
            if latest_src is not None:
                label_suffix = _source_chip_html(latest_src)
        rows.append(
            MatrixRow(
                name=li.line_item,
                levels=list(levels),
                unit=li.unit,
                cell_titles=cell_titles,
                label_suffix_html=label_suffix,
            )
        )
    if not rows:
        return
    body.write(_panel_head("YoY% — line items", sub="12 quarters · heat shading"))
    body.write('<div class="prose-pad">')
    body.write(yoy_heatmap_table(rows, list(periods), title="", display_quarters=12))
    body.write("</div></div>")


def _segments_yoy_panel(body: StringIO, seg: SegmentsSection) -> None:
    real = _segments_real(seg)
    if not real:
        return
    periods = seg.quarter_labels_full or seg.quarter_labels
    rows: list[MatrixRow] = []
    for s in real:
        levels = s.levels_full or s.values
        if not levels or all(v is None for v in levels):
            continue
        defn = seg.segment_definitions.get(s.segment_name) if seg.segment_definitions else None
        rows.append(
            MatrixRow(
                name=s.segment_name,
                levels=list(levels),
                unit=s.unit,
                tooltip=defn or "",
            )
        )
    if not rows:
        return
    sub_bits = [f"{len(rows)} segments · heat shading"]
    if seg.segment_definitions_fiscal_year:
        sub_bits.append(f"definitions from FY{seg.segment_definitions_fiscal_year} (hover 📖)")
    body.write(_panel_head("YoY% — revenue by segment", sub=" · ".join(sub_bits)))
    body.write('<div class="prose-pad">')
    body.write(yoy_heatmap_table(rows, list(periods), title="", display_quarters=12))
    body.write("</div></div>")


def _line_items_levels_panel(body: StringIO, fin: FinancialsSection, seg: SegmentsSection) -> None:
    """12-quarter levels table. Revenue row carries a click-to-expand
    drill-down showing the per-segment breakdown for the same quarters."""
    body.write(
        _panel_head(
            "Line items · last 12 quarters",
            sub=f"{fin.currency} millions · QoQ · YoY · 3-yr CAGR · click ▶ to drill",
        )
        + '<div class="table-scroll"><table class="tbl tbl-nowrap"><thead><tr>'
        "<th>Line item</th>"
    )
    last_labels = fin.quarter_labels[-12:]
    for lbl in last_labels:
        body.write(f'<th class="num">{_esc(quarter_short(lbl))}</th>')
    body.write('<th class="num">QoQ</th><th class="num">YoY</th><th class="num">3y CAGR</th>')
    body.write("</tr></thead><tbody>")
    real_segments = _segments_real(seg)
    for li in fin.line_items:
        drillable = (
            li.line_item.lower() == "revenue"
            and bool(real_segments)
            and seg.quarter_labels == fin.quarter_labels
        )
        row_id = f"fin-row-{_esc(li.line_item.lower().replace(' ', '-'))}"
        chev = chr(0x25B6) if drillable else ""
        cls = "fin-row drillable" if drillable else "fin-row"
        body.write(
            f'<tr class="{cls}" data-drill-target="{row_id}">'
            f'<td><span class="fin-chev">{chev}</span> {_esc(li.line_item)}</td>'
        )
        for v in li.values[-12:]:
            if v is None:
                body.write('<td class="num muted">—</td>')
            elif li.unit == "USD":
                body.write(f'<td class="num">{v:.2f}</td>')
            elif v < 0:
                body.write(f'<td class="num neg">({abs(v) / 1000:.1f})</td>')
            else:
                body.write(f'<td class="num">{v / 1000:.1f}</td>')
        g = li.growth
        body.write(_growth_cell(g.qoq))
        body.write(_growth_cell(g.yoy))
        body.write(_growth_cell(g.cagr_3y_ttm, muted=True))
        body.write("</tr>")
        if drillable:
            n_cols = 1 + len(last_labels) + 3
            body.write(
                f'<tr class="fin-drill" id="{row_id}" style="display:none">'
                f'<td colspan="{n_cols}" class="fin-drill-cell">'
            )
            _segment_drill_table(body, real_segments, fin.quarter_labels)
            body.write("</td></tr>")
    body.write("</tbody></table></div></div>")


def _segment_drill_table(
    body: StringIO, real_segments: list[SegmentSeries], quarter_labels: list[str]
) -> None:
    last_labels = quarter_labels[-12:]
    body.write('<table class="tbl tbl-nowrap fin-drill-table"><thead><tr>')
    body.write("<th>Segment</th>")
    for lbl in last_labels:
        body.write(f'<th class="num">{_esc(quarter_short(lbl))}</th>')
    body.write("</tr></thead><tbody>")
    for s in real_segments:
        body.write(f"<tr><td>{_esc(s.segment_name)}</td>")
        for v in s.values[-12:]:
            if v is None:
                body.write('<td class="num muted">—</td>')
            else:
                body.write(f'<td class="num">{v / 1000:.1f}</td>')
        body.write("</tr>")
    # Sum row.
    body.write('<tr class="fin-sum-row"><td><strong>Σ segments</strong></td>')
    for i in range(len(quarter_labels) - 12, len(quarter_labels)):
        if i < 0:
            body.write('<td class="num muted">—</td>')
            continue
        total = _sum_segments_at(real_segments, i)
        body.write(f'<td class="num"><strong>{total / 1000:.1f}</strong></td>')
    body.write("</tr></tbody></table>")


def _sum_segments_at(segments: list[SegmentSeries], idx: int) -> float:
    """Sum the per-segment value at the given quarter index, treating None
    as 0. Centralized so pyright sees a real ``float`` return type instead
    of choking on ``sum(generator[float|None])``."""
    out = 0.0
    for s in segments:
        if idx < 0 or idx >= len(s.values):
            continue
        v = s.values[idx]
        if v is not None:
            out += v
    return out


def _growth_cell(v: float | None, *, muted: bool = False) -> str:
    if v is None:
        return '<td class="num muted">—</td>'
    cls = "num muted" if muted else f"num {'pos' if v > 0 else 'neg'}"
    return f'<td class="{cls}">{v * 100:+.1f}%</td>'
