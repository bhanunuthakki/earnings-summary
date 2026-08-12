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
import token as _token
import tokenize
from collections.abc import Callable
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC = PROJECT_ROOT / "src"
sys.path.insert(0, str(SRC))

from ui.controls import (  # noqa: E402
    controls_css,
    icon_svg,
    k_empty,
    panel_section_title,
    panel_toolbar,
    ticker_label,
)
from ui.tokens import (  # noqa: E402
    CHROME_TOKENS,
    FONT_FAMILY_KEYWORDS,
    RADIUS_PX,
    TYPE_SCALE_PX,
    palette_css,
)

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
    from ui.tokens import PALETTE_DARK, PALETTE_LIGHT

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
    assert "margin-bottom: var(--gap)" in rule
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
    html = ticker_label("brk.b", 'A "B" & C <Co>', name_max="28ch")
    assert "BRK.B" in html
    assert "&quot;B&quot; &amp; C" in html
    assert "&lt;Co&gt;" in html
    assert "--k-tick-max:28ch" in html


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
    # size is a var with a layout fallback so a surface resizes without re-skinning
    assert "var(--k-dot-size, 8px)" in base
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
    assert "top: var(--cc-topbar-h, 0px)" in band
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
# Dimensions denied (design_language §2/§3):
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
#   * test_full_conformance_is_red is xfail(strict): it asserts the quarantine
#     is empty, so it "lands red" today and FLIPS to a hard failure the moment
#     the last surface graduates, forcing this scaffolding to be removed.
# ===========================================================================

# href-safe raw hex: a literal # + 3-8 hex digits, NOT preceded by a word char
# (so "&#8364;" entities and "var(--x" don't trip it), NOT preceded by a quote
# (so href="#dcf" anchor fragments don't read as colors), NOT preceded by % (the
# %23-encoded chevron data-URIs). Catches gradient-internal and var-fallback hex.
_RAW_HEX = re.compile(r"""(?<![\w&%"'])#[0-9a-fA-F]{3,8}(?![0-9a-fA-F])""")
_FONT_SIZE = re.compile(r"font-size:\s*([0-9.]+px)")
_RADIUS_DECL = re.compile(r"border-radius:\s*([^;}]+)")
_PX = re.compile(r"([0-9.]+px)")
_FONT_FAMILY = re.compile(r"font-family:\s*([^;}]+)")
_FONT_TOKEN = re.compile(r"^var\(\s*--(?:sans|serif|mono)\b")
# Legacy alias var-names (longest alternatives first so --ink doesn't shadow
# --ink-muted); the negative lookahead keeps --ink out of --ink-muted etc.
_ALIAS = re.compile(
    r"--(?:panel-alt|panel|bg-card|bg-elev|row-hover|ink-muted|ink|fg-muted|link"
    r"|font-serif|font-mono|font-body)(?![\w-])"
)
# Raw FUNCTION-form colors — rgb()/rgba()/hsl()/hsla(). The hex regex above is
# blind to these, which is exactly how every freehand drop-shadow, status wash,
# and white-wash hover slipped past the color guard across the whole dashboard
# (only the workspace had a bespoke rgba check). They fold into the "color"
# dimension: a surface composes --scrim / --shadow-pop / color-mix(var(--token)),
# never a raw rgba. The (?<![a-z]) lookbehind keeps the "rgb" inside
# "color-mix(in srgb, …)" from matching. tokens.py + charts_v2 stay EXEMPT.
_FUNC_COLOR = re.compile(r"(?<![a-z])(?:rgba?|hsla?)\([^)]*\)", re.IGNORECASE)
# Off-scale font WEIGHT: the kit/scale tops out at 600. 700/800/900/bold is the
# "heavier than the system" drift (design_language §1; the dominant non-guarded
# drift this enforcement closes).
_FONT_WEIGHT = re.compile(r"font-weight:\s*(bold|[789]00)\b")
# transition: all is forbidden — explicit properties only (design_language §3).
_TRANSITION_ALL = re.compile(r"transition:\s*all\b")

# --- COMPONENT drift the five TOKEN dimensions above are BLIND to
# (design_language §4): a surface that hand-rolls a FILLED STATUS BADGE — a
# selector the author NAMED a pill / badge / chip / tag, given an ok/warn/bad
# ``color-mix`` fill — instead of the kit's ``.k-pill``. ok/warn/bad are STATUS
# (never category, decoration, or unread), so the signal is unambiguous: it
# excludes tone WASHES (named differently — .chat-role, .cmt-pin, row tints),
# ACCENT unread/count marks (.ix-badge), and the §2 report CATEGORY tags
# (.qa-tag/.ir-type/.oi-kind — accent, not ok/warn/bad). And it catches the
# base+modifier pill pattern (e.g. ``.x-pill { radius-full } .x-pill.bad
# { color-mix fill }``) because the SELECTOR NAME, not the fill rule, carries the
# badge intent. The token guard passed every reinvented pill this whole sweep
# removed — this is the dimension that finally fails them. ``.k-pill`` /
# ``.k-chip`` / ``.k-well`` are the kit and are excluded (``.p-pill`` was folded
# into ``.k-pill`` — design-sync 2026-07-19). ---
_CSS_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)
_NAMED_BADGE = re.compile(r"[.#][\w-]*(?:pill|badge|chip|tag)\b", re.IGNORECASE)
_KIT_BADGE = re.compile(r"\b(?:k-pill|k-chip|k-well)\b")
_STATUS_FILL = re.compile(r"background(?:-color)?:\s*color-mix\(in srgb, var\(--(?:ok|warn|bad)\)")

DIMENSIONS = (
    "color",
    "font-size",
    "radius",
    "font-family",
    "alias",
    "kit-badge",
    "font-weight",
    "transition",
)

# All CSS-emitting modules (src-relative, posix). The registry IS the contract:
# a filesystem surface missing here — or here but gone from the filesystem —
# fails test_every_css_surface_is_registered until reconciled.
REGISTERED: frozenset[str] = frozenset(
    {
        "dashboard/_styles.py",
        "dashboard/inbox.py",
        "dashboard/upcoming.py",
        "pipeline/advisor_memos_panel.py",
        "pipeline/allocation_decisions_panel.py",
        "pipeline/allocation_recommendation_panel.py",
        "pipeline/analytical_dashboard_html.py",
        "pipeline/attribution_panel.py",
        "pipeline/calibration_scorecard_panel.py",
        "pipeline/cc_action.py",
        "pipeline/cc_overlay.py",
        "pipeline/credibility_panel.py",
        "pipeline/cron_health_panel.py",
        "pipeline/dashboard_html.py",
        "pipeline/data_policy_settings_panel.py",
        "pipeline/dcf_coverage_panel.py",
        "pipeline/dcf_globals_panel.py",
        "pipeline/decision_journal_panel.py",
        "pipeline/diet_panel.py",
        "pipeline/discovery_panel.py",
        "pipeline/etf_workup.py",
        "pipeline/evals_panel.py",
        "pipeline/explore_panel.py",
        "pipeline/fact_overrides_panel.py",
        "pipeline/ir_coverage_panel.py",
        "pipeline/journal_panel.py",
        "pipeline/ledger_panel.py",
        "pipeline/mobile_inbox_panel.py",
        "pipeline/model_eval_panel.py",
        "pipeline/open_loops.py",
        "pipeline/peeks.py",
        "pipeline/portfolio_console_panel.py",
        "pipeline/portfolio_panel.py",
        "pipeline/position_lifecycle_panel.py",
        "pipeline/positioning_panel.py",
        "pipeline/redteam_pnl_panel.py",
        "pipeline/research_cockpit.py",
        "pipeline/restatements_panel.py",
        "pipeline/section_coverage_panel.py",
        "pipeline/senior_partner_brief_panel.py",
        "pipeline/source_calls_panel.py",
        "pipeline/source_viewers.py",
        "pipeline/thesis_ledger_panel.py",
        "pipeline/ticker_command_center.py",
        "pipeline/ticker_settings_panel.py",
        "pipeline/triage_panel.py",
        "pipeline/validation_issues_panel.py",
        "pipeline/work_os_copilot.py",
        "pipeline/work_os_shell.py",
        "pipeline/worldview_panel.py",
        "redteam/brief.py",
        "report/renderers/charts_v2.py",
        "report/renderers/workspace_charts.py",
        "report/renderers/workspace_chat.py",
        "report/renderers/workspace_comments.py",
        "report/renderers/workspace_dcf.py",
        "report/renderers/workspace_sections/chrome.py",
        "report/renderers/workspace_sections/company.py",
        "report/renderers/workspace_sections/thesis_risk.py",
        "report/renderers/workspace_reader_assets.py",
        "report/renderers/workspace_styles.py",
        "ui/cite_marks.py",
        "ui/controls.py",
        "ui/living_grid.py",
        "ui/source_chip.py",
        "ui/tokens.py",
        "viewspec/render.py",
    }
)

# The two sanctioned SOURCES (design_language §1): tokens.py defines the palette
# (the one place raw hex lives); charts_v2 owns SVG chart internals whose
# axis/label sizes + fills are tuned to plot geometry, not the UI scale.
EXEMPT: frozenset[str] = frozenset({"ui/tokens.py", "report/renderers/charts_v2.py"})

# All application and in-app report surfaces now share the same visible type
# scale and radius contracts. Fully exempt token/chart sources remain above.
_FONT_SIZE_EXEMPT: frozenset[str] = frozenset()
_RADIUS_SANCTIONED: dict[str, frozenset[str]] = {}

# Known pre-existing drift, per (surface, dimension), each with its owner. The
# quarantine can only SHRINK (test_quarantine_only_shrinks). Seeded empirically
# from the scanner — NOT hand-curated, so it is the true current state.
QUARANTINE: dict[str, frozenset[str]] = {
    # --- provenance / evals / sources / coverage console — S10 owns these.
    #     S10 rebuilds them onto prov_row/prov_drawer this wave; S7 left them
    #     quarantined rather than race that rebuild. The S7 mechanical sweep
    #     graduated the dashboard / cockpit / pipeline long-tail (_styles,
    #     upcoming, analytical_dashboard_html, ask_dock, dashboard_html,
    #     explore_panel, portfolio_panel, research_cockpit, ticker_command_center,
    #     workspace_sections/chrome) — those came off this map. ---
    # --- cron_health / dcf_coverage / ir_coverage / restatements / source_calls /
    #     source_viewers graduated in the design-language conformance sweep
    #     (2026-06-14): their off-scale 2/3/4/9px corners were snapped to
    #     var(--radius) (boxes/inline code) or var(--radius-full) (dots / pill
    #     badges), so the 'radius' dimension is now clean on each. ---
    # validation_issues_panel graduated in the S10 resolve-wiring pass: its lone
    # off-scale 4px (.vi-note code) moved to var(--radius) when the detail rows
    # were rebuilt onto prov_row. Now fully token-clean.
    # evals_panel graduated in the S10 evals-drawer pass: its failed-case drawer
    # moved onto prov_drawer/prov_case and its score/mode pills onto .k-pill, so
    # the off-scale ev-pill/ev-score-*/ev-mode-* colors + radii + font-sizes were
    # deleted; the surviving run-bar / log / vchip CSS went onto the type/radius
    # tokens. All three dimensions (color/font-size/radius) now clean.
    # (pipeline/command_center_shell.py graduated in S1 PR2 — the shell namespace
    #  unfork; the dashboard / cockpit long-tail graduated in the S7 sweep —
    #  legacy-alias fallbacks, the .calib-fill gradient, the .cockpit-badge tone
    #  wells, 20px close glyphs and 2-4px radii all fixed onto tokens / color-mix.)
    # --- the report iframe / editorial surfaces — serialized S4 → S2-report-
    #     wiring → S10-PR2. The legacy-alias :root that chat / comments / charts /
    #     cite_marks consume is defined in workspace_styles; unforking it is one
    #     coupled change in that chain, not an independent S7 file-sweep. ---
    "report/renderers/workspace_charts.py": frozenset({"radius"}),
    # workspace_chat / workspace_comments / workspace_styles graduated their
    # font-family dimension when the legacy --font-* alias layer was unforked onto
    # the canonical --sans/--serif/--mono tokens (every font-family decl now reads
    # a real token).
    # workspace_chat's lone off-scale 3px code corner moved to var(--radius) in
    # the 2026-06-14 sweep; its 20px close glyph joined the compact display step.
    # UX audit (2026-07-18): workspace_chat / workspace_comments / workspace_styles
    # / ui/cite_marks graduated their `alias` dimension — the shared legacy
    # --panel/--panel-alt/--ink/--ink-muted/--bg-elev/--link :root block in
    # workspace_styles was deleted and every consumer repointed at the canonical
    # tokens (--surface/--paper/--fg/--muted/--accent). Same pass graduated
    # `color` on workspace_chat / workspace_comments / cite_marks: their raw
    # rgba() drop-shadows/washes moved onto var(--shadow-pop) / color-mix(var(--fg) …).
    # workspace_comments graduated its kit-badge dimension in the deferred-items
    # pass: .cmt-outbox-badge / .cmt-health-pill now ride the kit's .k-pill (tone
    # set in JS) with layout/mono-micro refine only. Its 20px close glyph joined
    # the compact display step; radius stays quarantined (report unfork).
    "report/renderers/workspace_comments.py": frozenset({"radius"}),
    # workspace_styles graduated its kit-badge dimension in the same pass: the
    # .decision-badge.outcome-* filled chips moved onto .k-pill + tone (routed in
    # thesis_risk.py). Report-spacing-rhythm pass (2026-08-02): `color`
    # graduated too — the last two raw literals (`.twk-seg button.active` /
    # `.twk-toggle::after` box-shadows, `rgba(0,0,0,.06)` / `rgba(0,0,0,.25)`)
    # moved onto `color-mix(in srgb, var(--scrim) N%, transparent)`, an exact
    # reproduction since `--scrim` is `rgba(0,0,0,0.5)` in both palettes; and
    # the four `border-radius: 999px` literals moved onto `var(--radius-full)`
    # (the token that value already resolves to). `radius` stays quarantined:
    # three small marks (`.legend-swatch` 2px, `.seg-bar` 2px, `.scenario-bar-
    # price` 1px) need a genuinely new sanctioned micro-radius, not a fix —
    # `var(--radius)` (8px) would over-round elements that small. Flagged as a
    # separate follow-up, not attempted here.
    "report/renderers/workspace_styles.py": frozenset({"radius"}),
    # cite_marks graduated `alias` in the same 2026-07-18 sweep (its --panel/--ink/
    # --link fallback chains repointed at the canonical tokens as sole values).
    # color/radius stay quarantined: its `var(--radius, 6px)` / `var(--shadow-pop,
    # 0 8px 24px rgba(...))` literal fallbacks (defensive — this CSS may render on
    # a minimal-token host with no --radius/--shadow-pop defined) still read as a
    # raw 6px radius / raw rgba to the scanner; pre-existing, not touched here.
    "ui/cite_marks.py": frozenset({"color", "radius"}),
    # --- kit-badge (the 2026-06-15 component dimension): all seeded surfaces have
    #     now GRADUATED onto .k-pill — the command-center two (allocation .ad-pill,
    #     position_lifecycle .plc-pill) and the report two (workspace_comments
    #     .cmt-* / workspace_styles .decision-badge) in the deferred-items pass.
    #     No surface carries a kit-badge quarantine: a NEW reinvented filled status
    #     badge in ANY surface now fails CI immediately. ---
}

_STRING_TOKENS = frozenset({_token.STRING})
_SKIP_TOKENS = frozenset(
    {_token.NL, _token.INDENT, _token.DEDENT, _token.COMMENT, _token.ENCODING, _token.ENDMARKER}
)


def _css_text(path: Path) -> str:
    """A module's CSS payload: the contents of its value string-literals,
    EXCLUDING comments and bare-statement docstrings (so prose like "PR #424"
    never reads as a hex color). Both ``X_CSS = "…"`` constants and inline
    ``style="…"`` attributes inside HTML f-strings are captured; comments and
    module/class/function docstrings are not."""
    out: list[str] = []
    depth = 0
    line_start = True  # at the start of a logical line, paren depth 0 → docstring
    with open(path, "rb") as fh:
        try:
            for tok in tokenize.tokenize(fh.readline):
                ttype, tstr = tok.type, tok.string
                if ttype == _token.OP:
                    if tstr in "([{":
                        depth += 1
                    elif tstr in ")]}":
                        depth = max(0, depth - 1)
                if ttype == _token.NEWLINE:
                    line_start = True
                    continue
                if ttype in _SKIP_TOKENS:
                    continue
                is_fstring = tokenize.tok_name.get(ttype, "").startswith("FSTRING")
                if ttype in _STRING_TOKENS and line_start and depth == 0:
                    line_start = False  # a bare string statement → a docstring → skip
                    continue
                if ttype in _STRING_TOKENS or is_fstring:
                    out.append(tstr)
                line_start = False
        except tokenize.TokenError:
            pass
    return "\n".join(out)


def _split_top_commas(value: str) -> list[str]:
    """Split a CSS value on top-level commas only — commas inside ``var(...)``
    stay put, so ``var(--mono, monospace)`` is ONE family, not two tokens."""
    parts: list[str] = []
    depth = 0
    cur: list[str] = []
    for ch in value:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    parts.append("".join(cur))
    return [p.strip() for p in parts if p.strip()]


def scan_surface(rel: str, text: str) -> dict[str, list[str]]:
    """Token-conformance violations of one surface's CSS text, keyed by
    dimension. Applies the §1 per-surface exemptions. Pure function — the tests
    decide what to do with the result (enforce / ratchet)."""
    if rel in EXEMPT:
        return {}
    found: dict[str, list[str]] = {}

    colors = set(_RAW_HEX.findall(text)) | set(_FUNC_COLOR.findall(text))
    if colors:
        found["color"] = sorted(colors)

    if rel not in _FONT_SIZE_EXEMPT:
        sizes = [m for m in _FONT_SIZE.findall(text) if m not in TYPE_SCALE_PX]
        if sizes:
            found["font-size"] = sorted(set(sizes))

    ok_radii = RADIUS_PX | _RADIUS_SANCTIONED.get(rel, frozenset())
    radii = [
        px for val in _RADIUS_DECL.findall(text) for px in _PX.findall(val) if px not in ok_radii
    ]
    if radii:
        found["radius"] = sorted(set(radii))

    families: list[str] = []
    for val in _FONT_FAMILY.findall(text):
        for part in _split_top_commas(val):
            if part in FONT_FAMILY_KEYWORDS or _FONT_TOKEN.match(part):
                continue
            families.append(part)
    if families:
        found["font-family"] = sorted(set(families))

    aliases = sorted({m.group(0) for m in _ALIAS.finditer(text)})
    if aliases:
        found["alias"] = aliases

    # Component drift: a reinvented filled status badge (see the regexes above).
    # CSS comments are stripped first so a /* … */ aside never reads as a rule.
    badges: list[str] = []
    for rule in re.split(r"(?<=})\s*", _CSS_COMMENT.sub("", text)):
        head, _, body = rule.partition("{")
        named = _NAMED_BADGE.search(head)
        if named and not _KIT_BADGE.search(head) and _STATUS_FILL.search(body):
            badges.append(named.group(0))
    if badges:
        found["kit-badge"] = sorted(set(badges))

    weights = _FONT_WEIGHT.findall(text)
    if weights:
        found["font-weight"] = sorted(set(weights))

    if _TRANSITION_ALL.search(text):
        found["transition"] = ["all"]

    return found


def _discovered_surfaces() -> set[str]:
    """Every CSS-emitting module under src/, by the ``var(--`` signal."""
    return {
        p.relative_to(SRC).as_posix()
        for p in SRC.rglob("*.py")
        if "var(--" in p.read_text(encoding="utf-8")
    }


def test_every_css_surface_is_registered() -> None:
    """Opt-OUT discovery: the filesystem, not a curated import list, defines the
    surface set. A new CSS-emitting module that no one classified — or a removed
    one still listed — fails here until reconciled with REGISTERED + (EXEMPT |
    QUARANTINE | clean)."""
    discovered = _discovered_surfaces()
    new_unregistered = discovered - REGISTERED
    stale_registered = REGISTERED - discovered
    assert not new_unregistered, (
        "new CSS-emitting surface(s) not registered — add to REGISTERED and "
        f"classify (clean / EXEMPT / QUARANTINE): {sorted(new_unregistered)}"
    )
    assert not stale_registered, (
        f"REGISTERED lists modules that no longer emit CSS: {sorted(stale_registered)}"
    )


def test_no_unquarantined_token_drift() -> None:
    """Every clean surface — and every non-quarantined DIMENSION of a
    quarantined surface — must be free of denied literals. This is the live
    enforcement: the moment a clean surface (or clean dimension) gains raw hex,
    an off-scale size/radius, a stray font, a legacy alias, or a reinvented
    status badge (kit-badge), CI fails."""
    offenders: dict[str, dict[str, list[str]]] = {}
    for rel in REGISTERED - EXEMPT:
        violations = scan_surface(rel, _css_text(SRC / rel))
        tolerated = QUARANTINE.get(rel, frozenset())
        live = {dim: vals for dim, vals in violations.items() if dim not in tolerated}
        if live:
            offenders[rel] = live
    assert not offenders, (
        "design-language drift in non-quarantined surface(s)/dimension(s). Fix the "
        "rendered output, do NOT add to QUARANTINE:\n"
        "  · color / font-size / radius / font-family / alias → use the token "
        "(tokens.py) / color-mix / on-scale value / canonical var name.\n"
        "  · kit-badge → you hand-rolled a FILLED STATUS PILL; use the kit's "
        "`.k-pill` (+ `.k-pill-ok/-warn/-bad`) from ui/controls.py, never a "
        "`color-mix(var(--ok|warn|bad))` background on your own .*-pill/-badge "
        f"class (design_language §4).\n{offenders}"
    )


def test_quarantine_only_shrinks() -> None:
    """The ratchet: a quarantined (surface, dimension) that is now CLEAN must be
    graduated out of QUARANTINE — leaving stale entries would let real drift
    hide behind a no-longer-true exemption. Also keeps QUARANTINE honest
    (registered, non-exempt)."""
    graduated: dict[str, list[str]] = {}
    for rel, dims in QUARANTINE.items():
        assert rel in REGISTERED, f"quarantined surface not registered: {rel}"
        assert rel not in EXEMPT, f"quarantined surface is also EXEMPT: {rel}"
        violations = scan_surface(rel, _css_text(SRC / rel))
        clean_now = sorted(d for d in dims if d not in violations)
        if clean_now:
            graduated[rel] = clean_now
    assert not graduated, (
        "these surfaces are now clean in the listed dimension(s) — remove them "
        f"from QUARANTINE (the ratchet only shrinks): {graduated}"
    )


def test_sanctioned_escapes_survive() -> None:
    """Each design_language §1 escape must NOT be flagged — the guard would be
    wrong to deny them."""
    # charts_v2 + tokens are fully exempt sources.
    assert (
        scan_surface(
            "report/renderers/charts_v2.py", _css_text(SRC / "report/renderers/charts_v2.py")
        )
        == {}
    )
    assert scan_surface("ui/tokens.py", _css_text(SRC / "ui/tokens.py")) == {}
    # Source chips and the workspace report now use the shared four-step scale.
    assert "font-size" not in scan_surface(
        "ui/source_chip.py", _css_text(SRC / "ui/source_chip.py")
    )
    assert "radius" not in scan_surface("ui/source_chip.py", _css_text(SRC / "ui/source_chip.py"))
    assert "font-size" not in scan_surface(
        "report/renderers/workspace_styles.py",
        _css_text(SRC / "report/renderers/workspace_styles.py"),
    )
    ws = _css_text(SRC / "report/renderers/workspace_styles.py")
    assert "font-size: 60px" not in ws and "font-size: 100px" not in ws
    # The chevron data-URIs (%23-encoded) are not colors; the kit stays clean.
    assert scan_surface("ui/controls.py", _css_text(SRC / "ui/controls.py")) == {}
    # 0.93em inline mono is an em, never a px font-size — naturally unflagged.
    assert scan_surface("x", "code { font-size: 0.93em; }") == {}


def test_func_color_dimension_catches_raw_rgba_hsl() -> None:
    """The color dimension now also denies rgb()/rgba()/hsl()/hsla() — the gap
    that hid every freehand shadow/wash/hover. It must NOT false-fire on the
    ``rgb`` inside ``color-mix(in srgb, …)`` (the sanctioned token idiom)."""
    assert "rgba(0, 0, 0, 0.3)" in scan_surface(
        "x", ".a { box-shadow: 0 2px 4px rgba(0, 0, 0, 0.3); }"
    ).get("color", [])
    assert "rgba(255,255,255,0.04)" in scan_surface(
        "x", ".a:hover { background: rgba(255,255,255,0.04); }"
    ).get("color", [])
    assert scan_surface("x", ".a { color: hsl(210, 50%, 40%); }").get("color")
    # NEGATIVE: the kit's color-mix idiom (note the "rgb" inside "srgb") is clean.
    assert "color" not in scan_surface(
        "x", ".a { background: color-mix(in srgb, var(--bad) 16%, transparent); }"
    )
    # NEGATIVE: a tokenized scrim / shadow carries no raw color.
    assert scan_surface("x", ".k-scrim { background: var(--scrim); }") == {}


def test_font_weight_and_transition_dimensions() -> None:
    """font-weight 700/800/900/bold (the kit tops out at 600) and ``transition:
    all`` (explicit properties only, §3) are denied; 600 and explicit-prop
    transitions are clean."""
    assert scan_surface("x", ".a { font-weight: 700; }")["font-weight"] == ["700"]
    assert scan_surface("x", ".a { font-weight: bold; }")["font-weight"] == ["bold"]
    assert "font-weight" not in scan_surface("x", ".a { font-weight: 600; }")
    assert scan_surface("x", ".a { transition: all 150ms ease; }")["transition"] == ["all"]
    assert "transition" not in scan_surface(
        "x", ".a { transition: color var(--transition), border-color var(--transition); }"
    )


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
_BUTTON_TAG = re.compile(r"<button\b[^>]*>", re.IGNORECASE)
_CLASS_ATTR = re.compile(r'class="([^"]*)"')
#: A button is kit-composed if any of its classes is one of these.
_KIT_BUTTON = frozenset({"k-btn", "k-chip"})
#: Sanctioned + grandfathered bespoke button classes — the QUARANTINE analogue
#: for §4 buttons (seeded from the current emitted set). Close glyphs (§3), tabs,
#: the icon theme-toggle / ⌘K launcher, and the §4.1 doorway are sanctioned
#: bespoke controls; the rest are grandfathered current-state to migrate
#: opportunistically. The check's job is to stop NET-NEW reinvented buttons — a
#: button whose class is none of these and not kit fails.
_BESPOKE_BUTTON_OK = frozenset(
    {
        # close-glyph dismiss buttons (§3): drawers / peeks / palette / chat / comments
        "cc-drawer-close",
        "tcc-drawer-close",
        "cc-peek-close",
        "cc-palette-close",
        "tri-d-close",
        "chat-close",
        "cmt-close",
        # the Ask DIY-builder popover close glyph (explore_panel.py) — was
        # classless chrome on `.ask-pop-head button` until the guard-extension
        # wave gave it a named class matching this same close-glyph family.
        "ask-pop-close",
        # icon-only launcher + theme toggle (§4 specialized-control carve-out)
        "cc-palette-btn",
        "cc-theme-toggle",
        # bespoke tab control
        "cc-tab",
        # the doorway: a datum rendered as a label that opens Ask (§4.1)
        "fact-doorway",
        # the earnings-prep memo's watch-item doorway (Wave 2): the owner's own
        # note text rendered as a line that opens Ask — same §4.1 shape as
        # fact-doorway / up-watch-item, not a skinned button.
        "prep-ask",
        # specialized Ask-dock control cluster
        "ask-dock-ctl",
        # grandfathered bespoke interactive rows / text buttons (migrate later)
        "up-watch-item",
        "tri-text",
        "dq-peek",
    }
)


def _emitted_button_classes(text: str) -> list[set[str]]:
    """Class token-sets of every ``<button>`` emitted in a source file. Skips
    classless buttons (JS-wired/structural — out of scope) and dynamic classes
    (``{…}`` / string concatenation — unverifiable statically, kit in practice)."""
    out: list[set[str]] = []
    for tag in _BUTTON_TAG.findall(text):
        m = _CLASS_ATTR.search(tag)
        if not m:
            continue
        cls = m.group(1)
        if "{" in cls or "+" in cls:
            continue
        out.append(set(cls.split()))
    return out


def test_buttons_compose_the_kit() -> None:
    """Every emitted ``<button>`` carries a kit button/chip class or an
    allowlisted bespoke control — a reinvented button cannot ship (design_language
    §4). Scans ALL of ``src`` (buttons live in markup files, not only the
    CSS-emitting surfaces). Seeded green; a net-new freehand button fails here."""
    offenders: dict[str, list[str]] = {}
    for path in sorted(SRC.rglob("*.py")):
        for classes in _emitted_button_classes(path.read_text(encoding="utf-8")):
            if classes & _KIT_BUTTON or classes & _BESPOKE_BUTTON_OK:
                continue
            offenders.setdefault(path.relative_to(SRC).as_posix(), []).append(
                " ".join(sorted(classes))
            )
    assert not offenders, (
        "reinvented <button>(s) — compose the kit (.k-btn / .k-chip) instead of a "
        "freehand class. A genuinely-bespoke control (close glyph, segmented "
        "selector, …) is justified into _BESPOKE_BUTTON_OK, not skinned ad hoc:\n"
        + "\n".join(f"  {rel}: {sorted(set(v))}" for rel, v in offenders.items())
    )


def test_button_coverage_fires_on_reinvention() -> None:
    """The Layer-B check's own contract: it flags a freehand button, passes kit
    + allowlisted ones, and ignores dynamic / classless buttons."""
    freehand = _emitted_button_classes('<button class="my-save-btn" type="button">Save</button>')
    assert freehand == [{"my-save-btn"}]
    assert not (freehand[0] & _KIT_BUTTON or freehand[0] & _BESPOKE_BUTTON_OK)  # → flagged
    assert _emitted_button_classes('<button class="k-btn k-btn-quiet">x</button>')[0] & _KIT_BUTTON
    assert _emitted_button_classes('<button class="cc-tab">x</button>')[0] & _BESPOKE_BUTTON_OK
    assert _emitted_button_classes('<button class="{cls}">x</button>') == []  # dynamic → skipped
    assert _emitted_button_classes("<button type='button'>x</button>") == []  # classless → skipped


def test_kit_badge_flags_reinvented_status_pills_only() -> None:
    """The COMPONENT dimension (design_language §4): a selector NAMED a
    pill/badge/chip/tag carrying an ok/warn/bad ``color-mix`` fill is a reinvented
    filled status pill — token-clean, so the five token dimensions never see it;
    this is the check that fails it. It must fire ONLY there — never on tone
    washes, accent unread/count marks, §2 report category tags, or the kit."""
    # FIRES: a hand-rolled filled status pill — including the base+modifier split
    # (the radius is on the base rule, the tone fill on a modifier rule).
    assert scan_surface(
        "x",
        ".x-pill { border-radius: var(--radius-full); }\n"
        ".x-pill.bad { background: color-mix(in srgb, var(--bad) 16%, transparent); }",
    )["kit-badge"] == [".x-pill"]
    assert "kit-badge" in scan_surface(
        "x",
        ".foo-badge { background: color-mix(in srgb, var(--ok) 16%, transparent); color: var(--ok); }",
    )
    # DOES NOT fire on a tone WASH (not named a badge/pill/chip/tag) …
    assert "kit-badge" not in scan_surface(
        "x", ".chat-role-user { background: color-mix(in srgb, var(--ok) 7%, transparent); }"
    )
    # … an ACCENT unread/count mark (accent is not a status tone) …
    assert "kit-badge" not in scan_surface(
        "x", ".ix-badge { background: var(--accent); color: var(--accent-contrast); }"
    )
    # … a §2 report CATEGORY tag (accent-soft, not ok/warn/bad) …
    assert "kit-badge" not in scan_surface(
        "x", ".qa-tag { background: var(--accent-soft); color: var(--accent); }"
    )
    # … the kit's OWN tone fills (.k-pill / .k-well are the canonical source) …
    assert "kit-badge" not in scan_surface(
        "x", ".k-pill-bad { background: color-mix(in srgb, var(--bad) 16%, transparent); }"
    )
    # … and a /* commented-out */ rule never reads as a live one.
    assert "kit-badge" not in scan_surface(
        "x", "/* .old-pill { background: color-mix(in srgb, var(--bad) 16%, transparent); } */"
    )


def test_scope_is_color_font_radius_never_layout() -> None:
    """Dimension scoping: layout px (width/margin/padding/gap/top/left/height)
    is never a violation; color/size/radius — in CSS rules AND inline style — is.
    This is the separate inline-style scan, folded into one pass."""
    assert (
        scan_surface("x", ".a { width: 60px; margin: 12px; gap: 7px; top: 3px; height: 40px; }")
        == {}
    )
    assert scan_surface("x", ".a { padding: 5px 9px; left: 4px; }") == {}
    # color / off-scale size / off-scale radius in a CSS rule → flagged.
    assert scan_surface("x", ".a { color: #ff0000; }")["color"] == ["#ff0000"]
    assert scan_surface("x", ".a { font-size: 19px; }")["font-size"] == ["19px"]
    assert scan_surface("x", ".a { border-radius: 4px; }")["radius"] == ["4px"]
    # an alien font literal and a legacy alias → flagged.
    assert "font-family" in scan_surface("x", ".a { font-family: 'Roboto', sans-serif; }")
    assert "alias" in scan_surface("x", ".a { color: var(--ink); }")
    # var(--mono, monospace) is ONE sanctioned family, not drift.
    assert "font-family" not in scan_surface("x", ".a { font-family: var(--mono, monospace); }")
    # INLINE style: a color is caught, a layout width is not.
    assert scan_surface("x", '<i style="color:#abc123"></i>')["color"] == ["#abc123"]
    assert scan_surface("x", '<i style="width:60px;font-size:19px"></i>')["font-size"] == ["19px"]
    # href fragments that look like hex are NOT colors.
    assert scan_surface("x", '<a href="#dcf">x</a>') == {}


@pytest.mark.xfail(strict=True, reason="quarantine non-empty until S7 sweep + report unfork land")
def test_full_conformance_is_red() -> None:
    """The guard 'lands red': full token conformance is not yet reached. This
    xfail FLIPS to a hard failure (strict) the moment QUARANTINE empties,
    forcing the scaffolding to be torn out. There is no --ignore bypass."""
    assert not QUARANTINE, f"still quarantined: {sorted(QUARANTINE)}"


def test_workspace_local_css_standardizes_primitives() -> None:
    """The report's local CSS uses shared color, type, and control primitives."""
    from report.renderers.workspace_styles import CSS as WS_CSS
    from ui.controls import controls_css as _ccss
    from ui.tokens import palette_css as _pcss

    local = WS_CSS.replace(_pcss("paper"), "").replace(_ccss("paper"), "")
    assert not _RAW_HEX.search(local), f"raw hex in workspace local CSS: {_RAW_HEX.findall(local)}"
    assert "rgba(" not in local.replace("rgba(0,0,0", "").replace("rgba(0, 0, 0", ""), (
        "non-neutral rgba wash in workspace local CSS"
    )
    assert "font-size: 60px" not in local and "font-size: 100px" not in local
    assert "font-size: 8.5px" not in local


def test_combobox_renders_two_part_ticker_labels() -> None:
    """The holding combobox keeps ticker value and company label separate."""
    from pipeline.ticker_command_center import _combobox  # pyright: ignore[reportPrivateUsage]

    combo = _combobox("NU", "Nu Holdings Ltd.")
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
_FONT_SIZE_DECL = re.compile(r"(?:^|[;\s])font-size\s*:")


def _inputrow_font_rules(css: str) -> list[str]:
    """Find input-row font declarations without cross-surface backtracking."""
    hits: list[str] = []
    for chunk in css.split("}"):
        selector, separator, body = chunk.rpartition("{")
        if separator and "-inputrow" in selector and _FONT_SIZE_DECL.search(body):
            hits.append(f"{selector}{{{body}}}")
    return hits


def test_no_font_size_on_inputrow_controls() -> None:
    """Guard #2: an Ask/DIY-style ``*-inputrow`` row pins no font-size on its
    input or buttons — they inherit the kit baseline (--fs-body, 13px) so the
    input matches the ``.k-btn`` buttons beside it and the mobile 16px floor
    (controls.py) is never overridden by a panel rule. Scanned over every
    registered CSS surface, not just the Ask panel."""
    offenders: dict[str, list[str]] = {}
    for rel in sorted(REGISTERED - EXEMPT):
        hits = _inputrow_font_rules(_css_text(SRC / rel))
        if hits:
            offenders[rel] = [" ".join(h.split()) for h in hits]
    assert not offenders, (
        "a *-inputrow control pins a font-size — drop it so the control inherits "
        f"the kit baseline (--fs-body, matching the .k-btn buttons): {offenders}"
    )


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
    from ui.tokens import PALETTE_DARK, PALETTE_LIGHT

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


def test_footnote_marker_scales_with_its_prose() -> None:
    """``.k-fn`` sizes in em on purpose: a reference mark rides the text it
    annotates, and prose runs at a different size per surface. A px step here
    would make the marker the wrong size in exactly the place it is used."""
    css = controls_css("paper")
    rule = re.search(r"\.k-fn\s*\{([^}]*)\}", css)
    assert rule is not None
    assert re.search(r"font-size:\s*[0-9.]+em", rule.group(1))


def test_note_rail_does_not_eat_the_reading_measure() -> None:
    """The document width is the measure PLUS the note rail, never a flat cap.

    Regression pin for a bug caught by browser measurement, not by review:
    ``.k-doc`` was a flat ``max-width: 76ch``, so in any section carrying a
    margin note the 13.5rem rail was subtracted from the inside and prose
    collapsed to ~40ch — unreadable, in exactly the sections that matter most.

    Deriving the width from ``--k-measure`` keeps prose at its measure whether
    or not a section is annotated, and lets full-bleed sections use the whole
    width. Verified in-browser at 1280px: prose 66ch, band full-bleed at 703px.
    """
    css = controls_css("paper")
    doc_rule = re.search(r"\.k-doc\s*\{([^}]*)\}", css)
    assert doc_rule is not None
    body = doc_rule.group(1)
    assert "--k-measure" in body, ".k-doc must define the reading measure"
    max_width = re.search(r"max-width:\s*([^;]+)", body)
    assert max_width is not None
    expr = max_width.group(1)
    assert expr.strip().startswith("calc("), (
        f"max-width is {expr!r} — a flat cap lets the note rail eat the measure"
    )
    assert "var(--k-measure)" in expr and "var(--k-note-w" in expr

    # Prose is capped independently, so a full-bleed section does not run long.
    prose_cap = re.search(r"\.k-doc\s+\.prose\s*\{([^}]*)\}", css)
    assert prose_cap is not None, ".k-doc .prose must cap at the measure"
    assert "var(--k-measure)" in prose_cap.group(1)


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
    assert re.search(r"--panel-pad-x:\s*0", body), (
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


def test_overlay_head_buttons_are_named_not_classless() -> None:
    """Every ``<button>`` inside an ``ask-pop-head`` / ``cc-drawer-head`` /
    ``cc-peek-head`` / ``cite-pop-head`` / ``tcc-drawer-head`` / ``ask-dock-
    head`` overlay head carries a class. Passes on the current tree — the
    explore_panel ``ask-pop-close`` fix (this same change) removed the one
    classless close button that used to hang off ``.ask-pop-head button``."""
    offenders: dict[str, list[str]] = {}
    for rel in sorted(REGISTERED - EXEMPT):
        hits = _classless_buttons_in_overlay_heads((SRC / rel).read_text(encoding="utf-8"))
        if hits:
            offenders[rel] = hits
    assert not offenders, (
        "classless <button> inside a *-head overlay container — give it a named, "
        f"styled class (see .ask-pop-close / .cc-peek-close): {offenders}"
    )


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
_BARE_TABLE_SELECTOR = re.compile(r"^\.[\w-]*table[\w-]*$")
# .pfc-table (portfolio_panel.py ~1564, the pairwise correlation matrix) is a
# DOCUMENTED deliberate keep: its headers ARE tickers, not prose labels, so
# whole-table mono is the correct read there, not label/header drift. There
# is no class literally named ".matrix" anywhere in the tree — don't allowlist
# a name that doesn't exist.
_MONO_TABLE_ALLOWLIST: frozenset[str] = frozenset({".pfc-table"})


def _whole_table_mono_selectors(text: str) -> list[str]:
    """Bare table-level selectors (no td/th qualifier) whose rule body pins
    ``font-family: var(--mono...)``. Mirrors this file's existing rule-split
    idiom (split CSS text on ``}``, partition each chunk on ``{``) rather than
    a full CSS parse — consistent with ``scan_surface``'s kit-badge scan
    above, and sufficient for the flat (non-nested) rule shapes every
    registered surface actually emits."""
    offenders: list[str] = []
    for rule in _CSS_COMMENT.sub("", text).split("}"):
        head, sep, body = rule.partition("{")
        if not sep or "var(--mono" not in body:
            continue
        for sel in _split_top_commas(head):
            sel = sel.strip()
            if sel in _MONO_TABLE_ALLOWLIST:
                continue
            if _BARE_TABLE_SELECTOR.match(sel):
                offenders.append(sel)
    return offenders


def test_no_whole_table_mono_outside_the_documented_allowlist() -> None:
    """No bare table-level selector pins mono across the whole table (which
    would also mono the label/header column) except the documented
    ``.pfc-table`` keep. Passes on the current tree — source_viewers'
    ``.sv-stmt-table`` (this same change) was rescoped to
    ``.sv-stmt-table td:not(:first-child)`` per the ``.vx-matrix td``
    pattern."""
    offenders: dict[str, list[str]] = {}
    for rel in sorted(REGISTERED - EXEMPT):
        hits = _whole_table_mono_selectors(_css_text(SRC / rel))
        if hits:
            offenders[rel] = hits
    assert not offenders, (
        "whole-table mono outside the documented .pfc-table keep — rescope to "
        f"value cells only (the .vx-matrix td pattern): {offenders}"
    )


def test_whole_table_mono_check_fires_on_known_violation() -> None:
    """Self-test: the exact pre-fix rule source_viewers.py used to emit
    (`.sv-stmt-table { font-family: var(--mono); ... }`, unqualified) —
    proving the guard detects it. The rescoped post-fix rule
    (`.sv-stmt-table td:not(:first-child)`) must come back clean, and the
    documented `.pfc-table` allowlist entry must never fire."""
    pre_fix = ".sv-stmt-table { border-collapse: collapse; font-family: var(--mono); }"
    assert _whole_table_mono_selectors(pre_fix) == [".sv-stmt-table"]

    post_fix = (
        ".sv-stmt-table { border-collapse: collapse; }\n"
        ".sv-stmt-table td:not(:first-child) { font-family: var(--mono); }"
    )
    assert _whole_table_mono_selectors(post_fix) == []

    allowlisted = ".pfc-table { border-collapse: collapse; font-family: var(--mono); }"
    assert _whole_table_mono_selectors(allowlisted) == []

    # A cell-scoped selector sharing a rule with something else must not
    # false-fire either (comma-split selector list).
    shared = ".vx-matrix td, .cv2-matrix td { font-family: var(--mono); }"
    assert _whole_table_mono_selectors(shared) == []


# ---------------------------------------------------------------------------
# Guard (c): CCAction adopter census — a REGRESSION-ONLY ratchet.
#
# CCAction.busy()/.release()/.receipt()/.leave() (PR #1092) is the busy/
# release/receipt/leave feedback primitive every action button should use
# instead of bare `disabled=true` / instant `.remove()`. This does NOT force
# adoption on a surface that never used it — that would be a mandate, not a
# regression guard, and plenty of legitimate non-button surfaces (this file's
# own REGISTERED set included) have no business referencing it. It only pins
# the CURRENT adopters and fails if one of THEM drops to zero references,
# i.e. a conformant surface silently regressed to the pre-#1092 pattern.
# ---------------------------------------------------------------------------

# Pinned via `grep -rl "CCAction.busy" src` at the time this ratchet was
# added. Growing this set (a NEW adopter) is always fine and expected over
# time; the ratchet only ever tightens by CI failing a member that vanishes.
_CCACTION_PINNED: frozenset[str] = frozenset(
    {
        "dashboard/inbox.py",
        "pipeline/advisor_memos_panel.py",
        "pipeline/allocation_decisions_panel.py",
        "pipeline/allocation_recommendation_panel.py",
        "pipeline/cc_action.py",
        "pipeline/dcf_globals_panel.py",
        "pipeline/decision_journal_panel.py",
        "pipeline/diet_panel.py",
        "pipeline/discovery_panel.py",
        "pipeline/evals_panel.py",
        "pipeline/explore_panel.py",
        "pipeline/journal_panel.py",
        "pipeline/ledger_panel.py",
        "pipeline/mobile_inbox_panel.py",
        "pipeline/peeks.py",
        "pipeline/portfolio_panel.py",
        "pipeline/position_lifecycle_panel.py",
        "pipeline/positioning_panel.py",
        "pipeline/ticker_command_center.py",
        "pipeline/ticker_settings_panel.py",
        "pipeline/triage_panel.py",
        "pipeline/validation_issues_panel.py",
        "pipeline/worldview_panel.py",
        "redteam/brief.py",
        "report/renderers/workspace_comments.py",
        "report/renderers/workspace_dcf.py",
        "report/renderers/workspace_decision_card.py",
        "ui/controls.py",
    }
)


def _ccaction_regressions(pinned: frozenset[str], read_text: Callable[[str], str]) -> list[str]:
    """Members of ``pinned`` whose current text no longer references
    ``CCAction.busy`` — i.e. a conformant surface regressed. Takes a
    ``read_text`` callable (rather than reading files itself) so the
    detection logic is unit-testable against synthetic text without a real
    filesystem round-trip."""
    return [rel for rel in sorted(pinned) if "CCAction.busy" not in read_text(rel)]


def test_ccaction_adopters_do_not_regress() -> None:
    """The ratchet: none of the pinned CCAction adopters may drop to zero
    ``CCAction.busy`` references. Passes today (the pinned set is exactly
    today's adopters); it exists to catch tomorrow's regression."""
    offenders = _ccaction_regressions(
        _CCACTION_PINNED, lambda rel: (SRC / rel).read_text(encoding="utf-8")
    )
    assert not offenders, (
        "CCAction adopter(s) regressed to zero CCAction.busy references — restore "
        f"the busy/release/receipt/leave wiring (PR #1092): {offenders}"
    )


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
