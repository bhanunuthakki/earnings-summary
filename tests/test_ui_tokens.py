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
    page_title,
    palette_css,
)


def test_light_and_dark_define_the_same_token_set() -> None:
    assert set(PALETTE_LIGHT) == set(PALETTE_DARK)


def test_white_overrides_are_a_subset_of_light() -> None:
    assert set(PALETTE_WHITE_OVERRIDES) <= set(PALETTE_LIGHT)


def test_semantic_unification() -> None:
    """Green=good / red=bad in BOTH themes; ok==pos and bad==neg; the accent
    is reserved for interactive elements (must differ from ok)."""
    for palette in (PALETTE_LIGHT, PALETTE_DARK):
        assert palette["ok"] == palette["pos"]
        assert palette["bad"] == palette["neg"]
        assert palette["accent"] != palette["ok"]
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


def test_type_scale_is_the_six_step_importance_ladder() -> None:
    """The semantic scale is a public contract for every renderer (and the
    queued UI sessions building on this one): six steps, strictly descending,
    named by importance — display > title > section > body > caption > micro."""
    order = ["fs-display", "fs-title", "fs-section", "fs-body", "fs-caption", "fs-micro"]
    assert list(TYPE_SCALE) == order
    sizes = [float(TYPE_SCALE[k].removesuffix("px")) for k in order]
    assert sizes == sorted(sizes, reverse=True)
    assert len(set(sizes)) == 6


def test_spacing_scale_ascends() -> None:
    steps = [float(v.removesuffix("px")) for v in SPACING_SCALE.values()]
    assert steps == sorted(steps)


def test_chrome_tokens_pin_the_one_radius_and_standard_transition() -> None:
    assert CHROME_TOKENS["radius"] == "8px"
    assert CHROME_TOKENS["radius-full"] == "999px"
    assert CHROME_TOKENS["transition"] == "150ms ease"


def test_scale_tokens_ride_along_in_both_palette_css_modes() -> None:
    for mode in ("paper", "dark"):
        root = palette_css(mode).split("}")[0]
        for name in (*TYPE_SCALE, *SPACING_SCALE, *CHROME_TOKENS):
            assert f"--{name}:" in root, f"--{name} missing from palette_css({mode!r}) :root"


def test_accent_contrast_is_ink_on_accent_per_theme() -> None:
    """UI polish v3: --accent-contrast is the only ink allowed on accent fill.
    Dark accent is a LIGHT blue, so its contrast ink is near-black; light
    accent is a dark blue, so its contrast ink is white."""
    assert PALETTE_DARK["accent-contrast"] == "#0c0d10"
    assert PALETTE_LIGHT["accent-contrast"] == "#ffffff"


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


def test_page_title_joins_with_middle_dots_and_drops_empties() -> None:
    assert page_title("NU", "workspace") == "NU · workspace"
    assert page_title("Portfolio", "", "  ", "command center") == "Portfolio · command center"
    assert page_title() == ""
