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
  <div class="work-os-reader-decision k-card" id="workOsBriefReaderDecision" aria-live="polite">
    <div><div class="stat-heading">Owner posture</div><div class="stat-number" id="workOsBriefOwnerState">â€”</div><div class="stat-subtext" id="workOsBriefOwnerMeta">No owner decision recorded</div></div>
    <div><div class="stat-heading">Model recommendation</div><div class="stat-number" id="workOsBriefModelState">â€”</div><div class="stat-subtext" id="workOsBriefModelMeta">No model recommendation recorded</div></div>
    <div><div class="stat-heading">Relationship</div><div class="k-pill k-pill-warn" id="workOsBriefDecisionRelationship">Unavailable</div><div class="stat-subtext">Thesis verdict remains separately labeled in the brief</div></div>
  </div>
  <div class="work-os-reader-layout">
    <nav class="work-os-reader-sections k-card" id="workOsBriefReaderSections"
         aria-label="Brief sections"></nav>
    <div class="work-os-reader-body" id="workOsBriefReaderBody" role="region"
         aria-live="polite"></div>
  </div>
</section>
""".strip()


def render_company_desk_shell() -> str:
    """Return the approved decision-workbench structure without demo facts."""

    return """
<section id="screen-workspace" class="screen-view" data-layout="decision-workbench">
  <div class="research-screen" id="workOsCompanyDesk" aria-live="polite">
    <div class="k-card research-toolbar">
      <div class="company-identity-switcher" id="companyPickerRoot">
        <div class="k-card-meta" id="companyPickerLabel">Company Desk</div>
        <div class="company-identity-row">
          <div class="k-tick"><span class="k-tick-sym k-tick-sym-display" id="deskTicker">—</span><span class="k-tick-name" id="deskCompanyName">Choose a portfolio company</span></div>
          <button class="company-picker-trigger k-btn k-btn-quiet k-btn-sm" id="companyPickerTrigger"
                  type="button" aria-haspopup="listbox" aria-controls="companyPickerPopover"
                  aria-expanded="false" aria-label="Switch company desk">Switch</button>
        </div>
        <div class="k-card-meta" id="deskCoverageRole">Governed company research</div>
        <div class="company-picker-popover k-card k-card-stack" id="companyPickerPopover" hidden>
          <label class="k-card-meta" for="companyPickerSearch">Find a company</label>
          <input id="companyPickerSearch" type="search" role="combobox"
                 aria-expanded="false" aria-autocomplete="list"
                 aria-controls="companyPickerList" autocomplete="off"
                 placeholder="Search ticker or company" spellcheck="false">
          <ul class="k-menu company-picker-list" id="companyPickerList" role="listbox"></ul>
        </div>
        <span class="work-os-live-status" id="companyPickerStatus" aria-live="polite"></span>
      </div>
      <div class="research-actions">
        <button class="k-btn k-btn-quiet k-btn-sm" type="button" data-research-chat="company">Ask Copilot</button>
        <span id="workOsEarningsDoorway"><span class="k-card-meta">Earnings artifact unavailable</span></span>
        <button class="k-btn k-btn-primary k-btn-sm" id="workOsFullBriefButton" type="button" disabled>Read full brief →</button>
      </div>
    </div>
    <div class="research-decision-band" id="deskDecisionBand">
      <div class="k-card k-card-stack"><div class="stat-heading">Owner posture</div><div class="stat-number" id="deskOwnerState">—</div><div class="stat-subtext" id="deskOwnerRevision">No owner decision recorded</div></div>
      <div class="k-card k-card-stack"><div class="stat-heading">Model recommendation</div><div class="stat-number" id="deskModelState">—</div><div class="stat-subtext" id="deskModelRevision">No model recommendation recorded</div></div>
      <div class="k-card k-card-stack"><div class="stat-heading">Position weight</div><div class="stat-number" id="deskPositionWeight">Weight unavailable</div><div class="stat-subtext" id="deskPositionSource">Tracker snapshot unavailable</div></div>
      <div class="k-card k-card-stack"><div class="stat-heading">DCF input price</div><div class="stat-number" id="deskInputPrice">—</div><div class="stat-subtext" id="deskInputPriceSource">No governed input price</div></div>
      <div class="k-card k-card-stack"><div class="stat-heading">DCF fair value</div><div class="stat-number" id="deskFairValue">—</div><div class="stat-subtext" id="deskFairValueSource">No governed fair value</div></div>
      <div class="k-card k-card-stack"><div class="stat-heading">Latest brief</div><div class="stat-number" id="deskBriefDate">—</div><div class="stat-subtext" id="deskBriefStatus">No indexed artifact</div></div>
    </div>
    <div class="research-grid">
      <article class="k-card k-card-stack">
        <div class="research-panel-head"><div><div class="k-card-title">Thesis contracts</div><div class="stat-subtext">Falsifiable conditions attached to the current decision</div></div><button class="k-btn k-btn-quiet k-btn-sm" type="button" data-research-chat="conditions">Ask Copilot</button></div>
        <div class="research-list" id="deskConditions"><div class="k-well">No governed conditions loaded.</div></div>
      </article>
      <aside class="k-card k-card-stack">
        <div class="research-panel-head"><div><div class="k-card-title">Open questions</div><div class="stat-subtext">Owner and model items remain distinct</div></div><button class="k-btn k-btn-quiet k-btn-sm" type="button" data-research-chat="questions">Ask Copilot</button></div>
        <form class="research-question-capture" id="deskQuestionCapture">
          <label class="k-card-meta" for="deskQuestionInput">Add an owner question</label>
          <div class="research-actions"><input id="deskQuestionInput" maxlength="2000" required placeholder="What should we track?" autocomplete="off"><button class="k-btn k-btn-primary k-btn-sm" type="submit">Track question</button></div>
          <span class="stat-subtext" id="deskQuestionCaptureStatus" aria-live="polite"></span>
        </form>
        <div class="research-list" id="deskQuestions"><div class="k-well">No open research questions loaded.</div></div>
      </aside>
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
    <div class="k-card research-toolbar">
      <div><div class="k-card-meta">Research Engine</div><h2 class="k-card-title">Brief Library</h2><div class="k-card-meta">Persisted research artifacts with stable identity, provenance, and freshness</div></div>
      <div class="research-actions">
        <label class="k-card-meta" for="briefTickerFilter">Ticker</label>
        <select class="k-select" id="briefTickerFilter"><option value="">All companies</option></select>
        <label class="k-card-meta" for="briefRoleFilter">Coverage</label>
        <select class="k-select" id="briefRoleFilter"><option value="">All</option><option value="portfolio">Portfolio</option><option value="evaluation">Evaluation</option><option value="unknown">Unknown</option></select>
      </div>
    </div>
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
    <div class="k-card research-toolbar">
      <div><div class="k-card-meta">Research Engine</div><h2 class="k-card-title">Fact &amp; Metric Playground</h2><div class="k-card-meta">Open-ended analysis over governed financial facts, KPIs, and segments</div></div>
      <div class="research-actions">
        <label class="k-card-meta" for="workOsFactTicker">Primary company</label>
        <select class="k-select" id="workOsFactTicker" aria-label="Choose primary company"><option value="">Loading tracked companies…</option></select>
      </div>
    </div>
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
