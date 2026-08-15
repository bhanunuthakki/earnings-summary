"""Canonical number/date formatting for the render layer (master build P0.1).

Single source of truth so workspace HTML, markdown, and dashboard panels render
the same value identically: compact money (135000000000 -> "135.0B"), percent
vs percentage-point deltas, calendar dates, and relative time. Consolidates the
previously duplicated per-renderer `_fmt_compact_usd` helpers (html.py,
markdown.py) and the dashboard's ad-hoc relative-time f-strings.

Every function returns PLAIN text — no HTML. Renderers add their own markup
(spans, title attributes) around these strings.
"""

from __future__ import annotations

from datetime import UTC, datetime

from report.render_clock import render_now


def fmt_compact_usd(v: float) -> str:
    """Compact magnitude: ``135.0B`` / ``45M`` / ``678K`` / ``1,234``.

    No currency symbol — callers prepend ``$`` where appropriate (preserves the
    behaviour of the per-renderer helpers this consolidates). Apply ONLY to
    monetary magnitudes — never to percentages, multiples/ratios, share counts,
    per-share values, basis points, or dates.
    """
    if abs(v) >= 1e9:
        return f"{v / 1e9:.1f}B"
    if abs(v) >= 1e6:
        return f"{v / 1e6:.0f}M"
    if abs(v) >= 1e3:
        return f"{v / 1e3:.0f}K"
    return f"{v:,.0f}"


def fmt_pct(value: float, *, decimals: int = 1, signed: bool = False) -> str:
    """Percent display of a value ALREADY in percent points: ``12.34`` -> ``12.3%``.

    Callers holding a ratio (0.1234) multiply by 100 first — the repo stores
    percent KPIs and FMP day-change as percent points, so that is the contract
    here. ``signed=True`` renders an explicit ``+`` on positive values (deltas).
    """
    sign = "+" if signed else ""
    return f"{value:{sign}.{decimals}f}%"


def fmt_pp(value: float, *, decimals: int = 1) -> str:
    """Percentage-POINT delta: ``-1.27`` -> ``-1.3pp``. Always signed — a pp
    figure is a movement, never a level (levels use :func:`fmt_pct`)."""
    return f"{value:+.{decimals}f}pp"


def fmt_date(iso: str, *, include_year: bool = True) -> str:
    """``2026-08-13`` -> ``Aug 13, 2026`` (or ``Aug 13``). Tolerant: a string
    that doesn't parse as an ISO date/datetime is returned unchanged."""
    try:
        d = datetime.fromisoformat(iso[:19] if len(iso) >= 19 else iso[:10])
    except ValueError:
        return iso
    label = f"{d.strftime('%b')} {d.day}"
    return f"{label}, {d.year}" if include_year else label


def fmt_reltime(iso: str, *, now: datetime | None = None) -> str:
    """ISO timestamp -> ``3h ago`` / ``5d ago`` / ``in 3d`` / ``just now``.

    Naive timestamps are read as UTC (the repo's storage convention). Tiers
    match the dashboard's historical rendering: minutes < 1h, hours < 1d,
    days < 30d, months beyond — mirrored for future instants (``in 2mo``).
    Unparseable input is returned unchanged.
    """
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        return iso
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    ref = now or render_now()
    if ref.tzinfo is None:
        ref = ref.replace(tzinfo=UTC)
    seconds = int((ref - dt).total_seconds())
    future = seconds < 0
    seconds = abs(seconds)
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        span = f"{seconds // 60}m"
    elif seconds < 86400:
        span = f"{seconds // 3600}h"
    elif seconds < 86400 * 30:
        span = f"{seconds // 86400}d"
    else:
        span = f"{seconds // (86400 * 30)}mo"
    return f"in {span}" if future else f"{span} ago"
