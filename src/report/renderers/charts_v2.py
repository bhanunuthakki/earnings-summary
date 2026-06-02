"""Analyst-grade chart + table primitives. Hand-rolled SVG, no JS.

Design goals (vs. existing charts.py):
  - Every data point labeled inline — no hover dependency, readable in
    screenshots and on print
  - Zero-baseline by default for level series
  - Paired views (level + YoY% side-by-side) for headline metrics
  - Heat-shaded YoY% matrices for fast decel/accel scanning

Modeled on MBI Deep Dives chart conventions.
"""

from __future__ import annotations

import html
import math
from dataclasses import dataclass

# 6-color colorblind-safe palette (Okabe-Ito ordering, adjusted).
PALETTE = [
    "#0173b2",  # blue
    "#de8f05",  # orange
    "#029e73",  # green
    "#cc78bc",  # purple
    "#ca9161",  # tan
    "#949494",  # grey
]
POS_COLOR = "#029e73"  # green
NEG_COLOR = "#cb4d4d"  # red
NEUTRAL_COLOR = "#5a6b78"


# ----- Number formatting -----

def fmt_compact(v: float | None, digits: int = 1) -> str:
    """Compact human-readable: 47,516 → 47.5K; 47,516,000,000 → 47.5B."""
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return "—"
    av = abs(v)
    if av >= 1_000_000_000:
        return f"{v / 1_000_000_000:.{digits}f}B"
    if av >= 1_000_000:
        return f"{v / 1_000_000:.{digits}f}M"
    if av >= 1_000:
        return f"{v / 1_000:.{digits}f}K"
    if av >= 1:
        return f"{v:.{digits}f}"
    return f"{v:.{digits + 1}f}"


def fmt_pct(v: float | None, digits: int = 1) -> str:
    """Percentage with sign: 0.224 → +22.4%. Pass v in decimal (0.224) or pct (22.4)?
    Convention here: v is already in PERCENT units (22.4 not 0.224)."""
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return "—"
    sign = "+" if v > 0 else ""
    return f"{sign}{v:.{digits}f}%"


def fmt_dollar(v: float | None, digits: int = 1) -> str:
    if v is None:
        return "—"
    return "$" + fmt_compact(v, digits)


# ----- Ratio vs flow classification (matrix display mode) -----

_LEVEL_UNITS = frozenset({"%", "percent", "pp", "bps", "ratio"})


def _is_level_unit(unit: str) -> bool:
    """True for ratio/percentage units whose YoY% is meaningless — the matrix
    shows the ABSOLUTE level instead (ROE, NIM, CET1, margins, NPL ratios).
    Dollar flows and counts keep the YoY% + CAGR treatment."""
    u = (unit or "").strip().lower()
    return u in _LEVEL_UNITS or u.endswith("%")


def _fmt_level(v: float | None, unit: str) -> str:
    """Absolute level cell for a ratio/percentage row."""
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return "—"
    u = (unit or "").strip().lower()
    if u in {"%", "percent"} or u.endswith("%"):
        return f"{v:.1f}%"
    if u == "bps":
        return f"{v:.0f}bps"
    if u == "ratio":
        return f"{v:.2f}"
    return fmt_compact(v)


def _fmt_level_delta(v: float | None, unit: str) -> str:
    """Absolute change over a horizon for a ratio/percentage row (pp/bps) — the
    useful trailing-column analog of CAGR, which is meaningless for a ratio."""
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return "—"
    u = (unit or "").strip().lower()
    if u == "bps":
        return f"{v:+.0f}bps"
    if u == "ratio":
        return f"{v:+.2f}"
    return f"{v:+.1f}pp"


# ----- Bar charts -----

@dataclass
class BarSpec:
    values: list[float | None]
    labels: list[str]
    title: str
    width: int = 540
    height: int = 220
    value_fmt: str = "pct"   # "pct" | "dollar" | "compact"
    signed_color: bool = True  # green/red based on sign
    bar_color: str | None = None  # override single color
    clip_outliers: bool = True  # clip Y-axis at IQR x 2.5 so one spike doesn't crush the rest


def bar_chart(spec: BarSpec) -> str:
    """Vertical bars, zero baseline, every bar labeled inline.

    Positive bars rise above zero; negative bars hang below. Y-axis shows
    zero line + min/max gridlines. X-axis labels rotated -35° (every label).
    """
    pad_left, pad_right, pad_top, pad_bottom = 50, 12, 26, 44
    width, height = spec.width, spec.height
    plot_w = width - pad_left - pad_right
    plot_h = height - pad_top - pad_bottom

    valid = [v for v in spec.values if v is not None]
    if not valid:
        return _empty_chart(spec.title, width, height)

    # Outlier clip: shrink axis to IQR x 2.5 so a single spike doesn't crush the chart.
    # Bars outside the clip range still draw with their full numeric label, but
    # the bar visually overflows (clipped at chart edge) and gets a ↑/↓ marker.
    if spec.clip_outliers and len(valid) >= 6:
        sorted_v = sorted(valid)
        q1 = sorted_v[len(sorted_v) // 4]
        q3 = sorted_v[(3 * len(sorted_v)) // 4]
        iqr = q3 - q1
        if iqr > 0:
            lo_clip = q1 - 2.5 * iqr
            hi_clip = q3 + 2.5 * iqr
            vmin = max(min(valid), lo_clip)
            vmax = min(max(valid), hi_clip)
        else:
            vmin, vmax = min(valid), max(valid)
    else:
        vmin, vmax = min(valid), max(valid)

    # Always include zero so the baseline is visible.
    if vmin > 0:
        vmin = 0
    if vmax < 0:
        vmax = 0
    if vmin == vmax:
        vmax = vmin + 1
    rng = vmax - vmin

    n = len(spec.values)
    col_w = plot_w / n
    bar_w = col_w * 0.62
    zero_y = pad_top + (vmax / rng) * plot_h

    def fmt(v: float) -> str:
        if spec.value_fmt == "pct":
            return fmt_pct(v)
        if spec.value_fmt == "dollar":
            return fmt_dollar(v)
        return fmt_compact(v)

    parts = [
        f'<svg class="cv2-chart" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" role="img" aria-label="{html.escape(spec.title)}">',
        f'<text x="{pad_left}" y="14" class="cv2-title">{html.escape(spec.title)}</text>',
    ]

    # Gridlines: zero + min + max. Skip if zero coincides.
    for y_val, css in [(vmax, "cv2-grid"), (0.0, "cv2-grid-zero"), (vmin, "cv2-grid")]:
        if y_val == 0 and (vmin == 0 or vmax == 0):
            continue
        y = pad_top + ((vmax - y_val) / rng) * plot_h
        parts.append(f'<line x1="{pad_left}" y1="{y:.1f}" x2="{width - pad_right}" y2="{y:.1f}" class="{css}"/>')
        label_txt = fmt(y_val) if css == "cv2-grid" else "0"
        parts.append(f'<text x="{pad_left - 4}" y="{y + 3:.1f}" class="cv2-axis" text-anchor="end">{label_txt}</text>')

    # Always emit the zero line if not already covered.
    if vmin < 0 < vmax:
        parts.append(
            f'<line x1="{pad_left}" y1="{zero_y:.1f}" x2="{width - pad_right}" y2="{zero_y:.1f}" '
            f'class="cv2-grid-zero"/>'
        )

    # Bars + labels.
    for i, v in enumerate(spec.values):
        x = pad_left + i * col_w + (col_w - bar_w) / 2
        label_txt = html.escape(spec.labels[i]) if i < len(spec.labels) else ""
        # X-axis label — horizontal (no rotation) thanks to compact "Q3'23" format.
        parts.append(
            f'<text x="{x + bar_w / 2:.1f}" y="{height - 12}" class="cv2-axis" '
            f'text-anchor="middle">{label_txt}</text>'
        )
        if v is None:
            continue
        # Clip the visible bar at the chart edges if the value overflows the clip range.
        v_clipped_top = min(max(v, 0), vmax)
        v_clipped_bot = max(min(v, 0), vmin)
        overflow_up = v > vmax
        overflow_dn = v < vmin
        y_top = pad_top + ((vmax - v_clipped_top) / rng) * plot_h
        y_bot = pad_top + ((vmax - v_clipped_bot) / rng) * plot_h
        bar_h = y_bot - y_top
        if spec.signed_color:
            color = POS_COLOR if v >= 0 else NEG_COLOR
        else:
            color = spec.bar_color or NEUTRAL_COLOR
        parts.append(
            f'<rect x="{x:.1f}" y="{y_top:.1f}" width="{bar_w:.1f}" height="{max(bar_h, 0.5):.1f}" '
            f'fill="{color}" class="cv2-bar"/>'
        )
        # Inline value label — above positive bars, below negative bars.
        v_text = fmt(v)
        if overflow_up:
            v_text = "↑ " + v_text
        elif overflow_dn:
            v_text = "↓ " + v_text
        if v >= 0:
            ly = y_top - 4
            anchor = "middle"
        else:
            ly = y_bot + 11
            anchor = "middle"
        parts.append(
            f'<text x="{x + bar_w / 2:.1f}" y="{ly:.1f}" class="cv2-bar-label" '
            f'text-anchor="{anchor}">{v_text}</text>'
        )

    parts.append("</svg>")
    return "".join(parts)


# ----- Multi-line chart (overlays) -----

@dataclass
class LineSeries:
    name: str
    values: list[float | None]


def multi_line_chart(
    series: list[LineSeries],
    labels: list[str],
    title: str,
    width: int = 560,
    height: int = 240,
    value_fmt: str = "pct",
) -> str:
    """Overlay N line series. Legend at top-right, end-of-line labels.

    Every data point gets a small dot + the value of the LAST point is
    printed at the end of each line. Y-axis auto-scales to ALL series.
    """
    pad_left, pad_right, pad_top, pad_bottom = 50, 56, 32, 38
    plot_w = width - pad_left - pad_right
    plot_h = height - pad_top - pad_bottom

    all_vals = [v for s in series for v in s.values if v is not None]
    if not all_vals:
        return _empty_chart(title, width, height)

    vmin, vmax = min(all_vals), max(all_vals)
    # Pct charts: include 0 if all positive (preserves "where margin came from").
    if value_fmt == "pct" and vmin > 0 and vmin < 5:
        vmin = 0
    if vmin == vmax:
        vmax = vmin + 1
    rng = vmax - vmin

    def fmt(v: float) -> str:
        if value_fmt == "pct":
            return fmt_pct(v)
        if value_fmt == "dollar":
            return fmt_dollar(v)
        return fmt_compact(v)

    n = len(labels)
    parts = [
        f'<svg class="cv2-chart" width="{width}" height="{height}" viewBox="0 0 {width} {height}" '
        f'role="img" aria-label="{html.escape(title)}">',
        f'<text x="{pad_left}" y="14" class="cv2-title">{html.escape(title)}</text>',
    ]

    # Y-axis: top, middle, bottom.
    for y_val in [vmax, (vmin + vmax) / 2, vmin]:
        y = pad_top + ((vmax - y_val) / rng) * plot_h
        parts.append(f'<line x1="{pad_left}" y1="{y:.1f}" x2="{width - pad_right}" y2="{y:.1f}" class="cv2-grid"/>')
        parts.append(f'<text x="{pad_left - 4}" y="{y + 3:.1f}" class="cv2-axis" text-anchor="end">{fmt(y_val)}</text>')

    # X-axis labels — every other one to avoid crowding, horizontal.
    step = max(1, n // 7)
    for i in range(0, n, step):
        x = pad_left + (i / (n - 1)) * plot_w if n > 1 else pad_left
        parts.append(
            f'<text x="{x:.1f}" y="{height - 12}" class="cv2-axis" text-anchor="middle">'
            f'{html.escape(labels[i])}</text>'
        )

    # Lines + dots + end-labels.
    for s_idx, s in enumerate(series):
        color = PALETTE[s_idx % len(PALETTE)]
        pts: list[tuple[float, float, int]] = []
        for i, v in enumerate(s.values):
            if v is None:
                continue
            x = pad_left + (i / (n - 1)) * plot_w if n > 1 else pad_left
            y = pad_top + ((vmax - v) / rng) * plot_h
            pts.append((x, y, i))
        if len(pts) >= 2:
            path = "M" + " L".join(f"{x:.1f},{y:.1f}" for x, y, _ in pts)
            parts.append(f'<path d="{path}" fill="none" stroke="{color}" stroke-width="1.6"/>')
        for x, y, _ in pts:
            parts.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="2.2" fill="{color}"/>')
        # End-of-line value label.
        if pts:
            x, y, i = pts[-1]
            last_v = s.values[i]
            assert last_v is not None
            parts.append(
                f'<text x="{x + 4:.1f}" y="{y + 3:.1f}" class="cv2-line-end" '
                f'fill="{color}">{fmt(last_v)}</text>'
            )

    # Legend top-right inside plot area.
    leg_x = pad_left + 8
    leg_y = pad_top + 2
    for s_idx, s in enumerate(series):
        color = PALETTE[s_idx % len(PALETTE)]
        parts.append(f'<rect x="{leg_x:.1f}" y="{leg_y:.1f}" width="10" height="10" fill="{color}"/>')
        parts.append(
            f'<text x="{leg_x + 14:.1f}" y="{leg_y + 9:.1f}" class="cv2-legend">{html.escape(s.name)}</text>'
        )
        leg_x += 14 + len(s.name) * 6.2 + 12

    parts.append("</svg>")
    return "".join(parts)


# ----- Paired chart: level bars + YoY% bars side-by-side -----

def paired_chart(
    level_values: list[float | None],
    yoy_values: list[float | None],
    labels: list[str],
    title: str,
    *,
    level_fmt: str = "compact",
    panel_width: int = 540,
    height: int = 220,
) -> str:
    """Two side-by-side bar charts: absolute levels (left) + YoY % (right).

    Both share the same X labels. Wrapper div uses cv2-pair grid.
    """
    left = bar_chart(BarSpec(
        values=level_values,
        labels=labels,
        title=f"{title} — level",
        width=panel_width,
        height=height,
        value_fmt=level_fmt,
        signed_color=False,
        bar_color=NEUTRAL_COLOR,
    ))
    right = bar_chart(BarSpec(
        values=yoy_values,
        labels=labels,
        title=f"{title} — YoY %",
        width=panel_width,
        height=height,
        value_fmt="pct",
        signed_color=True,
    ))
    return f'<div class="cv2-pair">{left}{right}</div>'


# ----- Stacked area (segment mix shift) -----

def stacked_area(
    series: list[LineSeries],
    labels: list[str],
    title: str,
    width: int = 720,
    height: int = 260,
) -> str:
    """Stacked area chart for segment mix-shift. Each band labeled at right.

    Series stacked bottom-to-top in the order provided. Top series gets
    its end-of-band label first. Y-axis shows total magnitude at min/max.
    """
    pad_left, pad_right, pad_top, pad_bottom = 56, 92, 32, 40
    plot_w = width - pad_left - pad_right
    plot_h = height - pad_top - pad_bottom

    n = len(labels)
    if n == 0 or not series:
        return _empty_chart(title, width, height)

    # Replace None with 0 for stacking — caller's responsibility to filter.
    stack: list[list[float]] = [
        [(v if v is not None else 0.0) for v in s.values] for s in series
    ]

    totals: list[float] = [
        sum(stack[s_i][t_i] for s_i in range(len(series))) for t_i in range(n)
    ]
    vmax: float = max(totals) if totals else 1.0
    if vmax == 0:
        vmax = 1.0

    # Cumulative bottoms per series per time.
    cum_bottoms: list[list[float]] = []
    running: list[float] = [0.0] * n
    for s_i in range(len(series)):
        cum_bottoms.append(list(running))
        for t_i in range(n):
            running[t_i] += stack[s_i][t_i]

    def x_at(i: int) -> float:
        return pad_left + (i / (n - 1)) * plot_w if n > 1 else pad_left

    def y_at(v: float) -> float:
        return pad_top + (1 - v / vmax) * plot_h

    parts = [
        f'<svg class="cv2-chart" width="{width}" height="{height}" viewBox="0 0 {width} {height}" '
        f'role="img" aria-label="{html.escape(title)}">',
        f'<text x="{pad_left}" y="14" class="cv2-title">{html.escape(title)}</text>',
    ]

    # Y gridlines: 0, half, full.
    for v_grid in [vmax, vmax / 2, 0.0]:
        y = y_at(v_grid)
        parts.append(f'<line x1="{pad_left}" y1="{y:.1f}" x2="{width - pad_right}" y2="{y:.1f}" class="cv2-grid"/>')
        parts.append(f'<text x="{pad_left - 4}" y="{y + 3:.1f}" class="cv2-axis" text-anchor="end">{fmt_compact(v_grid)}</text>')

    # X labels — every other, horizontal.
    step = max(1, n // 7)
    for i in range(0, n, step):
        parts.append(
            f'<text x="{x_at(i):.1f}" y="{height - 12}" class="cv2-axis" text-anchor="middle">'
            f'{html.escape(labels[i])}</text>'
        )

    # Bands.
    for s_i, s in enumerate(series):
        color = PALETTE[s_i % len(PALETTE)]
        top_pts: list[tuple[float, float]] = []
        bot_pts: list[tuple[float, float]] = []
        for t_i in range(n):
            bot: float = cum_bottoms[s_i][t_i]
            top: float = bot + stack[s_i][t_i]
            top_pts.append((x_at(t_i), y_at(top)))
            bot_pts.append((x_at(t_i), y_at(bot)))
        # Polygon: top points forward, bottom points reversed.
        path = "M" + " L".join(f"{x:.1f},{y:.1f}" for x, y in top_pts)
        path += " L" + " L".join(f"{x:.1f},{y:.1f}" for x, y in reversed(bot_pts))
        path += " Z"
        parts.append(f'<path d="{path}" fill="{color}" fill-opacity="0.85" stroke="white" stroke-width="0.5"/>')

        # Right-edge label.
        last_top_y: float = top_pts[-1][1]
        last_bot_y: float = bot_pts[-1][1]
        mid_y: float = (last_top_y + last_bot_y) / 2
        last_val: float = stack[s_i][-1]
        share: float = (last_val / totals[-1] * 100) if totals[-1] else 0
        parts.append(
            f'<text x="{width - pad_right + 4}" y="{mid_y + 3:.1f}" class="cv2-band-label" fill="{color}">'
            f'{html.escape(s.name)} {share:.0f}%</text>'
        )

    parts.append("</svg>")
    return "".join(parts)


def stacked_area_100pct(
    series: list[LineSeries],
    labels: list[str],
    title: str,
    width: int = 720,
    height: int = 260,
) -> str:
    """100%-stacked area chart — pure mix-shift view, totals normalized to 100%.

    Useful when you want to see CONCENTRATION change over time, not the
    absolute level. Bands always sum to 100% at every point.
    """
    pad_left, pad_right, pad_top, pad_bottom = 56, 92, 32, 40
    plot_w = width - pad_left - pad_right
    plot_h = height - pad_top - pad_bottom

    n = len(labels)
    if n == 0 or not series:
        return _empty_chart(title, width, height)

    stack: list[list[float]] = [
        [(v if v is not None else 0.0) for v in s.values] for s in series
    ]
    totals: list[float] = [
        sum(stack[s_i][t_i] for s_i in range(len(series))) for t_i in range(n)
    ]

    # Normalize to share at each time point.
    shares: list[list[float]] = []
    for s_i in range(len(series)):
        row: list[float] = []
        for t_i in range(n):
            tot = totals[t_i]
            row.append((stack[s_i][t_i] / tot * 100) if tot else 0.0)
        shares.append(row)

    cum_bottoms: list[list[float]] = []
    running: list[float] = [0.0] * n
    for s_i in range(len(series)):
        cum_bottoms.append(list(running))
        for t_i in range(n):
            running[t_i] += shares[s_i][t_i]

    def x_at(i: int) -> float:
        return pad_left + (i / (n - 1)) * plot_w if n > 1 else pad_left

    def y_at(v: float) -> float:
        return pad_top + (1 - v / 100.0) * plot_h

    parts = [
        f'<svg class="cv2-chart" width="{width}" height="{height}" viewBox="0 0 {width} {height}" '
        f'role="img" aria-label="{html.escape(title)}">',
        f'<text x="{pad_left}" y="14" class="cv2-title">{html.escape(title)}</text>',
    ]

    # Y-axis: 0%, 50%, 100%.
    for v_grid in [100, 50, 0]:
        y = y_at(v_grid)
        parts.append(f'<line x1="{pad_left}" y1="{y:.1f}" x2="{width - pad_right}" y2="{y:.1f}" class="cv2-grid"/>')
        parts.append(f'<text x="{pad_left - 4}" y="{y + 3:.1f}" class="cv2-axis" text-anchor="end">{v_grid}%</text>')

    # X labels — horizontal.
    step = max(1, n // 7)
    for i in range(0, n, step):
        parts.append(
            f'<text x="{x_at(i):.1f}" y="{height - 12}" class="cv2-axis" text-anchor="middle">'
            f'{html.escape(labels[i])}</text>'
        )

    for s_i, s in enumerate(series):
        color = PALETTE[s_i % len(PALETTE)]
        top_pts: list[tuple[float, float]] = []
        bot_pts: list[tuple[float, float]] = []
        for t_i in range(n):
            bot = cum_bottoms[s_i][t_i]
            top = bot + shares[s_i][t_i]
            top_pts.append((x_at(t_i), y_at(top)))
            bot_pts.append((x_at(t_i), y_at(bot)))
        path = "M" + " L".join(f"{x:.1f},{y:.1f}" for x, y in top_pts)
        path += " L" + " L".join(f"{x:.1f},{y:.1f}" for x, y in reversed(bot_pts))
        path += " Z"
        parts.append(f'<path d="{path}" fill="{color}" fill-opacity="0.85" stroke="white" stroke-width="0.5"/>')

        last_top_y: float = top_pts[-1][1]
        last_bot_y: float = bot_pts[-1][1]
        mid_y: float = (last_top_y + last_bot_y) / 2
        latest_share: float = shares[s_i][-1]
        parts.append(
            f'<text x="{width - pad_right + 4}" y="{mid_y + 3:.1f}" class="cv2-band-label" fill="{color}">'
            f'{html.escape(s.name)} {latest_share:.0f}%</text>'
        )

    parts.append("</svg>")
    return "".join(parts)


# ----- YoY heatmap matrix (HTML table) -----

@dataclass
class MatrixRow:
    name: str
    levels: list[float | None]    # absolute values aligned to `periods`
    unit: str = ""                # display hint
    tooltip: str = ""             # if set, name gets a 📖 mark + title= attribute


def yoy_heatmap_table(
    rows: list[MatrixRow],
    periods: list[str],
    title: str,
    display_quarters: int = 12,
    cagr_periods: tuple[int, ...] = (4, 8, 12),  # in quarters (1y, 2y, 3y)
) -> str:
    """Wide YoY% matrix with heat shading + trailing CAGR columns.

    Each cell shows YoY% (current quarter / 4-quarters-ago - 1). The right
    columns show trailing 1y/2y/3y CAGR computed from absolute levels.

    Cells are heat-shaded: green for positive growth, red for negative,
    saturation proportional to magnitude (capped at ±30%).
    """
    if not rows or not periods:
        return f'<div class="cv2-empty">No data for {html.escape(title)}</div>'

    # YoY needs 4 quarters of lookback, so we operate on the FULL series and
    # truncate the display window at the end.
    n_total = len(periods)
    start_display = max(0, n_total - display_quarters)
    display_periods = periods[start_display:]

    def yoy(curr: float | None, prior: float | None) -> float | None:
        if curr is None or prior is None or prior == 0:
            return None
        # Allow sign flips through (e.g. loss → profit) but cap extreme noise.
        return (curr / prior - 1) * 100

    def cagr(curr: float | None, prior: float | None, n_years: float) -> float | None:
        if curr is None or prior is None or prior <= 0 or curr <= 0:
            return None
        return ((curr / prior) ** (1 / n_years) - 1) * 100

    def heat_color(pct: float | None) -> str:
        if pct is None:
            return "background:transparent"
        # Saturation from |pct| capped at 30% → ~0.28 alpha.
        mag = min(abs(pct), 30) / 30
        alpha = 0.08 + 0.28 * mag
        if pct >= 0:
            return f"background:rgba(2,158,115,{alpha:.2f})"
        return f"background:rgba(203,77,77,{alpha:.2f})"

    def is_noisy(pct: float | None, base: float | None, latest: float | None) -> bool:
        """A cell is 'low-base noisy' when the divisor is much smaller than the
        latest comparable value — typical for cash flow / income lines where a
        single near-zero base distorts the ratio. We flag |YoY| > 200% OR
        |base| < 15% of |latest|."""
        if pct is None or base is None or latest is None:
            return False
        if abs(pct) > 200:
            return True
        return abs(latest) > 0 and abs(base) / abs(latest) < 0.15

    th = ['<thead><tr><th class="cv2-matrix-label">Line item</th>']
    for p in display_periods:
        th.append(f'<th class="cv2-matrix-q">{html.escape(p)}</th>')
    for q in cagr_periods:
        yrs = q / 4
        th.append(f'<th class="cv2-matrix-cagr">{int(yrs) if yrs.is_integer() else yrs}y CAGR</th>')
    th.append("</tr></thead>")

    tb = ["<tbody>"]
    latest = rows[0].levels[-1] if rows else None
    for row in rows:
        latest_val = row.levels[-1] if row.levels else None
        # Ratio/percentage rows render absolute levels (+ pp/bps change); dollar
        # flows and counts keep YoY% (+ CAGR). Driven off the per-row unit.
        level_mode = _is_level_unit(row.unit)
        if row.tooltip:
            label_html = (
                f'<span title="{html.escape(row.tooltip)}">{html.escape(row.name)}'
                f' <span class="cv2-matrix-def-mark">📖</span></span>'
            )
        else:
            label_html = html.escape(row.name)
        tb.append(f'<tr><th class="cv2-matrix-label">{label_html}</th>')
        for j_display, _ in enumerate(display_periods):
            j_full = start_display + j_display
            curr = row.levels[j_full]
            base = row.levels[j_full - 4] if j_full >= 4 else None
            pct = yoy(curr, base) if j_full >= 4 else None
            if level_mode:
                # Ratio/percentage metric: show the absolute level; shade by the
                # YoY direction of that level (YoY% of a ratio isn't meaningful).
                bg = heat_color(pct)
                tb.append(
                    f'<td class="cv2-matrix-cell" style="{bg}">{_fmt_level(curr, row.unit)}</td>'
                )
                continue
            noisy = is_noisy(pct, base, latest_val)
            cls = "cv2-matrix-cell cv2-matrix-noisy" if noisy else "cv2-matrix-cell"
            bg = "" if noisy else heat_color(pct)
            tb.append(f'<td class="{cls}" style="{bg}">{fmt_pct(pct, 1)}</td>')
        # Trailing columns: CAGR for flows; absolute pp/bps change for ratios.
        for q in cagr_periods:
            base = row.levels[-1 - q] if n_total > q else None
            if level_mode:
                delta = (
                    row.levels[-1] - base
                    if (base is not None and row.levels[-1] is not None)
                    else None
                )
                bg = heat_color(delta)
                tb.append(
                    f'<td class="cv2-matrix-cagr-cell" style="{bg}">{_fmt_level_delta(delta, row.unit)}</td>'
                )
                continue
            if n_total > q:
                pct = cagr(row.levels[-1], base, q / 4)
                noisy = is_noisy(pct, base, latest_val)
            else:
                pct = None
                noisy = False
            cls = "cv2-matrix-cagr-cell cv2-matrix-noisy" if noisy else "cv2-matrix-cagr-cell"
            bg = "" if noisy else heat_color(pct)
            tb.append(f'<td class="{cls}" style="{bg}">{fmt_pct(pct, 1)}</td>')
        tb.append("</tr>")
    tb.append("</tbody>")
    _ = latest  # placeholder; per-row latest is what gates noisiness

    return (
        f'<div class="cv2-matrix-wrap"><div class="cv2-matrix-title">{html.escape(title)}</div>'
        f'<table class="cv2-matrix">{"".join(th)}{"".join(tb)}</table>'
        f'<div class="cv2-matrix-footnote">Ratio/percentage rows show the absolute '
        f"level (trailing columns = pp/bps change); dollar/flow rows show YoY% "
        f"(trailing columns = CAGR). Gray italic cells mark low-base noise "
        f"(|YoY| &gt; 200% or base &lt; 15% of latest) — directionally meaningful, "
        f"numerically unstable.</div></div>"
    )


# ----- QoQ $-delta bars (MBI page-6 style) -----

def qoq_delta_bars(
    levels: list[float | None],
    labels: list[str],
    title: str,
    width: int = 720,
    height: int = 240,
) -> str:
    """Bars showing $ added each quarter QoQ. The 'incremental investment' view."""
    deltas: list[float | None] = [None]
    for i in range(1, len(levels)):
        prev, curr = levels[i - 1], levels[i]
        deltas.append((curr - prev) if (prev is not None and curr is not None) else None)
    return bar_chart(BarSpec(
        values=deltas,
        labels=labels,
        title=title,
        width=width,
        height=height,
        value_fmt="dollar",
        signed_color=True,
    ))


# ----- Empty/error -----

def _empty_chart(title: str, width: int, height: int) -> str:
    return (
        f'<svg class="cv2-chart" width="{width}" height="{height}">'
        f'<text x="{width / 2}" y="{height / 2}" class="cv2-empty-text" text-anchor="middle">'
        f'No data for {html.escape(title)}</text></svg>'
    )


# ----- Shared CSS -----

CSS = """
.cv2-chart { display: block; max-width: 100%; height: auto; font-family: 'Inter', -apple-system, sans-serif; }
.cv2-title { font-size: 12px; font-weight: 600; fill: #1a1f2e; }
.cv2-axis { font-size: 10px; fill: #67737d; font-family: 'JetBrains Mono', Consolas, monospace; }
.cv2-grid { stroke: #e3e7eb; stroke-width: 1; }
.cv2-grid-zero { stroke: #67737d; stroke-width: 1; stroke-dasharray: 2 2; }
.cv2-bar-label { font-size: 9px; fill: #1a1f2e; font-family: 'JetBrains Mono', Consolas, monospace; font-weight: 500; }
.cv2-line-end { font-size: 10px; font-weight: 600; font-family: 'JetBrains Mono', Consolas, monospace; }
.cv2-legend { font-size: 10.5px; fill: #1a1f2e; }
.cv2-band-label { font-size: 10px; font-family: 'Inter', -apple-system, sans-serif; font-weight: 500; }
.cv2-empty-text { font-size: 12px; fill: #67737d; }
.cv2-pair { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; margin: 8px 0; }
.cv2-matrix-wrap { margin: 14px 0; overflow-x: auto; }
.cv2-matrix-title { font-size: 13px; font-weight: 600; color: #1a1f2e; margin-bottom: 6px; }
.cv2-matrix { border-collapse: collapse; font-size: 11px; font-family: 'JetBrains Mono', Consolas, monospace; width: 100%; }
.cv2-matrix th, .cv2-matrix td { padding: 4px 6px; border: 1px solid #e3e7eb; text-align: right; white-space: nowrap; }
.cv2-matrix-label { text-align: left !important; font-weight: 600; color: #1a1f2e; background: #f7f9fb; position: sticky; left: 0; }
.cv2-matrix-q, .cv2-matrix-cagr { font-weight: 600; color: #1a1f2e; background: #f7f9fb; }
.cv2-matrix-cagr, .cv2-matrix-cagr-cell { border-left: 2px solid #1a1f2e !important; }
.cv2-matrix-noisy { color: #98a4af !important; font-style: italic; background: #fafbfc !important; }
.cv2-matrix-footnote { font-size: 11px; color: #67737d; margin-top: 6px; font-style: italic; }
.cv2-matrix-def-mark { cursor: help; opacity: 0.6; }
.cv2-matrix-def-mark:hover { opacity: 1; }
.chart-empty { font-size: 12px; color: var(--muted); padding: 8px 12px; background: var(--subheader-bg); border-radius: 4px; margin: 8px 0; }
.chart-grid-1col { display: grid; grid-template-columns: 1fr; gap: 16px; margin: 12px 0; }
.chart-cell { width: 100%; }
"""
