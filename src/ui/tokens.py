"""Design tokens — the single source of color, type, and branding.

Why this exists (master build P0.1): the app grew five independent CSS
token blocks. Three different accent blues coexisted, "good" rendered BLUE
in the workspace but GREEN on the dashboard surfaces, panel darks diverged
by a shade (#14161b vs #16171a), and no surface had a favicon. This module
owns the palette; surfaces compose their ``<style>`` from
:func:`palette_css` and keep only their layout/density tokens local.

Canonical semantic decision (2026-06-10): **green = good / red = bad**
everywhere (the financial convention, already used by the shell and the
analytical dashboard); the blue accent is reserved for interactive elements
(links, active tabs, selection). This flips the workspace's blue ``--ok``/
``--pos`` — including the YoY heatmaps — to green/red. If the matrices read
worse, revert is a two-line palette change here, nowhere else.

The variable NAMES are the workspace renderer's vocabulary (the most
complete set); other surfaces alias their legacy names onto these in a
one-line block rather than re-defining values.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Palettes. Keys become CSS custom properties: "bg" -> --bg.
# ---------------------------------------------------------------------------

# Light ("paper") — the workspace report's default reading theme.
PALETTE_LIGHT: dict[str, str] = {
    "bg": "#fafaf7",
    "surface": "#ffffff",
    "paper": "#f4f3ef",
    "fg": "#0c0d10",
    "fg-soft": "#2a2c33",
    "muted": "#6c6f78",
    "muted-2": "#9a9da6",
    "border": "#e4e3dd",
    "border-2": "#d1cfc7",
    "hairline": "#ecebe5",
    "accent": "#1d4ed8",
    "accent-soft": "#eef2ff",
    "ok": "#15803d",
    "warn": "#b97c00",
    "bad": "#b91c1c",
    "pos": "#15803d",
    "neg": "#b91c1c",
    "neu": "#6c6f78",
    "seg-1": "#0c0d10",
    "seg-2": "#43464e",
    "seg-3": "#7a7d86",
    "seg-4": "#b6b8be",
    "seg-5": "#dcdcd7",
    "tone-pos": "#eef2ff",
    "tone-neu": "#f4f3ef",
    "tone-opt": "#fff8e6",
    "tone-neg": "#fdf2f2",
}

# "white" is the light palette with a brighter page background.
PALETTE_WHITE_OVERRIDES: dict[str, str] = {
    "bg": "#ffffff",
    "paper": "#fafaf7",
    "hairline": "#efeeea",
}

# Dark — the dashboard surfaces' only theme; the workspace's opt-in theme.
PALETTE_DARK: dict[str, str] = {
    "bg": "#0c0d10",
    "surface": "#14161b",
    "paper": "#1a1d23",
    "fg": "#f4f3ef",
    "fg-soft": "#d5d6d2",
    "muted": "#888b94",
    "muted-2": "#5b5e66",
    "border": "#2a2d35",
    "border-2": "#383b44",
    "hairline": "#1f2127",
    "accent": "#8aa8ff",
    "accent-soft": "#1c2138",
    "ok": "#4ade80",
    "warn": "#f5c66a",
    "bad": "#f08a8a",
    "pos": "#4ade80",
    "neg": "#f08a8a",
    "neu": "#888b94",
    "seg-1": "#f4f3ef",
    "seg-2": "#b6b8be",
    "seg-3": "#7a7d86",
    "seg-4": "#43464e",
    "seg-5": "#25282f",
    "tone-pos": "#1a2238",
    "tone-neu": "#1a1d23",
    "tone-opt": "#2b2418",
    "tone-neg": "#2b1a1a",
}

FONT_TOKENS: dict[str, str] = {
    "sans": "'Inter', -apple-system, BlinkMacSystemFont, system-ui, sans-serif",
    "serif": "'Source Serif 4', 'Source Serif Pro', Georgia, serif",
    "mono": "'JetBrains Mono', ui-monospace, SFMono-Regular, Menlo, monospace",
}

# Categorical chart series (Okabe-Ito, colorblind-safe). Values unchanged
# from charts_v2's historical hardcode — exposed here so the next palette
# decision happens in one place.
CHART_SERIES: tuple[str, ...] = (
    "#0173b2",  # blue
    "#de8f05",  # orange
    "#029e73",  # green
    "#cc78bc",  # purple
    "#ca9161",  # brown
    "#949494",  # gray
)


def _vars(palette: dict[str, str], indent: str = "  ") -> str:
    return "\n".join(f"{indent}--{name}: {value};" for name, value in palette.items())


def palette_css(default: str = "paper") -> str:
    """The shared palette as CSS custom-property blocks.

    ``default="paper"`` emits the workspace contract: light ``:root`` plus
    ``[data-theme="white"]`` and ``[data-theme="dark"]`` overrides.
    ``default="dark"`` emits a dark-only ``:root`` for the dashboard
    surfaces that have no theme switcher. Font tokens ride along in
    ``:root`` either way.
    """
    if default == "dark":
        return ":root {\n" + _vars({**PALETTE_DARK, **FONT_TOKENS}) + "\n}\n"
    if default != "paper":
        raise ValueError(f"default must be 'paper' or 'dark', got {default!r}")
    white = {**PALETTE_LIGHT, **PALETTE_WHITE_OVERRIDES}
    white_delta = {k: v for k, v in white.items() if PALETTE_LIGHT.get(k) != v}
    return (
        ":root {\n"
        + _vars({**PALETTE_LIGHT, **FONT_TOKENS})
        + "\n}\n\n"
        + ':root[data-theme="white"] {\n'
        + _vars(white_delta)
        + "\n}\n\n"
        + ':root[data-theme="dark"] {\n'
        + _vars(PALETTE_DARK)
        + "\n}\n"
    )


# ---------------------------------------------------------------------------
# Branding: favicon + title convention.
# ---------------------------------------------------------------------------

# Sparkline-on-dark mark, inlined as a data URI so every surface stays a
# self-contained HTML file (no asset fetches — the workspace opens file://).
_FAVICON_SVG = (
    "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'>"
    "<rect width='32' height='32' rx='7' fill='%230c0d10'/>"
    "<path d='M6 22l6-7 4.5 3.5L24 9' stroke='%234ade80' stroke-width='2.6' "
    "fill='none' stroke-linecap='round' stroke-linejoin='round'/>"
    "<circle cx='25.5' cy='8' r='2.2' fill='%238aa8ff'/>"
    "</svg>"
)

FAVICON_LINK: str = (
    f'<link rel="icon" type="image/svg+xml" href="data:image/svg+xml,{_FAVICON_SVG}">'
)


def page_title(*parts: str) -> str:
    """Browser-tab title: non-empty parts joined with " · " (house style:
    context first, subject last — e.g. ``page_title("NU", "workspace")``)."""
    return " · ".join(p.strip() for p in parts if p and p.strip())
