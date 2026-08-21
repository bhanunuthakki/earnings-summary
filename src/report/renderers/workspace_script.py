"""Vanilla-JS interaction layer for the workspace renderer.

Wires up the interactions the server-rendered HTML alone can't do: grouped
tab + sub-tab switching (UX9), ``#tab=<id>`` deep links, quarter-card swap,
segment drill-down in the financials levels table. Single ``<script>`` block
inlined by the renderer. Collapses use native <details> (P4.1) — the
drill-down stays JS only because <tr> rows can't nest inside <details>.

Kept dependency-free on purpose so the deliverable stays a single
self-contained HTML doc that opens identically offline, in any browser, years
from now.
"""

from __future__ import annotations

JS = r"""
(function () {
  'use strict';

  // ---- Tab switching (UX9 grouped tabs) -----------------------------------
  // The top bar's .tab[data-tab=<group>] buttons swap the
  // .tab-group-pane[data-tab-group] panes; inside each group a pill row of
  // .subtab[data-subtab=<section>] buttons swaps the .subtab-pane[data-tab]
  // section panes. Section panes keep the legacy per-section data-tab ids so
  // saved comment anchors + cross-links keep resolving after the grouping.
  var topTabs = document.querySelectorAll('.tabs .tab[data-tab]');
  topTabs.forEach(function (btn) {
    btn.addEventListener('click', function () {
      var id = btn.getAttribute('data-tab');
      topTabs.forEach(function (t) {
        t.classList.toggle('active', t === btn);
        if (t === btn) t.setAttribute('aria-current', 'page');
        else t.removeAttribute('aria-current');
      });
      document.querySelectorAll('.tab-group-pane[data-tab-group]').forEach(function (p) {
        p.classList.toggle('active', p.getAttribute('data-tab-group') === id);
      });
    });
  });

  document.querySelectorAll('.tab-group-pane').forEach(function (pane) {
    var pills = pane.querySelectorAll('.subtab[data-subtab]');
    pills.forEach(function (pill) {
      pill.addEventListener('click', function () {
        var id = pill.getAttribute('data-subtab');
        pills.forEach(function (p) {
          p.classList.toggle('active', p === pill);
        });
        pane.querySelectorAll('.subtab-pane[data-tab]').forEach(function (sp) {
          sp.classList.toggle('active', sp.getAttribute('data-tab') === id);
        });
      });
    });
  });

  // Activate a tab by section OR group id. Section ids ("earnings", "bear",
  // ...) resolve through their pill (activating the owning group first);
  // single-section groups reuse the section id as the group id, so the
  // top-bar fallback covers them. Returns true when something matched.
  function activateSection(id) {
    if (!id) return false;
    var pill = document.querySelector('.subtab[data-subtab="' + id + '"]');
    if (pill) {
      var pane = pill.closest('.tab-group-pane');
      var gid = pane ? pane.getAttribute('data-tab-group') : null;
      var groupBtn = gid
        ? document.querySelector('.tabs .tab[data-tab="' + gid + '"]')
        : null;
      if (groupBtn) groupBtn.click();
      pill.click();
      return true;
    }
    var topBtn = document.querySelector('.tabs .tab[data-tab="' + id + '"]');
    if (topBtn) { topBtn.click(); return true; }
    return false;
  }

  // (Q&A accordion: now native <details class="qa-row"> — no JS. P4.1.)

  // ---- Cross-tab links (P4.3) ---------------------------------------------
  // <a data-xtab="bear" data-anchor="panel-failure-modes"> switches to the
  // named section's tab and scrolls the anchor panel into view (or the top
  // when no anchor). Authored by workspace_html._xlink_html.
  document.querySelectorAll('a[data-xtab]').forEach(function (link) {
    link.addEventListener('click', function (ev) {
      ev.preventDefault();
      activateSection(link.getAttribute('data-xtab'));
      var anchorId = link.getAttribute('data-anchor');
      var target = anchorId ? document.getElementById(anchorId) : null;
      if (target) {
        target.scrollIntoView({behavior: 'smooth', block: 'start'});
        target.classList.add('xlink-flash');
        setTimeout(function () { target.classList.remove('xlink-flash'); }, 1600);
      } else {
        var root = document.querySelector('.l1-root');
        if (root) root.scrollTop = 0;
      }
    });
  });

  // ---- Deep links: #tab=<section-or-group id> ------------------------------
  // Old per-section links (#tab=earnings) land on the right group + pill.
  function applyHash() {
    var m = /^#tab=([\w-]+)$/.exec(location.hash || '');
    if (m) activateSection(m[1]);
  }
  window.addEventListener('hashchange', applyHash);
  applyHash();

  // ---- Quarter selector ---------------------------------------------------
  // Quarter labels ("Q1 2026") are shared across groups (earnings + saydo
  // sit side by side; ir lives in another tab) — a click on one group's
  // button broadcasts the same quarter to every sibling group that has a
  // button for it, so switching the quarter in one place keeps every quarter
  // card on the page in sync. A group with no matching button is a silent
  // no-op (it may not cover that quarter).
  var quarterGroups = document.querySelectorAll('[data-quarter-group]');
  function swapQuarterGroup(group, q) {
    var groupId = group.getAttribute('data-quarter-group');
    group.querySelectorAll('button[data-quarter]').forEach(function (b) {
      b.classList.toggle('active', b.getAttribute('data-quarter') === q);
    });
    document
      .querySelectorAll('[data-quarter-card][data-quarter-group="' + groupId + '"]')
      .forEach(function (card) {
        var match = card.getAttribute('data-quarter') === q;
        card.hidden = !match;
      });
  }
  quarterGroups.forEach(function (group) {
    group.querySelectorAll('button[data-quarter]').forEach(function (btn) {
      btn.addEventListener('click', function () {
        var q = btn.getAttribute('data-quarter');
        swapQuarterGroup(group, q);
        quarterGroups.forEach(function (other) {
          if (other === group) return;
          var sibling = other.querySelector('button[data-quarter="' + q + '"]');
          if (sibling) swapQuarterGroup(other, q);
        });
      });
    });
  });

  // ---- Financials line-item drill-down -----------------------------------
  // Click a .fin-row.drillable to toggle the .fin-drill row whose id matches
  // data-drill-target. Updates the ▶ chevron to ▼ when open.
  document.querySelectorAll('.fin-row.drillable').forEach(function (row) {
    row.addEventListener('click', function () {
      var targetId = row.getAttribute('data-drill-target');
      if (!targetId) return;
      var target = document.getElementById(targetId);
      if (!target) return;
      var isOpen = !target.hidden;
      target.hidden = isOpen;
      var chev = row.querySelector('.fin-chev');
      if (chev) chev.textContent = isOpen ? '▶' : '▼';
    });
  });

  // ---- Collapsible persist: <details data-persist="key"> remembers state ---
  // Open/closed state survives reopen via localStorage (works offline). Silent
  // no-op when storage is unavailable (some file:// sandboxes).
  document.querySelectorAll('details[data-persist]').forEach(function (d) {
    var key = 'ws:det:' + d.getAttribute('data-persist');
    try {
      var saved = localStorage.getItem(key);
      if (saved === 'open') d.open = true;
      else if (saved === 'closed') d.open = false;
    } catch (e) {}
    d.addEventListener('toggle', function () {
      try { localStorage.setItem(key, d.open ? 'open' : 'closed'); } catch (e) {}
    });
  });

  // ---- Keyboard shortcuts: j/k panels · / filter · ? help · Esc -----------
  function inField(el) {
    return (
      !!el &&
      (el.tagName === 'INPUT' ||
        el.tagName === 'TEXTAREA' ||
        el.tagName === 'SELECT' ||
        el.isContentEditable)
    );
  }
  function visiblePanels() {
    return Array.prototype.filter.call(document.querySelectorAll('.panel'), function (p) {
      return p.offsetParent !== null;
    });
  }
  function movePanel(dir) {
    var panels = visiblePanels();
    if (!panels.length) return;
    var idx = -1;
    for (var i = 0; i < panels.length; i++) {
      if (panels[i].getBoundingClientRect().top <= 10) idx = i;
    }
    var next = Math.max(0, Math.min(panels.length - 1, idx + dir));
    panels[next].scrollIntoView({ behavior: 'smooth', block: 'start' });
  }
  function focusFilter() {
    var filters = Array.prototype.filter.call(
      document.querySelectorAll('.lg-filter'),
      function (f) {
        return f.offsetParent !== null;
      }
    );
    if (!filters.length) return false;
    var pick = filters[0];
    for (var i = 0; i < filters.length; i++) {
      if (filters[i].getBoundingClientRect().top > 0) {
        pick = filters[i];
        break;
      }
    }
    pick.focus();
    pick.select();
    return true;
  }
  var helpEl = null;
  function buildHelp() {
    if (helpEl) return helpEl;
    helpEl = document.createElement('div');
    helpEl.className = 'ws-kbd-help';
    helpEl.setAttribute('role', 'dialog');
    helpEl.setAttribute('aria-label', 'Keyboard shortcuts');
    helpEl.innerHTML =
      '<div class="ws-kbd-surface"><div class="ws-kbd-title">Keyboard shortcuts</div>' +
      '<dl class="ws-kbd-list">' +
      '<dt>j / k</dt><dd>next / previous panel</dd>' +
      '<dt>/</dt><dd>focus the table filter</dd>' +
      '<dt>?</dt><dd>toggle this help</dd>' +
      '</dl><div class="ws-kbd-hint">click or ? to close</div></div>';
    helpEl.addEventListener('click', function () {
      hideHelp();
    });
    document.body.appendChild(helpEl);
    return helpEl;
  }
  function showHelp() {
    buildHelp().classList.add('is-open');
  }
  function hideHelp() {
    if (helpEl) helpEl.classList.remove('is-open');
  }
  function helpOpen() {
    return !!helpEl && helpEl.classList.contains('is-open');
  }
  // NB: Escape is intentionally NOT handled here — CCOverlay owns the single
  // Escape handler for this document (one-Escape design law). The help overlay
  // closes on click or a second ? press.
  document.addEventListener('keydown', function (ev) {
    if (ev.defaultPrevented || ev.metaKey || ev.ctrlKey || ev.altKey) return;
    if (inField(document.activeElement)) return;
    if (ev.key === 'j') {
      ev.preventDefault();
      movePanel(1);
    } else if (ev.key === 'k') {
      ev.preventDefault();
      movePanel(-1);
    } else if (ev.key === '/') {
      if (focusFilter()) ev.preventDefault();
    } else if (ev.key === '?') {
      ev.preventDefault();
      if (helpOpen()) hideHelp();
      else showHelp();
    }
  });

  // ---- Initial highlight: ensure the first top tab is active if none set --
  var anyActive = document.querySelector('.tabs .tab.active');
  if (!anyActive && topTabs.length) topTabs[0].click();

  // ---- Sidebar collapse (BHA-71: compact, collapsible navigation rail) -----
  // Toggles .sidebar-collapsed on .report-sidebar; persists to sessionStorage
  // so reopening the same report in the same session preserves the preference.
  // Arrow-key navigation between nav items follows ARIA listbox pattern.
  var sidebar = document.querySelector('.report-sidebar');
  var toggleBtn = document.getElementById('report-sidebar-toggle');
  var SS_KEY = 'report:sidebar:collapsed';
  function applySidebarCollapsed(collapsed) {
    if (!sidebar) return;
    sidebar.classList.toggle('sidebar-collapsed', collapsed);
    if (toggleBtn) {
      var label = collapsed ? 'Expand navigation' : 'Collapse navigation';
      toggleBtn.setAttribute('aria-expanded', collapsed ? 'false' : 'true');
      toggleBtn.setAttribute('aria-label', label);
      toggleBtn.setAttribute('title', label);
    }
  }
  // Restore from sessionStorage
  try {
    var saved = sessionStorage.getItem(SS_KEY);
    if (saved === '1' || (saved === null && window.matchMedia('(max-width: 768px)').matches)) {
      applySidebarCollapsed(true);
    }
  } catch (e) {}
  if (toggleBtn) {
    toggleBtn.addEventListener('click', function () {
      var isCollapsed = sidebar ? sidebar.classList.contains('sidebar-collapsed') : false;
      applySidebarCollapsed(!isCollapsed);
      try { sessionStorage.setItem(SS_KEY, !isCollapsed ? '1' : '0'); } catch (e) {}
    });
  }
  // Arrow-key navigation within sidebar nav items (accessibility)
  if (sidebar) {
    sidebar.addEventListener('keydown', function (ev) {
      var items = Array.prototype.slice.call(sidebar.querySelectorAll('.k-nav-item'));
      if (!items.length) return;
      var focused = document.activeElement;
      var idx = items.indexOf(focused);
      if (ev.key === 'ArrowDown') {
        ev.preventDefault();
        var next = (idx + 1) % items.length;
        items[next].focus();
      } else if (ev.key === 'ArrowUp') {
        ev.preventDefault();
        var prev = (idx - 1 + items.length) % items.length;
        items[prev].focus();
      }
    });
  }
})();
"""

__all__ = ["JS"]
