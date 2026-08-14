"""Native-loading shell for the consolidated Provenance diagnostics console.

The System theme exposes one Provenance tab with Coverage kept first and an
anchor navigation band. Each diagnostic already has a standalone panel route,
so this assembler fetches them with bounded native-browser concurrency after
first paint. It does not depend on an optional global or an external CDN.

The anchor nav renders ``panel_toolbar(sticky=True)`` (owner directive
2026-08-02): across eleven sections (coverage/validation/evals/…) it stays
pinned below the shell topbar as the owner scrolls instead of scrolling away
after the first section.
"""

from __future__ import annotations

from html import escape
from typing import TYPE_CHECKING

from identity import DEFAULT_USER_ID
from ui.controls import panel_toolbar

if TYPE_CHECKING:
    from pathlib import Path

# (anchor id, navigation label, standalone panel id). The display order is the
# product contract: Coverage remains prominent and leads.
_SectionSpec = tuple[str, str, str]
PROVENANCE_SECTIONS: tuple[_SectionSpec, ...] = (
    ("coverage", "Coverage", "section_coverage"),
    ("validation", "Validation", "validation"),
    ("evals", "Evals", "evals"),
    ("model_eval", "Optimizer", "model_eval"),
    ("ir_coverage", "IR Docs", "ir_coverage"),
    ("source_calls", "Data Cache", "source_calls"),
    ("cron_health", "Cron Health", "cron_health"),
    ("dcf_coverage", "DCF Coverage", "dcf_coverage"),
    ("restatements", "Restatements", "restatements"),
    ("overrides", "Overrides", "overrides"),
    ("credibility", "Credibility", "credibility"),
)


def render_provenance_panel(
    db_path: Path,
    repo_root: Path,
    *,
    user_id: str = DEFAULT_USER_ID,
) -> str:
    """Render the consolidated shell without executing diagnostic builders.

    The arguments remain part of the public renderer contract; the standalone
    routes own their corresponding database/repository inputs.
    """
    del db_path, repo_root, user_id
    nav = "".join(
        f'<button type="button" class="k-chip k-chip-btn k-chip-tab" '
        f'data-prov-jump="prov-{anchor}">{escape(label)}</button>'
        for anchor, label, _panel_id in PROVENANCE_SECTIONS
    )
    toolbar = panel_toolbar(
        "Provenance",
        filters=nav,
        suppress_title=True,
        sticky=True,
    )
    status = (
        '<p class="muted" data-prov-live-status role="status" aria-live="polite">'
        "Preparing live operations sections.</p>"
    )
    body = "".join(
        f'<div class="prov-sec" id="prov-{anchor}">'
        f'<div class="cc-loading" data-prov-section data-prov-label="{escape(label)}" '
        f'data-prov-endpoint="/api/panel/{panel_id}" data-prov-state="loading" '
        'aria-busy="true">'
        '<div class="k-well" role="status">'
        f"<span>Loading {escape(label)}...</span>"
        "</div>"
        "</div></div>"
        for anchor, label, panel_id in PROVENANCE_SECTIONS
    )
    return f'<div class="prov-console">{toolbar}{status}{body}</div><script>{_PROV_NAV_JS}</script>'


# One guarded document-level listener (re-injected fragments never double-wire)
# that scrolls without changing location.hash. The shell uses the hash as its
# panel router, so normal href anchors would navigate away from Provenance.
_PROV_NAV_JS = """
(function () {
  if (!window.__ccProvNav) {
    window.__ccProvNav = true;
    document.addEventListener('click', function (ev) {
      var b = ev.target && ev.target.closest ? ev.target.closest('[data-prov-jump]') : null;
      if (!b) return;
      ev.preventDefault();
      var el = document.getElementById(b.getAttribute('data-prov-jump'));
      if (el) el.scrollIntoView({ behavior: 'smooth', block: 'start' });
    });
  }

  var script = document.currentScript;
  var root = script && script.previousElementSibling;
  if (!root || !root.classList.contains('prov-console')) return;
  var generation = String(Date.now()) + '-' + String(Math.random());
  root.dataset.provGeneration = generation;
  var status = root.querySelector('[data-prov-live-status]');
  var sections = Array.from(root.querySelectorAll('[data-prov-section]'));
  var controllers = new Map();
  var requestSequence = new WeakMap();
  var queued = new Set();
  var retryFocus = new WeakSet();
  var queue = [];
  var activeCount = 0;
  var disposed = false;
  var REQUEST_TIMEOUT_MS = 12000;
  var MAX_CONCURRENT = 3;

  function visible() {
    return !document.hidden && root.isConnected &&
      !root.closest('[hidden], [aria-hidden="true"]');
  }

  function current() {
    return !disposed && root.dataset.provGeneration === generation;
  }

  function announce(message) {
    if (current() && visible() && status) status.textContent = message;
  }

  function fetchedLabel() {
    return 'Fetched ' + new Date().toLocaleString();
  }

  function mount(section, markup, endpoint) {
    if (window.workOsMountHtml) {
      window.workOsMountHtml(section, markup, endpoint);
    } else {
      var endpointUrl = new URL(endpoint, window.location.href);
      if (endpointUrl.origin !== window.location.origin ||
          !endpointUrl.pathname.startsWith('/api/')) {
        throw new Error('Untrusted fragment endpoint');
      }
      section.innerHTML = markup;
      Array.from(section.querySelectorAll('script')).forEach(function (childScript) {
        if (childScript.src) {
          childScript.remove();
          throw new Error('Untrusted fragment script source');
        }
        var replacement = document.createElement('script');
        Array.from(childScript.attributes).forEach(function (attribute) {
          replacement.setAttribute(attribute.name, attribute.value);
        });
        replacement.textContent = childScript.textContent;
        childScript.replaceWith(replacement);
      });
    }
  }

  function showFailure(section, label, state, message, restoreFocus) {
    section.dataset.provState = state;
    section.innerHTML = '<div class="k-well k-well-warn" role="alert">' +
      label + ' ' + message + ' ' +
      '<button type="button" class="k-btn k-btn-quiet k-btn-sm" data-prov-retry>Retry</button></div>';
    announce(label + ' ' + message);
    if (restoreFocus) {
      var retry = section.querySelector('[data-prov-retry]');
      if (retry) retry.focus();
    }
  }

  async function loadSection(section, restoreFocus) {
    if (!current() || !visible()) return;
    var endpoint = section.dataset.provEndpoint;
    var label = section.dataset.provLabel || 'section';
    var sequence = (requestSequence.get(section) || 0) + 1;
    requestSequence.set(section, sequence);
    var controller = new AbortController();
    var requestState = { controller: controller, reason: '', timeoutId: 0, sequence: sequence };
    requestState.timeoutId = window.setTimeout(function () {
      requestState.reason = 'timeout';
      controller.abort();
    }, REQUEST_TIMEOUT_MS);
    controllers.set(section, requestState);
    section.dataset.provState = 'loading';
    section.setAttribute('aria-busy', 'true');
    section.innerHTML = '<div class="k-well" role="status">Loading ' + label + '...</div>';
    announce('Loading ' + label);
    try {
      var response = await fetch(endpoint, {
        signal: controller.signal,
        headers: { Accept: 'text/html' },
        cache: 'no-store'
      });
      if (!current() || !visible() || controllers.get(section) !== requestState) return;
      if (!response.ok) {
        showFailure(section, label, 'unavailable', 'is temporarily unavailable.', restoreFocus);
        return;
      }
      var markup = await response.text();
      if (!current() || !visible() || controllers.get(section) !== requestState) return;
      if (!markup.trim()) {
        section.dataset.provState = 'empty';
        section.innerHTML = '<div class="k-well" role="status">No ' + label + ' data is available.</div>';
      } else {
        try {
          mount(section, markup, endpoint);
          section.dataset.provState = 'loaded';
        } catch (_mountError) {
          showFailure(section, label, 'error', 'could not be rendered.', restoreFocus);
          return;
        }
      }
      var observed = document.createElement('p');
      observed.className = 'muted';
      observed.dataset.provFetched = '';
      observed.textContent = fetchedLabel();
      section.append(observed);
      announce(label + ' response received; ' + fetchedLabel().toLowerCase());
      if (restoreFocus) {
        section.tabIndex = -1;
        section.focus();
      }
    } catch (_fetchError) {
      if (!current() || controllers.get(section) !== requestState ||
          requestState.reason === 'hidden' || requestState.reason === 'unmounted') return;
      var message = requestState.reason === 'timeout'
        ? 'timed out before a response was received.'
        : 'is temporarily unavailable.';
      showFailure(section, label, 'unavailable', message, restoreFocus);
    } finally {
      window.clearTimeout(requestState.timeoutId);
      if (controllers.get(section) === requestState) {
        controllers.delete(section);
        section.removeAttribute('aria-busy');
      }
    }
  }

  root.addEventListener('click', function (ev) {
    var retry = ev.target && ev.target.closest ? ev.target.closest('[data-prov-retry]') : null;
    if (!retry) return;
    var section = retry.closest('[data-prov-section]');
    if (!section || (section.dataset.provState !== 'unavailable' &&
        section.dataset.provState !== 'error')) return;
    retryFocus.add(section);
    section.dataset.provState = 'loading';
    enqueue(section);
    announce('Retrying ' + (section.dataset.provLabel || 'section'));
    pump();
  });

  function enqueue(section) {
    var state = section.dataset.provState;
    if (state !== 'loading' || controllers.has(section) || queued.has(section)) return;
    queued.add(section);
    queue.push(section);
  }

  function summarize() {
    if (!current() || !visible() || activeCount || queue.length) return;
    var unavailable = root.querySelectorAll(
      '[data-prov-state="unavailable"], [data-prov-state="error"]'
    ).length;
    announce(unavailable
      ? sections.length - unavailable + ' live operations sections fetched; ' + unavailable + ' unavailable'
      : 'All live operations sections fetched at ' + new Date().toLocaleTimeString());
  }

  function pump() {
    if (!current() || !visible()) return;
    while (activeCount < MAX_CONCURRENT && queue.length) {
      var section = queue.shift();
      queued.delete(section);
      activeCount += 1;
      var restoreFocus = retryFocus.has(section);
      retryFocus.delete(section);
      loadSection(section, restoreFocus).finally(function () {
        activeCount -= 1;
        pump();
        summarize();
      });
    }
  }

  function abortRequests(reason) {
    controllers.forEach(function (requestState, section) {
      requestState.reason = reason;
      window.clearTimeout(requestState.timeoutId);
      requestState.controller.abort();
      section.dataset.provState = 'loading';
      section.removeAttribute('aria-busy');
    });
    controllers.clear();
  }

  function syncLifecycle() {
    if (!root.isConnected) {
      disposed = true;
      abortRequests('unmounted');
      visibilityObserver.disconnect();
      unmountObserver.disconnect();
      document.removeEventListener('visibilitychange', syncLifecycle);
      return;
    }
    if (!visible()) {
      abortRequests('hidden');
      return;
    }
    sections.forEach(enqueue);
    announce('Loading ' + sections.length + ' live operations sections');
    pump();
  }

  var visibilityObserver = new MutationObserver(syncLifecycle);
  visibilityObserver.observe(document.documentElement, {
    attributes: true,
    attributeFilter: ['hidden', 'aria-hidden'],
    subtree: true
  });
  var mountParent = root.parentElement;
  var unmountObserver = new MutationObserver(syncLifecycle);
  if (mountParent) unmountObserver.observe(mountParent, { childList: true });
  document.addEventListener('visibilitychange', syncLifecycle);
  syncLifecycle();
})();
""".strip()
