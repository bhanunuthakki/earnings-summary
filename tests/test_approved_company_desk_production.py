"""Production contract for the owner-approved Company Desk composition."""

from __future__ import annotations

from pipeline.work_os_research import render_company_desk_shell
from pipeline.work_os_shell import render_work_os_shell


def test_company_desk_uses_the_approved_one_page_hierarchy() -> None:
    html = render_company_desk_shell()

    assert 'data-layout="company-desk-approved"' in html
    assert 'data-testid="company-topline"' in html
    assert 'id="workOsDcfLink"' in html
    assert 'id="workOsDcfLink" aria-disabled="true" tabindex="-1"' in html
    assert ">DCF model ↗</a>" in html
    assert 'data-testid="decision-card"' in html
    assert 'id="deskTrackingBands"' in html
    assert 'data-testid="summary-thesis"' in html
    assert 'data-testid="q2-update"' in html
    assert 'data-testid="next-step-exploration"' in html
    assert 'data-testid="contracts-card"' in html
    assert 'data-testid="saydo-panel"' in html
    assert 'data-testid="open-full-saydo"' in html

    assert "Owner posture" not in html
    assert "Model recommendation" not in html
    assert "Financials &amp; DCF" not in html
    assert "Transcripts &amp; Q&amp;A" not in html
    assert "Notes &amp; Provenance" not in html


def test_company_desk_runtime_wires_real_dcf_bands_and_brief_doorways() -> None:
    html = render_work_os_shell()

    assert "desk.position.dcf_url" in html
    assert "dcfLink.removeAttribute('href')" in html
    assert "event.preventDefault()" in html
    assert "desk.price_action_bands" in html
    assert "price_action_bands_unencoded" in html
    assert "openWorkOsBriefReader(brief, { sectionId: 'saydo' })" in html
    assert "workOsSwitchCompanyDeskSection" in html
    assert "ArrowLeft" in html and "ArrowRight" in html


def test_company_desk_mobile_contract_does_not_hide_the_active_navigation_group() -> None:
    html = render_work_os_shell()

    assert ".company-desk-approved-grid" in html
    assert ".company-desk-decision-grid" in html
    assert ".company-desk-tracking-grid" in html
    assert ".nav-group:not(:nth-of-type(3))" not in html
