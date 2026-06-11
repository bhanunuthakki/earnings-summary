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
  document.querySelectorAll('[data-quarter-group]').forEach(function (group) {
    var groupId = group.getAttribute('data-quarter-group');
    group.querySelectorAll('button[data-quarter]').forEach(function (btn) {
      btn.addEventListener('click', function () {
        var q = btn.getAttribute('data-quarter');
        group.querySelectorAll('button[data-quarter]').forEach(function (b) {
          b.classList.toggle('active', b === btn);
        });
        document
          .querySelectorAll('[data-quarter-card][data-quarter-group="' + groupId + '"]')
          .forEach(function (card) {
            var match = card.getAttribute('data-quarter') === q;
            card.style.display = match ? '' : 'none';
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
      var isOpen = target.style.display !== 'none';
      target.style.display = isOpen ? 'none' : '';
      var chev = row.querySelector('.fin-chev');
      if (chev) chev.textContent = isOpen ? '▶' : '▼';
    });
  });

  // ---- Initial highlight: ensure the first top tab is active if none set --
  var anyActive = document.querySelector('.tabs .tab.active');
  if (!anyActive && topTabs.length) topTabs[0].click();
})();
"""

__all__ = ["JS"]
