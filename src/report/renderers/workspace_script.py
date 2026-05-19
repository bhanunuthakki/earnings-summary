"""Vanilla-JS interaction layer for the workspace renderer.

Wires up the interactions the server-rendered HTML alone can't do: tab
switching, Q&A accordion, quarter-card swap, segment drill-down in the
financials levels table. Single ``<script>`` block inlined by the renderer.

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

  // ---- Q&A accordion ------------------------------------------------------
  document.querySelectorAll('.qa-head').forEach(function (head) {
    head.addEventListener('click', function () {
      var row = head.closest('.qa-row');
      if (!row) return;
      var open = row.classList.toggle('open');
      var chev = head.querySelector('.qa-chev');
      if (chev) chev.textContent = open ? '-' : '+';
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
