"""Unit tests for the unified command-center shell renderer.

The shell is a pure function over a pre-built Overview HTML string + the
five-section table (UX redesign PR2); these lock the structural contract
the lazy-loader JS relies on (top-bar section nav + per-section sub-tab
rows, per-panel endpoints, cc-picker on the dropdown-driven Holding tab,
legacy-hash + section-alias redirects, the Ctrl+K palette chrome).
"""

from __future__ import annotations

from datetime import UTC, datetime

from pipeline.command_center_shell import (
    _LEGACY_PANEL_REDIRECTS,  # pyright: ignore[reportPrivateUsage]  # keep-in-sync contract under test
    SHELL_CSS,
    SHELL_JS,
    render_overview_panel,
    render_shell,
)


def test_overview_panel_is_the_research_cockpit() -> None:
    """Overview = the cockpit + the freshness strip. The IR-KPI + maintenance
    action blocks moved to the settings drawer (P3.4) and must NOT inline."""
    html = render_overview_panel({"portfolio": [], "evaluation": []}, coverage={})
    assert "Portfolio" in html
    assert "Evaluation" in html
    assert "No portfolio tickers." in html
    assert "cockpit-section" in html
    assert 'id="refresh-ir-form"' not in html
    assert "/actions/maintenance" not in html


def test_render_shell_five_section_structure() -> None:
    html = render_shell(
        overview_html="<div id='ov-marker'>OVERVIEW</div>",
        generated_at=datetime(2026, 6, 1, tzinfo=UTC),
    )
    assert html.startswith("<!doctype html>")
    assert html.rstrip().endswith("</body></html>")
    assert "<title>Portfolio · command center</title>" in html
    # Top-bar section nav: exactly the five sections.
    for section in ("home", "companies", "ask", "portfolio", "system"):
        assert f'data-theme-target="{section}"' in html
        assert f'data-cc-theme="{section}"' in html  # its sub-tab row exists
    # The old three-theme ids are gone from the nav.
    for dead in ("research", "governance"):
        assert f'data-theme-target="{dead}"' not in html
    # Surviving sub-tabs, each tagged with its section.
    for target in (
        "overview",
        "holding",
        "discovery",
        "journal",
        "explore",
        "portfolio",
        "decisions_record",
        "advisor_memos",
        "holdings",
        "section_coverage",
        "ir_coverage",
        "source_calls",
        "validation",
        "restatements",
    ):
        assert f'data-tab-target="{target}"' in html
    for drawered in ("budget", "actions"):
        assert f'data-tab-target="{drawered}"' not in html
    # Overview is inlined verbatim and marked loaded.
    assert "OVERVIEW" in html
    assert 'data-panel="overview" data-loaded="1"' in html
    # Topbar links to the live alerting surfaces stay reachable.
    assert 'href="/digest"' in html
    assert 'href="/feed"' in html


def test_single_tab_sections_suppress_their_sub_row() -> None:
    """Home and Ask have one sub-tab each — the row stays in the DOM (the JS
    derives the active section from the sub-tab's data-cc-theme) but is marked
    data-single so CSS suppresses it. Multi-tab sections must NOT be marked."""
    html = render_shell(overview_html="x", generated_at=datetime(2026, 6, 1, tzinfo=UTC))
    assert 'data-cc-theme="home" data-single="1"' in html
    assert 'data-cc-theme="ask" data-single="1"' in html
    assert 'data-cc-theme="companies" data-single="1"' not in html
    assert 'data-cc-theme="portfolio" data-single="1"' not in html
    assert 'data-cc-theme="system" data-single="1"' not in html
    # The CSS that suppresses single-tab rows + the [hidden] restatement that
    # keeps inactive rows from stacking (display:flex used to beat [hidden]).
    assert '.cc-subtabs[data-single="1"] { display: none; }' in SHELL_CSS
    assert ".cc-tabs[hidden] { display: none; }" in SHELL_CSS


def test_killed_surfaces_are_out_of_nav_but_redirected() -> None:
    """Pre-reads / Insiders / Predictions / Decisions died as nav surfaces
    (P1.1), budget/actions became drawer sections (P3.4), thesis_ledger folded
    into the Decisions record (P2.2), and the four section names alias to
    their landing panels (PR2); every one must remap client-side."""
    html = render_shell(overview_html="x", generated_at=datetime(2026, 6, 1, tzinfo=UTC))
    for killed in (
        "prereads",
        "insiders",
        "predictions",
        "decisions",
        "thesis_ledger",
        "budget",
        "actions",
    ):
        assert f'data-tab-target="{killed}"' not in html
        assert f'data-panel="{killed}"' not in html
        # The JS REDIRECTS map carries every killed panel.
        assert f"{killed}:" in SHELL_JS.replace("'", "")
    # Python-side map mirrors the JS one (keep-in-sync contract).
    assert set(_LEGACY_PANEL_REDIRECTS) == {
        "prereads",
        "insiders",
        "predictions",
        "decisions",
        "thesis_ledger",
        "budget",
        "actions",
        "home",
        "companies",
        "ask",
        "system",
    }
    for alias in ("home", "companies", "ask", "system"):
        assert f"{alias}:" in SHELL_JS.replace("'", "")
    for new_home in _LEGACY_PANEL_REDIRECTS.values():
        assert f'data-tab-target="{new_home}"' in html
    # The old decisions/ledger deep-links land on the allocation record.
    assert _LEGACY_PANEL_REDIRECTS["decisions"] == "decisions_record"
    assert _LEGACY_PANEL_REDIRECTS["thesis_ledger"] == "decisions_record"
    # Section aliases land on each section's first panel.
    assert _LEGACY_PANEL_REDIRECTS["home"] == "overview"
    assert _LEGACY_PANEL_REDIRECTS["companies"] == "holding"
    assert _LEGACY_PANEL_REDIRECTS["ask"] == "explore"
    assert _LEGACY_PANEL_REDIRECTS["system"] == "section_coverage"
    # The drawer-section legacy ids also auto-open the drawer on arrival.
    assert "DRAWER_OPENERS = { budget: 1, actions: 1 }" in SHELL_JS


def test_render_shell_lazy_endpoints_and_pickers() -> None:
    html = render_shell(overview_html="x", generated_at=datetime(2026, 6, 1, tzinfo=UTC))
    # Lazy panels carry their fetch endpoint and start unloaded.
    assert 'data-endpoint="/api/panel/holdings"' in html
    assert 'data-endpoint="/api/panel/validation"' in html
    assert 'data-loaded="0"' in html
    # Only the per-ticker Holding drill-down is dropdown-driven now.
    assert html.count('class="cc-picker"') == 1
    assert 'data-endpoint="/api/panel/holding"' in html
    # The shell JS + CSS are inlined.
    assert SHELL_JS[:30] in html
    assert SHELL_CSS[:30] in html


def test_settings_drawer_structure() -> None:
    """P3.4: admin is a drawer, not a tab — the topbar toggle, the drawer
    chrome, and the three lazy sections (budget / ticker settings /
    maintenance) reusing the existing panel endpoints."""
    html = render_shell(overview_html="x", generated_at=datetime(2026, 6, 1, tzinfo=UTC))
    assert 'id="cc-settings-toggle"' in html
    assert 'id="cc-drawer"' in html
    assert 'id="cc-drawer-close"' in html
    # Drawer sections lazy-load the SAME fragments the old tabs served.
    assert 'class="cc-drawer-sec" open data-endpoint="/api/panel/budget"' in html
    assert 'data-endpoint="/api/panel/ticker_settings"' in html
    assert 'data-endpoint="/api/panel/actions"' in html
    # The drawer endpoints are sections, not nav panels.
    assert '<section class="cc-panel" data-panel="budget"' not in html
    assert '<section class="cc-panel" data-panel="actions"' not in html


def test_command_palette_chrome() -> None:
    """PR2: the Ctrl/Cmd+K palette — overlay + input + list in the DOM, the
    topbar trigger button, and the JS wiring constants."""
    html = render_shell(overview_html="x", generated_at=datetime(2026, 6, 1, tzinfo=UTC))
    assert 'id="cc-palette"' in html
    assert 'id="cc-palette-input"' in html
    assert 'id="cc-palette-list"' in html
    assert 'id="cc-palette-open"' in html
    assert "openPalette" in SHELL_JS
    assert "metaKey" in SHELL_JS  # Cmd+K works on mac keyboards too


def test_render_shell_overview_not_a_lazy_endpoint() -> None:
    """Overview must be inlined, never assigned a /api/panel/ endpoint."""
    html = render_shell(overview_html="x", generated_at=datetime(2026, 6, 1, tzinfo=UTC))
    assert 'data-endpoint="/api/panel/overview"' not in html


def test_sub_tab_buttons_carry_their_section() -> None:
    """The JS derives the active section from the active sub-tab's
    data-cc-theme — every sub-tab button must carry one."""
    html = render_shell(overview_html="x", generated_at=datetime(2026, 6, 1, tzinfo=UTC))
    assert 'data-tab-target="overview" data-cc-theme="home"' in html
    assert 'data-tab-target="holding" data-cc-theme="companies"' in html
    assert 'data-tab-target="explore" data-cc-theme="ask"' in html
    assert 'data-tab-target="decisions_record" data-cc-theme="portfolio"' in html
    assert 'data-tab-target="validation" data-cc-theme="system"' in html
    assert 'data-tab-target="ir_coverage" data-cc-theme="system"' in html


def test_content_width_is_wide() -> None:
    """PR2: the 1280px cap left ~300px dead gutters per side at 1920 — the
    shell now flows to 1600."""
    assert "max-width: 1600px" in SHELL_CSS
    assert "max-width: 1280px" not in SHELL_CSS
