"""Read-only Settings surface for source collection policy."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from flask.testing import FlaskClient

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "execution"))

import comments_server  # noqa: E402

from pipeline.data_policy_settings_panel import (  # noqa: E402
    PolicyDisplayState,
    build_data_policy_settings_view,
    render_data_policy_settings_panel,
)
from pipeline.work_os_shell import render_work_os_shell  # noqa: E402


def test_policy_view_is_derived_from_the_canonical_authorization_contract() -> None:
    view = build_data_policy_settings_view()

    assert [role.role.value for role in view.roles] == [
        "portfolio",
        "evaluation",
        "watchlist",
        "index_member",
    ]
    rows = {row.key: row for row in view.rows}
    assert [cell.state for cell in rows["fmp_financial_facts"].cells] == [
        PolicyDisplayState.AUTOMATIC,
        PolicyDisplayState.ON_DEMAND,
        PolicyDisplayState.NEVER,
        PolicyDisplayState.SCREENING_ONLY,
    ]
    assert [cell.state for cell in rows["sec_companyfacts"].cells] == [
        PolicyDisplayState.AUTOMATIC,
        PolicyDisplayState.ON_DEMAND,
        PolicyDisplayState.NEVER,
        PolicyDisplayState.NEVER,
    ]
    assert [cell.state for cell in rows["sec_native_filings"].cells] == [
        PolicyDisplayState.AUTOMATIC,
        PolicyDisplayState.ON_DEMAND,
        PolicyDisplayState.NEVER,
        PolicyDisplayState.NEVER,
    ]
    assert [cell.state for cell in rows["ir_documents"].cells] == [
        PolicyDisplayState.AUTOMATIC,
        PolicyDisplayState.ON_DEMAND,
        PolicyDisplayState.NEVER,
        PolicyDisplayState.NEVER,
    ]
    assert [cell.state for cell in rows["text_transcripts"].cells] == [
        PolicyDisplayState.AUTOMATIC,
        PolicyDisplayState.ON_DEMAND,
        PolicyDisplayState.NEVER,
        PolicyDisplayState.NEVER,
    ]
    assert all(cell.state is PolicyDisplayState.NEVER for cell in rows["webcasts"].cells)


def test_fmp_operational_state_is_typed_and_explicitly_not_live() -> None:
    state = build_data_policy_settings_view().fmp_state

    assert state.integration_state == "not_yet_wired"
    assert state.provider_mode == "not_yet_wired"
    assert state.circuit_state == "not_yet_wired"
    assert state.backlog_count is None
    assert state.next_probe_at is None


def test_panel_is_legible_read_only_and_names_owner_approved_ir_sources() -> None:
    html = render_data_policy_settings_panel()

    assert 'data-settings-panel="data-collection"' in html
    assert "Portfolio" in html
    assert "Evaluation" in html
    assert "Watchlist" in html
    assert "Index members" in html
    assert "Automatic full" in html
    assert "Metadata only" in html
    assert "Screening only" in html
    assert "SEC CompanyFacts" in html
    assert "SEC native filings" in html
    assert "IR financial documents" in html
    assert "Text transcripts" in html
    assert "Webcasts" in html
    assert "Last 5 reported quarters" in html
    assert "https://ir.rubrik.com/financials/quarterly-results/default.aspx" in html
    assert "https://investors.wix.com/financials" in html
    assert "rubrik_quarter_table" in html
    assert "wix_visible_quarter" in html
    assert "not yet wired" in html.lower()
    assert "No live health claim is shown" in html
    assert "Save" not in html
    assert "Run now" not in html
    assert "fetch(" not in html
    assert "<form" not in html


def test_work_os_exposes_an_accessible_settings_tab_without_demo_health_claims() -> None:
    html = render_work_os_shell()

    assert 'id="opsTabQueue"' in html
    assert 'id="opsTabSettings"' in html
    assert (
        'id="opsTabQueue" class="k-chip k-chip-btn k-chip-tab is-on" '
        'role="tab" aria-selected="true" aria-controls="opsPaneQueue" '
        'style="min-block-size:var(--touch-target-size);" tabindex="0"' in html
    )
    assert (
        'id="opsTabSettings" class="k-chip k-chip-btn k-chip-tab" '
        'role="tab" aria-selected="false" aria-controls="opsPaneSettings" '
        'style="min-block-size:var(--touch-target-size);" tabindex="-1"' in html
    )
    assert 'role="tablist" aria-label="Operations hub views"' in html
    assert 'role="tabpanel" aria-labelledby="opsTabSettings"' in html
    assert 'data-settings-panel="data-collection"' in html
    assert "window.switchOpsTab = function" in html
    assert "aria-selected" in html
    assert "event.key !== 'ArrowLeft' && event.key !== 'ArrowRight'" in html
    assert "Pipeline Health" not in html
    assert "100% Sync" not in html
    assert "78% Free" not in html
    assert "DB Idempotency" not in html


@pytest.fixture
def client(tmp_path: Path) -> FlaskClient:
    (tmp_path / "data").mkdir()
    return comments_server.create_app(tmp_path).test_client()


def test_panel_route_is_read_only(client: FlaskClient) -> None:
    response = client.get("/api/panel/data_policy_settings")

    assert response.status_code == 200
    assert response.mimetype == "text/html"
    assert 'data-settings-panel="data-collection"' in response.get_data(as_text=True)
    assert client.post("/api/panel/data_policy_settings").status_code == 405
