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
      <button class="k-btn k-btn-quiet k-btn-sm" type="button" data-research-chat="brief-comments">Discuss comments</button>
      <button class="k-btn k-btn-primary k-btn-sm" type="button" data-research-chat="full-brief">Ask about this brief</button>
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
    """Return the native Generation 3 4-tab company desk decision workbench."""

    return """
<section id="screen-workspace" class="screen-view" data-layout="decision-workbench">
  <div class="research-screen" id="workOsCompanyDesk" aria-live="polite">
    <!-- Sticky Valuation & Identity Hero Bar -->
    <header class="k-card k-card-section k-desk-hero research-toolbar" id="companyDeskHero">
      <div class="company-identity-switcher" id="companyPickerRoot">
        <div class="company-identity-row">
          <h1 class="k-card-title" id="workOsCompanyDeskHeading"><span id="companyPickerLabel">Company Desk</span></h1>
          <div class="k-tick">
            <span class="k-tick-sym k-tick-sym-display" id="deskTicker">—</span>
            <span class="k-tick-name" id="deskCompanyName">Choose a portfolio company</span>
            <span class="k-card-meta" id="deskCoverageRole">unknown coverage</span>
          </div>
          <button class="company-picker-trigger k-btn k-btn-quiet k-btn-sm" id="companyPickerTrigger"
                  type="button" aria-haspopup="listbox" aria-controls="companyPickerPopover"
                  aria-expanded="false" aria-label="Switch company desk">Switch ▾</button>
        </div>
        <div class="company-picker-popover k-overlay k-card-stack" id="companyPickerPopover" hidden>
          <label class="k-card-meta" for="companyPickerSearch">Find a company</label>
          <input id="companyPickerSearch" type="search" role="combobox"
                 aria-expanded="false" aria-autocomplete="list"
                 aria-controls="companyPickerList" autocomplete="off"
                 placeholder="Search ticker or company" spellcheck="false">
          <ul class="k-menu company-picker-list" id="companyPickerList" role="listbox"></ul>
          <span class="work-os-live-status" id="companyPickerStatus" role="status" aria-live="polite"></span>
        </div>
      </div>
      <div class="desk-stats-strip">
        <div class="k-stat-cell"><div class="stat-heading">Price</div><div class="stat-number" id="deskLivePrice">—</div></div>
        <div class="k-stat-cell"><div class="stat-heading">DCF Fair Value</div><div class="stat-number" id="deskHeroFairValue">—</div></div>
        <div class="k-stat-cell"><div class="stat-heading">Valuation Gap</div><div class="stat-number" id="deskValuationGap"><span class="k-pill">—</span></div></div>
        <div class="k-stat-cell"><div class="stat-heading">Position Weight</div><div class="stat-number" id="deskHeroPositionWeight">—</div></div>
      </div>
      <div class="research-actions">
        <button class="k-btn k-btn-quiet k-btn-sm" type="button" data-research-chat="company">Ask Engine</button>
        <span id="workOsEarningsDoorway"><span class="k-card-meta">Earnings artifact unavailable</span></span>
        <button class="k-btn k-btn-primary k-btn-sm" id="workOsFullBriefButton" type="button" disabled>Read full brief →</button>
      </div>
    </header>
    <!-- 4-Tab Navigation Bar -->
    <nav class="desk-tabs-bar" aria-label="Company desk sections" role="tablist">
      <button class="k-btn k-btn-quiet k-btn-sm is-active" id="deskTabButtonThesis" type="button" role="tab" aria-selected="true" aria-controls="deskTabThesis" data-desk-tab="thesis">1. Thesis &amp; Say-Do</button>
      <button class="k-btn k-btn-quiet k-btn-sm" id="deskTabButtonFinancials" type="button" role="tab" aria-selected="false" aria-controls="deskTabFinancials" data-desk-tab="financials">2. Financials &amp; DCF</button>
      <button class="k-btn k-btn-quiet k-btn-sm" id="deskTabButtonTranscripts" type="button" role="tab" aria-selected="false" aria-controls="deskTabTranscripts" data-desk-tab="transcripts">3. Transcripts &amp; Q&amp;A</button>
      <button class="k-btn k-btn-quiet k-btn-sm" id="deskTabButtonNotes" type="button" role="tab" aria-selected="false" aria-controls="deskTabNotes" data-desk-tab="notes">4. Notes &amp; Provenance</button>
    </nav>

    <div class="k-card k-card-stat research-decision-band" id="deskDecisionBand" data-units="8" aria-label="Company decision summary">
      <div class="k-stat-cell"><div class="stat-heading">Owner posture</div><div class="stat-number" id="deskOwnerState">—</div><div class="stat-subtext" id="deskOwnerRevision">No owner decision recorded</div></div>
      <div class="k-stat-cell"><div class="stat-heading">Model recommendation</div><div class="stat-number" id="deskModelState">—</div><div class="stat-subtext" id="deskModelRevision">No model recommendation recorded</div></div>
      <div class="k-stat-cell"><div class="stat-heading">Decision relationship</div><div class="k-pill k-pill-warn" id="deskDecisionRelationship">Unavailable</div><div class="stat-subtext" id="deskDecisionFreshness">Decision state unavailable</div></div>
      <div class="k-stat-cell"><div class="stat-heading">Thesis risk</div><div class="k-pill k-pill-warn" id="deskThesisStatus">Unavailable</div><div class="stat-subtext" id="deskThesisAsOf">No current evaluated thesis state</div></div>
      <div class="k-stat-cell"><div class="stat-heading">Position weight</div><div class="stat-number" id="deskPositionWeight">Weight unavailable</div><div class="stat-subtext" id="deskPositionSource">Tracker snapshot unavailable</div></div>
      <div class="k-stat-cell"><div class="stat-heading">DCF input price</div><div class="stat-number" id="deskInputPrice">—</div><div class="stat-subtext" id="deskInputPriceSource">No governed input price</div></div>
      <div class="k-stat-cell"><div class="stat-heading">DCF fair value</div><div class="stat-number" id="deskFairValue">—</div><div class="stat-subtext" id="deskFairValueSource">No governed fair value</div></div>
      <div class="k-stat-cell"><div class="stat-heading">Latest brief</div><div class="stat-number" id="deskBriefDate">—</div><div class="stat-subtext" id="deskBriefStatus">No indexed artifact</div></div>
    </div>
    <!-- Tab 1: Thesis & Say-Do -->
    <div class="desk-tab-content" id="deskTabThesis" role="tabpanel" aria-labelledby="deskTabButtonThesis">
      <section class="k-card k-card-section" aria-labelledby="deskThesisRiskHeading">
        <header class="k-card-head"><div class="k-card-heading"><h2 class="k-card-title" id="deskThesisRiskHeading">Thesis risk</h2><p class="k-card-meta">Current report-backed thesis status; distinct from the allocation decision below</p></div><button class="k-btn k-btn-quiet k-btn-sm" id="deskThesisBriefDoorway" type="button" disabled>Read matching full brief →</button></header>
        <div class="research-list" id="deskThesisRisk"><div class="k-well">Current thesis evidence unavailable.</div></div>
      </section>
      <section class="k-card k-card-section" aria-labelledby="deskKpiSummaryHeading">
        <header class="k-card-head"><div class="k-card-heading"><h2 class="k-card-title" id="deskKpiSummaryHeading">Tier-1 KPI summary</h2><p class="k-card-meta">Exact governed series only · source and as-of shown on each item</p></div><button class="k-btn k-btn-quiet k-btn-sm" type="button" data-research-chat="thesis-kpis">Ask Copilot</button></header>
        <div class="research-list" id="deskKpiSummary"><div class="k-well">Tier-1 KPI evidence unavailable.</div></div>
      </section>
      <div class="research-grid">
        <article class="k-card k-card-section">
          <header class="k-card-head"><div class="k-card-heading"><h2 class="k-card-title">Decision conditions</h2><p class="k-card-meta">Falsifiable conditions attached to the current allocation decision</p></div><button class="k-btn k-btn-quiet k-btn-sm" type="button" data-research-chat="conditions">Ask Copilot</button></header>
          <div class="research-list" id="deskConditions"><div class="k-well">No governed conditions loaded.</div></div>
        </article>
        <aside class="k-card k-card-section">
          <header class="k-card-head"><div class="k-card-heading"><h2 class="k-card-title">Say/Do Management Track Record</h2><p class="k-card-meta">Historical commitments vs reported actuals</p></div></header>
          <div class="k-say-do-timeline" id="deskSayDoTimeline"><div class="k-well">Loading Say/Do tracking history…</div></div>
        </aside>
      </div>
    </div>

    <!-- Tab 2: Financials & DCF -->
    <div class="desk-tab-content" id="deskTabFinancials" role="tabpanel" aria-labelledby="deskTabButtonFinancials" hidden>
      <div class="k-card k-card-section">
        <header class="k-card-head"><div class="k-card-heading"><h2 class="k-card-title">Valuation Engine &amp; Segments</h2><p class="k-card-meta">DCF model assumptions and segment revenue trajectory</p></div></header>
        <div id="deskFinancialsSummary" class="k-matrix-grid">
          <div class="k-well">Select a company to inspect financial projections and DCF valuation bridges.</div>
        </div>
      </div>
    </div>

    <!-- Tab 3: Transcripts & QA -->
    <div class="desk-tab-content" id="deskTabTranscripts" role="tabpanel" aria-labelledby="deskTabButtonTranscripts" hidden>
      <div class="k-card k-card-section">
        <header class="k-card-head"><div class="k-card-heading"><h2 class="k-card-title">Earnings Transcripts &amp; Executive Quotes</h2><p class="k-card-meta">Verified speaker quotes with timestamped provenance</p></div></header>
        <div id="deskTranscriptsQA"><div class="k-well">No transcript quotes loaded for this company.</div></div>
      </div>
    </div>

    <!-- Tab 4: Notes & Provenance -->
    <div class="desk-tab-content" id="deskTabNotes" role="tabpanel" aria-labelledby="deskTabButtonNotes" hidden>
      <div class="research-grid">
        <article class="k-card k-card-section">
          <header class="k-card-head"><div class="k-card-heading"><h2 class="k-card-title">Open Questions &amp; Triage</h2><p class="k-card-meta">Owner and model items remain distinct</p></div><div class="research-actions"><button class="k-btn k-btn-quiet k-btn-sm" type="button" id="workOsManageResearchItems">Manage items</button><button class="k-btn k-btn-quiet k-btn-sm" type="button" data-research-chat="questions">Ask Engine</button></div></header>
          <form class="research-question-capture" id="deskQuestionCapture">
            <label class="k-card-meta" for="deskQuestionInput">Add an owner question</label>
            <div class="research-actions"><input id="deskQuestionInput" maxlength="2000" required placeholder="What should we track?" autocomplete="off"><button class="k-btn k-btn-primary k-btn-sm" type="submit">Track question</button></div>
            <span class="stat-subtext" id="deskQuestionCaptureStatus" aria-live="polite"></span>
          </form>
          <div class="research-list" id="deskQuestions"><div class="k-well">No open research questions loaded.</div></div>
        </article>
        <aside class="k-card k-card-section">
          <header class="k-card-head"><div class="k-card-heading"><h2 class="k-card-title">SEC Provenance &amp; Filing Links</h2><p class="k-card-meta">Direct EDGAR links to 10-K, 10-Q, 8-K sources</p></div></header>
          <div id="deskProvenanceLinks"><div class="k-well">No filing links indexed.</div></div>
        </aside>
      </div>
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
