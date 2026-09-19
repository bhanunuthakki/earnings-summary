"""Production renderer for the eight-screen Equity Work OS.

The high-fidelity prototype is intentionally the single markup source of truth.
This module applies the small production-only contract around it: live endpoint
mounts, honest allocation language, accessible transient surfaces, and a
responsive mobile cockpit.  Backend panel endpoints remain available as
drill-through data providers while the old command-center navigation is retired.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
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
from ui.controls import controls_css, controls_js
from ui.living_grid import head_assets as living_grid_head_assets
from ui.tokens import FAVICON_LINK, palette_css

_WORK_OS_RUNTIME_JS = Path(__file__).with_name("work_os_runtime.js").read_text(encoding="utf-8")


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
        "screen-evaluation",
        "nav-evaluation",
        "Evaluation",
        "/api/work-os/evaluation",
    ),
    ScreenSpec(
        "screen-brief-library",
        "nav-brief-library",
        "Brief Library",
        "/api/work-os/briefs",
    ),
    ScreenSpec(
        "screen-analytics-playground",
        "nav-analytics-playground",
        "Explore",
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
    "evaluation": "screen-evaluation",
    "candidates": "screen-evaluation",
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
    r"(?=<!-- =+\s*EVALUATION COVERAGE DESTINATION)",
    re.DOTALL,
)
_EVALUATION_SECTION_RE = re.compile(
    r'<section id="screen-evaluation".*?</section>\s*'
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
_SIDEBAR_COMMAND_RE = re.compile(
    r"\s*<!-- Command Bar Trigger -->\s*<div class=\"sidebar-cmd\"[^>]*>.*?</div>\s*"
    r"(?=<!-- LAYER 1: PORTFOLIO INTELLIGENCE -->)",
    re.S,
)


def _endpoint_map() -> dict[str, str]:
    return {screen.screen_id: screen.endpoint for screen in SCREEN_SPECS}


def _render_portfolio_cockpit_shell() -> str:
    """Return the compact, live-first Portfolio Copilot operating loop."""

    return """
<section id="screen-cockpit" class="screen-view is-active">
  <section class="work-os-portfolio-topline" aria-label="Portfolio NAV and governed actions">
    <article class="k-card k-card-stat work-os-nav-card" data-work-os-stat-key="nav" aria-labelledby="workOsPortfolioNavHeading">
      <div class="work-os-nav-card-body">
        <div class="stat-heading" id="workOsPortfolioNavHeading">Portfolio NAV</div>
        <div class="stat-number" id="workOsPortfolioNav">—</div>
        <div class="stat-subtext" id="workOsPortfolioNavDetail">Loading governed portfolio state</div>
        <div class="work-os-allocation-list" id="workOsPortfolioAllocation" aria-label="Portfolio allocation mix"></div>
      </div>
    </article>
    <article class="k-card k-card-section work-os-actions-rail" aria-labelledby="workOsActionHeading">
      <header class="k-section-head">
        <h2 class="k-section-title k-card-title" id="workOsActionHeading">Actions</h2>
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
    <div class="work-os-evaluation-controls" role="group" aria-label="Evaluation dialogue filters and sorting">
      <div class="work-os-evaluation-chips" role="group" aria-label="Filter evaluation dialogues">
        <button class="k-chip k-chip-btn is-active" type="button" data-work-os-eval-filter="all" aria-pressed="true">All</button>
        <button class="k-chip k-chip-btn" type="button" data-work-os-eval-filter="has_dialogue" aria-pressed="false">Has dialogue</button>
        <button class="k-chip k-chip-btn" type="button" data-work-os-eval-filter="has_notes" aria-pressed="false">Has notes</button>
        <button class="k-chip k-chip-btn" type="button" data-work-os-eval-filter="ready" aria-pressed="false">Ready</button>
      </div>
      <div class="work-os-evaluation-selects">
        <label class="k-field-inline">
          <span class="k-label">Sort</span>
          <select class="k-select" id="workOsEvaluationSort" aria-label="Sort evaluation dialogues">
            <option value="relevance" selected>Relevance</option>
            <option value="ticker_asc">Ticker A&ndash;Z</option>
          </select>
        </label>
        <label class="k-field-inline">
          <span class="k-label">Show</span>
          <select class="k-select" id="workOsEvaluationLimit" aria-label="Number of dialogues to show">
            <option value="3" selected>3</option>
            <option value="5">5</option>
            <option value="10">10</option>
          </select>
        </label>
      </div>
    </div>
    <div class="work-os-evaluation-list" id="workOsEvaluationDialogues">
      <div class="k-well" role="status">Loading bounded evaluation dialogues…</div>
    </div>
  </section>
</section>
""".strip()


def _render_evaluation_shell() -> str:
    """Render the complete mixed company/ETF evaluation destination."""

    return """
<section id="screen-evaluation" class="screen-view" data-layout="research-evaluation" role="region" aria-labelledby="workOsEvaluationSurfaceHeading">
  <header class="research-toolbar k-card k-card-section">
    <div>
      <div class="k-card-meta">Research Engine · Complete evaluation coverage</div>
      <h1 class="k-card-title" id="workOsEvaluationSurfaceHeading">Evaluation</h1>
      <p class="k-card-meta">Scan every company and ETF currently under evaluation, then open the appropriate governed research doorway.</p>
    </div>
    <div class="research-actions">
      <span class="k-pill" id="workOsEvaluationSurfaceCount">Loading</span>
      <button class="k-btn k-btn-quiet k-btn-sm" type="button" data-work-os-refresh-evaluation>Refresh</button>
    </div>
  </header>
  <section class="k-card k-card-section work-os-section" aria-label="Evaluation coverage list">
    <header class="k-section-head">
      <div>
        <div class="k-section-title k-card-title" role="heading" aria-level="2">Coverage</div>
        <div class="k-section-meta">Decision-useful profile, business durability, book impact, valuation, and verified research links. Open a side peek for rationale and evidence.</div>
      </div>
    </header>
    <div class="research-actions" role="group" aria-label="Filter evaluation coverage">
      <button class="k-chip is-active" type="button" data-work-os-evaluation-filter="all" aria-pressed="true">All</button>
      <button class="k-chip" type="button" data-work-os-evaluation-filter="compounders" aria-pressed="false">Compounders</button>
      <button class="k-chip" type="button" data-work-os-evaluation-filter="garp" aria-pressed="false">GARP</button>
      <button class="k-chip" type="button" data-work-os-evaluation-filter="needs_review" aria-pressed="false">Needs review</button>
      <button class="k-chip" type="button" data-work-os-evaluation-filter="etfs" aria-pressed="false">ETFs</button>
    </div>
    <div class="table-scroll">
      <table class="matrix-table">
        <thead>
          <tr>
            <th>Company</th>
            <th>Investment profile</th>
            <th>Business &amp; moat</th>
            <th>Portfolio role</th>
            <th>Valuation</th>
            <th>Research</th>
          </tr>
        </thead>
        <tbody id="workOsEvaluationRows">
          <tr><td colspan="6"><div class="k-well" role="status">Loading complete evaluation coverage…</div></td></tr>
        </tbody>
      </table>
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
{_WORK_OS_RUNTIME_JS}
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
        "Draft Rebalance Plan →": "Review Allocation Thresholds →",
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
        subtitle.innerText = "Existing-position buy, hold, trim, and sell bands";
        body.innerHTML = `
          <div class="k-well work-os-threshold-note">
            <div class="work-os-threshold-note-title">Decision discipline, not order routing</div>
            <p class="work-os-threshold-note-body">Review the current buy, hold, trim, and sell conditions. This workspace records an allocation decision; it never submits a broker order.</p>
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
    html = _SIDEBAR_COMMAND_RE.sub("", html, count=1)
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
    html = _EVALUATION_SECTION_RE.sub(_render_evaluation_shell() + "\n\n      ", html, count=1)
    html = _BRIEF_LIBRARY_SECTION_RE.sub(render_brief_library_shell() + "\n\n      ", html, count=1)
    html = _FACT_PLAYGROUND_SECTION_RE.sub(
        render_fact_playground_shell() + "\n\n      ", html, count=1
    )
    html = _FACT_PLAYGROUND_RUNTIME_RE.sub(
        "\n\n    // Governed Explore is mounted from /api/panel/explore.\n\n    // EMBEDDED DCF SLIDERS INSIDE REPORT",
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
        '<h1 class="k-card-title work-os-explore-title">Explore</h1>',
        '<h1 class="k-card-title work-os-explore-title" id="workOsFactPlaygroundHeading">Explore</h1>',
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
    select_runtime = f"<script data-k-select-runtime>{controls_js()}</script>"
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
        + "\n"
        + select_runtime
        + "\n</body>",
        1,
    )


@lru_cache(maxsize=1)
def _prototype_html() -> str:
    return _PROTOTYPE_PATH.read_text(encoding="utf-8")


# The render is ~30 substitution passes over the lru-cached prototype; the only
# request-varying byte is the `data-generated-at` stamp baked into the
# live-status element. The shell JS never parses that attribute (it only
# rewrites the element's textContent), so finished renders are memoized on a
# 30s bucket — the same freshness window as the server's panel response cache —
# and a served shell may carry a stamp up to one bucket (<=30s) old.
_SHELL_MEMO_BUCKET_SECONDS = 30
_SHELL_MEMO_MAX_ENTRIES = 4
_SHELL_MEMO_LOCK = threading.Lock()
_SHELL_MEMO: dict[tuple[int, str, int], str] = {}


def _shell_memo_key(rendered_at: datetime, *, exact_stamp: bool) -> tuple[int, str, int]:
    """Key every byte-affecting input of one shell render.

    ``rendered_at`` enters the bytes only through the stamp. Implicitly clocked
    renders are keyed by their 30s bucket (the accepted staleness above),
    while an explicitly pinned ``generated_at`` is keyed exactly so contract
    callers (tests, design canaries) always get their own bytes. The prototype
    fingerprint makes a changed mockup — after ``_prototype_html.cache_clear()``
    — a different key instead of a stale memo hit. ``db_path`` is deliberately
    absent: ``_add_production_contract`` never reads it.
    """
    stamp_clock = rendered_at.astimezone(UTC)
    return (
        int(stamp_clock.timestamp()) // _SHELL_MEMO_BUCKET_SECONDS,
        stamp_clock.isoformat() if exact_stamp else "",
        hash(_prototype_html()),
    )


def _shell_memo_etag(key: tuple[int, str, int]) -> str:
    """Derive the response validator from the memo key, not the rendered body."""
    digest = hashlib.sha256("|".join(str(part) for part in key).encode("utf-8")).hexdigest()
    return f'"{digest}"'


@dataclass(frozen=True, slots=True)
class ShellRenderResult:
    """One memoized shell render plus the response headers that describe it."""

    html: str
    etag: str
    cache_state: Literal["hit", "miss"]


def render_work_os_shell_result(
    *, generated_at: datetime | None = None, db_path: Path | None = None
) -> ShellRenderResult:
    """Render the shell together with its memo-key ETag and cache state.

    The ETag is a pure function of the memo key, so two renders that share a
    validator are byte-identical by construction, and a conditional request can
    be answered from the validator without re-running the substitution passes.
    """
    rendered_at = generated_at or datetime.now(UTC)
    key = _shell_memo_key(rendered_at, exact_stamp=generated_at is not None)
    with _SHELL_MEMO_LOCK:
        html = _SHELL_MEMO.get(key)
        if html is None:
            html = _make_allocation_language_honest(_prototype_html())
            html = _add_production_contract(html, rendered_at, db_path=db_path)
            if len(_SHELL_MEMO) >= _SHELL_MEMO_MAX_ENTRIES:
                _SHELL_MEMO.pop(next(iter(_SHELL_MEMO)))
            _SHELL_MEMO[key] = html
            return ShellRenderResult(html, _shell_memo_etag(key), "miss")
        return ShellRenderResult(html, _shell_memo_etag(key), "hit")


def render_work_os_shell(
    *, generated_at: datetime | None = None, db_path: Path | None = None
) -> str:
    """Render the exact prototype shell with production-safe behavior."""
    return render_work_os_shell_result(generated_at=generated_at, db_path=db_path).html


def clear_work_os_shell_render_cache() -> None:
    """Drop memoized renders (test seam around monkeypatched render internals)."""
    with _SHELL_MEMO_LOCK:
        _SHELL_MEMO.clear()
