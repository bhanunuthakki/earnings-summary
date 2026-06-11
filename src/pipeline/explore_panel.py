"""Research → Explore panel (master build P5.1): the ViewSpec builder UI.

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
.vx-builder { border:1px solid var(--border); border-radius:8px; background:var(--surface);
  padding:12px 14px; margin:4px 0 12px; }
.vx-row { display:flex; gap:8px; align-items:center; flex-wrap:wrap; margin-bottom:10px; }
.vx-row label { color:var(--muted); font-size:11.5px; }
.vx-row input, .vx-row select {
  background:var(--paper, #1a1d23); color:var(--fg); border:1px solid var(--border);
  border-radius:6px; padding:5px 9px; font-size:12.5px; }
.vx-row input[name="tickers"] { width:260px; text-transform:uppercase; }
.vx-row input[name="periods"], .vx-row input[name="cagr_years"] { width:54px; }
.vx-row input[name="view_name"] { width:200px; }
.vx-row button { background:#1c2138; color:var(--accent); border:1px solid var(--accent);
  border-radius:6px; padding:5px 12px; font-size:12.5px; cursor:pointer; }
.vx-row button:hover { filter:brightness(1.15); }
.vx-pickers { display:grid; grid-template-columns:repeat(3, minmax(180px, 1fr)); gap:10px;
  margin-bottom:10px; }
.vx-picker label { display:block; color:var(--muted); font-size:11px; margin-bottom:3px;
  text-transform:uppercase; letter-spacing:.05em; }
.vx-picker select { width:100%; background:var(--paper, #1a1d23); color:var(--fg-soft, var(--fg));
  border:1px solid var(--border); border-radius:6px; font-size:12px; padding:4px; }
.vx-saved-strip { display:flex; gap:6px; align-items:center; flex-wrap:wrap; }
.vx-saved { display:inline-flex; align-items:center; border:1px solid var(--border);
  border-radius:6px; background:var(--paper, #1a1d23); overflow:hidden; }
.vx-saved button { background:transparent; border:none; color:var(--fg-soft, var(--fg));
  font-size:12px; padding:4px 8px; cursor:pointer; }
.vx-saved button[data-act="load"]:hover { color:var(--accent); }
.vx-saved button[data-act="del"] { color:var(--muted); border-left:1px solid var(--border);
  padding:4px 7px; }
.vx-saved button[data-act="del"]:hover { color:var(--bad); }
.vx-none { color:var(--muted); font-size:12px; }
.vx-error { color:var(--bad); font-size:12.5px; margin:6px 0; }
.vx-hint { color:var(--muted); font-size:11.5px; margin-top:10px; }
.vx-nl { border-bottom:1px solid var(--border); padding-bottom:10px; }
.vx-nl input[name="nl_query"] { flex:1; min-width:280px; }
.vx-nl-msg { color:var(--muted); font-size:11.5px; }
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
        opts.append(f'<option value="{token}">{escape(text + suffix)}</option>')
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
    return f"""{_PANEL_STYLE}
<h2>Explore</h2>
<div id="vx-root">
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
<script>{_PANEL_JS}</script>"""
