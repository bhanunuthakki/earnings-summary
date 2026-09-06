"""Structural contract for the Explore redesign mockups."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKBENCH_MOCKUP = ROOT / "mockups" / "explore_hybrid_2_workbench_interactive.html"
ANALYSIS_SHEET_MOCKUP = ROOT / "mockups" / "explore_hybrid_2_analysis_sheet.html"
CATALOG_JS = ROOT / "mockups" / "explore_metric_catalog.js"


def test_mockups_exist_and_isolated_from_production() -> None:
    assert WORKBENCH_MOCKUP.exists()
    assert ANALYSIS_SHEET_MOCKUP.exists()
    assert CATALOG_JS.exists()

    shell = (ROOT / "src" / "pipeline" / "work_os_shell.py").read_text(encoding="utf-8")
    assert "explore_hybrid_2_workbench_interactive.html" not in shell
    assert "explore_hybrid_2_analysis_sheet.html" not in shell


def test_explore_strict_dcf_boundary_and_authority() -> None:
    wb_html = WORKBENCH_MOCKUP.read_text(encoding="utf-8")
    sheet_html = ANALYSIS_SHEET_MOCKUP.read_text(encoding="utf-8")

    # Read-only authority: zero mutable DCF endpoints or forms
    assert not re.search(r'fetch\(["\']\/api\/dcf', wb_html)
    assert not re.search(r'fetch\(["\']\/api\/dcf', sheet_html)
    assert not re.search(r'method=["\']POST["\']', wb_html, re.IGNORECASE)
    assert not re.search(r'method:\s*["\']POST["\']', wb_html, re.IGNORECASE)

    # Explore explicit read-only projection subtitle and notes
    assert "changes here shape the analysis, never the DCF" in wb_html
    assert "DCF context · read-only" in wb_html
    assert "valuation forecasts are read-only" in wb_html
    assert "Explore can display compatible forecast series" in wb_html


def test_investment_grade_accounting_break_disclosure() -> None:
    wb_html = WORKBENCH_MOCKUP.read_text(encoding="utf-8")
    catalog_text = CATALOG_JS.read_text(encoding="utf-8")

    # Q4 '25 break indicators and citations
    assert "BREAK ⚠️" in wb_html
    assert "doc:meli-2025-q4-p8" in wb_html
    assert "doc:meli-2025-q4-p8" in catalog_text
    assert "Accounting presentation break at Q4 \u201925" in wb_html
    assert "spans break*" in wb_html

    # Invariant: Never normalize away a semantic break
    assert "Never normalize away a break" in wb_html


def test_definition_doorways_and_accessibility() -> None:
    wb_html = WORKBENCH_MOCKUP.read_text(encoding="utf-8")

    # Surface doorways
    assert "data-definition" in wb_html
    assert "wb-break-tag" in wb_html
    assert "wb-chart-break-tag" in wb_html
    assert "wb-legend-break" in wb_html
    assert "definition-history-button" in wb_html

    # Keyboard & tablet focus styling
    assert ":focus-within" in wb_html
    assert ":focus-visible" in wb_html
    assert "@media(hover:none)" in wb_html or "@media (hover: none)" in wb_html


def test_explore_and_analytics_are_one_consolidated_flow() -> None:
    wb_html = WORKBENCH_MOCKUP.read_text(encoding="utf-8")
    sheet_html = ANALYSIS_SHEET_MOCKUP.read_text(encoding="utf-8")
    combined = f"{sheet_html}\n{wb_html}"

    # Explore is the only company-scoped Q&A surface. Analytics is its deep state.
    assert "Fact & Metric Playground" not in combined
    assert "DIY builder" not in combined
    assert 'id="explore-company"' in sheet_html
    assert 'id="workbench-company"' in wb_html
    assert "Auto route" in sheet_html
    assert "Analytics once" in sheet_html
    assert "Research answer" in sheet_html
    assert "Analytics answer" in sheet_html
    assert "Explore / Analytics" in wb_html
    assert "Back to Explore" in wb_html

    # One explicit escalation from a compact answer to the full-screen work state.
    assert sheet_html.count("data-open-sheet") == 2  # one element + one JS binding
    assert "Work with data" in sheet_html
    assert "Open full work bench" not in sheet_html


def test_analytics_workbench_uses_progressive_full_height_rails() -> None:
    wb_html = WORKBENCH_MOCKUP.read_text(encoding="utf-8")

    # The result owns the default work area; both supporting panels are
    # progressive, full-height rails with explicit resize/minimize controls.
    assert 'class="wb-body"' in wb_html
    assert 'id="fields-rail"' in wb_html
    assert 'id="metric-rail"' in wb_html
    assert 'id="fields-resizer"' in wb_html
    assert 'id="metric-resizer"' in wb_html
    assert wb_html.count('role="separator"') >= 2
    assert 'id="minimize-fields"' in wb_html
    assert 'id="minimize-metric"' in wb_html
    assert "exploreAnalyticsRailPrefs" in wb_html
    assert "localStorage.setItem" in wb_html
    assert "localStorage.getItem" in wb_html
    assert "--fields-rail-width" in wb_html
    assert "--metric-rail-width" in wb_html


def test_analytics_workbench_fields_are_horizontal_search_first_and_shared() -> None:
    wb_html = WORKBENCH_MOCKUP.read_text(encoding="utf-8")

    # The default field affordance is the operating band immediately after
    # Shape. Browse expands the complete governed catalog in the left rail.
    shape_index = wb_html.index('class="wb-shape"')
    fields_index = wb_html.index('id="fields-band"')
    result_index = wb_html.index('class="wb-result-head"')
    assert shape_index < fields_index < result_index
    assert 'id="field-band-search"' in wb_html
    assert 'id="field-band-selected"' in wb_html
    assert 'id="field-band-recommended"' in wb_html
    assert 'id="browse-all-fields"' in wb_html
    assert "Search every cached company fact" in wb_html

    # Inline row editing and the default field band use the same picker grammar
    # and the same catalog matching function, rather than lookalike controls.
    assert wb_html.count('data-metric-picker="true"') >= 2
    assert "renderMetricPicker" in wb_html
    assert "catalogMatches" in wb_html


def test_analytics_workbench_company_picker_and_prompt_hierarchy() -> None:
    wb_html = WORKBENCH_MOCKUP.read_text(encoding="utf-8")

    # Canonical searchable single-select anatomy retains the native select as
    # the typed value carrier and exposes an app-owned combobox/listbox.
    assert 'id="workbench-company"' in wb_html
    assert 'class="k-select-native"' in wb_html
    assert 'id="workbench-company-trigger"' in wb_html
    assert 'role="combobox"' in wb_html
    assert 'id="workbench-company-menu"' in wb_html
    assert 'role="listbox"' in wb_html
    assert "No matching companies" in wb_html
    assert "aria-activedescendant" in wb_html

    # There is no redundant top intent editor. Prompt context sits immediately
    # above the persistent bottom composer while its input remains editable.
    assert 'class="wb-intent"' not in wb_html
    assert 'id="analysis-intent"' not in wb_html
    prompt_index = wb_html.index('id="active-prompt"')
    composer_index = wb_html.index('class="wb-compose"')
    assert prompt_index < composer_index
    assert 'id="follow-up"' in wb_html
    assert ".wb-title h1{margin:0;overflow:hidden;font-size:var(--fs-display)" in wb_html


def test_metric_click_opens_persistent_read_only_inspector() -> None:
    wb_html = WORKBENCH_MOCKUP.read_text(encoding="utf-8")

    assert "openMetricRail" in wb_html
    assert "selectMetric" in wb_html
    assert 'data-metric-detail="' in wb_html
    assert "metricOpen" in wb_html
    assert "DCF context · read-only" in wb_html
    assert "No DCF overlay for this metric" in wb_html


def test_explore_answer_is_narrative_first_and_uses_reclaimed_canvas() -> None:
    sheet_html = ANALYSIS_SHEET_MOCKUP.read_text(encoding="utf-8")

    assert "<h1>Explore</h1>" not in sheet_html
    assert 'class="hs-head"' not in sheet_html
    assert sheet_html.index('id="hs-question"') < sheet_html.index('id="hs-response"')
    assert sheet_html.count('class="hs-answer-paragraph"') >= 3
    assert "Reported fact" in sheet_html
    assert "Management claim" in sheet_html
    assert "Calculation" in sheet_html
    assert "Analyst inference" in sheet_html
    assert 'class="hs-metric-link" data-definition="fintech" data-window="2y"' in sheet_html
    assert 'class="hs-metric-link" data-definition="commerce" data-window="2y"' in sheet_html
    assert 'class="hs-result"' not in sheet_html
    assert 'class="hs-chart"' not in sheet_html
    assert 'class="k-chip k-chip-accent hs-work-data" data-open-sheet' in sheet_html
    assert "width:min(1240px,100%)" in sheet_html


def test_explore_company_picker_and_metric_quick_view_are_progressive() -> None:
    sheet_html = ANALYSIS_SHEET_MOCKUP.read_text(encoding="utf-8")

    topbar_index = sheet_html.index('class="topbar"')
    company_index = sheet_html.index('id="explore-company"')
    content_index = sheet_html.index('class="content hs-content"')
    assert topbar_index < company_index < content_index
    assert 'class="k-select-native" id="explore-company"' in sheet_html
    assert 'id="explore-company-trigger"' in sheet_html
    assert 'id="explore-company-menu" role="listbox"' in sheet_html
    assert "No matching companies" in sheet_html
    assert "aria-activedescendant" in sheet_html
    assert 'id="metric-quick-view"' in sheet_html
    assert 'id="metric-latest"' in sheet_html
    assert 'id="metric-qoq"' in sheet_html
    assert 'id="metric-yoy"' in sheet_html
    assert 'id="metric-2y"' in sheet_html
    assert 'id="metric-5y"' in sheet_html
