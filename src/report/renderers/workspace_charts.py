"""Pure-SVG chart primitives for the workspace renderer.

Server-rendered, dependency-free, trivially testable (primitive lists in,
SVG/HTML strings out). Two primitives survive: ``sparkline`` (KPI strip,
ledger trends, valuation history, prompt-quality trends) and
``verdict_bar`` (the Say·Do rating history strip).

P6.1 retired the design-bundle slots that never got wired after the theme
migration: ``line_chart`` / ``stacked_bars`` (the YoY heatmap matrices in
charts_v2 won that job), ``sensitivity_grid`` + ``compute_gordon_sensitivity``
(DCF sensitivity lives in the Google Sheets workbooks instead), and the
hover-script layer they anticipated (never written).
"""

from __future__ import annotations

import html
from enum import StrEnum

CHARTS_V2_CSS = """
.cv2-chart { display: block; max-inline-size: 100%; height: auto; font-family: var(--sans); }
.cv2-title { font-size: var(--fs-title); font-weight: 600; fill: var(--fg); }
.cv2-axis { font-size: var(--fs-caption); fill: var(--muted); font-family: var(--mono); }
.cv2-grid { stroke: var(--border); stroke-width: var(--bw-thin); }
.cv2-grid-zero { stroke: var(--muted); stroke-width: var(--bw-thin); stroke-dasharray: 2 2; }
.cv2-bar-label { font-size: var(--fs-caption); fill: var(--fg); font-family: var(--mono); font-weight: 500; }
.cv2-line-end { font-size: var(--fs-caption); font-weight: 600; font-family: var(--mono); fill: currentColor; }
.cv2-legend { font-size: var(--fs-caption); fill: var(--fg); font-family: var(--sans); }
.cv2-legend-swatch { color: currentColor; }
.cv2-band-label { font-size: var(--fs-caption); font-family: var(--sans); font-weight: 500; fill: currentColor; }
.cv2-empty-text { font-size: var(--fs-body); fill: var(--muted); }
.cv2-pair { display: flex; gap: var(--sp-3); margin-block: var(--sp-2); margin-inline: initial; }
.cv2-pair > * { flex: 1; min-inline-size: 0; }
@media (max-width: 900px) { .cv2-pair { display: block; } }
.cv2-matrix-wrap { margin-block: var(--sp-3); margin-inline: initial; overflow-x: auto; }
.cv2-matrix-title { font-size: var(--fs-title); font-weight: 600; color: var(--fg); margin-bottom: var(--sp-1); }
.cv2-matrix { border-collapse: collapse; font-size: var(--fs-body); inline-size: stretch; max-inline-size: 100%; }
/* Canonical table rule: numbers mono, labels/headers sans. Every value cell
   is a <td> (cv2-matrix-cell / -cagr-cell); the row label + column headers are
   <th>, which inherit the page's sans. */
.cv2-matrix td { font-family: var(--mono); }
.cv2-matrix th, .cv2-matrix td { padding: var(--sp-1) var(--sp-2); border: var(--bw-thin) solid var(--border); text-align: right; white-space: nowrap; }
.cv2-matrix-label { text-align: left !important; font-weight: 600; color: var(--fg); background: var(--paper); position: sticky; inset-inline-start: 0; }
.cv2-matrix-q, .cv2-matrix-cagr { font-weight: 600; color: var(--fg); background: var(--paper); }
.cv2-matrix-cagr, .cv2-matrix-cagr-cell { border-left: var(--bw-thick) solid var(--fg) !important; }
.cv2-matrix-noisy { color: var(--muted) !important; font-style: italic; background: var(--paper) !important; }
.cv2-matrix-footnote { font-size: var(--fs-caption); color: var(--muted); margin-top: var(--sp-1); font-style: italic; }
.cv2-matrix-def-mark { cursor: help; opacity: 0.6; }
.cv2-matrix-def-mark:hover { opacity: 1; }
.chart-empty { font-size: var(--fs-body); color: var(--muted); padding: var(--sp-2) var(--sp-3); background: var(--paper); border-radius: var(--radius); margin-block: var(--sp-2); margin-inline: initial; }
.chart-grid-1col { display: grid; gap: var(--sp-4); margin-block: var(--sp-3); margin-inline: initial; }
.chart-cell { display: block; }
"""


class SparklineSize(StrEnum):
    """Closed chart recipes owned by the workspace visual master."""

    KPI = "kpi"
    COMPACT = "compact"
    MICRO = "micro"
    VALUATION = "valuation"


_SPARKLINE_GEOMETRY: dict[SparklineSize, tuple[int, int]] = {
    SparklineSize.KPI: (230, 36),
    SparklineSize.COMPACT: (120, 24),
    SparklineSize.MICRO: (84, 22),
    SparklineSize.VALUATION: (560, 60),
}


def _esc(text: str) -> str:
    return html.escape(text, quote=True)


def sparkline(
    values: list[float],
    *,
    size: SparklineSize = SparklineSize.KPI,
    dot: bool = True,
) -> str:
    """Tiny inline-SVG sparkline. Empty / single-point series degrade safely."""
    width, height = _SPARKLINE_GEOMETRY[size]
    svg_open = (
        f'<svg class="ws-spark ws-spark-{size.value}" viewBox="0 0 {width} {height}" '
        'aria-hidden="true">'
    )
    if not values:
        return f"{svg_open}</svg>"
    if len(values) == 1:
        cy = height / 2
        return (
            svg_open + f'<circle class="ws-spark-dot" cx="{width / 2:.1f}" cy="{cy:.1f}" r="2" />'
            f"</svg>"
        )
    min_y = min(values)
    max_y = max(values)
    span = max_y - min_y or 1.0
    points: list[tuple[float, float]] = []
    n = len(values)
    for i, v in enumerate(values):
        x = (i / (n - 1)) * (width - 2) + 1
        y = height - 2 - ((v - min_y) / span) * (height - 4)
        points.append((x, y))
    d = " ".join(("M" if i == 0 else "L") + f"{x:.1f},{y:.1f}" for i, (x, y) in enumerate(points))
    parts: list[str] = [svg_open]
    area = f"{d} L{points[-1][0]:.1f},{height} L{points[0][0]:.1f},{height} Z"
    parts.append(f'<path class="ws-spark-area" d="{area}" />')
    parts.append(f'<path class="ws-spark-line" d="{d}" />')
    if dot:
        lx, ly = points[-1]
        parts.append(f'<circle class="ws-spark-dot" cx="{lx:.1f}" cy="{ly:.1f}" r="2" />')
    parts.append("</svg>")
    return "".join(parts)


def verdict_bar(ratings: list[str]) -> str:
    """Inline history bar — one cell per quarter, color-coded by rating."""
    tones = {"EXCEEDED": "exceeded", "MET": "met", "MIXED": "mixed", "MISSED": "missed"}
    cells: list[str] = []
    for r in ratings:
        tone = tones.get(r, "unknown")
        cells.append(f'<div class="ws-verdict-cell ws-verdict-{tone}" title="{_esc(r)}"></div>')
    return f'<div class="ws-verdict-bar">{"".join(cells)}</div>'


__all__ = [
    "SparklineSize",
    "sparkline",
    "verdict_bar",
]
