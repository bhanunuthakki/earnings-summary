"""Shared Work OS research surfaces that are not persistent destinations."""

from __future__ import annotations


def render_brief_reader_shell() -> str:
    """Return the transient reader container mounted once beside the Work OS."""

    return """
<section class="work-os-reader" id="workOsBriefReader" role="dialog" aria-modal="true"
         aria-hidden="true" aria-labelledby="workOsBriefReaderTitle" hidden>
  <header class="work-os-reader-header">
    <div class="work-os-reader-actions">
      <button class="k-btn k-btn-quiet k-btn-sm" id="workOsBriefReaderBack" type="button"
              aria-label="Back to research workspace">Back</button>
    </div>
    <div class="work-os-reader-masthead">
      <div class="k-card-meta">Full Research Brief</div>
      <h2 class="k-card-title" id="workOsBriefReaderTitle">Research brief</h2>
      <div class="k-card-meta" id="workOsBriefReaderMeta">Persisted governed artifact</div>
    </div>
    <div class="work-os-reader-actions">
      <button class="k-btn k-btn-quiet k-btn-sm" type="button" data-research-chat="brief-comments" data-copilot-scope="full-brief">Discuss comments</button>
      <button class="k-btn k-btn-primary k-btn-sm" type="button" data-research-chat="full-brief" data-copilot-scope="full-brief">Ask about this brief</button>
      <button class="k-btn k-btn-quiet k-btn-sm" id="workOsBriefReaderClose" type="button"
              aria-label="Close full research brief">Close</button>
    </div>
  </header>
  <div class="work-os-reader-decision k-card k-card-stat" id="workOsBriefReaderDecision" aria-label="Decision state" aria-live="polite">
    <div class="k-stat-cell"><div class="stat-heading">Owner posture</div><div class="stat-number" id="workOsBriefOwnerState">—</div><div class="stat-subtext" id="workOsBriefOwnerMeta">No owner decision recorded</div></div>
    <div class="k-stat-cell"><div class="stat-heading">Model recommendation</div><div class="stat-number" id="workOsBriefModelState">—</div><div class="stat-subtext" id="workOsBriefModelMeta">No model recommendation recorded</div></div>
    <div class="k-stat-cell"><div class="stat-heading">Relationship</div><div class="k-pill k-pill-warn" id="workOsBriefDecisionRelationship">Unavailable</div><div class="stat-subtext">Thesis verdict remains separately labeled in the brief</div></div>
  </div>
  <div id="workOsBriefResearchItemsMount" aria-live="polite"></div>
  <div class="work-os-reader-layout">
    <nav class="work-os-reader-sections k-card k-card-nav" id="workOsBriefReaderSections"
         aria-label="Brief sections"></nav>
    <div class="work-os-reader-body" id="workOsBriefReaderBody" role="region"
         aria-live="polite"></div>
  </div>
</section>
""".strip()


def render_company_desk_shell() -> str:
    """Return the approved Company Desk as one decision-first page."""

    return """
<section id="screen-workspace" class="screen-view" data-layout="company-desk-approved" role="region" aria-labelledby="workOsCompanyDeskHeading">
  <div class="research-screen company-desk-approved-grid" id="workOsCompanyDesk" aria-live="polite">
    <header class="k-card k-card-section company-desk-topline" data-testid="company-topline">
      <div class="company-identity-switcher" id="companyPickerRoot">
        <h1 class="k-card-title" id="workOsCompanyDeskHeading"><span id="companyPickerLabel">Company Desk</span></h1>
        <div class="company-identity-row">
          <div class="k-tick">
            <span class="k-tick-sym k-tick-sym-display" id="deskTicker">—</span>
            <span class="k-tick-name" id="deskCompanyName">Choose a portfolio company</span>
            <span class="k-card-meta" id="deskCoverageRole">unknown coverage</span>
          </div>
          <button class="company-picker-trigger k-btn k-btn-quiet k-btn-sm" id="companyPickerTrigger"
                  type="button" aria-haspopup="listbox" aria-controls="companyPickerPopover"
                  aria-expanded="false" aria-label="Switch company desk">Switch</button>
        </div>
        <div class="company-picker-popover k-overlay k-card-stack" id="companyPickerPopover" hidden>
          <label class="k-card-meta" for="companyPickerSearch">Find a company</label>
          <input id="companyPickerSearch" type="search" role="combobox" aria-expanded="false"
                 aria-autocomplete="list" aria-controls="companyPickerList" autocomplete="off"
                 placeholder="Search ticker or company" spellcheck="false">
          <ul class="k-menu company-picker-list" id="companyPickerList" role="listbox"></ul>
          <span class="work-os-live-status" id="companyPickerStatus" role="status" aria-live="polite"></span>
        </div>
      </div>
      <div class="company-desk-actions">
        <button class="k-btn k-btn-quiet k-btn-sm" id="workOsAskCompany" type="button"
                data-research-chat="company" data-copilot-scope="company" disabled>Ask about company</button>
        <a class="k-btn k-btn-quiet k-btn-sm" id="workOsDcfLink" aria-disabled="true" tabindex="-1">DCF model ↗</a>
        <span id="workOsEarningsDoorway"><span class="k-card-meta">Earnings artifact unavailable</span></span>
        <button class="k-btn k-btn-primary k-btn-sm" id="workOsFullBriefButton" type="button" disabled>Read full brief →</button>
      </div>
      <div class="company-desk-facts" aria-label="Company snapshot">
        <div class="k-stat-cell"><div class="stat-heading">Last price</div><div class="stat-number" id="deskLivePrice">—</div></div>
        <div class="k-stat-cell"><div class="stat-heading">Fair value</div><div class="stat-number" id="deskFairValue">—</div></div>
        <div class="k-stat-cell"><div class="stat-heading">Upside</div><div class="stat-number" id="deskValuationGap"><span class="k-pill">—</span></div></div>
        <div class="k-stat-cell"><div class="stat-heading">Latest quarter</div><div class="stat-number" id="deskQuarterLabel">Pending</div></div>
      </div>
    </header>

    <article class="k-card k-card-section company-desk-decision" data-testid="decision-card">
      <header class="k-card-head"><div class="k-card-heading"><div class="k-card-meta">Decision &amp; tracking</div><h2 class="k-card-title">One owner view</h2></div></header>
      <div class="company-desk-decision-grid">
        <div><div class="stat-heading">Current action</div><div class="stat-number" id="deskOwnerState">Unavailable</div><div class="stat-subtext" id="deskOwnerRevision">No owner decision recorded</div></div>
        <div><div class="stat-heading">Thesis status</div><div class="k-pill k-pill-warn" id="deskThesisStatus">Unavailable</div><div class="stat-subtext" id="deskThesisAsOf">No current evaluated thesis state</div></div>
      </div>
      <div class="company-desk-tracking-grid" id="deskTrackingBands" aria-label="Buy, add, hold and trim price bands">
        <div class="k-well" role="status">Governed price bands are loading.</div>
      </div>
    </article>

    <div class="company-desk-summary-grid">
      <section class="k-card k-card-section" data-testid="summary-thesis" aria-labelledby="deskSummaryThesisHeading">
        <header class="k-card-head"><div class="k-card-heading"><h2 class="k-card-title" id="deskSummaryThesisHeading">Why I own this company</h2><p class="k-card-meta">Current report-backed thesis</p></div></header>
        <div id="deskThesisRisk"><div class="k-well">Current thesis evidence unavailable.</div></div>
      </section>
      <section class="k-card k-card-section" data-testid="q2-update" aria-labelledby="deskQ2UpdateHeading">
        <header class="k-card-head"><div class="k-card-heading"><h2 class="k-card-title" id="deskQ2UpdateHeading">Latest quarter update</h2><p class="k-card-meta">Governed earnings readout</p></div></header>
        <div id="deskQ2Update"><div class="k-well">The governed quarterly readout is pending.</div></div>
      </section>
    </div>

    <section class="k-card k-card-section" data-testid="next-step-exploration" aria-labelledby="deskNextStepHeading">
      <header class="k-card-head"><div class="k-card-heading"><h2 class="k-card-title" id="deskNextStepHeading">Next-step exploration</h2><p class="k-card-meta">Recent evidence and open questions</p></div></header>
      <div class="company-desk-exploration-grid">
        <div><h3 class="k-well-title">Recent relevant updates</h3><div class="research-list" id="deskRecentUpdates"><div class="k-well">No governed update is available.</div></div></div>
        <div>
          <div class="research-row"><h3 class="k-well-title">Open questions</h3><div class="research-actions"><button class="k-btn k-btn-quiet k-btn-sm" type="button" id="workOsAskQuestions" data-research-chat="open-questions" data-copilot-scope="open-questions" disabled>Ask these questions</button><button class="k-btn k-btn-quiet k-btn-sm" type="button" id="workOsManageResearchItems">Manage items</button></div></div>
          <form class="research-question-capture" id="deskQuestionCapture">
            <label class="k-card-meta" for="deskQuestionInput">Add an owner question</label>
            <div class="research-actions"><input id="deskQuestionInput" maxlength="2000" required placeholder="What should we track?" autocomplete="off"><button class="k-btn k-btn-primary k-btn-sm" type="submit">Track question</button></div>
            <span class="stat-subtext" id="deskQuestionCaptureStatus" aria-live="polite"></span>
          </form>
          <div class="research-list" id="deskQuestions"><div class="k-well">No open research questions.</div></div>
        </div>
      </div>
    </section>

    <section class="k-card k-card-section" data-testid="contracts-card" aria-labelledby="deskContractsHeading">
      <header class="k-card-head"><div class="k-card-heading"><h2 class="k-card-title" id="deskContractsHeading">Thesis contracts &amp; management follow-through</h2><p class="k-card-meta">Governed conditions and quarter-indexed Say / Do history</p></div><button class="k-btn k-btn-quiet k-btn-sm" type="button" id="workOsAskContracts" data-research-chat="thesis-contracts" data-copilot-scope="thesis-contracts" disabled>Stress-test contracts</button></header>
      <div class="research-tabs" role="tablist" aria-label="Thesis contracts and Say Do">
        <button class="research-tab k-btn k-btn-quiet" id="deskContractsTab" type="button" role="tab" aria-selected="true" aria-controls="deskContractsPanel" tabindex="0" data-company-desk-section="contracts">Thesis contracts</button>
        <button class="research-tab k-btn k-btn-quiet" id="deskSayDoTab" type="button" role="tab" aria-selected="false" aria-controls="deskSayDoPanel" tabindex="-1" data-company-desk-section="saydo">Say / Do · 4 quarters</button>
      </div>
      <div id="deskContractsPanel" role="tabpanel" aria-labelledby="deskContractsTab" data-company-desk-panel="contracts">
        <div class="research-list" id="deskConditions"><div class="k-well">No governed conditions are attached to the current decision.</div></div>
      </div>
      <div id="deskSayDoPanel" role="tabpanel" aria-labelledby="deskSayDoTab" data-company-desk-panel="saydo" data-testid="saydo-panel" hidden>
        <div class="k-say-do-timeline" id="deskSayDoTimeline"><div class="k-well">Say / Do history is loading.</div></div>
        <button class="k-btn k-btn-quiet k-btn-sm" id="workOsOpenFullSayDo" data-testid="open-full-saydo" type="button" disabled>Open full Say / Do section →</button>
      </div>
    </section>

    <div hidden aria-hidden="true">
      <div id="deskDecisionBand"></div><span id="deskModelState"></span><span id="deskModelRevision"></span>
      <span id="deskDecisionRelationship"></span><span id="deskDecisionFreshness"></span>
      <span id="deskPositionWeight"></span><span id="deskHeroPositionWeight"></span><span id="deskPositionSource"></span>
      <span id="deskInputPrice"></span><span id="deskInputPriceSource"></span><span id="deskFairValueSource"></span>
      <span id="deskHeroFairValue"></span><div id="deskFinancialsSummary"></div><div id="deskTranscriptsQA"></div>
      <div id="deskProvenanceLinks"></div><span id="deskBriefDate"></span><span id="deskBriefStatus"></span>
      <button id="deskThesisBriefDoorway" type="button"></button><div id="deskKpiSummary"></div>
    </div>
    <div class="k-well" id="deskWarnings" hidden></div>
  </div>
</section>
""".strip()


def render_brief_library_shell() -> str:
    """Return the persistent report inventory governed by artifact manifests."""

    return """
<section id="screen-brief-library" class="screen-view" data-layout="report-library">
  <div class="research-screen" aria-live="polite">
    <header class="k-card k-card-section research-toolbar">
      <div><div class="k-card-meta">Research Engine</div><h2 class="k-card-title">Brief Library</h2><div class="k-card-meta">Quarter-indexed earnings readouts and full research briefs with stable identity and freshness</div></div>
      <div class="research-actions">
        <label class="k-card-meta" for="briefKindFilter">Artifact</label>
        <select class="k-select" id="briefKindFilter"><option value="">All</option><option value="earnings_readout">Earnings readouts</option><option value="full_brief">Full briefs</option></select>
        <label class="k-card-meta" for="briefTickerFilter">Ticker</label>
        <select class="k-select" id="briefTickerFilter"><option value="">All companies</option></select>
        <label class="k-card-meta" for="briefRoleFilter">Coverage</label>
        <select class="k-select" id="briefRoleFilter"><option value="">All</option><option value="portfolio">Portfolio</option><option value="evaluation">Evaluation</option><option value="unknown">Unknown</option></select>
      </div>
    </header>
    <div class="research-library-grid" id="workOsBriefLibrary"><div class="k-well" role="status">Open Brief Library to load persisted artifacts.</div></div>
    <div class="k-well" id="briefLibraryWarnings" hidden></div>
  </div>
</section>
""".strip()


def render_fact_playground_shell() -> str:
    """Return the full-canvas mount for the governed Explore panel."""

    return """
<section id="screen-analytics-playground" class="screen-view" data-layout="governed-fact-playground">
  <div class="research-screen" aria-live="polite">
    <header class="k-card k-card-section research-toolbar">
      <div><div class="k-card-meta">Research Engine</div><h2 class="k-card-title">Fact &amp; Metric Playground</h2><div class="k-card-meta">Open-ended analysis over governed financial facts, KPIs, and segments</div></div>
      <div class="research-actions">
        <label class="k-card-meta" for="workOsFactTicker">Primary company</label>
        <select class="k-select" id="workOsFactTicker" aria-label="Choose primary company"><option value="">Loading tracked companies…</option></select>
      </div>
    </header>
    <div id="workOsFactPlayground">
      <div class="k-well" role="status">Open Fact &amp; Metric Playground to load governed data. No prototype values are being shown.</div>
    </div>
  </div>
</section>
""".strip()


__all__ = [
    "render_brief_library_shell",
    "render_brief_reader_shell",
    "render_company_desk_shell",
    "render_fact_playground_shell",
]
