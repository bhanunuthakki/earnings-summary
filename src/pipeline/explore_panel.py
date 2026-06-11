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

from identity import DEFAULT_USER_ID
from user_state.saved_views import SavedViewRow, list_views
from viewspec.engine import metric_catalog
from viewspec.spec import CADENCES, TRANSFORMS

_PANEL_STYLE = """<style>
.vx-builder { border-radius:var(--radius); background:var(--surface);
  padding:12px 14px; margin:4px 0 12px; }
.vx-row { display:flex; gap:8px; align-items:center; flex-wrap:wrap; margin-bottom:10px; }
.vx-row label { color:var(--muted); font-size:var(--fs-caption); }
.vx-row input, .vx-row select {
  background:var(--paper, #1a1d23); color:var(--fg); border:1px solid var(--border);
  border-radius:var(--radius); padding:5px 9px; font-size:var(--fs-body); }
.vx-row input[name="tickers"] { width:260px; text-transform:uppercase; }
.vx-row input[name="periods"], .vx-row input[name="cagr_years"] { width:54px; }
.vx-row input[name="view_name"] { width:200px; }
.vx-row button { background:var(--accent-soft); color:var(--accent); border:1px solid var(--accent);
  border-radius:var(--radius); padding:5px 12px; font-size:var(--fs-body); cursor:pointer;
  transition:filter var(--transition); }
.vx-row button:hover { filter:brightness(1.15); }
.vx-pickers { display:grid; grid-template-columns:repeat(3, minmax(180px, 1fr)); gap:10px;
  margin-bottom:10px; }
.vx-picker label { display:block; color:var(--muted); font-size:var(--fs-caption); margin-bottom:3px;
  text-transform:uppercase; letter-spacing:.06em; }
.vx-picker select { width:100%; background:var(--paper, #1a1d23); color:var(--fg-soft, var(--fg));
  border:1px solid var(--border); border-radius:var(--radius); font-size:var(--fs-caption); padding:4px; }
.vx-saved-strip { display:flex; gap:6px; align-items:center; flex-wrap:wrap; }
.vx-saved { display:inline-flex; align-items:center; border:1px solid var(--border);
  border-radius:var(--radius); background:var(--paper, #1a1d23); overflow:hidden; }
.vx-saved button { background:transparent; border:none; color:var(--fg-soft, var(--fg));
  font-size:var(--fs-caption); padding:4px 8px; cursor:pointer; transition:color var(--transition); }
.vx-saved button[data-act="load"]:hover { color:var(--accent); }
.vx-saved button[data-act="del"] { color:var(--muted); border-left:1px solid var(--border);
  padding:4px 7px; }
.vx-saved button[data-act="del"]:hover { color:var(--bad); }
.vx-none { color:var(--muted); font-size:var(--fs-caption); }
.vx-error { color:var(--bad); font-size:var(--fs-body); margin:6px 0; }
.vx-hint { color:var(--muted); font-size:var(--fs-caption); margin-top:10px; }
.vx-nl { border-bottom:1px solid var(--border); padding-bottom:10px; }
.vx-nl input[name="nl_query"] { flex:1; min-width:280px; }
.vx-nl-msg { color:var(--muted); font-size:var(--fs-caption); }

/* ---- Ask thread (UX redesign PR5) ---- */
.ask-thread { display:flex; flex-direction:column; gap:12px; margin:4px 0 14px; }
.ask-hello { color:var(--muted); font-size:var(--fs-body); line-height:1.5; border:1px dashed var(--border);
  border-radius:var(--radius); padding:14px 16px; }
.ask-chips { display:flex; gap:8px; flex-wrap:wrap; margin-top:10px; }
.ask-chip { background:var(--paper, #1a1d23); border:1px solid var(--border); color:var(--fg-soft, var(--fg));
  border-radius:var(--radius-full); padding:5px 12px; font-size:var(--fs-caption); cursor:pointer;
  transition:color var(--transition), border-color var(--transition); }
.ask-chip:hover { border-color:var(--accent); color:var(--accent); }
.ask-turn-user { align-self:flex-end; max-width:70%; background:var(--accent-soft); border:1px solid var(--accent);
  color:var(--fg); border-radius:var(--radius) var(--radius) 4px var(--radius); padding:8px 14px; font-size:var(--fs-body); }
.ask-turn-assistant { align-self:stretch; border:1px solid var(--border); background:var(--surface);
  border-radius:var(--radius); padding:12px 14px; }
.ask-meta { color:var(--muted); font-size:var(--fs-caption); margin-bottom:8px; display:flex; gap:10px;
  align-items:baseline; flex-wrap:wrap; }
.ask-meta .ask-err { color:var(--bad); }
.ask-actions { margin-top:8px; display:flex; gap:8px; }
.ask-actions button { background:transparent; border:1px solid var(--border); color:var(--muted);
  border-radius:var(--radius); padding:3px 10px; font-size:var(--fs-caption); cursor:pointer;
  transition:color var(--transition), border-color var(--transition); }
.ask-actions button:hover { border-color:var(--accent); color:var(--accent); }
.ask-busy { color:var(--muted); font-size:var(--fs-body); }
.ask-busy .dots::after { content:'…'; animation: askdots 1.2s steps(4, end) infinite; }
.ask-cite-row { margin-top:8px; display:flex; gap:6px; flex-wrap:wrap; }
.ask-cite { font-size:var(--fs-caption); color:var(--accent); border:1px solid var(--border);
  border-radius:var(--radius-full); padding:2px 9px; text-decoration:none;
  background:var(--paper, #1a1d23); transition:border-color var(--transition); }
.ask-cite:hover { border-color:var(--accent); }
.ask-prose a.ask-cite-mark { color:var(--accent); text-decoration:none; font-size:0.85em;
  vertical-align:super; }
@keyframes askdots { 0% { content:''; } 25% { content:'.'; } 50% { content:'..'; } 75% { content:'...'; } }
.ask-prose { font-size:var(--fs-body); line-height:1.55; color:var(--fg); }
.ask-prose p { margin:0 0 8px; }
.ask-prose ul { margin:4px 0 8px 18px; padding:0; }
.ask-prose li { margin:2px 0; }
.ask-prose code { font-family:var(--font-mono, monospace); font-size:0.93em;
  background:rgba(255,255,255,0.05); padding:1px 4px; border-radius:3px; }
.ask-prose pre.ask-code { background:rgba(0,0,0,0.3); border:1px solid var(--border);
  border-radius:var(--radius); padding:8px 10px; overflow-x:auto; font-size:var(--fs-caption);
  font-family:var(--font-mono, monospace); margin:6px 0; }
.ask-cmd { font-family:var(--font-mono, monospace); font-size:var(--fs-caption); white-space:pre-wrap;
  color:var(--fg); margin:0; }
.ask-inputrow { display:flex; gap:8px; align-items:center; margin-bottom:10px; }
.ask-inputrow input { flex:1; background:var(--paper, #1a1d23); color:var(--fg);
  border:1px solid var(--border); border-radius:var(--radius); padding:9px 13px; font-size:var(--fs-section); }
.ask-inputrow input:focus { outline:none; border-color:var(--accent); }
.ask-inputrow button { background:var(--accent-soft); color:var(--accent); border:1px solid var(--accent);
  border-radius:var(--radius); padding:9px 16px; font-size:var(--fs-body); cursor:pointer; }
.ask-ctx { color:var(--muted); font-size:var(--fs-caption); }
.ask-ctx a { color:var(--accent); cursor:pointer; }
.ask-builder-pop { border:1px solid var(--accent); border-radius:var(--radius); background:var(--surface);
  padding:0 14px 12px; margin-top:10px; box-shadow:0 14px 44px rgba(0,0,0,0.5); }
.ask-pop-head { display:flex; justify-content:space-between; align-items:center; padding:10px 0;
  font-size:var(--fs-body); font-weight:600; color:var(--fg); }
.ask-pop-head button { background:transparent; border:none; color:var(--muted); font-size:16px;
  cursor:pointer; padding:0 4px; }
.ask-pop-head button:hover { color:var(--fg); }
.ask-inputrow #ask-diy { color:var(--muted); border-color:var(--border); background:transparent; }
.ask-inputrow #ask-diy:hover { color:var(--accent); border-color:var(--accent); }
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
  function selectedTokens() {
    var out = [];
    ['vx-pick-fin', 'vx-pick-kpi', 'vx-pick-seg'].forEach(function (id) {
      var sel = el(id);
      for (var i = 0; i < sel.options.length; i++) {
        if (sel.options[i].selected) out.push(sel.options[i].value);
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
    sel.innerHTML = '';
    (entries || []).forEach(function (e) {
      var opt = document.createElement('option');
      opt.value = e.token;
      opt.textContent = e.label + (e.tickers > 1 ? ' (' + e.tickers + ')' : '');
      if (e.title) opt.title = e.title;
      if (keep && keep.indexOf(e.token) !== -1) opt.selected = true;
      sel.appendChild(opt);
    });
  }
  function loadCatalog(preselect, then) {
    var qs = new URLSearchParams({tickers: tickers().join(',')});
    fetch('/api/viewspec/catalog?' + qs).then(function (r) { return r.json(); })
      .then(function (cat) {
        fillPicker('vx-pick-fin', cat.fin, preselect);
        fillPicker('vx-pick-kpi', cat.kpi, preselect);
        fillPicker('vx-pick-seg', cat.seg, preselect);
        if (then) then();
      });
  }
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

  // ---- Scored peers (Ask v4): /api/peers/<T> serves the PR #400 peer
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
        body: JSON.stringify({spec: newSpec})
      }).then(function (r) {
        if (!r.ok) throw new Error('run failed');
        return r.text();
      }).then(function (h) {
        lastSpec = newSpec;
        setCtx(true);
        var added = merged.filter(function (t) { return current.indexOf(t) === -1; });
        card.innerHTML = '<div class="ask-meta">' + askEsc(base + ' + scored peers: ' + added.join(', ')) + '</div>'
          + h + askActionsHtml();
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
  function askLinkifyCites(html, items) {
    var map = {};
    (items || []).forEach(function (c) { if (c && c.n) map[String(c.n)] = c; });
    return html.replace(/\\[(\\d{1,2})\\]/g, function (m, n) {
      var c = map[n];
      var href = askCiteHref(c);
      if (!href) return m;
      return '<a class="ask-cite-mark" href="' + askEsc(href) + '" target="_blank" title="'
        + askEsc(c.label || '') + '">[' + n + ']</a>';
    });
  }
  function askCiteRowHtml(items) {
    if (!items || !items.length) return '';
    var chips = items.map(function (c) {
      var href = askCiteHref(c);
      if (!href) return '';
      return '<a class="ask-cite" href="' + askEsc(href) + '" target="_blank">['
        + askEsc(String(c.n)) + '] ' + askEsc(c.label || c.kind || 'source') + '</a>';
    }).join('');
    return chips ? '<div class="ask-cite-row">' + chips + '</div>' : '';
  }
  function askActionsHtml() {
    return '<div class="ask-actions">'
      + '<button type="button" data-ask-act="builder">Open in builder</button>'
      + '<button type="button" data-ask-act="peers">+ Peers</button>'
      + '<button type="button" data-ask-act="pin">Pin as view</button>'
      + '</div>';
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
      prose.className = 'ask-prose';
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
        card.innerHTML = '<div class="ask-meta">' + askEsc(msg) + '</div>'
          + (frag.html || '')
          + askActionsHtml();
        card.setAttribute('data-ask-spec', JSON.stringify(frag.spec || {}));
        askRemember('assistant', msg);
      } else if (finalEv) {
        var text = finalEv.text || proseText || '';
        if (finalEv.route === 'command') {
          card.innerHTML = '<pre class="ask-cmd">' + askEsc(text) + '</pre>';
        } else {
          var noteHtml = note ? '<div class="ask-meta">' + askEsc(note) + '</div>' : '';
          card.innerHTML = noteHtml
            + '<div class="ask-prose">' + askLinkifyCites(askMd(text), citations) + '</div>'
            + askCiteRowHtml(citations);
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
    var chip = ev.target.closest('.ask-chip');
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
  // out to this tab; replay it as text turns + seed the narrative history.
  // Registered BEFORE the palette consumer so a pending question lands
  // after the replayed thread.
  function consumeDockThread() {
    var raw = null;
    try {
      raw = sessionStorage.getItem('cc-ask-thread');
      if (raw) sessionStorage.removeItem('cc-ask-thread');
    } catch (e) { return; }
    if (!raw) return;
    var turns = [];
    try { turns = JSON.parse(raw) || []; } catch (e) { return; }
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
        div.innerHTML = '<div class="ask-prose">' + askMd(t.text) + '</div>';
      }
      askThread.appendChild(div);
    });
    askScroll();
  }
  window.addEventListener('cc-ask-q', consumeDockThread);
  consumeDockThread();

  // Ctrl/Cmd+K handoff: the shell palette stashes the typed query in
  // sessionStorage and jumps to #explore; consume it at wire-up (lazy
  // panel load) or on the palette's event (panel already loaded).
  function consumePaletteQuery() {
    var q = null;
    try { q = sessionStorage.getItem('cc-ask-q'); } catch (e) { return; }
    if (!q || askBusy) return;
    try { sessionStorage.removeItem('cc-ask-q'); } catch (e) {}
    submitAsk(q);
  }
  window.addEventListener('cc-ask-q', consumePaletteQuery);
  consumePaletteQuery();

  // Saved-view handoff (UX9b): the shell palette stashes a chosen view id and
  // jumps to #explore. Open the advanced builder fold and click that view's
  // load chip — reusing the same delegated load+run path the chip strip uses.
  function consumePaletteView() {
    var id = null;
    try { id = sessionStorage.getItem('cc-view-id'); } catch (e) { return; }
    if (!id) return;
    try { sessionStorage.removeItem('cc-view-id'); } catch (e) {}
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
        f'<button type="button" data-act="load" title="load + run">{escape(view.name)}</button>'
        '<button type="button" data-act="del" title="delete">&times;</button>'
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


def _picker_html(dom_id: str, label: str, entries: list[dict[str, object]]) -> str:
    opts: list[str] = []
    for e in entries:
        token = escape(str(e.get("token") or ""))
        text = str(e.get("label") or "")
        n_raw = e.get("tickers")
        n = n_raw if isinstance(n_raw, int) else 0
        suffix = f" ({n})" if n > 1 else ""
        title_raw = e.get("title")
        title_attr = f' title="{escape(str(title_raw))}"' if title_raw else ""
        opts.append(f'<option value="{token}"{title_attr}>{escape(text + suffix)}</option>')
    return (
        f'<div class="vx-picker"><label>{escape(label)}</label>'
        f'<select id="{dom_id}" multiple size="9">{"".join(opts)}</select></div>'
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


def render_explore_panel(db_path: Path, *, user_id: str = DEFAULT_USER_ID) -> str:
    """The Research → Explore tab fragment: builder + saved views + result."""
    tickers = _default_tickers(db_path, user_id)
    catalog: dict[str, list[dict[str, object]]] = (
        metric_catalog(db_path, tickers) if tickers else {"fin": [], "kpi": [], "seg": []}
    )
    transform_opts = "".join(
        f'<option value="{escape(t)}"{" selected" if t == "level" else ""}>{escape(t)}</option>'
        for t in TRANSFORMS
    )
    cadence_opts = "".join(f'<option value="{escape(c)}">{escape(c)}</option>' for c in CADENCES)
    tickers_val = escape(", ".join(tickers))
    saved = render_saved_views_list(db_path, user_id=user_id)
    first = tickers[0] if tickers else "NU"
    second = tickers[1] if len(tickers) > 1 else "MELI"
    return f"""{_PANEL_STYLE}
<h2>Ask</h2>
<div id="vx-root">
<div class="ask-thread" id="ask-thread">
  <div class="ask-hello">Ask anything across the tracked universe. Metric questions
 come back as live tables and charts with per-number source chips; narrative questions
 (&ldquo;why&rdquo;, &ldquo;what&rsquo;s the bear case&rdquo;) get a researched answer.
 Follow-ups refine the last answer (&ldquo;now annual&rdquo;, &ldquo;add {escape(second)}&rdquo;,
 &ldquo;same but as margins&rdquo;). <code>/view</code> forces a data view; <code>/help</code>
 lists commands.
    <div class="ask-chips">
      <button type="button" class="ask-chip"
        data-ask-q="{escape(first)} vs {escape(second)} revenue growth, last 8 quarters">
        {escape(first)} vs {escape(second)} revenue growth</button>
      <button type="button" class="ask-chip"
        data-ask-q="{escape(first)} margins, last 12 quarters">{escape(first)} margins</button>
      <button type="button" class="ask-chip"
        data-ask-q="revenue 3-year CAGR for {escape(first)}, {escape(second)}, annual">
        3y revenue CAGR</button>
    </div>
  </div>
</div>
<div class="ask-inputrow">
  <input id="ask-q" placeholder="Ask — e.g. {escape(first)} vs {escape(second)} revenue growth, last 8 quarters" autocomplete="off">
  <button type="button" id="ask-go">Ask</button>
  <button type="button" id="ask-diy"
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
    <button type="button" id="vx-nl-go">Compile</button>
    <span class="vx-nl-msg" id="vx-nl-msg">compiles into the builder below &mdash; never raw
 SQL; falls back to the pickers when it can&#x27;t parse</span>
  </div>
  <div class="vx-row">
    <label>tickers</label>
    <input id="vx-tickers" name="tickers" value="{tickers_val}"
      placeholder="NU, MELI, &hellip;">
    <button type="button" id="vx-load-metrics"
      title="Refresh the metric pickers for these tickers">Load metrics</button>
    <button type="button" id="vx-peers"
      title="Append the first ticker&#x27;s scored peer set (named rivals, same industry, tracked names)">+ Peers</button>
    <label>transform</label>
    <select id="vx-transform">{transform_opts}</select>
    <label>cadence</label>
    <select id="vx-cadence">{cadence_opts}</select>
    <label>periods</label>
    <input id="vx-periods" name="periods" type="number" min="1" max="40" value="12">
    <label>CAGR yrs</label>
    <input id="vx-cagr-years" name="cagr_years" type="number" min="1" max="10" value="3">
    <button type="button" id="vx-run">Run view</button>
  </div>
  <div class="vx-pickers">
    {_picker_html("vx-pick-fin", "Financial line items", catalog["fin"])}
    {_picker_html("vx-pick-kpi", "KPIs", catalog["kpi"])}
    {_picker_html("vx-pick-seg", "Segments", catalog["seg"])}
  </div>
  <div class="vx-row">
    <input id="vx-view-name" name="view_name" placeholder="view name">
    <button type="button" id="vx-save">Save view</button>
    <span class="vx-saved-strip" id="vx-saved-strip">{saved}</span>
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
