"""Unit tests for the unified command-center shell renderer.

The shell is a pure function over a pre-built Overview HTML string + the tab
table; these lock the structural contract the lazy-loader JS relies on (tab
bar, per-panel endpoints, cc-picker on the dropdown-driven tabs).
"""

from __future__ import annotations

from datetime import UTC, datetime

from pipeline.command_center_shell import (
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


def test_render_shell_structure() -> None:
    html = render_shell(
        overview_html="<div id='ov-marker'>OVERVIEW</div>",
        generated_at=datetime(2026, 6, 1, tzinfo=UTC),
    )
    assert html.startswith("<!doctype html>")
    assert html.rstrip().endswith("</body></html>")
    assert "<title>Portfolio · command center</title>" in html
    # Tab bar with a button per tab.
    assert 'class="cc-tabs"' in html
    for target in (
        "overview",
        "portfolio",
        "holdings",
        "holding",
        "prereads",
        "insiders",
        "predictions",
        "decisions",
        "budget",
    ):
        assert f'data-tab-target="{target}"' in html
    # Overview is inlined verbatim and marked loaded.
    assert "OVERVIEW" in html
    assert 'data-panel="overview" data-loaded="1"' in html
    # Topbar links to the live Personal-CIO alerting surfaces (digest + feed),
    # so they are reachable from the app rather than static-file-only.
    assert 'href="/digest"' in html
    assert 'href="/feed"' in html


def test_render_shell_lazy_endpoints_and_pickers() -> None:
    html = render_shell(overview_html="x", generated_at=datetime(2026, 6, 1, tzinfo=UTC))
    # Lazy panels carry their fetch endpoint and start unloaded.
    assert 'data-endpoint="/api/panel/holdings"' in html
    assert 'data-endpoint="/api/panel/budget"' in html
    assert 'data-loaded="0"' in html
    # Holding + Pre-reads + Insiders are dropdown-driven; the others are not.
    picker_count = html.count('class="cc-picker"')
    assert picker_count == 3  # holding + prereads + insiders
    assert 'data-endpoint="/api/panel/holding"' in html
    # The shell JS + CSS are inlined.
    assert SHELL_JS[:30] in html
    assert SHELL_CSS[:30] in html


def test_render_shell_overview_not_a_lazy_endpoint() -> None:
    """Overview must be inlined, never assigned a /api/panel/ endpoint."""
    html = render_shell(overview_html="x", generated_at=datetime(2026, 6, 1, tzinfo=UTC))
    assert 'data-endpoint="/api/panel/overview"' not in html
