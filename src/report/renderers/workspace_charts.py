"""Pure-SVG chart primitives for the workspace renderer.

Server-rendered (no JS framework — vanilla JS in ``workspace_script.py``
adds hover interactivity by reading ``data-*`` attributes off the markup).
All chart helpers take primitive lists / floats so they're trivially testable.

Adapted from the design bundle's ``charts.jsx``. Hover/tooltip behavior is
delegated to the script layer; this module's job is only the static SVG.
"""

from __future__ import annotations

import html
import json
from typing import Literal

# Editorial right-single-quote (U+2019) for short year prefixes like Q1 'YY.
# Hoisted to a module constant so f-strings can use it on Python 3.11 (which
# doesn't yet allow backslash escapes inside f-string substitutions).
_RSQUO = chr(0x2019)


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


def line_chart(
    values: list[float],
    labels: list[str],
    width: int = 500,
    height: int = 220,
    stroke: str = "var(--accent)",
    y_format: Literal["pct", "int", "float"] = "int",
) -> str:
    """Hoverable line chart. Hover crosshair + tooltip wired by the script layer.

    The chart embeds a JSON payload of ``[value, label]`` pairs in a
    ``data-points`` attribute. The script then handles ``mousemove`` to find
    the nearest x, reveal the marker, and float the tooltip.
    """
    if not values:
        return f'<svg width="{width}" height="{height}" aria-hidden="true"></svg>'

    pad_t, pad_r, pad_b, pad_l = 12, 24, 24, 36
    plot_w = width - pad_l - pad_r
    plot_h = height - pad_t - pad_b
    min_y = min(values)
    max_y = max(values)
    span = (max_y - min_y) or 1.0
    pad = span * 0.15
    y0 = min_y - pad
    y1 = max_y + pad
    n = len(values)

    def x_of(i: int) -> float:
        return pad_l + (i / max(n - 1, 1)) * plot_w

    def y_of(v: float) -> float:
        return pad_t + plot_h - ((v - y0) / (y1 - y0)) * plot_h

    pts = [(x_of(i), y_of(v)) for i, v in enumerate(values)]
    d = " ".join(("M" if i == 0 else "L") + f"{x:.1f},{y:.1f}" for i, (x, y) in enumerate(pts))
    area_d = f"{d} L{pts[-1][0]:.1f},{pad_t + plot_h} L{pts[0][0]:.1f},{pad_t + plot_h} Z"

    step = (y1 - y0) / 3
    ticks = [y0 + i * step for i in range(4)]

    def fmt(v: float) -> str:
        if y_format == "pct":
            return f"{v:.0f}%"
        if y_format == "float":
            return f"{v:.1f}"
        return f"{v:.0f}"

    parts: list[str] = [
        f'<svg class="ws-line" width="{width}" height="{height}" '
        f'style="display:block;cursor:crosshair" '
        f"data-points='{_esc(json.dumps([[v, lbl] for v, lbl in zip(values, labels, strict=False)]))}' "
        f'data-yfmt="{y_format}">'
    ]
    for t in ticks:
        ty = y_of(t)
        parts.append(
            f'<line x1="{pad_l}" x2="{pad_l + plot_w:.1f}" y1="{ty:.1f}" y2="{ty:.1f}" '
            f'stroke="var(--border)" stroke-width="0.5" stroke-dasharray="2 3" />'
        )
        parts.append(
            f'<text x="{pad_l - 6}" y="{ty + 3:.1f}" text-anchor="end" '
            f'font-size="9.5" fill="var(--muted)" font-family="var(--mono)">{_esc(fmt(t))}</text>'
        )
    sparse_idx = {0, n - 1}
    if n > 4:
        sparse_idx.add(n // 2)
    for i, lbl in enumerate(labels):
        if i in sparse_idx:
            parts.append(
                f'<text x="{x_of(i):.1f}" y="{height - 6}" text-anchor="middle" '
                f'font-size="9.5" fill="var(--muted)" font-family="var(--mono)">{_esc(lbl)}</text>'
            )
    parts.append(f'<path d="{area_d}" fill="{stroke}" opacity="0.08" />')
    parts.append(
        f'<path d="{d}" fill="none" stroke="{stroke}" stroke-width="1.5" stroke-linejoin="round" />'
    )
    for x, y in pts:
        parts.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="1.5" fill="{stroke}" />')
    # Crosshair + tooltip nodes hidden by default; script reveals on hover.
    parts.append(
        '<g class="ws-line-cursor" style="display:none">'
        f'<line x1="0" x2="0" y1="{pad_t}" y2="{pad_t + plot_h:.1f}" '
        f'stroke="{stroke}" stroke-width="0.5" stroke-dasharray="2 2" opacity="0.5" />'
        f'<circle r="3.5" fill="{stroke}" />'
        '<g class="ws-line-tip" transform="translate(0,-18)">'
        '<rect x="-30" y="-18" width="60" height="18" fill="var(--fg)" rx="2" />'
        '<text x="0" y="-5" text-anchor="middle" font-size="10" '
        'fill="var(--bg)" font-family="var(--mono)" font-weight="500"></text>'
        "</g></g>"
    )
    parts.append("</svg>")
    return "".join(parts)


def stacked_bars(
    series: list[tuple[str, list[float | None]]],
    labels: list[str],
    width: int = 560,
    height: int = 220,
    palette: list[str] | None = None,
) -> str:
    """Stacked bars over time. Hover tooltip wired by the script layer.

    ``series`` is a list of (segment_name, values_aligned_to_labels) tuples.
    Any ``None`` in the values is treated as zero for stacking but skipped in
    the rendered segment (no zero-height rect).
    """
    if not labels or not series:
        return f'<svg width="{width}" height="{height}" aria-hidden="true"></svg>'

    palette = palette or [
        "var(--seg-1)",
        "var(--seg-2)",
        "var(--seg-3)",
        "var(--seg-4)",
        "var(--seg-5)",
    ]
    pad_t, pad_r, pad_b, pad_l = 12, 12, 24, 40
    plot_w = width - pad_l - pad_r
    plot_h = height - pad_t - pad_b
    n = len(labels)
    bar_w = (plot_w / n) * 0.7

    def _cell(s: tuple[str, list[float | None]], i: int) -> float:
        if i >= len(s[1]):
            return 0.0
        v = s[1][i]
        return v if v is not None else 0.0

    totals = [sum(_cell(s, i) for s in series) for i in range(n)]
    max_t = max(totals) * 1.05 if any(totals) else 1.0

    parts: list[str] = [
        f'<svg class="ws-stacked" width="{width}" height="{height}" style="display:block">'
    ]
    # Grid + y labels
    for f in (0.0, 0.25, 0.5, 0.75, 1.0):
        gy = pad_t + plot_h - f * plot_h
        parts.append(
            f'<line x1="{pad_l}" x2="{pad_l + plot_w:.1f}" y1="{gy:.1f}" y2="{gy:.1f}" '
            f'stroke="var(--border)" stroke-width="0.5" '
            f'stroke-dasharray="{"0" if f == 0 else "2 3"}" />'
        )
        parts.append(
            f'<text x="{pad_l - 6}" y="{gy + 3:.1f}" text-anchor="end" font-size="9.5" '
            f'fill="var(--muted)" font-family="var(--mono)">'
            f"{(f * max_t / 1000):.0f}B</text>"
        )
    # Bars
    for i, lbl in enumerate(labels):
        center_x = pad_l + (i + 0.5) * (plot_w / n)
        bx = center_x - bar_w / 2
        y_cursor = pad_t + plot_h
        tooltip_lines = [_esc(lbl)]
        for si, (name, vals) in enumerate(series):
            v = _cell((name, vals), i)
            if v <= 0:
                continue
            h = (v / max_t) * plot_h
            y_cursor -= h
            color = palette[si % len(palette)]
            parts.append(
                f'<rect x="{bx:.1f}" y="{y_cursor:.1f}" width="{bar_w:.1f}" '
                f'height="{max(0.0, h - 0.5):.1f}" fill="{color}" opacity="0.85">'
                f"<title>{_esc(name)}: ${v / 1000:.1f}B</title></rect>"
            )
            tooltip_lines.append(f"{name}: ${v / 1000:.1f}B")
        # Sparse x labels
        if i == 0 or i == n - 1 or i % 3 == 0:
            parts.append(
                f'<text x="{center_x:.1f}" y="{height - 6}" text-anchor="middle" '
                f'font-size="9.5" fill="var(--muted)" font-family="var(--mono)">'
                f"{_esc(lbl.replace('20', _RSQUO))}</text>"
            )
    parts.append("</svg>")
    return "".join(parts)


def sensitivity_grid(
    waccs: list[float],
    growths: list[float],
    grid: list[list[float]],
    base_wacc: float,
    base_growth: float,
    current_price: float,
) -> str:
    """WACC × g sensitivity table. Cells colored above/below current price.

    Returns an HTML ``<table>`` (not SVG) because the design uses the
    paper/accent token swatches per cell.
    """
    out: list[str] = ['<table class="sens-table"><thead><tr><th></th>']
    for w in waccs:
        cls = " col-base" if abs(w - base_wacc) < 1e-9 else ""
        out.append(f'<th class="{cls.strip()}">{w * 100:.1f}%</th>')
    out.append("</tr></thead><tbody>")
    for gi, g in enumerate(growths):
        row_cls = "row-base" if abs(g - base_growth) < 1e-9 else ""
        out.append(f'<tr class="{row_cls}">')
        out.append(f'<th class="sens-row-h">g {g * 100:.1f}%</th>')
        for wi, w in enumerate(waccs):
            v = grid[gi][wi]
            is_base = abs(w - base_wacc) < 1e-9 and abs(g - base_growth) < 1e-9
            above = v > current_price
            cls = "sens-cell " + ("above" if above else "below")
            if is_base:
                cls += " base"
            display = f"{round(v)}" if v >= 100 else f"{v:.0f}"
            out.append(f'<td class="{cls}">{display}</td>')
        out.append("</tr>")
    out.append("</tbody></table>")
    return "".join(out)


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
            f'background:{color};opacity:{opacity};border-radius:1px"></div>'
        )
    return f'<div style="display:flex;gap:2px;align-items:center">{"".join(cells)}</div>'


def compute_gordon_sensitivity(
    base_value: float,
    base_wacc: float,
    base_growth: float,
    waccs: list[float] | None = None,
    growths: list[float] | None = None,
) -> dict[str, object]:
    """Approximate a WACC × g grid via Gordon growth around the base case.

    The pipeline only computes a single point estimate today, so we extrapolate
    a sensitivity surface using ``value = c / (wacc - g)`` where ``c`` is fit
    from the base. Returned dict is shaped for ``sensitivity_grid``.
    """
    waccs = waccs or [0.07, 0.075, 0.08, 0.085, 0.09, 0.095, 0.10, 0.105, 0.11]
    growths = growths or [0.015, 0.02, 0.025, 0.03, 0.035]
    # Solve for c: base = c / (base_wacc - base_growth)
    discount = max(base_wacc - base_growth, 1e-6)
    c = base_value * discount
    grid: list[list[float]] = []
    for g in growths:
        row: list[float] = []
        for w in waccs:
            denom = max(w - g, 1e-6)
            v = c / denom
            row.append(round(v, 2))
        grid.append(row)
    return {
        "waccs": waccs,
        "growths": growths,
        "grid": grid,
        "base_wacc": base_wacc,
        "base_growth": base_growth,
    }


def _quarter_short(label: str) -> str:
    """Convert ``2026 Q1`` → ``Q1 '26``. Pass-through if format unknown."""
    parts = label.split()
    if len(parts) == 2 and parts[0].isdigit() and len(parts[0]) == 4:
        yr = parts[0][2:]
        return f"{parts[1]} {_RSQUO}{yr}"
    return label


__all__ = [
    "_quarter_short",
    "compute_gordon_sensitivity",
    "line_chart",
    "sensitivity_grid",
    "sparkline",
    "stacked_bars",
    "verdict_bar",
]
