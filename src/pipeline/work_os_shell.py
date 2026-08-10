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

from pipeline.cc_overlay import CC_OVERLAY_CSS, CC_OVERLAY_JS
from pipeline.work_os_copilot import render_work_os_copilot
from ui.controls import controls_css


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
        "screen-full-brief",
        "nav-full-brief",
        "Full Research Brief",
        "/api/panel/holding",
    ),
    ScreenSpec(
        "screen-analytics-playground",
        "nav-analytics-playground",
        "Fact & Metric Playground",
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
        "Execution Queue & Operations",
        "/api/panel/provenance",
    ),
)

_LEGACY_HASHES: dict[str, str] = {
    "home": "screen-cockpit",
    "overview": "screen-cockpit",
    "companies": "screen-workspace",
    "holding": "screen-workspace",
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
_COPILOT_FUNCTION_RE = re.compile(
    r"\n\s*function populateCopilotPrompt\(promptText\) \{.*?"
    r"\n\s*// CONTEXTUAL SLIDE-OVER DRAWER",
    re.DOTALL,
)
_COPILOT_DRAWER_RE = re.compile(
    r" else if \(type === 'ask-copilot'\) \{.*?\n\s*\}",
    re.DOTALL,
)
_PIPELINE_SIMULATION_RE = re.compile(
    r"\n\s*// PIPELINE SIMULATION\n\s*function runPipelineJob\(jobName\) \{.*?"
    r"\n\s*\}\n\n\s*// AUDIT LOG FILTERING",
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
  {CC_OVERLAY_CSS}
  html, body {{ min-height: 100dvh; }}
  body {{ padding-bottom: env(safe-area-inset-bottom); }}
  .k-scrim {{ position: fixed; inset: 0; background: var(--scrim); z-index: 250; }}
  .k-scrim[hidden], .drawer-scrim {{ display: none !important; }}
  .work-os-live-status {{ position: absolute; inline-size: var(--bw-thin); block-size: var(--bw-thin); overflow: hidden; clip: rect(0 0 0 0); }}
  .work-os-report-frame, .work-os-brief-frame {{ width: 100%; min-height: calc(100dvh - var(--header-height) - var(--sp-6)); border: var(--bw-thin) solid var(--border); border-radius: var(--radius-card); background: var(--surface); }}
  .work-os-brief-canvas {{ display: flex; flex-direction: column; gap: var(--sp-3); min-height: 0; }}
  .work-os-company-desk {{ display: flex; flex-direction: column; gap: var(--sp-3); min-height: 0; }}
  .work-os-company-toolbar {{ display: flex; justify-content: space-between; align-items: center; gap: var(--sp-3); flex-wrap: wrap; }}
  .work-os-company-picker {{ display: flex; align-items: center; gap: var(--sp-2); flex-wrap: wrap; }}
  .work-os-action-copy {{ display: flex; align-items: center; gap: var(--sp-3); flex: 1; }}
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
    .sidebar-brand button, .nav-layer-title, .sidebar-cmd-text, .nav-text {{ display: none !important; }}
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
    .drill-drawer {{ width: 100%; max-width: 100%; border-radius: 0; }}
    input, select, textarea {{ font-size: var(--mobile-control-font-size) !important; }}
  }}
  .drill-drawer[hidden] {{ display: none !important; }}
  @media (prefers-reduced-motion: reduce) {{
    *, *::before, *::after {{ animation-duration: 0s !important; transition-duration: 0s !important; scroll-behavior: auto !important; }}
  }}
</style>
<div class="work-os-live-status" id="workOsLiveStatus" aria-live="polite" data-generated-at="{stamp}"></div>
<script id="work-os-overlay-runtime">{CC_OVERLAY_JS}</script>
<script id="work-os-production-runtime">
  const WORK_OS_ENDPOINTS = {endpoint_json};
  const WORK_OS_LEGACY_HASHES = {legacy_hash_json};
  const workOsRequests = new Map();
  const originalNavigateTo = window.navigateTo;
  window.workOsActiveTicker = 'NU';
  let workOsPortfolioHydration = null;
  const originalOpenDrillDrawer = window.openDrillDrawer;
  const originalCloseDrillDrawer = window.closeDrillDrawer;
  const originalOpenPeekDrawer = window.openPeekDrawer;
  const originalClosePeekDrawer = window.closePeekDrawer;
  const drillDrawer = document.getElementById('drillDrawer');
  const peekDrawer = document.getElementById('peekDrawer');
  const drillOverlay = drillDrawer && window.CCOverlay.register(drillDrawer, {{
    modal: true, priority: window.CCOverlay.PRIORITY.DRAWER, scrim: true,
    trapFocus: true, restoreFocus: true, motion: 'slide-right',
    group: 'work-os-drawer', closeId: 'drillDrawerClose', wireClose: false,
    onOpen: function () {{ drillDrawer.setAttribute('aria-hidden', 'false'); }},
    onClose: function () {{ drillDrawer.setAttribute('aria-hidden', 'true'); originalCloseDrillDrawer(); }}
  }});
  const peekOverlay = peekDrawer && window.CCOverlay.register(peekDrawer, {{
    modal: true, priority: window.CCOverlay.PRIORITY.PEEK, scrim: true,
    trapFocus: true, restoreFocus: true, motion: 'slide-right',
    group: 'work-os-drawer', closeId: 'peekDrawerClose', wireClose: false,
    onOpen: function () {{ peekDrawer.setAttribute('aria-hidden', 'false'); }},
    onClose: function () {{ peekDrawer.setAttribute('aria-hidden', 'true'); originalClosePeekDrawer(); }}
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

  window.openFullBriefCanvas = function (ticker) {{
    window.workOsActiveTicker = String(ticker || window.workOsActiveTicker || 'NU').toUpperCase();
    const screen = document.getElementById('screen-full-brief');
    if (screen) {{
      screen.innerHTML = '<div class="work-os-brief-canvas">' +
        '<div style="display: flex; justify-content: space-between; align-items: center; gap: var(--sp-2);">' +
        '<button class="k-btn k-btn-quiet k-btn-sm" type="button" onclick="navigateTo(\\'screen-workspace\\')">← Back to Company Desk</button>' +
        '<span class="k-chip k-chip-mono">' + escapeWorkOsHtml(window.workOsActiveTicker) + ' · live brief</span></div>' +
        workOsReportFrame(window.workOsActiveTicker, 'overview', 'work-os-brief-frame') +
        '</div>';
    }}
    window.navigateTo('screen-full-brief');
    const breadcrumb = document.getElementById('breadcrumb-title');
    if (breadcrumb) breadcrumb.textContent = 'Full Equity Research Brief (' + window.workOsActiveTicker + ')';
  }};

  function escapeWorkOsHtml(value) {{
    return String(value == null ? '' : value)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }}

  function workOsMoney(value) {{
    if (!Number.isFinite(value)) return '-';
    return new Intl.NumberFormat('en-US', {{ style: 'currency', currency: 'USD', maximumFractionDigits: value >= 1000 ? 0 : 2 }}).format(value);
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

  function workOsCompanyByTicker(ticker) {{
    const companies = workOsPortfolioHydration && Array.isArray(workOsPortfolioHydration.companies)
      ? workOsPortfolioHydration.companies : [];
    return companies.find(function (company) {{ return company.ticker === ticker; }}) || null;
  }}

  function workOsRenderCompanyDesk(ticker) {{
    const company = workOsCompanyByTicker(String(ticker || '').toUpperCase());
    const screen = document.getElementById('screen-workspace');
    if (!company || !screen) return;
    window.workOsActiveTicker = company.ticker;
    const status = company.thesis_status || 'status pending';
    const brief = company.report_url
      ? workOsReportFrame(company.ticker, 'overview', 'work-os-report-frame')
      : '<div class="k-well" role="status">Research brief pending. The scheduled portfolio pipeline will populate it when governed artifacts are ready.</div>';
    screen.innerHTML = '<div class="work-os-company-desk">' +
      '<div class="k-card work-os-company-toolbar">' +
        '<div class="work-os-company-picker"><label for="companyPickerSelect" class="stat-heading">Portfolio Company</label><select class="k-select" id="companyPickerSelect"></select></div>' +
        '<div class="k-action-row"><span class="' + workOsPillClass(status) + '">' + escapeWorkOsHtml(status) + '</span>' +
        '<button class="k-btn k-btn-quiet k-btn-sm" id="workOsThresholdButton" type="button">Buy / Hold / Trim / Sell Thresholds</button>' +
        (company.report_url ? '<button class="k-btn k-btn-primary k-btn-sm" id="workOsFullBriefButton" type="button">Open Full Brief Canvas &rarr;</button>' : '') + '</div>' +
      '</div>' +
      '<div class="k-card"><div class="k-ticker"><span class="k-ticker-symbol t-mono">' + escapeWorkOsHtml(company.ticker) + '</span><span class="k-ticker-name">' + escapeWorkOsHtml(company.name) + '</span></div>' +
        '<div class="stat-subtext">' + workOsMoney(company.price) + ' price &middot; ' + workOsMoney(company.fair_value) + ' fair value &middot; ' + workOsPercent(company.current_weight_pct) + ' portfolio weight</div></div>' + brief + '</div>';
    const picker = document.getElementById('companyPickerSelect');
    const companies = workOsPortfolioHydration.companies || [];
    if (picker) {{
      companies.forEach(function (item) {{ picker.add(new Option(item.ticker + ' - ' + item.name, item.ticker, false, item.ticker === company.ticker)); }});
      picker.addEventListener('change', function () {{ workOsRenderCompanyDesk(picker.value); }});
    }}
    const thresholdButton = document.getElementById('workOsThresholdButton');
    if (thresholdButton) thresholdButton.addEventListener('click', function () {{ openDrillDrawer('thresholds'); }});
    const briefButton = document.getElementById('workOsFullBriefButton');
    if (briefButton) briefButton.addEventListener('click', function () {{ openFullBriefCanvas(company.ticker); }});
  }}

  window.switchCompanyWorkspace = function (ticker) {{
    const requested = String(ticker || window.workOsActiveTicker || '').toUpperCase();
    if (!workOsCompanyByTicker(requested)) return;
    workOsRenderCompanyDesk(requested);
    window.navigateTo('screen-workspace');
    const breadcrumb = document.getElementById('breadcrumb-title');
    if (breadcrumb) breadcrumb.textContent = 'Company Desk (' + requested + ')';
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
        return '<div class="k-card k-card-interactive"><div class="k-action-row"><div class="work-os-action-copy">' +
          '<span class="k-ticker-symbol t-mono">' + escapeWorkOsHtml(action.ticker) + '</span><div><div class="stat-heading">' + escapeWorkOsHtml(action.headline) + '</div>' +
          '<div class="stat-subtext">' + escapeWorkOsHtml(action.detail) + '</div></div></div>' +
          '<button class="k-btn k-btn-primary k-btn-sm" type="button" data-work-os-ticker="' + escapeWorkOsHtml(action.ticker) + '">Open Company &rarr;</button></div></div>';
      }}).join('') : '<div class="k-well">No material portfolio-company reviews are waiting.</div>';
    }}
    const rows = document.getElementById('workOsPortfolioRows');
    if (rows) {{
      rows.innerHTML = companies.map(function (company) {{
        const weight = Number.isFinite(company.current_weight_pct) ? workOsPercent(company.current_weight_pct) : 'Weight unavailable';
        const status = company.thesis_status || 'status pending';
        const briefAction = company.report_url ? '<button class="k-chip is-active" type="button" data-work-os-full-brief="' + escapeWorkOsHtml(company.ticker) + '">Full Brief Canvas &rarr;</button>' : '<span class="k-chip">Brief pending</span>';
        return '<tr data-work-os-ticker="' + escapeWorkOsHtml(company.ticker) + '"><td><div class="k-ticker"><span class="k-ticker-symbol t-mono">' + escapeWorkOsHtml(company.ticker) + '</span><span class="k-ticker-name">' + escapeWorkOsHtml(company.name) + '</span></div></td>' +
          '<td><span class="k-pill">' + escapeWorkOsHtml(weight) + '</span></td><td class="num t-mono">' + workOsMoney(company.price) + ' / <strong>' + workOsMoney(company.fair_value) + '</strong></td>' +
          '<td><span class="' + workOsPillClass(status) + '">' + escapeWorkOsHtml(status) + '</span></td><td>' + briefAction + '</td>' +
          '<td class="num"><button class="k-btn k-btn-quiet k-btn-sm" type="button" data-work-os-thresholds="' + escapeWorkOsHtml(company.ticker) + '">Review Thresholds</button></td></tr>';
      }}).join('');
    }}
    document.querySelectorAll('[data-work-os-ticker]').forEach(function (node) {{ node.addEventListener('click', function () {{ switchCompanyWorkspace(node.dataset.workOsTicker); }}); }});
    document.querySelectorAll('[data-work-os-full-brief]').forEach(function (node) {{ node.addEventListener('click', function (event) {{ event.stopPropagation(); openFullBriefCanvas(node.dataset.workOsFullBrief); }}); }});
    document.querySelectorAll('[data-work-os-thresholds]').forEach(function (node) {{ node.addEventListener('click', function (event) {{ event.stopPropagation(); window.workOsActiveTicker = node.dataset.workOsThresholds; openDrillDrawer('thresholds'); }}); }});
    const preferred = workOsCompanyByTicker(window.workOsActiveTicker) || companies[0];
    if (preferred) workOsRenderCompanyDesk(preferred.ticker);
  }}

  async function workOsHydratePortfolio() {{
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
  }}

  function workOsScreenFromHash() {{
    const raw = window.location.hash.replace(/^#/, '').split('?')[0];
    if (!raw) return 'screen-cockpit';
    if (WORK_OS_ENDPOINTS[raw]) return raw;
    return WORK_OS_LEGACY_HASHES[raw] || 'screen-cockpit';
  }}

  window.navigateTo = function (screenId, options) {{
    const target = WORK_OS_ENDPOINTS[screenId] ? screenId : 'screen-cockpit';
    originalNavigateTo(target);
    if (!(options && options.fromHistory) && window.location.hash !== '#' + target) {{
      window.history.pushState({{ screenId: target }}, '', '#' + target);
    }}
  }};

  function workOsApplyHash(replaceLegacy) {{
    const screenId = workOsScreenFromHash();
    window.navigateTo(screenId, {{ fromHistory: true }});
    if (replaceLegacy && window.location.hash !== '#' + screenId) {{
      window.history.replaceState({{ screenId }}, '', '#' + screenId);
    }}
  }}

  window.addEventListener('hashchange', function () {{ workOsApplyHash(false); }});
  workOsApplyHash(true);
  workOsHydratePortfolio();
  const workOsLaunchParams = new URLSearchParams(window.location.search);
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
    if ((screenId === 'screen-workspace' || screenId === 'screen-full-brief') && window.workOsActiveTicker) {{
      return base + '?ticker=' + encodeURIComponent(window.workOsActiveTicker);
    }}
    return base;
  }}

  async function workOsLoadScreen(screenId, target) {{
    const endpoint = workOsEndpoint(screenId);
    if (!endpoint || !target) return;
    const prior = workOsRequests.get(screenId);
    if (prior) prior.abort();
    const controller = new AbortController();
    workOsRequests.set(screenId, controller);
    target.setAttribute('aria-busy', 'true');
    const status = document.getElementById('workOsLiveStatus');
    if (status) status.textContent = 'Loading live ' + screenId.replace('screen-', '') + ' data';
    try {{
      const response = await fetch(endpoint, {{ signal: controller.signal, headers: {{ Accept: 'text/html' }} }});
      if (!response.ok) throw new Error('HTTP ' + response.status);
      target.innerHTML = await response.text();
      target.dataset.loadedEndpoint = endpoint;
      if (window.htmx) window.htmx.process(target);
      if (status) status.textContent = 'Live data loaded';
    }} catch (error) {{
      if (error && error.name === 'AbortError') return;
      target.innerHTML = '<div class="k-well" role="alert">Live detail is temporarily unavailable. The screen summary remains usable.</div>';
      if (status) status.textContent = 'Live data could not be loaded';
    }} finally {{
      target.removeAttribute('aria-busy');
      if (workOsRequests.get(screenId) === controller) workOsRequests.delete(screenId);
    }}
  }}

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
    html = _PIPELINE_SIMULATION_RE.sub(
        """

    // Operational jobs are observed through the existing governed backend.
    function runPipelineJob(jobName) {
      openLiveDetail('screen-execution-queue');
    }

    // AUDIT LOG FILTERING""",
        html,
    )
    html = _COPILOT_DRAWER_RE.sub("", html)
    return _COPILOT_FUNCTION_RE.sub(
        "\n\n    // CONTEXTUAL SLIDE-OVER DRAWER",
        html,
    )


def _add_production_contract(html: str, generated_at: datetime) -> str:
    html = html.replace("openDrillDrawer('ask-copilot')", "openWorkOsCopilot()")
    html = html.replace(
        '<div class="card-grid-stat-4col">',
        '<div class="card-grid-stat-4col" id="workOsPortfolioStats">',
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
    controls = f'<style id="work-os-controls-css">{controls_css("dark")}</style>'
    return html.replace("</body>", controls + "\n" + copilot + "\n" + runtime + "\n</body>", 1)


@lru_cache(maxsize=1)
def _prototype_html() -> str:
    return _PROTOTYPE_PATH.read_text(encoding="utf-8")


def render_work_os_shell(*, generated_at: datetime | None = None) -> str:
    """Render the exact prototype shell with production-safe behavior."""

    rendered_at = generated_at or datetime.now(UTC)
    html = _make_allocation_language_honest(_prototype_html())
    return _add_production_contract(html, rendered_at)
