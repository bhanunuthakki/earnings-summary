"""Structural contract for the production Harvey-style Work OS shell."""

from __future__ import annotations

import inspect
import json
import re
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from pipeline.work_os_shell import COCKPIT_STAT_SPECS, SCREEN_SPECS, render_work_os_shell
from pipeline.work_os_styles import WORK_OS_CSS
from ui.conformance_scan import scan_surface_evidence


def _screen_fragment(html: str, screen_id: str) -> str:
    marker = f'id="{screen_id}" class="screen-view'
    start = html.index(marker)
    next_screen = html.find('class="screen-view', start + len(marker))
    return html[start:] if next_screen == -1 else html[start:next_screen]


def test_work_os_has_the_unified_performance_risk_destination() -> None:
    assert [screen.screen_id for screen in SCREEN_SPECS] == [
        "screen-cockpit",
        "screen-performance",
        "screen-workspace",
        "screen-evaluation",
        "screen-brief-library",
        "screen-analytics-playground",
        "screen-audit-log",
        "screen-execution-queue",
    ]
    assert (
        next(
            screen.label
            for screen in SCREEN_SPECS
            if screen.screen_id == "screen-analytics-playground"
        )
        == "Facts & Analytics"
    )


def test_portfolio_copilot_home_is_the_compact_three_part_operating_loop() -> None:
    """The home screen is a live portfolio loop, not a second dashboard."""
    html = render_work_os_shell()
    cockpit = _screen_fragment(html, "screen-cockpit")

    assert "Portfolio at a Glance" in cockpit
    assert "Evaluation dialogues" in cockpit
    assert "Recent owner dialogue and ready-to-discuss workups" in cockpit
    assert "not the full evaluation list" in cockpit
    assert "Portfolio pulse" not in cockpit
    assert "Portfolio Companies" not in cockpit
    assert "Performance" not in cockpit.split('id="screen-performance"', 1)[0]
    assert "Risk &amp; Factors" not in cockpit
    assert "Action Queue &amp; Review Pack" not in cockpit


def test_portfolio_copilot_home_composes_live_sortable_holdings_and_bounded_dialogues() -> None:
    html = render_work_os_shell()
    cockpit = _screen_fragment(html, "screen-cockpit")

    assert 'id="workOsPortfolioRows"' in cockpit
    assert 'id="workOsEvaluationDialogues"' in cockpit
    assert cockpit.count("data-work-os-portfolio-sort=") == 5
    for label in ("Company", "Weight", "Price/Target", "Status", "Key Links"):
        assert f">{label}</span>" in cockpit
    assert "workOsSortPortfolioRows" in html
    assert "fetch('/api/work-os/evaluation-dialogues?limit=3'" in html
    assert "workOsRenderEvaluationDialogues" in html
    assert "workOsOpenEvaluationDialogue" in html
    assert "workOsOpenEvaluationWorkup" in html
    assert "workOsCompareEvaluation" in html
    assert "data-work-os-evaluation-session" in html
    assert "data-work-os-evaluation-instrument" in html
    assert "escapeWorkOsHtml(linked ? sessionId : '')" in html
    dialogue_runtime = html.split("function workOsOpenEvaluationDialogue(button)", 1)[1].split(
        "function workOsOpenEvaluationWorkup", 1
    )[0]
    assert "window.openWorkOsCopilotSession(sessionId)" in dialogue_runtime
    workup_runtime = html.split("function workOsOpenEvaluationWorkup(button)", 1)[1].split(
        "function workOsCompareEvaluation", 1
    )[0]
    assert "instrument !== 'stock' && instrument !== 'etf'" in workup_runtime
    assert "'/api/peek/etf_workup?ticker='" in workup_runtime
    assert "window.switchCompanyWorkspace(safeTicker)" in workup_runtime
    compare_runtime = html.split("function workOsCompareEvaluation(ticker)", 1)[1].split(
        "document.addEventListener('click'", 1
    )[0]
    assert "'/api/peek/discovery-compare?tickers='" in compare_runtime
    assert "window.switchFactPlayground" not in compare_runtime
    assert "function workOsBindPortfolioInteractions()" in html
    sort_runtime = html.split("function workOsSortPortfolioRows(key)", 1)[1].split(
        "function workOsBindPortfolioInteractions", 1
    )[0]
    assert "workOsBindPortfolioInteractions();" in sort_runtime
    assert "href=\"/ticker/' + encodeURIComponent(company.ticker)" in html
    assert "company.dcf_url" in html
    assert "event.target.closest('button, a')" in html
    assert "if (!opened) window.location.assign(node.getAttribute('href'));" in html


def test_evaluation_is_a_complete_live_research_destination() -> None:
    html = render_work_os_shell()
    evaluation = _screen_fragment(html, "screen-evaluation")

    assert "Evaluation" in evaluation
    assert "Complete evaluation coverage" in evaluation
    assert 'id="workOsEvaluationRows"' in evaluation
    assert 'data-live-endpoint="/api/work-os/evaluation" id="screen-evaluation"' in html
    for label in (
        "Company",
        "Type",
        "Thesis",
        "Evaluation",
        "Portfolio fit",
        "DCF upside",
        "Research",
    ):
        assert f">{label}<" in evaluation
    assert "workOsRenderEvaluationSurface" in html
    assert "function workOsFiniteNumber(value)" in html
    assert "if (value == null || String(value).trim() === '') return null;" in html
    assert "fetch('/api/work-os/evaluation'" in html
    assert "Company Desk" in html
    assert "ETF workup" in html
    assert "if (!opened) window.location.assign(target.getAttribute('href'));" in html
    assert "No internal identifiers or encoded payloads are shown" in evaluation


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
    assert len(
        re.findall(r'<button type="button"[^>]+class="k-btn k-btn-quiet nav-item', html)
    ) == len(SCREEN_SPECS)
    operations_nav = html.split('id="nav-execution-queue"', 1)[1].split("</button>", 1)[0]
    assert '<span class="nav-text">Operations</span>' in operations_nav
    performance_nav = html.split('id="nav-performance"', 1)[1].split("</button>", 1)[0]
    assert '<span class="nav-text">Performance</span>' in performance_nav
    assert "Performance vs Index" not in performance_nav
    assert "Portfolio Performance vs Index Benchmark" not in html
    assert "breadcrumb.innerText = 'Performance'" in html
    assert "Execution Queue & Operations Hub" not in html
    assert "Operations & Execution Governance Hub" not in html


def test_operations_related_views_stay_inside_the_shell_context() -> None:
    html = render_work_os_shell()
    related = html.split("window.workOsOpenRelatedView =", 1)[1].split("</script>", 1)[0]

    assert "window.workOsOpenRelatedView =" in html
    assert 'href="/api/panel/data_policy_settings"' not in html
    assert "workOsLoadScreen('related-operations', body, endpoint)" in related
    assert "fetch(" not in related


def test_counterread_brand_is_the_accessible_home_control() -> None:
    html = render_work_os_shell(generated_at=datetime(2026, 8, 7, tzinfo=UTC))

    assert "<title>Counterread</title>" in html
    assert html.count(">Counterread</span>") == 1
    assert 'class="sidebar-home k-btn k-btn-quiet"' in html
    assert 'aria-label="Counterread home"' in html
    assert 'onclick="goCounterreadHome()"' in html
    assert 'class="counterread-mark"' in html
    assert 'aria-hidden="true"' in html
    assert 'data-counterread-observation="true"' in html
    assert 'stroke="currentColor"' in html
    assert 'fill="currentColor"' in html
    assert "EQUITY</span> OS" not in html
    assert "Equity Research OS" not in html
    assert html.count('<link rel="icon"') == 1


def test_counterread_home_uses_canonical_cockpit_history_without_losing_company_context() -> None:
    html = render_work_os_shell()

    assert "function workOsScreenUrl(screenId)" in html
    assert "params.delete('screen');" in html
    assert "url.hash = screenId;" in html
    assert (
        "const currentUrl = window.location.pathname + window.location.search + window.location.hash;"
        in html
    )
    assert "currentUrl !== workOsScreenUrl(target)" in html
    assert "window.history.pushState({ screenId: target }, '', workOsScreenUrl(target));" in html
    assert "window.goCounterreadHome = function ()" in html
    assert "window.navigateTo('screen-cockpit')" in html
    screen_url = html.split("function workOsScreenUrl(screenId)", 1)[1].split(
        "window.navigateTo = function", 1
    )[0]
    assert "params.delete('ticker')" not in screen_url


def test_counterread_home_survives_collapsed_and_mobile_sidebar_rules() -> None:
    html = render_work_os_shell()

    assert ".app-sidebar.is-collapsed .sidebar-logo" in html
    assert ".app-sidebar.is-collapsed .sidebar-brand" in html
    assert "flex-direction: column" in html
    assert ".sidebar-collapse-toggle, .nav-layer-title" in html
    assert ".sidebar-brand button, .nav-layer-title" not in html
    assert ".sidebar-home" in html
    assert "min-block-size: var(--touch-target-size)" in html
    assert "min-inline-size: var(--touch-target-size)" in html
    assert ".counterread-mark" in html
    assert "inline-size: var(--icon-size)" in html
    assert "block-size: var(--icon-size)" in html


def test_counterread_home_closes_transient_research_surfaces_before_navigation() -> None:
    html = render_work_os_shell()
    home_runtime = html.split("window.goCounterreadHome = function ()", 1)[1].split(
        "function workOsApplyHash", 1
    )[0]

    assert "briefReaderOverlay.close()" in home_runtime
    assert "drillOverlay.close()" in home_runtime
    assert "peekOverlay.close()" in home_runtime
    assert home_runtime.index("briefReaderOverlay.close()") < home_runtime.index(
        "window.navigateTo('screen-cockpit')"
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
        "screen-performance": "/api/panel/performance_risk",
        "screen-workspace": "/api/panel/holding",
        "screen-evaluation": "/api/work-os/evaluation",
        "screen-brief-library": "/api/work-os/briefs",
        "screen-analytics-playground": "/api/panel/explore",
        "screen-audit-log": "/api/panel/portfolio_record",
        "screen-execution-queue": "/api/panel/operations",
    }
    for screen_id, endpoint in expected.items():
        assert f'"{screen_id}": "{endpoint}"' in html
    assert "workOsLoadScreen" in html
    assert "AbortController" in html
    assert 'aria-live="polite"' in html


def test_persistent_portfolio_and_audit_screens_are_live_first_not_prototypes() -> None:
    html = render_work_os_shell()
    expected_mounts = {
        "screen-performance": "workOsPerformanceMount",
        "screen-audit-log": "workOsAuditMount",
    }

    for screen_id, mount_id in expected_mounts.items():
        fragment = _screen_fragment(html, screen_id)
        assert f'id="{mount_id}"' in fragment
        assert f'data-work-os-screen-id="{screen_id}"' in fragment
        assert 'data-work-os-refresh-screen="' + screen_id + '"' in fragment
        assert "Loading live" in fragment

    # These values belonged to the visual prototype and must never impersonate
    # current portfolio or decision state in the production shell.
    for stale_prototype_value in ("+32.4%", "$1,284,500", "BKNG", "Sharpe Ratio"):
        assert stale_prototype_value not in "".join(
            _screen_fragment(html, screen_id) for screen_id in expected_mounts
        )

    assert "const workOsPersistentMountIds" in html
    assert "data-work-os-refresh-screen" in html


def test_company_desk_research_items_doorway_stays_inside_decision_audit_log() -> None:
    html = render_work_os_shell()
    assert 'id="workOsManageResearchItems"' in html
    assert "window.navigateTo('screen-audit-log')" in html
    assert "csec-research-items" in html


def test_live_backend_mount_executes_local_fragment_scripts_without_optional_htmx() -> None:
    html = render_work_os_shell()

    assert "window.workOsMountHtml" in html
    assert "script.replaceWith(replacement)" in html
    assert "workOsMountHtml(target, markup, endpoint)" in html
    assert "window.htmx" not in html
    assert "window.livingGrid = window.livingGrid || function" in html
    assert ".lg { min-width: 0; max-width: 100%; overflow-x: auto; }" in html
    assert "Live data loaded" not in html
    assert "Live data fetched" in html
    assert "const workOsRequests = new WeakMap()" in html
    assert "WORK_OS_FETCH_TIMEOUT_MS = 15000" in html
    assert "prior.abortReason = 'superseded'" in html
    assert "Untrusted fragment script source" in html
    assert "data-work-os-retry" in html
    assert "workOsAbortTarget(document.getElementById('drawerBody'), 'hidden')" in html


def test_live_loader_runtime_rejects_stale_completion_and_times_out() -> None:
    node = shutil.which("node")
    if node is None:
        return
    html = render_work_os_shell()
    runtime = html.split("  function workOsTrustedFragmentEndpoint", 1)[1].split(
        "  function openLiveDetail", 1
    )[0]
    runtime = "function workOsTrustedFragmentEndpoint" + runtime
    harness = f"""
const workOsRequests = new WeakMap();
let workOsRequestGeneration = 0;
const WORK_OS_FETCH_TIMEOUT_MS = 20;
const WORK_OS_ENDPOINTS = {{'screen-a': '/api/a'}};
function workOsEndpoint(screenId) {{ return WORK_OS_ENDPOINTS[screenId] || ''; }}
const listeners = {{}};
global.window = {{
  location: new URL('http://127.0.0.1:7421/'), setTimeout, clearTimeout
}};
global.document = {{
  createElement: () => ({{ attributes: [], setAttribute() {{}}, textContent: '', replaceWith() {{}} }}),
  getElementById: () => null,
  addEventListener: (name, fn) => {{ listeners[name] = fn; }}
}};
function target() {{
  return {{
    innerHTML: '', dataset: {{}}, attributes: {{}}, isConnected: true,
    closest() {{ return null; }},
    setAttribute(k, v) {{ this.attributes[k] = v; }},
    removeAttribute(k) {{ delete this.attributes[k]; }},
    querySelectorAll() {{ return []; }}
  }};
}}
{runtime}
(async () => {{
  const pending = [];
  const pendingUrls = [];
  global.fetch = (url, options) => new Promise((resolve, reject) => {{
    options.signal.addEventListener('abort', () => {{
      const error = new Error('aborted'); error.name = 'AbortError'; reject(error);
    }});
    pendingUrls.push(url);
    pending.push(resolve);
  }});
  const mount = target();
  const first = workOsLoadScreen('related-operations', mount, '/api/related-first');
  const second = workOsLoadScreen('related-operations', mount, '/api/related-second');
  if (pendingUrls[0] !== '/api/related-first' || pendingUrls[1] !== '/api/related-second') {{
    throw new Error('related endpoint override was not governed');
  }}
  pending[1]({{ok:true, text:async()=>'<p>second</p>'}});
  await second;
  if (mount.innerHTML !== '<p>second</p>') throw new Error('second response not mounted');
  pending[0]({{ok:true, text:async()=>'<p>stale</p>'}});
  await first;
  if (mount.innerHTML !== '<p>second</p>') throw new Error('stale response mounted');

  global.fetch = (_url, options) => new Promise((_resolve, reject) => {{
    options.signal.addEventListener('abort', () => {{
      const error = new Error('aborted'); error.name = 'AbortError'; reject(error);
    }});
  }});
  const timeoutMount = target();
  await workOsLoadScreen('screen-a', timeoutMount);
  if (!timeoutMount.innerHTML.includes('timed out') ||
      !timeoutMount.innerHTML.includes('data-work-os-retry')) {{
    throw new Error('timeout retry state missing');
  }}

  let hiddenAbort = 0;
  global.fetch = (_url, options) => new Promise((_resolve, reject) => {{
    options.signal.addEventListener('abort', () => {{
      hiddenAbort += 1;
      const error = new Error('aborted'); error.name = 'AbortError'; reject(error);
    }});
  }});
  const hiddenMount = target();
  const hiddenRequest = workOsLoadScreen('screen-a', hiddenMount);
  workOsAbortTarget(hiddenMount, 'hidden');
  await hiddenRequest;
  if (hiddenAbort !== 1) throw new Error('drawer close did not abort the owned request');
  if (hiddenMount.innerHTML) throw new Error('hidden request mounted stale content');
}})().catch(error => {{ console.error(error); process.exitCode = 1; }});
"""
    result = subprocess.run(
        [node, "-"], input=harness, text=True, capture_output=True, check=False, timeout=10
    )
    assert result.returncode == 0, result.stderr


def test_fact_playground_is_a_live_governed_mount_not_static_verified_demo_data() -> None:
    html = render_work_os_shell()

    assert 'id="screen-analytics-playground"' in html
    assert 'id="workOsFactPlayground"' in html
    assert "workOsRenderFactPlayground" in html
    assert "fragment=work-os" in html
    assert "window.initExplorePanel()" in html
    assert 'id="workOsFactTicker"' in html
    assert "work-os-explore-tickers" in html
    assert "new Function" not in html
    assert "eval(" not in html
    assert "EXTRACTED_FACTS_DB" not in html
    assert "100% PROVENANCE VERIFIED" not in html
    assert "Mexico Deposits ($B)" not in html
    assert "No prototype values are being shown" in html


def test_work_os_deep_links_old_surfaces_into_the_unified_screen_ia() -> None:
    html = render_work_os_shell()
    for old_hash, screen_id in {
        "overview": "screen-cockpit",
        "holding": "screen-workspace",
        "screen-full-brief": "screen-brief-library",
        "diet": "screen-workspace",
        "portfolio_risk": "screen-performance",
        "musings": "screen-audit-log",
        "journal": "screen-audit-log",
        "provenance": "screen-execution-queue",
    }.items():
        assert f'"{old_hash}": "{screen_id}"' in html
    assert "hashchange" in html
    assert "history.replaceState" in html


def test_work_os_transient_history_uses_the_typed_route_wire_and_replays_only_known_surfaces() -> (
    None
):
    """Back/Forward has a closed state contract for the two supported transients."""

    html = render_work_os_shell()

    assert "WORK_OS_ROUTE_DESTINATIONS" in html
    assert "WORK_OS_HISTORY_DRAWER_TYPES" in html
    assert "function workOsEncodeHistoryRoute(route)" in html
    assert "function workOsRouteFromHistoryState(state)" in html
    assert "workOsRoute: workOsEncodeHistoryRoute(route)" in html
    assert "workOsPushTransientHistory('risk_drawer'" in html
    assert "workOsPushTransientHistory('peek'" in html
    assert "window.history.back();" in html
    assert "window.addEventListener('popstate', function () { workOsApplyHash(false); });" in html
    assert "workOsRestoreTransientFromHistory(window.history.state)" in html
    assert "workOsCloseTransientFromHistory('risk_drawer')" in html
    assert "workOsCloseTransientFromHistory('peek')" in html
    assert "WORK_OS_HISTORY_DRAWER_TYPES.has(type)" in html
    assert "if (workOsReplayingHistory) return false;" in html


def test_work_os_transient_history_preserves_only_existing_origin_and_focus_state() -> None:
    html = render_work_os_shell()

    assert "function workOsHistoryOrigin()" in html
    assert "focusId: workOsHistoryFocusId()" in html
    assert "workOsRestoreHistoryFocus(workOsLastTransientFocusId);" in html
    assert "workOsCloseHistoryTransients();" in html
    assert "workOsOpenPeekRoute(transient.route, transient.title, { fromHistory: true })" in html
    assert "window.openDrillDrawer(transient.drawerType, { fromHistory: true });" in html


def test_work_os_routed_peeks_have_one_safe_full_page_host_and_deep_link_contract() -> None:
    """Only registered read-only content routes can escape the compact peek."""

    html = render_work_os_shell()

    assert 'id="workOsFullPageDetail"' in html
    assert 'id="workOsPeekOpenFullPage"' in html
    assert 'id="workOsFullPageDetailBack"' in html
    assert "const WORK_OS_FULL_PAGE_PEEK_PATHS" in html
    assert "earnings-prep|earnings-readout" in html
    assert "new RegExp('^/source/[0-9]+$')" in html
    assert "function workOsCanonicalDetailRoute(route)" in html
    assert "function workOsOpenPeekFullPage(route, title, options)" in html
    assert "work_os_detail_origin" in html
    assert "window.history.pushState" in html
    assert "function workOsClosePeekFullPage()" in html
    assert "surface: 'screen-cockpit'" in html
    assert "window.matchMedia('(max-width: 47.5rem)').matches" in html
    assert (
        "originalOpenPeekDrawer(refKey)"
        not in html.split("function workOsOpenPeekFullPage(route, title, options)", 1)[1].split(
            "function workOsAbortPeekRequest", 1
        )[0]
    )


def test_work_os_full_brief_and_threshold_return_contracts_are_routed() -> None:
    html = render_work_os_shell()

    assert "function workOsBriefUrl(ticker, origin, focusId)" in html
    assert "work_os_brief" in html
    assert "workOsBriefReader" in html
    assert "window.openWorkOsBriefReader(briefTicker, { fromHistory: true })" in html
    assert "if (briefReaderOverlay) briefReaderOverlay.close();" in html
    assert "function workOsOpenThresholdReview(ticker)" in html
    assert "'/advisor/sizing-intents/' + encodeURIComponent(safeTicker)" in html
    assert "url.searchParams.set('work_os_origin'" in html
    assert "workOsOpenThresholdReview(node.dataset.workOsThresholds)" in html


def test_work_os_full_page_detail_reuses_known_content_routes_and_source_fragment_mode() -> None:
    html = render_work_os_shell()
    detail_runtime = html.split("function workOsCanonicalDetailRoute(route)", 1)[1].split(
        "async function workOsOpenPeekRoute", 1
    )[0]

    assert "parsed.pathname.startsWith('/source/')" in detail_runtime
    assert "parsed.searchParams.set('fragment', '1')" in detail_runtime
    assert "headers: { Accept: 'text/html' }" in detail_runtime
    assert "This persisted research detail is unavailable." in detail_runtime
    assert "workOsDecodeDetailOrigin(params.get('work_os_detail_origin'))" in html


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


def test_work_os_copilot_is_not_a_top_right_header_action() -> None:
    html = render_work_os_shell()
    prototype = (
        Path(__file__).resolve().parents[1] / "mockups" / "harvey_sidebar_flow.html"
    ).read_text(encoding="utf-8")
    assert "Ask Copilot" not in prototype
    assert prototype.count("openWorkOsCopilot()") == 1
    assert html.count('id="workOsCopilotLauncher"') == 1


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

    # The compact governed action rail stays a semantic well while preserving
    # the registered controls that are eligible for each exact alert identity.
    assert 'class="k-well work-os-action-card"' in html
    assert 'class="k-card k-card-interactive" style="padding:' not in html
    assert html.count("k-card-row-title") >= 1
    assert html.count('class="k-card-meta"') >= 4

    # Ordinary research headings use explicit title roles; metric cards retain
    # their separate stat label/number semantics.
    assert '<h2 class="k-card-title" id="workOsBriefReaderTitle">' in html
    assert '<h2 class="k-card-title" id="workOsBriefLibraryHeading">Brief Library</h2>' in html
    assert '<h3 class="k-card-title">' in html
    assert 'class="k-stat-cell"><div class="stat-heading">Owner posture</div>' in html
    assert 'class="stat-number" id="deskLivePrice"' in html


def test_l1_card_titles_and_hydration_hooks_compose_the_registry_roles() -> None:
    html = render_work_os_shell()
    target = "".join(
        _screen_fragment(html, screen_id) for screen_id in ("screen-cockpit", "screen-performance")
    )

    cockpit = _screen_fragment(html, "screen-cockpit")
    assert cockpit.count('class="stat-heading"') == 1
    assert target.count('class="k-card k-card-section research-toolbar"') == 1
    performance = _screen_fragment(html, "screen-performance")
    assert '<h1 class="k-card-title">Performance &amp; Risk</h1>' in performance
    assert 'id="workOsPerformanceMount"' in performance
    assert "workOsPortfolioNavDetail" in html
    assert "workOsRenderPortfolioRows(companies);" in html


def test_cockpit_stats_use_typed_keys_and_native_screen_anchors() -> None:
    html = render_work_os_shell()
    cockpit = _screen_fragment(html, "screen-cockpit")

    assert 'data-work-os-stat-key="nav"' in cockpit
    assert 'data-work-os-stat-key="companies"' not in cockpit
    assert 'data-work-os-stat-key="performance"' not in cockpit
    assert 'data-work-os-stat-key="risk"' not in cockpit
    assert "onclick=" not in cockpit
    assert [spec.key for spec in COCKPIT_STAT_SPECS] == ["nav"]
    assert "data-work-os-stat-key" in html
    assert "workOsPortfolioNavDetail" in html
    assert "payload.tracker_detail" in html
    assert "Tracker unavailable · research data only" in html
    render_index = html.index("workOsRenderPortfolio(payload);")
    announce_index = html.index("status.textContent = String(payload.tracker_detail", render_index)
    assert render_index < announce_index
    assert "status.textContent = trackerDetail" not in html


def test_l1_live_shells_do_not_ship_prototype_card_grids_or_inline_geometry() -> None:
    html = render_work_os_shell()
    target = "".join(
        _screen_fragment(html, screen_id) for screen_id in ("screen-cockpit", "screen-performance")
    )

    assert "card-grid-stat-4col" not in target
    assert 'class="card-grid-stat"' not in target
    assert 'class="work-os-portfolio-topline"' in target
    assert '<header class="k-section-head">' in _screen_fragment(html, "screen-cockpit")
    for screen_id in ("screen-performance",):
        fragment = _screen_fragment(html, screen_id)
        assert "style=" not in fragment
        assert fragment.count('class="k-card k-card-section research-toolbar"') == 1


def test_rendered_l1_shell_has_no_hidden_grid_signature_drift() -> None:
    evidence = scan_surface_evidence("rendered-work-os", render_work_os_shell())

    assert "off-scale-grid-column" not in evidence.violations()


def test_l2_l3_shell_composes_semantic_mounts_and_canonical_split_rails() -> None:
    html = render_work_os_shell()

    assert 'id="screen-workspace"' in html
    assert 'role="region" aria-labelledby="workOsCompanyDeskHeading"' in html
    assert (
        'id="workOsCompanyDeskHeading"><span id="companyPickerLabel">Company Desk</span></h1>'
        in html
    )
    assert 'class="research-screen company-desk-approved-grid"' in html
    assert 'class="company-desk-summary-grid"' in html
    assert 'id="screen-brief-library"' in html
    assert 'aria-labelledby="workOsBriefLibraryHeading"' in html
    assert 'id="workOsBriefLibraryHeading">Brief Library</h2>' in html
    assert 'id="screen-analytics-playground"' in html
    assert 'aria-labelledby="workOsFactPlaygroundHeading"' in html
    assert 'id="workOsFactPlaygroundHeading">Fact &amp; Metric Playground</h2>' in html
    assert 'id="screen-execution-queue"' in html
    assert 'role="region" aria-label="Operations"' in html
    assert 'class="k-grid-split-rail" data-layout-signature="k-grid-split-rail"' in html
    assert 'id="workOsOperationsMount"' in html
    assert '<div class="k-card-meta" role="status">Loading declared ownership' in html
    assert "workOsLoadScreen(target, operationsMount)" in html
    assert WORK_OS_CSS in html
    assert (
        "#screen-workspace .research-grid.k-grid-split-rail-lg { "
        "grid-template-columns: minmax(0, 1fr) var(--rail-lg); }"
    ) in WORK_OS_CSS
    assert "grid-template-columns: minmax(0, 1fr) var(--rail-sm);" in html
    assert "grid-template-columns: minmax(0, 1fr) var(--rail-lg);" in html


def test_brief_library_filters_rebuild_tickers_and_expose_accessible_clear_action() -> None:
    html = render_work_os_shell()

    assert 'class="k-btn k-btn-quiet k-btn-sm"' in html
    assert "data-clear-brief-filters" in html
    assert 'aria-label="Clear Brief Library filters"' in html
    assert "function workOsPopulateBriefTickerOptions" in html
    assert "const compatibleCompanies = companies.filter(function (company)" in html
    assert "const selectedTickerIsCompatible = Array.from(tickerFilter.options).some" in html
    assert "if (!selectedTickerIsCompatible) tickerFilter.value = '';" in html
    assert "roleFilter.addEventListener('change', function ()" in html
    assert "workOsPopulateBriefTickerOptions(tickerFilter, roleFilter.value);" in html
    assert (
        "data-clear-brief-filters"
        in html.split("No persisted research artifacts match these filters.", 1)[1]
    )


def test_l2_l3_mobile_uses_target_scoped_block_without_mutating_rail_signature() -> None:
    html = render_work_os_shell()

    assert "#screen-workspace .k-grid-split-rail-lg" in html
    assert "#screen-execution-queue .k-grid-split-rail" in html
    assert "display: block;" in html
    assert (
        ".k-grid-split-rail { display: grid; grid-template-columns: minmax(0, 1fr) 1fr;" not in html
    )
    assert ".k-grid-split-rail-lg { display: grid; grid-template-columns: 1fr 1fr;" not in html


def test_rendered_l2_l3_shell_has_no_target_scan_findings_or_unverifiable_markup() -> None:
    evidence = scan_surface_evidence("rendered-work-os", render_work_os_shell())

    target_dimensions = {
        "floating-card-title",
        "off-scale-grid-column",
        "unsanctioned-shape-geometry",
    }
    assert not target_dimensions.intersection(evidence.violations())
    # The rendered shell deliberately contains remote HTML response bodies and
    # a dynamically created stylesheet link. Source-level digest contracts pin
    # both recipes; the rendered probe keeps the boundary explicit rather than
    # silently treating it as statically verified.
    assert evidence.unverifiable_markup == (
        "dynamic-html-markup",
        "dynamic-visual-value",
    )


def test_rendered_shell_scan_proves_each_target_dimension_is_enforced() -> None:
    html = render_work_os_shell()

    title_mutation = html.replace(
        '<h2 class="k-card-title" id="workOsBriefLibraryHeading">Brief Library</h2>',
        '<h1 class="k-toolbar-title">Brief Library</h1>',
        1,
    )
    title_evidence = scan_surface_evidence("rendered-work-os", title_mutation)
    assert "floating-card-title" in title_evidence.violations()

    grid_mutation = html.replace(
        "</head>",
        "<style>.k-grid-split-rail-lg { grid-template-columns: 1fr 1fr; }</style></head>",
        1,
    )
    grid_evidence = scan_surface_evidence("rendered-work-os", grid_mutation)
    assert "off-scale-grid-column" in grid_evidence.violations()

    shape_mutation = html.replace(
        "</head>",
        "<style>.k-card { border-radius: var(--radius); "
        "border: var(--bw-thin) solid var(--border); "
        "box-shadow: var(--shadow-pop); }</style></head>",
        1,
    )
    shape_evidence = scan_surface_evidence("rendered-work-os", shape_mutation)
    assert "unsanctioned-shape-geometry" in shape_evidence.violations()


def test_work_os_shell_render_signature_remains_keyword_only() -> None:
    signature = inspect.signature(render_work_os_shell)

    assert tuple(signature.parameters) == ("generated_at", "db_path")
    assert signature.parameters["generated_at"].kind is inspect.Parameter.KEYWORD_ONLY
    assert signature.parameters["db_path"].kind is inspect.Parameter.KEYWORD_ONLY


def test_l1_dismissal_transition_survives_the_later_controls_cascade() -> None:
    html = render_work_os_shell()
    controls_at = html.index('id="work-os-controls-css"')
    target_transition = "#screen-cockpit .k-card-interactive {\n      transition: transform 200ms"

    assert target_transition in html[:controls_at]
    assert "opacity 220ms" in html[:controls_at]
    assert "max-height 280ms" in html[:controls_at]
    assert "margin 280ms" in html[:controls_at]
    assert "padding 280ms" in html[:controls_at]
    assert "setTimeout(() => {\n          card.remove();\n        }, 450);" in html


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
    assert prototype.count("openWorkOsCopilot()") == 1


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
    assert 'id="workOsBriefReaderBack"' in html
    assert 'id="workOsBriefReaderDecision"' in html
    assert 'id="workOsBriefReaderSections"' in html
    assert "workOsRenderReaderDecision(payload.decision)" in html
    assert "const owner = projection.owner" in html
    assert "const model = projection.model" in html
    assert "payload.sections" in html
    assert "section.dom_id" in html
    assert "WORK_OS_BRIEF_GROUP_IDS" in html
    for group_id in (
        "overview",
        "quarter",
        "financials",
        "thesis-risk",
        "valuation-comps",
        "sources",
    ):
        assert f"'{group_id}'" in html
    assert ".tab-group-pane[data-tab-group]" in html
    assert ".subtab-pane[data-tab]" in html
    assert "work-os-reader-group-button" in html
    assert "work-os-reader-section-button" in html
    assert "readerGroupActive" in html
    assert "readerSectionActive" in html
    assert "button.setAttribute('aria-expanded'" in html
    assert "sectionButton.setAttribute('aria-current', 'location')" in html
    assert "work-os-report-content k-doc" in html
    assert "editorial.v1" in html
    assert "startsWith('/source/')" in html
    assert "sourceUrl.searchParams.set('fragment', '1')" in html
    assert "window.workOsOpenPeekRoute = workOsOpenPeekRoute" in html
    assert 'data-research-chat="full-brief"' in html
    assert "workOsReaderContext = artifact" in html
    assert "briefReader.contains(trigger) && workOsReaderContext" in html
    assert "const chatTicker = readerScoped ? workOsReaderContext.ticker" in html
    assert "':artifact:' + workOsReaderContext.artifact_id" in html
    assert "sourceUrl.pathname + sourceUrl.search + sourceUrl.hash" in html
    assert "const sourceLocator = parsedRoute.hash ? parsedRoute.hash.slice(1)" in html
    assert "located.classList.add('is-cited-location')" in html
    assert "data-peek-url" in html
    brief_loader = html.split("async function workOsLoadBriefArtifact", 1)[1].split(
        "window.openWorkOsBriefReader", 1
    )[0]
    assert "<iframe" not in brief_loader
    assert "artifact.reader_mode !== 'shared_body'" in html
    assert "artifact.reader_mode !== 'shared_body' || !artifact.body_url" not in html
    assert "legacy brief has not been migrated" in html


def test_company_desk_and_library_use_production_read_models_not_demo_facts() -> None:
    html = render_work_os_shell()

    assert 'data-layout="company-desk-approved"' in html
    assert 'data-layout="report-library"' in html
    assert "/api/work-os/companies/" in html
    assert "workOsRenderBriefLibrary" in html
    assert html.count("async function workOsRenderCompanyDesk") == 1
    assert html.count("window.openWorkOsBriefReader = async function") == 1
    assert 'id="deskLivePrice"' in html
    assert 'id="deskValuationGap"' in html
    assert 'id="workOsBriefLibrary"' in html
    assert "Mexico deposits crossed" not in html
    assert "Structural Compounder in Latin American" not in html
    assert "data-research-chat" in html
    assert "openWorkOsCopilot" in html
    assert "item.list_type === 'portfolio' || item.list_type === 'evaluation'" in html
    assert "function workOsRenderCompanyBreadcrumb()" in html
    assert "breadcrumb.textContent = 'Company Desk';" in html
    assert "try { await workOsEnsureResearchCompanies(); }" in html
    assert 'id="deskQuestionCapture"' in html
    assert "kind: 'question'" in html
    assert "desk.question_store_status === 'unavailable'" in html
    assert "question.origin" in html and "question.approval" in html
    assert "condition.latest_value" in html
    assert "condition.observation_period" in html
    assert "condition.observation_unit || condition.unit" in html
    assert "condition.prior_value" in html
    assert "condition.observation_delta" in html
    assert "No prior observation" in html
    assert "condition.evidence_ref" in html
    assert "condition.status || 'PENDING DATA'" in html


def test_company_desk_condition_row_keeps_telemetry_rule_evidence_and_status_tone() -> None:
    html = render_work_os_shell()
    condition_row = html.split("const conditions =", 1)[1].split("const questions =", 1)[0]

    assert "condition.latest_value" in condition_row
    assert "condition.observation_period" in condition_row
    assert "condition.prior_value" in condition_row
    assert "condition.observation_delta" in condition_row
    assert "condition.evidence_ref" in condition_row
    assert "condition.operator" in condition_row and "condition.threshold" in condition_row
    assert "workOsPillClass(status)" in condition_row
    assert "PENDING DATA" in condition_row


def test_company_desk_separates_current_thesis_risk_from_decision_conditions() -> None:
    html = render_work_os_shell()
    desk = _screen_fragment(html, "screen-workspace")
    desk_ids = re.findall(r'\bid="([^"]+)"', desk)

    assert 'data-testid="decision-card"' in html
    assert 'id="deskTrackingBands"' in html
    assert 'id="deskThesisStatus"' in html
    assert 'id="deskSummaryThesisHeading">Why I own this company</h2>' in html
    assert 'id="deskConditions"' in html
    assert ">Thesis contracts</button>" in html
    assert "desk.thesis_risk" in html
    assert "desk.kpi_summary" in html
    assert "thesisRisk.break_rules" in html
    assert "rule.provenance_ref" in html
    assert "data-desk-thesis-rule" in html
    assert "sectionId: 'thesis'" in html
    assert "factRef: button.getAttribute('data-desk-kpi-evidence')" in html
    assert "No inferred values are shown." in html
    assert "deskThesisBriefDoorway" in html
    assert "Thesis evidence " in html
    assert len(desk_ids) == len(set(desk_ids))


def test_company_desk_evidence_controls_target_the_brief_thesis_and_exact_fact_anchor() -> None:
    """The Desk interaction path carries stable navigation, not copied evidence."""
    html = render_work_os_shell()
    reader = html.split("async function workOsLoadBriefArtifact", 1)[1].split(
        "window.openWorkOsBriefReader", 1
    )[0]
    desk = html.split("async function workOsRenderCompanyDesk", 1)[1].split(
        "async function workOsRenderBriefLibrary", 1
    )[0]

    assert "sectionGroupIds.set(sectionId, groupId)" in reader
    assert "const requestedSectionId = options" in reader
    assert "sectionGroupIds.get(requestedSectionId)" in reader
    assert "root.querySelectorAll('[data-fact-ref]')" in reader
    assert "node.getAttribute('data-fact-ref') === requestedFactRef" in reader
    assert "factAnchor.classList.add('is-cited-location')" in reader
    assert "data-desk-thesis-rule" in desk
    assert "openWorkOsBriefReader(brief, { sectionId: 'thesis' })" in desk
    assert "factRef: button.getAttribute('data-desk-kpi-evidence')" in desk
    assert "const evidenceButton = brief && kpi.evidence_ref" in desk


def test_company_desk_thesis_presentation_is_readable_and_human_labeled() -> None:
    """Thesis prose and rule telemetry must be scannable without losing evidence links."""
    html = render_work_os_shell()
    desk = html.split("async function workOsRenderCompanyDesk", 1)[1].split(
        "async function workOsRenderBriefLibrary", 1
    )[0]

    assert "function workOsSplitThesisSentences" in html
    assert "acronymMarker" in html
    assert "(?=[A-Z0-9(])" in html
    assert 'class="stat-subtext"' in desk
    assert 'class="research-list"' in desk
    assert "function workOsFormatThesisNumber" in html
    assert "maximumFractionDigits: 2" in desk
    assert "function workOsThesisStatus" in html
    assert "PASS" in html and "WATCH" in html and "BREACH" in html and "UNRESOLVED" in html
    assert "workOsFormatThesisNumber(rule.latest_value)" in desk
    assert "workOsFormatThesisNumber(rule.threshold)" in desk
    assert "workOsFormatThesisNumber(rule.distance_to_threshold)" in desk
    assert "String(rule.latest_value)" not in desk
    assert "String(rule.distance_to_threshold)" not in desk
    assert "attentionRules" in desk
    assert "passingCount" in desk
    assert "thesisStatus.textContent = presentation.label" in desk


def test_company_desk_sentence_splitter_preserves_nu_acronyms_and_decimals() -> None:
    """Execute the browser helper against the approved NU thesis and decimal prose."""
    node = shutil.which("node")
    if node is None:
        return
    html = render_work_os_shell()
    splitter = html.split("function workOsSplitThesisSentences", 1)[1].split(
        "function workOsFormatThesisNumber", 1
    )[0]
    thesis_path = Path(__file__).resolve().parents[1] / "micro_thesis/holdings/NU.json"
    thesis = json.loads(thesis_path.read_text(encoding="utf-8"))["thesis"]
    harness = f"""
function workOsSplitThesisSentences{splitter}
const thesis = {json.dumps(thesis)};
const segments = workOsSplitThesisSentences(thesis);
const optionality = segments.filter(segment => segment.includes('Bull-case optionality'));
if (optionality.length !== 1) throw new Error('NU optionality sentence was fragmented');
if (!optionality[0].includes('U.S. (Nubank, N.A.')) throw new Error('NU acronyms were split');
const decimalSegments = workOsSplitThesisSentences('Margin reached 29.5%. Growth stayed above 20.25%.');
if (decimalSegments.length !== 2) throw new Error('decimal sentence boundaries were malformed');
if (!decimalSegments[0].includes('29.5%') || !decimalSegments[1].includes('20.25%')) {{
  throw new Error('decimal values were split');
}}
"""
    result = subprocess.run(
        [node, "-"], input=harness, text=True, capture_output=True, check=False, timeout=10
    )
    assert result.returncode == 0, result.stderr


def test_earnings_peek_ignores_stale_requests_and_aborts_on_close() -> None:
    html = render_work_os_shell()

    assert "let workOsPeekRequestSequence = 0" in html
    assert "let workOsPeekRequestController = null" in html
    assert "const controller = new AbortController()" in html
    assert "signal: controller.signal" in html
    assert "requestSequence !== workOsPeekRequestSequence" in html
    assert "error.name === 'AbortError'" in html
    assert "workOsAbortPeekRequest();" in html
    assert "onBeforeClose: function () { workOsAbortPeekRequest(); }" in html
    assert "peekDrawer.classList.add('is-open')" in html
    assert "peekDrawer.classList.remove('is-open')" in html
    assert "drillDrawer.classList.add('is-open')" in html
    assert "drillDrawer.classList.remove('is-open')" in html


def test_company_desk_identity_ticker_uses_the_display_role_without_changing_shared_tickers() -> (
    None
):
    html = render_work_os_shell()

    assert 'class="k-tick-sym k-tick-sym-display" id="deskTicker"' in html
    assert ".k-tick-sym-display" in html
    assert "font-size: var(--fs-display)" in html
    assert 'class="k-ticker-symbol t-mono" id="deskTicker"' not in html


def test_full_brief_reader_has_a_resolved_modal_stacking_token() -> None:
    """The reader toolbar must stay above sticky app chrome and the scrim."""
    html = render_work_os_shell()

    assert "--z-modal: 300;" in html
    assert ".work-os-reader { position: fixed; inset: 0; z-index: var(--z-modal);" in html


def test_cockpit_hydration_does_not_construct_company_desk() -> None:
    html = render_work_os_shell()
    assert "fetch('/api/work-os/portfolio'" in html
    assert 'id="workOsPortfolioNav"' in html
    assert 'id="workOsActionQueue"' in html
    assert 'id="workOsPortfolioRows"' in html
    assert "workOsHydratePortfolio" in html
    assert "workOsRenderCompanyDesk" in html
    portfolio_runtime = html.split("function workOsRenderPortfolio(payload)", 1)[1].split(
        "async function workOsRenderEvaluationDialogues", 1
    )[0]
    assert "workOsRenderCompanyDesk(" not in portfolio_runtime
    assert "/api/tickers" not in portfolio_runtime
    assert "workOsApplyRequestedResearchState" in html
    assert html.rfind("workOsApplyRequestedResearchState();") > html.find(
        "Portfolio companies could not be loaded"
    )
    assert "workOsLaunchParams.get('ticker')" in html
    assert "function workOsReadCompanyContext()" in html
    assert "originalSwitchCompanyWorkspace" not in html


def test_primary_work_os_cards_use_one_declared_composition_archetype() -> None:
    html = render_work_os_shell()
    cockpit = _screen_fragment(html, "screen-cockpit")
    company = _screen_fragment(html, "screen-workspace")

    assert 'class="k-card k-card-stat work-os-nav-card"' in cockpit
    assert 'class="work-os-nav-card-body"' in cockpit
    assert cockpit.count("data-work-os-stat-key") == 1
    assert 'class="k-card k-card-section work-os-actions-rail"' in cockpit
    assert 'class="k-section-title k-card-title" id="workOsActionHeading"' in cockpit
    assert 'class="k-card-title k-card-row-title"' not in cockpit
    assert 'class="k-section-head"' in cockpit
    assert 'class="k-section-title"' in cockpit
    assert 'class="k-well work-os-action-card"' in html
    assert 'class="k-card k-card-section company-desk-topline"' in company
    assert 'class="company-desk-facts"' in company
    assert company.count('class="k-card k-card-stack"><div class="stat-heading">') == 0
    assert 'class="company-picker-popover k-overlay k-card-stack"' in company


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


def test_design_directive_routes_product_behavior_to_owned_contracts() -> None:
    directive = (
        Path(__file__).resolve().parents[1] / "directives" / "design_language.md"
    ).read_text(encoding="utf-8")
    assert "navigation and destination hierarchy: `directives/navigation_ia.md`" in directive
    assert "`directives/interaction_paradigm_2026_06.md`" in directive
    assert "comments and chat: `directives/report_comments_and_chat.md`" in directive
    assert "operational controls: `directives/operations_governance_surface.md`" in directive
    assert "Those contracts may specify behavior, data, and state" in directive
    assert "do not authorize a" in directive
    assert "new visual recipe" in directive


def test_company_desk_renders_governed_valuation_provenance() -> None:
    html = render_work_os_shell()

    assert 'id="deskLivePrice"' in html
    assert 'id="deskFairValue"' in html
    assert 'id="deskValuationGap"' in html
    assert "Weight unavailable" in html
    assert "position.price_as_of" in html
    assert "position.fair_value_as_of" in html
    assert "workOsMoney(position.price, position.currency)" in html
    assert "workOsMoney(position.fair_value, position.currency)" in html
    assert "Governed valuation snapshot" in html
    assert "positionState === 'not_held' ? 'Not held' : 'Weight unavailable'" in html
    company_desk_runtime = html.split("async function workOsRenderCompanyDesk", 1)[1].split(
        "function workOsBriefFilterCompanies", 1
    )[0]
    assert "company.current_weight_pct" not in company_desk_runtime
    assert "position.position_source === 'portfolio_tracker_api'" in company_desk_runtime
    assert "deskPositionSource" in company_desk_runtime
    assert "deskInputPriceSource" in company_desk_runtime
    assert "deskFairValueSource" in company_desk_runtime
    assert "deskBriefStatus" in company_desk_runtime


def test_company_desk_tabs_use_a_guarded_local_runtime() -> None:
    html = render_work_os_shell()

    assert 'onclick="switchDeskTab' not in html
    assert "function workOsSwitchCompanyDeskSection(section, focus)" in html
    assert "document.querySelectorAll('[data-company-desk-section]')" in html
    assert "event.target.closest('[data-company-desk-section]')" in html
    assert "button.setAttribute('aria-selected', active ? 'true' : 'false')" in html
    assert "panel.hidden = panel.dataset.companyDeskPanel !== section" in html
    assert 'role="tablist"' in html
    assert html.count('role="tab"') == 2
    assert html.count('role="tabpanel"') == 2
    assert "ArrowLeft" in html and "ArrowRight" in html


def test_cockpit_availability_does_not_use_missing_as_of_as_offline() -> None:
    html = render_work_os_shell()

    assert "payload.tracker_detail" in html
    assert "Tracker unavailable · research data only" in html
    assert (
        "payload.as_of ? 'As of ' + payload.as_of : 'Tracker offline - research data only'"
        not in html
    )


def test_company_desk_earnings_doorway_matches_full_brief_canvas_interaction() -> None:
    html = render_work_os_shell()

    assert 'id="workOsEarningsDoorway"' in html
    assert 'class="k-chip is-active" type="button" data-peek-url="' in html
    assert "escapeWorkOsHtml(doorway.route)" in html
    assert "escapeWorkOsHtml(doorway.label)" in html
    assert "Post-earnings readout — ' + ticker" in html
    assert "Earnings prep — ' + ticker" in html
    assert "workOsRenderEarningsDoorway(" in html
    assert "desk.earnings_doorway || null" in html
    assert "desk.latest_earnings_readout || null" in html
    assert "Pre-earnings brief pending" not in html
    assert "Post-earnings readout pending" not in html
    doorway_runtime = html.split("function workOsRenderEarningsDoorway", 1)[1].split(
        "function workOsCompanyByTicker", 1
    )[0]
    assert "data-work-os-full-brief" not in doorway_runtime
    assert "data-peek-url" in doorway_runtime
    assert "if (doorway && doorway.status === 'available' && doorway.route)" in doorway_runtime
    assert "if (doorway && doorway.status === 'pending')" in doorway_runtime
    pending_runtime = doorway_runtime.split("if (doorway && doorway.status === 'pending')", 1)[
        1
    ].split("if (latestButton)", 1)[0]
    assert "fallbackRoute" in pending_runtime
    assert "data-peek-url" in pending_runtime
    assert "</button>' + latestButton" in pending_runtime
    assert "<span" not in pending_runtime
    unavailable = doorway_runtime.split("Earnings artifact unavailable", 1)[0]
    assert "data-peek-url" in unavailable


def test_earnings_doorway_route_uses_the_document_level_peek_delegate() -> None:
    html = render_work_os_shell()

    assert "event.target.closest('[data-peek-url]')" in html
    assert "route.startsWith('/api/peek/')" in html
    assert "workOsOpenPeekRoute(route" in html
    assert "const response = await fetch(parsedRoute.pathname + parsedRoute.search" in html
    assert "headers: { Accept: 'text/html' }" in html
    assert "signal: controller.signal" in html
    assert "const html = await response.text()" in html
    assert "body.innerHTML = html" in html
    assert "peekOverlay.open()" in html
    assert "The persisted earnings artifact is unavailable." in html


def test_home_and_library_surface_latest_earnings_readouts_before_full_briefs() -> None:
    html = render_work_os_shell()

    assert "company.latest_earnings_readout || null" in html
    assert "data-work-os-readout" in html
    assert "Readout unavailable" not in _screen_fragment(html, "screen-cockpit")
    assert "node.tagName === 'TR'" in html
    assert "event.target.closest('button, a')" in html
    assert 'id="briefKindFilter"' in html
    assert '<option value="earnings_readout">Earnings readouts</option>' in html
    assert "const hydratedReadouts = workOsPortfolioHydration" in html
    assert "const readoutItems = hydratedReadouts" in html
    assert "await workOsEnsurePortfolioHydration()" in html
    assert "let workOsPortfolioLoading = null" in html
    assert "Read earnings readout &rarr;" in html
    assert "readoutCards + briefCards" in html
    assert "#screen-brief-library .research-actions" in html
    assert "grid-template-columns: auto minmax(0, 1fr)" in html
    assert ".research-library-card .k-btn" in html
    assert "min-block-size: var(--touch-target-size)" in html
    assert "workOsPortfolioHydration = null" in html
    assert "await workOsEnsurePortfolioHydration()" in html
    assert "data.artifact_id" in html


def test_nvo_action_queue_open_company_uses_the_canonical_desk_handoff() -> None:
    html = render_work_os_shell()

    assert "data-work-os-ticker=\"' + escapeWorkOsHtml(action.ticker) + '\">Open Company" in html
    assert "switchCompanyWorkspace(node.dataset.workOsTicker)" in html
    assert (
        "const requested = workOsNormalizeTicker(ticker) || workOsCurrentCompanyTicker();" in html
    )
    assert "fetch('/api/work-os/companies/' + encodeURIComponent(normalized) + '/desk'" in html


def test_action_queue_renders_only_exact_alert_evidence_doorways() -> None:
    """The Work OS action queue never invents a persisted alert identity."""
    node = shutil.which("node")
    if node is None:
        return
    html = render_work_os_shell()
    action_runtime = html.split("function workOsActionEvidence", 1)[1].split(
        "function workOsRenderPortfolio", 1
    )[0]
    harness = f"""
function escapeWorkOsHtml(value) {{
  return String(value == null ? '' : value)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/\"/g, '&quot;').replace(/'/g, '&#39;');
}}
function workOsActionEvidence{action_runtime}

const exact = workOsActionEvidence({{
  ticker: 'NU', action_id: 'alert:17', action_type: 'earnings_tone',
  lifecycle_state: 'pending', source_ref: 'alert:17', evidence_ref: 'sig-17'
}});
if (!exact.includes('data-work-os-action-evidence="exact"')) throw new Error('exact metadata missing');
if (!exact.includes('alert:17') || !exact.includes('sig-17')) throw new Error('exact provenance missing');
if (!exact.includes('data-peek-url="/api/governed-alerts/17/evidence"')) throw new Error('exact evidence route missing');
if (!exact.includes('Open alert evidence')) throw new Error('read-only doorway missing');

const aggregate = workOsActionEvidence({{
  ticker: 'NU', action_id: null, action_type: null,
  lifecycle_state: null, source_ref: null, evidence_ref: null
}});
if (!aggregate.includes('data-work-os-action-evidence="unbound"')) throw new Error('aggregate is not visibly unbound');
if (aggregate.includes('/api/governed-alerts/')) throw new Error('aggregate fabricated evidence route');
if (aggregate.includes('alert:')) throw new Error('aggregate fabricated alert identity');

const malformed = workOsActionEvidence({{
  ticker: 'NU', action_id: 'alert:0', action_type: 'earnings_tone',
  lifecycle_state: 'pending', source_ref: 'alert:0', evidence_ref: 'sig-0'
}});
if (!malformed.includes('data-work-os-action-evidence="unbound"')) throw new Error('malformed identity is actionable');
if (malformed.includes('/api/governed-alerts/')) throw new Error('malformed identity invented URL');

const partial = workOsActionEvidence({{
  ticker: 'NU', action_id: 'alert:19', action_type: 'earnings_tone',
  lifecycle_state: 'pending', source_ref: 'alert:19', evidence_ref: null
}});
if (!partial.includes('data-work-os-action-evidence="partial"')) throw new Error('partial identity is not visible');
if (partial.includes('/api/governed-alerts/')) throw new Error('partial identity exposed an evidence route');
"""
    result = subprocess.run(
        [node, "-"], input=harness, text=True, capture_output=True, check=False, timeout=10
    )

    assert result.returncode == 0, result.stderr
    assert "/approve" not in action_runtime
    assert "/api/actions/" not in action_runtime
    assert "queued_actions" not in action_runtime


def test_action_queue_governed_controls_are_closed_and_evidence_bound() -> None:
    """Only complete persisted identities get the core's registered action recipes."""
    node = shutil.which("node")
    if node is None:
        return
    html = render_work_os_shell()
    action_runtime = html.split("const WORK_OS_GOVERNED_ALERT_ACTION_RECIPES", 1)[1].split(
        "function workOsRenderPortfolio", 1
    )[0]
    harness = f"""
function escapeWorkOsHtml(value) {{ return String(value == null ? '' : value); }}
const window = {{ crypto: {{ randomUUID: () => 'stable-key' }} }};
const WORK_OS_GOVERNED_ALERT_ACTION_RECIPES{action_runtime}
const signature = 'a'.repeat(64);
const ordinary = workOsGovernedActionControls({{
  action_id: 'alert:4', action_type: 'material_news', lifecycle_state: 'pending',
  source_ref: 'alert:4', evidence_ref: signature
}});
if (!ordinary.includes('data-governed-alert-action="review"')) throw new Error('review missing');
if (!ordinary.includes('data-governed-alert-action="dismiss"')) throw new Error('dismiss missing');
if (ordinary.includes('acknowledge') || ordinary.includes('supersede')) throw new Error('ordinary recipe widened');
const thesis = workOsGovernedActionControls({{
  action_id: 'alert:5', action_type: 'thesis_drift', lifecycle_state: 'pending',
  source_ref: 'alert:5', evidence_ref: signature
}});
if (!thesis.includes('data-governed-alert-action="acknowledge"')) throw new Error('thesis acknowledge missing');
if (!thesis.includes('data-governed-alert-action="complete"')) throw new Error('thesis complete missing');
if (thesis.includes('data-governed-alert-action="dismiss"')) throw new Error('thesis recipe widened');
const partial = workOsGovernedActionControls({{
  action_id: 'alert:6', action_type: 'material_news', lifecycle_state: 'pending',
  source_ref: 'alert:6', evidence_ref: 'not-a-digest'
}});
if (partial) throw new Error('partial identity became actionable');
const first = workOsGovernedActionKey({{ alertId: '4' }}, 'review');
const replay = workOsGovernedActionKey({{ alertId: '4' }}, 'review');
if (first !== replay) throw new Error('double-submit key is not stable');
"""
    result = subprocess.run(
        [node, "-"], input=harness, text=True, capture_output=True, check=False, timeout=10
    )

    assert result.returncode == 0, result.stderr
    assert "/api/governed-alerts/' + alertId + '/actions" in action_runtime
    assert "store unavailable; no action was recorded" in action_runtime
    assert "conflicts with an existing action" in action_runtime


def test_company_switcher_is_attached_to_identity_and_accessible() -> None:
    html = render_work_os_shell()

    assert 'class="company-identity-switcher"' in html
    assert 'id="companyPickerLabel"' in html
    assert 'class="company-picker-trigger k-btn k-btn-quiet k-btn-sm"' in html
    assert 'id="companyPickerTrigger"' in html
    assert 'aria-haspopup="listbox"' in html
    assert 'aria-controls="companyPickerPopover"' in html
    assert 'aria-expanded="false"' in html
    assert 'id="companyPickerPopover"' in html
    assert 'id="companyPickerSearch"' in html
    assert 'role="combobox"' in html
    assert 'aria-autocomplete="list"' in html
    assert 'aria-controls="companyPickerList"' in html
    assert 'class="k-menu company-picker-list" id="companyPickerList" role="listbox"' in html
    assert 'id="companyPickerStatus"' in html
    assert ".company-identity-switcher:hover .company-picker-trigger" in html
    assert ".company-identity-switcher:focus-within .company-picker-trigger" in html
    assert "@media (hover: none)" in html
    assert "min-block-size: var(--touch-target-size)" in html


def test_company_switcher_overlay_is_not_clipped_or_hidden_by_the_toolbar() -> None:
    html = render_work_os_shell()

    assert ".research-toolbar.k-card { overflow: visible; }" in html
    assert (
        '.company-picker-popover input[type="search"] { inline-size: 100%; '
        "min-inline-size: 0; box-sizing: border-box;"
    ) in html
    assert (
        ".company-picker-trigger { opacity: 1; transform: none; "
        "transition: opacity var(--transition), transform var(--transition); }"
    ) in html


def test_company_picker_supports_search_keyboard_dismissal_and_focus_restore() -> None:
    html = render_work_os_shell()

    assert "window.CCOverlay.register(companyPickerPopover" in html
    assert "restoreFocus: true" in html
    assert "autofocus: false" in html
    assert "workOsRenderCompanyPickerOptions" in html
    assert "company.name" in html
    assert 'role="option"' in html
    assert "aria-activedescendant" in html
    assert "ev.key === 'ArrowDown'" in html
    assert "ev.key === 'ArrowUp'" in html
    assert "ev.key === 'Enter'" in html
    assert "companyPickerOverlay.close()" in html
    assert "document.addEventListener('click'" in html
    assert "!companyPickerRoot.contains(event.target)" in html


def test_company_switch_commits_atomically_and_rejects_stale_requests() -> None:
    html = render_work_os_shell()
    render_desk = html.split("async function workOsRenderCompanyDesk", 1)[1].split(
        "async function workOsRenderBriefLibrary", 1
    )[0]

    assert "let workOsCompanyRequestSequence = 0;" in html
    assert "let workOsCompanyRequestController = null;" in html
    assert "workOsCompanyRequestController.abort()" in render_desk
    assert "new AbortController()" in render_desk
    assert "signal: controller.signal" in render_desk
    assert "if (requestSequence !== workOsCompanyRequestSequence) return false;" in render_desk
    assert render_desk.index("const desk = await response.json();") < render_desk.index(
        "document.getElementById('deskTicker').textContent"
    )
    assert "window.workOsActiveTicker =" not in render_desk
    assert "return false;" in render_desk
    assert "return true;" in render_desk


def test_company_switch_keeps_url_and_rendered_identity_in_sync() -> None:
    html = render_work_os_shell()
    switch_runtime = html.split("window.switchCompanyWorkspace = async function", 1)[1].split(
        "function workOsRenderPortfolio", 1
    )[0]

    assert "function workOsCompanyContextUrl(ticker, screen)" in html
    assert "url.searchParams.set('ticker', normalized);" in html
    assert "url.searchParams.set('screen', screen);" in html
    assert (
        "window.history.pushState({ screenId: WORK_OS_COMPANY_CONTEXT_SCREENS[screen], ticker: normalized }"
        in html
    )
    assert "await workOsRenderCompanyDesk(requested)" in html
    assert "if (!committed) return false;" in html
    assert "window.navigateTo('screen-workspace'" in switch_runtime
    assert "workOsWriteCompanyContext(requested, 'company-desk', options);" in switch_runtime
    assert switch_runtime.index(
        "workOsWriteCompanyContext(requested, 'company-desk', options);"
    ) < switch_runtime.index("window.navigateTo('screen-workspace'")


def test_company_identity_is_only_committed_by_the_atomic_desk_transition() -> None:
    html = render_work_os_shell()
    brief_reader = html.split("window.openWorkOsBriefReader = async function", 1)[1].split(
        "window.openFullBriefCanvas", 1
    )[0]

    assert "window.workOsActiveTicker =" not in brief_reader
    assert (
        "const requestedTicker = workOsNormalizeTicker(tickerOrArtifact) || workOsCurrentCompanyTicker();"
        in brief_reader
    )
    assert "encodeURIComponent(requestedTicker)" in brief_reader

    threshold_handler = html.split("document.querySelectorAll('[data-work-os-thresholds]')", 1)[
        1
    ].split("function workOsRenderEvaluationDialogues", 1)[0]
    assert "window.workOsActiveTicker =" not in threshold_handler
    assert "event.stopPropagation();" in threshold_handler
    assert "workOsOpenThresholdReview(node.dataset.workOsThresholds);" in threshold_handler
    assert 'href="/advisor/sizing-intents/' in html


def test_company_context_coordinator_owns_desk_playground_and_breadcrumb_state() -> None:
    html = render_work_os_shell()

    assert "function workOsReadCompanyContext()" in html
    assert "function workOsWriteCompanyContext(ticker, screen, options)" in html
    assert "function workOsRenderCompanyBreadcrumb()" in html
    assert "breadcrumb.textContent = 'Company Desk';" in html
    assert "breadcrumb.textContent = 'Fact & Metric Playground';" in html
    assert "Company Desk (' + context.ticker + ')'" not in html
    assert "Fact & Metric Playground (' + context.ticker + ')'" not in html
    assert "workOsWriteCompanyContext(requested, 'company-desk'" in html
    assert "workOsWriteCompanyContext(requested, 'analytics-playground'" in html
    assert "workOsRenderCompanyBreadcrumb();" in html
    navigation = html.split("window.navigateTo = function", 1)[1].split(
        "window.goCounterreadHome", 1
    )[0]
    assert navigation.index("originalNavigateTo(target);") < navigation.index(
        "workOsRenderCompanyBreadcrumb();"
    )


def test_company_context_routes_desk_to_playground_and_global_copilot() -> None:
    html = render_work_os_shell()
    playground = html.split("async function workOsRenderFactPlayground", 1)[1].split(
        "const workOsFactTicker", 1
    )[0]

    assert "const ticker = workOsCurrentCompanyTicker();" in playground
    assert "'?fragment=work-os&tickers=' + encodeURIComponent(ticker)" in playground
    assert "window.workOsOpenGlobalCopilot = function ()" in html
    assert "company_ticker: workOsCurrentCompanyTicker()" in html
    assert 'onclick="workOsOpenGlobalCopilot()"' in html


def test_company_context_accessors_preserve_default_and_url_override_tickers() -> None:
    html = render_work_os_shell()
    context_accessor = html.split("function workOsReadCompanyContext()", 1)[1].split(
        "function workOsRenderCompanyBreadcrumb", 1
    )[0]
    global_copilot = html.split("window.workOsOpenGlobalCopilot = function ()", 1)[1].split(
        "const workOsPersistentMountIds", 1
    )[0]
    endpoint = html.split("function workOsEndpoint(screenId)", 1)[1].split(
        "function workOsTrustedFragmentEndpoint", 1
    )[0]

    assert "ticker: workOsNormalizeTicker(params.get('ticker'))" in context_accessor
    assert (
        "workOsReadCompanyContext().ticker || workOsNormalizeTicker(window.workOsActiveTicker) || 'NU'"
        in context_accessor
    )
    assert "company_ticker: workOsCurrentCompanyTicker()" in global_copilot
    assert "const ticker = workOsCurrentCompanyTicker();" in endpoint
    assert "'?ticker=' + encodeURIComponent(ticker)" in endpoint


def test_playground_company_fetch_is_context_bound_and_rejects_stale_responses() -> None:
    html = render_work_os_shell()
    playground = html.split("async function workOsRenderFactPlayground", 1)[1].split(
        "window.switchFactPlayground", 1
    )[0]

    assert "let workOsFactPlaygroundRequestSequence = 0;" in html
    assert "let workOsFactPlaygroundRequestController = null;" in html
    assert "workOsFactPlaygroundRequestController.abort();" in playground
    assert "signal: controller.signal" in playground
    assert "requestSequence !== workOsFactPlaygroundRequestSequence" in playground
    assert "mount.dataset.loadedTicker = ticker;" in playground


def test_company_context_playground_change_updates_url_and_history_restores_it() -> None:
    html = render_work_os_shell()
    change_handler = html.split("workOsFactTicker.addEventListener('change'", 1)[1].split(
        "window.navigateTo", 1
    )[0]
    history = html.split("function workOsRestoreCompanyContextFromHistory()", 1)[1].split(
        "window.addEventListener('hashchange'", 1
    )[0]

    assert "window.switchFactPlayground(workOsFactTicker.value);" in change_handler
    assert "context.screen === 'company-desk'" in history
    assert "context.screen === 'analytics-playground'" in history
    assert "window.switchCompanyWorkspace(context.ticker, { fromHistory: true })" in history
    assert "window.switchFactPlayground(context.ticker, { fromHistory: true })" in history
