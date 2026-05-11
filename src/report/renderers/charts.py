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
    last_value = next((v for v in reversed(values) if v is not None), None)
    last_title = f"<title>Latest: {_fmt_tooltip(last_value)}</title>" if last_value is not None else ""
    return (
        f'<svg class="sparkline" width="{width}" height="{height}" viewBox="0 0 {width} {height}" '
        f'preserveAspectRatio="none" aria-hidden="true">'
        f'<path d="{path}" fill="none" stroke="currentColor" stroke-width="1.5"/>'
        f'<circle cx="{last[0]:.1f}" cy="{last[1]:.1f}" r="2" fill="currentColor">{last_title}</circle>'
        f"</svg>"
    )


def line_chart(
    values: list[float | None],
    labels: list[str],
    title: str = "",
    width: int = 480,
    height: int = 180,
) -> str:
    """Larger labelled line chart for the financials primary metrics.

    Native browser tooltips (`<title>`) on each data point + an invisible
    hover-target rect per index so the user can mouse anywhere along the
    chart and read off the period + value.
    """
    if not any(v is not None for v in values):
        return f'<div class="chart-empty">No data for <em>{html.escape(title)}</em></div>'
    if sum(1 for v in values if v is not None) < 2:
        # Single-point series — render a labeled stat card instead of a misleading line.
        only_idx = next(i for i, v in enumerate(values) if v is not None)
        only_label = labels[only_idx] if only_idx < len(labels) else ""
        only_value = values[only_idx]
        return (
            f'<div class="chart-empty"><strong>{html.escape(title)}</strong><br>'
            f'<span class="meta">Only one observation: {html.escape(only_label)} '
            f'= {_fmt_tooltip(only_value)}</span></div>'
        )

    pad_left, pad_right, pad_top, pad_bottom = 48, 16, 22, 36
    plot_w = width - pad_left - pad_right
    plot_h = height - pad_top - pad_bottom

    valid_vals = [v for v in values if v is not None]
    vmin, vmax = min(valid_vals), max(valid_vals)
    if vmin == vmax:
        vmin -= 1
        vmax += 1
    rng = vmax - vmin

    n = len(values)
    pts: list[tuple[float, float, int]] = []  # (x, y, index)
    for i, v in enumerate(values):
        if v is None:
            continue
        x = pad_left + (i / (n - 1)) * plot_w if n > 1 else pad_left
        y = pad_top + (1 - (v - vmin) / rng) * plot_h
        pts.append((x, y, i))

    path = _build_path([(x, y) for (x, y, _) in pts])
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
        # Rotate labels around their anchor so 12 quarters fit without overlap.
        parts.append(
            f'<text x="{x:.1f}" y="{height - 10}" class="chart-axis" text-anchor="end" '
            f'transform="rotate(-35 {x:.1f} {height - 10})">{html.escape(labels[i])}</text>'
        )

    parts.append(f'<path d="{path}" fill="none" stroke="var(--accent)" stroke-width="1.8"/>')
    for x, y, i in pts:
        label = labels[i] if i < len(labels) else ""
        value = values[i]
        tip = f"{label}: {_fmt_tooltip(value)}" if label else _fmt_tooltip(value)
        parts.append(
            f'<circle cx="{x:.1f}" cy="{y:.1f}" r="3" fill="var(--accent)" class="chart-pt">'
            f'<title>{html.escape(tip)}</title></circle>'
        )
    # Invisible hover targets per index — wider catch area than the 3px circles.
    if n > 1:
        col_w = plot_w / (n - 1)
        for i, v in enumerate(values):
            if v is None:
                continue
            x = pad_left + (i / (n - 1)) * plot_w
            label = labels[i] if i < len(labels) else ""
            tip = f"{label}: {_fmt_tooltip(v)}" if label else _fmt_tooltip(v)
            parts.append(
                f'<rect x="{x - col_w / 2:.1f}" y="{pad_top}" width="{col_w:.1f}" '
                f'height="{plot_h:.1f}" fill="transparent" class="chart-hover-target">'
                f'<title>{html.escape(tip)}</title></rect>'
            )
    parts.append("</svg>")
    return "".join(parts)


def _fmt_tooltip(v: float | None) -> str:
    """Human-readable value for hover tooltips. Pairs with _fmt_axis on the axis."""
    if v is None:
        return "—"
    av = abs(v)
    if av >= 1_000_000_000:
        return f"{v / 1_000_000_000:,.2f}B"
    if av >= 1_000_000:
        return f"{v / 1_000_000:,.2f}M"
    if av >= 1_000:
        return f"{v:,.1f}"
    if av >= 1:
        return f"{v:,.2f}"
    return f"{v:.4f}"


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
    """Pick which x-axis labels to show.

    Labels are rotated -35° in line_chart so they fit even when dense.
    For 12-quarter charts we show every other quarter; for shorter
    series, every point. The first and last are always included.
    """
    if n <= 1:
        return [0] if n == 1 else []
    if n <= 8:
        return list(range(n))
    # 9-16 points: every other index, plus n-1 to guarantee the last label.
    indices = list(range(0, n, 2))
    if indices[-1] != n - 1:
        indices.append(n - 1)
    return indices


CHART_CSS = """
.sparkline { display: inline-block; vertical-align: middle; color: var(--accent); }
.chart { display: block; margin: 8px 0; max-width: 100%; height: auto; }
.chart-title { font-size: 11px; font-weight: 600; fill: var(--fg); }
.chart-grid { stroke: var(--border); stroke-width: 1; }
.chart-axis { font-size: 10px; fill: var(--muted); font-family: 'JetBrains Mono', Consolas, monospace; }
.chart-empty { font-size: 12px; color: var(--muted); padding: 8px 12px; background: var(--subheader-bg); border-radius: 4px; margin: 8px 0; }
.chart-grid-2col { display: grid; grid-template-columns: repeat(2, 1fr); gap: 16px; margin: 12px 0; }
.chart-grid-1col { display: grid; grid-template-columns: 1fr; gap: 16px; margin: 12px 0; }
.chart-pt { cursor: crosshair; transition: r 80ms ease-out; }
.chart-pt:hover { r: 5; }
.chart-hover-target { cursor: crosshair; pointer-events: all; }
.chart-hover-target:hover { fill: var(--accent); fill-opacity: 0.06; }
"""
