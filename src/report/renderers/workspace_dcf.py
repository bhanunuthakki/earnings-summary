"""In-app DCF editor — the modify→recompute loop on the valuation card.

Resolves the lone CRITICAL gap from the Close-the-Loops re-score: the
valuation card was read-only, so changing one assumption meant a Sheets/Excel
round-trip. This module surfaces the pure DCF engine (PR1's
``/api/dcf/recompute``) as editable controls under the valuation summary: edit
WACC / terminal method+multiple / margins / segment growth / the CAPM drivers,
and the Bull·Base·Bull scenarios + an in-app WACC×exit-multiple sensitivity
heatmap live-update on every keystroke — no xlsx in the loop.

"Save to model" (``/api/dcf/save``) is the explicit in-app commit: it writes the
edited cell-backed levers onto ``dcf/<T>.xlsx``, records the change in the S11
override ledger (the immutable Opus baseline is never overwritten), and
re-persists ``dcf_runs``. Push-to-Sheets (the card's existing "Open in Google
Sheets" link) stays the publish-to-Sheet commit.

WACC is a derived output (no input cell): editing a CAPM driver re-derives WACC
client-side via the same formula ``redesign.read_inputs`` uses, so a driver
change is durable; editing the WACC field directly is a preview-only override.

Self-contained CSS+JS like ``workspace_chat`` / ``workspace_comments``; the
shell composes ``CSS``/``JS`` into the document and calls ``render_dcf_editor``.
Token-only styling (design_language §2/§7) — no raw hex; ``test_ui_controls``
registers this surface.
"""

from __future__ import annotations

from io import StringIO

from report.renderers.workspace_sections._shared import _esc

__all__ = ["CSS", "JS", "dcf_inject_button", "dcf_inject_for_kpi", "render_dcf_editor"]

# --- Wave 5: KPI -> DCF driver mapping ------------------------------------
# A captured report value maps to a DCF input when its NAME matches an
# assumption AND its value can be put in MODEL units (a ratio for the percent
# fields; raw for beta / exit multiple). The match is conservative: an unknown
# unit or an out-of-range value yields no affordance (safe default — better to
# omit the link than inject a garbage assumption).
_DCF_KPI_PATTERNS: tuple[tuple[str, str], ...] = (
    ("effective tax", "tax_rate"),
    ("tax rate", "tax_rate"),
    ("cost of debt", "cost_of_debt"),
    ("equity risk premium", "equity_risk_premium"),
    ("risk-free", "risk_free_rate"),
    ("risk free", "risk_free_rate"),
    ("terminal growth", "terminal_growth_g"),
    ("exit multiple", "exit_multiple"),
    ("operating margin", "near_op_margin"),
    ("op margin", "near_op_margin"),
    ("wacc", "wacc"),
    ("beta", "beta"),
)
_DCF_RATIO_KEYS: frozenset[str] = frozenset(
    {
        "tax_rate",
        "cost_of_debt",
        "equity_risk_premium",
        "risk_free_rate",
        "terminal_growth_g",
        "near_op_margin",
        "wacc",
    }
)


def dcf_inject_for_kpi(
    name: str, value: float | None, unit: str | None
) -> tuple[str, float] | None:
    """Map a captured KPI to ``(dcf_input_key, value_in_model_units)`` or None
    when it doesn't cleanly map. Percent fields require a known unit so the
    percent->ratio conversion is unambiguous, and the result is range-bound so an
    odd capture can't inject an absurd assumption."""
    if value is None:
        return None
    low = name.lower()
    key: str | None = None
    for pat, candidate in _DCF_KPI_PATTERNS:
        if pat in low:
            key = candidate
            break
    if key is None:
        return None
    if key in _DCF_RATIO_KEYS:
        unit_norm = (unit or "").strip().lower()
        if unit_norm in ("%", "pct", "percent"):
            ratio = value / 100.0
        elif unit_norm in ("", "ratio") and abs(value) <= 1.5:
            ratio = value  # already a ratio
        else:
            return None
        if not (-0.5 <= ratio <= 1.5):
            return None
        return (key, ratio)
    # beta / exit multiple — raw value, light sanity bounds.
    if not (-5.0 <= value <= 100.0):
        return None
    return (key, value)


def dcf_inject_button(key: str, value: float, label: str) -> str:
    """The "-> DCF" affordance the DCF editor's global click handler picks up
    (``data-dcf-inject``). ``value`` is already in DCF model units."""
    return (
        '<button type="button" class="dcf-inject-btn k-btn k-btn-quiet k-btn-sm" '
        f'data-dcf-inject="{_esc(key)}" data-dcf-value="{value:.6f}" '
        f'data-dcf-label="{_esc(label)}" '
        f'title="Use {_esc(label)} in the DCF editor">&#8594; DCF</button>'
    )


def render_dcf_editor(body: StringIO, ticker: str) -> None:
    """Emit the editor shell under the valuation summary (Thesis tab).

    Static markup only — the controls + scenario block + heatmap are built by
    ``JS`` from the live ``/api/dcf/inputs`` response, so the rendered document
    is deterministic and the editor degrades to its collapsed launcher when the
    research server is offline or no redesigned workbook exists for the ticker.
    """
    body.write(
        f'<section class="dcf-edit" id="dcf-edit" data-dcf-ticker="{_esc(ticker)}">'
        '<button type="button" class="k-btn k-btn-quiet dcf-edit-toggle" '
        'id="dcf-edit-toggle" aria-expanded="false">Edit assumptions &amp; re-run ↻'
        "</button>"
        '<div class="dcf-edit-body" id="dcf-edit-body" hidden>'
        '<div class="dcf-edit-status" id="dcf-edit-status" role="status"></div>'
        '<div class="dcf-edit-cols">'
        '<div class="dcf-edit-controls" id="dcf-edit-controls"></div>'
        '<div class="dcf-edit-out">'
        '<div class="dcf-edit-scenarios" id="dcf-edit-scenarios"></div>'
        '<div class="dcf-heatmap" id="dcf-edit-heatmap"></div>'
        "</div></div>"
        '<div class="dcf-edit-actions">'
        '<span class="dcf-edit-hint">Live preview · Save writes the override ledger '
        "(baseline untouched)</span>"
        '<button type="button" class="k-btn k-btn-quiet k-btn-sm" id="dcf-edit-reset">'
        "Reset</button>"
        '<button type="button" class="k-btn k-btn-primary k-btn-sm" id="dcf-edit-save">'
        "Save to model</button>"
        "</div></div></section>"
    )


CSS = """
.dcf-edit { margin-top: var(--sp-3); border: 1px solid var(--border);
  border-radius: var(--radius); padding: var(--sp-3); background: var(--surface); }
.dcf-edit-toggle { font-size: var(--fs-caption); }
.dcf-edit-body { margin-top: var(--sp-3); }
.dcf-edit-status { font-size: var(--fs-caption); color: var(--muted);
  min-height: 1.2em; margin-bottom: var(--sp-2); }
.dcf-edit-status.is-bad { color: var(--bad); }
.dcf-edit-status.is-ok { color: var(--ok); }
.dcf-edit-cols { display: grid; grid-template-columns: minmax(0, 1.1fr) minmax(0, 1fr);
  gap: var(--sp-4); align-items: start; }
@media (max-width: 720px) { .dcf-edit-cols { grid-template-columns: 1fr; } }
.dcf-edit-group { margin-bottom: var(--sp-3); }
.dcf-edit-group-title { font-size: var(--fs-micro); font-weight: 600; color: var(--muted);
  text-transform: uppercase; letter-spacing: 0.05em; margin-bottom: var(--sp-2); }
.dcf-edit-fields { display: grid; grid-template-columns: repeat(auto-fill, minmax(132px, 1fr));
  gap: var(--sp-2); }
.dcf-edit-field { display: flex; flex-direction: column; gap: 2px; }
.dcf-edit-field label { font-size: var(--fs-micro); color: var(--muted); }
.dcf-edit-field input, .dcf-edit-field select { font-size: var(--fs-caption);
  padding: 3px 6px; font-variant-numeric: tabular-nums; }
.dcf-edit-field.is-derived input { color: var(--muted); font-style: italic; }
.dcf-seg-grid { display: grid; grid-template-columns: minmax(0, 1fr) auto auto; gap: var(--sp-2);
  align-items: center; }
.dcf-seg-grid .dcf-seg-name { font-size: var(--fs-caption); color: var(--fg-soft);
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.dcf-seg-grid input { width: 5.5em; font-size: var(--fs-caption); padding: 3px 6px;
  font-variant-numeric: tabular-nums; }
.dcf-seg-head { font-size: var(--fs-micro); color: var(--muted); text-transform: uppercase;
  letter-spacing: 0.04em; text-align: center; }
.dcf-edit-scenarios { display: flex; gap: var(--sp-2); margin-bottom: var(--sp-3); }
.dcf-scn { flex: 1; border: 1px solid var(--border); border-radius: var(--radius);
  padding: var(--sp-2); text-align: center; }
.dcf-scn-label { font-size: var(--fs-micro); color: var(--muted); text-transform: uppercase;
  letter-spacing: 0.05em; }
.dcf-scn-val { font-size: var(--fs-body); font-weight: 600; color: var(--fg);
  font-variant-numeric: tabular-nums; }
.dcf-scn-up { font-size: var(--fs-micro); font-variant-numeric: tabular-nums; }
.dcf-scn-up.pos { color: var(--ok); }
.dcf-scn-up.neg { color: var(--bad); }
.dcf-scn-up.muted { color: var(--muted); }
.dcf-scn.base { border-color: var(--accent); }
.dcf-heatmap { overflow-x: auto; }
.dcf-hm-cap { font-size: var(--fs-micro); color: var(--muted); margin-bottom: var(--sp-1); }
.dcf-hm-table { border-collapse: collapse; font-size: var(--fs-micro);
  font-variant-numeric: tabular-nums; }
.dcf-hm-table th, .dcf-hm-table td { padding: var(--sp-1) var(--sp-2); text-align: right;
  border: 1px solid var(--hairline); }
.dcf-hm-table th { color: var(--muted); font-weight: 600; }
.dcf-hm-table td.base { outline: 2px solid var(--accent); outline-offset: -2px; font-weight: 600; }
.dcf-hm-axis { font-size: var(--fs-micro); color: var(--muted); }
/* Wave 5: a captured report value injected into a driver flashes; the report's
   "-> DCF" affordance is a quiet accent micro-button. */
.dcf-edit-field input.dcf-injected { outline: 2px solid var(--accent); outline-offset: 1px;
  background: color-mix(in srgb, var(--accent) 12%, transparent); }
.dcf-inject-btn { color: var(--accent); margin-left: var(--sp-1); white-space: nowrap; }
"""


# Vanilla IIFE (no framework, matching workspace_comments/_chat). Plain string —
# NOT an f-string — so the JS braces need no doubling. No raw hex anywhere (the
# heatmap tints via color-mix over --ok/--bad tokens) so test_ui_controls passes.
JS = r"""
(function () {
  var root = document.getElementById('dcf-edit');
  if (!root) return;
  function readJson(id) {
    var el = document.getElementById(id);
    if (!el) return null;
    try { return JSON.parse(el.textContent); } catch (e) { return null; }
  }
  var boot = readJson('workspace-boot') || {};
  var SERVER_URL = boot.server_url || 'http://localhost:7421';
  var TICKER = root.getAttribute('data-dcf-ticker') || boot.ticker;

  var elToggle = document.getElementById('dcf-edit-toggle');
  var elBody = document.getElementById('dcf-edit-body');
  var elStatus = document.getElementById('dcf-edit-status');
  var elControls = document.getElementById('dcf-edit-controls');
  var elScenarios = document.getElementById('dcf-edit-scenarios');
  var elHeatmap = document.getElementById('dcf-edit-heatmap');
  var elReset = document.getElementById('dcf-edit-reset');
  var elSave = document.getElementById('dcf-edit-save');

  var loaded = null;   // canonical inputs as last fetched / saved
  var model = null;    // working copy with live edits
  var ready = false;
  var debounceTimer = null;

  // Rate-like fields edit as percent (x100); the rest are raw numbers.
  var SCALARS = [
    {key: 'wacc', label: 'WACC', pct: true, step: 0.1},
    {key: 'near_op_margin', label: 'Near op margin', pct: true, step: 0.5},
    {key: 'terminal_op_margin', label: 'Term op margin', pct: true, step: 0.5},
    {key: 'exit_multiple', label: 'Exit multiple', pct: false, step: 0.5},
    {key: 'terminal_growth_g', label: 'Terminal g', pct: true, step: 0.1},
    {key: 'tax_rate', label: 'Tax rate', pct: true, step: 0.5}
  ];
  var DRIVERS = [
    {key: 'beta', label: 'Beta', pct: false, step: 0.05},
    {key: 'risk_free_rate', label: 'Risk-free', pct: true, step: 0.1},
    {key: 'equity_risk_premium', label: 'ERP', pct: true, step: 0.1},
    {key: 'cost_of_debt', label: 'Cost of debt', pct: true, step: 0.1}
  ];
  var SPEC_BY_KEY = {};
  SCALARS.concat(DRIVERS).forEach(function (s) { SPEC_BY_KEY[s.key] = s; });
  var inputsByKey = {};   // key -> <input>, refreshed by buildControls (Wave 5)

  function setStatus(msg, tone) {
    elStatus.textContent = msg || '';
    elStatus.className = 'dcf-edit-status' + (tone ? ' is-' + tone : '');
  }
  function fmtMoney(x) {
    if (x === null || x === undefined || isNaN(x)) return '—';
    return '$' + Number(x).toFixed(2);
  }
  function fmtPct(x) { return (Number(x) * 100).toFixed(1) + '%'; }
  function fmtMult(x) { return Number(x).toFixed(1) + 'x'; }

  // The CAPM derivation, identical to redesign.read_inputs: editing a driver
  // re-derives WACC so the preview stays consistent (a direct WACC edit is a
  // preview-only override that the durable save expresses via the drivers).
  function deriveWacc(m) {
    var ke = m.risk_free_rate + m.beta * m.equity_risk_premium;
    var akd = m.cost_of_debt * (1 - m.tax_rate);
    var mcap = m.current_price * m.diluted_shares_m;
    var denom = mcap + m.total_debt_m;
    var ew = denom > 0 ? mcap / denom : 1.0;
    return ew * ke + (1 - ew) * akd;
  }

  function numField(spec, value, onChange) {
    var wrap = document.createElement('div');
    wrap.className = 'dcf-edit-field';
    var lab = document.createElement('label');
    lab.textContent = spec.label + (spec.pct ? ' (%)' : '');
    var inp = document.createElement('input');
    inp.type = 'number';
    inp.step = String(spec.step);
    inp.value = spec.pct ? (Number(value) * 100).toFixed(2) : String(value);
    inp.addEventListener('input', function () {
      var raw = parseFloat(inp.value);
      if (isNaN(raw)) return;
      onChange(spec.pct ? raw / 100 : raw);
    });
    wrap.appendChild(lab);
    wrap.appendChild(inp);
    return {wrap: wrap, input: inp};
  }

  function group(title) {
    var g = document.createElement('div');
    g.className = 'dcf-edit-group';
    var t = document.createElement('div');
    t.className = 'dcf-edit-group-title';
    t.textContent = title;
    g.appendChild(t);
    return g;
  }

  var waccInput = null;  // kept so driver edits can refresh the WACC display

  function buildControls() {
    elControls.textContent = '';

    // Terminal + valuation levers.
    var gVal = group('Terminal & valuation');
    var methodWrap = document.createElement('div');
    methodWrap.className = 'dcf-edit-field';
    var mlab = document.createElement('label');
    mlab.textContent = 'Terminal method';
    var sel = document.createElement('select');
    ['Exit multiple', 'Perpetuity'].forEach(function (opt) {
      var o = document.createElement('option');
      o.value = opt; o.textContent = opt;
      if (model.terminal_method === opt) o.selected = true;
      sel.appendChild(o);
    });
    sel.addEventListener('change', function () {
      model.terminal_method = sel.value;
      scheduleRecompute();
    });
    methodWrap.appendChild(mlab);
    methodWrap.appendChild(sel);
    var fieldsVal = document.createElement('div');
    fieldsVal.className = 'dcf-edit-fields';
    fieldsVal.appendChild(methodWrap);
    SCALARS.forEach(function (spec) {
      var f = numField(spec, model[spec.key], function (v) {
        model[spec.key] = v;
        scheduleRecompute();
      });
      if (spec.key === 'wacc') waccInput = f.input;
      inputsByKey[spec.key] = f.input;
      fieldsVal.appendChild(f.wrap);
    });
    gVal.appendChild(fieldsVal);
    elControls.appendChild(gVal);

    // CAPM drivers — editing one re-derives WACC (durable path).
    var gCapm = group('WACC drivers (re-derive WACC)');
    var fieldsCapm = document.createElement('div');
    fieldsCapm.className = 'dcf-edit-fields';
    DRIVERS.forEach(function (spec) {
      var f = numField(spec, model[spec.key], function (v) {
        model[spec.key] = v;
        model.wacc = deriveWacc(model);
        if (waccInput) waccInput.value = (model.wacc * 100).toFixed(2);
        scheduleRecompute();
      });
      inputsByKey[spec.key] = f.input;
      fieldsCapm.appendChild(f.wrap);
    });
    gCapm.appendChild(fieldsCapm);
    elControls.appendChild(gCapm);

    // Per-segment growth.
    var segs = model.segments || [];
    if (segs.length) {
      var gSeg = group('Segment growth (near / terminal)');
      var grid = document.createElement('div');
      grid.className = 'dcf-seg-grid';
      var h0 = document.createElement('div'); h0.className = 'dcf-seg-head'; h0.textContent = '';
      var h1 = document.createElement('div'); h1.className = 'dcf-seg-head'; h1.textContent = 'near %';
      var h2 = document.createElement('div'); h2.className = 'dcf-seg-head'; h2.textContent = 'term %';
      grid.appendChild(h0); grid.appendChild(h1); grid.appendChild(h2);
      segs.forEach(function (name) {
        var nm = document.createElement('div');
        nm.className = 'dcf-seg-name'; nm.textContent = name; nm.title = name;
        grid.appendChild(nm);
        grid.appendChild(segInput(model.near_growth_by_segment, name));
        grid.appendChild(segInput(model.terminal_growth_by_segment, name));
      });
      gSeg.appendChild(grid);
      elControls.appendChild(gSeg);
    }
  }

  function segInput(mapRef, name) {
    var inp = document.createElement('input');
    inp.type = 'number'; inp.step = '0.5';
    inp.value = (Number(mapRef[name]) * 100).toFixed(2);
    inp.addEventListener('input', function () {
      var raw = parseFloat(inp.value);
      if (isNaN(raw)) return;
      mapRef[name] = raw / 100;
      scheduleRecompute();
    });
    return inp;
  }

  function renderScenarios(data) {
    elScenarios.textContent = '';
    var price = data.current_price;
    var sc = data.scenarios || {};
    [['bear', 'Bear'], ['base', 'Base'], ['bull', 'Bull']].forEach(function (pair) {
      var key = pair[0];
      var cell = document.createElement('div');
      cell.className = 'dcf-scn' + (key === 'base' ? ' base' : '');
      var lab = document.createElement('div');
      lab.className = 'dcf-scn-label'; lab.textContent = pair[1];
      var val = document.createElement('div');
      val.className = 'dcf-scn-val'; val.textContent = fmtMoney(sc[key]);
      var up = document.createElement('div');
      var fv = sc[key];
      if (fv !== null && fv !== undefined && price) {
        var pct = (fv - price) / price * 100;
        up.className = 'dcf-scn-up ' + (pct >= 0 ? 'pos' : 'neg');
        up.textContent = (pct >= 0 ? '+' : '') + pct.toFixed(0) + '%';
      } else {
        up.className = 'dcf-scn-up muted'; up.textContent = '—';
      }
      cell.appendChild(lab); cell.appendChild(val); cell.appendChild(up);
      elScenarios.appendChild(cell);
    });
  }

  function renderHeatmap(sens) {
    elHeatmap.textContent = '';
    if (!sens || !sens.values) return;
    var price = sens.current_price || 0;
    var cap = document.createElement('div');
    cap.className = 'dcf-hm-cap';
    cap.textContent = 'Fair value / share - exit multiple (rows) x WACC (cols); '
      + 'green above price';
    elHeatmap.appendChild(cap);
    var tbl = document.createElement('table');
    tbl.className = 'dcf-hm-table';
    var thead = document.createElement('thead');
    var hr = document.createElement('tr');
    var corner = document.createElement('th');
    corner.className = 'dcf-hm-axis'; corner.textContent = 'mult \\ WACC';
    hr.appendChild(corner);
    sens.wacc_axis.forEach(function (w) {
      var th = document.createElement('th');
      th.textContent = fmtPct(w);
      hr.appendChild(th);
    });
    thead.appendChild(hr);
    tbl.appendChild(thead);
    var tbody = document.createElement('tbody');
    var mid = Math.floor(sens.values.length / 2);
    sens.values.forEach(function (row, i) {
      var tr = document.createElement('tr');
      var rh = document.createElement('th');
      rh.textContent = fmtMult(sens.multiple_axis[i]);
      tr.appendChild(rh);
      row.forEach(function (v, j) {
        var td = document.createElement('td');
        td.textContent = fmtMoney(v);
        var rel = price > 0 ? (v - price) / price : 0;
        var mag = Math.min(1, Math.abs(rel) / 0.5);
        var tone = rel >= 0 ? 'var(--ok)' : 'var(--bad)';
        var tint = Math.round(8 + mag * 30);
        td.style.background = 'color-mix(in srgb, ' + tone + ' ' + tint + '%, transparent)';
        td.title = fmtMult(sens.multiple_axis[i]) + ' · ' + fmtPct(sens.wacc_axis[j])
          + ' → ' + fmtMoney(v);
        if (i === mid && j === mid) td.className = 'base';
        tr.appendChild(td);
      });
      tbody.appendChild(tr);
    });
    tbl.appendChild(tbody);
    elHeatmap.appendChild(tbl);
  }

  function recompute() {
    if (!ready) return;
    setStatus('Recomputing…');
    fetch(SERVER_URL + '/api/dcf/recompute', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({inputs: model})
    }).then(function (r) {
      return r.json().then(function (j) { return {ok: r.ok, status: r.status, body: j}; });
    }).then(function (res) {
      if (!res.ok) {
        setStatus((res.body && res.body.error) || ('recompute failed (' + res.status + ')'), 'bad');
        return;
      }
      renderScenarios(res.body);
      renderHeatmap(res.body.sensitivity);
      var ou = res.body.over_under_pct;
      if (ou !== null && ou !== undefined) {
        var pct = (ou * 100);
        setStatus('Base ' + fmtMoney(res.body.fair_value_per_share_usd) + ' · '
          + (pct >= 0 ? 'over' : 'under') + ' by ' + Math.abs(pct).toFixed(0)
          + '% vs price · WACC ' + fmtPct(res.body.wacc), '');
      } else {
        setStatus('Base ' + fmtMoney(res.body.fair_value_per_share_usd)
          + ' · WACC ' + fmtPct(res.body.wacc), '');
      }
    }).catch(function () {
      setStatus('Research server offline — start comments_server to recompute.', 'bad');
    });
  }

  function scheduleRecompute() {
    if (debounceTimer) clearTimeout(debounceTimer);
    debounceTimer = setTimeout(recompute, 280);
  }

  function load() {
    setStatus('Loading model…');
    fetch(SERVER_URL + '/api/dcf/inputs/' + encodeURIComponent(TICKER))
      .then(function (r) {
        if (r.status === 404) { setStatus('No editable DCF model for this ticker.', ''); return null; }
        return r.json().then(function (j) { return {ok: r.ok, body: j}; });
      }).then(function (res) {
        if (!res) return;
        if (!res.ok || !res.body || !res.body.inputs) {
          setStatus((res.body && res.body.error) || 'Could not load the model.', 'bad');
          return;
        }
        loaded = res.body.inputs;
        model = JSON.parse(JSON.stringify(loaded));
        ready = true;
        buildControls();
        recompute();
        if (pendingInject) {
          var pi = pendingInject; pendingInject = null;
          applyInject(pi.key, pi.value, pi.label);
        }
      }).catch(function () {
        setStatus('Research server offline — start comments_server to edit.', 'bad');
      });
  }

  // --- Wave 5: KPI -> DCF driver injection ---------------------------------
  // A captured report value carries a "-> DCF" affordance
  // [data-dcf-inject=key data-dcf-value=<model units> data-dcf-label]. Clicking
  // it opens the editor, sets that input (re-deriving WACC for a CAPM driver),
  // and recomputes. If the editor hasn't loaded, the inject is queued and
  // applied once the model arrives.
  var pendingInject = null;
  function applyInject(key, value, label) {
    if (!model || !(key in model)) { setStatus('No DCF input "' + key + '".', 'bad'); return; }
    model[key] = value;
    if (DRIVERS.some(function (d) { return d.key === key; })) {
      model.wacc = deriveWacc(model);
      if (inputsByKey.wacc) inputsByKey.wacc.value = (model.wacc * 100).toFixed(2);
    }
    var inp = inputsByKey[key], spec = SPEC_BY_KEY[key];
    if (inp && spec) {
      inp.value = spec.pct ? (value * 100).toFixed(2) : String(value);
      inp.classList.add('dcf-injected');
      setTimeout(function () { inp.classList.remove('dcf-injected'); }, 1500);
    }
    setStatus('Injected ' + (label || key) + ' — recomputing…', 'ok');
    scheduleRecompute();
  }
  window.dcfSetDriver = function (key, value, label) {
    if (isNaN(value)) return;
    if (elBody.hidden) { elBody.hidden = false; elToggle.setAttribute('aria-expanded', 'true'); }
    root.scrollIntoView({behavior: 'smooth', block: 'center'});
    if (ready && model) {
      applyInject(key, value, label);
    } else {
      pendingInject = {key: key, value: value, label: label};
      if (loaded === null) load();
      else setStatus('Loading model to inject ' + (label || key) + '…', 'warn');
    }
  };
  document.addEventListener('click', function (ev) {
    var a = ev.target && ev.target.closest ? ev.target.closest('[data-dcf-inject]') : null;
    if (!a) return;
    ev.preventDefault();
    window.dcfSetDriver(
      a.getAttribute('data-dcf-inject'),
      parseFloat(a.getAttribute('data-dcf-value')),
      a.getAttribute('data-dcf-label') || ''
    );
  });

  elToggle.addEventListener('click', function () {
    var open = elBody.hidden;
    elBody.hidden = !open;
    elToggle.setAttribute('aria-expanded', open ? 'true' : 'false');
    if (open && !ready && loaded === null) load();
  });

  elReset.addEventListener('click', function () {
    if (!loaded) return;
    model = JSON.parse(JSON.stringify(loaded));
    buildControls();
    recompute();
  });

  elSave.addEventListener('click', function () {
    if (!ready) return;
    elSave.disabled = true;
    setStatus('Saving…');
    fetch(SERVER_URL + '/api/dcf/save', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ticker: TICKER, inputs: model})
    }).then(function (r) {
      return r.json().then(function (j) { return {ok: r.ok, status: r.status, body: j}; });
    }).then(function (res) {
      elSave.disabled = false;
      if (!res.ok) {
        setStatus((res.body && res.body.error) || ('save failed (' + res.status + ')'), 'bad');
        return;
      }
      // Adopt the canonical saved inputs (WACC re-derived from saved drivers) as
      // the new reset baseline, then re-render from the persisted state.
      if (res.body.inputs) {
        loaded = res.body.inputs;
        model = JSON.parse(JSON.stringify(loaded));
        buildControls();
      }
      if (res.body.sensitivity) { renderScenarios(res.body); renderHeatmap(res.body.sensitivity); }
      setStatus('Saved to model ✓ · override ledger updated (Opus baseline untouched).', 'ok');
    }).catch(function () {
      elSave.disabled = false;
      setStatus('Research server offline — could not save.', 'bad');
    });
  });
})();
"""
