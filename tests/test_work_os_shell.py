"""Structural contract for the production Harvey-style Work OS shell."""

from __future__ import annotations

import inspect
import re
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from pipeline.work_os_shell import SCREEN_SPECS, render_work_os_shell
from pipeline.work_os_styles import WORK_OS_CSS
from ui.conformance_scan import scan_surface_evidence


def _screen_fragment(html: str, screen_id: str) -> str:
    marker = f'id="{screen_id}" class="screen-view'
    start = html.index(marker)
    next_screen = html.find('class="screen-view', start + len(marker))
    return html[start:] if next_screen == -1 else html[start:next_screen]


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
    assert (
        next(
            screen.label
            for screen in SCREEN_SPECS
            if screen.screen_id == "screen-analytics-playground"
        )
        == "Facts & Analytics"
    )


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
    operations_nav = html.split('id="nav-execution-queue"', 1)[1].split("</button>", 1)[0]
    assert '<span class="nav-text">Operations</span>' in operations_nav
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
        "screen-performance": "/api/panel/portfolio_allocation",
        "screen-allocation": "/api/panel/portfolio_health",
        "screen-workspace": "/api/panel/holding",
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


def test_live_backend_mount_executes_local_fragment_scripts_without_optional_htmx() -> None:
    html = render_work_os_shell()

    assert "window.workOsMountHtml" in html
    assert "script.replaceWith(replacement)" in html
    assert "workOsMountHtml(target, markup, endpoint)" in html
    assert "window.htmx" not in html
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

    # The three static placeholders and the hydrated template share the same
    # compact card geometry instead of visibly jumping after the API response.
    assert html.count('class="k-card k-card-dense k-card-interactive"') >= 4
    assert 'class="k-card k-card-interactive" style="padding:' not in html
    assert html.count("k-card-row-title") >= 4
    assert html.count('class="k-card-meta"') >= 4

    # Ordinary research headings use explicit title roles; metric cards retain
    # their separate stat label/number semantics.
    assert '<h2 class="k-card-title" id="workOsBriefReaderTitle">' in html
    assert '<h2 class="k-card-title" id="workOsBriefLibraryHeading">Brief Library</h2>' in html
    assert '<h3 class="k-card-title">' in html
    assert 'class="k-card k-card-stack"><div class="stat-heading">Owner posture</div>' in html
    assert 'class="stat-number" id="deskOwnerState"' in html


def test_l1_card_titles_and_hydration_hooks_compose_the_registry_roles() -> None:
    html = render_work_os_shell()
    target = "".join(
        _screen_fragment(html, screen_id)
        for screen_id in ("screen-cockpit", "screen-performance", "screen-allocation")
    )

    assert '<div class="stat-heading">' not in target
    assert target.count('<h3 class="k-card-title stat-heading">') == 12
    assert '<div class="k-card-row-title">' not in _screen_fragment(html, "screen-cockpit")
    assert (
        _screen_fragment(html, "screen-cockpit").count('<h3 class="k-card-title k-card-row-title">')
        == 3
    )
    assert _screen_fragment(html, "screen-allocation").count('<h3 class="k-well-title">') >= 4
    performance = _screen_fragment(html, "screen-performance")
    assert (
        '<h2 class="k-card-title">Custom Metric & Time Horizon Analysis Engine</h2>' in performance
    )
    assert (
        '<h2 class="k-card-title" style="margin-bottom: var(--sp-3);">'
        "1-Year Relative Return Comparison</h2>"
    ) in performance
    assert (
        '<div style="font-weight: 600; font-size: var(--fs-title);">'
        "Custom Metric & Time Horizon Analysis Engine</div>"
    ) not in performance
    assert "card.querySelector('.stat-heading')" in html
    assert (
        '<h3 class="k-card-title k-card-row-title">\' + escapeWorkOsHtml(action.headline)'
    ) in html


def test_l1_card_grids_use_registry_archetypes_and_collapse_at_1100() -> None:
    html = render_work_os_shell()
    target = "".join(
        _screen_fragment(html, screen_id)
        for screen_id in ("screen-cockpit", "screen-performance", "screen-allocation")
    )

    assert "card-grid-stat-4col" not in target
    assert target.count('class="card-grid-stat"') >= 3
    assert (
        ".card-grid-stat { display: grid; grid-template-columns: repeat(auto-fit, "
        "minmax(var(--grid-card-sm), 1fr));"
    ) in html
    assert "@media (max-width: 1100px)" in html
    assert "@media (max-width: 1024px) { .dashboard-2col { grid-template-columns: 1fr; } }" in html
    assert "max-inline-size: calc(var(--grid-card-sm) + var(--grid-card-sm) + var(--sp-4));" in html
    assert "grid-template-columns: repeat(2, minmax(0, 1fr))" not in html
    assert (
        '#screen-cockpit [style*="display: flex"][style*="justify-content: space-between"]' in html
    )
    assert (
        '#screen-performance [style*="display: flex"][style*="justify-content: space-between"]'
        in html
    )
    assert "flex-wrap: wrap;" in html


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
    assert 'class="research-grid k-grid-split-rail-lg"' in html
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
    assert 'id="deskQuestionCapture"' in html
    assert "kind: 'question'" in html
    assert "desk.question_store_status === 'unavailable'" in html
    assert "question.origin" in html and "question.approval" in html


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
    assert 'id="workOsPortfolioStats"' in html
    assert 'id="workOsActionQueue"' in html
    assert 'id="workOsPortfolioRows"' in html
    assert "workOsHydratePortfolio" in html
    assert "workOsRenderCompanyDesk" in html
    portfolio_match = re.search(
        r"function workOsRenderPortfolio\(payload\).*?\n  \}\n\n  "
        r"async function workOsApplyRequestedResearchState",
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

    assert 'id="deskInputPrice"' in html
    assert 'id="deskFairValue"' in html
    assert "Weight unavailable" in html
    assert "position.price_as_of" in html
    assert "position.fair_value_as_of" in html
    assert "workOsMoney(position.price, position.currency)" in html
    assert "workOsMoney(position.fair_value, position.currency)" in html


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
    assert "Readout unavailable" in html
    assert "node.tagName === 'TR'" in html
    assert "event.target.closest('button')" in html
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
        "const requested = String(ticker || window.workOsActiveTicker || '').toUpperCase();" in html
    )
    assert "fetch('/api/work-os/companies/' + encodeURIComponent(normalized) + '/desk'" in html


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
        "window.workOsActiveTicker = normalized;"
    )
    assert "window.workOsActiveTicker = previousTicker;" in render_desk
    assert "return false;" in render_desk
    assert "return true;" in render_desk


def test_company_switch_keeps_url_and_rendered_identity_in_sync() -> None:
    html = render_work_os_shell()
    switch_runtime = html.split("window.switchCompanyWorkspace = async function", 1)[1].split(
        "function workOsRenderPortfolio", 1
    )[0]

    assert "function workOsCompanyDeskUrl(ticker)" in html
    assert "params.set('ticker', ticker);" in html
    assert "params.set('screen', 'company-desk');" in html
    assert "window.history.pushState({ screenId: 'screen-workspace', ticker: requested }" in html
    assert "await workOsRenderCompanyDesk(requested)" in html
    assert "if (!committed) return false;" in html
    assert "window.navigateTo('screen-workspace'" in switch_runtime
    assert "breadcrumb.textContent = 'Company Desk (' + requested + ')'" in switch_runtime
    assert switch_runtime.index("window.navigateTo('screen-workspace'") < switch_runtime.index(
        "breadcrumb.textContent = 'Company Desk (' + requested + ')'"
    )


def test_company_identity_is_only_committed_by_the_atomic_desk_transition() -> None:
    html = render_work_os_shell()
    brief_reader = html.split("window.openWorkOsBriefReader = async function", 1)[1].split(
        "window.openFullBriefCanvas", 1
    )[0]

    assert "window.workOsActiveTicker =" not in brief_reader
    assert "const requestedTicker = String(tickerOrArtifact" in brief_reader
    assert "encodeURIComponent(requestedTicker)" in brief_reader

    threshold_handler = html.split("document.querySelectorAll('[data-work-os-thresholds]')", 1)[
        1
    ].split("function workOsApplyRequestedResearchState", 1)[0]
    assert "window.workOsActiveTicker =" not in threshold_handler
    assert "switchCompanyWorkspace(node.dataset.workOsThresholds).then" in threshold_handler
    assert "if (committed) openDrillDrawer('thresholds')" in threshold_handler
