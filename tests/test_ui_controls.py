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
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC = PROJECT_ROOT / "src"
sys.path.insert(0, str(SRC))

from ui.controls import (  # noqa: E402
    controls_css,
    panel_section_title,
    panel_toolbar,
    ticker_label,
)
from ui.tokens import (  # noqa: E402
    FONT_FAMILY_KEYWORDS,
    RADIUS_PX,
    TYPE_SCALE_PX,
)

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

DIMENSIONS = ("color", "font-size", "radius", "font-family", "alias")

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
        "pipeline/analytical_dashboard_html.py",
        "pipeline/ask_dock.py",
        "pipeline/attribution_panel.py",
        "pipeline/cc_overlay.py",
        "pipeline/command_center_shell.py",
        "pipeline/cron_health_panel.py",
        "pipeline/dashboard_html.py",
        "pipeline/dcf_coverage_panel.py",
        "pipeline/diet_panel.py",
        "pipeline/discovery_panel.py",
        "pipeline/evals_panel.py",
        "pipeline/explore_panel.py",
        "pipeline/ir_coverage_panel.py",
        "pipeline/journal_panel.py",
        "pipeline/peeks.py",
        "pipeline/portfolio_panel.py",
        "pipeline/position_lifecycle_panel.py",
        "pipeline/research_cockpit.py",
        "pipeline/restatements_panel.py",
        "pipeline/section_coverage_panel.py",
        "pipeline/source_calls_panel.py",
        "pipeline/source_viewers.py",
        "pipeline/thesis_ledger_panel.py",
        "pipeline/ticker_command_center.py",
        "pipeline/ticker_settings_panel.py",
        "pipeline/validation_issues_panel.py",
        "report/renderers/charts_v2.py",
        "report/renderers/workspace_charts.py",
        "report/renderers/workspace_chat.py",
        "report/renderers/workspace_comments.py",
        "report/renderers/workspace_sections/chrome.py",
        "report/renderers/workspace_sections/thesis_risk.py",
        "report/renderers/workspace_styles.py",
        "ui/cite_marks.py",
        "ui/controls.py",
        "ui/source_chip.py",
        "ui/tokens.py",
        "viewspec/render.py",
    }
)

# The two sanctioned SOURCES (design_language §1): tokens.py defines the palette
# (the one place raw hex lives); charts_v2 owns SVG chart internals whose
# axis/label sizes + fills are tuned to plot geometry, not the UI scale.
EXEMPT: frozenset[str] = frozenset({"ui/tokens.py", "report/renderers/charts_v2.py"})

# §1 escapes that relax a single dimension on a single surface:
#   * workspace_styles' editorial reading/display ramp (15/28/60/100px …) — type
#     is "deliberately surface-specific"; its non-type primitives still conform.
#   * source_chip's 8.5px .src-chip mark.
_FONT_SIZE_EXEMPT: frozenset[str] = frozenset(
    {"report/renderers/workspace_styles.py", "ui/source_chip.py"}
)
#   * the 3px .src-chip corner (size AND corner sanctioned at that scale).
_RADIUS_SANCTIONED: dict[str, frozenset[str]] = {
    "report/renderers/workspace_styles.py": frozenset({"3px"}),
    "ui/source_chip.py": frozenset({"3px"}),
}

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
    "pipeline/cron_health_panel.py": frozenset({"radius"}),
    "pipeline/dcf_coverage_panel.py": frozenset({"radius"}),
    "pipeline/evals_panel.py": frozenset({"color", "font-size", "radius"}),
    "pipeline/ir_coverage_panel.py": frozenset({"radius"}),
    "pipeline/restatements_panel.py": frozenset({"radius"}),
    "pipeline/source_calls_panel.py": frozenset({"radius"}),
    "pipeline/source_viewers.py": frozenset({"radius"}),
    # validation_issues_panel graduated in the S10 resolve-wiring pass: its lone
    # off-scale 4px (.vi-note code) moved to var(--radius) when the detail rows
    # were rebuilt onto prov_row. Now fully token-clean.
    # (pipeline/command_center_shell.py graduated in S1 PR2 — the shell namespace
    #  unfork; the dashboard / cockpit long-tail graduated in the S7 sweep —
    #  legacy-alias fallbacks, the .calib-fill gradient, the .cockpit-badge tone
    #  wells, 20px close glyphs and 2-4px radii all fixed onto tokens / color-mix.)
    # --- the report iframe / editorial surfaces — serialized S4 → S2-report-
    #     wiring → S10-PR2. The legacy-alias :root that chat / comments / charts /
    #     cite_marks consume is defined in workspace_styles; unforking it is one
    #     coupled change in that chain, not an independent S7 file-sweep. ---
    "report/renderers/workspace_charts.py": frozenset({"radius"}),
    "report/renderers/workspace_chat.py": frozenset(
        {"alias", "font-family", "font-size", "radius"}
    ),
    "report/renderers/workspace_comments.py": frozenset(
        {"alias", "font-family", "font-size", "radius"}
    ),
    "report/renderers/workspace_styles.py": frozenset({"alias", "font-family", "radius"}),
    "ui/cite_marks.py": frozenset({"alias", "radius"}),
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

    hexes = _RAW_HEX.findall(text)
    if hexes:
        found["color"] = sorted(set(hexes))

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
    an off-scale size/radius, a stray font, or a legacy alias, CI fails."""
    offenders: dict[str, dict[str, list[str]]] = {}
    for rel in REGISTERED - EXEMPT:
        violations = scan_surface(rel, _css_text(SRC / rel))
        tolerated = QUARANTINE.get(rel, frozenset())
        live = {dim: vals for dim, vals in violations.items() if dim not in tolerated}
        if live:
            offenders[rel] = live
    assert not offenders, (
        "token-conformance drift in non-quarantined surface(s)/dimension(s). "
        "Fix the CSS (tokens / color-mix / on-scale size / --radius / canonical "
        f"var names) — do not add to QUARANTINE: {offenders}"
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
    # source_chip's 8.5px + 3px micro-mark is clean by sanction.
    assert "font-size" not in scan_surface(
        "ui/source_chip.py", _css_text(SRC / "ui/source_chip.py")
    )
    assert "radius" not in scan_surface("ui/source_chip.py", _css_text(SRC / "ui/source_chip.py"))
    # workspace reading/display ramp (60px/100px …) survives — type only.
    assert "font-size" not in scan_surface(
        "report/renderers/workspace_styles.py",
        _css_text(SRC / "report/renderers/workspace_styles.py"),
    )
    ws = _css_text(SRC / "report/renderers/workspace_styles.py")
    assert "60px" in ws and "100px" in ws  # the ramp really is present
    # The chevron data-URIs (%23-encoded) are not colors; the kit stays clean.
    assert scan_surface("ui/controls.py", _css_text(SRC / "ui/controls.py")) == {}
    # 0.93em inline mono is an em, never a px font-size — naturally unflagged.
    assert scan_surface("x", "code { font-size: 0.93em; }") == {}


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
    assert scan_surface("x", ".a { font-size: 20px; }")["font-size"] == ["20px"]
    assert scan_surface("x", ".a { border-radius: 4px; }")["radius"] == ["4px"]
    # an alien font literal and a legacy alias → flagged.
    assert "font-family" in scan_surface("x", ".a { font-family: 'Roboto', sans-serif; }")
    assert "alias" in scan_surface("x", ".a { color: var(--ink); }")
    # var(--mono, monospace) is ONE sanctioned family, not drift.
    assert "font-family" not in scan_surface("x", ".a { font-family: var(--mono, monospace); }")
    # INLINE style: a color is caught, a layout width is not.
    assert scan_surface("x", '<i style="color:#abc123"></i>')["color"] == ["#abc123"]
    assert scan_surface("x", '<i style="width:60px;font-size:20px"></i>')["font-size"] == ["20px"]
    # href fragments that look like hex are NOT colors.
    assert scan_surface("x", '<a href="#dcf">x</a>') == {}


@pytest.mark.xfail(strict=True, reason="quarantine non-empty until S7 sweep + report unfork land")
def test_full_conformance_is_red() -> None:
    """The guard 'lands red': full token conformance is not yet reached. This
    xfail FLIPS to a hard failure (strict) the moment QUARANTINE empties,
    forcing the scaffolding to be torn out. There is no --ignore bypass."""
    assert not QUARANTINE, f"still quarantined: {sorted(QUARANTINE)}"


def test_workspace_local_css_standardizes_non_type_primitives() -> None:
    """PR4 (user follow-up): the report's editorial exception is TYPE ONLY.
    Its local CSS (minus the shared palette/controls prefix, which legitimately
    carries the token hex values) has no raw hex or rgba washes, and the only
    3px corner left is the sanctioned .src-chip micro-mark."""
    from report.renderers.workspace_styles import CSS as WS_CSS
    from ui.controls import controls_css as _ccss
    from ui.tokens import palette_css as _pcss

    local = WS_CSS.replace(_pcss("paper"), "").replace(_ccss("paper"), "")
    assert not _RAW_HEX.search(local), f"raw hex in workspace local CSS: {_RAW_HEX.findall(local)}"
    assert "rgba(" not in local.replace("rgba(0,0,0", "").replace("rgba(0, 0, 0", ""), (
        "non-neutral rgba wash in workspace local CSS"
    )
    assert local.count("border-radius: 3px") == 1  # .src-chip only


def test_palette_rows_and_combobox_render_two_part_ticker_labels() -> None:
    """The 'one long label' kill (PR2): the shell palette renders ticker rows
    as .k-tick spans, and the holding combobox never bakes 'T · Name' into its
    input value (ticker value + .cc-combo-name overlay instead)."""
    from pipeline.command_center_shell import SHELL_JS
    from pipeline.ticker_command_center import _combobox  # pyright: ignore[reportPrivateUsage]

    assert "k-tick-sym" in SHELL_JS
    combo = _combobox("NU", "Nu Holdings Ltd.")
    assert 'value="NU"' in combo
    assert '<span class="cc-combo-name"' in combo
    assert "NU · Nu Holdings" not in combo
