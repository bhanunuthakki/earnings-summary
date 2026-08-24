"""Production renderer for the seven-screen Equity Work OS.

The high-fidelity prototype is intentionally the single markup source of truth.
This module applies the small production-only contract around it: live endpoint
mounts, honest allocation language, accessible transient surfaces, and a
responsive mobile cockpit.  Backend panel endpoints remain available as
drill-through data providers while the old command-center navigation is retired.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache
from html import escape
from pathlib import Path
from typing import Literal

from pipeline.cc_action import CC_ACTION_JS
from pipeline.cc_overlay import CC_OVERLAY_JS
from pipeline.explore_panel import EXPLORE_PANEL_JS
from pipeline.operations_panel import render_operations_shell
from pipeline.work_os_copilot import render_work_os_copilot
from pipeline.work_os_research import (
    render_brief_library_shell,
    render_brief_reader_shell,
    render_company_desk_shell,
    render_fact_playground_shell,
)
from pipeline.work_os_route_contract import DESTINATION_SURFACE_IDS
from pipeline.work_os_styles import WORK_OS_CSS
from ui.controls import controls_css
from ui.living_grid import head_assets as living_grid_head_assets
from ui.tokens import FAVICON_LINK, palette_css


@dataclass(frozen=True, slots=True)
class ScreenSpec:
    """One stable destination in the Work OS information architecture."""

    screen_id: str
    nav_id: str
    label: str
    endpoint: str


CockpitStatKey = Literal["nav"]
CockpitStatTarget = Literal["screen-performance"]


@dataclass(frozen=True, slots=True)
class CockpitStatSpec:
    """One stable Cockpit statistic and, where appropriate, its native destination."""

    key: CockpitStatKey
    label: str
    target: CockpitStatTarget | None = None
    accessible_name: str | None = None


COCKPIT_STAT_SPECS: tuple[CockpitStatSpec, ...] = (CockpitStatSpec("nav", "Portfolio NAV"),)


SCREEN_SPECS: tuple[ScreenSpec, ...] = (
    ScreenSpec("screen-cockpit", "nav-cockpit", "Portfolio Cockpit", "/api/panel/overview"),
    ScreenSpec(
        "screen-performance",
        "nav-performance",
        "Performance",
        "/api/panel/performance_risk",
    ),
    ScreenSpec("screen-workspace", "nav-workspace", "Company Desk", "/api/panel/holding"),
    ScreenSpec(
        "screen-brief-library",
        "nav-brief-library",
        "Brief Library",
        "/api/work-os/briefs",
    ),
    ScreenSpec(
        "screen-analytics-playground",
        "nav-analytics-playground",
        "Facts & Analytics",
        "/api/panel/explore",
    ),
    ScreenSpec(
        "screen-audit-log",
        "nav-audit-log",
        "Decision Audit Log",
        "/api/panel/portfolio_record",
    ),
    ScreenSpec(
        "screen-execution-queue",
        "nav-execution-queue",
        "Operations",
        "/api/panel/operations",
    ),
)

_LEGACY_HASHES: dict[str, str] = {
    "home": "screen-cockpit",
    "overview": "screen-cockpit",
    "companies": "screen-workspace",
    "holding": "screen-workspace",
    "screen-full-brief": "screen-brief-library",
    "diet": "screen-workspace",
    "discovery": "screen-workspace",
    "portfolio": "screen-performance",
    "portfolio_allocation": "screen-performance",
    "portfolio_health": "screen-performance",
    "portfolio_risk": "screen-performance",
    "screen-allocation": "screen-performance",
    "ask": "screen-analytics-playground",
    "explore": "screen-analytics-playground",
    "red_team": "screen-analytics-playground",
    "review": "screen-audit-log",
    "musings": "screen-audit-log",
    "journal": "screen-audit-log",
    "triage": "screen-audit-log",
    "decisions": "screen-audit-log",
    "system": "screen-execution-queue",
    "provenance": "screen-execution-queue",
    "settings": "screen-execution-queue",
    "actions": "screen-execution-queue",
}


_PROTOTYPE_PATH = Path(__file__).resolve().parents[2] / "mockups" / "harvey_sidebar_flow.html"

_TRADE_MODAL_RE = re.compile(
    r"\n\s*<!-- TRADE ORDER EXECUTION MODAL & SCRIM -->.*?\n\s*<!-- TOAST NOTICE -->",
    re.DOTALL,
)
_TRADE_FUNCTIONS_RE = re.compile(
    r"\n\s*// TRADE ORDER MODAL INTERACTION.*?\n\s*function openSheetDCFModel",
    re.DOTALL,
)
_REBALANCE_DRAWER_RE = re.compile(
    r"\n\s*\} else if \(type === 'rebalance-plan'\) \{.*?\n\s*\} else if \(type === 'dcf-priors'\)",
    re.DOTALL,
)
_NAV_ITEM_RE = re.compile(
    r'<a (?P<attrs>onclick="[^"]+" class="nav-item[^"]*" id="nav-[^"]+"[^>]*)>'
    r"(?P<body>.*?)</a>",
    re.DOTALL,
)


def _render_full_page_detail_host() -> str:
    """Render the one registered, read-only host for routed detail peeks."""

    return """
<section class="work-os-detail-page" id="workOsFullPageDetail" role="dialog" aria-modal="true" aria-hidden="true" aria-labelledby="workOsFullPageDetailTitle" hidden>
  <header class="work-os-detail-page-header">
    <button class="k-btn k-btn-quiet k-btn-sm work-os-detail-page-close" id="workOsFullPageDetailBack" type="button">Back</button>
    <div class="work-os-detail-page-title">
      <div class="k-card-meta">Research detail</div>
      <h1 class="k-card-title" id="workOsFullPageDetailTitle">Research detail</h1>
    </div>
    <button class="k-btn k-btn-quiet k-btn-sm" id="workOsFullPageDetailClose" type="button" aria-label="Close research detail">Close</button>
  </header>
  <main class="work-os-detail-page-body k-doc" id="workOsFullPageDetailBody" tabindex="-1">
    <div class="k-well" role="status">Loading persisted research detail…</div>
  </main>
</section>
""".strip()


_ALLOCATION_NAV_RE = re.compile(
    r'\s*<a onclick="navigateTo\(\'screen-allocation\'\)".*?id="nav-allocation".*?</a>',
    re.DOTALL,
)
_PIPELINE_SIMULATION_RE = re.compile(
    r"\n\s*// PIPELINE SIMULATION\n\s*function runPipelineJob\(jobName\) \{.*?"
    r"\n\s*\}\n\n\s*// AUDIT LOG FILTERING",
    re.DOTALL,
)
_COCKPIT_SECTION_RE = re.compile(
    r'<section id="screen-cockpit".*?</section>\s*'
    r"(?=<!-- =+\s*SURFACE: PORTFOLIO PERFORMANCE)",
    re.DOTALL,
)
_PERFORMANCE_SECTION_RE = re.compile(
    r'<section id="screen-performance".*?</section>\s*'
    r"(?=<!-- =+\s*SURFACE: PORTFOLIO ALLOCATION)",
    re.DOTALL,
)
_ALLOCATION_SECTION_RE = re.compile(
    r'<section id="screen-allocation".*?</section>\s*'
    r"(?=<!-- =+\s*SURFACE 2: COMPANY RESEARCH WORKSPACE)",
    re.DOTALL,
)
_COMPANY_DESK_SECTION_RE = re.compile(
    r'<section id="screen-workspace".*?</section>\s*'
    r"(?=<!-- =+\s*BRIEF LIBRARY PERSISTENT DESTINATION)",
    re.DOTALL,
)
_BRIEF_LIBRARY_SECTION_RE = re.compile(
    r'<section id="screen-brief-library".*?</section>\s*'
    r"(?=<!-- =+\s*SURFACE: EXTRACTED FACT & METRIC)",
    re.DOTALL,
)
_FACT_PLAYGROUND_SECTION_RE = re.compile(
    r'<section id="screen-analytics-playground".*?</section>\s*'
    r"(?=<!-- =+\s*SURFACE 3: DECISION AUDIT LOG)",
    re.DOTALL,
)
_FACT_PLAYGROUND_RUNTIME_RE = re.compile(
    r"\n\s*// EXTRACTED FACT & METRIC ANALYTICS PLAYGROUND DATABASE & LOGIC.*?"
    r"\n\s*// EMBEDDED DCF SLIDERS INSIDE REPORT",
    re.DOTALL,
)
_AUDIT_SECTION_RE = re.compile(
    r'<section id="screen-audit-log".*?</section>\s*'
    r"(?=<!-- =+\s*SURFACE 4: EXECUTION QUEUE)",
    re.DOTALL,
)
_OPERATIONS_SECTION_RE = re.compile(
    r'<section id="screen-execution-queue".*?</section>\s*'
    r"(?=</div>\s*</main>)",
    re.DOTALL,
)


def _endpoint_map() -> dict[str, str]:
    return {screen.screen_id: screen.endpoint for screen in SCREEN_SPECS}


def _render_portfolio_cockpit_shell() -> str:
    """Return the compact, live-first Portfolio Copilot operating loop."""

    return """
<section id="screen-cockpit" class="screen-view is-active">
  <section class="work-os-portfolio-topline" aria-label="Portfolio NAV and governed actions">
    <article class="k-card k-card-stat work-os-nav-card" data-work-os-stat-key="nav" aria-labelledby="workOsPortfolioNavHeading">
      <div class="stat-heading" id="workOsPortfolioNavHeading">Portfolio NAV</div>
      <div class="stat-number" id="workOsPortfolioNav">—</div>
      <div class="stat-subtext" id="workOsPortfolioNavDetail">Loading governed portfolio state</div>
      <div class="work-os-allocation-list" id="workOsPortfolioAllocation" aria-label="Portfolio allocation mix"></div>
    </article>
    <article class="k-card work-os-actions-rail" aria-labelledby="workOsActionHeading">
      <header class="k-section-head">
        <div class="k-section-title" id="workOsActionHeading" role="heading" aria-level="2">Actions</div>
        <span class="k-card-meta" id="workOsActionCount" aria-live="polite">Loading</span>
      </header>
      <div id="workOsActionQueue" class="work-os-action-queue">
        <div class="k-well" role="status">Loading governed portfolio actions…</div>
      </div>
    </article>
  </section>

  <section class="work-os-section" aria-labelledby="workOsHoldingsHeading">
    <header class="k-section-head">
      <div class="k-section-title" id="workOsHoldingsHeading" role="heading" aria-level="2">Portfolio at a Glance</div>
      <span class="k-card-meta" id="workOsPortfolioSortStatus" aria-live="polite">Portfolio order</span>
    </header>
    <div class="k-table-shell">
      <table class="matrix-table work-os-portfolio-table">
        <thead><tr>
          <th scope="col" aria-sort="none"><button class="k-btn k-btn-quiet k-btn-sm work-os-sort-button" type="button" data-work-os-portfolio-sort="company"><span>Company</span><span aria-hidden="true">↑</span></button></th>
          <th scope="col" aria-sort="none"><button class="k-btn k-btn-quiet k-btn-sm work-os-sort-button" type="button" data-work-os-portfolio-sort="weight"><span>Weight</span><span aria-hidden="true">↑</span></button></th>
          <th scope="col" aria-sort="none"><button class="k-btn k-btn-quiet k-btn-sm work-os-sort-button" type="button" data-work-os-portfolio-sort="price"><span>Price/Target</span><span aria-hidden="true">↑</span></button></th>
          <th scope="col" aria-sort="none"><button class="k-btn k-btn-quiet k-btn-sm work-os-sort-button" type="button" data-work-os-portfolio-sort="status"><span>Status</span><span aria-hidden="true">↑</span></button></th>
          <th scope="col" aria-sort="none"><button class="k-btn k-btn-quiet k-btn-sm work-os-sort-button" type="button" data-work-os-portfolio-sort="links"><span>Key Links</span><span aria-hidden="true">↑</span></button></th>
        </tr></thead>
        <tbody id="workOsPortfolioRows"><tr><td colspan="5"><div class="k-well" role="status">Loading governed portfolio companies…</div></td></tr></tbody>
      </table>
    </div>
  </section>

  <section class="work-os-section" aria-labelledby="workOsEvaluationHeading">
    <header class="k-section-head">
      <div>
        <div class="k-section-title" id="workOsEvaluationHeading" role="heading" aria-level="2">Evaluation dialogues</div>
        <p class="k-section-meta">Recent owner dialogue and ready-to-discuss workups · not the full evaluation list</p>
      </div>
      <span class="k-card-meta" id="workOsEvaluationCount" aria-live="polite">Loading</span>
    </header>
    <div class="work-os-evaluation-list" id="workOsEvaluationDialogues">
      <div class="k-well" role="status">Loading bounded evaluation dialogues…</div>
    </div>
  </section>
</section>
""".strip()


def _render_live_screen_shell(
    *,
    screen_id: str,
    mount_id: str,
    layer: str,
    title: str,
    description: str,
) -> str:
    """Return a truthful on-demand shell for one persistent Work OS screen."""

    return f"""
<section id="{escape(screen_id)}" class="screen-view">
  <div class="research-screen">
    <header class="k-card k-card-section research-toolbar">
      <div class="k-card-heading">
        <div class="k-card-meta">{escape(layer)}</div>
        <h1 class="k-card-title">{escape(title)}</h1>
        <p class="k-card-meta">{escape(description)}</p>
      </div>
      <button type="button" class="k-btn k-btn-quiet k-btn-sm"
        data-work-os-refresh-screen="{escape(screen_id)}">Refresh live view</button>
    </header>
    <div id="{escape(mount_id)}" data-work-os-screen-id="{escape(screen_id)}">
      <div class="k-well" role="status">Loading live {escape(title)}…</div>
    </div>
  </div>
</section>
""".strip()


def _nav_button(match: re.Match[str]) -> str:
    attrs = match.group("attrs").replace('class="', 'class="k-btn k-btn-quiet ', 1)
    return f'<button type="button" {attrs}>{match.group("body")}</button>'


def _production_runtime(generated_at: datetime) -> str:
    endpoint_json = json.dumps(_endpoint_map(), indent=2, sort_keys=False)
    legacy_hash_json = json.dumps(_LEGACY_HASHES, indent=2, sort_keys=False)
    route_destinations_json = json.dumps(DESTINATION_SURFACE_IDS)
    stamp = escape(generated_at.astimezone(UTC).isoformat().replace("+00:00", "Z"))
    return f"""
<style id="work-os-production-css">
  {WORK_OS_CSS}
</style>
<div class="work-os-live-status" id="workOsLiveStatus" aria-live="polite" data-generated-at="{stamp}"></div>
<script id="work-os-action-runtime">{CC_ACTION_JS}</script>
<script id="work-os-overlay-runtime">{CC_OVERLAY_JS}</script>
<script id="work-os-production-runtime">
  const WORK_OS_ENDPOINTS = {endpoint_json};
  const WORK_OS_LEGACY_HASHES = {legacy_hash_json};
  // Kept in sync with pipeline.work_os_route_contract: browser state is only
  // replayable when it names a registered destination and known transient.
  const WORK_OS_ROUTE_DESTINATIONS = {route_destinations_json};
  // These are the existing read-only content handlers registered by
  // comments_server_content_routes.  Full-page detail never upgrades an
  // arbitrary prototype callback or an unregistered endpoint into navigation.
  const WORK_OS_FULL_PAGE_PEEK_PATHS = [
    new RegExp('^/api/peek/(?:alerts|news-events|documents|score|earnings-prep|earnings-readout|fit|weekly-packet|whatif|etf_workup|discovery-compare)$'),
    new RegExp('^/api/peek/(?:alert/[0-9]+|ticker/[A-Za-z0-9.=-]+|memo/[a-z_]+|review/[A-Za-z0-9.=-]+|provenance(?:/[A-Za-z0-9_:-]+)?)$'),
    new RegExp('^/api/governed-alerts/[1-9][0-9]*/evidence$'),
    new RegExp('^/source/[0-9]+$')
  ];
  const WORK_OS_HISTORY_DRAWER_TYPES = new Set([
    'comparative-viewer', 'dcf-priors', 'dcf-sensitivity', 'factor-heatmap',
    'falsifier', 'financials', 'governance-limits', 'live-detail', 'llm-routing',
    'peers', 'rebalance-plan', 'saydo', 'thresholds', 'updates'
  ]);
  const workOsRequests = new WeakMap();
  let workOsRequestGeneration = 0;
  const WORK_OS_FETCH_TIMEOUT_MS = 15000;
  const originalNavigateTo = window.navigateTo;
  // The URL is the durable company-context boundary.  workOsActiveTicker remains
  // a compatibility mirror for embedded prototype callbacks only.
  window.workOsActiveTicker = 'NU';
  const WORK_OS_COMPANY_CONTEXT_SCREENS = {{
    'company-desk': 'screen-workspace',
    'analytics-playground': 'screen-analytics-playground'
  }};
  let workOsPortfolioHydration = null;
  let workOsPortfolioLoading = null;
  let workOsResearchCompanies = null;
  let workOsCompanyRequestSequence = 0;
  let workOsCompanyRequestController = null;
  let workOsPeekRequestSequence = 0;
  let workOsPeekRequestController = null;
  let workOsFullPageDetailRequestSequence = 0;
  let workOsFullPageDetailRequestController = null;
  let workOsLastTransientFocusId = null;
  let workOsReplayingHistory = false;
  let workOsReaderContext = null;
  let workOsFactPlaygroundLoading = null;
  let workOsFactPlaygroundRequestSequence = 0;
  let workOsFactPlaygroundRequestController = null;
  let companyPickerMatches = [];
  let companyPickerActiveIndex = -1;
  const workOsLaunchParams = new URLSearchParams(window.location.search);

  function workOsNormalizeTicker(ticker) {{
    return String(ticker || '').trim().toUpperCase();
  }}

  function workOsReadCompanyContext() {{
    const params = new URLSearchParams(window.location.search);
    return {{
      ticker: workOsNormalizeTicker(params.get('ticker')),
      screen: String(params.get('screen') || '')
    }};
  }}

  function workOsCurrentCompanyTicker() {{
    return workOsReadCompanyContext().ticker || workOsNormalizeTicker(window.workOsActiveTicker) || 'NU';
  }}

  function workOsRenderCompanyBreadcrumb() {{
    const context = workOsReadCompanyContext();
    const breadcrumb = document.getElementById('breadcrumb-title');
    if (!breadcrumb || !context.ticker) return;
    if (context.screen === 'company-desk') breadcrumb.textContent = 'Company Desk';
    if (context.screen === 'analytics-playground') breadcrumb.textContent = 'Fact & Metric Playground';
  }}

  function workOsCompanyContextUrl(ticker, screen) {{
    const url = new URL(window.location.href);
    const normalized = workOsNormalizeTicker(ticker);
    if (normalized) url.searchParams.set('ticker', normalized);
    else url.searchParams.delete('ticker');
    if (screen) url.searchParams.set('screen', screen);
    else url.searchParams.delete('screen');
    url.hash = WORK_OS_COMPANY_CONTEXT_SCREENS[screen] || workOsScreenFromHash();
    return url.pathname + url.search + url.hash;
  }}

  function workOsWriteCompanyContext(ticker, screen, options) {{
    const normalized = workOsNormalizeTicker(ticker);
    if (!normalized || !WORK_OS_COMPANY_CONTEXT_SCREENS[screen]) return false;
    const nextUrl = workOsCompanyContextUrl(normalized, screen);
    window.workOsActiveTicker = normalized;
    if (!(options && options.fromHistory)) {{
      const currentUrl = window.location.pathname + window.location.search + window.location.hash;
      if (currentUrl !== nextUrl) {{
        window.history.pushState({{ screenId: WORK_OS_COMPANY_CONTEXT_SCREENS[screen], ticker: normalized }}, '', nextUrl);
      }}
    }}
    workOsRenderCompanyBreadcrumb();
    return true;
  }}

  function workOsValidHistoryTicker(value) {{
    return value == null || (/^[A-Z][A-Z0-9.=-]{{0,14}}$/).test(String(value));
  }}

  function workOsValidHistorySection(value) {{
    return value == null || (/^[a-z][a-z0-9_-]*$/).test(String(value));
  }}

  function workOsPushHistoryState(state, url) {{
    try {{
      window.history.pushState(state, '', url);
      return true;
    }} catch (_error) {{
      // Embedded/static specimens can have an opaque origin. History is a
      // progressive enhancement there; the requested research view must still open.
      return false;
    }}
  }}

  function workOsEncodeHistoryRoute(route) {{
    const origin = route.origin || {{}};
    return [
      route.surface || '', route.ticker || '', route.section || '', route.overlay || '',
      origin.surface || '', origin.ticker || '', origin.section || ''
    ].join('|');
  }}

  function workOsRouteFromHistoryState(state) {{
    if (!state || typeof state !== 'object' || Array.isArray(state) || typeof state.workOsRoute !== 'string') return null;
    const fields = state.workOsRoute.split('|');
    if (fields.length !== 7 || fields.some(function (field) {{ return field.indexOf('\\0') !== -1; }})) return null;
    const surface = fields[0];
    const ticker = fields[1] || null;
    const section = fields[2] || null;
    const overlay = fields[3] || null;
    const originSurface = fields[4] || null;
    const originTicker = fields[5] || null;
    const originSection = fields[6] || null;
    if (!WORK_OS_ROUTE_DESTINATIONS.includes(surface) || !WORK_OS_ROUTE_DESTINATIONS.includes(originSurface)) return null;
    if (overlay !== 'risk_drawer' && overlay !== 'peek') return null;
    if (!workOsValidHistoryTicker(ticker) || !workOsValidHistoryTicker(originTicker)) return null;
    if (!workOsValidHistorySection(section) || !workOsValidHistorySection(originSection)) return null;
    return {{
      surface: surface, ticker: ticker, section: section, overlay: overlay,
      origin: {{ surface: originSurface, ticker: originTicker, section: originSection }}
    }};
  }}

  function workOsHistoryFocusId() {{
    const active = document.activeElement;
    return active instanceof HTMLElement && active.id ? active.id : null;
  }}

  function workOsHistoryOrigin() {{
    const current = workOsRouteFromHistoryState(window.history.state);
    if (current) return current.origin;
    const context = workOsReadCompanyContext();
    const screen = workOsScreenFromHash();
    return {{
      surface: WORK_OS_ROUTE_DESTINATIONS.includes(screen) ? screen : 'screen-cockpit',
      ticker: workOsValidHistoryTicker(context.ticker) ? context.ticker || null : null,
      section: workOsValidHistorySection(context.screen) ? context.screen || null : null
    }};
  }}

  function workOsPushTransientHistory(overlay, transient) {{
    const origin = workOsHistoryOrigin();
    const route = {{
      surface: origin.surface, ticker: origin.ticker, section: origin.section,
      overlay: overlay, origin: origin
    }};
    const state = Object.assign({{}}, window.history.state || {{}}, {{
      screenId: route.surface,
      ticker: route.ticker,
      workOsRoute: workOsEncodeHistoryRoute(route),
      workOsTransient: transient
    }});
    const currentUrl = window.location.pathname + window.location.search + window.location.hash;
    workOsLastTransientFocusId = transient.focusId || null;
    workOsPushHistoryState(state, currentUrl);
  }}

  function workOsRestoreHistoryFocus(focusId) {{
    if (!focusId) return;
    const focusTarget = document.getElementById(focusId);
    if (focusTarget && typeof focusTarget.focus === 'function') focusTarget.focus();
  }}

  window.workOsOpenGlobalCopilot = function () {{
    window.openWorkOsCopilot({{
      company_ticker: workOsCurrentCompanyTicker(),
      category: 'research',
      origin_key: 'work-os:global-launcher',
      coverage_role_at_creation: 'unknown',
      lifecycle_at_creation: 'unknown'
    }});
  }};
  const workOsPersistentMountIds = {{
    'screen-performance': 'workOsPerformanceMount',
    'screen-audit-log': 'workOsAuditMount'
  }};
  const originalOpenDrillDrawer = window.openDrillDrawer;
  const originalCloseDrillDrawer = window.closeDrillDrawer;
  const originalOpenPeekDrawer = window.openPeekDrawer;
  const originalClosePeekDrawer = window.closePeekDrawer;
  const drillDrawer = document.getElementById('drillDrawer');
  const peekDrawer = document.getElementById('peekDrawer');
  const fullPageDetail = document.getElementById('workOsFullPageDetail');
  const briefReader = document.getElementById('workOsBriefReader');
  const companyPickerRoot = document.getElementById('companyPickerRoot');
  const companyPickerTrigger = document.getElementById('companyPickerTrigger');
  const companyPickerPopover = document.getElementById('companyPickerPopover');
  const companyPickerSearch = document.getElementById('companyPickerSearch');
  const companyPickerList = document.getElementById('companyPickerList');
  const companyPickerStatus = document.getElementById('companyPickerStatus');

  const drillOverlay = drillDrawer && window.CCOverlay.register(drillDrawer, {{
    modal: true, priority: window.CCOverlay.PRIORITY.DRAWER, scrim: true,
    trapFocus: true, restoreFocus: true, motion: 'slide-right',
    group: 'work-os-drawer', closeId: 'drillDrawerClose', wireClose: false,
    onOpen: function () {{
      drillDrawer.classList.add('is-open');
      drillDrawer.setAttribute('aria-hidden', 'false');
    }},
    onBeforeClose: function () {{
      workOsAbortTarget(document.getElementById('drawerBody'), 'hidden');
    }},
    onClose: function () {{
      drillDrawer.classList.remove('is-open');
      drillDrawer.setAttribute('aria-hidden', 'true');
      originalCloseDrillDrawer();
      workOsDiscardClosedTransient('risk_drawer');
    }}
  }});
  const peekOverlay = peekDrawer && window.CCOverlay.register(peekDrawer, {{
    modal: true, priority: window.CCOverlay.PRIORITY.PEEK, scrim: true,
    trapFocus: true, restoreFocus: true, motion: 'slide-right',
    group: 'work-os-drawer', closeId: 'peekDrawerClose', wireClose: false,
    onOpen: function () {{
      peekDrawer.classList.add('is-open');
      peekDrawer.setAttribute('aria-hidden', 'false');
    }},
    onBeforeClose: function () {{ workOsAbortPeekRequest(); }},
    onClose: function () {{
      peekDrawer.classList.remove('is-open');
      peekDrawer.setAttribute('aria-hidden', 'true');
      originalClosePeekDrawer();
      workOsDiscardClosedTransient('peek');
    }}
  }});
  const fullPageDetailOverlay = fullPageDetail && window.CCOverlay.register(fullPageDetail, {{
    modal: true, priority: window.CCOverlay.PRIORITY.PEEK, scrim: false,
    trapFocus: true, restoreFocus: true, motion: 'fade',
    group: 'work-os-detail-page', closeId: 'workOsFullPageDetailClose', wireClose: true,
    onOpen: function () {{ fullPageDetail.hidden = false; fullPageDetail.setAttribute('aria-hidden', 'false'); }},
    onBeforeClose: function () {{ workOsAbortFullPageDetailRequest(); }},
    onClose: function () {{ fullPageDetail.hidden = true; fullPageDetail.setAttribute('aria-hidden', 'true'); }}
  }});
  const briefReaderOverlay = briefReader && window.CCOverlay.register(briefReader, {{
    modal: true, priority: window.CCOverlay.PRIORITY.PALETTE, scrim: false,
    trapFocus: true, restoreFocus: true, motion: 'fade',
    group: 'work-os-reader', closeId: 'workOsBriefReaderClose', wireClose: true,
    onOpen: function () {{ briefReader.hidden = false; briefReader.setAttribute('aria-hidden', 'false'); }},
    onClose: function () {{
      briefReader.hidden = true;
      briefReader.setAttribute('aria-hidden', 'true');
      workOsReaderContext = null;
    }}
  }});
  const companyPickerOverlay = companyPickerPopover && window.CCOverlay.register(companyPickerPopover, {{
    priority: 0, scrim: false, trapFocus: false, restoreFocus: true,
    autofocus: false, motion: 'rise', group: 'work-os-company-picker',
    onOpen: function () {{
      if (companyPickerTrigger) companyPickerTrigger.setAttribute('aria-expanded', 'true');
      if (companyPickerSearch) {{
        companyPickerSearch.setAttribute('aria-expanded', 'true');
        companyPickerSearch.focus();
      }}
    }},
    onClose: function () {{
      if (companyPickerTrigger) companyPickerTrigger.setAttribute('aria-expanded', 'false');
      if (companyPickerSearch) {{
        companyPickerSearch.setAttribute('aria-expanded', 'false');
        companyPickerSearch.removeAttribute('aria-activedescendant');
        companyPickerSearch.value = '';
      }}
    }}
  }});

  window.openDrillDrawer = function (type, options) {{
    const drawerType = typeof type === 'string' && WORK_OS_HISTORY_DRAWER_TYPES.has(type)
      ? type : null;
    if (!drawerType) return false;
    if (!(options && options.fromHistory)) {{
      workOsPushTransientHistory('risk_drawer', {{
        drawerType: drawerType, focusId: workOsHistoryFocusId()
      }});
    }}
    originalOpenDrillDrawer(drawerType);
    const reportTabs = {{ financials: 'financials', saydo: 'saydo', peers: 'comps', falsifier: 'bear' }};
    if (reportTabs[drawerType]) {{
      const ticker = workOsCurrentCompanyTicker();
      const title = document.getElementById('drawerTitle');
      const subtitle = document.getElementById('drawerSubtitle');
      const body = document.getElementById('drawerBody');
      if (title) title.textContent = ticker + ' · ' + drawerType;
      if (subtitle) subtitle.textContent = 'Live company brief detail';
      if (body) body.innerHTML = workOsReportFrame(ticker, reportTabs[drawerType], 'work-os-report-frame');
    }}
    if (drillOverlay) drillOverlay.open();
    return true;
  }};
  function workOsDiscardClosedTransient(overlay) {{
    if (workOsReplayingHistory) return false;
    const route = workOsRouteFromHistoryState(window.history.state);
    if (!route || route.overlay !== overlay) return false;
    window.history.back();
    return true;
  }}
  function workOsCloseTransientFromHistory(overlay) {{
    return workOsDiscardClosedTransient(overlay);
  }}
  window.closeDrillDrawer = function () {{
    if (!workOsCloseTransientFromHistory('risk_drawer') && drillOverlay) drillOverlay.close();
  }};
  window.openPeekDrawer = function (refKey) {{
    originalOpenPeekDrawer(refKey);
    if (peekOverlay) peekOverlay.open();
  }};
  window.closePeekDrawer = function () {{
    if (!workOsCloseTransientFromHistory('peek') && peekOverlay) peekOverlay.close();
  }};

  function workOsReportFrame(ticker, tabId, className) {{
    const safeTicker = encodeURIComponent(String(ticker || 'NU').toUpperCase());
    const safeTab = encodeURIComponent(tabId || 'overview');
    return '<iframe class="' + className + '" src="/reports/' + safeTicker + '#tab=' + safeTab + '" title="' + safeTicker + ' live research brief" loading="lazy"></iframe>';
  }}

  function workOsBriefUrl(ticker, origin, focusId) {{
    const url = new URL(window.location.href);
    url.searchParams.set('work_os_brief', ticker);
    url.searchParams.set('work_os_detail_origin', workOsEncodeDetailOrigin(origin));
    if (focusId) url.searchParams.set('work_os_focus', focusId);
    url.hash = origin.surface;
    return url.pathname + url.search + url.hash;
  }}
  window.closeWorkOsBriefReader = function () {{
    if (window.history.state && window.history.state.workOsBriefReader) {{ window.history.back(); return; }}
    if (briefReaderOverlay) briefReaderOverlay.close();
  }};
  const briefReaderBack = document.getElementById('workOsBriefReaderBack');
  if (briefReaderBack) briefReaderBack.addEventListener('click', window.closeWorkOsBriefReader);

  function escapeWorkOsHtml(value) {{
    return String(value == null ? '' : value)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }}

  const WORK_OS_BRIEF_GROUP_IDS = [
    'overview', 'quarter', 'financials', 'thesis-risk', 'valuation-comps', 'sources'
  ];

  async function workOsLoadBriefResearchItems(ticker) {{
    const mount = document.getElementById('workOsBriefResearchItemsMount');
    if (!mount || !ticker) return;
    mount.innerHTML = '<div class="k-well" role="status">Loading live research items…</div>';
    try {{
      const response = await fetch('/api/panel/journal?items=1&band=brief&ticker=' + encodeURIComponent(ticker), {{ headers: {{ Accept: 'text/html' }} }});
      if (!response.ok) throw new Error('HTTP ' + response.status);
      window.workOsMountHtml(mount, await response.text(), '/api/panel/journal');
    }} catch (error) {{
      mount.innerHTML = '<div class="k-well" role="alert">Research Items are unavailable; the persisted brief remains readable.</div>';
    }}
  }}

  async function workOsLoadBriefArtifact(artifact, options) {{
    const title = document.getElementById('workOsBriefReaderTitle');
    const body = document.getElementById('workOsBriefReaderBody');
    const meta = document.getElementById('workOsBriefReaderMeta');
    const sections = document.getElementById('workOsBriefReaderSections');
    workOsReaderContext = artifact;
    void workOsLoadBriefResearchItems(artifact.ticker);
    const displayTitle = artifact.title && String(artifact.title).toUpperCase().startsWith(String(artifact.ticker).toUpperCase())
      ? artifact.title
      : artifact.ticker + ' · ' + (artifact.title || 'Full Research Brief');
    if (title) title.textContent = displayTitle;
    if (meta) meta.textContent = artifact.report_date + ' · ' + String(artifact.coverage_role || 'unknown') + ' coverage';
    if (sections) sections.replaceChildren();
    workOsRenderReaderDecision(null);
    if (briefReaderOverlay) briefReaderOverlay.open();
    if (artifact.reader_mode !== 'shared_body') {{
      if (body) workOsReaderUnavailable(body, artifact, 'legacy_standalone');
      return;
    }}
    if (!body) return;
    body.innerHTML = '<div class="k-well" role="status">Loading complete persisted brief…</div>';
    try {{
      const bodyUrl = artifact.body_url || ('/api/work-os/briefs/' + encodeURIComponent(artifact.artifact_id) + '/body');
      const response = await fetch(bodyUrl, {{ headers: {{ Accept: 'application/json' }} }});
      if (!response.ok) {{
        const unavailable = response.status === 409 ? await response.json() : null;
        const error = new Error('HTTP ' + response.status);
        error.readerStatus = unavailable && unavailable.status;
        throw error;
      }}
      const payload = await response.json();
      if (!payload || payload.schema_version !== 'report_reader_payload.v1' || !payload.body_html || !payload.style_url || !payload.decision) throw new Error('invalid reader payload');
      workOsRenderReaderDecision(payload.decision);
      const host = document.createElement('div');
      host.className = 'work-os-report-host';
      host.setAttribute('role', 'document');
      host.setAttribute('aria-label', artifact.ticker + ' complete research brief');
      host.tabIndex = 0;
      const root = host.attachShadow({{ mode: 'open' }});
      const stylesheet = document.createElement('link');
      stylesheet.rel = 'stylesheet';
      stylesheet.href = payload.style_url;
      const content = document.createElement('div');
      content.className = 'work-os-report-content k-doc';
      content.dataset.readerFormat = 'editorial.v1';
      content.innerHTML = payload.body_html;
      root.append(stylesheet, content);
      body.replaceChildren(host);
      if (sections && Array.isArray(payload.sections)) {{
        const sectionLookup = new Map(payload.sections
          .filter(function (section) {{ return section && section.section_id && section.dom_id; }})
          .map(function (section) {{ return [String(section.section_id), section]; }}));
        const discoveredGroups = Array.from(
          root.querySelectorAll('.tab-group-pane[data-tab-group]')
        );
        const groupById = new Map(discoveredGroups.map(function (pane) {{
          return [String(pane.dataset.tabGroup || ''), pane];
        }}));
        const canonicalGroups = WORK_OS_BRIEF_GROUP_IDS
          .map(function (groupId) {{ return groupById.get(groupId); }})
          .filter(Boolean);
        const orderedGroups = canonicalGroups.length ? canonicalGroups : discoveredGroups;
        const groupControls = new Map();
        const sectionControls = new Map();
        const sectionGroupIds = new Map();

        function activateReaderSection(groupPane, sectionId, shouldScroll) {{
          const sectionPanes = Array.from(groupPane.querySelectorAll('.subtab-pane[data-tab]'));
          sectionPanes.forEach(function (sectionPane) {{
            const candidateSectionId = String(sectionPane.dataset.tab || '');
            const isActive = candidateSectionId === sectionId;
            sectionPane.dataset.readerSectionActive = isActive ? 'true' : 'false';
            const sectionButton = sectionControls.get(candidateSectionId);
            if (sectionButton) {{
              if (isActive) sectionButton.setAttribute('aria-current', 'location');
              else sectionButton.removeAttribute('aria-current');
            }}
            if (isActive && shouldScroll) {{
              const reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
              sectionPane.scrollIntoView({{
                behavior: reducedMotion ? 'auto' : 'smooth', block: 'start'
              }});
              if (typeof sectionPane.focus === 'function') {{
                sectionPane.setAttribute('tabindex', '-1');
                sectionPane.focus({{ preventScroll: true }});
              }}
            }}
          }});
        }}

        function activateReaderGroup(groupId, shouldScroll) {{
          orderedGroups.forEach(function (groupPane) {{
            const candidateId = String(groupPane.dataset.tabGroup || '');
            const isActive = candidateId === groupId;
            groupPane.dataset.readerGroupActive = isActive ? 'true' : 'false';
            const controls = groupControls.get(candidateId);
            if (controls) {{
              controls.button.setAttribute('aria-expanded', isActive ? 'true' : 'false');
              controls.nested.hidden = !isActive;
              if (isActive) controls.button.setAttribute('aria-current', 'location');
              else controls.button.removeAttribute('aria-current');
            }}
            if (!isActive) return;
            const firstPane = groupPane.querySelector('.subtab-pane[data-tab]');
            if (firstPane) activateReaderSection(
              groupPane, String(firstPane.dataset.tab || ''), shouldScroll
            );
          }});
        }}

        orderedGroups.forEach(function (groupPane) {{
          const groupId = String(groupPane.dataset.tabGroup || '');
          if (!groupId) return;
          const group = document.createElement('div');
          group.className = 'work-os-reader-group';
          const button = document.createElement('button');
          button.type = 'button';
          button.className = 'work-os-reader-group-button k-btn k-btn-quiet k-btn-sm';
          const heading = groupPane.querySelector('.reader-group-title');
          button.textContent = heading && heading.textContent
            ? heading.textContent.trim()
            : workOsHumanizeSection(groupId);
          button.dataset.groupId = groupId;
          button.setAttribute('aria-expanded', 'false');
          const nested = document.createElement('div');
          nested.className = 'work-os-reader-group-sections';
          nested.setAttribute('role', 'group');
          nested.setAttribute('aria-label', button.textContent + ' sections');
          nested.hidden = true;
          groupControls.set(groupId, {{ button: button, nested: nested }});
          button.addEventListener('click', function () {{ activateReaderGroup(groupId, true); }});
          groupPane.querySelectorAll('.subtab-pane[data-tab]').forEach(function (sectionPane) {{
            const sectionId = String(sectionPane.dataset.tab || '');
            const section = sectionLookup.get(sectionId);
            if (!section || !root.getElementById(section.dom_id)) return;
            const sectionButton = document.createElement('button');
            sectionButton.type = 'button';
            sectionButton.className = 'work-os-reader-section-button k-btn k-btn-quiet k-btn-sm';
            sectionButton.textContent = section.label || workOsHumanizeSection(sectionId);
            sectionButton.dataset.sectionId = sectionId;
            sectionControls.set(sectionId, sectionButton);
            sectionGroupIds.set(sectionId, groupId);
            sectionButton.addEventListener('click', function () {{
              activateReaderGroup(groupId, false);
              activateReaderSection(groupPane, sectionId, true);
            }});
            nested.appendChild(sectionButton);
          }});
          group.append(button, nested);
          sections.appendChild(group);
        }});
        const requestedSectionId = options && typeof options.sectionId === 'string'
          ? options.sectionId : '';
        const requestedGroupId = sectionGroupIds.get(requestedSectionId);
        const requestedGroup = requestedGroupId ? groupById.get(requestedGroupId) : null;
        if (requestedGroup) {{
          activateReaderGroup(requestedGroupId, false);
          activateReaderSection(requestedGroup, requestedSectionId, true);
        }} else {{
          const initialGroup = orderedGroups[0];
          if (initialGroup) activateReaderGroup(String(initialGroup.dataset.tabGroup || ''), false);
        }}
        const requestedFactRef = options && typeof options.factRef === 'string' ? options.factRef : '';
        if (requestedFactRef) {{
          const factAnchor = Array.from(root.querySelectorAll('[data-fact-ref]')).find(function (node) {{
            return node.getAttribute('data-fact-ref') === requestedFactRef;
          }});
          if (factAnchor) {{
            factAnchor.classList.add('is-cited-location');
            factAnchor.scrollIntoView({{ block: 'center' }});
          }}
        }}
      }}
      root.addEventListener('click', function (event) {{
        const trigger = event.composedPath().find(function (node) {{ return node && node.dataset && node.dataset.peekUrl; }});
        if (trigger) {{
          event.preventDefault();
          window.workOsOpenPeekRoute(trigger.dataset.peekUrl, trigger.dataset.peekTitle || 'Source detail');
          return;
        }}
        const sourceLink = event.composedPath().find(function (node) {{
          return node && node.tagName === 'A' && typeof node.getAttribute === 'function'
            && String(node.getAttribute('href') || '').startsWith('/source/');
        }});
        if (!sourceLink) return;
        event.preventDefault();
        const sourceUrl = new URL(sourceLink.getAttribute('href'), window.location.origin);
        sourceUrl.searchParams.set('fragment', '1');
        window.workOsOpenPeekRoute(sourceUrl.pathname + sourceUrl.search + sourceUrl.hash, sourceLink.textContent.trim() || 'Source detail');
      }});
    }} catch (error) {{
      workOsReaderUnavailable(body, artifact, error && error.readerStatus);
    }}
  }}

  window.openWorkOsBriefReader = async function (tickerOrArtifact, options) {{
    if (tickerOrArtifact && typeof tickerOrArtifact === 'object' && tickerOrArtifact.artifact_id) {{
      if (!(options && options.fromHistory)) {{
        const origin = workOsHistoryOrigin();
        const focusId = workOsHistoryFocusId();
        workOsPushHistoryState(Object.assign({{}}, window.history.state || {{}}, {{ workOsBriefReader: {{ ticker: tickerOrArtifact.ticker, origin: workOsEncodeDetailOrigin(origin), focusId: focusId }} }}), workOsBriefUrl(tickerOrArtifact.ticker, origin, focusId));
      }}
      await workOsLoadBriefArtifact(tickerOrArtifact, options);
      return;
    }}
    const requestedTicker = workOsNormalizeTicker(tickerOrArtifact) || workOsCurrentCompanyTicker();
    if (!requestedTicker) return;
    if (!(options && options.fromHistory)) {{
      const origin = workOsHistoryOrigin();
      const focusId = workOsHistoryFocusId();
      workOsPushHistoryState(Object.assign({{}}, window.history.state || {{}}, {{ workOsBriefReader: {{ ticker: requestedTicker, origin: workOsEncodeDetailOrigin(origin), focusId: focusId }} }}), workOsBriefUrl(requestedTicker, origin, focusId));
    }}
    const response = await fetch('/api/work-os/briefs?ticker=' + encodeURIComponent(requestedTicker) + '&limit=1', {{ headers: {{ Accept: 'application/json' }} }});
    const payload = response.ok ? await response.json() : null;
    if (!payload || !payload.items || !payload.items.length) {{
      const title = document.getElementById('workOsBriefReaderTitle');
      const body = document.getElementById('workOsBriefReaderBody');
      if (title) title.textContent = requestedTicker + ' Full Research Brief';
      if (body) body.innerHTML = '<div class="k-well" role="alert">No persisted research brief is indexed for this company.</div>';
      if (briefReaderOverlay) briefReaderOverlay.open();
      return;
    }}
    await workOsLoadBriefArtifact(payload.items[0], options);
  }};
  window.openFullBriefCanvas = window.openWorkOsBriefReader;

  function workOsMoney(value, currency) {{
    if (!Number.isFinite(value)) return '-';
    const resolvedCurrency = typeof currency === 'string' && /^[A-Z]{{3}}$/.test(currency) ? currency : 'USD';
    return new Intl.NumberFormat('en-US', {{ style: 'currency', currency: resolvedCurrency, maximumFractionDigits: value >= 1000 ? 0 : 2 }}).format(value);
  }}

  function workOsPercent(value) {{
    if (!Number.isFinite(value)) return '-';
    return new Intl.NumberFormat('en-US', {{ maximumFractionDigits: 1, signDisplay: 'exceptZero' }}).format(value) + '%';
  }}

  function workOsPortfolioPercent(value) {{
    if (!Number.isFinite(value)) return 'Weight unavailable';
    return new Intl.NumberFormat('en-US', {{ maximumFractionDigits: 1 }}).format(value) + '%';
  }}

  function workOsIntegerMoney(value, currency) {{
    if (!Number.isFinite(value)) return '—';
    const resolvedCurrency = typeof currency === 'string' && /^[A-Z]{{3}}$/.test(currency) ? currency : 'USD';
    return new Intl.NumberFormat('en-US', {{ style: 'currency', currency: resolvedCurrency, maximumFractionDigits: 0, minimumFractionDigits: 0 }}).format(value);
  }}

  function workOsAllocationRows(allocation) {{
    if (!allocation || allocation.state !== 'available' || !allocation.buckets) {{
      return '<div class="stat-subtext">Allocation mix unavailable</div>';
    }}
    const buckets = allocation.buckets;
    const entries = [
      ['Domestic ETF', buckets.us_etf], ['Intl ETF', buckets.international_etf],
      ['Domestic Equity', buckets.us_equity], ['Intl Equity', buckets.international_equity],
      ['Cash reserve', buckets.cash], ['Unclassified', buckets.unclassified]
    ];
    const rendered = entries.filter(function (entry) {{ return Number.isFinite(Number(entry[1] && entry[1].weight_pct)); }})
      .map(function (entry) {{ return '<div class="stat-subtext work-os-allocation-row">' + workOsPortfolioPercent(Number(entry[1].weight_pct)) + ' ' + entry[0] + '</div>'; }});
    return rendered.length ? rendered.join('') : '<div class="stat-subtext">Allocation mix unavailable</div>';
  }}

  function workOsPillClass(status) {{
    const normalized = String(status || '').toLowerCase();
    if (normalized.includes('breach') || normalized.includes('fail')) return 'k-pill k-pill-bad';
    if (normalized && !['intact', 'pass', 'passing', 'ok'].includes(normalized)) return 'k-pill k-pill-warn';
    return 'k-pill k-pill-ok';
  }}

  function workOsRenderEarningsDoorway(doorway, latestReadout, ticker) {{
    const target = document.getElementById('workOsEarningsDoorway');
    if (!target) return;
    if (doorway && doorway.status === 'available' && doorway.route) {{
      const title = doorway.phase === 'post'
        ? 'Post-earnings readout — ' + ticker
        : 'Earnings prep — ' + ticker;
      target.innerHTML = '<button class="k-chip is-active" type="button" data-peek-url="' + escapeWorkOsHtml(doorway.route) + '" data-peek-title="' + escapeWorkOsHtml(title) + '">' + escapeWorkOsHtml(doorway.label) + '</button>';
      return;
    }}
    const latestButton = latestReadout && latestReadout.route
      ? '<button class="k-chip is-active" type="button" data-peek-url="' + escapeWorkOsHtml(latestReadout.route) + '" data-peek-title="Post-earnings readout — ' + escapeWorkOsHtml(ticker) + '">' + escapeWorkOsHtml(latestReadout.period_label) + ' readout &rarr;</button>'
      : '';
    if (doorway && doorway.status === 'pending') {{
      const fallbackRoute = doorway.route || (doorway.phase === 'post'
        ? '/api/peek/earnings-readout?ticker=' + encodeURIComponent(ticker)
        : '/api/peek/earnings-prep?ticker=' + encodeURIComponent(ticker));
      const title = (doorway.phase === 'post' ? 'Post-earnings readout — ' : 'Earnings prep — ') + ticker;
      target.innerHTML = '<button class="k-chip" type="button" data-peek-url="' + escapeWorkOsHtml(fallbackRoute) + '" data-peek-title="' + escapeWorkOsHtml(title) + '">' + escapeWorkOsHtml(doorway.label) + '</button>' + latestButton;
      return;
    }}
    if (latestButton) {{ target.innerHTML = latestButton; return; }}
    target.innerHTML = '<span class="k-card-meta">Earnings artifact unavailable</span>';
  }}

  function workOsCanonicalDetailRoute(route) {{
    let parsed;
    try {{ parsed = new URL(route, window.location.origin); }} catch (_error) {{ return null; }}
    if (parsed.origin !== window.location.origin || !WORK_OS_FULL_PAGE_PEEK_PATHS.some(function (pattern) {{ return pattern.test(parsed.pathname); }})) return null;
    if (parsed.pathname.startsWith('/source/')) parsed.searchParams.set('fragment', '1');
    return parsed.pathname + parsed.search + parsed.hash;
  }}

  function workOsEncodeDetailOrigin(origin) {{
    return [origin.surface || '', origin.ticker || '', origin.section || ''].join('|');
  }}

  function workOsDecodeDetailOrigin(value) {{
    const fields = typeof value === 'string' ? value.split('|') : [];
    if (fields.length !== 3 || !WORK_OS_ROUTE_DESTINATIONS.includes(fields[0])) return null;
    const ticker = fields[1] || null;
    const section = fields[2] || null;
    if (!workOsValidHistoryTicker(ticker) || !workOsValidHistorySection(section)) return null;
    return {{ surface: fields[0], ticker: ticker, section: section }};
  }}

  function workOsFullPageDetailUrl(route, title, origin) {{
    const url = new URL(window.location.href);
    url.searchParams.set('work_os_detail', route);
    url.searchParams.set('work_os_detail_title', String(title || 'Research detail'));
    url.searchParams.set('work_os_detail_origin', workOsEncodeDetailOrigin(origin));
    url.hash = origin.surface;
    return url.pathname + url.search + url.hash;
  }}

  function workOsDetailOriginUrl(origin) {{
    const safeOrigin = origin || {{ surface: 'screen-cockpit', ticker: null, section: null }};
    const url = new URL(window.location.href);
    url.searchParams.delete('work_os_detail');
    url.searchParams.delete('work_os_detail_title');
    url.searchParams.delete('work_os_detail_origin');
    if (safeOrigin.ticker) url.searchParams.set('ticker', safeOrigin.ticker);
    else url.searchParams.delete('ticker');
    if (safeOrigin.section === 'company-desk' || safeOrigin.section === 'analytics-playground') url.searchParams.set('screen', safeOrigin.section);
    else url.searchParams.delete('screen');
    url.hash = safeOrigin.surface || 'screen-cockpit';
    return url.pathname + url.search + url.hash;
  }}

  function workOsAbortFullPageDetailRequest() {{
    workOsFullPageDetailRequestSequence += 1;
    if (workOsFullPageDetailRequestController) {{
      workOsFullPageDetailRequestController.abort();
      workOsFullPageDetailRequestController = null;
    }}
  }}

  async function workOsOpenPeekFullPage(route, title, options) {{
    const canonicalRoute = workOsCanonicalDetailRoute(route);
    const body = document.getElementById('workOsFullPageDetailBody');
    const heading = document.getElementById('workOsFullPageDetailTitle');
    if (!canonicalRoute || !body || !heading || !fullPageDetailOverlay) return false;
    const origin = options && options.origin ? options.origin : workOsHistoryOrigin();
    if (!(options && options.fromHistory)) {{
      workOsPushHistoryState(Object.assign({{}}, window.history.state || {{}}, {{
        screenId: origin.surface, ticker: origin.ticker,
        workOsFullPageDetail: {{ route: canonicalRoute, title: String(title || 'Research detail'), origin: workOsEncodeDetailOrigin(origin) }}
      }}), workOsFullPageDetailUrl(canonicalRoute, title, origin));
    }}
    heading.textContent = String(title || 'Research detail');
    body.innerHTML = '<div class="k-well" role="status">Loading persisted research detail…</div>';
    const requestSequence = ++workOsFullPageDetailRequestSequence;
    if (workOsFullPageDetailRequestController) workOsFullPageDetailRequestController.abort();
    const controller = new AbortController();
    workOsFullPageDetailRequestController = controller;
    fullPageDetailOverlay.open();
    try {{
      const parsedRoute = new URL(canonicalRoute, window.location.origin);
      const response = await fetch(parsedRoute.pathname + parsedRoute.search, {{ signal: controller.signal, headers: {{ Accept: 'text/html' }} }});
      if (!response.ok) throw new Error('HTTP ' + response.status);
      const markup = await response.text();
      if (requestSequence !== workOsFullPageDetailRequestSequence) return false;
      body.innerHTML = markup;
      const sourceLocator = parsedRoute.hash ? parsedRoute.hash.slice(1) : '';
      if (sourceLocator) {{
        const located = body.querySelector('#' + CSS.escape(sourceLocator));
        if (located) {{ located.classList.add('is-cited-location'); located.scrollIntoView({{ block: 'center' }}); }}
      }}
      return true;
    }} catch (error) {{
      if ((error && error.name === 'AbortError') || requestSequence !== workOsFullPageDetailRequestSequence) return false;
      body.innerHTML = '<div class="k-well" role="alert">This persisted research detail is unavailable.</div>';
      return false;
    }} finally {{
      if (requestSequence === workOsFullPageDetailRequestSequence && workOsFullPageDetailRequestController === controller) workOsFullPageDetailRequestController = null;
    }}
  }}
  window.workOsOpenPeekFullPage = workOsOpenPeekFullPage;

  function workOsClosePeekFullPage() {{
    const state = window.history.state && window.history.state.workOsFullPageDetail;
    if (state) {{ window.history.back(); return; }}
    const params = new URLSearchParams(window.location.search);
    const origin = workOsDecodeDetailOrigin(params.get('work_os_detail_origin')) || {{ surface: 'screen-cockpit', ticker: null, section: null }};
    window.history.replaceState({{ screenId: origin.surface, ticker: origin.ticker }}, '', workOsDetailOriginUrl(origin));
    if (fullPageDetailOverlay) fullPageDetailOverlay.close();
    window.navigateTo(origin.surface, {{ fromHistory: true }});
  }}
  window.closeWorkOsFullPageDetail = workOsClosePeekFullPage;
  const fullPageDetailBack = document.getElementById('workOsFullPageDetailBack');
  if (fullPageDetailBack) fullPageDetailBack.addEventListener('click', workOsClosePeekFullPage);

  async function workOsOpenPeekRoute(route, title, options) {{
    const ref = document.getElementById('peekRefKey');
    const body = document.getElementById('peekProse');
    const openFullPage = document.getElementById('workOsPeekOpenFullPage');
    const canonicalRoute = workOsCanonicalDetailRoute(route);
    if (!ref || !body || !peekOverlay || !canonicalRoute) return;
    if (!(options && options.fromHistory) && window.matchMedia('(max-width: 47.5rem)').matches) {{
      return workOsOpenPeekFullPage(canonicalRoute, title);
    }}
    const parsedRoute = new URL(canonicalRoute, window.location.origin);
    if (!(options && options.fromHistory)) {{
      workOsPushTransientHistory('peek', {{
        route: canonicalRoute,
        title: String(title || 'Research detail'),
        focusId: workOsHistoryFocusId()
      }});
    }}
    const requestSequence = ++workOsPeekRequestSequence;
    const sourceLocator = parsedRoute.hash ? parsedRoute.hash.slice(1) : '';
    if (workOsPeekRequestController) workOsPeekRequestController.abort();
    const controller = new AbortController();
    workOsPeekRequestController = controller;
    ref.textContent = title || 'Research detail';
    if (openFullPage) {{
      openFullPage.hidden = false;
      openFullPage.onclick = function () {{
        const origin = workOsHistoryOrigin();
        const routeState = workOsRouteFromHistoryState(window.history.state);
        if (routeState && routeState.overlay === 'peek') {{
          window.history.replaceState({{ screenId: origin.surface, ticker: origin.ticker }}, '', workOsDetailOriginUrl(origin));
        }}
        if (peekOverlay) peekOverlay.close();
        void workOsOpenPeekFullPage(canonicalRoute, title, {{ origin: origin }});
      }};
    }}
    body.innerHTML = '<div class="k-well" role="status">Loading persisted research artifact…</div>';
    peekOverlay.open();
    try {{
      const response = await fetch(parsedRoute.pathname + parsedRoute.search, {{
        signal: controller.signal,
        headers: {{ Accept: 'text/html' }}
      }});
      if (!response.ok) throw new Error('HTTP ' + response.status);
      const html = await response.text();
      if (requestSequence !== workOsPeekRequestSequence) return;
      body.innerHTML = html;
      if (sourceLocator) {{
        const located = body.querySelector('#' + CSS.escape(sourceLocator));
        if (located) {{
          located.classList.add('is-cited-location');
          located.scrollIntoView({{ block: 'center' }});
        }}
      }}
    }} catch (error) {{
      if ((error && error.name === 'AbortError') || requestSequence !== workOsPeekRequestSequence) return;
      body.innerHTML = '<div class="k-well" role="alert">The persisted earnings artifact is unavailable.</div>';
    }} finally {{
      if (requestSequence === workOsPeekRequestSequence && workOsPeekRequestController === controller) {{
        workOsPeekRequestController = null;
      }}
    }}
  }}

  function workOsOpenThresholdReview(ticker) {{
    const safeTicker = workOsNormalizeTicker(ticker);
    if (!safeTicker) return false;
    const origin = workOsHistoryOrigin();
    const url = new URL('/advisor/sizing-intents/' + encodeURIComponent(safeTicker), window.location.origin);
    url.searchParams.set('work_os_origin', workOsEncodeDetailOrigin(origin));
    window.location.assign(url.pathname + url.search);
    return true;
  }}

  function workOsAbortPeekRequest() {{
    workOsPeekRequestSequence += 1;
    if (workOsPeekRequestController) {{
      workOsPeekRequestController.abort();
      workOsPeekRequestController = null;
    }}
  }}
  window.workOsOpenPeekRoute = workOsOpenPeekRoute;

  document.addEventListener('click', function (event) {{
    const trigger = event.target instanceof Element
      ? event.target.closest('[data-peek-url]') : null;
    if (!trigger) return;
    const route = trigger.getAttribute('data-peek-url') || '';
    if (!route.startsWith('/api/peek/') && !/^\\/api\\/governed-alerts\\/[1-9][0-9]*\\/evidence$/.test(route)) return;
    event.preventDefault();
    event.stopPropagation();
    workOsOpenPeekRoute(route, trigger.getAttribute('data-peek-title') || 'Research detail');
  }});

  document.addEventListener('click', async function (event) {{
    const trigger = event.target instanceof Element
      ? event.target.closest('[data-generate-readout]') : null;
    if (!trigger) return;
    const ticker = trigger.getAttribute('data-generate-readout') || '';
    if (!ticker) return;
    event.preventDefault();
    event.stopPropagation();
    const originalText = trigger.textContent;
    trigger.disabled = true;
    trigger.textContent = 'Generating persisted readout…';
    try {{
      const response = await fetch('/api/earnings-readout/generate', {{
        method: 'POST',
        headers: {{ 'Content-Type': 'application/json', Accept: 'application/json' }},
        body: JSON.stringify({{ ticker: ticker }})
      }});
      const data = await response.json();
      if (!response.ok) {{
        throw new Error(data && data.error ? data.error : 'HTTP ' + response.status);
      }}
      const artifactId = Number(data.artifact_id);
      if (!Number.isInteger(artifactId) || artifactId <= 0) throw new Error('Invalid artifact identity');
      workOsPortfolioHydration = null;
      await workOsEnsurePortfolioHydration();
      await workOsOpenPeekRoute('/api/peek/earnings-readout?ticker=' + encodeURIComponent(ticker) + '&artifact_id=' + encodeURIComponent(String(artifactId)), 'Post-earnings readout — ' + ticker);
      if (workOsCurrentCompanyTicker() === ticker) {{
        await workOsRenderCompanyDesk(ticker);
      }}
    }} catch (err) {{
      trigger.disabled = false;
      trigger.textContent = originalText;
      const statusEl = document.createElement('span');
      statusEl.className = 'stat-subtext';
      statusEl.dataset.tone = 'bad';
      statusEl.textContent = ' (' + (err.message || 'Generation failed') + ')';
      trigger.insertAdjacentElement('afterend', statusEl);
    }}
  }});

  function workOsCompanyByTicker(ticker) {{
    const portfolioCompanies = workOsPortfolioHydration && Array.isArray(workOsPortfolioHydration.companies)
      ? workOsPortfolioHydration.companies : [];
    const researchCompanies = Array.isArray(workOsResearchCompanies) ? workOsResearchCompanies : [];
    return portfolioCompanies.concat(researchCompanies).find(function (company) {{ return company.ticker === ticker; }}) || null;
  }}

  function workOsCompanyPickerCompanies() {{
    const portfolioCompanies = workOsPortfolioHydration && Array.isArray(workOsPortfolioHydration.companies)
      ? workOsPortfolioHydration.companies : [];
    const seenTickers = new Set();
    return portfolioCompanies.concat(workOsResearchCompanies || []).filter(function (company) {{
      const ticker = String(company.ticker || '').toUpperCase();
      if (!ticker || seenTickers.has(ticker)) return false;
      seenTickers.add(ticker);
      return true;
    }});
  }}

  function workOsRenderCompanyPickerOptions(query, resetSelection) {{
    if (!companyPickerList || !companyPickerSearch) return;
    const normalizedQuery = String(query || '').trim().toLowerCase();
    companyPickerMatches = workOsCompanyPickerCompanies().filter(function (company) {{
      return String(company.ticker || '').toLowerCase().includes(normalizedQuery)
        || String(company.name || '').toLowerCase().includes(normalizedQuery);
    }}).sort(function (left, right) {{
      const leftRank = left.coverage_role === 'portfolio' ? 0 : 1;
      const rightRank = right.coverage_role === 'portfolio' ? 0 : 1;
      return leftRank - rightRank || String(left.ticker).localeCompare(String(right.ticker));
    }}).slice(0, 12);
    if (resetSelection) companyPickerActiveIndex = companyPickerMatches.length ? 0 : -1;
    else if (companyPickerActiveIndex >= companyPickerMatches.length) companyPickerActiveIndex = companyPickerMatches.length - 1;
    companyPickerList.innerHTML = companyPickerMatches.length ? companyPickerMatches.map(function (company, index) {{
      const selected = index === companyPickerActiveIndex;
      return '<li role="option" id="companyPickerOption-' + index + '" aria-selected="' + (selected ? 'true' : 'false') + '" data-company-picker-ticker="' + escapeWorkOsHtml(company.ticker) + '"><span class="k-ticker-symbol t-mono">' + escapeWorkOsHtml(company.ticker) + '</span><span class="k-ticker-name">' + escapeWorkOsHtml(company.name || company.ticker) + '</span></li>';
    }}).join('') : '<li class="k-card-meta">No matching companies</li>';
    if (companyPickerActiveIndex >= 0) companyPickerSearch.setAttribute('aria-activedescendant', 'companyPickerOption-' + companyPickerActiveIndex);
    else companyPickerSearch.removeAttribute('aria-activedescendant');
  }}

  async function workOsOpenCompanyPicker() {{
    if (!companyPickerOverlay || companyPickerOverlay.isOpen()) return;
    companyPickerActiveIndex = -1;
    if (companyPickerStatus) companyPickerStatus.textContent = 'Loading company list';
    companyPickerOverlay.open();
    try {{ await workOsEnsureResearchCompanies(); }} catch (error) {{ workOsResearchCompanies = []; }}
    workOsRenderCompanyPickerOptions('', true);
    if (companyPickerStatus) companyPickerStatus.textContent = companyPickerMatches.length + ' companies available';
  }}

  function workOsChooseCompany(ticker) {{
    if (companyPickerOverlay) companyPickerOverlay.close();
    window.switchCompanyWorkspace(ticker);
  }}

  if (companyPickerTrigger) {{
    companyPickerTrigger.addEventListener('click', function () {{
      if (companyPickerOverlay && companyPickerOverlay.isOpen()) companyPickerOverlay.close();
      else workOsOpenCompanyPicker();
    }});
    companyPickerTrigger.addEventListener('keydown', function (ev) {{
      if (ev.key === 'ArrowDown') {{ ev.preventDefault(); workOsOpenCompanyPicker(); }}
    }});
  }}
  if (companyPickerSearch) {{
    companyPickerSearch.addEventListener('input', function () {{ workOsRenderCompanyPickerOptions(companyPickerSearch.value, true); }});
    companyPickerSearch.addEventListener('keydown', function (ev) {{
      if (ev.key === 'ArrowDown') {{
        ev.preventDefault();
        if (companyPickerMatches.length) companyPickerActiveIndex = Math.min(companyPickerActiveIndex + 1, companyPickerMatches.length - 1);
      }} else if (ev.key === 'ArrowUp') {{
        ev.preventDefault();
        if (companyPickerMatches.length) companyPickerActiveIndex = Math.max(companyPickerActiveIndex - 1, 0);
      }} else if (ev.key === 'Enter') {{
        ev.preventDefault();
        if (companyPickerActiveIndex >= 0 && companyPickerMatches[companyPickerActiveIndex]) workOsChooseCompany(companyPickerMatches[companyPickerActiveIndex].ticker);
        return;
      }} else {{ return; }}
      workOsRenderCompanyPickerOptions(companyPickerSearch.value, false);
    }});
  }}
  if (companyPickerList) companyPickerList.addEventListener('click', function (event) {{
    const option = event.target instanceof Element ? event.target.closest('[data-company-picker-ticker]') : null;
    if (option) workOsChooseCompany(option.getAttribute('data-company-picker-ticker'));
  }});
  document.addEventListener('click', function (event) {{
    if (companyPickerOverlay && companyPickerOverlay.isOpen() && companyPickerRoot
        && event.target instanceof Node && !companyPickerRoot.contains(event.target)) {{
      companyPickerOverlay.close();
    }}
  }});

  const workOsDeskTabs = {{
    thesis: 'deskTabThesis',
    financials: 'deskTabFinancials',
    transcripts: 'deskTabTranscripts',
    notes: 'deskTabNotes'
  }};

  function workOsSwitchDeskTab(tab) {{
    const panelId = workOsDeskTabs[tab];
    if (!panelId) return false;
    document.querySelectorAll('[data-desk-tab]').forEach(function (button) {{
      const active = button.getAttribute('data-desk-tab') === tab;
      button.classList.toggle('is-active', active);
      button.setAttribute('aria-selected', active ? 'true' : 'false');
    }});
    Object.values(workOsDeskTabs).forEach(function (candidateId) {{
      const panel = document.getElementById(candidateId);
      if (panel) panel.hidden = candidateId !== panelId;
    }});
    return true;
  }}

  document.addEventListener('click', function (event) {{
    const tab = event.target instanceof Element ? event.target.closest('[data-desk-tab]') : null;
    if (tab) workOsSwitchDeskTab(tab.getAttribute('data-desk-tab'));
  }});

  async function workOsEnsureResearchCompanies() {{
    if (Array.isArray(workOsResearchCompanies)) return workOsResearchCompanies;
    const response = await fetch('/api/tickers', {{ headers: {{ Accept: 'application/json' }} }});
    if (!response.ok) throw new Error('HTTP ' + response.status);
    const payload = await response.json();
    workOsResearchCompanies = Array.isArray(payload.tickers) ? payload.tickers.filter(function (item) {{
      return item.list_type === 'portfolio' || item.list_type === 'evaluation';
    }}).map(function (item) {{
      return {{ ticker: String(item.ticker || '').toUpperCase(), name: item.name || item.ticker, coverage_role: item.list_type || 'unknown' }};
    }}) : [];
    return workOsResearchCompanies;
  }}

  async function workOsRenderCompanyDesk(ticker) {{
    const normalized = workOsNormalizeTicker(ticker) || workOsCurrentCompanyTicker();
    const screen = document.getElementById('screen-workspace');
    if (!normalized || !screen) return false;
    const requestSequence = ++workOsCompanyRequestSequence;
    if (workOsCompanyRequestController) workOsCompanyRequestController.abort();
    const controller = new AbortController();
    workOsCompanyRequestController = controller;
    if (companyPickerStatus) companyPickerStatus.textContent = 'Loading ' + normalized + ' company desk';
    screen.setAttribute('aria-busy', 'true');
    try {{
      try {{ await workOsEnsureResearchCompanies(); }} catch (error) {{ workOsResearchCompanies = []; }}
      if (requestSequence !== workOsCompanyRequestSequence) return false;
      const company = workOsCompanyByTicker(normalized);
      if (!company) throw new Error('Unknown company ' + normalized);
      const sayDoRequest = fetch('/api/company/' + encodeURIComponent(normalized) + '/say-do', {{ signal: controller.signal, headers: {{ Accept: 'application/json' }} }})
        .then(function (candidate) {{ return candidate.ok ? candidate.json() : null; }})
        .catch(function () {{ return null; }});
      const response = await fetch('/api/work-os/companies/' + encodeURIComponent(normalized) + '/desk', {{ signal: controller.signal, headers: {{ Accept: 'application/json' }} }});
      if (!response.ok) throw new Error('HTTP ' + response.status);
      const desk = await response.json();
      const sayDo = await sayDoRequest;
      if (requestSequence !== workOsCompanyRequestSequence) return false;
      const identity = desk.company || {{}};
      const identityTicker = String(identity.ticker || normalized).toUpperCase();
      if (identityTicker !== normalized) throw new Error('Company response mismatch');
      document.getElementById('deskTicker').textContent = identity.ticker || normalized;
      document.getElementById('deskCompanyName').textContent = identity.name || company.name;
      document.getElementById('deskCoverageRole').textContent = String(identity.coverage_role || 'unknown') + ' coverage';
      const decision = desk.current_decision || {{ relationship: 'unavailable' }};
      const ownerDecision = decision.owner || null;
      const modelDecision = decision.model || null;
      document.getElementById('deskDecisionBand').dataset.freshness = decision.freshness || 'unavailable';
      document.getElementById('deskOwnerState').textContent = ownerDecision ? String(ownerDecision.value).toUpperCase() : '—';
      document.getElementById('deskModelState').textContent = modelDecision ? String(modelDecision.value).toUpperCase() : '—';
      document.getElementById('deskOwnerRevision').textContent = workOsDecisionMeta(ownerDecision, 'No owner decision recorded') + (decision.freshness === 'stale' ? ' · stale' : '');
      document.getElementById('deskModelRevision').textContent = workOsDecisionMeta(modelDecision, 'No model recommendation recorded');
      const relationship = String(decision.relationship || 'unavailable');
      const decisionRelationship = document.getElementById('deskDecisionRelationship');
      if (decisionRelationship) {{
        decisionRelationship.textContent = relationship.replaceAll('_', ' ').toUpperCase();
        decisionRelationship.className = 'k-pill';
        decisionRelationship.classList.toggle('k-pill-ok', relationship === 'agree');
        decisionRelationship.classList.toggle('k-pill-bad', relationship === 'conflict');
        decisionRelationship.classList.toggle('k-pill-warn', relationship !== 'agree' && relationship !== 'conflict');
      }}
      const decisionFreshness = document.getElementById('deskDecisionFreshness');
      if (decisionFreshness) {{
        const freshness = String(decision.freshness || 'unavailable');
        decisionFreshness.textContent = freshness === 'current'
          ? 'Current decision state'
          : 'Decision state ' + freshness.replaceAll('_', ' ') + ' · do not treat as current';
      }}
      const position = desk.position || {{}};
      const weight = Number.isFinite(position.weight_pct) ? position.weight_pct : null;
      const positionState = String(position.position_state || 'unavailable');
      document.getElementById('deskPositionWeight').textContent = Number.isFinite(weight)
        ? workOsPercent(weight)
        : (positionState === 'not_held' ? 'Not held' : 'Weight unavailable');
      document.getElementById('deskHeroPositionWeight').textContent = Number.isFinite(weight)
        ? workOsPercent(weight)
        : (positionState === 'not_held' ? 'Not held' : 'Weight unavailable');
      const trackerPositionSource = position.position_source === 'portfolio_tracker_api'
        ? 'Portfolio Tracker snapshot'
        : 'Portfolio Tracker';
      document.getElementById('deskPositionSource').textContent = positionState === 'unavailable'
        ? 'Tracker snapshot unavailable'
        : trackerPositionSource + (position.position_as_of ? ' · as of ' + position.position_as_of : '') + (positionState === 'not_held' ? ' · not held' : '');
      const valuationSource = position.source ? String(position.source).replaceAll('_', ' ') : 'governed DCF snapshot';
      document.getElementById('deskLivePrice').textContent = Number.isFinite(position.price) ? workOsMoney(position.price, position.currency) : 'Unavailable';
      document.getElementById('deskInputPrice').textContent = Number.isFinite(position.price) ? workOsMoney(position.price, position.currency) : '—';
      document.getElementById('deskInputPriceSource').textContent = Number.isFinite(position.price) ? valuationSource + ' · as of ' + (position.price_as_of || 'date unavailable') : 'No governed input price';
      document.getElementById('deskFairValue').textContent = Number.isFinite(position.fair_value) ? workOsMoney(position.fair_value, position.currency) : '—';
      document.getElementById('deskFairValueSource').textContent = Number.isFinite(position.fair_value) ? valuationSource + ' · as of ' + (position.fair_value_as_of || 'date unavailable') : 'No governed fair value';
      document.getElementById('deskHeroFairValue').textContent = Number.isFinite(position.fair_value) ? workOsMoney(position.fair_value, position.currency) : '—';
      const valuationGap = Number.isFinite(position.price) && Number.isFinite(position.fair_value) && position.price !== 0
        ? ((position.fair_value / position.price) - 1) * 100 : null;
      document.getElementById('deskValuationGap').innerHTML = Number.isFinite(valuationGap)
        ? '<span class="k-pill ' + (valuationGap >= 0 ? 'k-pill-ok' : 'k-pill-bad') + '">' + escapeWorkOsHtml(workOsPercent(valuationGap)) + '</span>'
        : '<span class="k-pill">Unavailable</span>';
      const financials = document.getElementById('deskFinancialsSummary');
      if (financials) financials.innerHTML = Number.isFinite(position.price) || Number.isFinite(position.fair_value)
        ? '<div class="k-well"><strong>Governed valuation snapshot</strong><div class="stat-subtext">Price: ' + escapeWorkOsHtml(Number.isFinite(position.price) ? workOsMoney(position.price, position.currency) : 'unavailable') + ' · Fair value: ' + escapeWorkOsHtml(Number.isFinite(position.fair_value) ? workOsMoney(position.fair_value, position.currency) : 'unavailable') + ' · Source: ' + escapeWorkOsHtml(valuationSource) + ' · Price as of ' + escapeWorkOsHtml(position.price_as_of || 'unavailable') + ' · Fair value as of ' + escapeWorkOsHtml(position.fair_value_as_of || 'unavailable') + '</div></div>'
        : '<div class="k-well">Governed valuation inputs are unavailable for this company.</div>';
      const commitments = sayDo && Array.isArray(sayDo.commitments) ? sayDo.commitments : [];
      document.getElementById('deskSayDoTimeline').innerHTML = commitments.length ? commitments.map(function (commitment) {{
        return '<div class="k-well"><strong>' + escapeWorkOsHtml(commitment.statement) + '</strong><div class="stat-subtext">' + escapeWorkOsHtml(commitment.status || 'evaluating') + ' · ' + escapeWorkOsHtml(commitment.source_ref || 'source unavailable') + ' · as of ' + escapeWorkOsHtml(commitment.as_of || sayDo.as_of || 'unavailable') + '</div></div>';
      }}).join('') : '<div class="k-well">Say/Do history is unavailable or has no governed commitments.</div>';
      const transcripts = document.getElementById('deskTranscriptsQA');
      if (transcripts) transcripts.innerHTML = desk.latest_earnings_readout
        ? '<div class="k-well"><strong>' + escapeWorkOsHtml(desk.latest_earnings_readout.period_label || 'Latest earnings readout') + '</strong><div class="stat-subtext">Open the governed earnings artifact for sourced transcript evidence.</div></div>'
        : '<div class="k-well">No governed transcript Q&amp;A projection is available.</div>';
      const provenance = document.getElementById('deskProvenanceLinks');
      if (provenance) provenance.innerHTML = desk.latest_brief
        ? '<div class="k-well"><strong>Persisted research brief</strong><div class="stat-subtext">' + escapeWorkOsHtml(desk.latest_brief.report_date || 'date unavailable') + ' · ' + escapeWorkOsHtml(desk.latest_brief.coverage_role || 'unknown') + ' coverage</div></div>'
        : '<div class="k-well">No persisted research artifact is indexed for provenance.</div>';
      const brief = desk.latest_brief || null;
      document.getElementById('deskBriefDate').textContent = brief ? brief.report_date : '—';
      document.getElementById('deskBriefStatus').textContent = brief ? (brief.reader_mode === 'shared_body' ? 'Shared reader ready' : 'Legacy standalone') : 'No indexed artifact';
      const briefButton = document.getElementById('workOsFullBriefButton');
      if (briefButton) {{
        briefButton.disabled = !brief;
        briefButton.onclick = brief ? function () {{ openWorkOsBriefReader(brief); }} : null;
      }}
      const thesisRisk = desk.thesis_risk || {{ status: 'unavailable', unavailable_reason: 'missing' }};
      const thesisAvailable = thesisRisk.status === 'available';
      const thesisStatus = document.getElementById('deskThesisStatus');
      if (thesisStatus) {{
        const status = thesisAvailable ? String(thesisRisk.overall_breach_status || 'unavailable') : 'unavailable';
        thesisStatus.textContent = status.toUpperCase();
        thesisStatus.className = 'k-pill';
        thesisStatus.classList.toggle('k-pill-ok', status === 'ok');
        thesisStatus.classList.toggle('k-pill-bad', status === 'breach');
        thesisStatus.classList.toggle('k-pill-warn', status !== 'ok' && status !== 'breach');
      }}
      const thesisAsOf = document.getElementById('deskThesisAsOf');
      if (thesisAsOf) {{
        thesisAsOf.textContent = thesisAvailable
          ? 'Evaluated · as of ' + String(thesisRisk.evaluated_at || 'date unavailable')
          : 'Thesis evidence ' + String(thesisRisk.unavailable_reason || 'unavailable') + ' · do not treat as current';
      }}
      const thesisMount = document.getElementById('deskThesisRisk');
      if (thesisMount) {{
        if (!thesisAvailable) {{
          thesisMount.innerHTML = '<div class="k-well" role="alert">Current thesis risk is unavailable because its report-backed facts are ' + escapeWorkOsHtml(String(thesisRisk.unavailable_reason || 'unavailable')) + '.</div>';
        }} else {{
          const breakRules = Array.isArray(thesisRisk.break_rules) ? thesisRisk.break_rules : [];
          const ruleList = breakRules.length
            ? '<ul>' + breakRules.map(function (rule) {{
                const latest = Number.isFinite(rule.latest_value) ? String(rule.latest_value) + (rule.unit ? ' ' + escapeWorkOsHtml(String(rule.unit)) : '') : 'unknown';
                const distance = Number.isFinite(rule.distance_to_threshold) ? ' · distance ' + String(rule.distance_to_threshold) : '';
                const doorway = brief
                  ? '<button class="k-btn k-btn-quiet k-btn-sm" type="button" data-desk-thesis-rule="true">Open thesis evidence →</button>'
                  : '';
                return '<li><strong>' + escapeWorkOsHtml(String(rule.status || 'unresolved').toUpperCase()) + '</strong> · ' + escapeWorkOsHtml(String(rule.kpi_name || rule.rule_id || 'Break rule')) + ' · latest ' + latest + ' vs ' + escapeWorkOsHtml(String(rule.comparator || '')) + ' ' + escapeWorkOsHtml(String(rule.threshold ?? '')) + distance + '<div class="stat-subtext">Source ' + escapeWorkOsHtml(String(rule.provenance_ref || 'unavailable')) + ' · ' + escapeWorkOsHtml(String(rule.latest_period || thesisRisk.evaluated_at || 'date unavailable')) + '</div>' + doorway + '</li>';
              }}).join('') + '</ul>'
            : '<div class="stat-subtext">No evaluated canonical break rules are available.</div>';
          thesisMount.innerHTML = '<div class="k-well"><strong>' + escapeWorkOsHtml(String(thesisRisk.overall_breach_status || 'unavailable').toUpperCase()) + '</strong><div class="stat-subtext">Report evaluation · as of ' + escapeWorkOsHtml(String(thesisRisk.evaluated_at || 'date unavailable')) + '</div><p>' + escapeWorkOsHtml(String(thesisRisk.thesis || '')) + '</p>' + ruleList + '</div>';
          thesisMount.querySelectorAll('[data-desk-thesis-rule]').forEach(function (button) {{
            button.addEventListener('click', function () {{
              openWorkOsBriefReader(brief, {{ sectionId: 'thesis' }});
            }});
          }});
        }}
      }}
      const thesisBriefDoorway = document.getElementById('deskThesisBriefDoorway');
      if (thesisBriefDoorway) {{
        thesisBriefDoorway.disabled = !brief;
        thesisBriefDoorway.onclick = brief ? function () {{ openWorkOsBriefReader(brief, {{ sectionId: 'thesis' }}); }} : null;
      }}
      const kpiSummary = desk.kpi_summary || {{ status: 'unavailable', unavailable_reason: 'missing' }};
      const kpiMount = document.getElementById('deskKpiSummary');
      if (kpiMount) {{
        const kpis = Array.isArray(kpiSummary.items) ? kpiSummary.items : [];
        if (kpiSummary.status !== 'available' || !kpis.length) {{
          kpiMount.innerHTML = '<div class="k-well" role="alert">Tier-1 KPI evidence is ' + escapeWorkOsHtml(String(kpiSummary.unavailable_reason || 'unavailable')) + '. No inferred values are shown.</div>';
        }} else {{
          kpiMount.innerHTML = kpis.map(function (kpi) {{
            const currentStatus = String(kpi.current_status || 'unknown');
            const state = String(kpi.state || 'awaiting_data');
            const evidenceButton = brief && kpi.evidence_ref
              ? '<button class="k-btn k-btn-quiet k-btn-sm" type="button" data-desk-kpi-evidence="' + escapeWorkOsHtml(String(kpi.evidence_ref)) + '" data-desk-kpi-name="' + escapeWorkOsHtml(String(kpi.name || 'KPI')) + '">Open exact evidence →</button>'
              : '';
            return '<div class="k-well research-row"><div><strong>' + escapeWorkOsHtml(String(kpi.name || 'KPI')) + '</strong><div class="stat-subtext">Tier 1 · source ' + escapeWorkOsHtml(String(kpi.evidence_ref || kpi.source_hint || 'unavailable')) + ' · as of ' + escapeWorkOsHtml(String(kpi.latest_period || 'date unavailable')) + '</div><div class="stat-number">' + (Number.isFinite(kpi.latest_value) ? escapeWorkOsHtml(String(kpi.latest_value)) + (kpi.unit ? ' ' + escapeWorkOsHtml(String(kpi.unit)) : '') : 'Awaiting data') + '</div></div><div><span class="' + workOsPillClass(currentStatus) + '">' + escapeWorkOsHtml(state.replaceAll('_', ' ').toUpperCase()) + '</span>' + evidenceButton + '</div></div>';
          }}).join('');
          kpiMount.querySelectorAll('[data-desk-kpi-evidence]').forEach(function (button) {{
            button.addEventListener('click', function () {{
              openWorkOsBriefReader(brief, {{
                sectionId: 'thesis',
                factRef: button.getAttribute('data-desk-kpi-evidence')
              }});
            }});
          }});
        }}
      }}
      workOsRenderEarningsDoorway(
        desk.earnings_doorway || null,
        desk.latest_earnings_readout || null,
        normalized
      );
      const conditions = Array.isArray(desk.conditions) ? desk.conditions : [];
      document.getElementById('deskConditions').innerHTML = conditions.length ? conditions.map(function (condition) {{
        const latest = Number.isFinite(condition.latest_value) ? String(condition.latest_value) + ' ' + String(condition.observation_unit || condition.unit || '') + ' · ' + String(condition.observation_period || 'period unavailable') : 'No observed value';
        const prior = Number.isFinite(condition.prior_value)
          ? 'Prior ' + String(condition.prior_value) + ' ' + String(condition.prior_observation_unit || condition.unit || '') + ' · ' + String(condition.prior_observation_period || 'period unavailable') + (Number.isFinite(condition.observation_delta) ? ' · ' + (condition.observation_delta >= 0 ? '+' : '') + String(condition.observation_delta) + ' (' + String(condition.observation_comparison || 'unavailable') + ')' : ' · comparison unavailable')
          : 'No prior observation';
        const detail = condition.status_detail || condition.note || 'Governed decision condition';
        const status = String(condition.status || 'PENDING DATA');
        return '<div class="k-well research-row" data-stable-id="' + escapeWorkOsHtml(condition.stable_id) + '"><div><strong>' + escapeWorkOsHtml(condition.metric) + '</strong><div class="stat-subtext">' + escapeWorkOsHtml(latest) + ' · ' + escapeWorkOsHtml(detail) + '</div><div class="stat-subtext">' + escapeWorkOsHtml(prior) + '</div><div class="stat-subtext">Evidence: ' + escapeWorkOsHtml(condition.evidence_ref || 'unavailable') + '</div></div><div><span class="' + workOsPillClass(status) + '">' + escapeWorkOsHtml(status) + '</span><div class="stat-subtext">' + escapeWorkOsHtml(condition.operator) + ' ' + escapeWorkOsHtml(condition.threshold) + ' ' + escapeWorkOsHtml(condition.unit) + (Number(condition.for_periods) > 1 ? ' · ' + escapeWorkOsHtml(condition.for_periods) + ' periods' : '') + '</div></div></div>';
      }}).join('') : '<div class="k-well">No governed conditions are attached to the current decision.</div>';
      const questions = Array.isArray(desk.open_questions) ? desk.open_questions : [];
      document.getElementById('deskQuestions').innerHTML = questions.length ? questions.map(function (question) {{
        return '<div class="k-well" data-stable-id="' + escapeWorkOsHtml(question.stable_id) + '"><strong>' + escapeWorkOsHtml(question.body) + '</strong><div class="stat-subtext">' + escapeWorkOsHtml(question.origin) + ' · ' + escapeWorkOsHtml(question.approval) + ' · revision ' + escapeWorkOsHtml(question.revision) + '</div></div>';
      }}).join('') : (desk.question_store_status === 'unavailable'
        ? '<div class="k-well" role="alert">Open-question store unavailable.</div>'
        : '<div class="k-well">No open research questions.</div>');
      const warnings = Array.isArray(desk.warnings) ? desk.warnings : [];
      const warningBox = document.getElementById('deskWarnings');
      if (warningBox) {{ warningBox.hidden = !warnings.length; warningBox.textContent = warnings.length ? 'Unavailable: ' + warnings.join(', ') : ''; }}
      if (companyPickerStatus) companyPickerStatus.textContent = identityTicker + ' company desk loaded';
      return true;
    }} catch (error) {{
      if ((error && error.name === 'AbortError') || requestSequence !== workOsCompanyRequestSequence) return false;
      const warningBox = document.getElementById('deskWarnings');
      if (warningBox) {{ warningBox.hidden = false; warningBox.textContent = 'Unable to switch company desks. The prior company remains open.'; }}
      if (companyPickerStatus) companyPickerStatus.textContent = normalized + ' could not be loaded; ' + workOsCurrentCompanyTicker() + ' remains open';
      return false;
    }} finally {{
      if (requestSequence === workOsCompanyRequestSequence) {{
        screen.removeAttribute('aria-busy');
        if (workOsCompanyRequestController === controller) workOsCompanyRequestController = null;
      }}
    }}
  }}

  function workOsBriefFilterCompanies() {{
    const portfolioCompanies = workOsPortfolioHydration && Array.isArray(workOsPortfolioHydration.companies)
      ? workOsPortfolioHydration.companies : [];
    const seenTickers = new Set();
    return portfolioCompanies.concat(workOsResearchCompanies || []).filter(function (item) {{
      const ticker = String(item.ticker || '').toUpperCase();
      if (!ticker || seenTickers.has(ticker)) return false;
      seenTickers.add(ticker);
      return true;
    }}).map(function (item) {{
      return {{ ticker: String(item.ticker).toUpperCase(), name: item.name || item.ticker, coverage_role: item.coverage_role || 'unknown' }};
    }});
  }}

  function workOsPopulateBriefTickerOptions(tickerFilter, selectedRole) {{
    if (!tickerFilter) return;
    const previouslySelected = tickerFilter.value;
    tickerFilter.replaceChildren(new Option('All companies', ''));
    const companies = workOsBriefFilterCompanies();
    const compatibleCompanies = companies.filter(function (company) {{
      return !selectedRole || company.coverage_role === selectedRole;
    }});
    compatibleCompanies.forEach(function (company) {{
      tickerFilter.add(new Option(company.ticker + ' · ' + company.name, company.ticker));
    }});
    const selectedTickerIsCompatible = Array.from(tickerFilter.options).some(function (option) {{
      return option.value === previouslySelected;
    }});
    if (selectedTickerIsCompatible) tickerFilter.value = previouslySelected;
    if (!selectedTickerIsCompatible) tickerFilter.value = '';
  }}

  function workOsClearBriefFilters() {{
    const tickerFilter = document.getElementById('briefTickerFilter');
    const roleFilter = document.getElementById('briefRoleFilter');
    const kindFilter = document.getElementById('briefKindFilter');
    if (roleFilter) roleFilter.value = '';
    if (kindFilter) kindFilter.value = '';
    workOsPopulateBriefTickerOptions(tickerFilter, '');
    workOsRenderBriefLibrary();
  }}

  async function workOsRenderBriefLibrary() {{
    const target = document.getElementById('workOsBriefLibrary');
    if (!target) return;
    await workOsEnsurePortfolioHydration();
    try {{ await workOsEnsureResearchCompanies(); }} catch (error) {{ workOsResearchCompanies = []; }}
    const tickerFilter = document.getElementById('briefTickerFilter');
    const roleFilter = document.getElementById('briefRoleFilter');
    const kindFilter = document.getElementById('briefKindFilter');
    if (tickerFilter) {{
      workOsPopulateBriefTickerOptions(tickerFilter, roleFilter ? roleFilter.value : '');
    }}
    if (tickerFilter && !tickerFilter.dataset.bound) {{
      tickerFilter.addEventListener('change', workOsRenderBriefLibrary);
      tickerFilter.dataset.bound = '1';
    }}
    if (roleFilter && !roleFilter.dataset.bound) {{ roleFilter.addEventListener('change', function () {{
      workOsPopulateBriefTickerOptions(tickerFilter, roleFilter.value);
      workOsRenderBriefLibrary();
    }}); roleFilter.dataset.bound = '1'; }}
    if (kindFilter && !kindFilter.dataset.bound) {{ kindFilter.addEventListener('change', workOsRenderBriefLibrary); kindFilter.dataset.bound = '1'; }}
    const params = new URLSearchParams({{ limit: '100' }});
    if (tickerFilter && tickerFilter.value) params.set('ticker', tickerFilter.value);
    if (roleFilter && roleFilter.value) params.set('coverage_role', roleFilter.value);
    target.setAttribute('aria-busy', 'true');
    target.innerHTML = '<div class="k-well" role="status">Loading persisted research artifacts…</div>';
    try {{
      const response = await fetch('/api/work-os/briefs?' + params.toString(), {{ headers: {{ Accept: 'application/json' }} }});
      if (!response.ok) throw new Error('HTTP ' + response.status);
      const payload = await response.json();
      const items = Array.isArray(payload.items) ? payload.items : [];
      const selectedTicker = tickerFilter ? tickerFilter.value : '';
      const selectedRole = roleFilter ? roleFilter.value : '';
      const selectedKind = kindFilter ? kindFilter.value : '';
      const hydratedReadouts = workOsPortfolioHydration && Array.isArray(workOsPortfolioHydration.earnings_readouts)
        ? workOsPortfolioHydration.earnings_readouts : [];
      const readoutItems = hydratedReadouts.filter(function (readout) {{
        return (!selectedTicker || readout.ticker === selectedTicker)
          && (!selectedRole || readout.coverage_role === selectedRole);
      }}).sort(function (left, right) {{
        const periodOrder = String(right.fiscal_period).localeCompare(String(left.fiscal_period));
        return periodOrder || String(left.ticker).localeCompare(String(right.ticker));
      }});
      const showReadouts = !selectedKind || selectedKind === 'earnings_readout';
      const showBriefs = !selectedKind || selectedKind === 'full_brief';
      const readoutCards = showReadouts ? readoutItems.map(function (readout) {{
        const generated = readout.generated_at ? String(readout.generated_at).slice(0, 10) : 'generation time unavailable';
        return '<article class="k-card k-card-section research-library-card" data-readout-id="' + escapeWorkOsHtml(readout.artifact_id) + '"><div class="research-row"><span class="k-ticker-symbol t-mono">' + escapeWorkOsHtml(readout.ticker) + '</span><span class="k-pill k-pill-ok">available</span></div><div><div class="k-card-meta">earnings readout</div><h3 class="k-card-title">' + escapeWorkOsHtml(readout.ticker + ' ' + readout.period_label + ' earnings readout') + '</h3><div class="k-card-meta">quarter ended ' + escapeWorkOsHtml(readout.fiscal_period) + ' · ' + escapeWorkOsHtml(readout.coverage_role || 'tracked') + ' · generated ' + escapeWorkOsHtml(generated) + '</div></div><button class="k-btn k-btn-primary k-btn-sm" type="button" data-work-os-readout data-peek-url="' + escapeWorkOsHtml(readout.route) + '" data-peek-title="Post-earnings readout — ' + escapeWorkOsHtml(readout.ticker) + '">Read earnings readout &rarr;</button></article>';
      }}).join('') : '';
      const briefCards = showBriefs && items.length ? items.map(function (item) {{
        const statusClass = item.status === 'available' ? 'k-pill k-pill-ok' : 'k-pill k-pill-warn';
        return '<article class="k-card k-card-section research-library-card" data-artifact-id="' + escapeWorkOsHtml(item.artifact_id) + '"><div class="research-row"><span class="k-ticker-symbol t-mono">' + escapeWorkOsHtml(item.ticker) + '</span><span class="' + statusClass + '">' + escapeWorkOsHtml(item.status) + '</span></div><div><div class="k-card-meta">' + escapeWorkOsHtml(item.artifact_kind.replaceAll('_', ' ')) + '</div><h3 class="k-card-title">' + escapeWorkOsHtml(item.title) + '</h3><div class="k-card-meta">' + escapeWorkOsHtml(item.report_date) + ' · ' + escapeWorkOsHtml(item.coverage_role) + ' · ' + escapeWorkOsHtml(item.reader_mode) + '</div></div><button class="k-btn k-btn-primary k-btn-sm" type="button" data-open-artifact="' + escapeWorkOsHtml(item.artifact_id) + '">Read complete brief →</button></article>';
      }}).join('') : '';
      target.innerHTML = readoutCards + briefCards || '<div class="k-well"><p>No persisted research artifacts match these filters.</p><button class="k-btn k-btn-quiet k-btn-sm" type="button" data-clear-brief-filters aria-label="Clear Brief Library filters">Clear filters</button></div>';
      target.querySelectorAll('[data-open-artifact]').forEach(function (button) {{
        const artifact = items.find(function (item) {{ return item.artifact_id === button.dataset.openArtifact; }});
        button.addEventListener('click', function () {{ openWorkOsBriefReader(artifact); }});
      }});
      target.querySelectorAll('[data-clear-brief-filters]').forEach(function (button) {{
        button.addEventListener('click', workOsClearBriefFilters);
      }});
    }} catch (error) {{
      target.innerHTML = '<div class="k-well" role="alert">Brief Library inventory is temporarily unavailable.</div>';
    }} finally {{
      target.removeAttribute('aria-busy');
    }}
  }}

  window.switchCompanyWorkspace = async function (ticker, options) {{
    const requested = workOsNormalizeTicker(ticker) || workOsCurrentCompanyTicker();
    const committed = await workOsRenderCompanyDesk(requested);
    if (!committed) return false;
    workOsWriteCompanyContext(requested, 'company-desk', options);
    window.navigateTo('screen-workspace', {{ fromHistory: true, companyReady: true }});
    return true;
  }};

  function workOsActionEvidence(action) {{
    const actionId = action && typeof action.action_id === 'string' ? action.action_id : '';
    const alertMatch = /^alert:([1-9][0-9]*)$/.exec(actionId);
    if (!alertMatch) {{
      return '<div class="k-card-meta" data-work-os-action-evidence="unbound">Unbound source/evidence · exact pending-alert identity unavailable</div>';
    }}
    const actionType = typeof action.action_type === 'string' ? action.action_type.trim() : '';
    const lifecycleState = action.lifecycle_state === 'pending' ? action.lifecycle_state : '';
    const sourceRef = typeof action.source_ref === 'string' ? action.source_ref.trim() : '';
    const evidenceRef = typeof action.evidence_ref === 'string' ? action.evidence_ref.trim() : '';
    const hasFullIdentity = Boolean(actionType && lifecycleState && sourceRef === actionId && evidenceRef);
    if (!hasFullIdentity) {{
      return '<div class="k-card-meta" data-work-os-action-evidence="partial">Alert evidence doorway · full identity metadata unavailable</div>';
    }}
    const alertId = alertMatch[1];
    return '<div class="k-card-meta" data-work-os-action-evidence="exact">Pending alert · source ' + escapeWorkOsHtml(actionId) + ' · ' + escapeWorkOsHtml(actionType) + ' · ' + escapeWorkOsHtml(lifecycleState) + ' · evidence ' + escapeWorkOsHtml(evidenceRef) + '</div>' + '<button class="k-btn k-btn-quiet k-btn-sm" type="button" data-peek-url="/api/governed-alerts/' + escapeWorkOsHtml(alertId) + '/evidence" data-peek-title="Pending alert evidence — ' + escapeWorkOsHtml(action.ticker) + '">Open alert evidence &rarr;</button>';
  }}

  // The core owns the transition rules.  This small, closed browser map only
  // exposes controls whose action types the core accepts for each alert class.
  const WORK_OS_GOVERNED_ALERT_ACTION_RECIPES = Object.freeze({{
    thesis_drift: Object.freeze(['acknowledge', 'defer', 'complete', 'supersede']),
    default: Object.freeze(['review', 'dismiss'])
  }});
  const workOsGovernedAlertActionKeys = new Map();

  function workOsExactGovernedAlert(action) {{
    const actionId = action && typeof action.action_id === 'string' ? action.action_id : '';
    const alertMatch = /^alert:([1-9][0-9]*)$/.exec(actionId);
    const triggerKind = action && typeof action.action_type === 'string' ? action.action_type.trim() : '';
    const sourceRef = action && typeof action.source_ref === 'string' ? action.source_ref.trim() : '';
    const evidenceRef = action && typeof action.evidence_ref === 'string' ? action.evidence_ref.trim().toLowerCase() : '';
    if (!alertMatch || action.lifecycle_state !== 'pending' || !triggerKind || sourceRef !== actionId || !/^[0-9a-f]{{64}}$/.test(evidenceRef)) return null;
    return {{ alertId: alertMatch[1], actionId: actionId, triggerKind: triggerKind, evidenceRef: evidenceRef }};
  }}

  function workOsGovernedActionControls(action) {{
    const identity = workOsExactGovernedAlert(action);
    if (!identity) return '';
    const recipes = WORK_OS_GOVERNED_ALERT_ACTION_RECIPES[identity.triggerKind] || WORK_OS_GOVERNED_ALERT_ACTION_RECIPES.default;
    const labels = {{ review: 'Mark reviewed', dismiss: 'Dismiss', acknowledge: 'Acknowledge', defer: 'Defer', complete: 'Complete', supersede: 'Supersede' }};
    const buttons = recipes.map(function (actionType) {{
      const tone = actionType === 'dismiss' ? 'k-btn-danger' : 'k-btn-quiet';
      return '<button class="k-btn ' + tone + ' k-btn-sm" type="button" data-governed-alert-action="' + actionType + '" data-governed-alert-id="' + identity.alertId + '" data-governed-alert-evidence="' + escapeWorkOsHtml(identity.evidenceRef) + '" data-governed-alert-trigger="' + escapeWorkOsHtml(identity.triggerKind) + '">' + labels[actionType] + '</button>';
    }}).join('');
    return '<div class="research-actions" data-governed-alert-controls="' + identity.alertId + '">' + buttons + '</div><div class="k-card-meta" role="status" aria-live="polite" data-governed-alert-status="' + identity.alertId + '">Actions are evidence-bound and recorded locally.</div>';
  }}

  function workOsGovernedActionKey(identity, actionType) {{
    const key = identity.alertId + ':' + actionType;
    let value = workOsGovernedAlertActionKeys.get(key);
    if (!value) {{
      value = 'work-os-alert:' + identity.alertId + ':' + actionType + ':' + (window.crypto && typeof window.crypto.randomUUID === 'function' ? window.crypto.randomUUID() : Date.now().toString(36));
      workOsGovernedAlertActionKeys.set(key, value);
    }}
    return value;
  }}

  function workOsGovernedActionFields(actionType) {{
    if (actionType === 'dismiss') {{
      const dismissReason = window.prompt('Reason for dismissal (required):', '');
      return dismissReason && dismissReason.trim() ? {{ dismiss_reason: dismissReason.trim() }} : null;
    }}
    if (actionType === 'acknowledge') {{
      const note = window.prompt('Acknowledgement note (optional):', '');
      return {{ note: note && note.trim() ? note.trim() : null }};
    }}
    if (actionType === 'defer') {{
      const note = window.prompt('Reason for deferral (required):', '');
      if (!note || !note.trim()) return null;
      const until = window.prompt('Defer until (ISO date or date/time, required):', '');
      const parsed = until ? new Date(until) : new Date('');
      if (Number.isNaN(parsed.getTime())) return null;
      return {{ note: note.trim(), defer_until: parsed.toISOString() }};
    }}
    if (actionType === 'complete') {{
      const decisionId = window.prompt('Owner decision ID (positive integer, required):', '');
      if (!/^[1-9][0-9]*$/.test(String(decisionId || ''))) return null;
      return {{ decision_id: Number(decisionId) }};
    }}
    if (actionType === 'supersede') {{
      const replacementEpisodeId = window.prompt('Replacement thesis episode ID (required):', '');
      return replacementEpisodeId && replacementEpisodeId.trim() ? {{ replacement_episode_id: replacementEpisodeId.trim() }} : null;
    }}
    return {{}};
  }}

  async function workOsSubmitGovernedAlertAction(button) {{
    const alertId = String(button.getAttribute('data-governed-alert-id') || '');
    const actionType = String(button.getAttribute('data-governed-alert-action') || '');
    const evidenceRef = String(button.getAttribute('data-governed-alert-evidence') || '').toLowerCase();
    const triggerKind = String(button.getAttribute('data-governed-alert-trigger') || '');
    const recipes = WORK_OS_GOVERNED_ALERT_ACTION_RECIPES[triggerKind] || WORK_OS_GOVERNED_ALERT_ACTION_RECIPES.default;
    if (!/^[1-9][0-9]*$/.test(alertId) || !/^[0-9a-f]{{64}}$/.test(evidenceRef) || !recipes.includes(actionType)) return;
    const fields = workOsGovernedActionFields(actionType);
    if (fields === null) return;
    const controls = button.closest('[data-governed-alert-controls]');
    const status = controls && controls.parentElement ? controls.parentElement.querySelector('[data-governed-alert-status="' + alertId + '"]') : null;
    if (controls) controls.querySelectorAll('button').forEach(function (control) {{ control.disabled = true; }});
    if (status) status.textContent = 'Saving evidence-bound action…';
    const identity = {{ alertId: alertId }};
    const body = Object.assign({{
      idempotency_key: workOsGovernedActionKey(identity, actionType),
      evidence_ref: evidenceRef,
      action_type: actionType,
      occurred_at: new Date().toISOString()
    }}, fields);
    try {{
      const response = await fetch('/api/governed-alerts/' + alertId + '/actions', {{
        method: 'POST', headers: {{ 'Content-Type': 'application/json', Accept: 'application/json' }}, body: JSON.stringify(body)
      }});
      const payload = await response.json().catch(function () {{ return null; }});
      if (!response.ok) {{
        if (status) status.textContent = response.status === 409 ? 'Alert changed or conflicts with an existing action. Refresh evidence before retrying.' : response.status === 503 ? 'Alert action store unavailable; no action was recorded.' : 'Alert action could not be saved.';
        if (controls) controls.querySelectorAll('button').forEach(function (control) {{ control.disabled = false; }});
        return;
      }}
      const result = payload && payload.receipt && payload.receipt.result_state ? String(payload.receipt.result_state) : 'recorded';
      if (status) status.textContent = 'Saved · ' + result + '. Evidence remains available; Open Company returns to the Company Desk.';
    }} catch (error) {{
      if (status) status.textContent = 'Offline or unavailable; no action was recorded.';
      if (controls) controls.querySelectorAll('button').forEach(function (control) {{ control.disabled = false; }});
    }}
  }}

  let workOsPortfolioSort = {{ key: 'company', direction: 'ascending' }};

  function workOsPortfolioSortValue(company, key) {{
    if (key === 'weight') return Number.isFinite(company.current_weight_pct) ? company.current_weight_pct : -Infinity;
    if (key === 'price') return Number.isFinite(company.price) ? company.price : -Infinity;
    if (key === 'status') return String(company.thesis_status || 'status pending').toLowerCase();
    if (key === 'links') {{
      return (company.report_url ? 1 : 0) + (company.earnings_route ? 1 : 0) + 2;
    }}
    return String(company.name || company.ticker || '').toLowerCase();
  }}

  function workOsRenderPortfolioRows(companies) {{
    const rows = document.getElementById('workOsPortfolioRows');
    if (!rows) return;
    const direction = workOsPortfolioSort.direction === 'ascending' ? 1 : -1;
    const ordered = companies.slice().sort(function (left, right) {{
      const leftValue = workOsPortfolioSortValue(left, workOsPortfolioSort.key);
      const rightValue = workOsPortfolioSortValue(right, workOsPortfolioSort.key);
      if (typeof leftValue === 'number' && typeof rightValue === 'number') return direction * (leftValue - rightValue);
      return direction * String(leftValue).localeCompare(String(rightValue));
    }});
    rows.innerHTML = ordered.length ? ordered.map(function (company) {{
      const weight = workOsPortfolioPercent(company.current_weight_pct);
      const status = company.thesis_status || 'status pending';
      const readout = company.latest_earnings_readout || null;
      const statusDetail = company.pending_tier1_alerts
        ? company.pending_tier1_alerts + ' thesis-decisive alert' + (company.pending_tier1_alerts === 1 ? '' : 's')
        : company.pending_alerts
          ? company.pending_alerts + ' pending alert' + (company.pending_alerts === 1 ? '' : 's')
          : company.new_documents
            ? company.new_documents + ' new document' + (company.new_documents === 1 ? '' : 's')
            : 'No current portfolio alert';
      const readoutAction = readout && readout.route
        ? '<button class="k-chip is-active" type="button" data-work-os-readout data-peek-url="' + escapeWorkOsHtml(readout.route) + '" data-peek-title="Post-earnings readout — ' + escapeWorkOsHtml(company.ticker) + '">Earnings</button>'
        : company.earnings_route
          ? '<button class="k-chip" type="button" data-peek-url="' + escapeWorkOsHtml(company.earnings_route) + '" data-peek-title="Earnings research — ' + escapeWorkOsHtml(company.ticker) + '">Earnings</button>'
          : '';
      const briefAction = company.report_url
        ? '<button class="k-chip is-active" type="button" data-work-os-full-brief="' + escapeWorkOsHtml(company.ticker) + '">Brief</button>'
        : '';
      return '<tr data-work-os-ticker="' + escapeWorkOsHtml(company.ticker) + '"><td><div class="k-ticker"><span class="k-ticker-symbol t-mono">' + escapeWorkOsHtml(company.ticker) + '</span><span class="k-ticker-name">' + escapeWorkOsHtml(company.name) + '</span></div></td>' +
        '<td class="num"><span class="k-pill">' + escapeWorkOsHtml(weight) + '</span></td><td class="num t-mono"><div>' + workOsIntegerMoney(company.price) + ' / <strong>' + workOsIntegerMoney(company.fair_value) + '</strong></div><a class="k-card-meta work-os-threshold-link" data-work-os-thresholds="' + escapeWorkOsHtml(company.ticker) + '" href="/advisor/sizing-intents/' + encodeURIComponent(company.ticker) + '">Open buy / hold / trim / sell ladder</a></td>' +
        '<td><span class="' + workOsPillClass(status) + '">' + escapeWorkOsHtml(status) + '</span><div class="k-card-meta">' + escapeWorkOsHtml(statusDetail) + '</div></td><td><div class="research-actions"><button class="k-chip" type="button" data-work-os-ticker="' + escapeWorkOsHtml(company.ticker) + '">Company Desk</button>' + briefAction + readoutAction + '</div></td></tr>';
    }}).join('') : '<tr><td colspan="5"><div class="k-well">No governed portfolio companies are available.</div></td></tr>';
  }}

  function workOsSortPortfolioRows(key) {{
    if (!workOsPortfolioHydration || !Array.isArray(workOsPortfolioHydration.companies)) return;
    workOsPortfolioSort = {{ key: key, direction: workOsPortfolioSort.key === key && workOsPortfolioSort.direction === 'ascending' ? 'descending' : 'ascending' }};
    document.querySelectorAll('[data-work-os-portfolio-sort]').forEach(function (button) {{
      const active = button.getAttribute('data-work-os-portfolio-sort') === key;
      const header = button.closest('th');
      if (header) header.setAttribute('aria-sort', active ? workOsPortfolioSort.direction : 'none');
      const icon = button.querySelector('[aria-hidden="true"]');
      if (icon) icon.textContent = active && workOsPortfolioSort.direction === 'descending' ? '↓' : '↑';
    }});
    const status = document.getElementById('workOsPortfolioSortStatus');
    if (status) status.textContent = key + ' ' + workOsPortfolioSort.direction;
    workOsRenderPortfolioRows(workOsPortfolioHydration.companies);
    workOsBindPortfolioInteractions();
  }}

  function workOsBindPortfolioInteractions() {{
    document.querySelectorAll('[data-work-os-ticker]').forEach(function (node) {{
      if (node.dataset.workOsTickerBound === 'true') return;
      node.dataset.workOsTickerBound = 'true';
      node.addEventListener('click', function (event) {{
        if (node.tagName === 'TR' && event.target instanceof Element && event.target.closest('button')) return;
        switchCompanyWorkspace(node.dataset.workOsTicker);
      }});
    }});
    document.querySelectorAll('[data-governed-alert-action]').forEach(function (node) {{
      if (node.dataset.workOsAlertBound === 'true') return;
      node.dataset.workOsAlertBound = 'true';
      node.addEventListener('click', function (event) {{
        event.preventDefault(); event.stopPropagation(); workOsSubmitGovernedAlertAction(node);
      }});
    }});
    document.querySelectorAll('[data-work-os-full-brief]').forEach(function (node) {{
      if (node.dataset.workOsBriefBound === 'true') return;
      node.dataset.workOsBriefBound = 'true';
      node.addEventListener('click', function (event) {{
        event.stopPropagation(); openFullBriefCanvas(node.dataset.workOsFullBrief);
      }});
    }});
    document.querySelectorAll('[data-work-os-thresholds]').forEach(function (node) {{
      if (node.dataset.workOsThresholdBound === 'true') return;
      node.dataset.workOsThresholdBound = 'true';
      node.addEventListener('click', function (event) {{
        event.preventDefault(); event.stopPropagation(); workOsOpenThresholdReview(node.dataset.workOsThresholds);
      }});
    }});
  }}

  function workOsRenderPortfolio(payload) {{
    workOsPortfolioHydration = payload;
    const companies = Array.isArray(payload.companies) ? payload.companies : [];
    const nav = document.getElementById('workOsPortfolioNav');
    const navDetail = document.getElementById('workOsPortfolioNavDetail');
    const allocation = document.getElementById('workOsPortfolioAllocation');
    if (nav) nav.textContent = workOsIntegerMoney(payload.total_market_value);
    if (navDetail) navDetail.textContent = String(payload.tracker_detail || 'Tracker unavailable · research data only');
    if (allocation) allocation.innerHTML = workOsAllocationRows(payload.allocation);
    const actionHeading = document.getElementById('workOsActionHeading');
    const actionCount = document.getElementById('workOsActionCount');
    if (actionHeading) actionHeading.textContent = 'Actions';
    if (actionCount) actionCount.textContent = String((payload.actions || []).length) + ' open';
    const actionQueue = document.getElementById('workOsActionQueue');
    if (actionQueue) {{
      actionQueue.innerHTML = payload.actions && payload.actions.length ? payload.actions.map(function (action) {{
        return '<article class="k-well work-os-action-card"><div class="work-os-action-row"><div class="work-os-action-copy">' +
          '<span class="k-ticker-symbol t-mono">' + escapeWorkOsHtml(action.ticker) + '</span><div><h3 class="k-card-title k-card-row-title">' + escapeWorkOsHtml(action.headline) + '</h3>' +
          '<div class="k-card-meta">' + escapeWorkOsHtml(action.detail) + '</div>' + workOsActionEvidence(action) + workOsGovernedActionControls(action) + '</div></div>' +
          '<button class="k-btn k-btn-primary k-btn-sm" type="button" data-work-os-ticker="' + escapeWorkOsHtml(action.ticker) + '">Open Company</button></div></article>';
      }}).join('') : '<div class="k-well">No material portfolio-company reviews are waiting.</div>';
    }}
    workOsRenderPortfolioRows(companies);
    workOsBindPortfolioInteractions();
  }}

  async function workOsRenderEvaluationDialogues() {{
    const target = document.getElementById('workOsEvaluationDialogues');
    const count = document.getElementById('workOsEvaluationCount');
    if (!target) return;
    try {{
      const response = await fetch('/api/work-os/evaluation-dialogues?limit=3', {{ headers: {{ Accept: 'application/json' }} }});
      const payload = response.ok ? await response.json() : null;
      if (!payload || !Array.isArray(payload.items)) throw new Error('Invalid evaluation response');
      const items = payload.items;
      if (count) count.textContent = String(items.length) + ' active';
      target.innerHTML = items.length ? items.map(function (item) {{
        const ticker = String(item.ticker || '').toUpperCase();
        const instrument = item.instrument_type === 'etf' ? 'ETF' : item.instrument_type === 'stock' ? 'Stock' : 'Instrument unavailable';
        const linked = item.ask_session_link_state === 'linked';
        const readiness = String(item.workup_readiness || 'unavailable').replaceAll('_', ' ');
        const freshnessClass = item.freshness === 'available'
          ? 'k-pill k-pill-ok'
          : item.freshness === 'unavailable' ? 'k-pill k-pill-bad' : 'k-pill k-pill-warn';
        const noteDetail = Number.isInteger(item.open_note_count) && item.open_note_count > 0
          ? item.open_note_count + ' owner note' + (item.open_note_count === 1 ? '' : 's')
          : 'No owner notes recorded';
        const candidateId = Number.isInteger(item.discovery_candidate_id) && item.discovery_candidate_id > 0 ? String(item.discovery_candidate_id) : '';
        const instrumentValue = item.instrument_type === 'stock' || item.instrument_type === 'etf' ? item.instrument_type : '';
        return '<article class="k-well work-os-evaluation-thread" data-work-os-evaluation-ticker="' + escapeWorkOsHtml(ticker) + '"><div><div class="research-actions"><h3 class="k-card-title k-card-row-title"><span class="k-ticker-symbol t-mono">' + escapeWorkOsHtml(ticker) + '</span> · ' + escapeWorkOsHtml(item.name || ticker) + '</h3><span class="k-chip">' + escapeWorkOsHtml(instrument) + '</span><span class="' + freshnessClass + '">' + escapeWorkOsHtml(readiness) + ' workup</span></div><div class="k-card-meta">' + escapeWorkOsHtml(noteDetail) + (item.latest_note_at ? ' · updated ' + escapeWorkOsHtml(String(item.latest_note_at)) : '') + '</div></div><div class="research-actions"><button class="k-btn k-btn-primary k-btn-sm" type="button" data-work-os-evaluation-dialogue="' + escapeWorkOsHtml(ticker) + '" data-work-os-evaluation-candidate="' + escapeWorkOsHtml(candidateId) + '" data-work-os-evaluation-instrument="' + escapeWorkOsHtml(instrumentValue) + '">' + (linked ? 'Continue dialogue' : 'Start dialogue') + '</button><button class="k-btn k-btn-quiet k-btn-sm" type="button" data-work-os-evaluation-workup="' + escapeWorkOsHtml(ticker) + '">Open workup</button><button class="k-btn k-btn-quiet k-btn-sm" type="button" data-work-os-evaluation-compare="' + escapeWorkOsHtml(ticker) + '">Compare</button></div></article>';
      }}).join('') : '<div class="k-well">No evaluation dialogues are ready to discuss.</div>';
    }} catch (_error) {{
      if (count) count.textContent = 'Unavailable';
      target.innerHTML = '<div class="k-well" role="alert">Evaluation dialogues are temporarily unavailable. No prototype candidates are being shown.</div>';
    }}
  }}

  function workOsOpenEvaluationDialogue(button) {{
    const ticker = workOsNormalizeTicker(button.getAttribute('data-work-os-evaluation-dialogue'));
    if (!ticker || !window.openWorkOsCopilot) return;
    const candidateId = Number(button.getAttribute('data-work-os-evaluation-candidate'));
    const instrument = button.getAttribute('data-work-os-evaluation-instrument');
    window.openWorkOsCopilot({{
      company_ticker: ticker, category: 'research', origin_key: 'work-os:evaluation-dialogue:' + ticker,
      coverage_role_at_creation: 'evaluation', lifecycle_at_creation: 'active',
      evaluation_candidate_id: Number.isInteger(candidateId) && candidateId > 0 ? candidateId : null,
      evaluation_instrument_type: instrument === 'stock' || instrument === 'etf' ? instrument : null
    }});
  }}

  function workOsOpenEvaluationWorkup(ticker) {{
    const safeTicker = workOsNormalizeTicker(ticker);
    if (safeTicker) window.switchCompanyWorkspace(safeTicker);
  }}

  function workOsCompareEvaluation(ticker) {{
    const safeTicker = workOsNormalizeTicker(ticker);
    if (safeTicker) window.switchFactPlayground(safeTicker);
  }}

  document.addEventListener('click', function (event) {{
    const target = event.target instanceof Element ? event.target.closest('[data-work-os-portfolio-sort], [data-work-os-evaluation-dialogue], [data-work-os-evaluation-workup], [data-work-os-evaluation-compare]') : null;
    if (!target) return;
    if (target.hasAttribute('data-work-os-portfolio-sort')) {{ workOsSortPortfolioRows(target.getAttribute('data-work-os-portfolio-sort')); return; }}
    if (target.hasAttribute('data-work-os-evaluation-dialogue')) {{ workOsOpenEvaluationDialogue(target); return; }}
    if (target.hasAttribute('data-work-os-evaluation-workup')) {{ workOsOpenEvaluationWorkup(target.getAttribute('data-work-os-evaluation-workup')); return; }}
    if (target.hasAttribute('data-work-os-evaluation-compare')) workOsCompareEvaluation(target.getAttribute('data-work-os-evaluation-compare'));
  }});

  async function workOsApplyRequestedResearchState() {{
    const context = workOsReadCompanyContext();
    if (context.screen === 'company-desk' && context.ticker) {{
      await window.switchCompanyWorkspace(context.ticker, {{ fromHistory: true }});
    }} else if (context.screen === 'analytics-playground' && context.ticker) {{
      await window.switchFactPlayground(context.ticker, {{ fromHistory: true }});
    }} else if (context.screen === 'brief-library') {{
      window.navigateTo('screen-brief-library', {{ fromHistory: true }});
      workOsRenderBriefLibrary();
    }}
  }}

  async function workOsEnsurePortfolioHydration() {{
    if (workOsPortfolioHydration) return;
    if (!workOsPortfolioLoading) {{
      workOsPortfolioLoading = (async function () {{
        const status = document.getElementById('workOsLiveStatus');
        try {{
          const response = await fetch('/api/work-os/portfolio', {{ headers: {{ Accept: 'application/json' }} }});
          if (!response.ok) throw new Error('HTTP ' + response.status);
          const payload = await response.json();
          if (!payload || !Array.isArray(payload.companies)) throw new Error('Invalid portfolio response');
          workOsRenderPortfolio(payload);
          if (status) status.textContent = String(payload.tracker_detail || 'Tracker unavailable · research data only');
        }} catch (error) {{
          const nav = document.getElementById('workOsPortfolioNav');
          const navDetail = document.getElementById('workOsPortfolioNavDetail');
          if (nav) nav.textContent = '—';
          if (navDetail) navDetail.textContent = 'Tracker unavailable · research data only';
          const queue = document.getElementById('workOsActionQueue');
          if (queue) queue.innerHTML = '<div class="k-well" role="alert">Portfolio companies are temporarily unavailable. No prototype values are being shown.</div>';
          const rows = document.getElementById('workOsPortfolioRows');
          if (rows) rows.innerHTML = '<tr><td colspan="5"><div class="k-well" role="alert">Portfolio company data is temporarily unavailable.</div></td></tr>';
          if (status) status.textContent = 'Tracker unavailable · research data only';
        }}
      }})().finally(function () {{ workOsPortfolioLoading = null; }});
    }}
    await workOsPortfolioLoading;
  }}

  async function workOsHydratePortfolio() {{
    await Promise.all([workOsEnsurePortfolioHydration(), workOsRenderEvaluationDialogues()]);
    workOsApplyRequestedResearchState();
  }}

  function workOsScreenFromHash() {{
    const raw = window.location.hash.replace(/^#/, '').split('?')[0];
    if (!raw) return 'screen-cockpit';
    if (WORK_OS_ENDPOINTS[raw]) return raw;
    return WORK_OS_LEGACY_HASHES[raw] || 'screen-cockpit';
  }}

  function workOsScreenUrl(screenId) {{
    const url = new URL(window.location.href);
    const params = url.searchParams;
    params.delete('screen');
    url.hash = screenId;
    return url.pathname + url.search + url.hash;
  }}

  const deskQuestionCapture = document.getElementById('deskQuestionCapture');
  const workOsManageResearchItems = document.getElementById('workOsManageResearchItems');
  if (workOsManageResearchItems) workOsManageResearchItems.addEventListener('click', function () {{
    window.navigateTo('screen-audit-log');
    window.setTimeout(function () {{
      const researchItems = document.getElementById('csec-research-items');
      if (researchItems) researchItems.scrollIntoView({{ behavior: 'smooth', block: 'start' }});
    }}, 250);
  }});
  if (deskQuestionCapture) deskQuestionCapture.addEventListener('submit', async function (event) {{
    event.preventDefault();
    const input = document.getElementById('deskQuestionInput');
    const status = document.getElementById('deskQuestionCaptureStatus');
    const body = input ? input.value.trim() : '';
    if (!body) return;
    if (status) status.textContent = 'Saving owner question…';
    try {{
      const response = await fetch('/api/notes', {{
        method: 'POST',
        headers: {{ 'Content-Type': 'application/json', Accept: 'application/json' }},
        body: JSON.stringify({{ ticker: workOsCurrentCompanyTicker(), kind: 'question', body: body }})
      }});
      if (!response.ok) throw new Error('HTTP ' + response.status);
      if (input) input.value = '';
      if (status) status.textContent = 'Question saved';
      await workOsRenderCompanyDesk(workOsCurrentCompanyTicker());
    }} catch (error) {{
      if (status) status.textContent = 'Question could not be saved';
    }}
  }});

  function workOsFormatDecisionDate(rawDate) {{
    if (!rawDate) return '';
    const parsed = new Date(rawDate);
    return Number.isNaN(parsed.getTime()) ? String(rawDate) : parsed.toISOString().slice(0, 10);
  }}

  function workOsDecisionMeta(state, emptyLabel) {{
    if (!state) return emptyLabel;
    const source = state.source_lens ? String(state.source_lens).replaceAll('_', ' ') : state.decided_by;
    const revision = workOsFormatDecisionDate(state.revision);
    const asOf = workOsFormatDecisionDate(state.as_of);
    return source + (revision ? ' · revision ' + revision : '') + (asOf ? ' · as of ' + asOf : '');
  }}

  function workOsRenderReaderDecision(decision) {{
    const projection = decision || {{ relationship: 'unavailable' }};
    const owner = projection.owner || null;
    const model = projection.model || null;
    const ownerStateEl = document.getElementById('workOsBriefOwnerState');
    const ownerMetaEl = document.getElementById('workOsBriefOwnerMeta');
    const modelStateEl = document.getElementById('workOsBriefModelState');
    const modelMetaEl = document.getElementById('workOsBriefModelMeta');
    if (ownerStateEl) ownerStateEl.textContent = owner ? String(owner.value).toUpperCase() : '—';
    if (ownerMetaEl) ownerMetaEl.textContent = workOsDecisionMeta(owner, 'No owner decision recorded');
    if (modelStateEl) modelStateEl.textContent = model ? String(model.value).toUpperCase() : '—';
    if (modelMetaEl) modelMetaEl.textContent = workOsDecisionMeta(model, 'No model recommendation recorded');
    const relationship = String(projection.relationship || 'unavailable');
    const freshness = projection.freshness ? String(projection.freshness).replaceAll('_', ' ') : '';
    const relationshipNode = document.getElementById('workOsBriefDecisionRelationship');
    if (relationshipNode) {{
      relationshipNode.textContent = relationship.replaceAll('_', ' ').toUpperCase() + (freshness ? ' · ' + freshness : '');
      relationshipNode.className = 'k-pill';
      relationshipNode.classList.toggle('k-pill-ok', relationship === 'agree');
      relationshipNode.classList.toggle('k-pill-bad', relationship === 'conflict');
      relationshipNode.classList.toggle('k-pill-warn', relationship !== 'agree' && relationship !== 'conflict');
    }}
  }}

  function workOsReaderUnavailable(body, artifact, status) {{
    const reasons = {{
      legacy_standalone: 'This legacy brief has not been migrated to the shared reader body.',
      body_missing: 'The indexed shared reader body is missing.',
      body_checksum_mismatch: 'The persisted reader body failed its integrity check.'
    }};
    const message = reasons[status] || 'The complete reader body is unavailable.';
    body.innerHTML = '<div class="k-well" role="alert">' + escapeWorkOsHtml(message) + ' <a class="k-btn k-btn-primary k-btn-sm" href="' + escapeWorkOsHtml(artifact.standalone_url) + '">Open persisted standalone brief →</a></div>';
  }}

  function workOsHumanizeSection(sectionId) {{
    return String(sectionId || 'section').replace(/^section[-_:]?/i, '').replaceAll('_', ' ').replaceAll('-', ' ');
  }}

  async function workOsRenderFactPlayground() {{
    const mount = document.getElementById('workOsFactPlayground');
    const picker = document.getElementById('workOsFactTicker');
    const endpoint = WORK_OS_ENDPOINTS['screen-analytics-playground'];
    const ticker = workOsCurrentCompanyTicker();
    if (!mount || !endpoint || !ticker) return false;
    if (mount.dataset.loadedEndpoint === endpoint && mount.dataset.loadedTicker === ticker) return true;
    const requestSequence = ++workOsFactPlaygroundRequestSequence;
    if (workOsFactPlaygroundRequestController) workOsFactPlaygroundRequestController.abort();
    const controller = new AbortController();
    workOsFactPlaygroundRequestController = controller;
    workOsFactPlaygroundLoading = (async function () {{
      mount.setAttribute('aria-busy', 'true');
      mount.innerHTML = '<div class="k-well" role="status">Loading governed facts and metrics…</div>';
      try {{
        const companies = await workOsEnsureResearchCompanies();
        if (requestSequence !== workOsFactPlaygroundRequestSequence) return false;
        if (picker) {{
          picker.innerHTML = companies.map(function (company) {{
            const selected = company.ticker === ticker ? ' selected' : '';
            return '<option value="' + escapeWorkOsHtml(company.ticker) + '"' + selected + '>' +
              escapeWorkOsHtml(company.ticker + ' · ' + company.name) + '</option>';
          }}).join('');
        }}
        const response = await fetch(endpoint + '?fragment=work-os&tickers=' + encodeURIComponent(ticker), {{
          signal: controller.signal, headers: {{ Accept: 'text/html' }}
        }});
        if (!response.ok) throw new Error('HTTP ' + response.status);
        const markup = await response.text();
        if (requestSequence !== workOsFactPlaygroundRequestSequence) return false;
        mount.innerHTML = markup;
        if (typeof window.initExplorePanel !== 'function') throw new Error('Explore initializer unavailable');
        window.initExplorePanel();
        mount.dataset.loadedEndpoint = endpoint;
        mount.dataset.loadedTicker = ticker;
        return true;
      }} catch (error) {{
        if ((error && error.name === 'AbortError') || requestSequence !== workOsFactPlaygroundRequestSequence) return false;
        mount.innerHTML = '<div class="k-well" role="alert">Facts &amp; Analytics is temporarily unavailable. No prototype values are being shown.</div>';
        return false;
      }} finally {{
        if (requestSequence === workOsFactPlaygroundRequestSequence) {{
          mount.removeAttribute('aria-busy');
          if (workOsFactPlaygroundRequestController === controller) workOsFactPlaygroundRequestController = null;
          workOsFactPlaygroundLoading = null;
        }}
      }}
    }})();
    return workOsFactPlaygroundLoading;
  }}

  window.switchFactPlayground = async function (ticker, options) {{
    const requested = workOsNormalizeTicker(ticker) || workOsCurrentCompanyTicker();
    if (!workOsWriteCompanyContext(requested, 'analytics-playground', options)) return false;
    window.navigateTo('screen-analytics-playground', {{ fromHistory: true, companyContextReady: true }});
    return workOsRenderFactPlayground();
  }};

  const workOsFactTicker = document.getElementById('workOsFactTicker');
  if (workOsFactTicker) workOsFactTicker.addEventListener('change', function () {{
    if (!workOsFactTicker.value) return;
    window.switchFactPlayground(workOsFactTicker.value);
  }});

  window.navigateTo = function (screenId, options) {{
    const target = WORK_OS_ENDPOINTS[screenId] ? screenId : 'screen-cockpit';
    if (target === 'screen-workspace' && workOsPortfolioHydration && !(options && options.companyReady)) {{
      const ticker = workOsCurrentCompanyTicker();
      if (ticker) {{ window.switchCompanyWorkspace(ticker, {{ fromHistory: !!(options && options.fromHistory) }}); return; }}
    }}
    if (target === 'screen-brief-library') workOsRenderBriefLibrary();
    if (target === 'screen-analytics-playground' && !(options && options.companyContextReady)) {{
      const ticker = workOsCurrentCompanyTicker();
      if (ticker) {{ window.switchFactPlayground(ticker, {{ fromHistory: !!(options && options.fromHistory) }}); return; }}
    }}
    if (target === 'screen-analytics-playground') workOsRenderFactPlayground();
    originalNavigateTo(target);
    workOsRenderCompanyBreadcrumb();
    const persistentMountId = workOsPersistentMountIds[target];
    const persistentMount = persistentMountId ? document.getElementById(persistentMountId) : null;
    if (persistentMount && persistentMount.dataset.loadedEndpoint !== workOsEndpoint(target)) {{
      workOsLoadScreen(target, persistentMount);
    }}
    if (target === 'screen-execution-queue') {{
      const operationsMount = document.getElementById('workOsOperationsMount');
      if (operationsMount && operationsMount.dataset.loadedEndpoint !== workOsEndpoint(target)) {{
        workOsLoadScreen(target, operationsMount);
      }}
    }}
    const currentUrl = window.location.pathname + window.location.search + window.location.hash;
    if (!(options && options.fromHistory) && currentUrl !== workOsScreenUrl(target)) {{
      window.history.pushState({{ screenId: target }}, '', workOsScreenUrl(target));
    }}
  }};

  window.goCounterreadHome = function () {{
    if (briefReaderOverlay) briefReaderOverlay.close();
    if (drillOverlay) drillOverlay.close();
    if (peekOverlay) peekOverlay.close();
    window.navigateTo('screen-cockpit');
  }};

  async function workOsRestoreCompanyContextFromHistory() {{
    const context = workOsReadCompanyContext();
    if (context.screen === 'company-desk' && context.ticker) {{
      return window.switchCompanyWorkspace(context.ticker, {{ fromHistory: true }});
    }}
    if (context.screen === 'analytics-playground' && context.ticker) {{
      return window.switchFactPlayground(context.ticker, {{ fromHistory: true }});
    }}
    window.navigateTo(workOsScreenFromHash(), {{ fromHistory: true }});
    return true;
  }}

  function workOsCloseHistoryTransients() {{
    if (peekOverlay) peekOverlay.close();
    if (drillOverlay) drillOverlay.close();
    if (briefReaderOverlay) briefReaderOverlay.close();
    workOsRestoreHistoryFocus(workOsLastTransientFocusId);
    workOsLastTransientFocusId = null;
  }}

  async function workOsRestoreTransientFromHistory(state) {{
    const route = workOsRouteFromHistoryState(state);
    if (!route) {{
      workOsReplayingHistory = true;
      try {{
        workOsCloseHistoryTransients();
        return await workOsRestoreCompanyContextFromHistory();
      }} finally {{
        workOsReplayingHistory = false;
      }}
    }}
    const transient = state && typeof state === 'object' ? state.workOsTransient : null;
    if (!transient || typeof transient !== 'object') {{
      workOsReplayingHistory = true;
      try {{
        workOsCloseHistoryTransients();
        return await workOsRestoreCompanyContextFromHistory();
      }} finally {{
        workOsReplayingHistory = false;
      }}
    }}
    workOsReplayingHistory = true;
    try {{
      workOsCloseHistoryTransients();
      await workOsRestoreCompanyContextFromHistory();
      workOsLastTransientFocusId = typeof transient.focusId === 'string' ? transient.focusId : null;
      if (route.overlay === 'peek' && typeof transient.route === 'string') {{
        return await workOsOpenPeekRoute(transient.route, transient.title, {{ fromHistory: true }});
      }}
      if (route.overlay === 'risk_drawer' && typeof transient.drawerType === 'string') {{
        return window.openDrillDrawer(transient.drawerType, {{ fromHistory: true }});
      }}
      return true;
    }} finally {{
      workOsReplayingHistory = false;
    }}
  }}

  async function workOsApplyHash(replaceLegacy) {{
    const screenId = workOsScreenFromHash();
    if (replaceLegacy && window.location.hash !== '#' + screenId) {{
      window.history.replaceState({{ screenId }}, '', '#' + screenId);
    }}
    const stateDetail = window.history.state && window.history.state.workOsFullPageDetail;
    const params = new URLSearchParams(window.location.search);
    const route = stateDetail && typeof stateDetail.route === 'string'
      ? stateDetail.route : params.get('work_os_detail');
    if (route && workOsCanonicalDetailRoute(route)) {{
      const origin = workOsDecodeDetailOrigin(stateDetail && stateDetail.origin)
        || workOsDecodeDetailOrigin(params.get('work_os_detail_origin'))
        || {{ surface: 'screen-cockpit', ticker: null, section: null }};
      return workOsOpenPeekFullPage(route, stateDetail && stateDetail.title || params.get('work_os_detail_title') || 'Research detail', {{ fromHistory: true, origin: origin }});
    }}
    if (route) {{
      const fallback = {{ surface: 'screen-cockpit', ticker: null, section: null }};
      window.history.replaceState({{ screenId: fallback.surface }}, '', workOsDetailOriginUrl(fallback));
      window.navigateTo(fallback.surface, {{ fromHistory: true }});
      return false;
    }}
    const briefState = window.history.state && window.history.state.workOsBriefReader;
    const briefTicker = briefState && typeof briefState.ticker === 'string'
      ? briefState.ticker : params.get('work_os_brief');
    if (briefTicker && workOsValidHistoryTicker(briefTicker)) {{
      workOsLastTransientFocusId = briefState && typeof briefState.focusId === 'string'
        ? briefState.focusId : params.get('work_os_focus');
      return window.openWorkOsBriefReader(briefTicker, {{ fromHistory: true }});
    }}
    if (fullPageDetailOverlay) fullPageDetailOverlay.close();
    return workOsRestoreTransientFromHistory(window.history.state);
  }}

  window.addEventListener('hashchange', function () {{ workOsApplyHash(false); }});
  window.addEventListener('popstate', function () {{ workOsApplyHash(false); }});
  workOsApplyHash(true);
  workOsHydratePortfolio();
  document.addEventListener('click', function (event) {{
    const trigger = event.target instanceof Element ? event.target.closest('[data-research-chat]') : null;
    if (!trigger) return;
    const readerScoped = !!(briefReader && briefReader.contains(trigger) && workOsReaderContext);
    const chatTicker = readerScoped ? workOsReaderContext.ticker : workOsCurrentCompanyTicker();
    const originSuffix = readerScoped ? ':artifact:' + workOsReaderContext.artifact_id : '';
    window.openWorkOsCopilot({{
      company_ticker: chatTicker || null,
      category: 'research',
      origin_key: 'work-os:' + String(trigger.getAttribute('data-research-chat') || 'company') + originSuffix,
      coverage_role_at_creation: readerScoped
        ? (workOsReaderContext.coverage_role || 'unknown')
        : ((workOsCompanyByTicker(workOsCurrentCompanyTicker()) || {{}}).coverage_role || 'unknown'),
      lifecycle_at_creation: 'active'
    }});
  }});
  if (workOsLaunchParams.get('copilot') === '1') {{
    window.setTimeout(function () {{
      window.openWorkOsCopilot({{
        company_ticker: workOsLaunchParams.get('ticker') || null,
        category: 'research',
        report_date: workOsLaunchParams.get('report_date') || null,
        origin_key: workOsLaunchParams.get('origin_key') || 'standalone-report',
        coverage_role_at_creation: 'unknown',
        lifecycle_at_creation: 'unknown'
      }});
    }}, 0);
  }}

  function workOsEndpoint(screenId) {{
    const base = WORK_OS_ENDPOINTS[screenId];
    if (!base) return '';
    const ticker = workOsCurrentCompanyTicker();
    if (screenId === 'screen-workspace' && ticker) {{
      return base + '?ticker=' + encodeURIComponent(ticker);
    }}
    return base;
  }}

  function workOsTrustedFragmentEndpoint(endpoint) {{
    try {{
      const url = new URL(endpoint, window.location.href);
      return url.origin === window.location.origin && url.pathname.startsWith('/api/');
    }} catch (_error) {{
      return false;
    }}
  }}

  function workOsMountHtml(target, markup, endpoint) {{
    if (!workOsTrustedFragmentEndpoint(endpoint)) {{
      throw new Error('Untrusted fragment endpoint');
    }}
    target.innerHTML = markup;
    Array.from(target.querySelectorAll('script')).forEach(function (script) {{
      if (script.src) {{
        const scriptUrl = new URL(script.src, window.location.href);
        const trusted = script.hasAttribute('data-work-os-trusted-script') &&
          scriptUrl.origin === window.location.origin;
        if (!trusted) {{
          script.remove();
          throw new Error('Untrusted fragment script source');
        }}
      }}
      const replacement = document.createElement('script');
      Array.from(script.attributes).forEach(function (attribute) {{
        replacement.setAttribute(attribute.name, attribute.value);
      }});
      replacement.textContent = script.textContent;
      script.replaceWith(replacement);
    }});
  }}
  window.workOsMountHtml = workOsMountHtml;

  function workOsLoadError(target, screenId, message) {{
    target.innerHTML = '<div class="k-well k-well-warn" role="alert">' + message + ' ' +
      '<button type="button" class="k-btn k-btn-quiet k-btn-sm" data-work-os-retry>Retry</button></div>';
    target.dataset.workOsScreenId = screenId;
  }}

  function workOsTargetVisible(target) {{
    return target.isConnected !== false &&
      !(target.closest && target.closest('[hidden], [aria-hidden="true"]'));
  }}

  function workOsAbortTarget(target, reason) {{
    if (!target) return;
    const requestState = workOsRequests.get(target);
    if (!requestState) return;
    requestState.abortReason = reason;
    window.clearTimeout(requestState.timeoutId);
    requestState.controller.abort();
    target.removeAttribute('aria-busy');
    workOsRequests.delete(target);
  }}

  async function workOsLoadScreen(screenId, target, endpointOverride) {{
    const endpoint = endpointOverride || workOsEndpoint(screenId);
    if (!endpoint || !workOsTrustedFragmentEndpoint(endpoint) ||
        !target || !workOsTargetVisible(target)) return;
    const prior = workOsRequests.get(target);
    if (prior) {{
      prior.abortReason = 'superseded';
      prior.controller.abort();
      window.clearTimeout(prior.timeoutId);
    }}
    const controller = new AbortController();
    const requestState = {{
      controller: controller,
      generation: ++workOsRequestGeneration,
      abortReason: '',
      timeoutId: 0
    }};
    requestState.timeoutId = window.setTimeout(function () {{
      requestState.abortReason = 'timeout';
      controller.abort();
    }}, WORK_OS_FETCH_TIMEOUT_MS);
    workOsRequests.set(target, requestState);
    target.setAttribute('aria-busy', 'true');
    target.dataset.workOsScreenId = screenId;
    if (endpointOverride) target.dataset.workOsEndpoint = endpoint;
    else delete target.dataset.workOsEndpoint;
    const status = document.getElementById('workOsLiveStatus');
    if (status) status.textContent = 'Loading live ' + screenId.replace('screen-', '') + ' data';
    try {{
      const response = await fetch(endpoint, {{ signal: controller.signal, headers: {{ Accept: 'text/html' }} }});
      if (workOsRequests.get(target) !== requestState || !workOsTargetVisible(target)) return;
      if (!response.ok) throw new Error('HTTP ' + response.status);
      const markup = await response.text();
      if (workOsRequests.get(target) !== requestState || !workOsTargetVisible(target)) return;
      workOsMountHtml(target, markup, endpoint);
      target.dataset.loadedEndpoint = endpoint;
      if (status) status.textContent = 'Live data fetched at ' + new Date().toLocaleTimeString();
    }} catch (error) {{
      if (workOsRequests.get(target) !== requestState ||
          requestState.abortReason === 'superseded' || requestState.abortReason === 'hidden') return;
      const timedOut = requestState.abortReason === 'timeout';
      workOsLoadError(
        target,
        screenId,
        timedOut
          ? 'Live detail timed out. The screen summary remains usable.'
          : 'Live detail is temporarily unavailable. The screen summary remains usable.'
      );
      if (status) status.textContent = timedOut ? 'Live data timed out' : 'Live data could not be loaded';
    }} finally {{
      window.clearTimeout(requestState.timeoutId);
      if (workOsRequests.get(target) === requestState) {{
        target.removeAttribute('aria-busy');
        workOsRequests.delete(target);
      }}
    }}
  }}
  window.workOsLoadScreen = workOsLoadScreen;

  document.addEventListener('click', function (event) {{
    const refresh = event.target && event.target.closest
      ? event.target.closest('[data-work-os-refresh-screen]')
      : null;
    if (refresh) {{
      const screenId = refresh.dataset.workOsRefreshScreen;
      const mountId = workOsPersistentMountIds[screenId];
      const mount = mountId ? document.getElementById(mountId) : null;
      if (mount) workOsLoadScreen(screenId, mount);
      return;
    }}
    const retry = event.target && event.target.closest
      ? event.target.closest('[data-work-os-retry]')
      : null;
    if (!retry) return;
    const target = retry.closest('[data-work-os-screen-id]');
    if (target && target.dataset.workOsScreenId) {{
      workOsLoadScreen(
        target.dataset.workOsScreenId,
        target,
        target.dataset.workOsEndpoint || undefined
      );
    }}
  }});

  function openLiveDetail(screenId) {{
    const endpoint = workOsEndpoint(screenId);
    if (!endpoint) return;
    openDrillDrawer('live-detail');
    const body = document.getElementById('drawerBody');
    const title = document.getElementById('drawerTitle');
    const subtitle = document.getElementById('drawerSubtitle');
    if (title) title.textContent = 'Live system detail';
    if (subtitle) subtitle.textContent = endpoint;
    if (body) {{
      body.innerHTML = '<div class="k-well" role="status">Loading live detail…</div>';
      workOsLoadScreen(screenId, body);
    }}
  }}

  window.workOsOpenRelatedView = function (endpoint, title) {{
    if (!workOsTrustedFragmentEndpoint(endpoint)) return;
    openDrillDrawer('live-detail');
    const body = document.getElementById('drawerBody');
    const heading = document.getElementById('drawerTitle');
    const subtitle = document.getElementById('drawerSubtitle');
    if (heading) heading.textContent = title;
    if (subtitle) subtitle.textContent = 'Related Operations view';
    if (!body) return;
    body.innerHTML = '<div class="k-well" role="status">Loading related view…</div>';
    workOsLoadScreen('related-operations', body, endpoint);
  }};

</script>
"""


def _make_allocation_language_honest(html: str) -> str:
    replacements = {
        "Ratify & Trade": "Review Thresholds",
        "Confirm Add Execution (+0.5% weight)": "Review Add Threshold (+0.5% weight)",
        "Executing item persists state & clears queue": "Completing an item clears it from this session",
        "executed and persisted to DB": "completed for this session",
        "Add +0.5%": "Review Buy Band",
        "Trim -0.5%": "Review Trim Band",
        "Trim Limit": "Review Trim Band",
        "Hold / Rebalance": "Review Hold Band",
        "Execute Trade Order": "Review Thresholds",
        "Draft Rebalance Plan →": "Review Next-Dollar Plan →",
        "Click trade action to launch execution modal": "Review decision thresholds before allocating capital",
        "Trade Order": "Allocation Decision",
        "Trade Execution": "Allocation Decision",
        "Rebalance target allocation order executed": "Allocation threshold decision recorded",
        "showToast('Disconfirming limit settings saved'); markViewModified();": "openDrillDrawer('thresholds');",
        "showToast('View parameters & slider state saved to DB')": "showToast('View parameters updated for this session')",
        "closeDrillDrawer(); showToast('DCF Calibration Priors updated & saved to DB');": "closeDrillDrawer(); openLiveDetail('screen-execution-queue');",
        "closeDrillDrawer(); showToast('LLM Transport Routing updated');": "closeDrillDrawer(); openLiveDetail('screen-execution-queue');",
        "Save DCF Calibration": "Open Live DCF Operations",
        "Save Transport Config": "Open Live Routing Operations",
    }
    for old, new in replacements.items():
        html = html.replace(old, new)
    html = re.sub(
        r'onclick="(?:event\.stopPropagation\(\);\s*)?openTradeModal\([^\"]+\);?"',
        "onclick=\"event.stopPropagation(); openDrillDrawer('thresholds')\"",
        html,
    )
    html = html.replace("openDrillDrawer('rebalance-plan')", "openDrillDrawer('thresholds')")
    html = _TRADE_MODAL_RE.sub("\n\n  <!-- TOAST NOTICE -->", html)
    html = _TRADE_FUNCTIONS_RE.sub("\n\n    function openSheetDCFModel", html)
    html = _REBALANCE_DRAWER_RE.sub(
        """
      } else if (type === 'thresholds') {
        title.innerText = "Buy / Hold / Trim / Sell Thresholds";
        subtitle.innerText = "Existing-position decision bands and governed Next-Dollar Allocation";
        body.innerHTML = `
          <div class="k-well work-os-threshold-note">
            <div class="work-os-threshold-note-title">Decision discipline, not order routing</div>
            <p class="work-os-threshold-note-body">Review the current buy, hold, trim, and sell conditions together with the next-dollar recommendation. This workspace records an allocation decision; it never submits a broker order.</p>
          </div>
          <button class="k-btn k-btn-primary k-btn-sm" onclick="openLiveDetail('screen-performance')">Open Performance &amp; Risk →</button>`;
      } else if (type === 'dcf-priors')""",
        html,
    )
    return _PIPELINE_SIMULATION_RE.sub(
        """

    // Operational jobs are observed through the existing governed backend.
    function runPipelineJob(jobName) {
      openLiveDetail('screen-execution-queue');
    }

    // AUDIT LOG FILTERING""",
        html,
    )


def _add_production_contract(
    html: str, generated_at: datetime, *, db_path: Path | None = None
) -> str:
    html = html.replace("</title>", f"</title>{FAVICON_LINK}", 1)
    html = html.replace("Execution Queue & Operations Hub", "Operations")
    html = html.replace("Operations & Execution Governance Hub", "Operations")
    html = html.replace("Portfolio Performance vs Index Benchmark", "Performance")
    html = html.replace(
        '<span class="nav-text">Execution Queue & Operations</span>',
        '<span class="nav-text">Operations</span>',
        1,
    )
    html = html.replace(
        '<span class="nav-text">Performance vs Index</span>',
        '<span class="nav-text">Performance</span>',
        1,
    )
    html = _COCKPIT_SECTION_RE.sub(_render_portfolio_cockpit_shell() + "\n\n      ", html, count=1)
    html = _PERFORMANCE_SECTION_RE.sub(
        _render_live_screen_shell(
            screen_id="screen-performance",
            mount_id="workOsPerformanceMount",
            layer="Portfolio Intelligence",
            title="Performance & Risk",
            description="Live benchmarking, allocation, posture, and risk evidence",
        )
        + "\n\n      ",
        html,
        count=1,
    )
    html = _ALLOCATION_SECTION_RE.sub("\n\n      ", html, count=1)
    html = _ALLOCATION_NAV_RE.sub("", html, count=1)
    html = _COMPANY_DESK_SECTION_RE.sub(render_company_desk_shell() + "\n\n      ", html, count=1)
    html = _BRIEF_LIBRARY_SECTION_RE.sub(render_brief_library_shell() + "\n\n      ", html, count=1)
    html = _FACT_PLAYGROUND_SECTION_RE.sub(
        render_fact_playground_shell() + "\n\n      ", html, count=1
    )
    html = _FACT_PLAYGROUND_RUNTIME_RE.sub(
        "\n\n    // Governed Facts & Analytics is mounted from /api/panel/explore.\n\n    // EMBEDDED DCF SLIDERS INSIDE REPORT",
        html,
        count=1,
    )
    html = html.replace("      updateFactPlaygroundTable();\n", "", 1)
    html = _AUDIT_SECTION_RE.sub(
        _render_live_screen_shell(
            screen_id="screen-audit-log",
            mount_id="workOsAuditMount",
            layer="Operations & Governance",
            title="Decision Audit Log",
            description="Live decisions, memos, triggers, and the governed research record",
        )
        + "\n\n      ",
        html,
        count=1,
    )
    html = _OPERATIONS_SECTION_RE.sub(render_operations_shell() + "\n      ", html, count=1)
    html = html.replace(
        '<div class="sidebar-cmd" onclick="openWorkOsCopilot()">',
        '<button type="button" class="sidebar-cmd k-btn k-btn-quiet" aria-label="Search or ask" onclick="workOsOpenGlobalCopilot()">',
        1,
    )
    command_end = "</span>\n      </div>\n\n      <!-- LAYER 1: PORTFOLIO INTELLIGENCE -->"
    html = html.replace(
        command_end,
        "</span>\n      </button>\n\n      <!-- LAYER 1: PORTFOLIO INTELLIGENCE -->",
        1,
    )
    html = html.replace(
        '<section id="screen-cockpit" class="screen-view is-active">',
        '<section id="screen-cockpit" class="screen-view is-active" data-mobile-surface="cockpit">',
        1,
    )
    html = html.replace(
        '<section id="screen-workspace" class="screen-view" data-layout="decision-workbench">',
        '<section id="screen-workspace" class="screen-view" data-layout="decision-workbench" '
        'role="region" aria-labelledby="workOsCompanyDeskHeading">',
        1,
    )
    html = html.replace(
        '<div class="k-card-meta" id="companyPickerLabel">Company Desk</div>',
        '<h1 class="k-card-title" id="workOsCompanyDeskHeading"><span id="companyPickerLabel">Company Desk</span></h1>',
        1,
    )
    html = html.replace(
        '<div class="research-grid">',
        '<div class="research-grid k-grid-split-rail-lg">',
        1,
    )
    html = html.replace(
        '<section id="screen-brief-library" class="screen-view" data-layout="report-library">',
        '<section id="screen-brief-library" class="screen-view" data-layout="report-library" '
        'role="region" aria-labelledby="workOsBriefLibraryHeading">',
        1,
    )
    html = html.replace(
        '<h2 class="k-card-title">Brief Library</h2>',
        '<h2 class="k-card-title" id="workOsBriefLibraryHeading">Brief Library</h2>',
        1,
    )
    html = html.replace(
        '<section id="screen-analytics-playground" class="screen-view" data-layout="governed-fact-playground">',
        '<section id="screen-analytics-playground" class="screen-view" data-layout="governed-fact-playground" '
        'role="region" aria-labelledby="workOsFactPlaygroundHeading">',
        1,
    )
    html = html.replace(
        '<h2 class="k-card-title">Fact &amp; Metric Playground</h2>',
        '<h2 class="k-card-title" id="workOsFactPlaygroundHeading">Fact &amp; Metric Playground</h2>',
        1,
    )
    html = html.replace(
        '<aside class="drill-drawer" id="drillDrawer">',
        '<aside class="drill-drawer" id="drillDrawer" role="dialog" aria-modal="true" aria-hidden="true" aria-labelledby="drawerTitle" hidden>',
        1,
    )
    html = html.replace(
        '<button class="k-btn k-btn-quiet k-btn-sm" onclick="closeDrillDrawer()">',
        '<button id="drillDrawerClose" class="k-btn k-btn-quiet k-btn-sm" onclick="closeDrillDrawer()">',
        1,
    )
    html = html.replace(
        '<aside class="drill-drawer" id="peekDrawer"',
        '<aside class="drill-drawer" id="peekDrawer" role="dialog" aria-modal="true" aria-hidden="true" aria-label="Source citation" hidden',
        1,
    )
    peek_start = html.index('id="peekDrawer"')
    peek_close = '<button class="k-btn k-btn-quiet k-btn-sm" onclick="closePeekDrawer()">'
    peek_close_at = html.index(peek_close, peek_start)
    html = (
        html[:peek_close_at]
        + '<button id="workOsPeekOpenFullPage" class="k-btn k-btn-quiet k-btn-sm" type="button" hidden>Open full page</button>'
        + '<button id="peekDrawerClose" class="k-btn k-btn-quiet k-btn-sm" onclick="closePeekDrawer()">'
        + html[peek_close_at + len(peek_close) :]
    )
    html = _NAV_ITEM_RE.sub(_nav_button, html)
    for screen in SCREEN_SPECS:
        needle = f'<section id="{screen.screen_id}"'
        html = html.replace(
            needle,
            f'<section data-live-endpoint="{screen.endpoint}" id="{screen.screen_id}"',
            1,
        )
    runtime = _production_runtime(generated_at)
    copilot = render_work_os_copilot()
    reader = render_brief_reader_shell()
    full_page_detail = _render_full_page_detail_host()
    controls = (
        f'<style id="work-os-controls-css">{palette_css("dark")}{controls_css("dark")}</style>'
    )
    grid_assets = living_grid_head_assets()
    return html.replace(
        "</body>",
        controls
        + "\n"
        + grid_assets
        + "\n"
        + reader
        + "\n"
        + full_page_detail
        + f'\n<script id="work-os-explore-runtime">{EXPLORE_PANEL_JS}</script>\n'
        + runtime
        + "\n"
        + copilot
        + "\n</body>",
        1,
    )


@lru_cache(maxsize=1)
def _prototype_html() -> str:
    return _PROTOTYPE_PATH.read_text(encoding="utf-8")


def render_work_os_shell(
    *, generated_at: datetime | None = None, db_path: Path | None = None
) -> str:
    """Render the exact prototype shell with production-safe behavior."""

    rendered_at = generated_at or datetime.now(UTC)
    html = _make_allocation_language_honest(_prototype_html())
    return _add_production_contract(html, rendered_at, db_path=db_path)
