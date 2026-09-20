"""Drift test for the Streamlit sandbox kit: token + control parity.

Pins the two parities the settled sandbox scope requires:

1. Theme parity — the kit's injected layer is EXACTLY ``palette_css("dark")``
   + ``controls_css("dark")`` (the same layers, in the same order, every
   Flask page composes), and the committed generated mirrors under
   ``explore-sandbox/`` match the authorities. The design-sync gate checks
   the mirrors in CI; this test also catches drift for contributors who run
   pytest but not the design gate.
2. Control parity — the kit's helpers ARE ``src/ui/controls.py``'s functions
   (re-export identity), so the sandbox can never fork control semantics
   even by accident.
"""

from __future__ import annotations

import sys
import tomllib
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.path.insert(0, str(PROJECT_ROOT / "explore-sandbox"))

import sandbox_kit  # noqa: E402

import ui.controls as controls  # noqa: E402
import ui.tokens as tokens  # noqa: E402
from scripts.gen_streamlit_theme import (  # noqa: E402
    CONFIG_OUTPUT,
    CONTROLS_CSS_OUTPUT,
    CSS_OUTPUT,
    render_config,
    render_controls_css,
    render_css,
)


def test_theme_css_is_exactly_the_two_authority_layers() -> None:
    assert sandbox_kit.theme_css() == tokens.palette_css("dark") + "\n" + controls.controls_css(
        "dark"
    )


def test_control_helpers_are_the_authoritys_functions() -> None:
    for name in sandbox_kit.__all__:
        if name == "theme_css":
            continue
        assert getattr(sandbox_kit, name) is getattr(controls, name), (
            f"sandbox_kit.{name} is not src/ui/controls.py's function — the kit forked a control"
        )


def test_committed_theme_mirrors_match_the_authorities() -> None:
    for path, render in (
        (CSS_OUTPUT, render_css),
        (CONTROLS_CSS_OUTPUT, render_controls_css),
        (CONFIG_OUTPUT, render_config),
    ):
        assert path.exists(), f"{path.relative_to(PROJECT_ROOT)} is missing — regenerate the theme"
        assert path.read_text(encoding="utf-8") == render(), (
            f"{path.relative_to(PROJECT_ROOT)} drifted — run scripts/gen_streamlit_theme.py"
        )


def test_streamlit_config_theme_maps_the_dark_palette() -> None:
    loaded = tomllib.loads(CONFIG_OUTPUT.read_text(encoding="utf-8"))
    theme = loaded.get("theme")
    assert isinstance(theme, dict)
    assert theme.get("base") == "dark"
    assert theme.get("primaryColor") == tokens.PALETTE_DARK["accent"]
    assert theme.get("backgroundColor") == tokens.PALETTE_DARK["bg"]
    assert theme.get("secondaryBackgroundColor") == tokens.PALETTE_DARK["paper"]
    assert theme.get("textColor") == tokens.PALETTE_DARK["fg"]
