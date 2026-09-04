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
