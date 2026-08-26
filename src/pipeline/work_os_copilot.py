"""Read-only Copilot workspace for the production Equity Work OS.

The renderer is deliberately independent of Flask and persistence.  It emits
one DOM workspace and a browser controller that consumes the existing Ask
session and streaming APIs.  Backend support for richer session/research
context is additive: current servers safely ignore those planned fields.
"""

from pipeline.work_os_styles import WORK_OS_COPILOT_CSS as WORK_OS_COPILOT_CSS_MASTER
from ui.controls import icon_svg

WORK_OS_COPILOT_CSS = WORK_OS_COPILOT_CSS_MASTER


_WORK_OS_COPILOT_HTML = """
<button class="work-os-copilot-launcher k-btn k-btn-quiet"
  id="workOsCopilotLauncher" type="button" aria-label="Open Copilot"
  aria-controls="workOsCopilot" aria-expanded="false" data-copilot-dock-state="idle">__ASK_ICON__
  <span class="k-chip k-chip-mono k-chip-accent" id="workOsCopilotLauncherPillStreaming" hidden
    aria-live="polite" aria-atomic="true">Researching</span>
  <span class="k-chip k-chip-mono k-chip-ok" id="workOsCopilotLauncherPillComplete" hidden
    aria-live="polite" aria-atomic="true">Response ready</span>
  <span class="k-chip k-chip-mono k-chip-bad" id="workOsCopilotLauncherPillError" hidden
    aria-live="polite" aria-atomic="true">Needs attention</span></button>
<section class="work-os-copilot" id="workOsCopilot" data-mode="canvas" role="dialog"
  aria-modal="true" aria-hidden="true" aria-labelledby="workOsCopilotTitle" hidden>
  <aside class="work-os-copilot-history" aria-label="Conversation history">
    <div class="work-os-copilot-history-head">
      <button class="k-btn k-btn-primary k-btn-sm" id="workOsCopilotNewChat" type="button">New chat</button>
      <label class="work-os-copilot-history-search">
        <span class="work-os-live-status">Search conversations</span>
        <input id="workOsCopilotHistorySearch" type="search" placeholder="Search history">
      </label>
    </div>
    <div class="work-os-copilot-filter-stack">
      <div class="work-os-copilot-filter-row" aria-label="Coverage role filter">
        <button class="k-chip k-chip-btn k-chip-accent" data-copilot-coverage="all" type="button">All</button>
        <button class="k-chip k-chip-btn" data-copilot-coverage="portfolio" type="button">Portfolio</button>
        <button class="k-chip k-chip-btn" aria-label="Evaluation" data-copilot-coverage="evaluation" type="button">Eval</button>
      </div>
      <div class="work-os-copilot-filter-row work-os-copilot-filter-context">
        <label class="k-label" for="workOsCopilotCompany">Co.</label>
        <select class="k-select" id="workOsCopilotCompany" aria-label="Filter by company"><option value="">All</option></select>
        <label class="k-label" for="workOsCopilotCategory">Type</label>
        <select class="k-select" id="workOsCopilotCategory">
          <option value="">All</option>
          <option value="research">Research</option>
          <option value="thesis">Decision</option>
          <option value="governed_fact">Metrics</option>
        </select>
      </div>
    </div>
    <div class="work-os-copilot-sessions" id="workOsCopilotHistory" aria-live="polite">
      <div class="k-well" role="status">Loading conversations&hellip;</div>
    </div>
  </aside>
  <div class="work-os-copilot-main">
    <header class="work-os-copilot-toolbar">
      <div class="work-os-copilot-heading">
        <div class="work-os-copilot-title" id="workOsCopilotTitle">Work OS Copilot</div>
        <div class="work-os-copilot-subtitle" id="workOsCopilotStatus">Grounded research workspace</div>
      </div>
      <div class="work-os-copilot-toolbar-actions">
        <button class="k-btn k-btn-quiet k-btn-sm" data-copilot-mode="fullscreen" id="workOsCopilotFullscreen"
          type="button" aria-label="Toggle full screen" aria-pressed="false">__FULLSCREEN_ICON__<span>Full screen</span></button>
        <button class="k-btn k-btn-quiet k-btn-sm" id="workOsCopilotClose" type="button"
          aria-label="Minimize Copilot">__CLOSE_ICON__</button>
      </div>
    </header>
    <div class="work-os-copilot-thread" id="workOsCopilotThread" aria-live="polite">
      <div class="k-well" role="status">Start a grounded conversation from the composer below.</div>
    </div>
    <form class="work-os-copilot-composer" id="workOsCopilotComposer">
      <label for="workOsCopilotInput" class="work-os-live-status">Ask a research question</label>
      <textarea id="workOsCopilotInput" name="query" placeholder="Ask about a company, thesis, source, or governed fact" required></textarea>
      <div class="work-os-copilot-composer-actions">
        <div class="work-os-copilot-context" id="workOsCopilotContext" aria-live="polite">
          <span class="k-chip k-chip-mono">Read-only research</span>
        </div>
        <button class="k-btn k-btn-primary" id="workOsCopilotSend" type="submit">__ASK_ICON__<span>Ask</span></button>
      </div>
    </form>
    <aside class="work-os-copilot-evidence" id="workOsCopilotEvidence" role="dialog"
      aria-modal="true" aria-hidden="true" aria-label="Research context and evidence" hidden>
      <div class="work-os-copilot-evidence-head">
        <div><div class="work-os-copilot-title">Research context</div><div class="work-os-copilot-subtitle">Sources and governed facts</div></div>
        <button class="k-btn k-btn-quiet k-btn-sm" id="workOsCopilotEvidenceClose" type="button" aria-label="Close evidence">__CLOSE_ICON__</button>
      </div>
      <div class="work-os-copilot-evidence-body" id="workOsCopilotEvidenceBody">
        <div class="k-well">Select a cited source or governed fact to inspect its evidence links.</div>
      </div>
    </aside>
  </div>
</section>
"""


WORK_OS_COPILOT_JS = r"""
(function () {
  'use strict';
  var root = document.getElementById('workOsCopilot');
  if (!root || root.dataset.controllerReady === 'true') return;
  root.dataset.controllerReady = 'true';

  var launcher = document.getElementById('workOsCopilotLauncher');
  var launcherPills = {
    streaming: document.getElementById('workOsCopilotLauncherPillStreaming'),
    complete: document.getElementById('workOsCopilotLauncherPillComplete'),
    error: document.getElementById('workOsCopilotLauncherPillError')
  };
  var fullscreen = document.getElementById('workOsCopilotFullscreen');
  var historyNode = document.getElementById('workOsCopilotHistory');
  var historySearch = document.getElementById('workOsCopilotHistorySearch');
  var companySelect = document.getElementById('workOsCopilotCompany');
  var categorySelect = document.getElementById('workOsCopilotCategory');
  var thread = document.getElementById('workOsCopilotThread');
  var form = document.getElementById('workOsCopilotComposer');
  var input = document.getElementById('workOsCopilotInput');
  var send = document.getElementById('workOsCopilotSend');
  var status = document.getElementById('workOsCopilotStatus');
  var evidence = document.getElementById('workOsCopilotEvidence');
  var evidenceBody = document.getElementById('workOsCopilotEvidenceBody');
  var contextNode = document.getElementById('workOsCopilotContext');
  var sessions = [];
  var currentSessionId = null;
  var currentSessionContext = null;
  var currentSessionRevision = 0;
  var activeCoverage = 'all';
  var lastSpec = null;
  var activeCitations = [];
  var pendingContext = {};
  var busy = false;
  var sessionLoadToken = 0;
  var copilotOverlay = window.CCOverlay.register(root, {
    modal: true, priority: window.CCOverlay.PRIORITY.DOCK, scrim: false,
    trapFocus: true, restoreFocus: true, motion: 'rise',
    closeId: 'workOsCopilotClose', wireClose: true,
    onOpen: function () {
      root.setAttribute('aria-hidden', 'false');
      launcher.setAttribute('aria-expanded', 'true');
      if (launcher.dataset.copilotDockState === 'complete') setCopilotDockState('idle');
      loadCopilotSessions();
      window.setTimeout(function () { input.focus(); }, 0);
    },
    onClose: function () {
      root.setAttribute('aria-hidden', 'true');
      launcher.setAttribute('aria-expanded', 'false');
    }
  });
  var evidenceOverlay = window.CCOverlay.register(evidence, {
    modal: true, priority: window.CCOverlay.PRIORITY.PEEK, scrim: false,
    trapFocus: true, restoreFocus: true, motion: 'slide-right',
    closeId: 'workOsCopilotEvidenceClose', wireClose: true,
    onOpen: function () { evidence.setAttribute('aria-hidden', 'false'); },
    onClose: function () { evidence.setAttribute('aria-hidden', 'true'); }
  });

  function esc(value) {
    var node = document.createElement('span');
    node.textContent = String(value == null ? '' : value);
    return node.innerHTML;
  }

  function normalizedScopeItems(value) {
    var kinds = ['company', 'thesis_contract', 'open_question', 'brief_artifact'];
    var seen = {};
    return (Array.isArray(value) ? value : []).filter(function (item) {
      if (!item || typeof item !== 'object' || !kinds.includes(item.kind)) return false;
      item.stable_id = String(item.stable_id || '').trim();
      item.label = String(item.label || '').replace(/[\r\n]+/g, ' ').trim();
      if (!/^[A-Za-z0-9][A-Za-z0-9:._-]{0,255}$/.test(item.stable_id) ||
          !item.label || item.label.length > 256 || seen[item.stable_id]) return false;
      seen[item.stable_id] = true;
      return true;
    }).slice(0, 100).map(function (item) {
      return {kind: item.kind, stable_id: item.stable_id, label: item.label};
    });
  }

  function setCopilotDockState(nextState) {
    var states = {
      idle: {label: 'Open Copilot'},
      streaming: {label: 'Open Copilot - researching'},
      complete: {label: 'Open Copilot - response ready'},
      error: {label: 'Open Copilot - response needs attention'}
    };
    var state = states[nextState] || states.idle;
    launcher.dataset.copilotDockState = nextState in states ? nextState : 'idle';
    launcher.setAttribute('aria-label', state.label);
    Object.keys(launcherPills).forEach(function (name) {
      launcherPills[name].hidden = name !== launcher.dataset.copilotDockState;
    });
  }

  function contextFor(session) {
    return (session && session.session_context) || {};
  }

  function sessionCompany(session) {
    var context = contextFor(session);
    return String(context.company_ticker || session.company_ticker || '').toUpperCase();
  }

  function sessionCoverage(session) {
    var context = contextFor(session);
    return String(context.coverage_role_at_creation || session.coverage_role_at_creation || '').toLowerCase();
  }

  function sessionCategory(session) {
    var context = contextFor(session);
    return String(context.category || session.category || '').toLowerCase();
  }

  function formatThesisVersion(value) {
    var raw = String(value || '').trim();
    if (!raw) return '';
    if (/^[a-f0-9]{64}$/i.test(raw)) return 'Thesis ' + raw.slice(0, 8);
    return /^v/i.test(raw) ? raw : 'Thesis ' + raw;
  }

  function sessionThesisVersion(session) {
    var context = contextFor(session);
    return formatThesisVersion(context.thesis_version);
  }

  function populateCopilotCompanies() {
    var selected = companySelect.value;
    var names = {};
    sessions.forEach(function (session) {
      var ticker = sessionCompany(session);
      if (ticker) names[ticker] = true;
    });
    var hydrated = window.workOsPortfolioHydration && window.workOsPortfolioHydration.companies;
    if (Array.isArray(hydrated)) {
      hydrated.forEach(function (company) {
        var ticker = String(company.ticker || '').toUpperCase();
        if (ticker) names[ticker] = true;
      });
    }
    companySelect.innerHTML = '<option value="">All</option>';
    Object.keys(names).sort().forEach(function (ticker) {
      var option = document.createElement('option');
      option.value = ticker;
      option.textContent = ticker;
      companySelect.appendChild(option);
    });
    companySelect.value = names[selected] ? selected : '';
  }

  function formatSessionUpdatedAt(value) {
    if (!value) return '';
    var parsed = new Date(value);
    if (Number.isNaN(parsed.getTime())) return String(value).slice(0, 16);
    return new Intl.DateTimeFormat('en-US', {
      month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit',
      hour12: false, timeZone: 'UTC'
    }).format(parsed) + ' UTC';
  }

  function sessionRow(session) {
    var row = document.createElement('div');
    row.className = 'work-os-copilot-session';
    row.dataset.sessionId = session.id;
    var main = document.createElement('button');
    main.className = 'k-btn k-btn-quiet k-btn-sm work-os-copilot-session-main';
    main.type = 'button';
    var company = sessionCompany(session);
    var coverage = sessionCoverage(session);
    var category = sessionCategory(session);
    var thesisVersion = sessionThesisVersion(session);
    var updated = formatSessionUpdatedAt(session.updated_at);
    var metadata = [company, coverage, category, thesisVersion, updated].filter(Boolean).join(' | ');
    main.innerHTML = '<span class="work-os-copilot-session-copy"><span class="work-os-copilot-session-title">' +
      esc(session.title || 'Untitled conversation') + '</span><span class="work-os-copilot-session-meta">' +
      esc(metadata || session.updated_at || '') + '</span></span>';
    main.addEventListener('click', function () { loadCopilotSession(session.id); });
    var actions = document.createElement('span');
    actions.className = 'work-os-copilot-session-actions';
    var rename = document.createElement('button');
    rename.className = 'k-btn k-btn-quiet k-btn-sm';
    rename.type = 'button';
    rename.setAttribute('aria-label', 'Rename conversation');
    rename.textContent = 'Rename';
    rename.addEventListener('click', function () { renameCopilotSession(session); });
    var remove = document.createElement('button');
    remove.className = 'k-btn k-btn-danger k-btn-sm';
    remove.type = 'button';
    remove.setAttribute('aria-label', 'Delete conversation');
    remove.textContent = 'Delete';
    remove.addEventListener('click', function () { deleteCopilotSession(session.id); });
    actions.appendChild(rename);
    actions.appendChild(remove);
    row.appendChild(main);
    row.appendChild(actions);
    return row;
  }

  function filterCopilotSessions() {
    var query = historySearch.value.trim().toLowerCase();
    var company = companySelect.value;
    var category = categorySelect.value;
    var visible = sessions.filter(function (session) {
      var searchable = [session.title, sessionCompany(session), sessionCategory(session)].join(' ').toLowerCase();
      return (!query || searchable.indexOf(query) !== -1) &&
        (!company || sessionCompany(session) === company) &&
        (!category || sessionCategory(session) === category) &&
        (activeCoverage === 'all' || sessionCoverage(session) === activeCoverage);
    });
    historyNode.innerHTML = '';
    if (!visible.length) {
      historyNode.innerHTML = '<div class="k-well">' +
        (sessions.length ? 'No conversations match these filters.' : 'No governed conversation history yet.') +
        '</div>';
      return;
    }
    visible.forEach(function (session) { historyNode.appendChild(sessionRow(session)); });
  }

  function loadCopilotSessions() {
    historyNode.innerHTML = '<div class="k-well" role="status">Loading conversations&hellip;</div>';
    fetch('/api/ask/sessions?limit=200', {headers: {Accept: 'application/json'}})
      .then(function (response) {
        if (!response.ok) throw new Error('HTTP ' + response.status);
        return response.json();
      })
      .then(function (payload) {
        sessions = Array.isArray(payload.sessions) ? payload.sessions : [];
        populateCopilotCompanies();
        filterCopilotSessions();
      })
      .catch(function () {
        historyNode.innerHTML = '<div class="k-well" role="alert">Copilot is temporarily unavailable. Conversation history could not be loaded.</div>';
      });
  }

  function appendTurn(role, text) {
    var turn = document.createElement('article');
    turn.className = 'work-os-copilot-turn';
    turn.dataset.role = role;
    var well = document.createElement('div');
    well.className = 'k-well';
    var copy = document.createElement('div');
    copy.className = 'work-os-copilot-turn-copy';
    copy.textContent = text || '';
    well.appendChild(copy);
    turn.appendChild(well);
    thread.appendChild(turn);
    thread.scrollTop = thread.scrollHeight;
    return turn;
  }

  function renderNewChatEmptyState() {
    var suggestions = suggestionsForPendingContext();
    thread.innerHTML = '';
    var well = document.createElement('div');
    well.className = 'k-well';
    well.setAttribute('role', 'status');
    well.textContent = 'Start a grounded conversation.';
    var row = document.createElement('div');
    row.className = 'work-os-copilot-suggestions';
    suggestions.forEach(function (prompt) {
      var button = document.createElement('button');
      button.className = 'k-chip k-chip-btn';
      button.type = 'button';
      button.dataset.copilotSuggestion = prompt;
      button.textContent = prompt;
      button.addEventListener('click', function () {
        input.value = prompt;
        form.requestSubmit();
      });
      row.appendChild(button);
    });
    thread.appendChild(well);
    thread.appendChild(row);
  }

  function suggestionsForPendingContext() {
    var kind = String(pendingContext.context_kind || '');
    if (kind === 'thesis-contracts') return [
      'Which attached thesis contract is closest to breach?',
      'What evidence changed the attached contract statuses?',
      'Stress-test the attached contract thresholds.'
    ];
    if (kind === 'open-questions') return [
      'Answer the attached open questions from governed evidence.',
      'Which attached question is most decision-relevant?',
      'What evidence is still missing for these questions?'
    ];
    if (kind === 'full-brief') return [
      'What changed since this brief was published?',
      'Challenge the attached brief using newer evidence.',
      'Trace the brief claims to their strongest sources.'
    ];
    if (kind === 'company') return [
      'What changed for this company since the last review?',
      'Show the latest governed KPIs for this company.',
      'Stress-test the current company thesis.'
    ];
    return [
      'What changed since the last review?',
      'Show the latest governed KPIs.',
      'Stress-test the current thesis.'
    ];
  }

  function renderStoredTurn(turn) {
    var node = appendTurn(turn.role === 'user' ? 'user' : 'assistant', turn.text || '');
    if (Number.isInteger(turn.id)) node.dataset.turnId = String(turn.id);
    if (turn.role !== 'user' && Array.isArray(turn.citations) && turn.citations.length) {
      renderCitationRow(node, turn.citations);
    }
    return node;
  }

  function hydrateStoredExchangeArtifact(exchangeArtifact, loadToken) {
    if (!exchangeArtifact || exchangeArtifact.schema_version !== 'session_exchange_artifact.v1' ||
        typeof exchangeArtifact.exchange_id !== 'string' ||
        typeof exchangeArtifact.request_id !== 'string' ||
        exchangeArtifact.exchange_id !== exchangeArtifact.request_id ||
        !Number.isInteger(exchangeArtifact.assistant_turn_id) ||
        !Number.isInteger(exchangeArtifact.session_revision)) return;
    var artifact = exchangeArtifact.artifacts;
    if (!artifact || artifact.schema_version !== 'exchange_artifacts.v1') return;
    var host = thread.querySelector('[data-turn-id="' + String(exchangeArtifact.assistant_turn_id) + '"]');
    if (!host) {
      host = document.createElement('article');
      host.className = 'work-os-copilot-turn work-os-copilot-turn-assistant';
      host.dataset.turnId = String(exchangeArtifact.assistant_turn_id);
      thread.appendChild(host);
    }
    if (artifact.view_spec && typeof artifact.view_spec === 'object' && !Array.isArray(artifact.view_spec)) {
      lastSpec = artifact.view_spec;
      var fragment = document.createElement('div');
      fragment.className = 'work-os-copilot-fragment';
      fragment.setAttribute('role', 'status');
      fragment.textContent = 'Restoring saved view...';
      host.appendChild(fragment);
      fetch('/api/viewspec/run', {
        method: 'POST', headers: {'Content-Type': 'application/json', Accept: 'text/html'},
        body: JSON.stringify({spec: artifact.view_spec})
      }).then(function (response) {
        if (!response.ok) throw new Error('HTTP ' + response.status);
        return response.text();
      }).then(function (html) {
        if (loadToken !== sessionLoadToken || !fragment.isConnected) return;
        fragment.removeAttribute('role');
        fragment.innerHTML = html;
      }).catch(function () {
        if (loadToken !== sessionLoadToken || !fragment.isConnected) return;
        fragment.setAttribute('role', 'alert');
        fragment.textContent = 'The saved view could not be restored.';
      });
    }
    if (Array.isArray(artifact.citations) && artifact.citations.length &&
        !host.querySelector('.work-os-copilot-citations')) {
      renderCitationRow(host, artifact.citations);
    }
    var ref = normalizeProposalRef(artifact.proposal_ref);
    if (ref) renderCopilotProposal(host, {ref: ref, diff: null});
    if (artifact.proposal_error &&
        artifact.proposal_error.schema_version === 'proposal_error.v1') {
      var persistedError = normalizeProposalEventError({error: artifact.proposal_error});
      renderCopilotProposalError(host, persistedError);
    }
  }

  function loadCopilotSession(sessionId) {
    sessionLoadToken += 1;
    var loadToken = sessionLoadToken;
    lastSpec = null;
    currentSessionId = null;
    currentSessionContext = null;
    currentSessionRevision = 0;
    activeCitations = [];
    pendingContext = {};
    send.disabled = true;
    renderCopilotContext();
    thread.innerHTML = '<div class="k-well" role="status">Loading conversation...</div>';
    status.textContent = 'Loading conversation...';
    fetch('/api/ask/sessions/' + encodeURIComponent(sessionId), {headers: {Accept: 'application/json'}})
      .then(function (response) {
        if (!response.ok) throw new Error('HTTP ' + response.status);
        return response.json();
      })
      .then(function (session) {
        if (loadToken !== sessionLoadToken) return;
        currentSessionId = session.id;
        currentSessionContext = session.session_context
          ? Object.assign({}, session.session_context) : null;
        currentSessionRevision = Number.isInteger(session.session_revision)
          ? session.session_revision : 0;
        renderCopilotContext();
        thread.innerHTML = '';
        var turns = Array.isArray(session.turns) ? session.turns : [];
        if (!turns.length) thread.innerHTML = '<div class="k-well">This conversation has no turns yet.</div>';
        turns.forEach(renderStoredTurn);
        var exchanges = Array.isArray(session.exchange_artifacts) ? session.exchange_artifacts : [];
        exchanges.forEach(function (exchangeArtifact) {
          hydrateStoredExchangeArtifact(exchangeArtifact, loadToken);
        });
        send.disabled = false;
        status.textContent = session.title || 'Grounded research workspace';
      })
      .catch(function () {
        if (loadToken !== sessionLoadToken) return;
        send.disabled = false;
        status.textContent = 'Conversation unavailable';
        thread.innerHTML = '<div class="k-well" role="alert">Copilot is temporarily unavailable. This conversation could not be loaded.</div>';
      });
  }

  function renameCopilotSession(session) {
    var title = window.prompt('Rename conversation', session.title || '');
    if (!title || !title.trim()) return;
    fetch('/api/ask/sessions/' + encodeURIComponent(session.id), {
      method: 'PATCH', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({title: title.trim()})
    }).then(function (response) {
      if (!response.ok) throw new Error('HTTP ' + response.status);
      return response.json();
    }).then(loadCopilotSessions).catch(function () {
      status.textContent = 'Rename failed. The conversation was not changed.';
    });
  }

  function deleteCopilotSession(sessionId) {
    if (!window.confirm('Delete this conversation? This cannot be undone.')) return;
    fetch('/api/ask/sessions/' + encodeURIComponent(sessionId), {method: 'DELETE'})
      .then(function (response) {
        if (!response.ok && response.status !== 204) throw new Error('HTTP ' + response.status);
        if (currentSessionId === sessionId) startNewCopilotSession();
        loadCopilotSessions();
      }).catch(function () { status.textContent = 'Delete failed. The conversation was not changed.'; });
  }

  function startNewCopilotSession() {
    sessionLoadToken += 1;
    currentSessionId = null;
    currentSessionContext = null;
    currentSessionRevision = 0;
    lastSpec = null;
    activeCitations = [];
    pendingContext = {};
    renderCopilotContext();
    renderNewChatEmptyState();
    status.textContent = 'Grounded research workspace';
    input.value = '';
    input.focus();
  }

  function detachIncompatibleCopilotSession(nextCompany) {
    var normalizedNext = String(nextCompany || '').trim().toUpperCase();
    var currentCompany = String(
      (currentSessionContext && currentSessionContext.company_ticker) || ''
    ).trim().toUpperCase();
    if (!normalizedNext || !currentCompany || currentCompany === normalizedNext) return false;
    sessionLoadToken += 1;
    currentSessionId = null;
    currentSessionContext = null;
    currentSessionRevision = 0;
    lastSpec = null;
    activeCitations = [];
    renderNewChatEmptyState();
    status.textContent = 'Grounded research workspace';
    return true;
  }

  function humanizeCitationPart(value) {
    return String(value || '').replace(/[_-]+/g, ' ').trim();
  }

  function sourceContextLabel(citation) {
    var parts = [citation.ticker, citation.period, humanizeCitationPart(citation.doc_type)]
      .filter(Boolean);
    if (!parts.length) return 'Open cited source';
    return 'Open ' + parts.join(' ');
  }

  function evidenceSource(citation) {
    var internal = citation.href || '';
    var original = citation.source_url || '';
    var factRef = citation.fact_ref || citation.factRef || '';
    var actions = '';
    if (internal) actions += '<a class="k-btn k-btn-quiet k-btn-sm" href="' + esc(internal) + '">' + esc(sourceContextLabel(citation)) + '</a>';
    if (original && original !== internal) actions += '<a class="k-btn k-btn-quiet k-btn-sm" href="' + esc(original) + '" target="_blank" rel="noopener">Open original source</a>';
    if (factRef) actions += '<button class="k-btn k-btn-quiet k-btn-sm" type="button" data-fact-ref="' + esc(factRef) + '" data-open-fact-playground>Fact &amp; Metric Playground</button>';
    return '<div class="k-card work-os-copilot-source"><div class="work-os-copilot-title">' +
      esc(citation.label || citation.title || 'Source') + '</div><div class="work-os-copilot-subtitle">' +
      esc(citation.snippet || citation.claim || '') + '</div><div class="work-os-copilot-source-actions">' + actions + '</div></div>';
  }

  function bindEvidenceActions() {
    evidenceBody.querySelectorAll('[data-open-fact-playground]').forEach(function (button) {
      button.addEventListener('click', function () {
        window.workOsCopilotPendingFactRef = button.dataset.factRef || '';
        closeWorkOsCopilot();
        if (typeof window.navigateTo === 'function') window.navigateTo('screen-analytics-playground');
      });
    });
  }

  function openCopilotEvidence(citation) {
    var items = citation ? [citation] : activeCitations;
    evidenceBody.innerHTML = items.length
      ? items.map(evidenceSource).join('')
      : '<div class="k-well">No source or governed fact was attached to this turn.</div>';
    bindEvidenceActions();
    evidenceOverlay.open();
  }

  function closeCopilotEvidence() {
    evidenceOverlay.close();
  }

  function renderCitationRow(turn, citations) {
    activeCitations = citations;
    var row = document.createElement('div');
    row.className = 'work-os-copilot-citations';
    citations.forEach(function (citation, index) {
      var button = document.createElement('button');
      button.className = 'k-chip k-chip-btn k-chip-accent';
      button.type = 'button';
      button.textContent = '[' + String(citation.n || index + 1) + '] ' + (citation.label || citation.title || 'source');
      button.addEventListener('click', function () { openCopilotEvidence(citation); });
      row.appendChild(button);
    });
    turn.appendChild(row);
  }

  function sameOriginActionUrl(value) {
    if (typeof value !== 'string' || !value) return null;
    try {
      var url = new URL(value, window.location.origin);
      if (url.origin !== window.location.origin) return null;
      return url.pathname + url.search + url.hash;
    } catch (_) {
      return null;
    }
  }

  function normalizeProposalRef(value) {
    if (!value || typeof value !== 'object' || Array.isArray(value)) return null;
    var rawId = value.proposal_id;
    if (!(typeof rawId === 'string' || Number.isInteger(rawId))) return null;
    var proposalId = Number(rawId);
    if (!Number.isInteger(proposalId) || proposalId < 1) return null;
    var proposalRevision = Number(value.proposal_revision);
    if (!Number.isInteger(proposalRevision) || proposalRevision < 0) return null;
    var statuses = ['pending', 'approved', 'rejected'];
    var allowed = Array.isArray(value.allowed_actions) ? value.allowed_actions.filter(function (action) {
      return action === 'approve' || action === 'reject';
    }) : [];
    return {
      schema_version: value.schema_version || 'ask_proposal_ref.v1',
      proposal_id: proposalId,
      proposal_revision: proposalRevision,
      status: statuses.includes(value.status) ? value.status : 'pending',
      detail_url: sameOriginActionUrl(value.detail_url),
      decision_url: sameOriginActionUrl(value.decision_url),
      allowed_actions: allowed
    };
  }

  var kpiFields = [
    'name', 'current', 'prior', 'yoy', 'status', 'break_condition', 'source',
    'frequency', 'as_of', 'note', 'notes'
  ];

  function proposalScalarText(value) {
    if (value === undefined) return 'Not present';
    if (value === null) return 'null';
    if (typeof value === 'string') return value;
    if (typeof value === 'number' || typeof value === 'boolean') return String(value);
    return 'Structured value';
  }

  function appendKpiChangeRow(rows, entryLabel, field, oldValue, newValue) {
    var row = document.createElement('div');
    row.className = 'work-os-copilot-kpi-row';
    var label = document.createElement('div');
    label.className = 'k-label';
    label.textContent = entryLabel + ' / ' + humanizeCitationPart(field);
    var current = document.createElement('div');
    current.textContent = proposalScalarText(oldValue);
    var proposed = document.createElement('div');
    proposed.textContent = proposalScalarText(newValue);
    row.appendChild(label);
    row.appendChild(current);
    row.appendChild(proposed);
    rows.appendChild(row);
  }

  function renderKpiProposalComparison(oldEntries, newEntries) {
    var wrapper = document.createElement('div');
    wrapper.className = 'k-well work-os-copilot-kpi-changes';
    var header = document.createElement('div');
    header.className = 'work-os-copilot-kpi-row';
    ['KPI field', 'Current', 'Proposed'].forEach(function (copy) {
      var heading = document.createElement('div');
      heading.className = 'k-label';
      heading.textContent = copy;
      header.appendChild(heading);
    });
    wrapper.appendChild(header);
    var rows = document.createElement('div');
    rows.className = 'work-os-copilot-kpi-changes';
    var oldList = Array.isArray(oldEntries) ? oldEntries.slice(0, 100) : [];
    var newList = Array.isArray(newEntries) ? newEntries.slice(0, 100) : [];
    var entryCount = Math.max(oldList.length, newList.length);
    for (var index = 0; index < entryCount; index += 1) {
      var oldEntry = oldList[index] && typeof oldList[index] === 'object' ? oldList[index] : {};
      var newEntry = newList[index] && typeof newList[index] === 'object' ? newList[index] : {};
      var entryLabel = String(newEntry.name || oldEntry.name || ('KPI ' + String(index + 1)));
      kpiFields.forEach(function (field) {
        var oldValue = Object.prototype.hasOwnProperty.call(oldEntry, field) ? oldEntry[field] : undefined;
        var newValue = Object.prototype.hasOwnProperty.call(newEntry, field) ? newEntry[field] : undefined;
        if (oldValue === newValue) return;
        appendKpiChangeRow(rows, entryLabel, field, oldValue, newValue);
      });
    }
    if (!rows.childNodes.length) {
      var empty = document.createElement('div');
      empty.className = 'work-os-copilot-subtitle';
      empty.textContent = 'No KPI field changes were found.';
      rows.appendChild(empty);
    }
    wrapper.appendChild(rows);
    return wrapper;
  }

  function renderProposalBody(card, proposal) {
    proposal = proposal && typeof proposal === 'object' ? proposal : {};
    var oldBody = card.querySelector('[data-proposal-body]');
    if (oldBody) oldBody.remove();
    var body = document.createElement('div');
    body.dataset.proposalBody = '';
    if (proposal.target_path) {
      var path = document.createElement('span');
      path.className = 'k-chip k-chip-mono';
      path.textContent = String(proposal.target_path);
      body.appendChild(path);
    }
    if (proposal.kind === 'kpi' && Array.isArray(proposal.old_value) && Array.isArray(proposal.new_value)) {
      body.appendChild(renderKpiProposalComparison(proposal.old_value, proposal.new_value));
    } else if (Object.prototype.hasOwnProperty.call(proposal, 'old_value') ||
        Object.prototype.hasOwnProperty.call(proposal, 'new_value')) {
      var grid = document.createElement('div');
      grid.className = 'work-os-copilot-proposal-grid';
      grid.appendChild(proposalComparisonCell('Current', proposal.old_value));
      grid.appendChild(proposalComparisonCell('Proposed', proposal.new_value));
      body.appendChild(grid);
    } else {
      var kv = document.createElement('div');
      kv.className = 'work-os-copilot-proposal-kv';
      Object.keys(proposal).filter(function (key) {
        return !['summary', 'title', 'target_path', 'proposal_ref'].includes(key);
      }).slice(0, 4).forEach(function (key) {
        var label = document.createElement('span');
        label.className = 'k-label';
        label.textContent = humanizeCitationPart(key);
        var value = document.createElement('span');
        value.className = 'work-os-copilot-proposal-cell';
        value.textContent = proposalDisplayValue(proposal[key]);
        kv.appendChild(label);
        kv.appendChild(value);
      });
      if (kv.childNodes.length) body.appendChild(kv);
    }
    card.appendChild(body);
  }

  function updateProposalCardState(card, nextState, message) {
    card.dataset.status = nextState;
    var statusNode = card.querySelector('[data-proposal-status]');
    if (statusNode) statusNode.textContent = message;
    var terminal = ['approved', 'rejected', 'stale', 'target-drift', 'conflict'].includes(nextState);
    var detailReady = card.dataset.proposalDetailReady === 'true';
    card.querySelectorAll('[data-proposal-decision]').forEach(function (button) {
      button.disabled = nextState === 'pending' || terminal || !detailReady;
    });
  }

  function focusProposalTarget(card, target) {
    target = target || card;
    if (!target || typeof target.focus !== 'function') return;
    try { target.focus({preventScroll: true}); } catch (_) { target.focus(); }
  }

  function clearProposalRecoveryActions(card) {
    card.querySelectorAll('[data-proposal-recovery]').forEach(function (button) { button.remove(); });
  }

  function offerProposalRecoveryAction(card, label, handler, moveFocus) {
    clearProposalRecoveryActions(card);
    card.querySelectorAll('[data-proposal-decision]').forEach(function (button) { button.disabled = true; });
    var recovery = document.createElement('button');
    recovery.className = 'k-btn k-btn-quiet k-btn-sm';
    recovery.type = 'button';
    recovery.dataset.proposalRecovery = '';
    recovery.textContent = label;
    recovery.addEventListener('click', handler);
    card.querySelector('[data-proposal-actions]').appendChild(recovery);
    if (moveFocus !== false) focusProposalTarget(card, recovery);
    return recovery;
  }

  function normalizeProposalError(result, ref) {
    if (!result || result.schema_version !== 'ask_proposal_error.v1' ||
        !result.error || typeof result.error !== 'object') {
      return {code: 'invalid_error_response', message: 'The proposal service returned an invalid error.'};
    }
    var error = result.error;
    if (typeof error.code !== 'string' || typeof error.message !== 'string' ||
        (ref && Number(error.proposal_id) !== ref.proposal_id)) {
      return {code: 'invalid_error_response', message: 'The proposal service returned an invalid error.'};
    }
    return error;
  }

  function renderProposalActions(card, ref) {
    var actions = card.querySelector('[data-proposal-actions]');
    actions.innerHTML = '';
    if (!ref || !ref.decision_url || !ref.allowed_actions.includes('approve')) {
      var unavailable = document.createElement('button');
      unavailable.className = 'k-btn k-btn-quiet k-btn-sm work-os-copilot-proposal-action';
      unavailable.type = 'button';
      unavailable.disabled = true;
      unavailable.textContent = 'Approval unavailable';
      actions.appendChild(unavailable);
      return;
    }
    var action = document.createElement('button');
    action.className = 'k-btn k-btn-primary k-btn-sm';
    action.type = 'button';
    action.dataset.proposalDecision = 'approve';
    action.textContent = 'Approve change';
    action.addEventListener('click', function () { decideCopilotProposal(card, ref, 'approve'); });
    actions.appendChild(action);
    if (ref.allowed_actions.includes('reject')) {
      var reject = document.createElement('button');
      reject.className = 'k-btn k-btn-quiet k-btn-sm';
      reject.type = 'button';
      reject.dataset.proposalDecision = 'reject';
      reject.textContent = 'Keep current';
      reject.addEventListener('click', function () { decideCopilotProposal(card, ref, 'reject'); });
      actions.appendChild(reject);
    }
    if (ref.status === 'approved') updateProposalCardState(card, 'approved', 'Change approved');
    if (ref.status === 'rejected') updateProposalCardState(card, 'rejected', 'Kept current');
  }

  async function loadCopilotProposalDetail(card, ref, moveFocus) {
    if (!ref || !ref.detail_url) return;
    updateProposalCardState(card, 'pending', 'Loading proposal...');
    var proposalStatus = card.querySelector('[data-proposal-status]');
    if (moveFocus) focusProposalTarget(card, proposalStatus);
    clearProposalRecoveryActions(card);
    try {
      var response = await fetch(ref.detail_url, {headers: {'Accept': 'application/json'}});
      var detail = await response.json();
      var authoritativeRef = normalizeProposalRef(detail);
      if (!response.ok || !authoritativeRef || authoritativeRef.proposal_id !== ref.proposal_id) {
        throw new Error('invalid proposal detail');
      }
      ref.proposal_revision = authoritativeRef.proposal_revision;
      ref.status = authoritativeRef.status;
      ref.detail_url = authoritativeRef.detail_url;
      ref.decision_url = authoritativeRef.decision_url;
      ref.allowed_actions = authoritativeRef.allowed_actions;
      card.dataset.proposalDetailReady = 'true';
      var title = card.querySelector('[data-proposal-title]');
      if (title) title.textContent = detail.summary || (detail.kind === 'kpi' ? 'KPI change' : 'Thesis change');
      renderProposalBody(card, detail);
      renderProposalActions(card, ref);
      if (ref.status === 'approved') updateProposalCardState(card, 'approved', 'Change approved');
      else if (ref.status === 'rejected') updateProposalCardState(card, 'rejected', 'Kept current');
      else updateProposalCardState(card, 'ready', 'Review exact current and proposed values.');
      if (moveFocus) focusProposalTarget(card, proposalStatus);
    } catch (_) {
      card.dataset.proposalDetailReady = 'false';
      updateProposalCardState(card, 'error', 'Proposal details could not be loaded.');
      offerProposalRecoveryAction(card, 'Retry details', function () {
        loadCopilotProposalDetail(card, ref, true);
      }, moveFocus);
    }
  }

  function handleProposalDecisionError(card, ref, decision, result, response) {
    var error = normalizeProposalError(result, ref);
    if (error.code === 'mutation_busy') {
      updateProposalCardState(card, 'retryable', 'Another governed mutation is in progress. Retry is safe.');
      offerProposalRecoveryAction(card, decision === 'approve' ? 'Retry approval' : 'Retry keep current', function () {
        decideCopilotProposal(card, ref, decision);
      });
      return;
    }
    if (error.code === 'revision_conflict' || error.code === 'status_conflict') {
      if (Number.isInteger(error.current_proposal_revision)) ref.proposal_revision = error.current_proposal_revision;
      if (['pending', 'approved', 'rejected'].includes(error.current_status)) ref.status = error.current_status;
      card.dataset.proposalDetailReady = 'false';
      updateProposalCardState(card, 'stale', 'Proposal changed; review the latest version.');
      offerProposalRecoveryAction(card, 'Review latest', function () {
        loadCopilotProposalDetail(card, ref, true);
      });
      return;
    }
    if (error.code === 'idempotency_conflict') {
      updateProposalCardState(card, 'conflict', 'Decision request conflicts with a different prior action.');
      clearProposalRecoveryActions(card);
      focusProposalTarget(card, card.querySelector('[data-proposal-status]'));
      return;
    }
    if (error.code === 'target_drift' || response.status === 412) {
      updateProposalCardState(card, 'target-drift', 'Target changed since this proposal.');
      clearProposalRecoveryActions(card);
      focusProposalTarget(card, card.querySelector('[data-proposal-status]'));
      return;
    }
    if (response.status === 409) {
      updateProposalCardState(card, 'conflict', error.message || 'Proposal decision conflict.');
      clearProposalRecoveryActions(card);
      focusProposalTarget(card, card.querySelector('[data-proposal-status]'));
      return;
    }
    updateProposalCardState(card, 'error', 'Approval failed. Retry is safe.');
    offerProposalRecoveryAction(card, decision === 'approve' ? 'Retry approval' : 'Retry keep current', function () {
      decideCopilotProposal(card, ref, decision);
    });
  }

  async function decideCopilotProposal(card, ref, decision) {
    if (!ref || !ref.decision_url || !ref.allowed_actions.includes(decision)) return;
    var requestKey = decision === 'approve' ? 'approveRequestId' : 'rejectRequestId';
    var decisionRequestId = card.dataset[requestKey] || buildRequestId();
    card.dataset[requestKey] = decisionRequestId;
    updateProposalCardState(card, 'pending', decision === 'approve' ? 'Approval pending...' : 'Keeping current...');
    clearProposalRecoveryActions(card);
    var proposalStatus = card.querySelector('[data-proposal-status]');
    focusProposalTarget(card, proposalStatus);
    var body = {
      schema_version: 'ask_proposal_decision.v1',
      proposal_id: ref.proposal_id,
      decision: decision,
      expected_proposal_revision: ref.proposal_revision,
      decision_request_id: decisionRequestId
    };
    try {
      var response = await fetch(ref.decision_url, {
        method: 'POST',
        headers: {'Accept': 'application/json', 'Content-Type': 'application/json'},
        body: JSON.stringify(body)
      });
      var result = await response.json().catch(function () { return {}; });
      if (response.status === 409 || response.status === 412 || !response.ok) {
        handleProposalDecisionError(card, ref, decision, result, response);
        return;
      }
      if (Number.isInteger(result.proposal_revision)) ref.proposal_revision = result.proposal_revision;
      ref.status = result.status || (decision === 'approve' ? 'approved' : 'rejected');
      updateProposalCardState(card, ref.status, decision === 'approve' ? 'Change approved' : 'Kept current');
      focusProposalTarget(card, proposalStatus);
    } catch (_) {
      handleProposalDecisionError(card, ref, decision, {}, {status: 0});
    }
  }

  function renderCopilotProposal(turn, item) {
    item = item && typeof item === 'object' ? item : {};
    var ref = item.ref || null;
    var proposal = item.diff && typeof item.diff === 'object' ? item.diff : {};
    var card = document.createElement('div');
    card.className = 'k-card work-os-copilot-proposal';
    card.tabIndex = -1;
    card.dataset.proposalDetailReady = item.diff ? 'true' : 'false';
    var head = document.createElement('div');
    head.className = 'work-os-copilot-proposal-head';
    var title = document.createElement('div');
    title.className = 'work-os-copilot-title';
    title.dataset.proposalTitle = '';
    title.textContent = proposal.summary || proposal.title || 'Governed proposal reference';
    var actions = document.createElement('div');
    actions.className = 'work-os-copilot-proposal-actions';
    actions.dataset.proposalActions = '';
    head.appendChild(title);
    head.appendChild(actions);
    card.appendChild(head);
    var proposalStatus = document.createElement('div');
    proposalStatus.className = 'work-os-copilot-subtitle work-os-copilot-proposal-status';
    proposalStatus.dataset.proposalStatus = '';
    proposalStatus.setAttribute('aria-live', 'polite');
    proposalStatus.tabIndex = -1;
    card.appendChild(proposalStatus);
    renderProposalBody(card, proposal);
    renderProposalActions(card, ref);
    turn.appendChild(card);
    if (ref && ref.detail_url && !item.diff) loadCopilotProposalDetail(card, ref);
    else if (ref && ref.status === 'approved') updateProposalCardState(card, 'approved', 'Change approved');
    else if (ref && ref.status === 'rejected') updateProposalCardState(card, 'rejected', 'Kept current');
    else updateProposalCardState(card, 'ready', ref ? 'Review exact current and proposed values.' : 'Approval unavailable for this reference.');
  }

  function normalizeProposalEventError(event) {
    var raw = event && event.error;
    var code = raw && typeof raw === 'object' && typeof raw.code === 'string'
      ? raw.code : (event && typeof event.code === 'string' ? event.code : 'proposal_unavailable');
    var message = raw && typeof raw === 'object' && typeof raw.message === 'string'
      ? raw.message : (event && typeof event.message === 'string' ? event.message : raw);
    if (!/^[a-z][a-z0-9_]{0,63}$/.test(code)) code = 'proposal_unavailable';
    return {
      code: code,
      message: typeof message === 'string' && message.trim()
        ? message.trim().slice(0, 256) : 'Proposal could not be governed.'
    };
  }

  function renderCopilotProposalError(turn, proposalError) {
    var card = document.createElement('div');
    card.className = 'k-well work-os-copilot-proposal-error';
    card.setAttribute('role', 'alert');
    card.tabIndex = -1;
    var title = document.createElement('div');
    title.className = 'work-os-copilot-title';
    title.textContent = 'Proposal unavailable';
    var copy = document.createElement('div');
    copy.className = 'work-os-copilot-subtitle';
    copy.textContent = proposalError.message;
    var code = document.createElement('span');
    code.className = 'k-chip k-chip-mono';
    code.textContent = proposalError.code;
    card.appendChild(title);
    card.appendChild(copy);
    card.appendChild(code);
    turn.appendChild(card);
  }

  function proposalDisplayValue(value) {
    return proposalScalarText(value);
  }

  function proposalComparisonCell(label, value) {
    var cell = document.createElement('div');
    cell.className = 'k-well work-os-copilot-proposal-cell';
    var heading = document.createElement('div');
    heading.className = 'k-label';
    heading.textContent = label;
    var copy = document.createElement('div');
    copy.textContent = proposalDisplayValue(value);
    cell.appendChild(heading);
    cell.appendChild(copy);
    return cell;
  }

  function parseCopilotSseFrame(frame) {
    var data = frame.split('\n').filter(function (line) { return line.indexOf('data:') === 0; })
      .map(function (line) { return line.replace(/^data:\s?/, ''); }).join('\n');
    if (!data) return null;
    try { return JSON.parse(data); } catch (_) { return null; }
  }

  function handleCopilotEvent(event, state) {
    switch (event.type) {
      case 'session':
        if (event.session_id) currentSessionId = event.session_id;
        if (Number.isInteger(event.session_revision)) {
          currentSessionRevision = event.session_revision;
        }
        if (event.session_context) {
          currentSessionContext = Object.assign({}, event.session_context);
          renderCopilotContext();
        }
        break;
      case 'stage':
        state.stage.textContent = event.note || event.stage || 'Researching';
        break;
      case 'delta':
        state.text += event.text || '';
        state.copy.textContent = state.text;
        break;
      case 'fragment':
        lastSpec = event.spec || lastSpec;
        state.fragment.innerHTML = event.html || '';
        break;
      case 'final':
        state.final = event.text || state.text;
        if (!state.text && state.final) state.copy.textContent = state.final;
        if (Number.isInteger(event.session_revision)) {
          currentSessionRevision = event.session_revision;
        }
        break;
      case 'citations':
        state.citations = Array.isArray(event.items) ? event.items : [];
        break;
      case 'diff_proposal':
        var liveRef = normalizeProposalRef(event.proposal_ref || (event.diff && event.diff.proposal_ref));
        state.proposals.push({ref: liveRef, diff: event.diff || event.proposal || {}});
        break;
      case 'artifacts':
        var artifacts = event.artifacts || event;
        lastSpec = artifacts.view_spec || lastSpec;
        break;
      case 'proposal_ref':
        var ref = normalizeProposalRef(event.proposal_ref || event.ref);
        state.proposals.push({ref: ref, diff: null});
        break;
      case 'proposal_error':
        state.proposalErrors.push(normalizeProposalEventError(event));
        break;
      case 'error':
        state.error = event.error || event.message || 'Copilot is temporarily unavailable.';
        break;
    }
  }

  function finishCopilotTurn(state) {
    busy = false;
    send.disabled = false;
    state.stage.remove();
    if (state.error) {
      state.copy.setAttribute('role', 'alert');
      state.copy.textContent = state.error;
    }
    if (state.citations.length) renderCitationRow(state.turn, state.citations);
    state.proposals.forEach(function (proposal) { renderCopilotProposal(state.turn, proposal); });
    state.proposalErrors.forEach(function (proposalError) {
      renderCopilotProposalError(state.turn, proposalError);
    });
    status.textContent = state.error ? 'Copilot request failed' :
      (state.proposalErrors.length ? 'Grounded response complete with proposal warning' : 'Grounded response complete');
    if (copilotOverlay.isOpen()) setCopilotDockState('idle');
    else setCopilotDockState(state.error ? 'error' : 'complete');
    loadCopilotSessions();
  }

  function buildRequestId() {
    if (window.crypto && typeof window.crypto.randomUUID === 'function') return window.crypto.randomUUID();
    return 'copilot-' + Date.now().toString(36);
  }

  function buildNewSessionContext() {
    var company = pendingContext.company_ticker || companySelect.value;
    var coverage = pendingContext.coverage_role_at_creation ||
      (activeCoverage === 'all' ? 'unknown' : activeCoverage);
    var categories = ['general', 'research', 'governed_fact', 'thesis', 'kpi'];
    var category = categories.includes(pendingContext.category)
      ? pendingContext.category : (categorySelect.value || 'general');
    var snapshot = {
      company_ticker: company || null,
      coverage_role_at_creation: coverage,
      lifecycle_at_creation: pendingContext.lifecycle_at_creation || 'unknown',
      category: category
    };
    if (pendingContext.thesis_version) snapshot.thesis_version = pendingContext.thesis_version;
    if (pendingContext.report_date) snapshot.report_date = pendingContext.report_date;
    if (pendingContext.origin_key) snapshot.origin_key = pendingContext.origin_key;
    if (Number.isInteger(pendingContext.evaluation_candidate_id) && pendingContext.evaluation_candidate_id > 0) {
      snapshot.evaluation_candidate_id = pendingContext.evaluation_candidate_id;
    }
    if (pendingContext.evaluation_instrument_type === 'stock' || pendingContext.evaluation_instrument_type === 'etf') {
      snapshot.evaluation_instrument_type = pendingContext.evaluation_instrument_type;
    }
    return snapshot;
  }

  function renderCopilotContext() {
    var snapshot = currentSessionContext || pendingContext || {};
    contextNode.innerHTML = '';
    var values = ['Read-only research'];
    if (snapshot.company_ticker) values.push(String(snapshot.company_ticker));
    if (snapshot.coverage_role_at_creation) values.push(String(snapshot.coverage_role_at_creation));
    if (snapshot.category) values.push(String(snapshot.category));
    if (snapshot.thesis_version) values.push(formatThesisVersion(snapshot.thesis_version));
    values.forEach(function (value) {
      var chip = document.createElement('span');
      chip.className = 'k-chip k-chip-mono';
      chip.textContent = value;
      contextNode.appendChild(chip);
    });
    normalizedScopeItems(pendingContext.scope_items).forEach(function (item) {
      var button = document.createElement('button');
      button.className = 'k-chip k-chip-btn k-chip-mono';
      button.type = 'button';
      button.textContent = item.label + ' x';
      button.setAttribute('aria-label', 'Remove context ' + item.label);
      button.addEventListener('click', function () { removePendingScopeItem(item.stable_id); });
      contextNode.appendChild(button);
    });
  }

  function removePendingScopeItem(stableId) {
    pendingContext.scope_items = normalizedScopeItems(pendingContext.scope_items).filter(function (item) {
      return item.stable_id !== stableId;
    });
    renderCopilotContext();
    if (!currentSessionId && !busy) renderNewChatEmptyState();
  }

  function submitCopilotQuestion(query) {
    if (busy || !query) return;
    busy = true;
    send.disabled = true;
    setCopilotDockState('streaming');
    var oldEmpty = thread.querySelector('.k-well[role="status"]');
    if (oldEmpty) oldEmpty.remove();
    appendTurn('user', query);
    var turn = appendTurn('assistant', '');
    var copy = turn.querySelector('.work-os-copilot-turn-copy');
    var stage = document.createElement('div');
    stage.className = 'work-os-copilot-stage';
    stage.setAttribute('role', 'status');
    stage.textContent = 'Preparing governed context...';
    turn.querySelector('.k-well').prepend(stage);
    var fragment = document.createElement('div');
    fragment.className = 'work-os-copilot-fragment';
    turn.appendChild(fragment);
    var state = {turn: turn, copy: copy, stage: stage, fragment: fragment, text: '', final: '', citations: [], proposals: [], proposalErrors: [], error: ''};
    var requestId = buildRequestId();
    if (!currentSessionContext) currentSessionContext = buildNewSessionContext();
    renderCopilotContext();
    var company = currentSessionContext.company_ticker || '';
    status.textContent = 'Researching...';
    fetch('/api/ask/stream', {
      method: 'POST', headers: {'Content-Type': 'application/json', Accept: 'text/event-stream'},
      body: JSON.stringify({
        query: query, tickers: company ? [company] : [], context_spec: lastSpec,
        session_id: currentSessionId, request_id: requestId,
        expected_revision: currentSessionRevision,
        session_context: currentSessionContext,
        research_context: {
          screen_id: String(window.location.hash || '').replace(/^#/, '') || 'screen-cockpit',
          fact_ref: pendingContext.fact_ref || null,
          source_ref: pendingContext.source_ref || null,
          scope_items: normalizedScopeItems(pendingContext.scope_items)
        }
      })
    }).then(function (response) {
      if (!response.ok || !response.body) throw new Error('HTTP ' + response.status);
      var reader = response.body.getReader();
      var decoder = new TextDecoder();
      var buffer = '';
      function pump() {
        return reader.read().then(function (result) {
          if (result.done) { finishCopilotTurn(state); return; }
          buffer += decoder.decode(result.value, {stream: true});
          var frames = buffer.split('\n\n');
          buffer = frames.pop();
          frames.forEach(function (frame) {
            var event = parseCopilotSseFrame(frame);
            if (event) handleCopilotEvent(event, state);
          });
          thread.scrollTop = thread.scrollHeight;
          return pump();
        });
      }
      return pump();
    }).catch(function () {
      state.error = 'Copilot is temporarily unavailable. Retry when the local research service is ready.';
      finishCopilotTurn(state);
    });
  }

  function openWorkOsCopilot(context) {
    var supplied = context || {};
    pendingContext = Object.assign({}, supplied);
    if (!['portfolio', 'evaluation', 'unknown'].includes(supplied.coverage_role_at_creation)) {
      delete pendingContext.coverage_role_at_creation;
    }
    if (!['active', 'archived', 'unknown'].includes(supplied.lifecycle_at_creation)) {
      delete pendingContext.lifecycle_at_creation;
    }
    if (!Number.isInteger(supplied.evaluation_candidate_id) || supplied.evaluation_candidate_id <= 0) {
      delete pendingContext.evaluation_candidate_id;
    }
    if (!['stock', 'etf'].includes(supplied.evaluation_instrument_type)) {
      delete pendingContext.evaluation_instrument_type;
    }
    pendingContext.scope_items = normalizedScopeItems(supplied.scope_items);
    pendingContext.context_kind = String(supplied.context_kind || '').trim();
    if (pendingContext.company_ticker) {
      pendingContext.company_ticker = String(pendingContext.company_ticker).toUpperCase();
      companySelect.value = pendingContext.company_ticker;
    }
    var detached = detachIncompatibleCopilotSession(pendingContext.company_ticker);
    if (!detached && currentSessionId && pendingContext.scope_items.length) {
      startNewCopilotSession();
      pendingContext = Object.assign({}, supplied, {
        scope_items: normalizedScopeItems(supplied.scope_items),
        context_kind: String(supplied.context_kind || '').trim()
      });
      detached = true;
    }
    if (detached) input.value = '';
    if (pendingContext.prompt) input.value = pendingContext.prompt;
    if (!currentSessionId && !busy) renderNewChatEmptyState();
    renderCopilotContext();
    copilotOverlay.open();
  }

  function closeWorkOsCopilot() {
    if (evidenceOverlay.isOpen()) closeCopilotEvidence();
    copilotOverlay.close();
  }

  window.openWorkOsCopilot = openWorkOsCopilot;
  window.openWorkOsCopilotSession = function (sessionId) {
    var safeSessionId = typeof sessionId === 'string' ? sessionId.trim() : '';
    if (!safeSessionId) return false;
    copilotOverlay.open();
    loadCopilotSession(safeSessionId);
    return true;
  };
  window.closeWorkOsCopilot = closeWorkOsCopilot;
  window.workOsOpenCopilot = openWorkOsCopilot;
  window.openCopilotEvidence = openCopilotEvidence;

  launcher.addEventListener('click', function () {
    if (typeof window.workOsOpenGlobalCopilot === 'function') window.workOsOpenGlobalCopilot();
    else openWorkOsCopilot();
  });
  document.getElementById('workOsCopilotNewChat').addEventListener('click', startNewCopilotSession);
  fullscreen.addEventListener('click', function () {
    root.dataset.mode = root.dataset.mode === 'fullscreen' ? 'canvas' : 'fullscreen';
    var isFullscreen = root.dataset.mode === 'fullscreen';
    fullscreen.setAttribute('aria-pressed', String(isFullscreen));
    var label = fullscreen.querySelector('span');
    if (label) label.textContent = isFullscreen ? 'Exit full screen' : 'Full screen';
  });
  historySearch.addEventListener('input', filterCopilotSessions);
  companySelect.addEventListener('change', filterCopilotSessions);
  categorySelect.addEventListener('change', filterCopilotSessions);
  root.querySelectorAll('[data-copilot-coverage]').forEach(function (button) {
    button.addEventListener('click', function () {
      activeCoverage = button.dataset.copilotCoverage || 'all';
      root.querySelectorAll('[data-copilot-coverage]').forEach(function (item) {
        item.classList.toggle('k-chip-accent', item === button);
      });
      filterCopilotSessions();
    });
  });
  form.addEventListener('submit', function (ev) {
    ev.preventDefault();
    var query = input.value.trim();
    if (!query || busy) return;
    submitCopilotQuestion(query);
    input.value = '';
  });
  input.addEventListener('keydown', function (ev) {
    if ((ev.metaKey || ev.ctrlKey) && ev.key === 'Enter') { ev.preventDefault(); form.requestSubmit(); }
  });
  window.addEventListener('keydown', function (ev) {
    if ((ev.metaKey || ev.ctrlKey) && ev.key.toLowerCase() === 'k') {
      ev.preventDefault();
      openWorkOsCopilot();
    }
  });
  renderNewChatEmptyState();
  renderCopilotContext();
})();
"""


def render_work_os_copilot() -> str:
    """Render the single production Copilot workspace and its controller."""

    markup = (
        _WORK_OS_COPILOT_HTML.replace("__ASK_ICON__", icon_svg("ask"))
        .replace("__CLOSE_ICON__", icon_svg("close"))
        .replace("__FULLSCREEN_ICON__", icon_svg("cockpit"))
    )
    return (
        f'<style id="work-os-copilot-css">{WORK_OS_COPILOT_CSS}</style>'
        f"{markup}<script>{WORK_OS_COPILOT_JS}</script>"
    )
