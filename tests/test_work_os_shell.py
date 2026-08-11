"""Structural contract for the production Harvey-style Work OS shell."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path

from pipeline.work_os_shell import SCREEN_SPECS, render_work_os_shell


def test_work_os_has_only_the_eight_persistent_destinations() -> None:
    assert [screen.screen_id for screen in SCREEN_SPECS] == [
        "screen-cockpit",
        "screen-performance",
        "screen-allocation",
        "screen-workspace",
        "screen-brief-library",
        "screen-analytics-playground",
        "screen-audit-log",
        "screen-execution-queue",
    ]


def test_work_os_shell_preserves_the_prototype_navigation_and_layers() -> None:
    html = render_work_os_shell(generated_at=datetime(2026, 8, 7, tzinfo=UTC))
    for screen in SCREEN_SPECS:
        assert f'id="{screen.screen_id}"' in html
        assert f'id="{screen.nav_id}"' in html
        assert screen.label in html
    assert html.count('class="screen-view') == len(SCREEN_SPECS)
    assert "L1 · Portfolio Intelligence" in html
    assert "L2 · Research Engine" in html
    assert "L3 · Operations & Governance" in html
    assert (
        len(re.findall(r'<button type="button"[^>]+class="k-btn k-btn-quiet nav-item', html)) == 8
    )


def test_work_os_shell_retires_standalone_legacy_frontend_destinations() -> None:
    html = render_work_os_shell()
    for retired in (
        'id="nav-discovery"',
        'id="nav-diet"',
        'id="nav-review"',
        'id="nav-journal"',
        'id="nav-triage"',
        'id="nav-mobile-inbox"',
        'id="nav-provenance"',
    ):
        assert retired not in html


def test_rebalance_and_broker_execution_are_not_product_actions() -> None:
    html = render_work_os_shell()
    for forbidden in (
        "Trade Order Execution",
        "Execute Batch Rebalance",
        "Ratify & Trade",
        "executeTradeOrder",
        "executeBatchRebalance",
        "openTradeModal",
    ):
        assert forbidden not in html
    assert "Buy / Hold / Trim / Sell Thresholds" in html
    assert "Next-Dollar Allocation" in html


def test_prototype_buttons_do_not_fake_backend_mutation_or_pipeline_execution() -> None:
    html = render_work_os_shell()
    for forbidden in (
        "manual_trigger",
        "finished successfully!",
        "saved to DB",
        "Disconfirming limit settings saved",
        "Save DCF Calibration",
        "Save Transport Config",
    ):
        assert forbidden not in html
    assert "Open Live DCF Operations" in html
    assert "Open Live Routing Operations" in html
    assert "openLiveDetail('screen-execution-queue')" in html


def test_work_os_shell_uses_live_backend_mounts_without_removing_old_endpoints() -> None:
    html = render_work_os_shell()
    expected = {
        "screen-cockpit": "/api/panel/overview",
        "screen-performance": "/api/panel/portfolio_allocation",
        "screen-allocation": "/api/panel/portfolio_health",
        "screen-workspace": "/api/panel/holding",
        "screen-brief-library": "/api/work-os/briefs",
        "screen-analytics-playground": "/api/panel/explore",
        "screen-audit-log": "/api/panel/portfolio_record",
        "screen-execution-queue": "/api/panel/provenance",
    }
    for screen_id, endpoint in expected.items():
        assert f'"{screen_id}": "{endpoint}"' in html
    assert "workOsLoadScreen" in html
    assert "AbortController" in html
    assert 'aria-live="polite"' in html


def test_work_os_deep_links_old_surfaces_into_the_eight_screen_ia() -> None:
    html = render_work_os_shell()
    for old_hash, screen_id in {
        "overview": "screen-cockpit",
        "holding": "screen-workspace",
        "screen-full-brief": "screen-brief-library",
        "diet": "screen-workspace",
        "portfolio_risk": "screen-allocation",
        "musings": "screen-audit-log",
        "journal": "screen-audit-log",
        "provenance": "screen-execution-queue",
    }.items():
        assert f'"{old_hash}": "{screen_id}"' in html
    assert "hashchange" in html
    assert "history.replaceState" in html


def test_work_os_shell_has_one_search_ask_entry_and_accessible_transients() -> None:
    html = render_work_os_shell()
    assert html.count("Search / Ask") == 1
    assert 'aria-label="Search or ask"' in html
    assert 'role="dialog"' in html
    assert 'aria-modal="true"' in html
    assert html.count('aria-hidden="true"') >= 2
    assert "CCOverlay.register" in html
    assert "trapFocus: true" in html
    assert "restoreFocus: true" in html
    assert "cc-overlay-scrim" in html
    assert "prefers-reduced-motion" in html
    assert "focus()" in html


def test_work_os_search_and_command_k_open_the_one_copilot_workspace() -> None:
    html = render_work_os_shell()

    assert html.count('id="workOsCopilot"') == 1
    assert html.count('id="workOsCopilotThread"') == 1
    assert "openWorkOsCopilot()" in html
    assert "workOsOpenCopilot" in html
    assert "ev.key.toLowerCase() === 'k'" in html
    assert "metaKey || ev.ctrlKey" in html
    assert html.count("ev.key.toLowerCase() === 'k'") == 1
    assert "event.key.toLowerCase() === 'k'" not in html
    assert html.count('class="screen-view') == len(SCREEN_SPECS)


def test_work_os_shell_composes_the_canonical_dark_control_baseline() -> None:
    html = render_work_os_shell()

    assert '<style id="work-os-controls-css">' in html
    assert "--bg: #090a0c" in html
    assert ":root { color-scheme: dark;" in html
    assert 'input[type="search"]' in html
    assert "select, textarea" in html
    assert "font-family: var(--sans)" in html
    assert "border: 1px solid var(--border)" in html
    assert "border-radius: var(--radius)" in html
    assert html.index('id="work-os-controls-css"') < html.index('id="work-os-copilot-css"')


def test_work_os_cards_use_canonical_density_and_type_roles_before_and_after_hydration() -> None:
    html = render_work_os_shell()

    # The three static placeholders and the hydrated template share the same
    # compact card geometry instead of visibly jumping after the API response.
    assert html.count('class="k-card k-card-dense k-card-interactive"') >= 4
    assert 'class="k-card k-card-interactive" style="padding:' not in html
    assert html.count('class="k-card-row-title"') >= 4
    assert html.count('class="k-card-meta"') >= 4

    # Ordinary research headings use explicit title roles; metric cards retain
    # their separate stat label/number semantics.
    assert '<h2 class="k-card-title" id="workOsBriefReaderTitle">' in html
    assert '<h2 class="k-card-title">Brief Library</h2>' in html
    assert '<h3 class="k-card-title">' in html
    assert 'class="k-card k-card-stack"><div class="stat-heading">Owner posture</div>' in html
    assert 'class="stat-number" id="deskOwnerState"' in html


def test_legacy_search_ask_drawer_and_non_durable_runtime_are_removed() -> None:
    html = render_work_os_shell()

    assert "ask-copilot" not in html
    assert "executeCopilotQuery" not in html
    assert "copilotInput" not in html
    assert "copilotResponse" not in html
    assert "fetch('/api/ask'," not in html
    assert html.count("fetch('/api/ask/stream'") == 1
    assert "work-os:ask-session" not in html
    assert "AI COPILOT ANALYSIS" not in html
    assert "Grounded Citations: doc:bcb_jun26_p4" not in html
    assert 'role="status"' in html
    assert 'role="alert"' in html


def test_prototype_template_has_no_dead_copilot_runtime_to_strip() -> None:
    prototype = (
        Path(__file__).resolve().parents[1] / "mockups" / "harvey_sidebar_flow.html"
    ).read_text(encoding="utf-8")

    assert "openDrillDrawer('ask-copilot')" not in prototype
    assert "type === 'ask-copilot'" not in prototype
    assert "populateCopilotPrompt" not in prototype
    assert "executeCopilotQuery" not in prototype
    assert prototype.count("openWorkOsCopilot()") == 2


def test_full_brief_is_transient_reader_state_not_persistent_navigation() -> None:
    html = render_work_os_shell()
    assert 'id="nav-brief-library"' in html
    assert 'id="nav-full-brief"' not in html
    assert 'id="workOsBriefReader"' in html
    assert 'aria-modal="true"' in html
    assert "openWorkOsBriefReader" in html
    assert "navigateTo('screen-full-brief')" not in html
    assert "workOsLoadBriefArtifact" in html
    assert "artifact.body_url" in html
    assert "attachShadow" in html
    assert "report_reader_payload.v1" in html
    assert "content.innerHTML = payload.body_html" in html
    brief_loader = html.split("async function workOsLoadBriefArtifact", 1)[1].split(
        "window.openWorkOsBriefReader", 1
    )[0]
    assert "<iframe" not in brief_loader
    assert "legacy brief has not been migrated" in html


def test_company_desk_and_library_use_production_read_models_not_demo_facts() -> None:
    html = render_work_os_shell()

    assert 'data-layout="decision-workbench"' in html
    assert 'data-layout="report-library"' in html
    assert "/api/work-os/companies/" in html
    assert "workOsRenderBriefLibrary" in html
    assert html.count("async function workOsRenderCompanyDesk") == 1
    assert html.count("window.openWorkOsBriefReader = async function") == 1
    assert 'id="deskOwnerState"' in html
    assert 'id="deskModelState"' in html
    assert 'id="workOsBriefLibrary"' in html
    assert "Mexico deposits crossed" not in html
    assert "Structural Compounder in Latin American" not in html
    assert "data-research-chat" in html
    assert "openWorkOsCopilot" in html
    assert "item.list_type === 'portfolio' || item.list_type === 'evaluation'" in html
    assert "Company Desk (' + (identity.ticker || normalized) + ')'" in html
    assert "try { await workOsEnsureResearchCompanies(); }" in html


def test_cockpit_hydration_does_not_construct_company_desk() -> None:
    html = render_work_os_shell()
    assert "fetch('/api/work-os/portfolio'" in html
    assert 'id="workOsPortfolioStats"' in html
    assert 'id="workOsActionQueue"' in html
    assert 'id="workOsPortfolioRows"' in html
    assert "workOsHydratePortfolio" in html
    assert "workOsRenderCompanyDesk" in html
    portfolio_match = re.search(
        r"function workOsRenderPortfolio\(payload\).*?\n  \}\n\n  function "
        r"workOsApplyRequestedResearchState",
        html,
        re.DOTALL,
    )
    assert portfolio_match is not None
    portfolio_runtime = portfolio_match.group(0)
    assert "workOsRenderCompanyDesk(" not in portfolio_runtime
    assert "/api/tickers" not in portfolio_runtime
    assert "workOsApplyRequestedResearchState" in html
    assert html.rfind("workOsApplyRequestedResearchState();") > html.find(
        "Portfolio companies could not be loaded"
    )
    assert "workOsLaunchParams.get('ticker')" in html
    assert "workOsLaunchParams.get('screen')" in html
    assert "originalSwitchCompanyWorkspace" not in html


def test_mobile_inbox_is_the_same_responsive_cockpit() -> None:
    html = render_work_os_shell()
    assert "100dvh" in html
    assert "env(safe-area-inset-bottom)" in html
    assert "@media (max-width:" in html
    assert 'data-mobile-surface="cockpit"' in html
    assert "width: var(--sidebar-collapsed-width)" in html
    assert "min-width: var(--sidebar-collapsed-width)" in html
    assert "min-block-size: var(--touch-target-size)" in html
    assert "font-size: var(--mobile-control-font-size) !important" in html
    assert "overflow-x: hidden; overflow-y: auto" in html


def test_design_directive_records_the_simplification_boundary() -> None:
    directive = (
        Path(__file__).resolve().parents[1] / "directives" / "design_language.md"
    ).read_text(encoding="utf-8")
    assert "These eight destinations are the complete primary IA" in directive
    assert "Cockpit is the only inbox" in directive
    assert "No trade-execution surface" in directive
    assert "Ask may create governed thesis and KPI proposal cards" in directive
    assert "explicit Owner" in directive
    assert "approval before the owning domain module applies it" in directive
    assert "Diet destination and general-purpose feed are retired" in directive
    assert "Discovery has no primary navigation" in directive
    assert "One responsive product" in directive
    assert "clears it for the current session" in directive
    assert "Only an explicit decision or threshold change may create durable state" in directive


def test_company_desk_renders_governed_valuation_provenance() -> None:
    html = render_work_os_shell()

    assert 'id="deskInputPrice"' in html
    assert 'id="deskFairValue"' in html
    assert "Weight unavailable" in html
    assert "position.price_as_of" in html
    assert "position.fair_value_as_of" in html
    assert "workOsMoney(position.price, position.currency)" in html
    assert "workOsMoney(position.fair_value, position.currency)" in html


def test_nvo_action_queue_open_company_uses_the_canonical_desk_handoff() -> None:
    html = render_work_os_shell()

    assert "data-work-os-ticker=\"' + escapeWorkOsHtml(action.ticker) + '\">Open Company" in html
    assert "switchCompanyWorkspace(node.dataset.workOsTicker)" in html
    assert (
        "const requested = String(ticker || window.workOsActiveTicker || '').toUpperCase();" in html
    )
    assert "fetch('/api/work-os/companies/' + encodeURIComponent(normalized) + '/desk'" in html
