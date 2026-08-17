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


def _esc(text: str) -> str:
    return html.escape(text, quote=True)


def sparkline(
    values: list[float],
    width: int = 230,
    height: int = 36,
    stroke: str = "currentColor",
    stroke_width: float = 1.25,
    fill: str | None = "currentColor",
    dot: bool = True,
) -> str:
    """Tiny inline-SVG sparkline. Empty / single-point series degrade safely."""
    if not values:
        return f'<svg width="{width}" height="{height}" aria-hidden="true"></svg>'
    if len(values) == 1:
        cy = height / 2
        return (
            f'<svg width="{width}" height="{height}" aria-hidden="true">'
            f'<circle cx="{width / 2:.1f}" cy="{cy:.1f}" r="2" fill="{stroke}" />'
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
    parts: list[str] = [
        f'<svg width="{width}" height="{height}" '
        f'style="overflow:visible;display:block" aria-hidden="true">'
    ]
    if fill:
        area = f"{d} L{points[-1][0]:.1f},{height} L{points[0][0]:.1f},{height} Z"
        parts.append(f'<path d="{area}" fill="{fill}" opacity="0.18" />')
    parts.append(
        f'<path d="{d}" fill="none" stroke="{stroke}" '
        f'stroke-width="{stroke_width}" stroke-linejoin="round" stroke-linecap="round" />'
    )
    if dot:
        lx, ly = points[-1]
        parts.append(f'<circle cx="{lx:.1f}" cy="{ly:.1f}" r="2" fill="{stroke}" />')
    parts.append("</svg>")
    return "".join(parts)


def verdict_bar(ratings: list[str]) -> str:
    """Inline history bar — one cell per quarter, color-coded by rating."""
    colors: dict[str, str] = {
        "EXCEEDED": "var(--accent)",
        "MET": "var(--fg)",
        "MIXED": "var(--warn)",
        "MISSED": "var(--muted)",
    }
    cells: list[str] = []
    for r in ratings:
        color = colors.get(r, "var(--border)")
        opacity = "1" if r == "EXCEEDED" else "0.55"
        cells.append(
            f'<div title="{_esc(r)}" style="width:12px;height:18px;'
            f'background:{color};opacity:{opacity};border-radius:var(--radius-sm)"></div>'
        )
    return f'<div style="display:flex;gap:2px;align-items:center">{"".join(cells)}</div>'


__all__ = [
    "sparkline",
    "verdict_bar",
]
