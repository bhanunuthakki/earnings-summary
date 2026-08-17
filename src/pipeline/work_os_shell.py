"""Production renderer for the eight-screen Equity Work OS.

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

from pipeline.cc_action import CC_ACTION_CSS, CC_ACTION_JS
from pipeline.cc_overlay import CC_OVERLAY_CSS, CC_OVERLAY_JS
from pipeline.explore_panel import EXPLORE_PANEL_JS
from pipeline.operations_panel import render_operations_shell
from pipeline.work_os_copilot import render_work_os_copilot
from pipeline.work_os_research import (
    render_brief_library_shell,
    render_brief_reader_shell,
    render_company_desk_shell,
    render_fact_playground_shell,
)
from ui.controls import controls_css
from ui.tokens import FAVICON_LINK, palette_css


@dataclass(frozen=True, slots=True)
class ScreenSpec:
    """One stable destination in the Work OS information architecture."""

    screen_id: str
    nav_id: str
    label: str
    endpoint: str


SCREEN_SPECS: tuple[ScreenSpec, ...] = (
    ScreenSpec("screen-cockpit", "nav-cockpit", "Portfolio Cockpit", "/api/panel/overview"),
    ScreenSpec(
        "screen-performance",
        "nav-performance",
        "Performance vs Index",
        "/api/panel/portfolio_allocation",
    ),
    ScreenSpec(
        "screen-allocation",
        "nav-allocation",
        "Risk & Allocations",
        "/api/panel/portfolio_health",
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
    "portfolio_health": "screen-allocation",
    "portfolio_risk": "screen-allocation",
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
_PIPELINE_SIMULATION_RE = re.compile(
    r"\n\s*// PIPELINE SIMULATION\n\s*function runPipelineJob\(jobName\) \{.*?"
    r"\n\s*\}\n\n\s*// AUDIT LOG FILTERING",
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
_OPERATIONS_SECTION_RE = re.compile(
    r'<section id="screen-execution-queue".*?</section>\s*'
    r"(?=</div>\s*</main>)",
    re.DOTALL,
)


def _endpoint_map() -> dict[str, str]:
    return {screen.screen_id: screen.endpoint for screen in SCREEN_SPECS}


def _nav_button(match: re.Match[str]) -> str:
    attrs = match.group("attrs").replace('class="', 'class="k-btn k-btn-quiet ', 1)
    return f'<button type="button" {attrs}>{match.group("body")}</button>'


def _production_runtime(generated_at: datetime) -> str:
    endpoint_json = json.dumps(_endpoint_map(), indent=2, sort_keys=False)
    legacy_hash_json = json.dumps(_LEGACY_HASHES, indent=2, sort_keys=False)
    stamp = escape(generated_at.astimezone(UTC).isoformat().replace("+00:00", "Z"))
    return f"""
<style id="work-os-production-css">
  {CC_ACTION_CSS}
  {CC_OVERLAY_CSS}
  html, body {{ min-height: 100dvh; }}
  body {{ padding-bottom: env(safe-area-inset-bottom); }}
  .k-scrim {{ position: fixed; inset: 0; background: var(--scrim); z-index: 250; }}
  .k-scrim[hidden], .drawer-scrim {{ display: none !important; }}
  .work-os-live-status {{ position: absolute; inline-size: var(--bw-thin); block-size: var(--bw-thin); overflow: hidden; clip: rect(0 0 0 0); }}
  .work-os-report-frame {{ width: 100%; min-height: calc(100dvh - var(--header-height) - var(--sp-6)); border: var(--bw-thin) solid var(--border); border-radius: var(--radius-card); background: var(--surface); }}
  .work-os-report-host {{ display: block; min-height: 100%; border: var(--bw-thin) solid var(--border); border-radius: var(--radius-card); background: var(--surface); overflow: hidden; }}
  .work-os-reader {{ position: fixed; inset: 0; z-index: var(--z-modal); display: flex; flex-direction: column; gap: var(--sp-3); min-height: 0; padding: var(--sp-4); background: var(--bg); overflow: hidden; }}
  .work-os-reader[hidden] {{ display: none !important; }}
  .work-os-reader-header {{ position: sticky; inset-block-start: 0; display: grid; grid-template-columns: auto minmax(0, 1fr) auto; align-items: center; gap: var(--sp-3); border-bottom: var(--bw-thin) solid var(--border); padding-bottom: var(--sp-3); background: var(--bg); }}
  .work-os-reader-masthead {{ min-width: 0; text-align: center; }}
  .work-os-reader-actions {{ display: flex; align-items: center; gap: var(--sp-2); }}
  .work-os-reader-decision {{ display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: var(--sp-3); max-inline-size: var(--main-max-width); inline-size: 100%; margin-inline: auto; }}
  .work-os-reader-layout {{ display: grid; grid-template-columns: var(--grid-card-sm) minmax(0, 1fr); gap: var(--sp-4); flex: 1 1 auto; min-height: 0; max-inline-size: var(--main-max-width); inline-size: 100%; margin-inline: auto; overflow: hidden; }}
  .work-os-reader-sections {{ display: flex; flex-direction: column; align-self: start; gap: var(--sp-1); max-block-size: 100%; overflow-y: auto; }}
  .work-os-reader-sections:empty {{ display: none; }}
  .work-os-reader-body {{ flex: 1 1 auto; min-height: 0; overflow: auto; }}
  .work-os-company-desk {{ display: flex; flex-direction: column; gap: var(--sp-3); min-height: 0; }}
  .work-os-company-toolbar {{ display: flex; justify-content: space-between; align-items: center; gap: var(--sp-3); flex-wrap: wrap; }}
  .work-os-company-picker {{ display: flex; align-items: center; gap: var(--sp-2); flex-wrap: wrap; }}
  .company-identity-switcher {{ position: relative; min-width: 0; border-radius: var(--radius); }}
  .company-identity-row {{ display: flex; align-items: center; gap: var(--sp-2); min-block-size: var(--touch-target-size); }}
  .company-picker-trigger {{ opacity: 0; transform: translateX(var(--lift-sm)); transition: opacity var(--transition), transform var(--transition); }}
  .company-identity-switcher:hover .company-picker-trigger,
  .company-identity-switcher:focus-within .company-picker-trigger {{ opacity: 1; transform: translateX(0); }}
  .company-picker-popover {{ position: absolute; inset-block-start: calc(100% + var(--sp-1)); inset-inline-start: 0; z-index: 220; inline-size: var(--grid-card-md); max-inline-size: calc(100vw - var(--sp-6)); box-shadow: var(--shadow-pop); }}
  .company-picker-popover[hidden] {{ display: none !important; }}
  .company-picker-popover input[type="search"] {{ inline-size: 100%; min-block-size: var(--touch-target-size); }}
  .company-picker-list {{ max-block-size: var(--grid-card-sm); overflow-y: auto; }}
  .company-picker-list [role="option"] {{ display: flex; align-items: baseline; justify-content: space-between; gap: var(--sp-3); }}
  .company-picker-list [aria-selected="true"] {{ background: var(--paper); color: var(--accent); }}
  .sidebar-home {{ min-block-size: var(--icon-button-size); }}
  @media (hover: none) {{
    .company-picker-trigger {{ min-block-size: var(--touch-target-size); opacity: 1; transform: none; }}
  }}
  .work-os-action-copy {{ display: flex; align-items: center; gap: var(--sp-3); flex: 1; }}
  .research-screen {{ display: flex; flex-direction: column; gap: var(--sp-3); min-height: 0; }}
  .research-toolbar, .research-panel-head, .research-actions {{ display: flex; justify-content: space-between; align-items: center; gap: var(--sp-3); flex-wrap: wrap; }}
  .research-decision-band {{ display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: var(--sp-3); }}
  .research-grid {{ display: grid; grid-template-columns: minmax(0, 2fr) minmax(0, 1fr); gap: var(--sp-3); align-items: start; }}
  .research-list {{ display: flex; flex-direction: column; gap: var(--sp-2); margin-top: var(--sp-3); }}
  .research-question-capture {{ display: flex; flex-direction: column; gap: var(--sp-2); margin-top: var(--sp-3); }}
  .research-question-capture input {{ flex: 1 1 auto; min-inline-size: var(--grid-card-sm); }}
  .is-cited-location {{ background: color-mix(in srgb, var(--warn) 14%, transparent); }}
  .research-row {{ display: flex; justify-content: space-between; align-items: flex-start; gap: var(--sp-3); }}
  .research-library-grid {{ display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: var(--sp-3); }}
  @media (max-width: 47.5rem) {{
    body {{ display: flex; min-width: 0; }}
    .app-sidebar,
    .app-sidebar.is-collapsed {{
      position: sticky; inset-block-start: 0; z-index: 200;
      width: var(--sidebar-collapsed-width); min-width: var(--sidebar-collapsed-width);
      height: 100dvh; min-height: 0; padding: var(--sp-2);
      overflow-x: hidden; overflow-y: auto;
      border-right: var(--bw-thin) solid var(--border); border-bottom: 0;
    }}
    .app-sidebar > div:first-child {{
      display: flex; flex-direction: column; align-items: stretch;
      gap: var(--sp-1); width: 100%;
    }}
    .sidebar-brand {{ align-items: center; padding: var(--sp-2) 0; margin: 0; }}
    .sidebar-collapse-toggle, .nav-layer-title, .sidebar-cmd-text, .nav-text {{ display: none !important; }}
    .sidebar-home {{
      width: 100%; min-block-size: var(--touch-target-size); min-inline-size: var(--touch-target-size);
      justify-content: center; padding: var(--sp-2);
    }}
    .sidebar-logo {{ display: none; }}
    .sidebar-cmd, .app-sidebar .nav-item, .app-sidebar.is-collapsed .nav-item {{
      flex: 0 0 auto; justify-content: center;
      min-block-size: var(--touch-target-size); min-inline-size: var(--touch-target-size);
      width: 100%; margin: 0; padding: var(--sp-2);
    }}
    .app-sidebar .nav-item::after {{ display: none; }}
    .app-main {{ width: 100%; min-width: 0; padding-bottom: calc(var(--sp-4) + env(safe-area-inset-bottom)); }}
    .app-header {{ padding-inline: var(--sp-3); }}
    .main-content {{ width: 100%; min-width: 0; padding: var(--sp-3); }}
    .screen-view, .k-card {{ min-width: 0; }}
    .matrix-table {{ display: block; max-width: 100%; overflow-x: auto; }}
    .k-action-row {{ flex-wrap: wrap; gap: var(--sp-2); }}
    .screen-view [style*="grid-template-columns"] {{ grid-template-columns: 1fr !important; }}
    .research-decision-band, .research-grid, .research-library-grid, .work-os-reader-decision {{ grid-template-columns: 1fr; }}
    #screen-brief-library .research-toolbar {{ align-items: stretch; }}
    #screen-brief-library .research-actions {{ display: grid; grid-template-columns: auto minmax(0, 1fr); inline-size: 100%; }}
    #screen-brief-library .research-actions .k-select {{ min-width: 0; inline-size: 100%; }}
    .research-actions .k-chip, .research-actions .k-btn, .research-library-card .k-btn {{ min-block-size: var(--touch-target-size); }}
    .work-os-reader {{ padding: var(--sp-3); }}
    .work-os-reader-layout {{ display: block; overflow: auto; }}
    .work-os-reader-sections {{ display: none; }}
    .work-os-reader-body {{ overflow: visible; }}
    .company-picker-trigger {{ min-block-size: var(--touch-target-size); opacity: 1; transform: none; }}
    .drill-drawer {{ width: 100%; max-width: 100%; border-radius: 0; }}
    input, select, textarea {{ font-size: var(--mobile-control-font-size) !important; }}
  }}
  .drill-drawer[hidden] {{ display: none !important; }}
  @media (prefers-reduced-motion: reduce) {{
    *, *::before, *::after {{ animation-duration: 0s !important; transition-duration: 0s !important; scroll-behavior: auto !important; }}
  }}
</style>
<div class="work-os-live-status" id="workOsLiveStatus" aria-live="polite" data-generated-at="{stamp}"></div>
<script id="work-os-action-runtime">{CC_ACTION_JS}</script>
<script id="work-os-overlay-runtime">{CC_OVERLAY_JS}</script>
<script id="work-os-production-runtime">
  const WORK_OS_ENDPOINTS = {endpoint_json};
  const WORK_OS_LEGACY_HASHES = {legacy_hash_json};
  const workOsRequests = new WeakMap();
  let workOsRequestGeneration = 0;
  const WORK_OS_FETCH_TIMEOUT_MS = 15000;
  const originalNavigateTo = window.navigateTo;
  window.workOsActiveTicker = 'NU';
  let workOsPortfolioHydration = null;
  let workOsPortfolioLoading = null;
  let workOsResearchCompanies = null;
  let workOsCompanyRequestSequence = 0;
  let workOsCompanyRequestController = null;
  let workOsPeekRequestSequence = 0;
  let workOsPeekRequestController = null;
  let workOsReaderContext = null;
  let workOsFactPlaygroundLoading = null;
  let companyPickerMatches = [];
  let companyPickerActiveIndex = -1;
  const workOsLaunchParams = new URLSearchParams(window.location.search);
  const workOsRequestedScreen = workOsLaunchParams.get('screen');
  const workOsRequestedTicker = String(workOsLaunchParams.get('ticker') || '').toUpperCase();
  const originalOpenDrillDrawer = window.openDrillDrawer;
  const originalCloseDrillDrawer = window.closeDrillDrawer;
  const originalOpenPeekDrawer = window.openPeekDrawer;
  const originalClosePeekDrawer = window.closePeekDrawer;
  const drillDrawer = document.getElementById('drillDrawer');
  const peekDrawer = document.getElementById('peekDrawer');
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
    }}
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

  window.openDrillDrawer = function (type) {{
    originalOpenDrillDrawer(type);
    const reportTabs = {{ financials: 'financials', saydo: 'saydo', peers: 'comps', falsifier: 'bear' }};
    if (reportTabs[type]) {{
      const ticker = window.workOsActiveTicker || 'NU';
      const title = document.getElementById('drawerTitle');
      const subtitle = document.getElementById('drawerSubtitle');
      const body = document.getElementById('drawerBody');
      if (title) title.textContent = ticker + ' · ' + type;
      if (subtitle) subtitle.textContent = 'Live company brief detail';
      if (body) body.innerHTML = workOsReportFrame(ticker, reportTabs[type], 'work-os-report-frame');
    }}
    if (drillOverlay) drillOverlay.open();
  }};
  window.closeDrillDrawer = function () {{ if (drillOverlay) drillOverlay.close(); }};
  window.openPeekDrawer = function (refKey) {{
    originalOpenPeekDrawer(refKey);
    if (peekOverlay) peekOverlay.open();
  }};
  window.closePeekDrawer = function () {{ if (peekOverlay) peekOverlay.close(); }};

  function workOsReportFrame(ticker, tabId, className) {{
    const safeTicker = encodeURIComponent(String(ticker || 'NU').toUpperCase());
    const safeTab = encodeURIComponent(tabId || 'overview');
    return '<iframe class="' + className + '" src="/reports/' + safeTicker + '#tab=' + safeTab + '" title="' + safeTicker + ' live research brief" loading="lazy"></iframe>';
  }}

  window.closeWorkOsBriefReader = function () {{ if (briefReaderOverlay) briefReaderOverlay.close(); }};
  const briefReaderBack = document.getElementById('workOsBriefReaderBack');
  if (briefReaderBack) briefReaderBack.addEventListener('click', window.closeWorkOsBriefReader);

  function escapeWorkOsHtml(value) {{
    return String(value == null ? '' : value)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }}

  async function workOsLoadBriefArtifact(artifact) {{
    const title = document.getElementById('workOsBriefReaderTitle');
    const body = document.getElementById('workOsBriefReaderBody');
    const meta = document.getElementById('workOsBriefReaderMeta');
    const sections = document.getElementById('workOsBriefReaderSections');
    workOsReaderContext = artifact;
    if (title) title.textContent = artifact.ticker + ' · ' + artifact.title;
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
        payload.sections.forEach(function (section) {{
          if (!section || !section.dom_id || !root.getElementById(section.dom_id)) return;
          const button = document.createElement('button');
          button.type = 'button';
          button.className = 'k-btn k-btn-quiet k-btn-sm';
          button.textContent = section.label || workOsHumanizeSection(section.section_id);
          button.dataset.sectionId = section.section_id;
          button.addEventListener('click', function () {{
            root.getElementById(section.dom_id)?.scrollIntoView({{ behavior: 'smooth', block: 'start' }});
          }});
          sections.appendChild(button);
        }});
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

  window.openWorkOsBriefReader = async function (tickerOrArtifact) {{
    if (tickerOrArtifact && typeof tickerOrArtifact === 'object' && tickerOrArtifact.artifact_id) {{
      await workOsLoadBriefArtifact(tickerOrArtifact);
      return;
    }}
    const requestedTicker = String(tickerOrArtifact || window.workOsActiveTicker || 'NU').toUpperCase();
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
    await workOsLoadBriefArtifact(payload.items[0]);
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

  async function workOsOpenPeekRoute(route, title) {{
    const ref = document.getElementById('peekRefKey');
    const body = document.getElementById('peekProse');
    if (!ref || !body || !peekOverlay) return;
    const requestSequence = ++workOsPeekRequestSequence;
    const parsedRoute = new URL(route, window.location.origin);
    const sourceLocator = parsedRoute.hash ? parsedRoute.hash.slice(1) : '';
    if (workOsPeekRequestController) workOsPeekRequestController.abort();
    const controller = new AbortController();
    workOsPeekRequestController = controller;
    ref.textContent = title || 'Research detail';
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
    if (!route.startsWith('/api/peek/')) return;
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
      if (window.workOsActiveTicker === ticker) {{
        await workOsRenderCompanyDesk(ticker);
      }}
    }} catch (err) {{
      trigger.disabled = false;
      trigger.textContent = originalText;
      const statusEl = document.createElement('span');
      statusEl.className = 'stat-subtext';
      statusEl.style.color = 'var(--bad)';
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
    const normalized = String(ticker || window.workOsActiveTicker || '').toUpperCase();
    const previousTicker = window.workOsActiveTicker;
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
      const response = await fetch('/api/work-os/companies/' + encodeURIComponent(normalized) + '/desk', {{ signal: controller.signal, headers: {{ Accept: 'application/json' }} }});
      if (!response.ok) throw new Error('HTTP ' + response.status);
      const desk = await response.json();
      if (requestSequence !== workOsCompanyRequestSequence) return false;
      const identity = desk.company || {{}};
      const identityTicker = String(identity.ticker || normalized).toUpperCase();
      if (identityTicker !== normalized) throw new Error('Company response mismatch');
      window.workOsActiveTicker = normalized;
      const breadcrumb = document.getElementById('breadcrumb-title');
      if (breadcrumb) breadcrumb.textContent = 'Company Desk (' + (identity.ticker || normalized) + ')';
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
      const position = desk.position || {{}};
      const hasCockpitPosition = Number.isFinite(company.current_weight_pct);
      const weight = Number.isFinite(position.weight_pct) ? position.weight_pct : (hasCockpitPosition ? company.current_weight_pct : null);
      document.getElementById('deskPositionWeight').textContent = Number.isFinite(weight) ? workOsPercent(weight) : 'Weight unavailable';
      document.getElementById('deskPositionSource').textContent = Number.isFinite(weight) ? 'Portfolio Cockpit snapshot' : 'Tracker snapshot unavailable';
      const valuationSource = position.source ? String(position.source).replaceAll('_', ' ') : 'governed DCF snapshot';
      document.getElementById('deskInputPrice').textContent = Number.isFinite(position.price) ? workOsMoney(position.price, position.currency) : '—';
      document.getElementById('deskInputPriceSource').textContent = Number.isFinite(position.price) ? valuationSource + ' · as of ' + (position.price_as_of || 'date unavailable') : 'No governed input price';
      document.getElementById('deskFairValue').textContent = Number.isFinite(position.fair_value) ? workOsMoney(position.fair_value, position.currency) : '—';
      document.getElementById('deskFairValueSource').textContent = Number.isFinite(position.fair_value) ? valuationSource + ' · as of ' + (position.fair_value_as_of || 'date unavailable') : 'No governed fair value';
      const brief = desk.latest_brief || null;
      document.getElementById('deskBriefDate').textContent = brief ? brief.report_date : '—';
      document.getElementById('deskBriefStatus').textContent = brief ? (brief.reader_mode === 'shared_body' ? 'Shared reader ready' : 'Legacy standalone') : 'No indexed artifact';
      const briefButton = document.getElementById('workOsFullBriefButton');
      if (briefButton) {{
        briefButton.disabled = !brief;
        briefButton.onclick = brief ? function () {{ openWorkOsBriefReader(brief); }} : null;
      }}
      workOsRenderEarningsDoorway(
        desk.earnings_doorway || null,
        desk.latest_earnings_readout || null,
        normalized
      );
      const conditions = Array.isArray(desk.conditions) ? desk.conditions : [];
      document.getElementById('deskConditions').innerHTML = conditions.length ? conditions.map(function (condition) {{
        return '<div class="k-well research-row" data-stable-id="' + escapeWorkOsHtml(condition.stable_id) + '"><div><strong>' + escapeWorkOsHtml(condition.metric) + '</strong><div class="stat-subtext">' + escapeWorkOsHtml(condition.note || 'Governed decision condition') + '</div></div><span class="k-chip k-chip-mono">' + escapeWorkOsHtml(condition.operator) + ' ' + escapeWorkOsHtml(condition.threshold) + ' ' + escapeWorkOsHtml(condition.unit) + '</span></div>';
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
      window.workOsActiveTicker = previousTicker;
      const warningBox = document.getElementById('deskWarnings');
      if (warningBox) {{ warningBox.hidden = false; warningBox.textContent = 'Unable to switch company desks. The prior company remains open.'; }}
      if (companyPickerStatus) companyPickerStatus.textContent = normalized + ' could not be loaded; ' + previousTicker + ' remains open';
      return false;
    }} finally {{
      if (requestSequence === workOsCompanyRequestSequence) {{
        screen.removeAttribute('aria-busy');
        if (workOsCompanyRequestController === controller) workOsCompanyRequestController = null;
      }}
    }}
  }}

  async function workOsRenderBriefLibrary() {{
    const target = document.getElementById('workOsBriefLibrary');
    if (!target) return;
    await workOsEnsurePortfolioHydration();
    try {{ await workOsEnsureResearchCompanies(); }} catch (error) {{ workOsResearchCompanies = []; }}
    const tickerFilter = document.getElementById('briefTickerFilter');
    const roleFilter = document.getElementById('briefRoleFilter');
    const kindFilter = document.getElementById('briefKindFilter');
    const portfolioCompanies = workOsPortfolioHydration && Array.isArray(workOsPortfolioHydration.companies) ? workOsPortfolioHydration.companies : [];
    if (tickerFilter && !tickerFilter.dataset.bound) {{
      const seenTickers = new Set();
      portfolioCompanies.concat(workOsResearchCompanies || []).filter(function (item) {{
        if (!item.ticker || seenTickers.has(item.ticker)) return false;
        seenTickers.add(item.ticker); return true;
      }}).forEach(function (item) {{ tickerFilter.add(new Option(item.ticker + ' · ' + item.name, item.ticker)); }});
      tickerFilter.addEventListener('change', workOsRenderBriefLibrary);
      tickerFilter.dataset.bound = '1';
    }}
    if (roleFilter && !roleFilter.dataset.bound) {{ roleFilter.addEventListener('change', workOsRenderBriefLibrary); roleFilter.dataset.bound = '1'; }}
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
        return '<article class="k-card k-card-stack research-library-card" data-readout-id="' + escapeWorkOsHtml(readout.artifact_id) + '"><div class="research-row"><span class="k-ticker-symbol t-mono">' + escapeWorkOsHtml(readout.ticker) + '</span><span class="k-pill k-pill-ok">available</span></div><div><div class="k-card-meta">earnings readout</div><h3 class="k-card-title">' + escapeWorkOsHtml(readout.ticker + ' ' + readout.period_label + ' earnings readout') + '</h3><div class="k-card-meta">quarter ended ' + escapeWorkOsHtml(readout.fiscal_period) + ' · ' + escapeWorkOsHtml(readout.coverage_role || 'tracked') + ' · generated ' + escapeWorkOsHtml(generated) + '</div></div><button class="k-btn k-btn-primary k-btn-sm" type="button" data-work-os-readout data-peek-url="' + escapeWorkOsHtml(readout.route) + '" data-peek-title="Post-earnings readout — ' + escapeWorkOsHtml(readout.ticker) + '">Read earnings readout &rarr;</button></article>';
      }}).join('') : '';
      const briefCards = showBriefs && items.length ? items.map(function (item) {{
        const statusClass = item.status === 'available' ? 'k-pill k-pill-ok' : 'k-pill k-pill-warn';
        return '<article class="k-card k-card-stack research-library-card" data-artifact-id="' + escapeWorkOsHtml(item.artifact_id) + '"><div class="research-row"><span class="k-ticker-symbol t-mono">' + escapeWorkOsHtml(item.ticker) + '</span><span class="' + statusClass + '">' + escapeWorkOsHtml(item.status) + '</span></div><div><div class="k-card-meta">' + escapeWorkOsHtml(item.artifact_kind.replaceAll('_', ' ')) + '</div><h3 class="k-card-title">' + escapeWorkOsHtml(item.title) + '</h3><div class="k-card-meta">' + escapeWorkOsHtml(item.report_date) + ' · ' + escapeWorkOsHtml(item.coverage_role) + ' · ' + escapeWorkOsHtml(item.reader_mode) + '</div></div><button class="k-btn k-btn-primary k-btn-sm" type="button" data-open-artifact="' + escapeWorkOsHtml(item.artifact_id) + '">Read complete brief →</button></article>';
      }}).join('') : '';
      target.innerHTML = readoutCards + briefCards || '<div class="k-well">No persisted research artifacts match these filters.</div>';
      target.querySelectorAll('[data-open-artifact]').forEach(function (button) {{
        const artifact = items.find(function (item) {{ return item.artifact_id === button.dataset.openArtifact; }});
        button.addEventListener('click', function () {{ openWorkOsBriefReader(artifact); }});
      }});
    }} catch (error) {{
      target.innerHTML = '<div class="k-well" role="alert">Brief Library inventory is temporarily unavailable.</div>';
    }} finally {{
      target.removeAttribute('aria-busy');
    }}
  }}

  function workOsCompanyDeskUrl(ticker) {{
    const url = new URL(window.location.href);
    const params = url.searchParams;
    params.set('ticker', ticker);
    params.set('screen', 'company-desk');
    url.hash = 'screen-workspace';
    return url.pathname + url.search + url.hash;
  }}

  window.switchCompanyWorkspace = async function (ticker, options) {{
    const requested = String(ticker || window.workOsActiveTicker || '').toUpperCase();
    const committed = await workOsRenderCompanyDesk(requested);
    if (!committed) return false;
    window.navigateTo('screen-workspace', {{ fromHistory: true, companyReady: true }});
    const breadcrumb = document.getElementById('breadcrumb-title');
    if (breadcrumb) breadcrumb.textContent = 'Company Desk (' + requested + ')';
    if (!(options && options.fromHistory)) {{
      window.history.pushState({{ screenId: 'screen-workspace', ticker: requested }}, '', workOsCompanyDeskUrl(requested));
    }}
    return true;
  }};

  function workOsRenderPortfolio(payload) {{
    workOsPortfolioHydration = payload;
    const companies = payload.companies || [];
    const stats = document.getElementById('workOsPortfolioStats');
    if (stats) {{
      const cards = stats.querySelectorAll('.k-card');
      const labels = ['Portfolio NAV', 'Performance', 'Risk & Factors', 'Portfolio Companies'];
      const values = [workOsMoney(payload.total_market_value), 'Open live view', 'Open live view', String(companies.length)];
      const details = [payload.as_of ? 'As of ' + payload.as_of : 'Tracker offline - research data only', 'Performance vs Index', 'Risk & Allocations', 'Governed portfolio universe'];
      cards.forEach(function (card, index) {{
        const heading = card.querySelector('.stat-heading');
        const number = card.querySelector('.stat-number');
        const detail = card.querySelector('.stat-subtext');
        if (heading) heading.textContent = labels[index];
        if (number) number.textContent = values[index];
        if (detail) detail.textContent = details[index];
      }});
    }}
    const actionHeading = document.getElementById('workOsActionHeading');
    if (actionHeading) actionHeading.textContent = 'Action Queue & Review Pack (' + payload.actions.length + ' Items)';
    const actionQueue = document.getElementById('workOsActionQueue');
    if (actionQueue) {{
      actionQueue.innerHTML = payload.actions.length ? payload.actions.map(function (action) {{
        return '<div class="k-card k-card-dense k-card-interactive"><div class="k-action-row"><div class="work-os-action-copy">' +
          '<span class="k-ticker-symbol t-mono">' + escapeWorkOsHtml(action.ticker) + '</span><div><h3 class="k-card-title k-card-row-title">' + escapeWorkOsHtml(action.headline) + '</h3>' +
          '<div class="k-card-meta">' + escapeWorkOsHtml(action.detail) + '</div></div></div>' +
          '<button class="k-btn k-btn-primary k-btn-sm" type="button" data-work-os-ticker="' + escapeWorkOsHtml(action.ticker) + '">Open Company &rarr;</button></div></div>';
      }}).join('') : '<div class="k-well">No material portfolio-company reviews are waiting.</div>';
    }}
    const rows = document.getElementById('workOsPortfolioRows');
    if (rows) {{
      rows.innerHTML = companies.map(function (company) {{
        const weight = Number.isFinite(company.current_weight_pct) ? workOsPercent(company.current_weight_pct) : 'Weight unavailable';
        const status = company.thesis_status || 'status pending';
        const readout = company.latest_earnings_readout || null;
        const fallbackReadoutAction = company.earnings_route
          ? '<button class="k-chip is-active" type="button" data-peek-url="' + escapeWorkOsHtml(company.earnings_route) + '" data-peek-title="Earnings research — ' + escapeWorkOsHtml(company.ticker) + '">' + escapeWorkOsHtml(company.earnings_label || 'Open earnings research →') + '</button>'
          : '<span class="k-chip">Readout unavailable</span>';
        const readoutAction = readout && readout.route
          ? '<button class="k-chip is-active" type="button" data-work-os-readout data-peek-url="' + escapeWorkOsHtml(readout.route) + '" data-peek-title="Post-earnings readout — ' + escapeWorkOsHtml(company.ticker) + '">' + escapeWorkOsHtml(readout.period_label) + ' readout &rarr;</button>'
          : fallbackReadoutAction;
        const briefAction = company.report_url ? '<button class="k-chip is-active" type="button" data-work-os-full-brief="' + escapeWorkOsHtml(company.ticker) + '">Full Brief Canvas &rarr;</button>' : '<span class="k-chip">Brief pending</span>';
        return '<tr data-work-os-ticker="' + escapeWorkOsHtml(company.ticker) + '"><td><div class="k-ticker"><span class="k-ticker-symbol t-mono">' + escapeWorkOsHtml(company.ticker) + '</span><span class="k-ticker-name">' + escapeWorkOsHtml(company.name) + '</span></div></td>' +
          '<td><span class="k-pill">' + escapeWorkOsHtml(weight) + '</span></td><td class="num t-mono">' + workOsMoney(company.price) + ' / <strong>' + workOsMoney(company.fair_value) + '</strong></td>' +
          '<td><span class="' + workOsPillClass(status) + '">' + escapeWorkOsHtml(status) + '</span></td><td><div class="research-actions">' + readoutAction + briefAction + '</div></td>' +
          '<td class="num"><button class="k-btn k-btn-quiet k-btn-sm" type="button" data-work-os-thresholds="' + escapeWorkOsHtml(company.ticker) + '">Review Thresholds</button></td></tr>';
      }}).join('');
    }}
    document.querySelectorAll('[data-work-os-ticker]').forEach(function (node) {{ node.addEventListener('click', function (event) {{
      if (node.tagName === 'TR' && event.target instanceof Element && event.target.closest('button')) return;
      switchCompanyWorkspace(node.dataset.workOsTicker);
    }}); }});
    document.querySelectorAll('[data-work-os-full-brief]').forEach(function (node) {{ node.addEventListener('click', function (event) {{ event.stopPropagation(); openFullBriefCanvas(node.dataset.workOsFullBrief); }}); }});
    document.querySelectorAll('[data-work-os-thresholds]').forEach(function (node) {{ node.addEventListener('click', function (event) {{
      event.stopPropagation();
      window.switchCompanyWorkspace(node.dataset.workOsThresholds).then(function (committed) {{
        if (committed) openDrillDrawer('thresholds');
      }});
    }}); }});
  }}

  async function workOsApplyRequestedResearchState() {{
    if (workOsRequestedScreen === 'company-desk') {{
      await window.switchCompanyWorkspace(workOsRequestedTicker || window.workOsActiveTicker, {{ fromHistory: true }});
    }} else if (workOsRequestedScreen === 'brief-library') {{
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
          if (status) status.textContent = payload.status === 'ok' ? 'Portfolio companies loaded' : 'Portfolio companies loaded with live weights unavailable';
        }} catch (error) {{
          const stats = document.getElementById('workOsPortfolioStats');
          if (stats) stats.querySelectorAll('.stat-number').forEach(function (node) {{ node.textContent = '-'; }});
          const queue = document.getElementById('workOsActionQueue');
          if (queue) queue.innerHTML = '<div class="k-well" role="alert">Portfolio companies are temporarily unavailable. No prototype values are being shown.</div>';
          const rows = document.getElementById('workOsPortfolioRows');
          if (rows) rows.innerHTML = '<tr><td colspan="6"><div class="k-well" role="alert">Portfolio company data is temporarily unavailable.</div></td></tr>';
          if (status) status.textContent = 'Portfolio companies could not be loaded';
        }}
      }})().finally(function () {{ workOsPortfolioLoading = null; }});
    }}
    await workOsPortfolioLoading;
  }}

  async function workOsHydratePortfolio() {{
    await workOsEnsurePortfolioHydration();
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
        body: JSON.stringify({{ ticker: window.workOsActiveTicker, kind: 'question', body: body }})
      }});
      if (!response.ok) throw new Error('HTTP ' + response.status);
      if (input) input.value = '';
      if (status) status.textContent = 'Question saved';
      await workOsRenderCompanyDesk(window.workOsActiveTicker);
    }} catch (error) {{
      if (status) status.textContent = 'Question could not be saved';
    }}
  }});

  function workOsDecisionMeta(state, emptyLabel) {{
    if (!state) return emptyLabel;
    const source = state.source_lens ? String(state.source_lens).replaceAll('_', ' ') : state.decided_by;
    return source + ' · revision ' + state.revision;
  }}

  function workOsRenderReaderDecision(decision) {{
    const projection = decision || {{ relationship: 'unavailable' }};
    const owner = projection.owner || null;
    const model = projection.model || null;
    document.getElementById('workOsBriefOwnerState').textContent = owner ? String(owner.value).toUpperCase() : '—';
    document.getElementById('workOsBriefOwnerMeta').textContent = workOsDecisionMeta(owner, 'No owner decision recorded');
    document.getElementById('workOsBriefModelState').textContent = model ? String(model.value).toUpperCase() : '—';
    document.getElementById('workOsBriefModelMeta').textContent = workOsDecisionMeta(model, 'No model recommendation recorded');
    const relationship = String(projection.relationship || 'unavailable');
    const freshness = String(projection.freshness || 'unavailable');
    const relationshipNode = document.getElementById('workOsBriefDecisionRelationship');
    relationshipNode.textContent = relationship.replaceAll('_', ' ') + ' · ' + freshness;
    relationshipNode.className = relationship === 'agree' ? 'k-pill k-pill-ok' : (relationship === 'conflict' ? 'k-pill k-pill-bad' : 'k-pill k-pill-warn');
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
    if (!mount || !endpoint || mount.dataset.loadedEndpoint === endpoint) return;
    if (workOsFactPlaygroundLoading) return workOsFactPlaygroundLoading;
    workOsFactPlaygroundLoading = (async function () {{
      mount.setAttribute('aria-busy', 'true');
      mount.innerHTML = '<div class="k-well" role="status">Loading governed facts and metrics…</div>';
      try {{
        const companies = await workOsEnsureResearchCompanies();
        if (picker) {{
          picker.innerHTML = companies.map(function (company) {{
            const selected = company.ticker === window.workOsActiveTicker ? ' selected' : '';
            return '<option value="' + escapeWorkOsHtml(company.ticker) + '"' + selected + '>' +
              escapeWorkOsHtml(company.ticker + ' · ' + company.name) + '</option>';
          }}).join('');
        }}
        const ticker = String(window.workOsActiveTicker || (companies[0] || {{}}).ticker || '').toUpperCase();
        const response = await fetch(endpoint + '?fragment=work-os&tickers=' + encodeURIComponent(ticker), {{
          headers: {{ Accept: 'text/html' }}
        }});
        if (!response.ok) throw new Error('HTTP ' + response.status);
        mount.innerHTML = await response.text();
        if (typeof window.initExplorePanel !== 'function') throw new Error('Explore initializer unavailable');
        window.initExplorePanel();
        mount.dataset.loadedEndpoint = endpoint;
      }} catch (error) {{
        mount.innerHTML = '<div class="k-well" role="alert">Facts &amp; Analytics is temporarily unavailable. No prototype values are being shown.</div>';
      }} finally {{
        mount.removeAttribute('aria-busy');
        workOsFactPlaygroundLoading = null;
      }}
    }})();
    return workOsFactPlaygroundLoading;
  }}

  const workOsFactTicker = document.getElementById('workOsFactTicker');
  if (workOsFactTicker) workOsFactTicker.addEventListener('change', function () {{
    const root = document.getElementById('vx-root');
    if (!root || !workOsFactTicker.value) return;
    root.dispatchEvent(new CustomEvent('work-os-explore-tickers', {{
      detail: {{ tickers: [workOsFactTicker.value] }}
    }}));
  }});

  window.navigateTo = function (screenId, options) {{
    const target = WORK_OS_ENDPOINTS[screenId] ? screenId : 'screen-cockpit';
    if (target === 'screen-workspace' && workOsPortfolioHydration && !(options && options.companyReady)) {{
      const locationParams = new URLSearchParams(window.location.search);
      const locationTicker = locationParams.get('screen') === 'company-desk' ? String(locationParams.get('ticker') || '').toUpperCase() : '';
      workOsRenderCompanyDesk(locationTicker || window.workOsActiveTicker);
    }}
    if (target === 'screen-brief-library') workOsRenderBriefLibrary();
    if (target === 'screen-analytics-playground') workOsRenderFactPlayground();
    originalNavigateTo(target);
    if (target === 'screen-execution-queue') {{
      const operations = document.getElementById(target);
      if (operations && operations.dataset.loadedEndpoint !== workOsEndpoint(target)) {{
        workOsLoadScreen(target, operations);
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

  function workOsApplyHash(replaceLegacy) {{
    const screenId = workOsScreenFromHash();
    window.navigateTo(screenId, {{ fromHistory: true }});
    if (replaceLegacy && window.location.hash !== '#' + screenId) {{
      window.history.replaceState({{ screenId }}, '', '#' + screenId);
    }}
  }}

  window.addEventListener('hashchange', function () {{ workOsApplyHash(false); }});
  window.addEventListener('popstate', function () {{ workOsApplyHash(false); }});
  workOsApplyHash(true);
  workOsHydratePortfolio();
  document.addEventListener('click', function (event) {{
    const trigger = event.target instanceof Element ? event.target.closest('[data-research-chat]') : null;
    if (!trigger) return;
    const readerScoped = !!(briefReader && briefReader.contains(trigger) && workOsReaderContext);
    const chatTicker = readerScoped ? workOsReaderContext.ticker : window.workOsActiveTicker;
    const originSuffix = readerScoped ? ':artifact:' + workOsReaderContext.artifact_id : '';
    window.openWorkOsCopilot({{
      company_ticker: chatTicker || null,
      category: 'research',
      origin_key: 'work-os:' + String(trigger.getAttribute('data-research-chat') || 'company') + originSuffix,
      coverage_role_at_creation: readerScoped
        ? (workOsReaderContext.coverage_role || 'unknown')
        : ((workOsCompanyByTicker(window.workOsActiveTicker) || {{}}).coverage_role || 'unknown'),
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
    if (screenId === 'screen-workspace' && window.workOsActiveTicker) {{
      return base + '?ticker=' + encodeURIComponent(window.workOsActiveTicker);
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
          <div class="k-well">
            <div style="font-weight: 600; font-size: var(--fs-title);">Decision discipline, not order routing</div>
            <p style="font-size: var(--fs-body); color: var(--fg-soft);">Review the current buy, hold, trim, and sell conditions together with the next-dollar recommendation. This workspace records an allocation decision; it never submits a broker order.</p>
          </div>
          <button class="k-btn k-btn-primary k-btn-sm" onclick="openLiveDetail('screen-allocation')">Open live allocation guidance →</button>`;
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
    html = html.replace(
        '<span class="nav-text">Execution Queue & Operations</span>',
        '<span class="nav-text">Operations</span>',
        1,
    )
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
    html = _OPERATIONS_SECTION_RE.sub(render_operations_shell() + "\n      ", html, count=1)
    html = html.replace(
        '<div class="card-grid-stat">',
        '<div class="card-grid-stat" id="workOsPortfolioStats">',
        1,
    )
    html = html.replace(
        "Action Queue & Review Pack (3 Items)",
        '<span id="workOsActionHeading">Action Queue & Review Pack</span>',
        1,
    )
    html = html.replace(
        '<div style="display: flex; flex-direction: column; gap: var(--sp-2);">\n            <!-- Action Card 1 -->',
        '<div id="workOsActionQueue" style="display: flex; flex-direction: column; gap: var(--sp-2);">\n            <!-- Action Card 1 -->',
        1,
    )
    html = html.replace("<tbody>", '<tbody id="workOsPortfolioRows">', 1)
    html = html.replace(
        '<div class="sidebar-cmd" onclick="openWorkOsCopilot()">',
        '<button type="button" class="sidebar-cmd k-btn k-btn-quiet" aria-label="Search or ask" onclick="openWorkOsCopilot()">',
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
    controls = (
        f'<style id="work-os-controls-css">{palette_css("dark")}{controls_css("dark")}</style>'
    )
    return html.replace(
        "</body>",
        controls
        + "\n"
        + reader
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
