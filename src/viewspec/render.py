"""Render a ViewResult as an HTML fragment (master build P5.1).

One matrix table (rows = ticker x metric, columns = the period axis) with
per-number provenance chips on every fin/kpi cell, plus an optional
multi-line chart of the same transformed series via charts_v2. The
fragment carries its own ``<style>`` block so it renders identically
wherever it lands — the Explore panel's result pane, the saved-view embed
endpoint (``/api/views/<id>/fragment``), or any future cockpit/report
inclusion.

Heat shading follows the charts_v2 convention (green positive / red
negative, saturation by magnitude) but only for signed-growth transforms
(yoy / cagr) — levels and margins are read as values, not directions.
"""

from __future__ import annotations

from html import escape

from report.renderers.charts_v2 import LineSeries, fmt_compact, fmt_pct, multi_line_chart
from ui.source_chip import SOURCE_CHIP_CSS, source_chip_html
from viewspec.engine import ViewResult, ViewRow

_MAX_CHART_SERIES = 8

# Fragment-scoped styles: the vx-* matrix plus the shared chip anatomy and
# dark-shell overrides for the charts_v2 SVG text (its stylesheet hardcodes
# light-theme fills; the dashboard shell is dark).
VIEWSPEC_CSS = (
    """
.vx-result { margin-top: 6px; }
.vx-meta { color: var(--muted); font-size: 11.5px; margin: 4px 0 8px; }
.vx-warn { color: var(--warn); font-size: 11.5px; margin: 2px 0; }
.vx-wrap { overflow-x: auto; }
.vx-matrix { border-collapse: collapse; font-size: 11px;
  font-family: var(--mono, monospace); }
.vx-matrix th, .vx-matrix td { padding: 4px 7px; border: 1px solid var(--border);
  text-align: right; white-space: nowrap; }
.vx-matrix thead th { color: var(--fg); background: var(--paper, var(--surface)); }
.vx-matrix .vx-label { text-align: left; font-weight: 600; color: var(--fg);
  background: var(--paper, var(--surface)); position: sticky; left: 0; }
.vx-matrix td { color: var(--fg-soft, var(--fg)); }
.vx-unit { color: var(--muted); font-weight: 400; }
.vx-chart { margin-top: 12px; }
.vx-chart .cv2-title { fill: var(--fg); }
.vx-chart .cv2-axis { fill: var(--muted); }
.vx-chart .cv2-grid { stroke: var(--border); }
.vx-chart .cv2-grid-zero { stroke: var(--muted); }
.vx-chart .cv2-legend { fill: var(--fg-soft, var(--fg)); }
.vx-chart .cv2-bar-label { fill: var(--fg); }
.vx-chart .cv2-empty-text { fill: var(--muted); }
"""
    + SOURCE_CHIP_CSS
)


def _heat_style(pct: float | None) -> str:
    """charts_v2's YoY heat shading, inline: alpha scales with |pct| capped
    at 30 — readable on light and dark surfaces alike."""
    if pct is None:
        return ""
    mag = min(abs(pct), 30) / 30
    alpha = 0.08 + 0.28 * mag
    if pct >= 0:
        return f"background:rgba(2,158,115,{alpha:.2f})"
    return f"background:rgba(203,77,77,{alpha:.2f})"


def _is_pct_unit(unit: str | None) -> bool:
    u = (unit or "").strip().lower()
    return u in ("%", "percent", "pp") or u.endswith("%")


def _fmt_cell(value: float | None, transform: str, unit: str | None) -> str:
    if value is None:
        return "—"
    if transform in ("yoy", "cagr", "margin"):
        return fmt_pct(value)
    # level: honor percent-ish underlying units; bps/ratio get their own
    # precision; everything else renders compact.
    u = (unit or "").strip().lower()
    if _is_pct_unit(unit):
        return f"{value:.1f}%"
    if u == "bps":
        return f"{value:.0f}bps"
    if u == "ratio":
        return f"{value:.2f}"
    return fmt_compact(value)


def _transform_caption(transform: str, cagr_years: int) -> str:
    if transform == "yoy":
        return "YoY % vs the same calendar bucket a year earlier"
    if transform == "cagr":
        return f"trailing {cagr_years}y CAGR ending at each bucket"
    if transform == "margin":
        return "value / revenue (same ticker &amp; bucket), %"
    return "levels"


def _row_html(row: ViewRow, transform: str) -> str:
    unit_sub = f' <span class="vx-unit">({escape(row.unit)})</span>' if row.unit else ""
    cells: list[str] = [f'<th class="vx-label">{escape(row.label)}{unit_sub}</th>']
    shade = transform in ("yoy", "cagr")
    for cell in row.cells:
        style = f' style="{_heat_style(cell.value)}"' if shade else ""
        chip = f" {source_chip_html(cell.source)}" if cell.source is not None else ""
        cells.append(f"<td{style}>{_fmt_cell(cell.value, transform, row.unit)}{chip}</td>")
    return f"<tr>{''.join(cells)}</tr>"


def render_view_fragment(result: ViewResult, *, include_chart: bool = True) -> str:
    """The full self-styled fragment: meta line, warnings, matrix, chart."""
    spec = result.spec
    parts: list[str] = [f"<style>{VIEWSPEC_CSS}</style>", '<div class="vx-result">']
    meta = (
        f"{len(result.rows)} series · {len(result.period_labels)} {spec.cadence} buckets · "
        f"{escape(spec.transform)} — {_transform_caption(spec.transform, spec.cagr_years)}"
    )
    parts.append(f'<div class="vx-meta">{meta}</div>')
    for w in result.warnings:
        parts.append(f'<div class="vx-warn">⚠ {escape(w)}</div>')
    if not result.rows:
        parts.append(
            '<div class="vx-meta">Nothing to plot — no data for any requested '
            "(ticker, metric) pair.</div>"
        )
        parts.append("</div>")
        return "".join(parts)

    head = ['<tr><th class="vx-label">Series</th>']
    head.extend(f"<th>{escape(p)}</th>" for p in result.period_labels)
    head.append("</tr>")
    body = [_row_html(row, spec.transform) for row in result.rows]
    parts.append(
        '<div class="vx-wrap"><table class="vx-matrix">'
        f"<thead>{''.join(head)}</thead><tbody>{''.join(body)}</tbody></table></div>"
    )

    if include_chart:
        parts.append(f'<div class="vx-chart">{_chart_html(result)}</div>')
    parts.append("</div>")
    return "".join(parts)


def _chart_html(result: ViewResult) -> str:
    spec = result.spec
    rows = result.rows[:_MAX_CHART_SERIES]
    series = [LineSeries(name=row.label, values=[c.value for c in row.cells]) for row in rows]
    value_fmt = "pct" if spec.transform in ("yoy", "cagr", "margin") else "compact"
    svg = multi_line_chart(
        series,
        result.period_labels,
        title=f"{spec.transform} · {spec.cadence}",
        width=720,
        height=260,
        value_fmt=value_fmt,
    )
    note = ""
    if len(result.rows) > _MAX_CHART_SERIES:
        note = (
            f'<div class="vx-meta">chart shows the first {_MAX_CHART_SERIES} of '
            f"{len(result.rows)} series</div>"
        )
    return svg + note
