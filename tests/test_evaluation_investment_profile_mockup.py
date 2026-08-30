"""Structural contract for the isolated Evaluation investment-profile mockup."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MOCKUP_PATH = ROOT / "mockups" / "evaluation_investment_profile_mockup.html"


def _mockup() -> str:
    return MOCKUP_PATH.read_text(encoding="utf-8")


def test_mockup_is_isolated_and_labels_its_truth_boundary() -> None:
    production = (ROOT / "src" / "pipeline" / "work_os_shell.py").read_text(encoding="utf-8")

    assert MOCKUP_PATH.exists()
    assert MOCKUP_PATH.name not in production
    html = _mockup()
    assert "Prototype only" in html
    assert "Illustrative suggestions" in html
    assert "No live portfolio or research state is read or written" in html


def test_header_uses_the_proposed_global_56px_rule() -> None:
    html = _mockup()

    assert "--header-height: 56px" in html
    assert "block-size: var(--header-height)" in html
    assert "Workspaces / <strong>Evaluation</strong>" in html


def test_front_table_removes_composite_scores_and_uses_investor_language() -> None:
    html = _mockup()
    table = html.split('data-testid="evaluation-table"', 1)[1].split("</table>", 1)[0]

    for heading in (
        "Company",
        "Investment profile",
        "Business &amp; moat",
        "Portfolio role",
        "Valuation",
        "Research",
    ):
        assert f">{heading}<" in table
    assert ">Evaluation<" not in table
    assert ">Portfolio fit<" not in table
    assert "score_why" not in table
    assert "3.74" not in table
    assert "0.88" not in table


def test_seeded_company_and_etf_labels_are_filterable_multi_labels() -> None:
    html = _mockup()

    for label in (
        "Long-term compounder",
        "GARP",
        "Elite growth / expensive",
        "Turnaround",
        "Narrative re-rating",
        "Growth inflection",
        "Cash-yield value",
        "Optionality",
        "Thematic exposure",
        "Factor sleeve",
        "Diversifier",
        "Defensive / hedge",
        "Income",
    ):
        assert label in html
    assert 'data-filter-label="all"' in html
    assert 'data-filter-label="garp"' in html
    assert 'data-filter-label="long-term-compounder"' in html
    assert 'data-filter-label="etf"' in html
    assert "applyLabelFilter" in html


def test_owner_ratification_is_distinct_from_system_suggestion() -> None:
    html = _mockup()

    assert "System suggested" in html
    assert "Owner ratified" in html
    assert "Review suggested" in html
    assert 'data-action="ratify"' in html
    assert 'data-action="edit-labels"' in html
    assert 'data-action="reject-suggestion"' in html
    assert "System updates never overwrite an owner-ratified profile" in html


def test_moat_uses_four_business_durability_levels_and_separate_coverage() -> None:
    html = _mockup()

    for level in (
        "Multi-business moat",
        "Core-business moat",
        "Narrow / conditional moat",
        "No demonstrated moat",
    ):
        assert level in html
    assert "Evidence insufficient remains a separate coverage state" in html
    assert "moat: 'Strong'" not in html
    assert "moat: 'Mixed'" not in html
    assert "moat: 'Unproven" not in html


def test_profile_and_portfolio_drawer_share_accessible_overlay_behavior() -> None:
    html = _mockup()

    assert 'id="profileDrawer"' in html
    assert 'role="dialog"' in html
    assert 'aria-modal="true"' in html
    assert 'role="tablist"' in html
    assert 'data-drawer-tab="profile"' in html
    assert 'data-drawer-tab="portfolio"' in html
    assert "openDrawer" in html
    assert "closeDrawer" in html
    assert "trapDrawerFocus" in html
    assert "event.key === 'Escape'" in html
    assert "event.key === 'Tab'" in html
    assert "restoreFocus" in html


def test_drawer_names_existing_evidence_sources_and_future_update_contract() -> None:
    html = _mockup()

    for source in (
        "Full brief",
        "DCF run",
        "Post-earnings readout",
        "Candidate fit",
        "Investment Decision Card",
    ):
        assert source in html
    assert "New evidence creates a proposed revision" in html
    assert "Owner ratification remains unchanged" in html
    assert "30-day decision window" in html


def test_mockup_uses_shared_kit_shapes_and_responsive_rules() -> None:
    html = _mockup()

    for primitive in ("k-card", "k-btn", "k-chip", "k-pill", "k-well", "k-overlay", "k-drawer"):
        assert primitive in html
    assert not re.search(r'\sstyle="', html)
    assert "@media (max-width: 72rem)" in html
    assert "@media (max-width: 48rem)" in html
    assert "prefers-reduced-motion: reduce" in html
    assert "overflow-x: auto" in html
