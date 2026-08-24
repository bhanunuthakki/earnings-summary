"""Structural contract for the isolated Company Desk page mockup."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MOCKUP_PATH = ROOT / "mockups" / "company_desk_mockup.html"


def _mockup() -> str:
    return MOCKUP_PATH.read_text(encoding="utf-8")


def _section(html: str, testid: str) -> str:
    start = html.index(f'data-testid="{testid}"')
    end = html.index("</section>", start)
    return html[start:end]


def test_mockup_is_isolated_from_production_renderers() -> None:
    shell = (ROOT / "src" / "pipeline" / "work_os_shell.py").read_text(encoding="utf-8")

    assert MOCKUP_PATH.exists()
    assert "company_desk_mockup.html" not in shell
    assert '"mockups" / "harvey_sidebar_flow.html"' in shell


def test_company_desk_label_is_not_repeated_below_fixed_header() -> None:
    html = _mockup()

    assert html.count('data-label-surface="sidebar">Company Desk<') == 1
    assert html.count('data-label-surface="fixed-header">Company Desk<') == 1
    assert "<h1>Company Desk</h1>" not in html
    assert '<div class="page-heading">' not in html
    assert (
        '<h1><span class="mono">NU</span> <span class="company-name">Nu Holdings</span></h1>'
        in html
    )


def test_topline_removes_copilot_and_adds_dcf_link_and_one_decision_card() -> None:
    html = _mockup()
    topline = _section(html, "company-topline")

    assert "Ask Copilot" not in topline
    assert 'data-testid="dcf-link"' in topline
    assert ">DCF model ↗</a>" in topline
    assert topline.count('data-testid="decision-card"') == 1
    assert 'data-testid="decision-grid"' not in html
    assert "Owner posture" not in html
    assert "Model recommendation" not in html


def test_decision_card_combines_tracking_bands_and_thesis_status() -> None:
    html = _mockup()
    card = html.split('data-testid="decision-card"', 1)[1].split("</article>", 1)[0]

    for label in ("Buy", "Add", "Hold", "Trim"):
        assert f">{label}<" in card
    assert card.count('data-testid="tracking-band"') == 4
    assert "Thesis status" in card
    assert "INTACT" in card
    assert "Current action" in card
    assert "ADD" in card


def test_tracking_actions_use_governed_application_tones() -> None:
    html = _mockup()
    card = html.split('data-testid="decision-card"', 1)[1].split("</article>", 1)[0]

    for action in ("buy", "add", "hold", "trim"):
        assert f"tracking-band-{action}" in card
    assert ".tracking-band-buy" in html and "var(--ok)" in html
    assert ".tracking-band-add" in html and "var(--accent)" in html
    assert ".tracking-band-hold" in html and "var(--muted)" in html
    assert ".tracking-band-trim" in html and "var(--warn)" in html


def test_summary_thesis_and_q2_update_precede_next_step_exploration() -> None:
    html = _mockup()

    thesis_at = html.index('data-testid="summary-thesis"')
    q2_at = html.index('data-testid="q2-update"')
    exploration_at = html.index('data-testid="next-step-exploration"')

    assert thesis_at < exploration_at
    assert q2_at < exploration_at
    assert "Why I own NU" in html
    assert "Q2 update" in html
    assert "governed Q2 readout is pending" in html
    assert "Recent relevant updates" in html
    assert "Open questions" in html
    assert "Thesis contracts" in html


def test_contracts_card_has_a_four_quarter_saydo_tab_and_full_brief_jump() -> None:
    html = _mockup()
    section = _section(html, "contracts-card")
    saydo = section.split('data-testid="saydo-panel"', 1)[1]

    assert 'role="tablist"' in section
    assert ">Thesis contracts</button>" in section
    assert ">Say / Do · 4 quarters</button>" in section
    assert 'data-testid="saydo-table"' in saydo
    apostrophe = "\N{RIGHT SINGLE QUOTATION MARK}"
    for quarter in (
        f"Q3 {apostrophe}25",
        f"Q4 {apostrophe}25",
        f"Q1 {apostrophe}26",
        f"Q2 {apostrophe}26",
    ):
        assert quarter in saydo
    for rating in ("EXCEEDED", "MET", "MIXED", "AWAITING"):
        assert rating in saydo
    assert 'data-testid="open-full-saydo"' in saydo
    assert ">Open full Say / Do section →</button>" in saydo


def test_contracts_and_saydo_use_project_side_by_side_tabs() -> None:
    html = _mockup()
    section = _section(html, "contracts-card")

    assert 'class="research-tabs" role="tablist"' in section
    assert section.count('class="research-tab k-btn k-btn-quiet"') == 2
    assert 'class="k-chip k-chip-btn"' not in section
    assert "grid-template-columns: repeat(2, minmax(var(--sp-0), 1fr))" in html
    assert '.research-tab[aria-selected="true"]' in html


def test_saydo_tables_are_reverse_chronological() -> None:
    html = _mockup()
    section = _section(html, "contracts-card")
    card_saydo = section.split('data-testid="saydo-panel"', 1)[1]
    full_saydo = html.split('id="briefSaydoOverlay"', 1)[1].split("<script>", 1)[0]
    apostrophe = "\N{RIGHT SINGLE QUOTATION MARK}"
    expected = (
        f"Q2 {apostrophe}26",
        f"Q1 {apostrophe}26",
        f"Q4 {apostrophe}25",
        f"Q3 {apostrophe}25",
    )

    for table in (card_saydo, full_saydo):
        positions = [table.index(quarter) for quarter in expected]
        assert positions == sorted(positions)


def test_full_brief_jump_opens_a_accessible_saydo_dialog() -> None:
    html = _mockup()

    assert 'id="briefSaydoOverlay"' in html
    assert 'role="dialog"' in html
    assert 'aria-modal="true"' in html
    assert "openFullSaydo" in html
    assert "closeFullSaydo" in html
    assert "trapBriefFocus" in html
    assert "event.key === 'Tab'" in html
    assert "event.key === 'Escape'" in html
    assert "focus({ preventScroll: true })" in html
    assert "Brief section · Say / Do" in html


def test_tabs_use_roving_tabindex() -> None:
    html = _mockup()

    assert 'id="contractsTab" role="tab" tabindex="0"' in html
    assert 'id="saydoTab" role="tab" tabindex="-1"' in html
    assert "contractsTab.tabIndex = isContracts ? 0 : -1" in html
    assert "saydoTab.tabIndex = isContracts ? -1 : 0" in html


def test_mockup_uses_kit_tokens_and_responsive_contract() -> None:
    html = _mockup()

    for primitive in ("k-card", "k-btn", "k-chip", "k-pill", "k-well"):
        assert primitive in html
    assert not re.search(r'\sstyle="', html)
    assert "Visual-only kit simulation" in html
    assert "@media (max-width: 70rem)" in html
    assert "@media (max-width: 48rem)" in html
    assert "overflow: auto" in html
    assert "prefers-reduced-motion: reduce" in html
    assert "Illustrative layout" in html
