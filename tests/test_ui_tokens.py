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
    FAVICON_LINK,
    FONT_TOKENS,
    PALETTE_DARK,
    PALETTE_LIGHT,
    PALETTE_WHITE_OVERRIDES,
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
