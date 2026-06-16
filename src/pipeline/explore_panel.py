"""Ask panel (UX redesign PR5 over master build P5.1/P5.2).

Conversational-first: a chat thread over the unified ask engine
(``POST /api/ask`` → ``src/ask/engine.py`` with the PORTFOLIO context
pack). Data questions compile to a validated ViewSpec, execute, and render
inline as a matrix/chart card; narrative questions stream through the same
claude-CLI path as the report drawer's chat and render as prose; /discovery
and /help reply instantly. Follow-ups send the previous spec as context so
"now annual" / "add MELI" refine instead of starting over, plus the thread
tail for narrative continuity; every view card offers "Open in builder" and
"Pin as view". The full ViewSpec builder survives untouched inside the
"Advanced builder" fold below the thread.

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
from ui.cite_marks import CITE_MARKS_SNIPPET
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

/* ---- Ask thread (UX redesign PR5) ---- */
.ask-thread { display:flex; flex-direction:column; gap:12px; margin:4px 0 14px; }
.ask-hello { color:var(--muted); font-size:var(--fs-body); line-height:1.5; border:1px dashed var(--border);
  border-radius:var(--radius); padding:14px 16px; }
.ask-chips { display:flex; gap:8px; flex-wrap:wrap; margin-top:10px; }
.ask-turn-user { align-self:flex-end; max-width:70%; background:var(--accent-soft); border:1px solid var(--accent);
  color:var(--fg); border-radius:var(--radius); padding:8px 14px; font-size:var(--fs-body); }
.ask-turn-assistant { align-self:stretch; border:1px solid var(--border); background:var(--surface);
  border-radius:var(--radius); padding:12px 14px; }
.ask-meta { color:var(--muted); font-size:var(--fs-caption); margin-bottom:8px; display:flex; gap:10px;
  align-items:baseline; flex-wrap:wrap; }
.ask-meta .ask-err { color:var(--bad); }
.ask-actions { margin-top:8px; display:flex; gap:10px; align-items:baseline;
  justify-content:space-between; flex-wrap:wrap; }
.ask-actions-sum { color:var(--muted); font-size:var(--fs-caption); }
.ask-actions-btns { display:flex; gap:8px; }
.ask-busy { color:var(--muted); font-size:var(--fs-body); }
.ask-busy .dots::after { content:'…'; animation: askdots 1.2s steps(4, end) infinite; }
.ask-cite-row { margin-top:8px; display:flex; gap:6px; flex-wrap:wrap; }
@keyframes askdots { 0% { content:''; } 25% { content:'.'; } 50% { content:'..'; } 75% { content:'...'; } }
.ask-cmd { font-family:var(--mono); font-size:var(--fs-caption); white-space:pre-wrap;
  color:var(--fg); margin:0; }
.ask-inputrow { display:flex; gap:8px; align-items:center; margin-bottom:10px; }
/* No font-size here: the input inherits the kit baseline (--fs-body, 13px) so it
   matches the .k-btn buttons beside it, and the mobile 16px floor (controls.py)
   is no longer overridden. The Ask/DIY buttons are .k-btn (primary/quiet) — no
   bespoke .ask-inputrow button rule. */
.ask-inputrow input { flex:1; padding:9px 13px; }
.ask-ctx { color:var(--muted); font-size:var(--fs-caption); }
.ask-ctx a { color:var(--accent); cursor:pointer; }
.ask-builder-pop { border:1px solid var(--accent); border-radius:var(--radius); background:var(--surface);
  padding:0 14px 12px; margin-top:10px; box-shadow:0 14px 44px rgba(0,0,0,0.5); }
.ask-pop-head { display:flex; justify-content:space-between; align-items:center; padding:10px 0;
  font-size:var(--fs-body); font-weight:600; color:var(--fg); }
.ask-pop-head button { background:transparent; border:none; color:var(--muted); font-size:var(--fs-display);
  cursor:pointer; padding:0 4px; }
.ask-pop-head button:hover { color:var(--fg); }
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
    el('vx-result').innerHTML = '<div class="vx-none">Running\\u2026</div>';
    fetch('/api/viewspec/run', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({spec: spec})
    }).then(function (r) {
      if (r.ok) return r.text().then(function (h) { el('vx-result').innerHTML = h; });
      return r.json().then(function (e) { showError(e.error || ('HTTP ' + r.status)); });
    });
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
    btn.disabled = true;
    msg.textContent = 'compiling…';
    fetch('/api/viewspec/compile', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({query: q, tickers: tickers()})
    }).then(function (r) { return r.json(); }).then(function (res) {
      btn.disabled = false;
      if (res.status === 'ok' && res.spec) {
        msg.textContent = 'compiled — builder updated';
        applySpec(res.spec);
      } else {
        msg.textContent = res.message || res.error || 'compile failed — use the builder';
      }
    }).catch(function () {
      btn.disabled = false;
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
  // DIY button / "Open in builder" instead of living as a bottom fold. ----
  var builderPop = el('ask-advanced');
  function openBuilder() {
    if (!builderPop) return;
    builderPop.hidden = false;
    builderPop.scrollIntoView({behavior: 'smooth', block: 'nearest'});
  }
  function closeBuilder() { if (builderPop) builderPop.hidden = true; }
  var diyBtn = el('ask-diy');
  if (diyBtn) diyBtn.addEventListener('click', function () {
    if (builderPop && builderPop.hidden) openBuilder(); else closeBuilder();
  });
  var popClose = el('ask-pop-close');
  if (popClose) popClose.addEventListener('click', closeBuilder);
  document.addEventListener('keydown', function (ev) {
    if (ev.key === 'Escape') closeBuilder();
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
    vxPeers.disabled = true;
    fetchPeers(cur[0], function (peers) {
      vxPeers.disabled = false;
      if (!peers.length) { showError('No scored peers for ' + cur[0] + '.'); return; }
      var merged = cur.slice();
      peers.forEach(function (p) { if (merged.indexOf(p) === -1) merged.push(p); });
      el('vx-tickers').value = merged.slice(0, 16).join(', ');
      loadCatalog(selectedTokens());
    });
  });
  function addPeersToCard(btn, card, spec) {
    var base = (spec.tickers && spec.tickers[0]) || tickers()[0];
    if (!base || !card) return;
    btn.disabled = true;
    btn.textContent = 'adding peers…';
    fetchPeers(base, function (peers) {
      var current = spec.tickers || [];
      var merged = current.slice();
      peers.forEach(function (p) { if (merged.indexOf(p) === -1) merged.push(p); });
      merged = merged.slice(0, 16);
      if (merged.length === current.length) {
        btn.textContent = 'no new peers';
        return;
      }
      var newSpec = JSON.parse(JSON.stringify(spec));
      newSpec.tickers = merged;
      fetch('/api/viewspec/run', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({spec: newSpec, summary: false})
      }).then(function (r) {
        if (!r.ok) throw new Error('run failed');
        return r.text();
      }).then(function (h) {
        lastSpec = newSpec;
        setCtx(true);
        var added = merged.filter(function (t) { return current.indexOf(t) === -1; });
        card.innerHTML = h + askActionsHtml(base + ' + scored peers: ' + added.join(', '));
        card.setAttribute('data-ask-spec', JSON.stringify(newSpec));
        askScroll();
      }).catch(function () {
        btn.disabled = false;
        btn.textContent = '+ Peers';
      });
    });
  }
  el('vx-save').addEventListener('click', function () {
    var name = el('vx-view-name').value.trim();
    if (!name) { showError('Name the view before saving.'); return; }
    var spec = buildSpec();
    if (!spec.metrics.length) { showError('Pick at least one metric.'); return; }
    fetch('/api/views', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({name: name, spec: spec})
    }).then(function (r) {
      if (r.ok) { refreshSaved(); }
      else { r.json().then(function (e) { showError(e.error || 'save failed'); }); }
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
    var html = '<div class="vx-inject-ok"><strong>Injected into ' + askEsc(inj.ticker)
      + ' DCF.</strong> ' + askEsc(inj.metric_label) + ' = ' + fmtNum(inj.raw_value) + rawUnit
      + ' \\u2192 ' + askEsc(inj.field_label) + ' = ' + fmtNum(inj.applied_value) + conv + '<br>'
      + 'Repriced: fair value ' + (fv !== null && fv !== undefined ? '$' + fmtNum(fv) : 'n/a')
      + (wacc !== null && wacc !== undefined ? ' \\u00b7 WACC ' + (wacc * 100).toFixed(1) + '%' : '')
      + (inj.fact_id !== null && inj.fact_id !== undefined ? ' \\u00b7 from fact #' + inj.fact_id : '')
      + (inj.period_end ? ' \\u00b7 as of ' + askEsc(inj.period_end) : '')
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
    injectBtn.disabled = true;
    el('vx-result').innerHTML = '<div class="vx-none">Injecting\\u2026</div>';
    fetch('/api/dcf/inject-fact', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ticker: ts[0], token: toks[0], field: fieldSel ? fieldSel.value : ''})
    }).then(function (r) {
      return r.json().then(function (res) { return {ok: r.ok, res: res}; });
    }).then(function (o) {
      injectBtn.disabled = false;
      if (!o.ok) { showError((o.res && o.res.error) || 'injection failed'); return; }
      renderInjection(o.res);
    }).catch(function () {
      injectBtn.disabled = false;
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
    var html = '<div class="vx-inject-ok"><strong>' + verb + ' ' + askEsc(res.ticker)
      + ' DCF reference sheet.</strong> ' + askEsc(f.label) + ' = ' + fmtNum(f.value) + unit
      + (f.period_end ? ' \\u00b7 as of ' + askEsc(f.period_end) : '')
      + (f.source ? ' \\u00b7 ' + askEsc(f.source) : '')
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
    refBtn.disabled = true;
    el('vx-result').innerHTML = '<div class="vx-none">Adding reference\\u2026</div>';
    fetch('/api/dcf/inject-fact-sheet', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ticker: ts[0], token: toks[0]})
    }).then(function (r) {
      return r.json().then(function (res) { return {ok: r.ok, res: res}; });
    }).then(function (o) {
      refBtn.disabled = false;
      if (!o.ok) { showError((o.res && o.res.error) || 'adding reference failed'); return; }
      renderReference(o.res);
    }).catch(function () {
      refBtn.disabled = false;
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

  // ---- Ask thread: one conversational engine (src/ask/engine.py) behind
  // POST /api/ask. Data questions come back as view fragments; narrative
  // questions as prose; /discovery + /help as instant command replies.
  // Follow-ups send the previous spec (view refinement) AND the thread
  // tail (narrative continuity — the server keeps no Ask-tab state). ----
  var askThread = el('ask-thread');
  var askInput = el('ask-q');
  var askGo = el('ask-go');
  var askCtx = el('ask-ctx');
  var lastSpec = null;
  var askBusy = false;
  var askHistory = [];

  function askEsc(s) {
    return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  }
  function askMd(text) {
    var html = askEsc(text);
    html = html.replace(/```[\\w]*\\n([\\s\\S]*?)```/g, function (_m, body) {
      return '<pre class="ask-code">' + body + '</pre>';
    });
    html = html.replace(/`([^`]+)`/g, '<code>$1</code>');
    html = html.replace(/\\*\\*([^*]+)\\*\\*/g, '<strong>$1</strong>');
    html = html.replace(/(?:^|\\n)[-*]\\s+(.+)/g, '\\n<li>$1</li>');
    html = html.replace(/(<li>[\\s\\S]+?<\\/li>)+/g, function (b) { return '<ul>' + b + '</ul>'; });
    return html.split(/\\n\\n+/).map(function (p) {
      if (/^<(pre|ul)/.test(p)) return p;
      return '<p>' + p.replace(/\\n/g, '<br>') + '</p>';
    }).join('');
  }
  function askCiteHref(c) {
    return (c && (c.href || c.source_url)) || '';
  }
  // Inline superscript cite chips (S8): the shared ui.cite_marks helper
  // upgrades the finished prose; popovers carry label + S2 confidence %.
  function askLinkifyCites(html, items) {
    if (!window.ccCiteMarks || !(items || []).length) return html;
    return window.ccCiteMarks.linkify(html, items);
  }
  function askCiteRowHtml(items, claims) {
    var chips = (items || []).map(function (c) {
      var href = askCiteHref(c);
      if (!href) return '';
      return '<a class="k-chip k-chip-accent" href="' + askEsc(href) + '" target="_blank">['
        + askEsc(String(c.n)) + '] ' + askEsc(c.label || c.kind || 'source') + '</a>';
    }).join('');
    var warn = window.ccCiteMarks ? window.ccCiteMarks.unverifiedChipHtml(claims) : '';
    return (chips || warn) ? '<div class="ask-cite-row">' + chips + warn + '</div>' : '';
  }
  function askActionsHtml(summary) {
    var sum = summary ? '<span class="ask-actions-sum">' + askEsc(summary) + '</span>' : '';
    return '<div class="ask-actions">' + sum
      + '<span class="ask-actions-btns">'
      + '<button type="button" class="k-btn k-btn-quiet k-btn-sm" data-ask-act="builder">Open in builder</button>'
      + '<button type="button" class="k-btn k-btn-quiet k-btn-sm" data-ask-act="peers">+ Peers</button>'
      + '<button type="button" class="k-btn k-btn-quiet k-btn-sm" data-ask-act="pin">Pin as view</button>'
      + '</span></div>';
  }
  function askRemember(role, text) {
    askHistory.push({role: role, text: String(text || '').slice(0, 1200)});
    if (askHistory.length > 12) askHistory = askHistory.slice(-12);
  }
  function askScroll() { askThread.scrollTop = askThread.scrollHeight; }
  function clearHello() {
    var hello = askThread.querySelector('.ask-hello');
    if (hello) hello.remove();
  }
  function setCtx(on) {
    lastSpec = on ? lastSpec : null;
    if (askCtx) askCtx.hidden = !on;
  }

  // Ask v2: consume the SSE stream (POST /api/ask/stream) so progress is
  // real — stage frames drive the busy line, narrative deltas render as
  // they arrive, fragment/final assemble the answer card at the end.
  function submitAsk(q) {
    if (askBusy) return;
    var query = (q || askInput.value).trim();
    if (!query) { askInput.focus(); return; }
    askBusy = true;
    askInput.value = '';
    // After the first question the example placeholder is stale — every later
    // turn refines the last answer, so prompt for a follow-up (mirrors ask_dock).
    askInput.placeholder = 'Ask a follow-up…';
    clearHello();
    var user = document.createElement('div');
    user.className = 'ask-turn-user';
    user.textContent = query;
    askThread.appendChild(user);
    var card = document.createElement('div');
    card.className = 'ask-turn-assistant';
    card.innerHTML = '<div class="ask-busy">working<span class="dots"></span></div>';
    askThread.appendChild(card);
    askScroll();
    askRemember('user', query);

    var prose = null;
    var proseText = '';
    var frag = null;
    var note = '';
    var finalEv = null;
    var citations = [];
    var claims = [];
    var errored = false;

    function busyLine(text) {
      var busy = card.querySelector('.ask-busy');
      if (busy) busy.innerHTML = askEsc(text) + '<span class="dots"></span>';
    }
    function ensureProse() {
      if (prose) return;
      var busy = card.querySelector('.ask-busy');
      if (busy) busy.remove();
      prose = document.createElement('div');
      prose.className = 'prose';
      card.appendChild(prose);
    }
    function handleEvent(ev) {
      if (ev.type === 'stage') {
        if (ev.note) note = ev.note;
        if (ev.stage === 'compiling') busyLine('compiling the view');
        else if (ev.stage === 'running') busyLine('running the view');
        else busyLine(ev.note || 'researching — prose answers can take ~30s');
      } else if (ev.type === 'delta') {
        ensureProse();
        proseText += ev.text || '';
        prose.textContent = proseText;
        askScroll();
      } else if (ev.type === 'fragment') {
        frag = ev;
      } else if (ev.type === 'final') {
        finalEv = ev;
      } else if (ev.type === 'citations') {
        citations = ev.items || [];
        claims = ev.claims || [];
      } else if (ev.type === 'error') {
        errored = true;
        card.innerHTML = '<div class="ask-meta"><span class="ask-err">'
          + askEsc(ev.error || 'Could not answer that — try the advanced builder.')
          + '</span></div>';
      }
    }
    function finish() {
      askBusy = false;
      if (errored) { askScroll(); return; }
      if (frag) {
        lastSpec = frag.spec || lastSpec;
        setCtx(true);
        var msg = (finalEv && finalEv.text) || 'done';
        // The reconciled summary rides the actions row (no separate caption
        // band); the fragment was rendered with include_summary=false.
        card.innerHTML = (frag.html || '') + askActionsHtml(msg);
        card.setAttribute('data-ask-spec', JSON.stringify(frag.spec || {}));
        askRemember('assistant', msg);
      } else if (finalEv) {
        var text = finalEv.text || proseText || '';
        if (finalEv.route === 'command') {
          card.innerHTML = '<pre class="ask-cmd">' + askEsc(text) + '</pre>';
        } else {
          var noteHtml = note ? '<div class="ask-meta">' + askEsc(note) + '</div>' : '';
          card.innerHTML = noteHtml
            + '<div class="prose">' + askLinkifyCites(askMd(text), citations) + '</div>'
            + askCiteRowHtml(citations, claims);
        }
        askRemember('assistant', text);
      } else {
        card.innerHTML = '<div class="ask-meta"><span class="ask-err">no answer — try again</span></div>';
      }
      askScroll();
    }

    fetch('/api/ask/stream', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({query: query, tickers: tickers(), context_spec: lastSpec,
                            history: askHistory.slice(0, -1)})
    }).then(function (r) {
      if (!r.ok || !r.body) throw new Error('HTTP ' + r.status);
      var reader = r.body.getReader();
      var decoder = new TextDecoder();
      var buffer = '';
      function pump() {
        return reader.read().then(function (res) {
          if (res.done) { finish(); return; }
          buffer += decoder.decode(res.value, {stream: true});
          var parts = buffer.split('\\n\\n');
          buffer = parts.pop();
          parts.forEach(function (frame) {
            var line = frame.replace(/^data:\\s*/, '');
            if (!line) return;
            try { handleEvent(JSON.parse(line)); } catch (e) {}
          });
          return pump();
        });
      }
      return pump();
    }).catch(function () {
      askBusy = false;
      card.innerHTML = '<div class="ask-meta"><span class="ask-err">network error — try again</span></div>';
      askScroll();
    });
  }

  if (askGo) askGo.addEventListener('click', function () { submitAsk(); });
  if (askInput) askInput.addEventListener('keydown', function (ev) {
    if (ev.key === 'Enter') { ev.preventDefault(); submitAsk(); }
  });
  var ctxClear = el('ask-ctx-clear');
  if (ctxClear) ctxClear.addEventListener('click', function () { setCtx(false); });

  if (askThread) askThread.addEventListener('click', function (ev) {
    var chip = ev.target.closest('.k-chip');
    if (chip) { submitAsk(chip.getAttribute('data-ask-q') || chip.textContent); return; }
    var act = ev.target.closest('button[data-ask-act]');
    if (!act) return;
    var holder = act.closest('[data-ask-spec]');
    var spec = {};
    try { spec = JSON.parse(holder ? holder.getAttribute('data-ask-spec') : '{}'); } catch (e) {}
    if (act.getAttribute('data-ask-act') === 'builder') {
      openBuilder();
      applySpec(spec);
    } else if (act.getAttribute('data-ask-act') === 'peers') {
      addPeersToCard(act, holder, spec);
    } else {
      var name = window.prompt('Save this view as…');
      if (!name) return;
      fetch('/api/views', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({name: name.trim(), spec: spec})
      }).then(function (r) { if (r.ok) refreshSaved(); });
    }
  });

  // Home-dock handoff (Ask v4): the dock stashes its thread when popping
  // out to this tab (store key askThread — legacy cc-ask-thread migrates);
  // replay it as text turns + seed the narrative history. Registered BEFORE
  // the palette consumer so a pending question lands after the replayed
  // thread. This fragment only ever runs inside the shell, where CCState is
  // inlined first — the guard covers a bare fragment render.
  function consumeDockThread() {
    if (!window.CCState) return;
    var turns = window.CCState.getJSON('askThread') || [];
    window.CCState.del('askThread');
    if (!turns.length) return;
    clearHello();
    turns.forEach(function (t) {
      if (!t || !t.text) return;
      askRemember(t.role, t.text);
      var div = document.createElement('div');
      if (t.role === 'user') {
        div.className = 'ask-turn-user';
        div.textContent = t.text;
      } else {
        div.className = 'ask-turn-assistant';
        div.innerHTML = '<div class="prose">' + askMd(t.text) + '</div>';
      }
      askThread.appendChild(div);
    });
    askScroll();
  }
  window.addEventListener('cc-ask-q', consumeDockThread);
  consumeDockThread();

  // Ctrl/Cmd+K handoff: the shell palette stashes the typed query in the
  // store (key askQ — legacy cc-ask-q migrates; the EVENT name stays
  // 'cc-ask-q') and jumps to #explore; consume it at wire-up (lazy panel
  // load) or on the palette's event (panel already loaded).
  function consumePaletteQuery() {
    if (!window.CCState) return;
    var q = window.CCState.get('askQ');
    if (!q || askBusy) return;
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
    var fold = el('ask-advanced');
    if (fold) fold.open = true;
    chip.click();
    if (fold) fold.scrollIntoView({behavior: 'smooth', block: 'start'});
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
        conn = sqlite3.connect(str(db_path), timeout=5.0)
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
    # No <h2>Ask</h2>: this panel is the single sub-tab of an already-labeled
    # nav section, so the nav owns the title (design_language §6.1 — single-sub-
    # tab sections suppress their own section name; the shell hides the sub-tab
    # row via data-single and never re-injects the title). Re-printing it here
    # was the redundant horizontal "Ask" bar.
    return f"""{_PANEL_STYLE}
{CITE_MARKS_SNIPPET}
<div id="vx-root">
<div class="ask-thread" id="ask-thread">
  <div class="ask-hello">Ask anything across the tracked universe. Metric questions
 come back as live tables and charts with per-number source chips; narrative questions
 (&ldquo;why&rdquo;, &ldquo;what&rsquo;s the bear case&rdquo;) get a researched answer.
 Follow-ups refine the last answer (&ldquo;now annual&rdquo;, &ldquo;add {escape(second)}&rdquo;,
 &ldquo;same but as margins&rdquo;). <code>/view</code> forces a data view; <code>/help</code>
 lists commands.
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
  <input id="ask-q" placeholder="Ask — e.g. {escape(first)} vs {escape(second)} revenue growth, last 8 quarters" autocomplete="off">
  <button type="button" id="ask-go" class="k-btn k-btn-primary">Ask</button>
  <button type="button" id="ask-diy" class="k-btn k-btn-quiet"
    title="Build a view by hand — tickers, metrics, transform, saved views">DIY</button>
  <span class="ask-ctx" id="ask-ctx" hidden>refining the last view &middot;
 <a id="ask-ctx-clear">start fresh</a></span>
</div>
<div class="ask-advanced ask-builder-pop" id="ask-advanced" hidden>
<div class="ask-pop-head"><span>DIY builder &middot; saved views</span>
<button type="button" id="ask-pop-close" title="Close (Esc)">&times;</button></div>
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
