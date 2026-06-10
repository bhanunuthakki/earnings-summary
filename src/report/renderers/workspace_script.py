"""Vanilla-JS interaction layer for the workspace renderer.

Wires up the interactions the server-rendered HTML alone can't do: tab
switching, quarter-card swap, segment drill-down in the financials levels
table. Single ``<script>`` block inlined by the renderer. Collapses use
native <details> (P4.1) — the drill-down stays JS only because <tr> rows
can't nest inside <details>.

Kept dependency-free on purpose so the deliverable stays a single
self-contained HTML doc that opens identically offline, in any browser, years
from now.
"""

from __future__ import annotations

JS = r"""
(function () {
  'use strict';

  // ---- Tab switching ------------------------------------------------------
  document.querySelectorAll('.tab[data-tab]').forEach(function (btn) {
    btn.addEventListener('click', function () {
      var id = btn.getAttribute('data-tab');
      document.querySelectorAll('.tab[data-tab]').forEach(function (t) {
        t.classList.toggle('active', t === btn);
      });
      document.querySelectorAll('.tab-pane[data-tab]').forEach(function (p) {
        p.classList.toggle('active', p.getAttribute('data-tab') === id);
      });
    });
  });

  // (Q&A accordion: now native <details class="qa-row"> — no JS. P4.1.)

  // ---- Cross-tab links (P4.3) ---------------------------------------------
  // <a data-xtab="bear" data-anchor="panel-failure-modes"> switches to the
  // named tab and scrolls the anchor panel into view (or the top when no
  // anchor). Authored by workspace_html._xlink_html.
  document.querySelectorAll('a[data-xtab]').forEach(function (link) {
    link.addEventListener('click', function (ev) {
      ev.preventDefault();
      var tabBtn = document.querySelector('.tab[data-tab="' + link.getAttribute('data-xtab') + '"]');
      if (tabBtn) tabBtn.click();
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

  // ---- Initial highlight: ensure the first tab is active if none set ------
  var anyActive = document.querySelector('.tab.active');
  if (!anyActive) {
    var first = document.querySelector('.tab[data-tab]');
    if (first) first.click();
  }
})();
"""

__all__ = ["JS"]
