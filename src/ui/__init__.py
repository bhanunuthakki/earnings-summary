"""src/ui/ — shared design-language primitives (master build P0.1).

One source of truth for the palette, fonts, chart series, favicon, and the
browser-title convention, consumed by every HTML-rendering surface
(workspace report, command-center shell, analytical dashboard, digest/feed,
legacy report). Surfaces keep their own LAYOUT tokens (spacing, density) —
only color/typography/branding are shared, because those are what drifted.
"""

from ui.tokens import (
    CHART_SERIES,
    FAVICON_LINK,
    PALETTE_DARK,
    PALETTE_LIGHT,
    page_title,
    palette_css,
)

__all__ = [
    "CHART_SERIES",
    "FAVICON_LINK",
    "PALETTE_DARK",
    "PALETTE_LIGHT",
    "page_title",
    "palette_css",
]
