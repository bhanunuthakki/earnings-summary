"""Shared Work OS research surfaces that are not persistent destinations."""

from __future__ import annotations


def render_brief_reader_shell() -> str:
    """Return the transient reader container mounted once beside the Work OS."""

    return """
<section class="work-os-reader" id="workOsBriefReader" role="dialog" aria-modal="true"
         aria-hidden="true" aria-labelledby="workOsBriefReaderTitle" hidden>
  <header class="work-os-reader-header">
    <div>
      <div class="stat-heading">Full Research Brief</div>
      <h2 id="workOsBriefReaderTitle">Research brief</h2>
    </div>
    <button class="k-btn k-btn-quiet k-btn-sm" id="workOsBriefReaderClose" type="button"
            aria-label="Close full research brief">Close</button>
  </header>
  <div class="work-os-reader-body" id="workOsBriefReaderBody" role="region"
       aria-live="polite"></div>
</section>
""".strip()


def render_company_desk_shell() -> str:
    """Return the approved decision-workbench structure without demo facts."""

    return """
<section id="screen-workspace" class="screen-view" data-layout="decision-workbench">
  <div class="research-screen" id="workOsCompanyDesk" aria-live="polite">
    <div class="k-card research-toolbar">
      <div>
        <div class="stat-heading">Company Desk</div>
        <div class="k-ticker"><span class="k-ticker-symbol t-mono" id="deskTicker">—</span><span class="k-ticker-name" id="deskCompanyName">Choose a portfolio company</span></div>
        <div class="stat-subtext" id="deskCoverageRole">Governed company research</div>
      </div>
      <div class="research-actions">
        <label class="stat-heading" for="companyPickerSelect">Company</label>
        <select class="k-select" id="companyPickerSelect"></select>
        <button class="k-btn k-btn-quiet k-btn-sm" type="button" data-research-chat="company">Ask Copilot</button>
        <button class="k-btn k-btn-primary k-btn-sm" id="workOsFullBriefButton" type="button" disabled>Read full brief →</button>
      </div>
    </div>
    <div class="research-decision-band" id="deskDecisionBand">
      <div class="k-card"><div class="stat-heading">Owner posture</div><div class="stat-number" id="deskOwnerState">—</div><div class="stat-subtext" id="deskOwnerRevision">No owner decision recorded</div></div>
      <div class="k-card"><div class="stat-heading">Model recommendation</div><div class="stat-number" id="deskModelState">—</div><div class="stat-subtext">Advisory state, never owner state</div></div>
      <div class="k-card"><div class="stat-heading">Position</div><div class="stat-number" id="deskPositionWeight">—</div><div class="stat-subtext" id="deskPositionSource">Current snapshot unavailable</div></div>
      <div class="k-card"><div class="stat-heading">Latest brief</div><div class="stat-number" id="deskBriefDate">—</div><div class="stat-subtext" id="deskBriefStatus">No indexed artifact</div></div>
    </div>
    <div class="research-grid">
      <article class="k-card">
        <div class="research-panel-head"><div><div class="stat-heading">Thesis contracts</div><div class="stat-subtext">Falsifiable conditions attached to the current decision</div></div><button class="k-btn k-btn-quiet k-btn-sm" type="button" data-research-chat="conditions">Ask Copilot</button></div>
        <div class="research-list" id="deskConditions"><div class="k-well">No governed conditions loaded.</div></div>
      </article>
      <aside class="k-card">
        <div class="research-panel-head"><div><div class="stat-heading">Open questions</div><div class="stat-subtext">Owner and model items remain distinct</div></div><button class="k-btn k-btn-quiet k-btn-sm" type="button" data-research-chat="questions">Ask Copilot</button></div>
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
      <div><div class="stat-heading">Research Engine</div><h2>Brief Library</h2><div class="stat-subtext">Persisted research artifacts with stable identity, provenance, and freshness</div></div>
      <div class="research-actions">
        <label class="stat-heading" for="briefTickerFilter">Ticker</label>
        <select class="k-select" id="briefTickerFilter"><option value="">All companies</option></select>
        <label class="stat-heading" for="briefRoleFilter">Coverage</label>
        <select class="k-select" id="briefRoleFilter"><option value="">All</option><option value="portfolio">Portfolio</option><option value="evaluation">Evaluation</option><option value="unknown">Unknown</option></select>
      </div>
    </div>
    <div class="research-library-grid" id="workOsBriefLibrary"><div class="k-well" role="status">Open Brief Library to load persisted artifacts.</div></div>
    <div class="k-well" id="briefLibraryWarnings" hidden></div>
  </div>
</section>
""".strip()


__all__ = [
    "render_brief_library_shell",
    "render_brief_reader_shell",
    "render_company_desk_shell",
]
