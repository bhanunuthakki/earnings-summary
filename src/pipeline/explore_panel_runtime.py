"""Client runtime for the company Work Bench and contextual Copilot handoff.

The server remains authoritative for answers, governed fact catalogs, ViewSpec
execution, and saved analyses. Browser state is limited to presentation
preferences (rail sizes and minimized state).
"""

EXPLORE_PANEL_JS = r"""
window.initExplorePanel = function () {
  var root = document.getElementById('vx-root');
  if (!root || root.dataset.wired) return;
  root.dataset.wired = '1';
  function el(id) { return document.getElementById(id); }
  function isCurrent() { return root.isConnected && document.getElementById('vx-root') === root; }
  function esc(value) {
    return String(value == null ? '' : value).replace(/&/g, '&amp;')
      .replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }
  function ticker() { return String(el('vx-tickers').value || '').trim().toUpperCase(); }
  var workbenchTickers = [];
  var workbenchOpener = null;
  var catalog = [];
  var catalogExpanded = false;
  var selected = {};
  var drawerMetric = null;
  var catalogController = null;
  var catalogVersion = 0;
  var analysisController = null;
  var analysisVersion = 0;
  var workbenchVersion = 0;
  var savedController = null;
  var savedVersion = 0;
  function navigateProcedure(screen) {
    if (typeof window.navigateTo === 'function') window.navigateTo(screen);
    else root.setAttribute('data-navigation-status', 'Open this destination from the application navigation.');
  }
  function retireAnalysis() {
    analysisVersion += 1;
    if (analysisController) analysisController.abort();
    analysisController = null;
    if (isCurrent()) {
      if (window.CCAction) window.CCAction.release(el('vx-run'));
      var result = el('vx-result');
      if (result.querySelector('.vx-result') && !result.querySelector('[data-prior-analysis]')) {
        var notice = document.createElement('div');
        notice.className = 'vx-meta';
        notice.setAttribute('role', 'status');
        notice.setAttribute('data-prior-analysis', '');
        notice.textContent = 'Previous analysis. Run again to apply the current selections.';
        result.prepend(notice);
      }
    }
  }
  function retireWorkbench() {
    workbenchVersion += 1;
    catalogVersion += 1;
    if (catalogController) catalogController.abort();
    catalogController = null;
    retireAnalysis();
  }
  var lifetimeObserver = new MutationObserver(function () {
    if (isCurrent()) return;
    retireWorkbench();
    savedVersion += 1;
    if (savedController) savedController.abort();
    lifetimeObserver.disconnect();
  });
  lifetimeObserver.observe(document.body, {childList: true, subtree: true});


  function openCopilot(fromWorkbench) {
    if (typeof window.openWorkOsCopilot !== 'function') {
      var unavailable = 'Copilot is unavailable here. Open it from the application navigation.';
      if (fromWorkbench) showWorkbenchError(unavailable);
      else el('vx-copilot-status').textContent = unavailable;
      return;
    }
    var company = fromWorkbench && workbenchTickers.length ? workbenchTickers[0] : ticker();
    var prompt = 'What should I examine for ' + (company || 'this company') + '?';
    if (fromWorkbench) {
      prompt = 'Help me interpret this company-data analysis: '
        + el('vx-active-prompt').textContent + '\nAnalysis selections: ' + JSON.stringify(buildSpec());
      closeWorkbench();
    }
    el('vx-copilot-status').textContent = '';
    window.openWorkOsCopilot({company_ticker: company || null, category: 'research',
      context_kind: 'company', prompt: prompt});
  }

  // Copilot owns answer rendering. The caller supplies its question or saved
  // answer spec; compile/run remain the validation owners for the workbench.
  root.addEventListener('work-os-explore-analysis', function (event) {
    if (!isCurrent()) return;
    var detail = event.detail || {};
    var query = typeof detail.query === 'string' ? detail.query.trim() : '';
    var spec = detail.spec && typeof detail.spec === 'object' && !Array.isArray(detail.spec)
      ? detail.spec : null;
    var opener = detail.opener instanceof HTMLElement ? detail.opener : null;
    openWorkbench(query, spec, opener);
  });

  root.addEventListener('work-os-explore-tickers', function (event) {
    var values = event.detail && Array.isArray(event.detail.tickers) ? event.detail.tickers : [];
    if (!values.length) return;
    el('vx-tickers').value = String(values[0]).trim().toUpperCase();
    workbenchTickers = values.map(function (value) { return String(value).trim().toUpperCase(); }).filter(Boolean);
    retireWorkbench();
    if (el('vx-workbench').open) loadCatalog([]);
  });

  function normalizeEntries(raw) {
    var out = [];
    function displayLabel(value) {
      var label = String(value || '').replace(/_/g, ' ').replace(/\s+/g, ' ').trim();
      if (!label) return 'Unnamed metric';
      return label === label.toLowerCase() ? label.charAt(0).toUpperCase() + label.slice(1) : label;
    }
    ['fin', 'kpi', 'seg', 'detail'].forEach(function (domain) {
      (raw[domain] || []).forEach(function (entry) {
        out.push({token: entry.token, label: displayLabel(entry.label), title: entry.title || '',
          origin: entry.origin || domain, domain: domain,
          requiredCadence: entry.required_cadence || '',
          supportedTransforms: entry.supported_transforms || []});
      });
    });
    return out;
  }
  function selectedTokens() { return Object.keys(selected); }
  function entryFor(token) { return catalog.find(function (entry) { return entry.token === token; }); }
  function selectMetric(token, on) {
    retireAnalysis();
    if (on === false) delete selected[token];
    else {
      selected[token] = true;
      var entry = entryFor(token);
      if (entry && entry.requiredCadence) el('vx-cadence').value = entry.requiredCadence;
      if (entry && entry.supportedTransforms.length
          && entry.supportedTransforms.indexOf(el('vx-transform').value) === -1) {
        el('vx-transform').value = entry.supportedTransforms[0];
      }
    }
    renderSelected();
  }
  function renderSelected() {
    var host = el('vx-selected-fields');
    var tokens = selectedTokens();
    host.innerHTML = tokens.map(function (token) {
      var entry = entryFor(token) || {label: token};
      return '<span class="vx-field-chip"><button type="button" class="k-chip k-chip-btn" '
        + 'data-inspect-metric="' + esc(token) + '">' + esc(entry.label) + '</button>'
        + '<button type="button" class="k-btn k-btn-quiet k-btn-sm vx-chip-remove" data-remove-metric="' + esc(token)
        + '" aria-label="Remove ' + esc(entry.label) + '">&times;</button></span>';
    }).join('') || '<span class="vx-none">Choose a governed fact to begin.</span>';
    el('vx-selected-count').textContent = tokens.length + (tokens.length === 1 ? ' field' : ' fields');
    renderSuggestions();
  }
  function matches(entry, query) {
    var haystack = (entry.label + ' ' + entry.token + ' ' + entry.title + ' ' + entry.origin).toLowerCase();
    return !query || haystack.indexOf(query) !== -1;
  }
  function renderSuggestions() {
    var query = String(el('vx-field-search').value || '').trim().toLowerCase();
    var rows = catalog.filter(function (entry) { return matches(entry, query); }).slice(0, 12);
    el('vx-field-suggestions').innerHTML = rows.map(function (entry) {
      var isOn = !!selected[entry.token];
      return '<button type="button" class="vx-field-option' + (isOn ? ' is-on' : '')
        + '" data-toggle-metric="' + esc(entry.token) + '"><span>' + esc(entry.label)
        + '</span><small>' + esc(entry.domain.toUpperCase()) + (entry.title ? ' · ' + esc(entry.title) : '')
        + '</small></button>';
    }).join('') || '<div class="vx-none">No matching cached fact.</div>';
    var catalogRows = catalogExpanded ? catalog : catalog.slice(0, 80);
    var catalogMarkup = catalogRows.map(function (entry) {
      return '<button type="button" class="vx-field-option' + (selected[entry.token] ? ' is-on' : '')
        + '" data-toggle-metric="' + esc(entry.token) + '"><span>' + esc(entry.label)
        + '</span><small>' + esc(entry.domain.toUpperCase())
        + (entry.title ? ' · ' + esc(entry.title) : '') + '</small></button>';
    }).join('');
    if (!catalogExpanded && catalog.length > catalogRows.length) {
      catalogMarkup += '<button type="button" class="k-btn k-btn-quiet" id="vx-show-all-fields">Show all '
        + catalog.length + ' fields</button>';
    }
    el('vx-field-catalog').innerHTML = catalogMarkup
      || '<div class="vx-none">No governed fields are available.</div>';
  }
  function loadCatalog(keep, callback) {
    var universe = workbenchTickers.length ? workbenchTickers : (ticker() ? [ticker()] : []);
    retireAnalysis();
    var version = ++catalogVersion;
    if (catalogController) catalogController.abort();
    catalogController = new AbortController();
    el('vx-field-catalog').setAttribute('aria-busy', 'true');
    window.uiFetch('/api/viewspec/catalog?tickers=' + encodeURIComponent(universe.join(',')),
      {signal: catalogController.signal}).then(function (r) { return r.json(); })
      .then(function (raw) {
        if (!isCurrent() || version !== catalogVersion) return;
        el('vx-field-catalog').removeAttribute('aria-busy');
        catalogController = null;
        catalog = normalizeEntries(raw);
        catalogExpanded = false;
        selected = {};
        (keep || []).forEach(function (token) {
          if (entryFor(token)) selected[token] = true;
        });
        renderSelected();
        if (callback) callback();
      }).catch(function () {
        if (!isCurrent() || version !== catalogVersion) return;
        el('vx-field-catalog').removeAttribute('aria-busy');
        catalogController = null;
        catalog = [];
        selected = {};
        renderSelected();
        showWorkbenchError('Could not load the governed field catalog. Try again.');
      });
  }
  function buildSpec() {
    return {tickers: workbenchTickers.length ? workbenchTickers.slice() : (ticker() ? [ticker()] : []), metrics: selectedTokens(),
      transform: el('vx-transform').value, cadence: el('vx-cadence').value,
      periods: parseInt(el('vx-periods').value, 10) || 8,
      cagr_years: parseInt(el('vx-cagr-years').value, 10) || 3};
  }
  function updateWorkbenchTitle() {
    el('vx-workbench-title').textContent = (workbenchTickers[0] || 'Company')
      + (workbenchTickers.length > 1 ? ' + ' + (workbenchTickers.length - 1) + ' peers' : '')
      + ' analysis';
    var picker = el('vx-workbench-company');
    if (picker && workbenchTickers[0]
        && Array.from(picker.options).some(function (option) { return option.value === workbenchTickers[0]; })) {
      picker.value = workbenchTickers[0];
      picker.dispatchEvent(new Event('input', {bubbles: true}));
    }
  }
  function syncWorkbenchCompanies() {
    var source = document.getElementById('workOsFactTicker');
    var picker = el('vx-workbench-company');
    if (!source || !picker || !source.options.length) return;
    picker.innerHTML = Array.from(source.options).map(function (option) {
      return '<option value="' + esc(option.value) + '">' + esc(option.textContent) + '</option>';
    }).join('');
  }
  function applySpec(spec, shouldRun) {
    if (!spec) return;
    retireWorkbench();
    workbenchTickers = Array.isArray(spec.tickers) ? spec.tickers.map(function (value) {
      return String(value).trim().toUpperCase();
    }).filter(Boolean) : workbenchTickers;
    updateWorkbenchTitle();
    el('vx-transform').value = spec.transform || 'level';
    el('vx-cadence').value = spec.cadence || 'quarterly';
    el('vx-periods').value = spec.periods || 8;
    el('vx-cagr-years').value = spec.cagr_years || 3;
    var tokens = (spec.metrics || []).map(function (metric) {
      if (typeof metric === 'string') return metric;
      if (typeof metric.token === 'string' && metric.token) return metric.token;
      function quotePart(value) {
        return encodeURIComponent(String(value)).replace(/[!'()*]/g, function (character) {
          return '%' + character.charCodeAt(0).toString(16).toUpperCase();
        });
      }
      if (metric.domain === 'detail') {
        return 'detail:' + quotePart(metric.dim_type) + ':'
          + quotePart(metric.dim_name) + ':' + quotePart(metric.key);
      }
      if (metric.domain === 'seg') {
        return 'seg:' + quotePart(metric.dim_type) + ':' + quotePart(metric.dim_name)
          + ':' + quotePart(metric.key);
      }
      return metric.domain + ':' + quotePart(metric.key);
    });
    loadCatalog(tokens, shouldRun ? runView : null);
  }
  function compileForWorkbench(query, contextSpec) {
    var version = ++workbenchVersion;
    retireAnalysis();
    el('vx-active-prompt').textContent = query || 'New company-data analysis';
    if (!query) return;
    fetch('/api/viewspec/compile', {method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({query: query,
        tickers: workbenchTickers.length ? workbenchTickers : (ticker() ? [ticker()] : []),
        context_spec: contextSpec || null})})
      .then(function (r) { return r.json(); }).then(function (result) {
        if (!isCurrent() || version !== workbenchVersion) return;
        if (result.status === 'ok' && result.spec) applySpec(result.spec, true);
        else showWorkbenchError(result.message || 'Could not reshape this analysis.');
      }).catch(function () {
        if (isCurrent() && version === workbenchVersion) showWorkbenchError('Could not reshape this analysis.');
      });
  }
  function openWorkbench(query, spec, opener) {
    retireWorkbench();
    workbenchOpener = opener || document.activeElement;
    workbenchTickers = spec && Array.isArray(spec.tickers) ? spec.tickers.slice() : (ticker() ? [ticker()] : []);
    syncWorkbenchCompanies();
    el('vx-workbench').showModal();
    document.body.classList.add('explore-workbench-open');
    updateWorkbenchTitle();
    el('vx-active-prompt').textContent = query || 'New company-data analysis';
    if (spec) applySpec(spec, true);
    else if (query) compileForWorkbench(query, null);
    else {
      selected = {};
      el('vx-transform').value = 'level';
      el('vx-cadence').value = 'quarterly';
      el('vx-periods').value = 8;
      el('vx-cagr-years').value = 3;
      el('vx-view-name').value = '';
      el('vx-save-status').textContent = '';
      loadCatalog([]);
      el('vx-result').innerHTML = '<div class="vx-none">Choose fields, shape the window, and run.</div>';
    }
    el('vx-back').focus();
  }
  function closeWorkbench() {
    retireWorkbench();
    if (el('vx-workbench').open) el('vx-workbench').close();
    document.body.classList.remove('explore-workbench-open');
    if (workbenchOpener && document.contains(workbenchOpener)) workbenchOpener.focus();
    workbenchOpener = null;
  }
  function showWorkbenchError(message) {
    var result = el('vx-result');
    var notice = document.createElement('div');
    notice.className = 'vx-error';
    notice.setAttribute('role', 'alert');
    notice.setAttribute('data-workbench-read-error', '');
    notice.textContent = message;
    if (result.querySelector('.vx-result')) {
      var previousError = result.querySelector('[data-workbench-read-error]');
      if (previousError) previousError.remove();
      result.prepend(notice);
    } else result.replaceChildren(notice);
  }
  function runView() {
    var spec = buildSpec();
    if (!spec.metrics.length) { showWorkbenchError('Choose at least one field.'); return; }
    retireAnalysis();
    var version = analysisVersion;
    var controller = new AbortController();
    analysisController = controller;
    var deadline = window.setTimeout(function () {
      controller.abort(new DOMException('Analysis timed out', 'TimeoutError'));
    }, 30000);
    var button = el('vx-run');
    if (window.CCAction) window.CCAction.busy(button, 'Running…');
    // This POST computes a view without persisting state. Its own request owner
    // supplies cancellation; uiFetch leaves POST and stream lifecycles alone.
    fetch('/api/viewspec/run', {method: 'POST', headers: {'Content-Type': 'application/json'},
      signal: controller.signal, body: JSON.stringify({spec: spec})}).then(function (response) {
        if (response.ok) return response.text();
        return response.json().then(function (error) { throw new Error(error.error || 'View failed'); });
      }).then(function (html) {
        if (!isCurrent() || version !== analysisVersion || JSON.stringify(buildSpec()) !== JSON.stringify(spec)) return;
        el('vx-result').innerHTML = html;
      }).catch(function (error) {
        if (!isCurrent() || version !== analysisVersion) return;
        showWorkbenchError(controller.signal.aborted ? 'Analysis timed out. Try again.' : error.message || 'View failed');
      }).finally(function () {
        window.clearTimeout(deadline);
        if (!isCurrent() || version !== analysisVersion) return;
        analysisController = null;
        if (window.CCAction) window.CCAction.release(button);
      });
  }
  function saveView() {
    var name = String(el('vx-view-name').value || '').trim();
    if (!name) { el('vx-view-name').focus(); return; }
    var spec = buildSpec();
    fetch('/api/views', {method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({name: name, spec: spec})}).then(function (response) {
        if (!isCurrent()) return;
        if (!response.ok) throw new Error('save failed');
        el('vx-save-status').textContent = 'Saved';
        refreshSavedViews();
      }).catch(function () {
        if (isCurrent()) el('vx-save-status').textContent = 'Could not save';
      });
  }
  function openInspector(token) {
    var entry = entryFor(token);
    if (!entry) return;
    drawerMetric = token;
    el('vx-inspector-title').textContent = entry.label;
    el('vx-inspector-definition').textContent = entry.title || 'No cached definition note.';
    el('vx-inspector-token').textContent = entry.token;
    el('vx-inspector-origin').textContent = entry.origin || entry.domain;
    el('vx-inspector').hidden = false;
    root.classList.add('vx-inspector-open');
  }
  function closeInspector() {
    drawerMetric = null;
    el('vx-inspector').hidden = true;
    root.classList.remove('vx-inspector-open');
  }
  function setView(view) {
    var result = el('vx-result');
    result.classList.toggle('show-table-only', view === 'table');
    result.classList.toggle('show-chart-only', view === 'chart');
    root.querySelectorAll('[data-result-view]').forEach(function (button) {
      button.setAttribute('aria-pressed', String(button.getAttribute('data-result-view') === view));
    });
  }

  function refreshSavedViews() {
    var version = ++savedVersion;
    if (savedController) savedController.abort();
    savedController = new AbortController();
    window.uiFetch('/api/panel/explore?fragment=views', {signal: savedController.signal})
      .then(function (response) { return response.text(); })
      .then(function (html) {
        if (isCurrent() && version === savedVersion) el('vx-saved-list').innerHTML = html;
      }).catch(function () {
        if (isCurrent() && version === savedVersion) el('vx-saved-list').innerHTML = '<span class="vx-none" role="alert">Could not load saved analyses. Close and reopen Saved to retry.</span>';
      });
  }
  function toggleSavedViews() {
    var panel = el('vx-saved-panel');
    panel.hidden = !panel.hidden;
    el('vx-saved-toggle').setAttribute('aria-expanded', String(!panel.hidden));
    if (!panel.hidden) refreshSavedViews();
  }
  el('vx-open-empty').addEventListener('click', function (event) { openWorkbench('', null, event.currentTarget); });
  el('vx-ask-copilot').addEventListener('click', function () { openCopilot(false); });
  el('vx-workbench-copilot').addEventListener('click', function () { openCopilot(true); });
  el('vx-procedure-reports').addEventListener('click', function () { navigateProcedure('screen-brief-library'); });
  el('vx-procedure-operations').addEventListener('click', function () { navigateProcedure('screen-execution-queue'); });
  el('vx-procedure-analytics').addEventListener('click', function (event) { openWorkbench('', null, event.currentTarget); });
  el('vx-back').addEventListener('click', closeWorkbench);
  el('vx-workbench').addEventListener('cancel', function (event) {
    event.preventDefault(); closeWorkbench();
  });
  el('vx-run').addEventListener('click', runView);
  el('vx-save').addEventListener('click', saveView);
  el('vx-workbench-go').addEventListener('click', function () {
    var query = String(el('vx-workbench-q').value || '').trim();
    if (!query) return;
    el('vx-workbench-q').value = '';
    compileForWorkbench(query, buildSpec());
  });
  el('vx-workbench-q').addEventListener('keydown', function (event) {
    if (event.key === 'Enter') { event.preventDefault(); el('vx-workbench-go').click(); }
  });
  el('vx-workbench-company').addEventListener('change', function (event) {
    var value = String(event.currentTarget.value || '').trim().toUpperCase();
    if (!value) return;
    retireWorkbench();
    workbenchTickers = [value];
    selected = {};
    updateWorkbenchTitle();
    loadCatalog([], function () {
      el('vx-result').innerHTML = '<div class="vx-none">Choose fields for ' + esc(value)
        + ', then run the analysis.</div>';
    });
  });
  el('vx-field-search').addEventListener('input', renderSuggestions);
  el('vx-field-search').addEventListener('focus', function () { el('vx-field-suggestions').hidden = false; });
  el('vx-browse-fields').addEventListener('click', function () {
    var rail = el('vx-fields-rail');
    rail.hidden = !rail.hidden;
    root.classList.toggle('vx-fields-open', !rail.hidden);
  });
  el('vx-fields-minimize').addEventListener('click', function () {
    el('vx-fields-rail').hidden = true; root.classList.remove('vx-fields-open');
  });
  el('vx-inspector-close').addEventListener('click', closeInspector);
  el('vx-saved-toggle').addEventListener('click', toggleSavedViews);
  el('vx-saved-list').addEventListener('click', function (event) {
    var saved = event.target.closest('.vx-saved');
    if (!saved) return;
    if (event.target.closest('[data-act="load"]')) {
      try { applySpec(JSON.parse(saved.dataset.spec || '{}'), true); } catch (_err) { return; }
      el('vx-view-name').value = saved.dataset.viewName || '';
      el('vx-saved-panel').hidden = true;
      el('vx-saved-toggle').setAttribute('aria-expanded', 'false');
      return;
    }
    if (event.target.closest('[data-act="del"]')) {
      fetch('/api/views/' + encodeURIComponent(saved.dataset.viewId), {method: 'DELETE'})
        .then(function (response) { if (isCurrent() && response.ok) refreshSavedViews(); });
    }
  });
  root.addEventListener('click', function (event) {
    if (!event.target.closest('.vx-field-search-wrap')) el('vx-field-suggestions').hidden = true;
    var toggle = event.target.closest('[data-toggle-metric]');
    if (toggle) { var tok = toggle.getAttribute('data-toggle-metric'); selectMetric(tok, !selected[tok]); return; }
    if (event.target.closest('#vx-show-all-fields')) {
      catalogExpanded = true; renderSuggestions(); return;
    }
    var remove = event.target.closest('[data-remove-metric]');
    if (remove) { selectMetric(remove.getAttribute('data-remove-metric'), false); return; }
    var inspect = event.target.closest('[data-inspect-metric]');
    if (inspect) { openInspector(inspect.getAttribute('data-inspect-metric')); return; }
    var view = event.target.closest('[data-result-view]');
    if (view) setView(view.getAttribute('data-result-view'));
  });
  function wireResizer(handle, target, min, max) {
    function apply(next) {
      next = Math.max(min, Math.min(max, next));
      target.style.width = next + 'px';
      handle.setAttribute('aria-valuenow', String(next));
    }
    handle.addEventListener('pointerdown', function (event) {
      event.preventDefault(); handle.setPointerCapture(event.pointerId);
      var start = event.clientX;
      var current = parseInt(getComputedStyle(target).width, 10);
      function move(moveEvent) {
        var delta = moveEvent.clientX - start;
        var next = target === el('vx-inspector') ? current - delta : current + delta;
        next = Math.max(min, Math.min(max, next));
        apply(next);
      }
      function up(upEvent) { handle.releasePointerCapture(upEvent.pointerId); handle.removeEventListener('pointermove', move); handle.removeEventListener('pointerup', up); }
      handle.addEventListener('pointermove', move); handle.addEventListener('pointerup', up);
    });
    handle.addEventListener('keydown', function (event) {
      if (event.key !== 'ArrowLeft' && event.key !== 'ArrowRight') return;
      event.preventDefault();
      var current = parseInt(getComputedStyle(target).width, 10);
      var direction = event.key === 'ArrowRight' ? 1 : -1;
      if (target === el('vx-inspector')) direction *= -1;
      apply(current + direction * 16);
    });
  }
  wireResizer(el('vx-fields-resizer'), el('vx-fields-rail'), 240, 520);
  wireResizer(el('vx-inspector-resizer'), el('vx-inspector'), 260, 560);
  ['vx-transform', 'vx-cadence', 'vx-periods', 'vx-cagr-years'].forEach(function (id) {
    el(id).addEventListener('change', retireAnalysis);
  });
};
window.initExplorePanel();
"""


__all__ = ["EXPLORE_PANEL_JS"]
