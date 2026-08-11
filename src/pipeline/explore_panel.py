"""Fact Playground panel with deterministic views and Copilot handoff.

Research prompts open the sole Work OS Copilot conversation surface.  This
panel never calls an LLM or renders a second chat thread.  Its local builder
remains deterministic: users can compile or assemble a validated ViewSpec,
execute it, inspect provenance, and save the resulting view.

Original P5.1 builder notes:

On-the-fly slice-and-dice over financial_facts / kpi_facts / segments:
pick tickers, pick metrics from the live catalog, pick a transform
(level / YoY / CAGR / margin) and cadence, run — the result matrix (with
per-number provenance chips) and chart render instantly and LLM-free.
Views save by name (saved_views, 0079) and re-run from their chips; the
same fragments embed anywhere via ``GET /api/views/<id>/fragment``.

Served by ``/api/panel/explore`` (lazy command-center tab).
``?fragment=views`` returns just the saved-view chip strip — the panel's
JS refreshes that after save/delete. Execution goes through
``POST /api/viewspec/run``; the catalog reloads from
``GET /api/viewspec/catalog`` when the ticker set changes.

The natural-language box (P5.2) rides on top: ``POST /api/viewspec/compile``
turns a question into a spec via a fast model and ``applySpec`` populates
the builder and runs it — so every compile lands in the SAME validated
spec the pickers build, never raw SQL. A failed or budget-skipped compile
just reports itself in the message slot; the builder keeps working.
"""

from __future__ import annotations

import json
import sqlite3
from html import escape
from pathlib import Path

from dcf.fact_drivers import driver_field_options
from identity import DEFAULT_USER_ID
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite
from user_state.saved_views import SavedViewRow, list_views
from viewspec.engine import metric_catalog
from viewspec.spec import CADENCES, TRANSFORMS

_PANEL_STYLE = """<style>
.vx-builder { border-radius:var(--radius); background:var(--surface);
  padding:12px 14px; margin:4px 0 12px; }
.vx-row { display:flex; gap:8px; align-items:center; flex-wrap:wrap; margin-bottom:10px; }
.vx-row label { color:var(--muted); font-size:var(--fs-caption); }
/* Inputs/selects: skinned by the shared control kit (ui/controls.py) —
   only layout lives here. */
.vx-row input[name="tickers"] { width:260px; text-transform:uppercase; }
.vx-row input[name="periods"], .vx-row input[name="cagr_years"] { width:54px; }
.vx-row input[name="view_name"] { width:200px; }
.vx-pickers { display:grid; grid-template-columns:repeat(3, minmax(180px, 1fr)); gap:10px;
  margin-bottom:10px; }
.vx-picker { display:flex; flex-direction:column; }
.vx-picker label { display:block; color:var(--muted); font-size:var(--fs-caption); margin-bottom:3px;
  text-transform:uppercase; letter-spacing:.06em; }
.vx-picker select { width:100%; }
/* Type-ahead filter: a huge per-ticker fact list (capture-every-number long
   tail) stays usable. Skinned by the control kit; only layout lives here. */
.vx-pick-search { width:100%; margin-bottom:4px; }
.vx-pick-count { color:var(--muted); font-size:var(--fs-caption); margin-top:3px; min-height:1.1em; }
/* ---- Key-metrics preselect bubbles (key_metrics_picker.md) ---- */
.vx-keymetrics { display:flex; align-items:baseline; gap:8px; flex-wrap:wrap; margin-bottom:10px; }
.vx-keymetrics:empty { display:none; }
.vx-km-label { color:var(--muted); font-size:var(--fs-caption); text-transform:uppercase;
  letter-spacing:.06em; white-space:nowrap; }
.vx-km-chips { display:flex; gap:6px; flex-wrap:wrap; }
.vx-saved-strip { display:flex; gap:6px; align-items:center; flex-wrap:wrap; }
.vx-saved { display:inline-flex; align-items:center; gap:4px; }
.vx-none { color:var(--muted); font-size:var(--fs-caption); }
.vx-error { color:var(--bad); font-size:var(--fs-body); margin:6px 0; }
/* Inject-one-fact → DCF driver (S6): a single picked fact, single ticker. */
.vx-inject select { min-width:240px; }
.vx-inject-ok { color:var(--fg); font-size:var(--fs-body); line-height:1.5;
  border-left:3px solid var(--ok); padding:4px 0 4px 10px; margin:6px 0; }
.vx-inject-ok strong { color:var(--ok); }
.vx-hint { color:var(--muted); font-size:var(--fs-caption); margin-top:10px; }
.vx-nl { border-bottom:1px solid var(--border); padding-bottom:10px; }
.vx-nl input[name="nl_query"] { flex:1; min-width:280px; }
.vx-nl-msg { color:var(--muted); font-size:var(--fs-caption); }

/* ---- Copilot prompt handoff + deterministic builder ---- */
.ask-thread { display:flex; flex-direction:column; gap:12px; margin:4px 0 14px; }
.ask-hello { color:var(--muted); font-size:var(--fs-body); line-height:1.5; border:1px dashed var(--border);
  border-radius:var(--radius); padding:14px 16px; }
.ask-chips { display:flex; gap:8px; flex-wrap:wrap; margin-top:10px; }
.ask-inputrow { display:flex; gap:8px; align-items:center; margin-bottom:10px; }
/* No font-size here: the input inherits the kit baseline (--fs-body, 13px) so it
   matches the .k-btn buttons beside it, and the mobile 16px floor (controls.py)
   is no longer overridden. The Ask/DIY buttons are .k-btn (primary/quiet) — no
   bespoke .ask-inputrow button rule. */
.ask-inputrow input { flex:1; padding:9px 13px; }
.ask-builder-pop { border:1px solid var(--border); border-radius:var(--radius); background:var(--surface);
  padding:0 14px 12px; margin-top:10px; box-shadow:var(--shadow-pop); }
.ask-pop-head { display:flex; justify-content:space-between; align-items:center; padding:10px 0;
  font-size:var(--fs-body); font-weight:600; color:var(--fg); }
/* the close glyph (§3): a NAMED, styled control — not raw descendant-selector
   chrome — matching the cc-peek-close / cc-drawer-close treatment. */
.ask-pop-close { background:transparent; border:none; color:var(--muted); font-size:var(--fs-display);
  cursor:pointer; padding:0 4px; }
.ask-pop-close:hover { color:var(--fg); }
.ask-advanced .vx-builder { border:none; padding-left:0; padding-right:0; margin-top:0; }
</style>"""

# Plain string (not an f-string) so braces pass through untouched; the panel
# assembler drops it into one <script> tag. All state lives in the DOM.
_PANEL_JS = """
(function () {
  var root = document.getElementById('vx-root');
  if (!root || root.dataset.wired) return;
  root.dataset.wired = '1';
  function el(id) { return document.getElementById(id); }
  function escapeHtml(value) {
    return String(value).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  }
  function tickers() {
    return el('vx-tickers').value.split(',').map(function (s) {
      return s.trim().toUpperCase();
    }).filter(Boolean);
  }
  // ---- Type-ahead pickers: the per-ticker fact list can run to many hundreds
  // of entries (capture-every-number long tail), so each picker filters as you
  // type. Selection is tracked in a per-select token map (not the live <option>
  // .selected flags) so a pick survives being filtered out of view — the map is
  // the source of truth selectedTokens() reads. ----
  var PICKERS = ['vx-pick-fin', 'vx-pick-kpi', 'vx-pick-seg'];
  function pickerState(id) {
    var sel = el(id);
    if (!sel) return null;
    if (!sel._entries) {
      // Hydrate from the server-rendered options (works before any reload).
      sel._entries = [];
      sel._selected = {};
      for (var i = 0; i < sel.options.length; i++) {
        var o = sel.options[i];
        sel._entries.push({
          token: o.value, label: o.textContent, title: o.title || '',
          origin: o.getAttribute('data-origin') || '',
          override: o.getAttribute('data-override') === '1'
        });
        if (o.selected) sel._selected[o.value] = true;
      }
    }
    return sel;
  }
  function updateCount(id) {
    var sel = el(id);
    var cEl = el(id + '-count');
    if (!sel || !sel._entries || !cEl) return;
    var qEl = el(id + '-q');
    var q = ((qEl && qEl.value) || '').trim().toLowerCase();
    var total = sel._entries.length;
    var shown = total;
    if (q) {
      shown = sel._entries.filter(function (e) {
        return e.label.toLowerCase().indexOf(q) !== -1 ||
               e.token.toLowerCase().indexOf(q) !== -1;
      }).length;
    }
    var nsel = Object.keys(sel._selected).length;
    cEl.textContent = (q ? shown + ' of ' + total : String(total)) +
      (nsel ? ' \\u00b7 ' + nsel + ' picked' : '');
  }
  function renderPicker(id) {
    var sel = pickerState(id);
    if (!sel) return;
    var qEl = el(id + '-q');
    var q = ((qEl && qEl.value) || '').trim().toLowerCase();
    sel.innerHTML = '';
    sel._entries.forEach(function (e) {
      if (q && e.label.toLowerCase().indexOf(q) === -1 &&
              e.token.toLowerCase().indexOf(q) === -1) return;
      var opt = document.createElement('option');
      opt.value = e.token;
      opt.textContent = e.label;
      if (e.title) opt.title = e.title;
      if (e.origin) opt.setAttribute('data-origin', e.origin);
      if (e.override) opt.setAttribute('data-override', '1');
      if (sel._selected[e.token]) opt.selected = true;
      sel.appendChild(opt);
    });
    updateCount(id);
  }
  function syncSelection(id) {
    var sel = el(id);
    if (!sel || !sel._selected) return;
    // Only the rendered options reflect this interaction; filtered-out picks
    // stay in the map untouched (that's the whole point).
    for (var i = 0; i < sel.options.length; i++) {
      var o = sel.options[i];
      if (o.selected) sel._selected[o.value] = true;
      else delete sel._selected[o.value];
    }
  }
  function selectedTokens() {
    var out = [];
    PICKERS.forEach(function (id) {
      var sel = el(id);
      if (sel && sel._selected) {
        for (var tok in sel._selected) {
          if (Object.prototype.hasOwnProperty.call(sel._selected, tok)) out.push(tok);
        }
      }
    });
    return out;
  }
  function buildSpec() {
    return {
      tickers: tickers(),
      metrics: selectedTokens(),
      transform: el('vx-transform').value,
      cadence: el('vx-cadence').value,
      periods: parseInt(el('vx-periods').value, 10) || 12,
      cagr_years: parseInt(el('vx-cagr-years').value, 10) || 3
    };
  }
  function showError(msg) {
    el('vx-result').innerHTML = '<div class="vx-error">' + msg + '</div>';
  }
  function fillPicker(id, entries, keep) {
    var sel = el(id);
    if (!sel) return;
    var present = {};
    sel._entries = (entries || []).map(function (e) {
      present[e.token] = true;
      return {
        token: e.token,
        label: e.label + (e.tickers > 1 ? ' (' + e.tickers + ')' : ''),
        title: e.title || '', origin: e.origin || '', override: !!e.override_only
      };
    });
    // Reset selection to the requested keep set (only tokens that exist now).
    sel._selected = {};
    (keep || []).forEach(function (t) { if (present[t]) sel._selected[t] = true; });
    renderPicker(id);
  }
  function loadCatalog(preselect, then) {
    var qs = new URLSearchParams({tickers: tickers().join(',')});
    fetch('/api/viewspec/catalog?' + qs).then(function (r) { return r.json(); })
      .then(function (cat) {
        fillPicker('vx-pick-fin', cat.fin, preselect);
        fillPicker('vx-pick-kpi', cat.kpi, preselect);
        fillPicker('vx-pick-seg', cat.seg, preselect);
        refreshKeyMetrics();
        if (then) then();
      });
  }

  // ---- Key-metrics preselect bubbles (key_metrics_picker.md): a click on a
  // chip toggles that metric in the right picker (routed by the token's domain
  // prefix), so the most important metrics are one tap away. Selection rides the
  // picker's `_selected` token map — the source of truth selectedTokens() reads
  // (S5) — NOT the live <option> flags, so a chip pick still registers even when
  // a search filter has scrolled that option out of view. The chip row re-fetches
  // (?fragment=keymetrics) whenever the ticker set changes and re-marks chips
  // whose token is already selected. ----
  function kmSelectId(token) {
    if (token.indexOf('fin:') === 0) return 'vx-pick-fin';
    if (token.indexOf('kpi:') === 0) return 'vx-pick-kpi';
    if (token.indexOf('seg:') === 0) return 'vx-pick-seg';
    return null;
  }
  function kmIsSelected(token) {
    var id = kmSelectId(token);
    var sel = id && pickerState(id);
    return !!(sel && sel._selected && sel._selected[token]);
  }
  function kmToggleToken(token) {
    var id = kmSelectId(token);
    var sel = id && pickerState(id);
    if (!sel) return false;
    // Only honor tokens the picker actually carries (so a stale chip is inert).
    var known = (sel._entries || []).some(function (e) { return e.token === token; });
    if (!known) return false;
    if (sel._selected[token]) delete sel._selected[token];
    else sel._selected[token] = true;
    renderPicker(id);  // reflect in the visible <option>s + the picked count
    return true;
  }
  function syncKmChips() {
    var box = el('vx-keymetrics');
    if (!box) return;
    var chips = box.querySelectorAll('.km-chip');
    for (var i = 0; i < chips.length; i++) {
      chips[i].classList.toggle('is-on', kmIsSelected(chips[i].getAttribute('data-km-token')));
    }
  }
  function refreshKeyMetrics() {
    var box = el('vx-keymetrics');
    if (!box) return;
    var qs = new URLSearchParams({tickers: tickers().join(','), fragment: 'keymetrics'});
    fetch('/api/panel/explore?' + qs).then(function (r) { return r.text(); })
      .then(function (h) { box.innerHTML = h; syncKmChips(); })
      .catch(function () {});
  }
  var kmBox = el('vx-keymetrics');
  if (kmBox) kmBox.addEventListener('click', function (ev) {
    var chip = ev.target.closest('.km-chip');
    if (!chip) return;
    var tok = chip.getAttribute('data-km-token');
    if (!tok || !kmToggleToken(tok)) return;
    chip.classList.toggle('is-on', kmIsSelected(tok));
  });
  syncKmChips();
  function runView() {
    var spec = buildSpec();
    if (!spec.tickers.length) { showError('Add at least one ticker.'); return; }
    if (!spec.metrics.length) { showError('Pick at least one metric.'); return; }
    var btn = el('vx-run');
    CCAction.busy(btn, 'Running\\u2026');
    el('vx-result').innerHTML = '<div class="vx-none">Running\\u2026</div>';
    fetch('/api/viewspec/run', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({spec: spec})
    }).then(function (r) {
      if (r.ok) return r.text().then(function (h) { CCAction.release(btn); el('vx-result').innerHTML = h; });
      return r.json().then(function (e) { CCAction.release(btn); showError(e.error || ('HTTP ' + r.status)); });
    }).catch(function () { CCAction.release(btn); showError('network error — try again'); });
  }
  function refreshSaved() {
    fetch('/api/panel/explore?fragment=views').then(function (r) { return r.text(); })
      .then(function (h) { el('vx-saved-strip').innerHTML = h; });
  }
  function applySpec(spec) {
    el('vx-tickers').value = (spec.tickers || []).join(', ');
    el('vx-transform').value = spec.transform || 'level';
    el('vx-cadence').value = spec.cadence || 'quarterly';
    el('vx-periods').value = spec.periods || 12;
    el('vx-cagr-years').value = spec.cagr_years || 3;
    var tokens = (spec.metrics || []).map(function (m) {
      if (typeof m === 'string') return m;
      if (m.domain === 'seg') return 'seg:' + m.dim_type + ':' + m.dim_name + ':' + m.key;
      return m.domain + ':' + m.key;
    });
    loadCatalog(tokens, runView);
  }
  function compileNL() {
    var q = el('vx-nl-q').value.trim();
    var msg = el('vx-nl-msg');
    if (!q) { msg.textContent = 'Type a question first.'; return; }
    var btn = el('vx-nl-go');
    CCAction.busy(btn);
    msg.textContent = 'compiling…';
    fetch('/api/viewspec/compile', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({query: q, tickers: tickers()})
    }).then(function (r) { return r.json(); }).then(function (res) {
      CCAction.release(btn);
      if (res.status === 'ok' && res.spec) {
        msg.textContent = 'compiled — builder updated';
        applySpec(res.spec);
      } else {
        msg.textContent = res.message || res.error || 'compile failed — use the builder';
      }
    }).catch(function () {
      CCAction.release(btn);
      msg.textContent = 'compile failed — use the builder';
    });
  }
  el('vx-nl-go').addEventListener('click', compileNL);
  el('vx-nl-q').addEventListener('keydown', function (ev) {
    if (ev.key === 'Enter') { ev.preventDefault(); compileNL(); }
  });
  el('vx-load-metrics').addEventListener('click', function () { loadCatalog(); });
  el('vx-run').addEventListener('click', runView);

  // Wire each picker: hydrate from the server-rendered options, filter on
  // keystroke, and keep the selection map synced when the user (de)selects.
  PICKERS.forEach(function (id) {
    pickerState(id);
    renderPicker(id);
    var qEl = el(id + '-q');
    if (qEl) qEl.addEventListener('input', function () { renderPicker(id); });
    var sel = el(id);
    if (sel) sel.addEventListener('change', function () {
      syncSelection(id); updateCount(id);
    });
  });

  // ---- DIY builder popover (Ask v4): the builder opens on demand from the
  // DIY button / "Open in builder" instead of living as a bottom fold.
  // CCOverlay registration (Law 3 / design_language §3.1): this used to carry
  // its OWN document-level keydown Escape listener — the last per-surface
  // Escape in the shell, and one that ALWAYS fired regardless of whatever
  // else (a peek, the palette) was open on top. Registering it instead puts
  // it on the shared priority ladder; ``priority`` is left UNSET (defaults to
  // 0), the one rung below window.CCOverlay.PRIORITY.DOCK (10) — a scoped,
  // scrimless, in-panel popover never outranks a shell overlay for Escape,
  // it only claims Escape when nothing higher on the ladder (palette / peek /
  // drawer / dock) is open. closeId auto-wires the (x); wireClose default
  // stays on so no separate click listener is needed either. ----
  var builderPop = el('ask-advanced');
  var builderOv = window.CCOverlay && builderPop && window.CCOverlay.register(builderPop, {
    scrim: false, trapFocus: false, restoreFocus: false, autofocus: false,
    motion: 'none', closeId: 'ask-pop-close'
  });
  function openBuilder() {
    if (!builderPop) return;
    if (builderOv) builderOv.open(); else builderPop.hidden = false;
    builderPop.scrollIntoView({behavior: 'smooth', block: 'nearest'});
  }
  function closeBuilder() {
    if (!builderPop) return;
    if (builderOv) builderOv.close(); else builderPop.hidden = true;
  }
  var diyBtn = el('ask-diy');
  if (diyBtn) diyBtn.addEventListener('click', function () {
    if (builderPop && builderPop.hidden) openBuilder(); else closeBuilder();
  });

  // ---- Scored peers (Ask v4): /api/peers/<T> serves the PR 400 peer
  // scoring; "+ Peers" widens a view to the comparable set. ----
  function fetchPeers(base, cb) {
    fetch('/api/peers/' + encodeURIComponent(base))
      .then(function (r) { return r.json(); })
      .then(function (res) {
        cb((res.peers || []).map(function (p) { return p.ticker; }));
      })
      .catch(function () { cb([]); });
  }
  var vxPeers = el('vx-peers');
  if (vxPeers) vxPeers.addEventListener('click', function () {
    var cur = tickers();
    if (!cur.length) { showError('Add a base ticker first.'); return; }
    CCAction.busy(vxPeers);
    fetchPeers(cur[0], function (peers) {
      CCAction.release(vxPeers);
      if (!peers.length) { showError('No scored peers for ' + cur[0] + '.'); return; }
      var merged = cur.slice();
      peers.forEach(function (p) { if (merged.indexOf(p) === -1) merged.push(p); });
      el('vx-tickers').value = merged.slice(0, 16).join(', ');
      loadCatalog(selectedTokens());
    });
  });
  el('vx-save').addEventListener('click', function () {
    var name = el('vx-view-name').value.trim();
    if (!name) { showError('Name the view before saving.'); return; }
    var spec = buildSpec();
    if (!spec.metrics.length) { showError('Pick at least one metric.'); return; }
    var saveBtn = el('vx-save');
    CCAction.busy(saveBtn, 'Saving\\u2026');
    fetch('/api/views', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({name: name, spec: spec})
    }).then(function (r) {
      CCAction.release(saveBtn);
      if (r.ok) { refreshSaved(); }
      else { r.json().then(function (e) { showError(e.error || 'save failed'); }); }
    }).catch(function () {
      CCAction.release(saveBtn);
      showError('network error — try again');
    });
  });

  // ---- Inject one picked fact as a DCF driver (S6). A single fact + single
  // ticker + a target driver field; the server resolves the latest value
  // (override-aware), converts units/scale, sanity-bounds, and commits via the
  // clobber-safe apply_edits path. ----
  function fmtNum(v) {
    if (v === null || v === undefined || isNaN(v)) return String(v);
    var a = Math.abs(v);
    if (a !== 0 && a < 0.01) return v.toFixed(4);
    if (a < 1) return v.toFixed(3);
    if (a < 1000) return (Math.round(v * 100) / 100).toString();
    return Math.round(v).toLocaleString();
  }
  function renderInjection(res) {
    var inj = res.injection || {};
    var fv = res.fair_value_per_share_usd;
    var wacc = res.wacc;
    var rawUnit = inj.raw_unit ? (' ' + inj.raw_unit) : '';
    var conv = inj.conversion ? (' \\u00b7 ' + inj.conversion) : '';
    var html = '<div class="vx-inject-ok"><strong>Injected into ' + escapeHtml(inj.ticker)
      + ' DCF.</strong> ' + escapeHtml(inj.metric_label) + ' = ' + fmtNum(inj.raw_value) + rawUnit
      + ' \\u2192 ' + escapeHtml(inj.field_label) + ' = ' + fmtNum(inj.applied_value) + conv + '<br>'
      + 'Repriced: fair value ' + (fv !== null && fv !== undefined ? '$' + fmtNum(fv) : 'n/a')
      + (wacc !== null && wacc !== undefined ? ' \\u00b7 WACC ' + (wacc * 100).toFixed(1) + '%' : '')
      + (inj.fact_id !== null && inj.fact_id !== undefined ? ' \\u00b7 from fact #' + inj.fact_id : '')
      + (inj.period_end ? ' \\u00b7 as of ' + escapeHtml(inj.period_end) : '')
      + '</div>';
    el('vx-result').innerHTML = html;
  }
  var injectBtn = el('vx-inject-dcf');
  if (injectBtn) injectBtn.addEventListener('click', function () {
    var ts = tickers();
    if (ts.length !== 1) {
      showError('Injecting a DCF driver targets one company — narrow to exactly one ticker.');
      return;
    }
    var toks = selectedTokens();
    if (toks.length !== 1) { showError('Pick exactly one fact to inject as a DCF driver.'); return; }
    var fieldSel = el('vx-inject-field');
    CCAction.busy(injectBtn, 'Injecting\\u2026');
    el('vx-result').innerHTML = '<div class="vx-none">Injecting\\u2026</div>';
    fetch('/api/dcf/inject-fact', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ticker: ts[0], token: toks[0], field: fieldSel ? fieldSel.value : ''})
    }).then(function (r) {
      return r.json().then(function (res) { return {ok: r.ok, res: res}; });
    }).then(function (o) {
      CCAction.release(injectBtn);
      if (!o.ok) { showError((o.res && o.res.error) || 'injection failed'); return; }
      renderInjection(o.res);
    }).catch(function () {
      CCAction.release(injectBtn);
      showError('network error — try again');
    });
  });

  // ---- Park one picked fact on the DCF reference sheet (S7). A single fact +
  // single ticker; the server resolves the latest value (override-aware) and
  // writes it — native unit, value + period + source — into the companion
  // dcf/facts/<T>.xlsx the refresh never rebuilds, so it survives every model
  // refresh. Reference-only: no driver field, no reprice. ----
  function renderReference(res) {
    var f = res.fact || {};
    var unit = f.unit ? (' ' + f.unit) : '';
    var verb = res.action === 'updated' ? 'Updated on' : 'Added to';
    var html = '<div class="vx-inject-ok"><strong>' + verb + ' ' + escapeHtml(res.ticker)
      + ' DCF reference sheet.</strong> ' + escapeHtml(f.label) + ' = ' + fmtNum(f.value) + unit
      + (f.period_end ? ' \\u00b7 as of ' + escapeHtml(f.period_end) : '')
      + (f.source ? ' \\u00b7 ' + escapeHtml(f.source) : '')
      + (f.fact_id !== null && f.fact_id !== undefined ? ' \\u00b7 fact #' + f.fact_id : '')
      + '<br>' + res.count + (res.count === 1 ? ' fact' : ' facts') + ' on the sheet \\u00b7 '
      + 'survives DCF refresh (separate workbook)</div>';
    el('vx-result').innerHTML = html;
  }
  var refBtn = el('vx-inject-ref');
  if (refBtn) refBtn.addEventListener('click', function () {
    var ts = tickers();
    if (ts.length !== 1) {
      showError('A DCF reference fact attaches to one company — narrow to exactly one ticker.');
      return;
    }
    var toks = selectedTokens();
    if (toks.length !== 1) { showError('Pick exactly one fact to add as a DCF reference.'); return; }
    CCAction.busy(refBtn, 'Adding reference\\u2026');
    el('vx-result').innerHTML = '<div class="vx-none">Adding reference\\u2026</div>';
    fetch('/api/dcf/inject-fact-sheet', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ticker: ts[0], token: toks[0]})
    }).then(function (r) {
      return r.json().then(function (res) { return {ok: r.ok, res: res}; });
    }).then(function (o) {
      CCAction.release(refBtn);
      if (!o.ok) { showError((o.res && o.res.error) || 'adding reference failed'); return; }
      renderReference(o.res);
    }).catch(function () {
      CCAction.release(refBtn);
      showError('network error — try again');
    });
  });
  root.addEventListener('click', function (ev) {
    var btn = ev.target.closest('button[data-act]');
    if (!btn) return;
    var holder = btn.closest('[data-view-id]');
    if (!holder) return;
    var id = holder.getAttribute('data-view-id');
    if (btn.getAttribute('data-act') === 'del') {
      fetch('/api/views/' + id, {method: 'DELETE'}).then(function (r) {
        if (r.ok) refreshSaved();
      });
      return;
    }
    var spec = {};
    try { spec = JSON.parse(holder.getAttribute('data-spec') || '{}'); } catch (e) {}
    el('vx-view-name').value = holder.getAttribute('data-view-name') || '';
    applySpec(spec);
  });

  // ---- Research prompt handoff: Work OS Copilot is the sole conversation
  // surface.  Fact Playground keeps only prompt entry and deterministic DIY
  // view controls; it never executes a research model itself. ----
  var askThread = el('ask-thread');
  var askInput = el('ask-q');
  var askGo = el('ask-go');

  // Research questions hand off to the one Work OS Copilot surface.
  function submitAsk(q) {
    var query = (q || askInput.value).trim();
    if (!query) { askInput.focus(); return; }
    askInput.value = '';
    if (window.openWorkOsCopilot) {
      window.openWorkOsCopilot({
        company_ticker: tickers()[0] || null,
        category: 'research',
        origin_key: 'work-os:fact-playground',
        coverage_role_at_creation: 'unknown',
        lifecycle_at_creation: 'unknown',
        prompt: query
      });
    } else {
      window.location.assign('/?copilot=1&origin_key=fact-playground');
    }
  }

  if (askGo) askGo.addEventListener('click', function () { submitAsk(); });
  if (askInput) askInput.addEventListener('keydown', function (ev) {
    if (ev.key === 'Enter') { ev.preventDefault(); submitAsk(); }
  });

  if (askThread) askThread.addEventListener('click', function (ev) {
    var chip = ev.target.closest('.k-chip');
    if (chip) submitAsk(chip.getAttribute('data-ask-q') || chip.textContent);
  });

  // Ctrl/Cmd+K handoff: the shell palette stashes the typed query in the
  // store (key askQ — legacy cc-ask-q migrates; the EVENT name stays
  // 'cc-ask-q') and jumps to #explore; consume it at wire-up (lazy panel
  // load) or on the palette's event (panel already loaded).
  function consumePaletteQuery() {
    if (!window.CCState) return;
    var q = window.CCState.get('askQ');
    if (!q) return;
    window.CCState.del('askQ');
    submitAsk(q);
  }
  window.addEventListener('cc-ask-q', consumePaletteQuery);
  consumePaletteQuery();

  // Saved-view handoff (UX9b): the shell palette stashes a chosen view id
  // (store key askViewId — legacy cc-view-id migrates) and jumps to
  // #explore. Open the advanced builder fold and click that view's load
  // chip — reusing the same delegated load+run path the chip strip uses.
  function consumePaletteView() {
    if (!window.CCState) return;
    var id = window.CCState.get('askViewId');
    if (!id) return;
    window.CCState.del('askViewId');
    var chip = root.querySelector('[data-view-id="' + id + '"] button[data-act="load"]');
    if (!chip) return;
    // NB: 'ask-advanced' is a plain div (no disclosure-widget semantics), so
    // a bare `.open = true` here was a dead assignment predating the
    // CCOverlay conversion above; openBuilder() is the real show call.
    openBuilder();
    chip.click();
  }
  window.addEventListener('cc-view-id', consumePaletteView);
  consumePaletteView();
})();
"""


def _saved_chip(view: SavedViewRow) -> str:
    spec_attr = escape(json.dumps(view.spec))
    return (
        f'<span class="vx-saved" data-view-id="{view.id}" '
        f'data-view-name="{escape(view.name)}" data-spec="{spec_attr}">'
        f'<button type="button" class="k-btn k-btn-sm k-btn-quiet" data-act="load" '
        f'title="load + run">{escape(view.name)}</button>'
        '<button type="button" class="k-btn k-btn-sm k-btn-quiet" data-act="del" '
        'title="delete">&times;</button>'
        "</span>"
    )


def render_saved_views_list(db_path: Path, *, user_id: str = DEFAULT_USER_ID) -> str:
    """Just the saved-view chip strip (the ``?fragment=views`` fragment)."""
    try:
        views = list_views(user_id=user_id, db_path=db_path)
    except (sqlite3.Error, FileNotFoundError, RuntimeError):
        views = []  # pre-0079 schema / missing DB degrades to empty
    if not views:
        return '<span class="vx-none">No saved views yet.</span>'
    return "".join(_saved_chip(v) for v in views)


def _picker_option(e: dict[str, object]) -> str:
    """One <option> for a catalog entry — carries the coverage suffix, a
    definition tooltip (augmented with the metric's origin), and
    data-origin / data-override hooks the type-ahead/badge JS reads."""
    token = escape(str(e.get("token") or ""))
    text = str(e.get("label") or "")
    n_raw = e.get("tickers")
    n = n_raw if isinstance(n_raw, int) else 0
    suffix = f" ({n})" if n > 1 else ""
    origin = str(e.get("origin") or "")
    override_only = bool(e.get("override_only"))
    title_raw = str(e.get("title") or "")
    # Origin is surfaced on hover, not in the option text, so the list stays one
    # clean searchable column (the owner's default) while still distinguishing
    # analyst-curated from auto-captured / company-doc figures.
    note = ""
    if override_only:
        note = "company-document figure (no FMP base)"
    elif origin == "capture":
        note = "auto-captured metric (capture-every-number)"
    full_title = " — ".join(p for p in (title_raw, note) if p)
    title_attr = f' title="{escape(full_title)}"' if full_title else ""
    origin_attr = f' data-origin="{escape(origin)}"' if origin else ""
    override_attr = ' data-override="1"' if override_only else ""
    return (
        f'<option value="{token}"{title_attr}{origin_attr}{override_attr}>'
        f"{escape(text + suffix)}</option>"
    )


def _picker_html(dom_id: str, label: str, entries: list[dict[str, object]]) -> str:
    opts = "".join(_picker_option(e) for e in entries)
    return (
        f'<div class="vx-picker"><label>{escape(label)}</label>'
        f'<input type="search" id="{dom_id}-q" class="vx-pick-search" '
        f'placeholder="filter…" autocomplete="off" aria-label="{escape(label)} filter">'
        f'<select id="{dom_id}" multiple size="9">{opts}</select>'
        f'<span class="vx-pick-count" id="{dom_id}-count"></span></div>'
    )


def _default_tickers(db_path: Path, user_id: str) -> list[str]:
    """The portfolio list — the natural starting universe for a pivot."""
    if not db_path.exists():
        return []
    try:
        conn = connect_sqlite(db_path, role=SQLiteConnectionRole.READ_ONLY)
    except sqlite3.Error:
        return []
    try:
        rows = conn.execute(
            "SELECT ticker FROM tracked_companies "
            "WHERE user_id = ? AND list_type = 'portfolio' ORDER BY ticker",
            (user_id,),
        ).fetchall()
        return [str(r[0]) for r in rows]
    except sqlite3.Error:
        return []
    finally:
        conn.close()


def render_keymetrics_fragment(db_path: Path, tickers: list[str]) -> str:
    """The ``?fragment=keymetrics`` body: the inner HTML of ``#vx-keymetrics``
    for ``tickers`` (label + chips), swapped in by the panel JS on a ticker
    change. Reads the LLM cache directly + the tier-graded baseline (no LLM call,
    no heavy import); empty string when there are no key-metric hints."""
    from pipeline.key_metrics import key_metric_bubbles, render_key_metrics_inner

    symbols = [t.strip().upper() for t in tickers if t.strip()]
    catalog: dict[str, list[dict[str, object]]] = (
        metric_catalog(db_path, symbols) if symbols else {"fin": [], "kpi": [], "seg": []}
    )
    bubbles = key_metric_bubbles(db_path, symbols, catalog)
    return render_key_metrics_inner(bubbles, symbols)


def render_explore_panel(db_path: Path, *, user_id: str = DEFAULT_USER_ID) -> str:
    """The Research → Explore tab fragment: builder + saved views + result."""
    tickers = _default_tickers(db_path, user_id)
    catalog: dict[str, list[dict[str, object]]] = (
        metric_catalog(db_path, tickers) if tickers else {"fin": [], "kpi": [], "seg": []}
    )
    # Key-metrics preselect bubbles (key_metrics_picker.md): the tier-graded
    # baseline + cached LLM picks, merged. Reuses the catalog already loaded
    # above so the render path stays a single catalog read.
    from pipeline.key_metrics import key_metric_bubbles, render_key_metrics_inner

    keymetrics_inner = render_key_metrics_inner(
        key_metric_bubbles(db_path, tickers, catalog), tickers
    )
    transform_opts = "".join(
        f'<option value="{escape(t)}"{" selected" if t == "level" else ""}>{escape(t)}</option>'
        for t in TRANSFORMS
    )
    cadence_opts = "".join(f'<option value="{escape(c)}">{escape(c)}</option>' for c in CADENCES)
    inject_field_opts = "".join(
        f'<option value="{escape(o["key"])}">{escape(o["label"])}</option>'
        for o in driver_field_options()
    )
    tickers_val = escape(", ".join(tickers))
    saved = render_saved_views_list(db_path, user_id=user_id)
    first = tickers[0] if tickers else "NU"
    second = tickers[1] if len(tickers) > 1 else "MELI"
    # Research prompts are doorway controls into Work OS Copilot.  The panel
    # itself owns only deterministic ViewSpec compilation and execution.
    return f"""{_PANEL_STYLE}
<div id="vx-root">
<div class="ask-thread" id="ask-thread">
  <div class="ask-hello">Send a research question to Work OS Copilot, the single
 conversation surface. Use DIY to build a deterministic table or chart directly from
 governed facts without an LLM.
    <div class="ask-chips">
      <button type="button" class="k-chip"
        data-ask-q="{escape(first)} vs {escape(second)} revenue growth, last 8 quarters">
        {escape(first)} vs {escape(second)} revenue growth</button>
      <button type="button" class="k-chip"
        data-ask-q="{escape(first)} margins, last 12 quarters">{escape(first)} margins</button>
      <button type="button" class="k-chip"
        data-ask-q="revenue 3-year CAGR for {escape(first)}, {escape(second)}, annual">
        3y revenue CAGR</button>
    </div>
  </div>
</div>
<div class="ask-inputrow">
  <input id="ask-q" placeholder="Send to Copilot — e.g. {escape(first)} vs {escape(second)} revenue growth" autocomplete="off">
  <button type="button" id="ask-go" class="k-btn k-btn-primary">Open Copilot</button>
  <button type="button" id="ask-diy" class="k-btn k-btn-quiet"
    title="Build a view by hand — tickers, metrics, transform, saved views">DIY</button>
</div>
<div class="ask-advanced ask-builder-pop" id="ask-advanced" hidden>
<div class="ask-pop-head"><span>DIY builder &middot; saved views</span>
<button type="button" class="ask-pop-close" id="ask-pop-close" title="Close (Esc)">&times;</button></div>
<div class="vx-builder">
  <div class="vx-row vx-nl">
    <label>ask</label>
    <input id="vx-nl-q" name="nl_query"
      placeholder="e.g. NU vs MELI revenue growth, last 8 quarters">
    <button type="button" class="k-btn k-btn-quiet k-btn-sm" id="vx-nl-go">Compile</button>
    <span class="vx-nl-msg" id="vx-nl-msg">compiles into the builder below &mdash; never raw
 SQL; falls back to the pickers when it can&#x27;t parse</span>
  </div>
  <div class="vx-row">
    <label>tickers</label>
    <input id="vx-tickers" name="tickers" value="{tickers_val}"
      placeholder="NU, MELI, &hellip;">
    <button type="button" class="k-btn k-btn-quiet k-btn-sm" id="vx-load-metrics"
      title="Refresh the metric pickers for these tickers">Load metrics</button>
    <button type="button" class="k-btn k-btn-quiet k-btn-sm" id="vx-peers"
      title="Append the first ticker&#x27;s scored peer set (named rivals, same industry, tracked names)">+ Peers</button>
    <label>transform</label>
    <select id="vx-transform">{transform_opts}</select>
    <label>cadence</label>
    <select id="vx-cadence">{cadence_opts}</select>
    <label>periods</label>
    <input id="vx-periods" name="periods" type="number" min="1" max="40" value="12">
    <label>CAGR yrs</label>
    <input id="vx-cagr-years" name="cagr_years" type="number" min="1" max="10" value="3">
    <button type="button" class="k-btn k-btn-quiet k-btn-sm" id="vx-run">Run view</button>
  </div>
  <div class="vx-keymetrics" id="vx-keymetrics">{keymetrics_inner}</div>
  <div class="vx-pickers">
    {_picker_html("vx-pick-fin", "Financial line items", catalog["fin"])}
    {_picker_html("vx-pick-kpi", "KPIs", catalog["kpi"])}
    {_picker_html("vx-pick-seg", "Segments", catalog["seg"])}
  </div>
  <div class="vx-row">
    <input id="vx-view-name" name="view_name" placeholder="view name">
    <button type="button" class="k-btn k-btn-quiet k-btn-sm" id="vx-save">Save view</button>
    <span class="vx-saved-strip" id="vx-saved-strip">{saved}</span>
  </div>
  <div class="vx-row vx-inject">
    <label>inject one fact &rarr; DCF</label>
    <select id="vx-inject-field" aria-label="DCF driver field">{inject_field_opts}</select>
    <button type="button" class="k-btn k-btn-quiet k-btn-sm" id="vx-inject-dcf"
      title="Set a redesigned-DCF driver from the single picked fact &amp; single ticker — latest value, override-aware, unit-converted, sanity-bounded">Inject as DCF driver</button>
    <button type="button" class="k-btn k-btn-quiet k-btn-sm" id="vx-inject-ref"
      title="Park the single picked fact on the ticker's DCF reference sheet (a companion workbook the refresh never rebuilds) — latest value, override-aware, native unit, survives every model refresh">Add as reference</button>
  </div>
</div>
<div id="vx-result"><div class="vx-none">Pick tickers + metrics and run. Saved views
 re-run from their chips; every fin/KPI number carries its source chip.</div></div>
<p class="vx-hint">Transforms: level = raw values &middot; yoy = % vs the same calendar
 bucket a year ago &middot; cagr = trailing N-year compound growth &middot; margin = value
 / revenue. Cross-ticker columns align on calendar buckets derived from each fiscal
 period end. Saved views embed elsewhere via /api/views/&lt;id&gt;/fragment.</p>
</div>
</div>
<script>{_PANEL_JS}</script>"""
