"""Canonical compact number formatting for the report render layer.

Single source of truth so workspace HTML, markdown, and panels render large
monetary figures consistently (e.g. 135000000000 -> "135.0B") instead of raw
floats. Consolidates the previously duplicated per-renderer `_fmt_compact_usd`
helpers (html.py, markdown.py).

Apply ONLY to monetary magnitudes — never to percentages, multiples/ratios,
share counts, per-share values, basis points, or dates.
"""

from __future__ import annotations


def fmt_compact_usd(v: float) -> str:
    """Compact magnitude: ``135.0B`` / ``45M`` / ``678K`` / ``1,234``.

    No currency symbol — callers prepend ``$`` where appropriate (preserves the
    behaviour of the per-renderer helpers this consolidates).
    """
    if abs(v) >= 1e9:
        return f"{v / 1e9:.1f}B"
    if abs(v) >= 1e6:
        return f"{v / 1e6:.0f}M"
    if abs(v) >= 1e3:
        return f"{v / 1e3:.0f}K"
    return f"{v:,.0f}"
