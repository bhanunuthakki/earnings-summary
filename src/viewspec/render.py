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

Growth values beyond the per-transform n/m limits (lumpy bases, sign
flips — BN-style ±1700% YoY cells) render as a muted "n/m" with the real
value in the cell tooltip, and stay out of the heat shading and the
chart's y-scale so the remaining series keep a readable range.
"""

from __future__ import annotations

from html import escape

from report.renderers.charts_v2 import LineSeries, fmt_compact, fmt_pct, multi_line_chart
from ui.source_chip import SOURCE_CHIP_CSS, source_chip_html
from viewspec.engine import ViewResult, ViewRow

_MAX_CHART_SERIES = 8

# |value| display limits (percent points) past which a growth cell is "not
# meaningful": a tiny or sign-flipping base produces faithful-but-absurd
# growth numbers that read as garbage and blow out the shared color/y scales.
_NM_LIMITS: dict[str, float] = {"yoy": 500.0, "cagr": 200.0}

# Fragment-scoped styles: the vx-* matrix plus the shared chip anatomy and
# dark-shell overrides for the charts_v2 SVG text (its stylesheet hardcodes
# light-theme fills; the dashboard shell is dark).
VIEWSPEC_CSS = (
    """
.vx-result { margin-top: var(--sp-1); }
.vx-meta { color: var(--muted); font-size: var(--fs-caption); margin: 4px 0 8px; }
.vx-warn { color: var(--warn); font-size: var(--fs-caption); margin: 2px 0; }
.vx-wrap { overflow-x: auto; }
.vx-matrix { border-collapse: collapse; font-size: var(--fs-caption); }
.vx-matrix th, .vx-matrix td { padding: 4px 7px; border: 1px solid var(--border);
  text-align: right; white-space: nowrap; }
.vx-matrix thead th { color: var(--fg); background: var(--paper, var(--surface)); }
.vx-matrix .vx-label { text-align: left; font-weight: 600; color: var(--fg);
  background: var(--paper, var(--surface)); position: sticky; left: 0; }
/* Canonical table rule: numbers mono, labels/headers sans. Data cells are <td>;
   the row label (.vx-label) and column headers are <th>, inheriting sans. */
.vx-matrix td { color: var(--fg-soft, var(--fg)); font-family: var(--mono); }
.vx-matrix td.vx-nm { color: var(--muted); cursor: help; }
.vx-unit { color: var(--muted); font-weight: 400; }
.vx-chart { margin-top: var(--sp-3); }
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
    """charts_v2's YoY heat shading, inline: opacity scales with |pct| capped
    at 30 — readable on light and dark surfaces alike. Tinted through the
    status tokens (positive=--ok, negative=--bad) via color-mix, so the shading
    tracks the palette instead of a hardcoded green/red rgba."""
    if pct is None:
        return ""
    mag = min(abs(pct), 30) / 30
    pen = 8 + 28 * mag  # 8%..36% token mix, mirrors the old 0.08..0.36 alpha
    token = "var(--ok)" if pct >= 0 else "var(--bad)"
    return f"background:color-mix(in srgb, {token} {pen:.0f}%, transparent)"


def _is_nm(value: float | None, transform: str) -> bool:
    """True when a growth value is past the transform's n/m display limit."""
    limit = _NM_LIMITS.get(transform)
    return limit is not None and value is not None and abs(value) > limit


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


def _cadence_noun(cadence: str, n: int) -> str:
    """Human bucket noun for the summary line: quarterly→quarter(s),
    annual→year(s), anything else→period(s)."""
    base = {"quarterly": "quarter", "annual": "year"}.get(cadence, "period")
    return f"{n} {base}{'' if n == 1 else 's'}"


def view_summary(result: ViewResult) -> str:
    """One reconciled status line for a view — the single source of truth shared
    by the fragment's caption band and the Ask card's actions row.

    States the series count, transform, cadence, and how many buckets actually
    came back. When the request outran the data (``spec.periods`` > buckets
    returned) it appends a ``⚠ N missing`` gap clause so the shortfall reads as
    signal rather than a silent mismatch; n/m suppressions are tallied last.
    """
    spec = result.spec
    n_returned = len(result.period_labels)
    parts = [
        f"{len(result.rows)} series",
        escape(spec.transform),
        escape(spec.cadence),
        _cadence_noun(spec.cadence, n_returned),
    ]
    missing = spec.periods - n_returned
    if missing > 0:
        parts.append(f"⚠ {missing} missing")
    nm_count = sum(1 for row in result.rows for c in row.cells if _is_nm(c.value, spec.transform))
    if nm_count:
        parts.append(f"{nm_count} value{'' if nm_count == 1 else 's'} n/m")
    return " · ".join(parts)


def _row_html(row: ViewRow, transform: str, definition: str | None = None) -> str:
    unit_sub = f' <span class="vx-unit">({escape(row.unit)})</span>' if row.unit else ""
    # Definition tooltip (Ask v4): hovering the series label explains the
    # metric (kpi_definitions notes / the fin glossary).
    title_attr = f' title="{escape(definition)}"' if definition else ""
    cells: list[str] = [f'<th class="vx-label"{title_attr}>{escape(row.label)}{unit_sub}</th>']
    shade = transform in ("yoy", "cagr")
    limit = _NM_LIMITS.get(transform)
    for cell in row.cells:
        chip = f" {source_chip_html(cell.source)}" if cell.source is not None else ""
        if limit is not None and cell.value is not None and abs(cell.value) > limit:
            tip = f"{fmt_pct(cell.value)} — beyond ±{limit:.0f}%, not meaningful"
            cells.append(f'<td class="vx-nm" title="{tip}">n/m{chip}</td>')
            continue
        style = f' style="{_heat_style(cell.value)}"' if shade else ""
        cells.append(f"<td{style}>{_fmt_cell(cell.value, transform, row.unit)}{chip}</td>")
    return f"<tr>{''.join(cells)}</tr>"


def render_view_fragment(
    result: ViewResult, *, include_chart: bool = True, include_summary: bool = True
) -> str:
    """The full self-styled fragment: optional summary line, warnings, matrix, chart.

    ``include_summary=False`` drops the ``.vx-meta`` status band — the Ask card
    relocates that line onto its actions row, so the embedded fragment must not
    print it again. The default keeps standalone embeds (the DIY-builder result,
    ``GET /api/views/<id>/fragment``) self-describing.
    """
    spec = result.spec
    parts: list[str] = [f"<style>{VIEWSPEC_CSS}</style>", '<div class="vx-result">']
    if include_summary and result.rows:
        parts.append(f'<div class="vx-meta">{view_summary(result)}</div>')
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
    body = [
        _row_html(row, spec.transform, result.definitions.get(row.metric.token()))
        for row in result.rows
    ]
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
    # n/m values are dropped (not clamped): the chart's y-scale and polylines
    # both skip None points, so the sane series keep a readable range.
    series = [
        LineSeries(
            name=row.label,
            values=[None if _is_nm(c.value, spec.transform) else c.value for c in row.cells],
        )
        for row in rows
    ]
    value_fmt = "pct" if spec.transform in ("yoy", "cagr", "margin") else "compact"
    # No title: the summary line (caption band or the Ask card's actions row)
    # already states transform · cadence, and the legend names the series — a
    # chart title here was the third restatement of the same words.
    svg = multi_line_chart(
        series,
        result.period_labels,
        title="",
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
