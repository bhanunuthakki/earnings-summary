"""Unit tests for the unified command-center shell renderer.

The shell is a pure function over a pre-built Overview HTML string + the
three-theme table (master build P1.1); these lock the structural contract
the lazy-loader JS relies on (theme row + sub-tab rows, per-panel
endpoints, cc-picker on the dropdown-driven Holding tab, legacy-hash
redirects for the killed surfaces).
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


def test_overview_panel_reuses_existing_renderers() -> None:
    """Empty rows still render both section headers (via dashboard_html's
    _render_section) plus the IR-KPI + maintenance action blocks."""
    html = render_overview_panel({"portfolio": [], "evaluation": []}, coverage={})
    assert "Portfolio" in html
    assert "Evaluation" in html
    assert "No portfolio tickers." in html
    # The IR-KPI + maintenance action blocks are inlined.
    assert 'id="refresh-ir-form"' in html
    assert "/actions/maintenance" in html


def test_render_shell_three_theme_structure() -> None:
    html = render_shell(
        overview_html="<div id='ov-marker'>OVERVIEW</div>",
        generated_at=datetime(2026, 6, 1, tzinfo=UTC),
    )
    assert html.startswith("<!doctype html>")
    assert html.rstrip().endswith("</body></html>")
    assert "<title>Portfolio · command center</title>" in html
    # Primary theme row: exactly the three themes.
    for theme in ("research", "portfolio", "governance"):
        assert f'data-theme-target="{theme}"' in html
        assert f'data-cc-theme="{theme}"' in html  # its sub-tab row exists
    # Surviving sub-tabs, each tagged with its theme.
    for target in (
        "overview",
        "holding",
        "portfolio",
        "holdings",
        "thesis_ledger",
        "ir_coverage",
        "source_calls",
        "budget",
    ):
        assert f'data-tab-target="{target}"' in html
    # Overview is inlined verbatim and marked loaded.
    assert "OVERVIEW" in html
    assert 'data-panel="overview" data-loaded="1"' in html
    # Topbar links to the live alerting surfaces stay reachable.
    assert 'href="/digest"' in html
    assert 'href="/feed"' in html


def test_killed_surfaces_are_out_of_nav_but_redirected() -> None:
    """Pre-reads / Insiders / Predictions / Decisions died as nav surfaces
    (master build P1.1); their legacy deep-links must remap client-side."""
    html = render_shell(overview_html="x", generated_at=datetime(2026, 6, 1, tzinfo=UTC))
    for killed in ("prereads", "insiders", "predictions", "decisions"):
        assert f'data-tab-target="{killed}"' not in html
        assert f'data-panel="{killed}"' not in html
        # The JS REDIRECTS map carries every killed panel.
        assert f"{killed}:" in SHELL_JS.replace("'", "")
    # Python-side map mirrors the JS one (keep-in-sync contract).
    assert set(_LEGACY_PANEL_REDIRECTS) == {"prereads", "insiders", "predictions", "decisions"}
    for new_home in _LEGACY_PANEL_REDIRECTS.values():
        assert f'data-tab-target="{new_home}"' in html


def test_render_shell_lazy_endpoints_and_pickers() -> None:
    html = render_shell(overview_html="x", generated_at=datetime(2026, 6, 1, tzinfo=UTC))
    # Lazy panels carry their fetch endpoint and start unloaded.
    assert 'data-endpoint="/api/panel/holdings"' in html
    assert 'data-endpoint="/api/panel/budget"' in html
    assert 'data-loaded="0"' in html
    # Only the per-ticker Holding drill-down is dropdown-driven now.
    assert html.count('class="cc-picker"') == 1
    assert 'data-endpoint="/api/panel/holding"' in html
    # The shell JS + CSS are inlined.
    assert SHELL_JS[:30] in html
    assert SHELL_CSS[:30] in html


def test_render_shell_overview_not_a_lazy_endpoint() -> None:
    """Overview must be inlined, never assigned a /api/panel/ endpoint."""
    html = render_shell(overview_html="x", generated_at=datetime(2026, 6, 1, tzinfo=UTC))
    assert 'data-endpoint="/api/panel/overview"' not in html


def test_sub_tab_buttons_carry_their_theme() -> None:
    """The JS derives the active theme from the active sub-tab's
    data-cc-theme — every sub-tab button must carry one."""
    html = render_shell(overview_html="x", generated_at=datetime(2026, 6, 1, tzinfo=UTC))
    assert 'data-tab-target="overview" data-cc-theme="research"' in html
    assert 'data-tab-target="thesis_ledger" data-cc-theme="portfolio"' in html
    assert 'data-tab-target="budget" data-cc-theme="governance"' in html
