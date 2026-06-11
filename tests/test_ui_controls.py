"""Tests for the shared control kit (UI polish v3 — src/ui/controls.py).

These pin the contracts the design language (directives/design_language.md)
promises every surface: the element baseline kills the native select look
(appearance:none + the theme chevron + one accent focus ring), the button
hierarchy and chip kit derive from tokens (no raw hex), the canonical ticker
label renders mono-symbol + muted-truncated-name with the full name in
``title``, and the CSS survives the brace-doubled ``str.format`` splicing two
page heads use. Composition is asserted on the real page CSS constants so a
surface can't silently drop the kit.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from ui.controls import controls_css, ticker_label  # noqa: E402

# ---------------------------------------------------------------------------
# controls_css — modes
# ---------------------------------------------------------------------------


def test_dark_mode_pins_dark_scheme_and_chevron() -> None:
    css = controls_css("dark")
    assert "color-scheme: dark" in css
    assert "--k-chevron:" in css
    assert "data-theme" not in css  # single-theme surfaces get no override block


def test_paper_mode_emits_light_root_plus_dark_override() -> None:
    css = controls_css("paper")
    assert "color-scheme: light" in css
    assert ':root[data-theme="dark"]' in css
    # Both chevron inks present: light root + dark override.
    assert "%236c6f78" in css and "%23888b94" in css


def test_rejects_unknown_mode() -> None:
    with pytest.raises(ValueError):
        controls_css("sepia")


# ---------------------------------------------------------------------------
# The element baseline — the native look is dead
# ---------------------------------------------------------------------------


def test_single_selects_lose_native_chrome_and_gain_the_chevron() -> None:
    css = controls_css("dark")
    sel = css.split("select:not([multiple])", 1)[1].split("}", 1)[0]
    assert "appearance: none" in sel
    assert "background-image: var(--k-chevron)" in sel
    # Multi-selects keep no chevron (no arrow to draw) — but stay skinned.
    assert "select[multiple]" in css


def test_one_focus_ring_from_tokens() -> None:
    css = controls_css("dark")
    ring = css.split("input:focus-visible", 1)[1].split("}", 1)[0]
    assert "border-color: var(--accent)" in ring
    assert "var(--accent-soft)" in ring


def test_checkboxes_ride_the_accent() -> None:
    assert "accent-color: var(--accent)" in controls_css("dark")


def test_form_baseline_typesets_from_the_scale() -> None:
    css = controls_css("dark")
    base = css.split("input:not([type])", 1)[1].split("}", 1)[0]
    assert "font-size: var(--fs-body)" in base
    assert "border-radius: var(--radius)" in base
    assert "background-color: var(--paper)" in base


# ---------------------------------------------------------------------------
# Buttons, chips, menu — tokens only
# ---------------------------------------------------------------------------


def test_button_hierarchy_is_three_intents_from_tokens() -> None:
    css = controls_css("dark")
    primary = css.split(".k-btn-primary", 1)[1].split("}", 1)[0]
    assert "background: var(--accent)" in primary
    assert "color: var(--accent-contrast)" in primary
    quiet = css.split(".k-btn-quiet", 1)[1].split("}", 1)[0]
    assert "border-color: var(--border)" in quiet
    danger = css.split(".k-btn-danger", 1)[1].split("}", 1)[0]
    assert "var(--bad)" in danger


def test_chips_are_full_radius_micro_uppercase() -> None:
    css = controls_css("dark")
    chip = css.split(".k-chip {", 1)[1].split("}", 1)[0]
    assert "border-radius: var(--radius-full)" in chip
    assert "font-size: var(--fs-micro)" in chip
    assert "text-transform: uppercase" in chip
    for tone in ("ok", "warn", "bad", "accent"):
        assert f".k-chip-{tone}" in css


def test_menu_uses_the_one_popover_shadow() -> None:
    css = controls_css("dark")
    menu = css.split(".k-menu {", 1)[1].split("}", 1)[0]
    assert "box-shadow: var(--shadow-pop)" in menu


def test_no_raw_hex_outside_the_chevron_data_uris() -> None:
    """The kit derives everything from tokens; the only literal colors are the
    two URL-encoded chevron strokes (%23xxxxxx inside the data URIs)."""
    css = controls_css("paper")
    assert not re.search(r"(?<!%23)#[0-9a-fA-F]{3,8}\b", css)


# ---------------------------------------------------------------------------
# format()-template safety
# ---------------------------------------------------------------------------


def test_survives_brace_doubled_format_splicing() -> None:
    """Two page heads splice CSS through str.format after doubling braces
    (analytical dashboard, standalone ticker page). The kit must round-trip."""
    css = controls_css("dark")
    doubled = css.replace("{", "{{").replace("}", "}}")
    assert doubled.format() == css


# ---------------------------------------------------------------------------
# ticker_label — the canonical two-part label
# ---------------------------------------------------------------------------


def test_ticker_label_two_part_shape() -> None:
    html = ticker_label("nu", "Nu Holdings Ltd.")
    assert html.startswith('<span class="k-tick"')
    assert 'title="Nu Holdings Ltd."' in html  # full name always available
    assert '<span class="k-tick-sym">NU</span>' in html  # upper-cased symbol
    assert '<span class="k-tick-name">Nu Holdings Ltd.</span>' in html


def test_ticker_label_href_links_symbol_only() -> None:
    html = ticker_label("NU", "Nu Holdings Ltd.", href="/ticker/NU")
    assert '<a class="k-tick-sym" href="/ticker/NU">NU</a>' in html
    assert '</a><span class="k-tick-name">' in html  # the name stays plain text


def test_ticker_label_without_name_has_no_name_span_or_title() -> None:
    html = ticker_label("META")
    assert "k-tick-name" not in html
    assert "title=" not in html


def test_ticker_label_escapes_and_caps() -> None:
    html = ticker_label("brk.b", 'A "B" & C <Co>', name_max="28ch")
    assert "BRK.B" in html
    assert "&quot;B&quot; &amp; C" in html
    assert "&lt;Co&gt;" in html
    assert "--k-tick-max:28ch" in html


# ---------------------------------------------------------------------------
# Composition — every page CSS carries the kit
# ---------------------------------------------------------------------------


def test_shell_and_dashboard_and_workspace_compose_the_kit() -> None:
    from dashboard._styles import CSS as DASH_CSS
    from pipeline.command_center_shell import SHELL_CSS
    from report.renderers.workspace_styles import CSS as WS_CSS

    for css in (SHELL_CSS, DASH_CSS, WS_CSS):
        assert ".k-btn-primary" in css
        assert "--k-chevron" in css
    # The workspace is the theme-switching surface: paper variant.
    assert "color-scheme: light" in WS_CSS
    assert "color-scheme: dark" in SHELL_CSS
