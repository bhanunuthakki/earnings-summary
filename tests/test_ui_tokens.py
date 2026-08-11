"""Tests for the shared design tokens (master build P0.1).

These pin the contracts every HTML surface now depends on: light/dark
palettes define the SAME token set, semantics are unified (green=good,
red=bad, accent reserved for interactive), the generated CSS blocks carry
the theme markers each surface's <style> composition expects, and the
favicon stays brace-free (several surfaces splice it into ``str.format``
templates where a stray ``{`` would crash page rendering).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from ui.tokens import (  # noqa: E402
    CHART_SERIES,
    CHROME_TOKENS,
    FAVICON_LINK,
    FONT_TOKENS,
    PALETTE_DARK,
    PALETTE_LIGHT,
    PALETTE_WHITE_OVERRIDES,
    SPACING_SCALE,
    TYPE_SCALE,
    TYPE_SCALE_PX,
    page_title,
    palette_css,
)


def test_light_and_dark_define_the_same_token_set() -> None:
    assert set(PALETTE_LIGHT) == set(PALETTE_DARK)


def test_white_overrides_are_a_subset_of_light() -> None:
    assert set(PALETTE_WHITE_OVERRIDES) <= set(PALETTE_LIGHT)


def test_semantic_unification() -> None:
    """Green=good / red=bad in BOTH themes; the accent is reserved for
    interactive elements (must differ from ok). The old --pos/--neg/--neu
    status aliases and the second gray --muted-2 were folded into
    --ok/--bad/--muted (design-sync 2026-07-19), so they must be GONE."""
    for palette in (PALETTE_LIGHT, PALETTE_DARK):
        assert palette["accent"] != palette["ok"]
        assert "series-qqq" in palette
        for gone in ("pos", "neg", "neu", "muted-2"):
            assert gone not in palette
    assert PALETTE_DARK["bg"] == "#090a0c"
    assert PALETTE_DARK["ok"] == "#4ade80"
    assert PALETTE_LIGHT["ok"] == "#15803d"


def test_palette_css_paper_emits_three_theme_blocks() -> None:
    css = palette_css("paper")
    assert css.startswith(":root {")
    assert ':root[data-theme="white"]' in css
    assert ':root[data-theme="dark"]' in css
    assert "--ok: #15803d;" in css.split('[data-theme="dark"]')[0]
    assert "--ok: #4ade80;" in css.split('[data-theme="dark"]')[1]
    for font in FONT_TOKENS:
        assert f"--{font}:" in css


def test_palette_css_dark_is_a_single_root_block() -> None:
    css = palette_css("dark")
    assert css.startswith(":root {")
    assert "data-theme" not in css
    assert "--ok: #4ade80;" in css
    assert "--sans:" in css


def test_palette_css_rejects_unknown_default() -> None:
    with pytest.raises(ValueError):
        palette_css("sepia")


def test_type_scale_has_four_visual_steps_with_legacy_aliases() -> None:
    """The compact hierarchy has four visible sizes. Older semantic names
    remain public aliases so renderers can migrate without inventing another
    visual step or breaking their existing token references."""
    canonical = {
        "fs-display": "20px",
        "fs-title": "15px",
        "fs-body": "13px",
        "fs-caption": "11px",
    }
    assert {name: TYPE_SCALE[name] for name in canonical} == canonical
    assert TYPE_SCALE["fs-stat"] == "var(--fs-display)"
    assert TYPE_SCALE["fs-header-title"] == "var(--fs-title)"
    assert TYPE_SCALE["fs-serif-body"] == "var(--fs-body)"
    for name in ("fs-mono-sm", "fs-micro", "fs-nano"):
        assert TYPE_SCALE[name] == "var(--fs-caption)"
    assert frozenset(canonical.values()) == TYPE_SCALE_PX


def test_spacing_scale_ascends() -> None:
    steps = [float(v.removesuffix("px")) for v in SPACING_SCALE.values()]
    assert steps == sorted(steps)


def test_chrome_tokens_pin_the_one_radius_and_standard_transition() -> None:
    assert CHROME_TOKENS["radius"] == "8px"
    assert CHROME_TOKENS["radius-card"] == "10px"
    assert CHROME_TOKENS["radius-drawer"] == "14px"
    assert CHROME_TOKENS["radius-full"] == "999px"
    assert CHROME_TOKENS["transition"] == "150ms ease"


def test_scale_tokens_ride_along_in_both_palette_css_modes() -> None:
    for mode in ("paper", "dark"):
        root = palette_css(mode).split("}")[0]
        for name in (*TYPE_SCALE, *SPACING_SCALE, *CHROME_TOKENS):
            assert f"--{name}:" in root, f"--{name} missing from palette_css({mode!r}) :root"


def _relative_luminance(hex_color: str) -> float:
    raw = hex_color.lstrip("#")
    channels = [int(raw[i : i + 2], 16) / 255 for i in (0, 2, 4)]
    linear = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def contrast_ratio(a: str, b: str) -> float:
    """WCAG 2.1 contrast ratio between two #rrggbb colors."""
    la, lb = _relative_luminance(a), _relative_luminance(b)
    return (max(la, lb) + 0.05) / (min(la, lb) + 0.05)


def test_accent_contrast_is_ink_on_accent_per_theme() -> None:
    """UI polish v3: --accent-contrast is the only ink allowed on accent fill.

    This pins the PROPERTY, not a frozen hex: the ink must actually be legible
    on the accent fill it sits on. An earlier version asserted the literal
    ``#0c0d10``, which broke on the 2026-07-25 palette warming without saying
    anything about legibility — a literal that moves with the palette tests
    nothing. On dark the ink is the page ground (a light accent takes near-black
    ink); on light it is white.
    """
    assert PALETTE_DARK["accent-contrast"] == PALETTE_DARK["bg"]
    assert PALETTE_LIGHT["accent-contrast"] == "#ffffff"
    for palette in (PALETTE_LIGHT, PALETTE_DARK):
        ratio = contrast_ratio(palette["accent-contrast"], palette["accent"])
        assert ratio >= 4.5, (
            f"--accent-contrast {palette['accent-contrast']} on --accent "
            f"{palette['accent']} is {ratio:.2f}:1, below the 4.5:1 AA-body floor"
        )


def test_mark_carries_text_and_mark_soft_is_keyline_only() -> None:
    """The editorial mark (2026-07-25) is two tokens because it has two jobs.

    ``--mark`` sets type (section labels, footnote markers) so it must clear
    AA-body on its own ground. ``--mark-soft`` is a 1px keyline shade and is
    deliberately BELOW that floor — this test is what stops someone promoting it
    to a text color, and what stops someone "fixing" it to be readable.
    """
    for palette in (PALETTE_LIGHT, PALETTE_DARK):
        on_text = contrast_ratio(palette["mark"], palette["bg"])
        assert on_text >= 4.5, f"--mark is {on_text:.2f}:1 on --bg, below AA-body"
        keyline = contrast_ratio(palette["mark-soft"], palette["bg"])
        assert keyline < 4.5, (
            f"--mark-soft is {keyline:.2f}:1 — that is text-legible. It is a "
            "keyline token; if a rule needs to carry type, use --mark."
        )


def test_mark_is_not_the_warn_semantic() -> None:
    """A section label must never be readable as a caution state."""
    for palette in (PALETTE_LIGHT, PALETTE_DARK):
        assert palette["mark"] != palette["warn"]
        assert palette["mark-soft"] != palette["warn"]


def test_shadow_pop_defined_in_both_themes() -> None:
    for palette in (PALETTE_LIGHT, PALETTE_DARK):
        assert "shadow-pop" in palette
        assert "rgba" in palette["shadow-pop"]


def test_chart_series_is_six_unique_colors() -> None:
    assert len(CHART_SERIES) == 6
    assert len(set(CHART_SERIES)) == 6
    assert all(c.startswith("#") for c in CHART_SERIES)


def test_favicon_is_a_brace_free_data_uri_link() -> None:
    assert FAVICON_LINK.startswith('<link rel="icon"')
    assert "data:image/svg+xml," in FAVICON_LINK
    # Spliced into str.format templates — a literal brace would crash them.
    assert "{" not in FAVICON_LINK
    assert "}" not in FAVICON_LINK
    assert "%23f4f3ef" in FAVICON_LINK
    assert "circle" in FAVICON_LINK
    assert "polyline" in FAVICON_LINK
    assert "%234ade80" not in FAVICON_LINK
    assert "%238aa8ff" not in FAVICON_LINK


def test_page_title_joins_with_middle_dots_and_drops_empties() -> None:
    assert page_title("NU", "workspace") == "NU · workspace"
    assert page_title("Portfolio", "", "  ", "command center") == "Portfolio · command center"
    assert page_title() == ""
