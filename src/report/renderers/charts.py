"""Inline SVG chart helpers — no external libs, no JS, prints clean.

Two primitives:
  - sparkline(values, width, height): row-level trend indicator, ~120x28px
  - bar_chart(values, labels, width, height): full-width bars per quarter

Both accept lists with None values (gaps); both auto-scale to the data range
and emit a self-contained <svg> string ready to drop into HTML.
"""

from __future__ import annotations

import html


def sparkline(values: list[float | None], width: int = 120, height: int = 28) -> str:
    """Tiny line chart for trend-at-a-glance. Skips None segments."""
    points = _scale_points(values, width, height, pad=2)
    if len(points) < 2:
        return ""
    path = _build_path(points)
    last = points[-1]
    return (
        f'<svg class="sparkline" width="{width}" height="{height}" viewBox="0 0 {width} {height}" '
        f'preserveAspectRatio="none" aria-hidden="true">'
        f'<path d="{path}" fill="none" stroke="currentColor" stroke-width="1.5"/>'
        f'<circle cx="{last[0]:.1f}" cy="{last[1]:.1f}" r="2" fill="currentColor"/>'
        f"</svg>"
    )


def line_chart(
    values: list[float | None],
    labels: list[str],
    title: str = "",
    width: int = 480,
    height: int = 180,
) -> str:
    """Larger labelled line chart for the financials primary metrics."""
    if not any(v is not None for v in values) or len(values) < 2:
        return f'<div class="chart-empty">No data for <em>{html.escape(title)}</em></div>'

    pad_left, pad_right, pad_top, pad_bottom = 44, 16, 22, 26
    plot_w = width - pad_left - pad_right
    plot_h = height - pad_top - pad_bottom

    valid_vals = [v for v in values if v is not None]
    vmin, vmax = min(valid_vals), max(valid_vals)
    if vmin == vmax:
        vmin -= 1
        vmax += 1
    rng = vmax - vmin

    pts: list[tuple[float, float]] = []
    for i, v in enumerate(values):
        if v is None:
            continue
        x = pad_left + (i / (len(values) - 1)) * plot_w if len(values) > 1 else pad_left
        y = pad_top + (1 - (v - vmin) / rng) * plot_h
        pts.append((x, y))

    path = _build_path(pts)
    y_axis_labels = [
        (pad_top, _fmt_axis(vmax)),
        (pad_top + plot_h / 2, _fmt_axis((vmin + vmax) / 2)),
        (pad_top + plot_h, _fmt_axis(vmin)),
    ]

    parts = [
        f'<svg class="chart" width="{width}" height="{height}" viewBox="0 0 {width} {height}" role="img" aria-label="{html.escape(title)}">',
    ]
    if title:
        parts.append(f'<text x="{pad_left}" y="14" class="chart-title">{html.escape(title)}</text>')
    for y, label in y_axis_labels:
        parts.append(f'<line x1="{pad_left}" y1="{y:.1f}" x2="{width - pad_right}" y2="{y:.1f}" class="chart-grid"/>')
        parts.append(f'<text x="{pad_left - 4}" y="{y + 3:.1f}" class="chart-axis" text-anchor="end">{label}</text>')

    label_indices = _x_label_indices(len(labels))
    for i in label_indices:
        x = pad_left + (i / (len(labels) - 1)) * plot_w if len(labels) > 1 else pad_left
        parts.append(
            f'<text x="{x:.1f}" y="{height - 8}" class="chart-axis" text-anchor="middle">{html.escape(labels[i])}</text>'
        )

    parts.append(f'<path d="{path}" fill="none" stroke="var(--accent)" stroke-width="1.8"/>')
    for x, y in pts:
        parts.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="2" fill="var(--accent)"/>')
    parts.append("</svg>")
    return "".join(parts)


def _scale_points(
    values: list[float | None], width: int, height: int, pad: int
) -> list[tuple[float, float]]:
    valid = [(i, v) for i, v in enumerate(values) if v is not None]
    if not valid:
        return []
    vmin = min(v for _, v in valid)
    vmax = max(v for _, v in valid)
    if vmin == vmax:
        vmin -= 1
        vmax += 1
    rng = vmax - vmin
    n = len(values)
    plot_w = width - 2 * pad
    plot_h = height - 2 * pad
    pts: list[tuple[float, float]] = []
    for i, v in valid:
        x = pad + (i / (n - 1)) * plot_w if n > 1 else pad
        y = pad + (1 - (v - vmin) / rng) * plot_h
        pts.append((x, y))
    return pts


def _build_path(points: list[tuple[float, float]]) -> str:
    if not points:
        return ""
    parts = [f"M{points[0][0]:.1f},{points[0][1]:.1f}"]
    for x, y in points[1:]:
        parts.append(f"L{x:.1f},{y:.1f}")
    return " ".join(parts)


def _fmt_axis(v: float) -> str:
    """Compact axis label: 12,345 → 12.3K; 1,234,567 → 1.2M."""
    av = abs(v)
    if av >= 1_000_000:
        return f"{v / 1_000_000:.1f}M"
    if av >= 1_000:
        return f"{v / 1_000:.1f}K"
    if av >= 100:
        return f"{v:.0f}"
    if av >= 1:
        return f"{v:.1f}"
    return f"{v:.2f}"


def _x_label_indices(n: int) -> list[int]:
    """Pick which x-axis labels to show — first, middle(s), last for readability."""
    if n <= 1:
        return [0] if n == 1 else []
    if n <= 4:
        return list(range(n))
    if n <= 8:
        return [0, n // 2, n - 1]
    return [0, n // 3, 2 * n // 3, n - 1]


CHART_CSS = """
.sparkline { display: inline-block; vertical-align: middle; color: var(--accent); }
.chart { display: block; margin: 8px 0; max-width: 100%; height: auto; }
.chart-title { font-size: 11px; font-weight: 600; fill: var(--fg); }
.chart-grid { stroke: var(--border); stroke-width: 1; }
.chart-axis { font-size: 10px; fill: var(--muted); font-family: 'JetBrains Mono', Consolas, monospace; }
.chart-empty { font-size: 12px; color: var(--muted); padding: 8px 12px; background: var(--subheader-bg); border-radius: 4px; margin: 8px 0; }
.chart-grid-2col { display: grid; grid-template-columns: repeat(2, 1fr); gap: 16px; margin: 12px 0; }
.chart-grid-1col { display: grid; grid-template-columns: 1fr; gap: 16px; margin: 12px 0; }
"""
