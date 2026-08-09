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

# Light ("paper") — the workspace report's opt-in reading theme. The report
# itself is hardcoded to dark (see workspace_html.py's _document — "no theme
# switcher, no chrome", a deliberate 2026-07-18 decision, kept on purpose);
# this palette exists for whatever surface opts into it, not as that
# surface's default.
PALETTE_LIGHT: dict[str, str] = {
    "bg": "#fafaf7",
    "surface": "#ffffff",
    "paper": "#f4f3ef",
    "fg": "#0d0d0c",
    "fg-soft": "#2d2a23",
    # The one de-emphasis gray. #6f6b60 clears WCAG AA against this bg for the
    # real body text it backs (td.muted, crumb-sep, news-date): 5.09:1 here and
    # 5.32:1 on the white theme's #ffffff. The old second gray (--muted-2)
    # collapsed here (design-sync 2026-07-19): two grays of de-emphasis was one
    # too many. Warmed 2026-07-25 alongside the dark palette — the cool pair
    # measured 4.80:1 / 5.02:1, so the warming also bought a little headroom.
    "muted": "#6f6b60",
    "border": "#e4e3dd",
    "border-2": "#d1cfc7",
    "hairline": "#ecebe5",
    "accent": "#1d4ed8",
    "accent-soft": "#eef2ff",
    # Text/glyph color ON an accent-filled surface (k-btn-primary, badges).
    "accent-contrast": "#ffffff",
    # Status: green=good, red=bad, amber=watch — the one semantic tone family.
    # (--pos/--neg/--neu folded into --ok/--bad/--muted, design-sync 2026-07-19:
    # they were value-identical duplicates.)
    "ok": "#15803d",
    "warn": "#b97c00",
    "bad": "#b91c1c",
    # The editorial mark (2026-07-25). A muted ochre reserved for DOCUMENT
    # furniture — section labels, the masthead rule, footnote markers, margin-
    # note keylines. It is deliberately duller and browner than --warn so a
    # section label can never be misread as a caution state, and it never
    # fills a control (that is --accent's job).
    #
    # --mark carries text: 5.62:1 here, 6.32:1 on the dark ground — AA-body on
    # both. --mark-soft is a KEYLINE ONLY (2.05:1 light / 2.88:1 dark): it is
    # legal on a 1px rule or border and never on type. The two are separate
    # tokens precisely so that constraint is expressible.
    "mark": "#8a5a28",
    "mark-soft": "#c8ad8a",
    "series-qqq": "#5b8def",
    "seg-1": "#0d0d0c",
    "seg-2": "#443f34",
    "seg-3": "#7d7869",
    "seg-4": "#b5b0a4",
    "seg-5": "#dcdcd7",
    # One popover/menu elevation per theme (combobox lists, peeks, palettes).
    "shadow-pop": "0 12px 32px rgba(15, 15, 20, 0.18)",
    # The one modal scrim wash (CCOverlay / .k-scrim). A neutral black veil, not
    # a palette color — tokenized so surfaces never freehand `rgba(0,0,0,.5)`.
    "scrim": "rgba(0, 0, 0, 0.5)",
    "glass-bg": "rgba(255, 255, 255, 0.75)",
    "glass-border": "rgba(0, 0, 0, 0.06)",
}

# "white" is the light palette with a brighter page background.
PALETTE_WHITE_OVERRIDES: dict[str, str] = {
    "bg": "#ffffff",
    "paper": "#fafaf7",
    "hairline": "#efeeea",
}

# Dark — the dashboard surfaces' only theme; the workspace's opt-in theme.
#
# Warmed 2026-07-25 & elevated 2026-08-07 ("calm desk, deep drawers" & quiet Work OS):
# Ultra-deep warm obsidian ground (#090a0c) with subtle neutral temperature,
# glassmorphism surface tokens, and crisp hairline borders.
PALETTE_DARK: dict[str, str] = {
    "bg": "#090a0c",
    "surface": "#121316",
    "paper": "#18191d",
    "fg": "#f4f3ef",
    "fg-soft": "#c5c2b8",
    # The one de-emphasis gray (see PALETTE_LIGHT).
    "muted": "#7c7e87",
    "border": "#22242a",
    "border-2": "#31343e",
    "hairline": "#18191d",
    "accent": "#8aa8ff",
    "accent-soft": "#181f38",
    # Dark accent is light — ink on it must be near-black, not white.
    "accent-contrast": "#090a0c",
    "ok": "#4ade80",
    "warn": "#f5c66a",
    "bad": "#f08a8a",
    # See PALETTE_LIGHT. On dark the mark is lighter than the ground; the soft
    # variant is the keyline shade, not a text color.
    "mark": "#b08d5f",
    "mark-soft": "#705737",
    "series-qqq": "#5b8def",
    "seg-1": "#f4f3ef",
    "seg-2": "#b5b0a4",
    "seg-3": "#7d7869",
    "seg-4": "#443f34",
    "seg-5": "#262219",
    "shadow-pop": "0 16px 48px -8px rgba(0, 0, 0, 0.7)",
    "scrim": "rgba(0, 0, 0, 0.65)",
    "glass-bg": "rgba(18, 19, 22, 0.75)",
    "glass-border": "rgba(255, 255, 255, 0.05)",
}

FONT_TOKENS: dict[str, str] = {
    "sans": "'Inter', -apple-system, BlinkMacSystemFont, system-ui, sans-serif",
    "serif": "'Source Serif 4', 'Source Serif Pro', Georgia, serif",
    "mono": "'JetBrains Mono', ui-monospace, SFMono-Regular, Menlo, monospace",
}

# ---------------------------------------------------------------------------
# Semantic type scale (compact Work OS hierarchy, v6, 2026-08-09).
#
# There are exactly four VISIBLE sizes: display 20, title 15, body 13, and
# caption 11. Older semantic names remain aliases because they are a public
# renderer contract, but aliases may not introduce another visible step.
TYPE_SCALE: dict[str, str] = {
    "fs-display": "20px",
    "fs-title": "15px",
    "fs-body": "13px",
    "fs-caption": "11px",
    "fs-stat": "var(--fs-display)",
    "fs-header-title": "var(--fs-title)",
    "fs-serif-body": "var(--fs-body)",
    "fs-mono-sm": "var(--fs-caption)",
    "fs-micro": "var(--fs-caption)",
    "fs-nano": "var(--fs-caption)",
}

# Spacing scale — gaps, paddings, and margins snap to these steps so density
# differences between surfaces stay deliberate (a surface may still define
# local layout tokens, but their VALUES should come from this ladder).
SPACING_SCALE: dict[str, str] = {
    "sp-half": "2px",
    "sp-1": "4px",
    "sp-2": "8px",
    "sp-3": "12px",
    "sp-4": "16px",
    "sp-5": "24px",
    "sp-6": "32px",
}

# Chrome & Work OS tokens: corner radii, layout dimensions, blur filters, and motion timing.
CHROME_TOKENS: dict[str, str] = {
    "sidebar-width": "240px",
    "sidebar-collapsed-width": "72px",
    "main-max-width": "1240px",
    "drawer-width": "540px",
    "header-height": "48px",
    "nav-item-height": "32px",
    "icon-size": "16px",
    "icon-button-size": "32px",
    # Platform accessibility floors are not content-type or spacing steps.
    # They may intentionally sit outside the four-step visible type hierarchy.
    "mobile-control-font-size": "16px",
    "touch-target-size": "44px",
    "toast-offset-bottom": "28px",
    "toast-offset-right": "28px",
    "dot-size": "6px",
    "ticker-width": "34px",
    "bar-track-height": "8px",
    "grid-card-lg": "340px",
    "grid-card-md": "280px",
    "grid-card-sm": "240px",
    "bw-thin": "1px",
    "bw-thick": "2px",
    "lift-sm": "-1px",
    "lift-md": "-2px",
    "toast-hide-y": "100px",
    "blur-sm": "6px",
    "blur-md": "16px",
    "blur-lg": "24px",
    "radius-sm": "2px",
    "radius-md": "3px",
    "radius": "8px",
    "radius-card": "10px",
    "radius-drawer": "14px",
    "radius-full": "999px",
    "transition": "150ms ease",
    "transition-fluid": "250ms cubic-bezier(0.16, 1, 0.3, 1)",
    "shadow-btn-primary": "0 2px 10px rgba(138, 168, 255, 0.22)",
    "shadow-btn-primary-hover": "0 4px 16px rgba(138, 168, 255, 0.38)",
    "shadow-card": "0 4px 20px -2px rgba(0, 0, 0, 0.45), 0 0 0 1px rgba(255, 255, 255, 0.05)",
    "shadow-card-hover": "0 12px 28px -6px rgba(0, 0, 0, 0.55), 0 0 0 1px rgba(255, 255, 255, 0.1)",
    "shadow-drawer": "-16px 0 48px rgba(0, 0, 0, 0.75)",
}

# Rides along inside every palette_css() :root block.
_SCALE_TOKENS: dict[str, str] = {**TYPE_SCALE, **SPACING_SCALE, **CHROME_TOKENS}

# Token-kind annotations (design-sync 2026-07-19): a trailing ``/* @kind X */``
# on the tokens whose ROLE isn't inferable from a color/px value — the three
# font families and the motion timing. The design-sync converter reads these to
# categorize non-color tokens; harmless CSS comments everywhere else. Colors and
# scale-px tokens are self-describing and carry no annotation. --k-chevron (a
# theme-dependent glyph, defined in controls.py) is annotated there.
_TOKEN_KINDS: dict[str, str] = {
    "sans": "font",
    "serif": "font",
    "mono": "font",
    "transition": "other",
}

# ---------------------------------------------------------------------------
# Scale introspection — the sanctioned-literal sets the opt-out conformance
# guard (tests/test_ui_controls.py, design_language §2/§7) checks rendered CSS
# against. Derived from the dicts above so the guard can NEVER drift from the
# values: add a step to TYPE_SCALE and it is instantly a legal size. These are
# data, not a runtime registry — there is no DB table or mutable state here.
# ---------------------------------------------------------------------------

#: Every on-scale ``font-size`` px literal (e.g. ``"13px"``). A px font-size
#: outside this set is "off-scale" and denied (the workspace reading/display
#: ramp and the ``.src-chip`` micro-mark are the only sanctioned escapes — see
#: design_language §1).
TYPE_SCALE_PX: frozenset[str] = frozenset(
    value for value in TYPE_SCALE.values() if value.endswith("px")
)

#: The only px ``border-radius`` literals the system allows: ``--radius`` (8px,
#: every rectangular box) and ``--radius-full`` (999px, pills/dots). 3/4/5/6px
#: corners are drift (design_language §3). Surfaces should write
#: ``var(--radius)`` / ``var(--radius-full)``; the px values are what those
#: resolve to, kept here so the guard accepts an inlined equivalent.
RADIUS_PX: frozenset[str] = frozenset(
    v for k, v in CHROME_TOKENS.items() if k.startswith("radius") and v.endswith("px")
)

#: The sanctioned ``font-family`` *values* — exactly the three FONT_TOKENS,
#: referenced as CSS variables. There are three font ROLES (sans UI / serif
#: reading-prose / mono annotation) and no fourth: any other family literal in
#: a ``font-family`` declaration is the owner's "too many fonts" drift.
FONT_FAMILY_TOKENS: frozenset[str] = frozenset(f"var(--{name})" for name in FONT_TOKENS)

#: Generic fallback keywords that may stand beside (or instead of) a font
#: token in a ``font-family`` value without counting as a new family.
FONT_FAMILY_KEYWORDS: frozenset[str] = frozenset(
    {"monospace", "sans-serif", "serif", "system-ui", "inherit", "initial", "unset"}
)

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
    def _line(name: str, value: str) -> str:
        kind = _TOKEN_KINDS.get(name)
        suffix = f" /* @kind {kind} */" if kind else ""
        return f"{indent}--{name}: {value};{suffix}"

    return "\n".join(_line(name, value) for name, value in palette.items())


def palette_css(default: str = "paper") -> str:
    """The shared palette as CSS custom-property blocks.

    ``default="paper"`` emits the workspace contract: light ``:root`` plus
    ``[data-theme="white"]`` and ``[data-theme="dark"]`` overrides.
    ``default="dark"`` emits a dark-only ``:root`` for the dashboard
    surfaces that have no theme switcher. Font tokens and the semantic
    type/spacing/chrome scale ride along in ``:root`` either way.
    """
    if default == "dark":
        return ":root {\n" + _vars({**PALETTE_DARK, **FONT_TOKENS, **_SCALE_TOKENS}) + "\n}\n"
    if default != "paper":
        raise ValueError(f"default must be 'paper' or 'dark', got {default!r}")
    white = {**PALETTE_LIGHT, **PALETTE_WHITE_OVERRIDES}
    white_delta = {k: v for k, v in white.items() if PALETTE_LIGHT.get(k) != v}
    return (
        ":root {\n"
        + _vars({**PALETTE_LIGHT, **FONT_TOKENS, **_SCALE_TOKENS})
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
    "<rect width='32' height='32' rx='7' fill='%23090a0c'/>"
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
