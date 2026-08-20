"""Durable Work OS Copilot handoff for standalone and embedded reports.

Reports no longer own a second chat thread or proposal mutation path. The
small report-side surface passes ticker, report date, and an optional fact
reference to the production Copilot controller when it is available, with a
same-origin Work OS link as the standalone fallback.
"""

from report.renderers.workspace_styles import CHAT_CSS
from ui.cite_marks import CITE_MARKS_JS

JS = (
    CITE_MARKS_JS
    + r"""
(function () {
  'use strict';

  function init() {
    var boot = window.__workspaceCommentBoot;
    if (!boot) {
      window.setTimeout(init, 100);
      return;
    }
    var SERVER_URL = /^https?:$/.test(window.location.protocol)
      ? window.location.origin
      : (boot.server_url || 'http://localhost:7421');
    var TICKER = String(boot.ticker || '').toUpperCase();
    var REPORT_DATE = String(boot.report_date || '');
    var sidebar = document.getElementById('chat-sidebar');
    var toggle = document.getElementById('chat-toggle');
    var handoff = document.getElementById('chat-open-copilot');
    if (!sidebar || !toggle || !handoff) return;

    var launchQuery = new URLSearchParams({
      copilot: '1', ticker: TICKER, report_date: REPORT_DATE,
      origin_key: 'report:' + TICKER + ':' + REPORT_DATE
    });
    var launchUrl = SERVER_URL + '/?' + launchQuery.toString() + '#screen-workspace';
    handoff.href = launchUrl;
    handoff.textContent = 'Open in Copilot';

    function openDurableCopilot(context) {
      var payload = Object.assign({
        company_ticker: TICKER,
        category: 'research',
        report_date: REPORT_DATE,
        origin_key: 'report:' + TICKER + ':' + REPORT_DATE,
        coverage_role_at_creation: 'unknown',
        lifecycle_at_creation: 'unknown'
      }, context || {});
      try {
        if (window.parent !== window && typeof window.parent.openWorkOsCopilot === 'function') {
          window.parent.openWorkOsCopilot(payload);
          return true;
        }
      } catch (_) { /* cross-origin embeds use the clear Work OS link */ }
      if (typeof window.openWorkOsCopilot === 'function') {
        window.openWorkOsCopilot(payload);
        return true;
      }
      return false;
    }

    function applyOpen(open) {
      sidebar.setAttribute('aria-hidden', open ? 'false' : 'true');
      sidebar.classList.toggle('open', open);
      toggle.classList.toggle('open', open);
      document.documentElement.classList.toggle('chat-sidebar-open', open);
    }

    var chatOv = window.CCOverlay && window.CCOverlay.register(sidebar, {
      modal: true, priority: 30, scrim: false, trapFocus: false, restoreFocus: true,
      motion: 'none', toggleHidden: false, autofocus: false,
      group: 'report-sidebar', closeId: 'chat-close', wireClose: false,
      onOpen: function () { applyOpen(true); },
      onClose: function () { applyOpen(false); }
    });
    function setOpen(open) {
      if (!chatOv) { applyOpen(open); return; }
      if (open) chatOv.open(); else chatOv.close();
    }

    var embedded = false;
    try { embedded = window.self !== window.top; } catch (_) { embedded = true; }
    if (embedded) {
      var launcher = document.getElementById('chat-drawer');
      if (launcher) launcher.hidden = true;
      handoff.target = '_top';
    }

    toggle.addEventListener('click', function () {
      if (!openDurableCopilot({})) setOpen(sidebar.getAttribute('aria-hidden') === 'true');
    });
    sidebar.querySelector('.chat-close').addEventListener('click', function () { setOpen(false); });
    handoff.addEventListener('click', function (event) {
      if (!openDurableCopilot({})) return;
      event.preventDefault();
      setOpen(false);
    });

    document.addEventListener('click', function (event) {
      var doorway = event.target.closest && event.target.closest('.fact-doorway');
      if (!doorway) return;
      var host = doorway.closest('[data-fact-ref]');
      var factRef = host && host.getAttribute('data-fact-ref');
      if (!factRef) return;
      var label = (doorway.textContent || '').replace(/\s+/g, ' ').trim();
      if (openDurableCopilot({
        fact_ref: factRef,
        prompt: label ? ('Review ' + label + ' with its governed evidence.') : 'Review this governed fact.'
      })) event.preventDefault();
    });
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
})();
"""
)

CSS = CHAT_CSS
