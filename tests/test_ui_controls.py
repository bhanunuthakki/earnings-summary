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
from collections.abc import Callable
from pathlib import Path

import pytest

from ui.controls import (
    controls_css,
    controls_js,
    icon_svg,
    k_empty,
    panel_section_title,
    panel_toolbar,
    ticker_label,
)
from ui.tokens import CHROME_TOKENS, PALETTE_DARK, PALETTE_LIGHT, palette_css

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC = PROJECT_ROOT / "src"


# ---------------------------------------------------------------------------
# controls_css — modes
# ---------------------------------------------------------------------------


def test_no_legacy_copilot_prompt_execution_primitive() -> None:
    source = (SRC / "ui" / "controls.py").read_text(encoding="utf-8")
    assert "copilot_prompt_chip" not in source
    assert "populateCopilotPrompt" not in source
    assert "k-chip-copilot" not in source


def test_dark_mode_pins_dark_scheme_and_chevron() -> None:
    css = controls_css("dark")
    assert "color-scheme: dark" in css
    assert "--k-chevron:" in css
    assert "data-theme" not in css  # single-theme surfaces get no override block


def test_paper_mode_emits_light_root_plus_dark_override() -> None:
    css = controls_css("paper")
    assert "color-scheme: light" in css
    assert ':root[data-theme="dark"]' in css
    # Both chevron inks present: light root + dark override. Derived from the
    # palette rather than spelled as literals — the literal form of this
    # assertion silently pinned the pre-2026-07-25 cool grays.
    light_ink = "%23" + PALETTE_LIGHT["muted"].lstrip("#")
    dark_ink = "%23" + PALETTE_DARK["muted"].lstrip("#")
    assert light_ink in css and dark_ink in css
    assert light_ink != dark_ink


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


def test_searchable_single_select_owns_visuals_keyboard_and_dynamic_adoption() -> None:
    css = controls_css("dark")
    js = controls_js()

    for selector in (
        ".k-select-shell",
        ".k-select-trigger",
        ".k-select-menu",
        ".k-select-option",
        ".k-select-native",
    ):
        assert selector in css
    assert "background: var(--surface)" in css
    assert ".k-select-option[hidden] { display: none !important; }" in css
    assert ".k-select-shell > .k-select-menu" in css
    assert '[data-k-select-shell="true"] > .k-select-menu' in css
    assert "position: fixed; inset-block: auto" in css
    assert "inset-inline: var(--sp-3)" in css
    assert "select:not([multiple])" in js
    assert "new MutationObserver" in js
    assert "role', 'combobox'" in js
    assert "role', 'listbox'" in js
    assert "aria-activedescendant" in js
    assert "ArrowDown" in js and "ArrowUp" in js
    assert "Backspace" in js and "Escape" in js
    assert "data-k-select-default" in js
    assert "new Event('change', { bubbles: true })" in js
    assert 'type="search"' not in js


def test_searchable_single_select_keeps_multi_select_semantics_native() -> None:
    js = controls_js()

    assert "select.multiple" in js
    assert "select:not([multiple])" in js


def test_every_standalone_select_document_adopts_the_global_runtime() -> None:
    standalone_owners: list[Path] = []
    for path in SRC.rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        if "<select" in source and ("<!doctype" in source or "<html" in source):
            standalone_owners.append(path)
            assert "controls_js" in source, path.relative_to(SRC)
            assert "data-k-select-runtime" in source, path.relative_to(SRC)

    assert standalone_owners
    for relative in (
        "pipeline/work_os_shell.py",
        "report/renderers/workspace_html.py",
    ):
        source = (SRC / relative).read_text(encoding="utf-8")
        assert "controls_js" in source
        assert "data-k-select-runtime" in source


def test_one_focus_ring_from_tokens() -> None:
    css = controls_css("dark")
    ring = css.split("input:focus-visible", 1)[1].split("}", 1)[0]
    assert "border-color: var(--accent)" in ring
    assert "var(--accent-soft)" in ring


def test_checkboxes_ride_the_accent() -> None:
    """The native accent-color box is gone; the kit DRAWS checkbox/radio
    (appearance:none) and a checked one fills the accent (design-sync
    2026-07-19 own-every-pixel kill list)."""
    css = controls_css("dark")
    assert "appearance: none" in css
    checked = css.split('input[type="checkbox"]:checked', 1)[1].split("}", 1)[0]
    assert "background-color: var(--accent)" in checked
    # the checkmark glyph is a theme-dependent data-URI var, like the chevron
    assert "--k-check:" in css


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


def test_work_os_navigation_and_icon_primitives_are_canonical() -> None:
    css = controls_css("dark")
    assert ".k-sidebar {" in css
    assert ".k-nav-item {" in css
    assert ".k-icon-btn {" in css
    assert "width: var(--sidebar-width)" in css
    assert "min-height: var(--nav-item-height)" in css
    assert "width: var(--icon-size)" in css

    icon = icon_svg("portfolio", classes="test-icon")
    assert icon.startswith('<svg class="k-icon test-icon"')
    assert 'aria-hidden="true"' in icon
    assert 'stroke="currentColor"' in icon
    assert "<path" in icon


def test_icon_svg_rejects_unknown_names() -> None:
    with pytest.raises(ValueError):
        icon_svg("made-up")


def test_counterread_icon_is_the_canonical_monochrome_observation_mark() -> None:
    icon = icon_svg("counterread", classes="counterread-mark")

    assert icon.startswith('<svg class="k-icon counterread-mark"')
    assert icon.count("<polyline") == 2
    assert 'data-counterread-observation="true"' in icon
    assert 'fill="currentColor"' in icon
    assert 'stroke="currentColor"' in icon


def test_standalone_earnings_calendar_composes_the_design_system() -> None:
    """Execution-generated HTML is a product surface too; keep it inside the
    design-sync boundary instead of letting the src-only discovery miss it."""
    source = (PROJECT_ROOT / "execution" / "build_earnings_calendar.py").read_text(encoding="utf-8")
    assert 'palette_css("dark")' in source
    assert 'controls_css("dark")' in source
    assert 'class="calendar-sidebar k-sidebar"' in source
    assert 'class="k-chip k-chip-mono calendar-kind' in source
    assert 'class="badge ' not in source
    assert '"Segoe UI"' not in source
    assert 'aria-label="Earnings calendar"' in source
    assert 'aria-label="Open command center"' in source
    assert 'aria-label="Research briefs"' in source
    assert 'href="http://127.0.0.1:7421/"' in source
    assert source.count('class="calendar-section k-card k-card-stack"') == 2
    assert 'class="calendar-section k-card"' in source
    assert source.count('class="k-card-title"') == 2
    assert 'class="calendar-meta k-card-meta"' in source
    assert ".calendar-table {{ background:" not in source
    assert "h2 {{" not in source
    assert source.count('class="calendar-table-wrap"') == 3
    assert "overflow-x: auto" in source


def test_standalone_earnings_calendar_uses_counterread_home_brand() -> None:
    source = (PROJECT_ROOT / "execution" / "build_earnings_calendar.py").read_text(encoding="utf-8")

    assert "<title>Counterread — Earnings Calendar — {today.isoformat()}</title>" in source
    assert 'class="calendar-brand k-btn k-btn-quiet"' in source
    assert 'href="http://127.0.0.1:7421/"' in source
    assert 'aria-label="Counterread home"' in source
    assert '{icon_svg("counterread", classes="counterread-mark")}' in source
    assert '<span class="calendar-brand-label">Counterread</span>' in source
    assert ".calendar-brand-label, .calendar-layer-title" in source
    assert ".calendar-brand, .calendar-layer-title" not in source
    assert ".calendar-brand {{ width: var(--touch-target-size);" in source
    assert "min-height: var(--touch-target-size)" in source
    assert "min-width: var(--touch-target-size)" in source
    assert "Earnings OS" not in source


def test_dashboard_alert_cards_only_add_layout_to_card_kit() -> None:
    from dashboard._styles import CSS as DASHBOARD_CSS

    rule = DASHBOARD_CSS.split(".alert-card {", 1)[1].split("}", 1)[0]
    assert "margin-bottom: var(--sp-3)" in rule
    for property_name in ("background:", "border:", "border-radius:", "padding:", "box-shadow:"):
        assert property_name not in rule


def test_mobile_controls_keep_accessible_text_and_touch_floors() -> None:
    css = controls_css("dark")
    tokens = palette_css("dark")
    assert CHROME_TOKENS["mobile-control-font-size"] == "16px"
    assert CHROME_TOKENS["touch-target-size"] == "44px"
    assert "--mobile-control-font-size: 16px" in tokens
    assert "--touch-target-size: 44px" in tokens
    mobile = css.split("@media (max-width: 768px)", 1)[1]
    assert "font-size: var(--mobile-control-font-size)" in mobile
    assert "min-height: var(--touch-target-size)" in mobile


def test_chips_are_full_radius_caption_uppercase() -> None:
    css = controls_css("dark")
    chip = css.split(".k-chip {", 1)[1].split("}", 1)[0]
    assert "border-radius: var(--radius-full)" in chip
    # design-sync 2026-07-19: --fs-micro folded into --fs-caption (4-step scale)
    assert "font-size: var(--fs-caption)" in chip
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
    html = ticker_label("brk.b", 'A "B" & C <Co>', wide=True)
    assert "BRK.B" in html
    assert "&quot;B&quot; &amp; C" in html
    assert "&lt;Co&gt;" in html
    assert "k-tick-wide" in html
    assert "style=" not in html


# ---------------------------------------------------------------------------
# Composition — every page CSS carries the kit
# ---------------------------------------------------------------------------


def test_dashboard_and_workspace_compose_the_kit() -> None:
    from dashboard._styles import CSS as DASH_CSS
    from report.renderers.workspace_styles import CSS as WS_CSS

    for css in (DASH_CSS, WS_CSS):
        assert ".k-btn-primary" in css
        assert "--k-chevron" in css
    # The workspace is the theme-switching surface: paper variant.
    assert "color-scheme: light" in WS_CSS


# ---------------------------------------------------------------------------
# New control-kit primitives (S1): status pills/wells, overlay, panel toolbar.
# ---------------------------------------------------------------------------


def test_status_pill_kit_is_soft_fill_over_tokens() -> None:
    """``.k-pill`` is the one FILLED status badge — a soft color-mix fill +
    token ink. The four semantic variants derive from --ok/--warn/--bad/--accent
    so the per-panel raw-hex pill systems can be deleted (S7)."""
    css = controls_css("dark")
    base = css.split(".k-pill {", 1)[1].split("}", 1)[0]
    assert "border-radius: var(--radius-full)" in base
    for tone, tok in (("ok", "--ok"), ("warn", "--warn"), ("bad", "--bad"), ("accent", "--accent")):
        rule = css.split(f".k-pill-{tone}", 1)[1].split("}", 1)[0]
        assert f"color-mix(in srgb, var({tok})" in rule
        assert f"color: var({tok})" in rule


def test_status_well_kit_is_block_soft_fill() -> None:
    css = controls_css("dark")
    base = css.split(".k-well {", 1)[1].split("}", 1)[0]
    assert "border-radius: var(--radius)" in base
    for tok in ("--ok", "--warn", "--bad", "--accent"):
        assert f"color-mix(in srgb, var({tok}) 16%, transparent)" in css


def test_card_kit_owns_compact_geometry_and_explicit_type_roles() -> None:
    """Cards and their ordinary titles have one shared, responsive contract.

    Metric cards keep the separate ``stat-*`` vocabulary; these primitives
    prevent ordinary research/action headings from borrowing metric type.
    """
    css = controls_css("dark")
    card = css.split(".k-card {", 1)[1].split("}", 1)[0]
    assert "background: var(--surface)" in card
    assert "border: var(--bw-thin) solid var(--border)" in card
    assert "border-radius: var(--radius-card)" in card
    assert "padding: var(--sp-3)" in card
    assert "min-width: 0" in card
    assert "height:" not in card
    assert "min-height:" not in card
    assert "max-height:" not in card

    dense = css.split(".k-card-dense {", 1)[1].split("}", 1)[0]
    assert "padding: var(--sp-2) var(--sp-3)" in dense
    stack = css.split(".k-card-stack {", 1)[1].split("}", 1)[0]
    assert "flex-direction: column" in stack
    assert "gap: var(--sp-3)" in stack

    title = css.split(".k-card-title {", 1)[1].split("}", 1)[0]
    assert "font-size: var(--fs-title)" in title
    assert "color: var(--fg)" in title
    row_title = css.split(".k-card-row-title {", 1)[1].split("}", 1)[0]
    assert "font-size: var(--fs-body)" in row_title
    meta = css.split(".k-card-meta {", 1)[1].split("}", 1)[0]
    assert "font-size: var(--fs-caption)" in meta
    assert "color: var(--muted)" in meta

    assert ".stat-heading {" in css
    assert ".stat-number {" in css


def test_card_kit_owns_semantic_archetypes_and_title_alignment() -> None:
    """Cards choose one composition recipe instead of restyling their anatomy."""

    css = controls_css("dark")
    head = css.split(".k-card-head {", 1)[1].split("}", 1)[0]
    assert "display: flex" in head
    assert "align-items: flex-start" in head
    assert "justify-content: space-between" in head
    assert "gap: var(--sp-3)" in head

    heading = css.split(".k-card-heading {", 1)[1].split("}", 1)[0]
    assert "display: flex" in heading
    assert "flex-direction: column" in heading
    assert "gap: var(--sp-1)" in heading

    stat = css.split(".k-card-stat {", 1)[1].split("}", 1)[0]
    assert "padding: 0" in stat
    stat_grid = css.split(".k-stat-grid {", 1)[1].split("}", 1)[0]
    assert "display: grid" in stat_grid
    assert "grid-template-columns: repeat(4, minmax(0, 1fr))" in stat_grid
    stat_cell = css.split(".k-stat-cell {", 1)[1].split("}", 1)[0]
    assert "padding: var(--sp-3)" in stat_cell

    action = css.split(".k-card-action {", 1)[1].split("}", 1)[0]
    assert "padding: var(--sp-2) var(--sp-3)" in action
    section = css.split(".k-card-section {", 1)[1].split("}", 1)[0]
    assert "display: flex" in section and "gap: var(--sp-3)" in section
    desk_hero = css.split(".k-desk-hero {", 1)[1].split("}", 1)[0]
    assert "padding: var(--sp-3)" in desk_hero
    navigation = css.split(".k-card-nav {", 1)[1].split("}", 1)[0]
    assert "display: flex" in navigation


def test_status_dot_kit_is_currentcolor_circle_over_tokens() -> None:
    """``.k-dot`` is the one filled circular status dot — a full-radius circle
    filled with ``currentColor`` so a tone modifier only sets ``color`` (the
    .k-prov-tick idiom). The four semantic variants derive from
    --ok/--warn/--bad/--muted so the per-panel dot-tone systems
    (.dot-*/.fdot-*/.ch-dot-*/.cc-system-dot-*) can be deleted."""
    css = controls_css("dark")
    base = css.split(".k-dot {", 1)[1].split("}", 1)[0]
    assert "border-radius: var(--radius-full)" in base
    assert "background: currentColor" in base
    # Diameter is master-owned; consumers cannot resize the primitive ad hoc.
    assert "var(--dot-size)" in base
    assert "--k-dot-size" not in base
    for tone, tok in (("ok", "--ok"), ("warn", "--warn"), ("bad", "--bad"), ("muted", "--muted")):
        rule = css.split(f".k-dot-{tone}", 1)[1].split("}", 1)[0]
        assert f"color: var({tok})" in rule


def test_num_text_kit_is_green_red_over_tokens() -> None:
    """``.k-num-pos`` / ``.k-num-neg`` are the one green/red NUMBER-text tone
    (P&L cells, deltas, alpha) → --ok/--bad, so the dashboard/pipeline surfaces
    stop duplicating td.pos/.sk-val.pos etc. Status green/red only — the report's
    .pos accent-wayfinding (--accent/--muted) is a separate LOCAL concern."""
    css = controls_css("dark")
    pos = css.split(".k-num-pos {", 1)[1].split("}", 1)[0]
    neg = css.split(".k-num-neg {", 1)[1].split("}", 1)[0]
    assert "color: var(--ok)" in pos
    assert "color: var(--bad)" in neg


def test_overlay_primitive_scrim_and_elevation() -> None:
    """``.k-scrim`` + ``.k-overlay`` are the tokenized substrate S4's CCOverlay
    JS wires dismissal onto: neutral scrim, surface panel, one radius + the one
    popover shadow, motion on open, reduced-motion respected."""
    css = controls_css("dark")
    overlay = css.split(".k-overlay {", 1)[1].split("}", 1)[0]
    assert "background: var(--surface)" in overlay
    assert "border-radius: var(--radius)" in overlay
    assert "box-shadow: var(--shadow-pop)" in overlay
    assert "@keyframes k-overlay-rise" in css
    assert "prefers-reduced-motion" in css


def test_panel_toolbar_is_one_band_title_then_controls() -> None:
    """One operating band: title on the left, filters + actions together on the
    right (design_language §6.1)."""
    css = controls_css("dark")
    assert ".k-toolbar {" in css and "margin-right: auto" in css
    tb = panel_toolbar(
        "Discovery", filters='<span class="k-chip">a</span>', actions="<button>Run</button>"
    )
    assert tb.count("k-toolbar") >= 1
    assert '<h2 class="k-toolbar-title">Discovery</h2>' in tb
    # filters and actions share ONE controls row (no second band).
    assert tb.count("k-toolbar-controls") == 1
    assert tb.index("k-toolbar-title") < tb.index("k-toolbar-controls")


def test_panel_toolbar_sticky_pins_below_the_shell_topbar() -> None:
    """Owner directive 2026-08-02: ``sticky=True`` adds ``.k-toolbar-sticky``
    (never replacing ``.k-toolbar`` — the layout/spacing rules still apply),
    and the plain non-sticky band is unaffected by the new kwarg."""
    css = controls_css("dark")
    band = css.split(".k-toolbar-sticky, .k-chip-tabs-sticky {", 1)[1].split("}", 1)[0]
    assert "position: sticky" in band
    assert "top: var(--header-height)" in band
    assert "background: var(--bg)" in band
    assert "border-bottom: 1px solid var(--border)" in band

    tb = panel_toolbar("Provenance", filters='<span class="k-chip">a</span>', sticky=True)
    assert 'class="k-toolbar k-toolbar-sticky"' in tb
    plain = panel_toolbar("Provenance", filters='<span class="k-chip">a</span>')
    assert 'class="k-toolbar"' in plain and "k-toolbar-sticky" not in plain


def test_chip_tab_active_state_is_an_underline_not_a_filled_pill() -> None:
    """Owner directive 2026-08-02: a chip-tab's ACTIVE state must never change
    the chip's box size (a filled pill / border recolor would), so it is
    strictly an inset box-shadow (never resizes the box) + accent text —
    never a ``background`` fill."""
    css = controls_css("dark")
    inactive = css.split(".k-chip-tab {", 1)[1].split("}", 1)[0]
    assert "border-color: transparent" in inactive
    active = css.split(".k-chip-tab.is-on {", 1)[1].split("}", 1)[0]
    assert "color: var(--accent)" in active
    assert "box-shadow: inset 0 -2px 0 0 var(--accent)" in active
    assert "background" not in active  # never a filled pill


def test_panel_section_title_suppressed_when_nav_owns_it() -> None:
    """A panel under a single-sub-tab section must not re-print its name — the
    nav already shows it (design_language §6.1)."""
    assert panel_section_title("Ask") == '<h2 class="k-toolbar-title">Ask</h2>'
    assert panel_section_title("Ask", suppressed=True) == ""
    assert panel_section_title("") == ""
    # Suppressed title + present controls → band with no heading, no empty <h2>.
    tb = panel_toolbar("Ask", actions="<button>Go</button>", suppress_title=True)
    assert "k-toolbar-title" not in tb
    assert "k-toolbar-controls" in tb


# ---------------------------------------------------------------------------
# k_empty — the D4 empty/degraded-state primitive
# ---------------------------------------------------------------------------


def test_k_empty_css_is_a_muted_line_from_tokens() -> None:
    css = controls_css("dark")
    rule = css.split(".k-empty {", 1)[1].split("}", 1)[0]
    assert "color: var(--muted)" in rule
    assert "font-size: var(--fs-body)" in rule
    assert ".k-empty-chip {" in css


def test_k_empty_line_only_has_no_chip_span() -> None:
    html = k_empty("Not derivable yet - a risk snapshot unlocks this read.")
    assert html == '<p class="k-empty">Not derivable yet - a risk snapshot unlocks this read.</p>'
    assert "k-empty-chip" not in html


def test_k_empty_with_chip_appends_the_doorway_inline() -> None:
    chip = '<button type="button" class="k-chip k-chip-btn">Encode targets</button>'
    html = k_empty("No active target set.", chip)
    assert html.startswith('<p class="k-empty">No active target set.')
    assert f'<span class="k-empty-chip">{chip}</span></p>' in html


def test_k_empty_escapes_the_line_but_not_the_chip() -> None:
    html = k_empty('Diagnostics: "tracker offline" <raw>', "<b>ok</b>")
    assert "&quot;tracker offline&quot;" in html
    assert "&lt;raw&gt;" in html
    assert "<b>ok</b>" in html  # chip_html rides through unescaped (pre-rendered HTML)


# ===========================================================================
# Opt-out token-conformance guard (design_language §2/§7 — Enforcement).
#
# The OLD guard was opt-IN: it imported ~22 hand-listed panel CSS constants and
# regexed only raw hex. The shell shipped a legacy-alias :root, raw-hex
# gradients and off-scale glyphs and passed GREEN — it was simply never on the
# list. This inverts that. Every CSS-emitting module under src/ is auto-
# discovered from the filesystem; each must be CLEAN, sanctioned-EXEMPT, or
# QUARANTINED with an owner. A hardcoded clean-list would just rebuild the
# allowlist sin, so there isn't one — the registry exists only to make a NEW
# unregistered surface fail loudly until someone classifies it.
#
# Discovery signal: a module "emits CSS" iff its source contains ``var(--`` (the
# token reference every surface stylesheet uses). That is exactly the ~41
# modules; the registry below must equal what the filesystem yields.
#
# The shared scanner owns all denied dimensions (design_language §2/§3),
# including the four registry-backed composition checks added by BHA-90:
# floating-card-title, off-scale-indent, unsanctioned-shape-geometry, and
# off-scale-grid-column. The existing token dimensions remain unchanged:
#   color       raw hex — incl. ``var(--x, #hex)`` fallbacks AND hex inside
#               ``linear-gradient(...)`` (a literal # anywhere that isn't an
#               href fragment or %23-encoded data-URI)
#   font-size   off-scale ``font-size: <px>`` (not a TYPE_SCALE step)
#   radius      off-scale ``border-radius: <px>`` (not --radius / --radius-full)
#   font-family any family value but the three font TOKENS + generic keywords —
#               the owner's "too many fonts" complaint, first-class enforced
#   alias       legacy alias var-names (--panel/--ink/--link/--font-mono/…)
# DIMENSION-SCOPED to color/font/radius: layout px (width/margin/padding/gap/
# top/left/height) is NEVER touched — the px regexes anchor on ``font-size:`` /
# ``border-radius:`` only. Inline ``style="…"`` rides the same scan (both CSS
# rules and inline styles live in the module's string literals); a layout
# ``style="width:60px"`` is ignored, a color ``style="color:#f00"`` is caught
# (both asserted below).
#
# RED-STATE POLICY — allowlist-with-expiry, NOT block-on-merge. Block-on-merge
# would wedge the 3 parallel Wave-1 siblings and a permanently-red CI invites an
# ``--ignore`` escape hatch (there is none here). Instead:
#   * Every CLEAN surface — and every clean DIMENSION of a quarantined surface —
#     is ENFORCED: new drift fails immediately (test_no_unquarantined_drift).
#   * Known pre-existing drift is QUARANTINED per (surface, dimension) with its
#     owning session. The quarantine can only SHRINK: a quarantined dimension
#     that becomes clean FAILS (test_quarantine_only_shrinks) — "graduate it" —
#     so S7's burn-down is ratcheted, not optional.
#   * Full conformance is a normal green gate: the quarantine is empty, and any
#     new drift fails immediately without an ignore path.
# ===========================================================================


# ===========================================================================
# Layer B — button kit-coverage (design_language §4: "compose the kit, never
# reinvent"). The TOKEN dimensions + kit-badge are blind to a reinvented BUTTON
# (accent/border/padding fills are legit everywhere, so a CSS-signature regex
# false-fires). Instead this is a POSITIVE check on EMITTED <button> markup:
# every button carries a kit class (.k-btn / .k-chip / .k-prov-act) or an
# allowlisted bespoke control. A NEW `<button class="my-btn">` fails until it
# composes the kit — the structural backstop that stops button reinvention the
# way the workspace-consistency sweep had to fix it by hand.
# ===========================================================================
#: A button is kit-composed if any of its classes is one of these.
# Bespoke approvals are typed registry data; this scanner only consumes them.


def test_combobox_renders_two_part_ticker_labels() -> None:
    """The holding combobox keeps ticker value and company label separate."""
    import pipeline.ticker_command_center as ticker_command_center

    combo = getattr(ticker_command_center, "_combobox")("NU", "Nu Holdings Ltd.")
    assert 'value="NU"' in combo
    assert '<span class="cc-combo-name"' in combo
    assert "NU · Nu Holdings" not in combo


# ===========================================================================
# S8 — Ask title-ownership + control-row sizing (interaction_paradigm §3 "Ask").
# Two narrowly-scoped structural guards so the class of miss can't regress:
#   #2  no explicit font-size on a ``*-inputrow`` control — input/buttons inherit
#       the kit baseline (--fs-body) so they MATCH each other, and the panel rule
#       never overrides the mobile 16px floor.
#   #1  a single-sub-tab panel must not re-print its section name as a top-level
#       <h2> — the nav owns the title (design_language §6.1; Law 3).
# ===========================================================================

# A CSS rule whose selector names a ``*-inputrow`` AND whose body sets font-size.
# (The opt-out hex/px guard can't catch this: ``font-size: var(--fs-section)`` is
# a valid token — just the WRONG one for a control row, which should match the
# 13px ``.k-btn`` buttons beside it.) Split at rule boundaries first so this
# guard stays linear instead of backtracking across every registered surface.


def test_fact_anchor_attrs_emits_handle_and_degrades() -> None:
    """The doorway-handle helper (S12, Law 2): the anchor key is always present;
    the stable fact_ref is emitted only when known (else degrade), with
    data-fact-ref ordered before data-ask-q (exact wins). Values are escaped."""
    from ui.controls import fact_anchor_attrs

    # Degrade: no handle → name-keyed anchor only.
    assert fact_anchor_attrs(None, "NPL ratio") == 'data-anchor-key="NPL ratio"'
    # With a handle → both, anchor-key first.
    both = fact_anchor_attrs("kpi:NU:42", 'Risk "adj" NIM')
    assert 'data-anchor-key="Risk &quot;adj&quot; NIM"' in both  # quotes escaped
    assert 'data-fact-ref="kpi:NU:42"' in both
    # Precedence: a cell carrying both — data-fact-ref ordered before data-ask-q.
    triple = fact_anchor_attrs("kpi:NU:42", "NIM", ask_q="How has NIM trended?")
    assert triple.index("data-fact-ref") < triple.index("data-ask-q")


def test_glyph_ink_tracks_the_palette() -> None:
    """The two theme-dependent glyph inks are DERIVED from tokens, not copied.

    Regression pin for a silent drift found on 2026-07-25: ``_CHEVRON_DARK`` and
    ``_CHECK_DARK`` froze ``%23888b94`` / ``%230c0d10`` while their comments
    claimed they tracked ``--muted`` / ``--accent-contrast``. Warming the dark
    palette falsified both and nothing failed — the chevron just kept rendering
    in the old cool gray on a warm surface.

    This asserts the LINK, so it survives any future palette edit: whatever the
    palette says today must be the ink in today's glyph, and the stale cool
    values must be gone from the rendered CSS.
    """

    def enc(value: str) -> str:
        return "%23" + value.lstrip("#")

    dark_css = controls_css("dark")
    light_css = controls_css("paper")
    dark_chevron = re.search(r"--k-chevron:\s*([^;]+);", dark_css)
    light_chevron = re.search(r"--k-chevron:\s*([^;]+);", light_css)
    dark_check = re.search(r"--k-check:\s*([^;]+);", dark_css)
    light_check = re.search(r"--k-check:\s*([^;]+);", light_css)
    assert dark_chevron is not None and light_chevron is not None
    assert dark_check is not None and light_check is not None

    assert enc(PALETTE_DARK["muted"]) in dark_chevron.group(1)
    assert enc(PALETTE_LIGHT["muted"]) in light_chevron.group(1)
    assert enc(PALETTE_DARK["accent-contrast"]) in dark_check.group(1)
    assert enc(PALETTE_LIGHT["accent-contrast"]) in light_check.group(1)

    # The glyphs must differ per theme — one shared ink would mean a glyph is
    # illegible on one of the two grounds.
    assert dark_chevron.group(1) != light_chevron.group(1)
    assert dark_check.group(1) != light_check.group(1)

    # And the rendered CSS must carry no pre-warming ink.
    for mode in ("paper", "dark"):
        css = controls_css(mode)
        assert "%23888b94" not in css, "stale cool --muted ink in chevron"
        assert "%230c0d10" not in css, "stale cool --accent-contrast ink in check"


# ---------------------------------------------------------------------------
# The research document primitives (design_language §6.3)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["paper", "dark"])
def test_document_primitives_present(mode: str) -> None:
    """The document kit ships in both modes — it is not a dark-only surface."""
    css = controls_css(mode)
    for selector in (
        ".k-doc",
        ".k-doc-row",
        ".k-doc-mast",
        ".k-note",
        ".k-note-title",
        ".k-fn",
        ".k-band",
        ".k-qa",
        ".k-label-mark",
    ):
        assert selector in css, f"{selector} missing from controls_css({mode!r})"


def test_doc_row_carries_the_note_column_not_the_document() -> None:
    """A margin note attaches per-section; the document reserves no gutter.

    The whole point of ``.k-doc-row`` is that ``.k-doc`` stays a single column.
    If ``.k-doc`` ever grows its own ``grid-template-columns`` the layout is back
    to a standing rail that sits empty everywhere a note isn't.
    """
    css = controls_css("paper")
    doc_rule = re.search(r"\.k-doc\s*\{([^}]*)\}", css)
    assert doc_rule is not None
    assert "grid-template-columns" not in doc_rule.group(1)

    row_rule = re.search(r"\.k-doc-row\s*\{([^}]*)\}", css)
    assert row_rule is not None
    assert "grid-template-columns" in row_rule.group(1)


@pytest.mark.parametrize("mode", ["paper", "dark"])
def test_mark_soft_is_only_ever_a_keyline(mode: str) -> None:
    """``--mark-soft`` is below the AA-body floor by design (see test_ui_tokens).

    That makes it safe on a 1px rule and unsafe on type. This is the CSS-side
    half of that contract: every declaration consuming it must be a border. The
    token test pins the ratio; this pins the usage, so the two together make
    "keyline only" an enforced property rather than a comment.
    """
    css = controls_css(mode)
    uses = re.findall(r"([a-z-]+)\s*:\s*[^;{}]*var\(--mark-soft\)", css)
    assert uses, "--mark-soft is unused; drop the token or use it"
    offenders = [prop for prop in uses if not prop.startswith("border")]
    assert not offenders, (
        f"--mark-soft used on {offenders} — it is a keyline token below the text "
        "contrast floor. Carry type with --mark."
    )


@pytest.mark.parametrize("mode", ["paper", "dark"])
def test_mark_never_fills_a_control(mode: str) -> None:
    """The editorial mark is furniture ink, never a fill.

    ``--accent`` owns interactive fills. If ``--mark`` starts backgrounding
    things it becomes a second accent and the "one interactive color" rule in
    §2 quietly dies.
    """
    css = controls_css(mode)
    fills = re.findall(r"(background(?:-color)?)\s*:\s*[^;{}]*var\(--mark\b", css)
    assert not fills, f"--mark used as a fill ({fills}); it is ink, not a background"


def test_footnote_marker_uses_the_master_type_scale() -> None:
    """Reference marks use the canonical caption role, never a local type step."""
    css = controls_css("paper")
    rule = re.search(r"\.k-fn\s*\{([^}]*)\}", css)
    assert rule is not None
    assert "font-size: var(--fs-caption)" in rule.group(1)


def test_note_rail_does_not_eat_the_reading_measure() -> None:
    """The document width is the measure PLUS the note rail, never a flat cap.

    Regression pin for a bug caught by browser measurement, not by review:
    ``.k-doc`` was a flat ``max-width: 76ch``, so in any section carrying a
    margin note the 13.5rem rail was subtracted from the inside and prose
    collapsed to ~40ch — unreadable, in exactly the sections that matter most.

    Deriving the width from ``--reading-measure`` keeps prose at its measure whether
    or not a section is annotated, and lets full-bleed sections use the whole
    width. Verified in-browser at 1280px: prose 66ch, band full-bleed at 703px.
    """
    css = controls_css("paper")
    doc_rule = re.search(r"\.k-doc\s*\{([^}]*)\}", css)
    assert doc_rule is not None
    body = doc_rule.group(1)
    assert "--k-measure" not in body
    max_width = re.search(r"max-width:\s*([^;]+)", body)
    assert max_width is not None
    expr = max_width.group(1)
    assert expr.strip().startswith("calc("), (
        f"max-width is {expr!r} — a flat cap lets the note rail eat the measure"
    )
    assert "var(--reading-measure)" in expr and "var(--note-rail-width)" in expr

    # Prose is capped independently, so a full-bleed section does not run long.
    prose_cap = re.search(r"\.k-doc\s+\.prose\s*\{([^}]*)\}", css)
    assert prose_cap is not None, ".k-doc .prose must cap at the measure"
    assert "var(--reading-measure)" in prose_cap.group(1)


# ---------------------------------------------------------------------------
# §6.3 document form on the workspace report
# ---------------------------------------------------------------------------


def test_workspace_panels_debox_inside_a_document() -> None:
    """Inside a ``.k-doc`` the report's panels become sections, not cards.

    §6.3's load-bearing rule is "rules instead of boxes": a research page that
    grows cards reads as a dashboard. This is implemented as a scoped stylesheet
    block rather than a rewrite of twenty ``_panel_head()`` call sites, so the
    markup — cross-link targets, comment anchors, panel ids — is untouched and
    the golden HTML diff stays one wrapper class.

    Verified in-browser on the real golden pane: 9 panels de-boxed, section
    titles in --mark at 11px uppercase, rows and table cells flush, thesis lede
    still capped at 66ch, and the mark clears AA-body on all three themes
    (paper 5.62:1 / white 5.88:1 / dark 6.32:1).
    """
    from report.renderers.workspace_styles import CSS

    rule = re.search(r"\.k-doc\s+\.panel\s*\{([^}]*)\}", CSS)
    assert rule is not None, "workspace CSS must map .panel to a document section"
    body = rule.group(1)
    # The box goes away...
    assert re.search(r"border:\s*0", body)
    assert re.search(r"background:\s*none", body)
    assert re.search(r"border-radius:\s*0", body)
    # ...replaced by a hairline section rule.
    assert "border-top:" in body
    # --panel-pad-x is the single lever that flushes heads, val-rows and .tbl
    # cells to the document's left edge. Without it the section keeps card
    # padding and still reads as a box with the border removed.
    assert re.search(r"--panel-pad-x:\s*var\(--indent-0\)", body), (
        "panels must flush to the document edge via --panel-pad-x"
    )

    title = re.search(r"\.k-doc\s+\.panel-title\s*\{([^}]*)\}", CSS)
    assert title is not None
    assert "var(--mark)" in title.group(1), "section labels take the editorial mark"


def test_thesis_tab_opts_into_document_form() -> None:
    """The thesis tab declares itself a document.

    ``k-doc`` brings the semantics; ``k-doc-fluid`` drops the kit's outer width
    clamp because the report owns its page width and spans wide financial
    tables. Both are required — ``k-doc`` alone would squeeze the report to the
    reading measure and break every financial table on the tab.
    """
    golden = PROJECT_ROOT / "tests" / "golden" / "workspace" / "portfolio" / "pane_thesis.html"
    html = golden.read_text(encoding="utf-8")
    assert 'class="tab-body k-doc k-doc-fluid"' in html

    css = controls_css("paper")
    fluid = re.search(r"\.k-doc-fluid\s*\{([^}]*)\}", css)
    assert fluid is not None and "max-width: none" in fluid.group(1)


# ===========================================================================
# Guard extensions (Wave 1 — k_empty + guard extensions): three narrowly
# scoped structural checks the opt-out token guard (Layer A, above) and the
# button-coverage guard (Layer B, above) are both blind to. Each ships with a
# self-test proving it actually fires on a known violation shape — a guard
# that can't demonstrate detection is a no-op guard (repo standing rule).
# ===========================================================================

# ---------------------------------------------------------------------------
# Guard (a): classless <button> inside an overlay *-head container.
#
# design_language §3: every close glyph is a NAMED, styled control (compare
# cc-peek-close / cc-drawer-close) — not raw chrome hung off a descendant
# selector like the pre-fix `.ask-pop-head button`. The known head containers
# are enumerated (not pattern-matched off "-head" alone) so the check stays
# precise: these six are the overlay-head shape this guard cares about.
# ---------------------------------------------------------------------------

_OVERLAY_HEAD_CLASSES = (
    "ask-pop-head",
    "cc-drawer-head",
    "cc-peek-head",
    "cite-pop-head",
    "tcc-drawer-head",
    "ask-dock-head",
)
_HEAD_CONTAINER_RE = re.compile(
    r'class="[^"]*\b(?:' + "|".join(re.escape(c) for c in _OVERLAY_HEAD_CLASSES) + r')\b[^"]*"'
)
_BUTTON_OPEN_RE = re.compile(r"<button\b([^>]*)>", re.IGNORECASE)
# How far past a *-head container's class attribute to look for its buttons.
# The real containers are small (a title span + one close glyph) — 400 chars
# comfortably covers every real head, div or span alike, without reaching
# into unrelated markup further down the page.
_HEAD_SCAN_WINDOW = 400


def _classless_buttons_in_overlay_heads(text: str) -> list[str]:
    """``<button>`` tags with no ``class=`` attribute inside a *-head overlay
    container. Scans a bounded window after the container's class attribute
    rather than tag-balancing — containers are ``<div>`` in most surfaces but
    ``<span>`` in ``cite-pop-head``, so there is no single closing tag to hunt
    for. Returns each offending button's raw attribute string (for messages)."""
    offenders: list[str] = []
    for m in _HEAD_CONTAINER_RE.finditer(text):
        window = text[m.end() : m.end() + _HEAD_SCAN_WINDOW]
        for attrs in _BUTTON_OPEN_RE.findall(window):
            if "class=" not in attrs:
                offenders.append(attrs.strip())
    return offenders


def test_overlay_head_classless_button_check_fires_on_known_violation() -> None:
    """Self-test: this is the exact pre-fix markup shape explore_panel.py used
    to emit (`<button type="button" id="ask-pop-close" title="Close (Esc)">`
    with no class, inside `.ask-pop-head`) — proving the guard above actually
    detects it rather than trivially passing. The post-fix shape (a named
    class added) must come back clean."""
    pre_fix = (
        '<div class="ask-pop-head"><span>DIY builder &middot; saved views</span>'
        '<button type="button" id="ask-pop-close" title="Close (Esc)">&times;</button></div>'
    )
    hits = _classless_buttons_in_overlay_heads(pre_fix)
    assert len(hits) == 1
    assert 'id="ask-pop-close"' in hits[0]

    post_fix = pre_fix.replace(
        '<button type="button" id="ask-pop-close"',
        '<button type="button" class="ask-pop-close" id="ask-pop-close"',
    )
    assert _classless_buttons_in_overlay_heads(post_fix) == []

    # A span-shaped head (cite-pop-head) is scanned the same way — no button
    # at all there today, so it must stay clean.
    span_head = '<span class="cite-pop-head"><span class="cite-pop-tick">NU</span></span>'
    assert _classless_buttons_in_overlay_heads(span_head) == []


# ---------------------------------------------------------------------------
# Guard (b): whole-TABLE mono (font-family: var(--mono) on a bare table-level
# selector, catching the label/header column in the mono treatment meant for
# value cells only). Canonical pattern: viewspec/render.py's `.vx-matrix td`
# scopes mono to <td> only, leaving `.vx-matrix .vx-label` / header `<th>` to
# inherit body's sans.
# ---------------------------------------------------------------------------

# A selector that is nothing but a single class ending "…table…" with no
# descendant qualifier (no " td" / " th" / anything after it) — e.g.
# ".sv-stmt-table" fires, ".sv-stmt-table td" and ".vx-matrix" (no "table" in
# the name) do not.
# Deliberate whole-table mono approvals are typed registry data; the selector
# scanner remains here.


# ---------------------------------------------------------------------------
# Guard (c): CCAction adopter census — a REGRESSION-ONLY ratchet.
#
# CCAction.busy()/.release()/.receipt()/.leave() (PR #1092) provides busy,
# release, receipt, and leave feedback for surfaces that adopt it. This is a
# regression-only census. It does not require adoption by surfaces outside
# the pinned set. It fails if a current adopter drops to zero references.
# ---------------------------------------------------------------------------

# Pinned adopters are typed registry data. Growing that floor remains allowed;
# the scanner below still fails only when a pinned member regresses.


def _ccaction_regressions(pinned: frozenset[str], read_text: Callable[[str], str]) -> list[str]:
    """Members of ``pinned`` whose current text no longer references
    ``CCAction.busy`` — i.e. a conformant surface regressed. Takes a
    ``read_text`` callable (rather than reading files itself) so the
    detection logic is unit-testable against synthetic text without a real
    filesystem round-trip."""
    return [rel for rel in sorted(pinned) if "CCAction.busy" not in read_text(rel)]


def test_ccaction_ratchet_fires_on_regression_but_never_forces_adoption() -> None:
    """Self-test: proves the ratchet fires when a PINNED adopter loses its
    CCAction.busy reference, and — just as important — proves it does NOT
    fire for a file outside the pinned set that never adopted CCAction at
    all (no forced adoption; this is a floor, not a mandate)."""
    texts = {
        "pipeline/discovery_panel.py": "// CCAction.busy(el) still wired here",
        "pipeline/journal_panel.py": "// regressed: bare el.disabled = true now",
        "pipeline/some_new_panel.py": "// never adopted CCAction at all",
    }
    pinned = frozenset({"pipeline/discovery_panel.py", "pipeline/journal_panel.py"})
    offenders = _ccaction_regressions(pinned, lambda rel: texts[rel])
    assert offenders == ["pipeline/journal_panel.py"]
    assert "pipeline/some_new_panel.py" not in offenders
