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
from report.renderers.workspace_styles import DCF_CSS

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
        '<div class="dcf-edit-status" id="dcf-edit-status" role="status" aria-live="polite"></div>'
        '<button type="button" class="k-btn k-btn-quiet k-btn-sm" id="dcf-edit-retry" hidden>Retry loading model</button>'
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


CSS = DCF_CSS


# Vanilla IIFE (no framework, matching workspace_comments/_chat). Plain string —
# NOT an f-string — so the JS braces need no doubling. No raw hex anywhere (the
# heatmap tints via color-mix over --ok/--bad tokens) so test_ui_controls passes.
JS = r"""
(function () {
  var editors = new WeakMap();
  window.initDcfEditor = function (root, options) {
  if (!root) return null;
  if (editors.has(root)) return editors.get(root);
  options = options || {};
  var scope = options.scope || document;
  var listeners = new AbortController();
  var destroyed = false;
  var presentationTimers = new Set();
  function later(callback) {
    var timer = setTimeout(function () {
      presentationTimers.delete(timer);
      if (!destroyed) callback();
    }, 1500);
    presentationTimers.add(timer);
  }
  function listen(target, event, callback) {
    target.addEventListener(event, callback, {signal: listeners.signal});
  }
  function control(id) {
    // Inert shared bodies keep the publisher's reader-<digest>- ID namespace.
    return root.querySelector('[id="' + id + '"], [id$="-' + id + '"]');
  }
  function readJson(id) {
    var el = document.getElementById(id);
    if (!el) return null;
    try { return JSON.parse(el.textContent); } catch (e) { return null; }
  }
  var boot = readJson('workspace-boot') || {};
  var SERVER_URL = /^https?:$/.test(window.location.protocol)
    ? window.location.origin
    : (boot.server_url || 'http://localhost:7421');
  var MUTATION_HEADERS = options.mutationHeaders || window.__workspaceMutationHeaders || {'Content-Type': 'application/json'};
  function requestHeaders() {
    return typeof MUTATION_HEADERS === 'function' ? MUTATION_HEADERS() : MUTATION_HEADERS;
  }
  var TICKER = options.ticker || root.getAttribute('data-dcf-ticker') || boot.ticker;
  var loadedFamily = null;
  var editableKeys = [];
  var readOnlyReasons = {};
  var familyEdits = {};
  var authorValue = 'owner';
  var rationaleValue = '';

  var elToggle = control('dcf-edit-toggle');
  var elBody = control('dcf-edit-body');
  var elStatus = control('dcf-edit-status');
  var elControls = control('dcf-edit-controls');
  var elScenarios = control('dcf-edit-scenarios');
  var elHeatmap = control('dcf-edit-heatmap');
  var elReset = control('dcf-edit-reset');
  var elSave = control('dcf-edit-save');
  var elRetry = control('dcf-edit-retry'); // Older saved reports have no retry control.
  var retryInstruction = elRetry ? 'Retry loading the model.'
    : 'Close and reopen Edit assumptions to retry loading the model.';
  if (!TICKER || !elToggle || !elBody || !elStatus || !elControls || !elScenarios
      || !elHeatmap || !elReset || !elSave) return null;
  var loadController = null;
  var loadGeneration = 0;
  var INPUT_READ_TIMEOUT_MS = 15000;

  var loaded = null;   // canonical inputs as last fetched / saved
  var model = null;    // working copy with live edits
  var ready = false;
  var debounceTimer = null;
  var editGeneration = 0;
  var inputGeneration = 0;
  var modelGeneration = 0;
  var previewController = null;
  var PREVIEW_TIMEOUT_MS = 15000;
  var waccMode = 'override'; // Loading honors the workbook's current rate.
  var waccOverride = false;
  var dirty = false;
  var saving = false;

  // Rate-like fields edit as percent (x100); the rest are raw numbers.
  var SCALARS = [
    {key: 'wacc', label: 'WACC (preview only)', pct: true, step: 0.1},
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
  var syncRoot = document.createElement('div');
  syncRoot.className = 'dcf-report-sync k-well';
  syncRoot.setAttribute('data-report-sync-bar', '');
  root.insertBefore(syncRoot, elToggle);
  var syncBar = window.initReportSyncBar(syncRoot, {
    ticker: TICKER, candidateArtifactId: options.candidateArtifactId || null
  });

  function setStatus(msg, tone) {
    elStatus.textContent = msg || '';
    elStatus.className = 'dcf-edit-status';
    elStatus.dataset.tone = tone || 'neutral';
  }
  function fmtMoney(x) {
    if (x === null || x === undefined || isNaN(x)) return '—';
    return '$' + Number(x).toFixed(2);
  }
  function fmtPct(x) { return (Number(x) * 100).toFixed(1) + '%'; }
  function fmtMult(x) { return Number(x).toFixed(1) + 'x'; }

  function selectWaccMode(key) {
    if (key === 'wacc') { waccMode = 'override'; waccOverride = true; }
    else if (key === 'tax_rate' || DRIVERS.some(function (d) { return d.key === key; })) {
      waccMode = 'drivers'; waccOverride = false;
    }
  }
  function previewNote() {
    return waccOverride ? ' · WACC override is preview-only; save uses drivers.' : '';
  }
  function invalidatePreview() {
    editGeneration++;
    if (debounceTimer) clearTimeout(debounceTimer);
    debounceTimer = null;
    if (previewController) previewController.abort();
    previewController = null;
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
    listen(inp, 'input', function () {
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

    if (loadedFamily) {
      var familyGroup = group(loadedFamily === 'nu_platform_dcf'
        ? 'NU platform DCF assumptions' : 'MELI platform SOTP assumptions');
      var familyFields = document.createElement('div'); familyFields.className = 'dcf-edit-fields';
      editableKeys.forEach(function (key) {
        var isPct = !(/^(years|derive_|exit_|beta|op_exit_)/.test(key));
        var isInt = /^(years|derive_)/.test(key);
        var f = numField({key: key, label: key.replace(/_/g, ' '), pct: isPct,
          step: isInt ? 1 : (isPct ? 0.5 : 0.1)}, model[key], function (v) {
          var val = isInt ? Math.round(v) : v;
          model[key] = val; familyEdits[key] = val; scheduleRecompute();
        });
        inputsByKey[key] = f.input; familyFields.appendChild(f.wrap);
      });
      familyGroup.appendChild(familyFields); elControls.appendChild(familyGroup);
      if (Object.keys(readOnlyReasons).length) {
        var lockedGroup = group('Locked facts & allocations (read-only)');
        var lockedFields = document.createElement('div'); lockedFields.className = 'dcf-edit-fields';
        Object.keys(readOnlyReasons).forEach(function (roKey) {
          var wrap = document.createElement('div'); wrap.className = 'dcf-edit-field is-derived';
          var label = document.createElement('label'); label.textContent = roKey.replace(/_/g, ' ');
          var input = document.createElement('input'); input.type = 'text'; input.disabled = true;
          input.value = String(model[roKey] !== undefined ? model[roKey] : '—');
          input.title = readOnlyReasons[roKey];
          wrap.appendChild(label); wrap.appendChild(input); lockedFields.appendChild(wrap);
        });
        lockedGroup.appendChild(lockedFields); elControls.appendChild(lockedGroup);
      }
      var attributionGroup = group('Save attribution (required for family models)');
      var attributionFields = document.createElement('div'); attributionFields.className = 'dcf-edit-fields';
      var authorWrap = document.createElement('div'); authorWrap.className = 'dcf-edit-field';
      var authorLabel = document.createElement('label'); authorLabel.textContent = 'Author';
      var authorInput = document.createElement('input'); authorInput.type = 'text';
      authorInput.setAttribute('data-dcf-author', ''); authorInput.value = authorValue;
      listen(authorInput, 'input', function () { authorValue = authorInput.value; });
      authorWrap.appendChild(authorLabel); authorWrap.appendChild(authorInput);
      var rationaleWrap = document.createElement('div'); rationaleWrap.className = 'dcf-edit-field';
      var rationaleLabel = document.createElement('label'); rationaleLabel.textContent = 'Rationale';
      var rationaleInput = document.createElement('input'); rationaleInput.type = 'text';
      rationaleInput.setAttribute('data-dcf-rationale', ''); rationaleInput.placeholder = 'Rationale (min 20 chars)';
      rationaleInput.value = rationaleValue;
      listen(rationaleInput, 'input', function () { rationaleValue = rationaleInput.value; });
      rationaleWrap.appendChild(rationaleLabel); rationaleWrap.appendChild(rationaleInput);
      attributionFields.appendChild(authorWrap); attributionFields.appendChild(rationaleWrap);
      attributionGroup.appendChild(attributionFields); elControls.appendChild(attributionGroup);
      return;
    }

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
    listen(sel, 'change', function () {
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
        selectWaccMode(spec.key);
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
        selectWaccMode(spec.key);
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
    listen(inp, 'input', function () {
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
      cell.className = 'dcf-scn';
      cell.dataset.scenario = key;
      var lab = document.createElement('div');
      lab.className = 'dcf-scn-label'; lab.textContent = pair[1];
      var val = document.createElement('div');
      val.className = 'dcf-scn-val'; val.textContent = fmtMoney(sc[key]);
      var up = document.createElement('div');
      var fv = sc[key];
      if (fv !== null && fv !== undefined && price) {
        var pct = (fv - price) / price * 100;
        up.className = 'dcf-scn-up';
        up.dataset.tone = pct >= 0 ? 'positive' : 'negative';
        up.textContent = (pct >= 0 ? '+' : '') + pct.toFixed(0) + '%';
      } else {
        up.className = 'dcf-scn-up'; up.dataset.tone = 'muted'; up.textContent = '—';
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
        td.dataset.tone = rel >= 0 ? 'positive' : 'negative';
        td.dataset.intensity = String(Math.round(mag * 3));
        td.title = fmtMult(sens.multiple_axis[i]) + ' · ' + fmtPct(sens.wacc_axis[j])
          + ' → ' + fmtMoney(v);
        if (i === mid && j === mid) td.classList.add('base');
        tr.appendChild(td);
      });
      tbody.appendChild(tr);
    });
    tbl.appendChild(tbody);
    elHeatmap.appendChild(tbl);
  }

  function recompute() {
    if (!ready || destroyed) return;
    var generation = editGeneration;
    var controller = new AbortController();
    previewController = controller;
    var timedOut = false;
    var timer = setTimeout(function () { timedOut = true; controller.abort(); }, PREVIEW_TIMEOUT_MS);
    function current() { return generation === editGeneration && root.isConnected; }
    setStatus((dirty ? 'Unsaved edits · ' : '') + 'Recomputing…' + previewNote());
    return fetch(SERVER_URL + '/api/dcf/recompute', {
      method: 'POST',
      headers: requestHeaders(),
      signal: controller.signal,
      body: loadedFamily
        ? JSON.stringify({ticker: TICKER, family: loadedFamily, inputs: familyEdits})
        : JSON.stringify({inputs: model, wacc_mode: waccMode})
    }).then(function (r) {
      if (!current() || controller.signal.aborted) return null;
      return r.json().then(function (j) { return {ok: r.ok, status: r.status, body: j}; });
    }).then(function (res) {
      if (!current() || controller.signal.aborted || !res) return;
      if (!res.ok) {
        setStatus((dirty ? 'Unsaved edits · ' : '')
          + ((res.body && res.body.error) || ('recompute failed (' + res.status + ')')), 'bad');
        return;
      }
      if (loadedFamily) {
        elScenarios.textContent = '';
        var previewItems = [['Base', fmtMoney(res.body.value_per_share)]];
        if (loadedFamily === 'nu_platform_dcf') {
          previewItems.push(['RI', fmtMoney(res.body.ri_value_per_share)]);
          previewItems.push(['Exit P/E', res.body.exit_pe === undefined ? '—' : fmtMult(res.body.exit_pe)]);
        } else {
          previewItems.push(['Operating EV', fmtMoney(res.body.operating_ev)]);
          previewItems.push(['Credit Equity', fmtMoney(res.body.credit_equity)]);
        }
        previewItems.forEach(function (item) {
          var cell = document.createElement('div'); cell.className = 'dcf-scn';
          var label = document.createElement('div'); label.className = 'dcf-scn-label'; label.textContent = item[0];
          var value = document.createElement('div'); value.className = 'dcf-scn-val'; value.textContent = item[1];
          cell.appendChild(label); cell.appendChild(value); elScenarios.appendChild(cell);
        });
        elHeatmap.textContent = '';
        setStatus((dirty ? 'Unsaved edits · ' : '') + 'Fair value '
          + fmtMoney(res.body.value_per_share) + ' (' + loadedFamily + ')', '');
        return;
      }
      model.wacc = res.body.wacc;
      if (waccInput) waccInput.value = (model.wacc * 100).toFixed(2);
      renderScenarios(res.body);
      renderHeatmap(res.body.sensitivity);
      var ou = res.body.over_under_pct;
      if (ou !== null && ou !== undefined) {
        var pct = (ou * 100);
        setStatus((dirty ? 'Unsaved edits · ' : '') + 'Base ' + fmtMoney(res.body.fair_value_per_share_usd) + ' · '
          + (pct >= 0 ? 'over' : 'under') + ' by ' + Math.abs(pct).toFixed(0)
          + '% vs price · WACC ' + fmtPct(res.body.wacc) + previewNote(), '');
      } else {
        setStatus((dirty ? 'Unsaved edits · ' : '') + 'Base ' + fmtMoney(res.body.fair_value_per_share_usd)
          + ' · WACC ' + fmtPct(res.body.wacc) + previewNote(), '');
      }
    }).catch(function () {
      if (!current() || (controller.signal.aborted && !timedOut)) return;
      setStatus((dirty ? 'Unsaved edits · ' : '') + (timedOut
        ? 'Preview timed out. Edit an assumption to try again.'
        : 'Research server unavailable. Edit an assumption to try again.'), 'bad');
    }).finally(function () {
      clearTimeout(timer);
      if (previewController === controller) previewController = null;
    });
  }

  function scheduleRecompute() {
    invalidatePreview(); // Invalidate before the debounce gap, not at dispatch.
    inputGeneration++;
    dirty = true;
    setStatus('Unsaved edits · Recomputing…' + previewNote());
    debounceTimer = setTimeout(function () { debounceTimer = null; recompute(); }, 280);
  }

  function cancelLoad() {
    invalidatePreview();
    loadGeneration++;
    if (loadController) loadController.abort();
    loadController = null;
  }
  function load() {
    if (destroyed) return;
    cancelLoad();
    modelGeneration++;
    inputGeneration++;
    ready = false;
    var request = loadGeneration;
    var controller = new AbortController();
    loadController = controller;
    // This report may run from a file against its configured server origin.
    // Keep the deadline active through JSON body consumption, not only headers.
    var timer = setTimeout(function () { controller.abort(); }, INPUT_READ_TIMEOUT_MS);
    if (elRetry) elRetry.hidden = true;
    setStatus('Loading model…');
    return fetch(SERVER_URL + '/api/dcf/inputs/' + encodeURIComponent(TICKER), {
      headers: requestHeaders(),
      signal: controller.signal
    })
      .then(function (r) {
        if (request !== loadGeneration || controller.signal.aborted) return null;
        if (r.status === 404) { setStatus('No editable DCF model for this ticker.', ''); return null; }
        return r.json().then(function (j) { return {ok: r.ok, body: j}; });
      }).then(function (res) {
        if (request !== loadGeneration || controller.signal.aborted || !root.isConnected || !res) return;
        if (!res.ok || !res.body || !res.body.inputs) {
          setStatus(((res.body && res.body.error) || 'Could not load the model.')
            + (elRetry ? '' : ' ' + retryInstruction), 'bad');
          if (elRetry) elRetry.hidden = false;
          return;
        }
        loaded = res.body.inputs;
        loadedFamily = res.body.family || null;
        editableKeys = Array.isArray(res.body.editable) ? res.body.editable : [];
        readOnlyReasons = res.body.read_only || {};
        familyEdits = {};
        model = JSON.parse(JSON.stringify(loaded));
        waccMode = 'override'; waccOverride = false; dirty = false;
        ready = true;
        buildControls();
        recompute();
        if (pendingInject) {
          var pi = pendingInject; pendingInject = null;
          applyInject(pi.key, pi.value, pi.label);
        }
      }).catch(function () {
        if (request !== loadGeneration || !root.isConnected) return;
        setStatus((controller.signal.aborted
          ? 'Model loading timed out. '
          : 'Model could not load from the research server. ') + retryInstruction, 'bad');
        if (elRetry) elRetry.hidden = false;
      }).finally(function () {
        clearTimeout(timer);
        if (loadController === controller) loadController = null;
      });
  }
  if (elRetry) listen(elRetry, 'click', load);
  listen(window, 'pagehide', cancelLoad);

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
    selectWaccMode(key);
    var inp = inputsByKey[key], spec = SPEC_BY_KEY[key];
    if (inp && spec) {
      inp.value = spec.pct ? (value * 100).toFixed(2) : String(value);
      inp.classList.add('dcf-injected');
      later(function () { inp.classList.remove('dcf-injected'); });
    }
    setStatus('Injected ' + (label || key) + ' — recomputing…', 'ok');
    scheduleRecompute();
  }
  function setDriver(key, value, label) {
    if (destroyed || isNaN(value)) return;
    if (elBody.hidden) { elBody.hidden = false; elToggle.setAttribute('aria-expanded', 'true'); }
    root.scrollIntoView({behavior: 'smooth', block: 'center'});
    if (ready && model) {
      applyInject(key, value, label);
    } else {
      pendingInject = {key: key, value: value, label: label};
      if (loaded === null) load();
      else setStatus('Loading model to inject ' + (label || key) + '…', 'warn');
    }
  }
  listen(scope, 'click', function (ev) {
    var a = ev.target && ev.target.closest ? ev.target.closest('[data-dcf-inject]') : null;
    if (!a) return;
    ev.preventDefault();
    setDriver(
      a.getAttribute('data-dcf-inject'),
      parseFloat(a.getAttribute('data-dcf-value')),
      a.getAttribute('data-dcf-label') || ''
    );
  });

  listen(elToggle, 'click', function () {
    var open = elBody.hidden;
    elBody.hidden = !open;
    elToggle.setAttribute('aria-expanded', open ? 'true' : 'false');
    if (open) {
      if (!ready && loaded === null) load();
      else if (ready) recompute(); // Resume previews cancelled while the editor was closed.
    } else cancelLoad();
  });

  listen(elReset, 'click', function () {
    if (!loaded) return;
    invalidatePreview();
    inputGeneration++;
    model = JSON.parse(JSON.stringify(loaded));
    familyEdits = {};
    waccMode = 'override'; waccOverride = false; dirty = false;
    buildControls();
    recompute();
  });

  listen(elSave, 'click', function () {
    if (!ready || saving) return;
    if (loadedFamily && (!authorValue.trim() || rationaleValue.trim().length < 20)) {
      setStatus('Author and rationale (min 20 chars) are required to save.', 'warn');
      return;
    }
    invalidatePreview();
    var savedInputGeneration = inputGeneration;
    var loadedGeneration = modelGeneration;
    saving = true;
    CCAction.busy(elSave, 'Saving…');
    setStatus('Saving…');
    fetch(SERVER_URL + '/api/dcf/save', {
      method: 'POST',
      headers: requestHeaders(),
      body: loadedFamily
        ? JSON.stringify({ticker: TICKER, family: loadedFamily, inputs: familyEdits,
          author: authorValue.trim(), rationale: rationaleValue.trim()})
        : JSON.stringify({ticker: TICKER, inputs: model})
    }).then(function (r) {
      return r.json().then(function (j) { return {ok: r.ok, status: r.status, body: j}; });
    }).then(function (res) {
      if (destroyed) return;
      if (!res.ok) {
        saving = false;
        CCAction.release(elSave);
        if (loadedGeneration === modelGeneration && root.isConnected) {
          setStatus('Unsaved edits · ' + ((res.body && res.body.error)
            || ('save failed (' + res.status + ')')), 'bad');
        }
        return;
      }
      // An older save is a durable receipt for its captured inputs only.
      if (loadedGeneration === modelGeneration && root.isConnected) {
        if (loadedFamily && (res.body.status === 'proposal_pending_review' || res.body.saved === false)) {
          dirty = false; familyEdits = {};
          setStatus('Proposal ' + (res.body.proposal_id ? res.body.proposal_id.slice(0, 12) : '')
            + ' recorded; pending review packet before live model promotion.', 'warn');
          CCAction.receipt(elSave, 'Proposal pending review');
          saving = false; later(function () { CCAction.release(elSave); });
          return;
        }
        if (loadedFamily) {
          familyEdits = {}; dirty = false;
          setStatus('Saved to model' + (res.body.model_revision && res.body.model_revision.row_id
            ? ' (run #' + res.body.model_revision.row_id + ').' : '.'), 'ok');
          window.dispatchEvent(new CustomEvent('dcf-model-saved', {detail: {
            ticker: TICKER, model_revision: res.body.model_revision || null
          }}));
          if (syncBar) syncBar.revalidate();
          CCAction.receipt(elSave, '✓ Saved');
          saving = false; later(function () { CCAction.release(elSave); });
          return;
        }
        if (res.body.inputs) loaded = res.body.inputs;
        if (savedInputGeneration === inputGeneration) {
          invalidatePreview(); // A reopened preview cannot replace the persisted receipt.
          if (res.body.inputs) {
            model = JSON.parse(JSON.stringify(loaded));
            waccMode = 'override'; waccOverride = false; dirty = false;
            buildControls();
          }
          if (res.body.sensitivity) { renderScenarios(res.body); renderHeatmap(res.body.sensitivity); }
          setStatus(res.body.recovery_required
            ? 'Saved to model. Backup cleanup failed; further saves require recovery.'
            : 'Saved to model.', res.body.recovery_required ? 'warn' : 'ok');
        } else {
          dirty = true;
          setStatus('Earlier inputs saved. Current edits are unsaved.' + previewNote()
            + (res.body.recovery_required ? ' Backup cleanup failed; further saves require recovery.' : ''), 'warn');
        }
      }
      CCAction.receipt(elSave, '✓ Saved');
      // Saving again after further slider adjustments is the normal flow —
      // unlock once the receipt has registered rather than staying terminal.
      later(function () { saving = false; CCAction.release(elSave); });
    }).catch(function () {
      if (destroyed) return;
      saving = false;
      CCAction.release(elSave);
      if (loadedGeneration === modelGeneration && root.isConnected) {
        setStatus('Save not confirmed. Current edits remain unsaved. Reload the model to check.', 'bad');
      }
    });
  });
  function destroy() {
    if (destroyed) return;
    destroyed = true;
    modelGeneration++;
    cancelLoad();
    listeners.abort();
    if (syncBar) syncBar.destroy();
    presentationTimers.forEach(clearTimeout);
    presentationTimers.clear();
    editors.delete(root);
  }
  var handle = {destroy: destroy, setDriver: setDriver};
  editors.set(root, handle);
  return handle;
  };
  window.initReportSyncBar = function (root, options) {
    if (!root) return null;
    options = options || {};
    var bootEl = document.getElementById('workspace-boot'), boot = {};
    try { boot = bootEl ? JSON.parse(bootEl.textContent) : {}; } catch (e) {}
    var transport = window.uiFetch || fetch;
    var TICKER = options.ticker || root.getAttribute('data-dcf-ticker') || boot.ticker;
    var SERVER_URL = /^https?:$/.test(window.location.protocol) ? window.location.origin
      : (boot.server_url || 'http://localhost:7421');
    var lastEtag = null, candidateArtifactId = options.candidateArtifactId || null;
    var lastStatus = null, busy = false;
    var statusEl = document.createElement('span');
    statusEl.className = 'dcf-report-sync-status'; statusEl.dataset.syncState = 'loading';
    var actions = document.createElement('div'); actions.className = 'dcf-report-sync-actions';
    var btnRefresh = document.createElement('button');
    btnRefresh.type = 'button'; btnRefresh.className = 'k-btn k-btn-quiet k-btn-sm';
    btnRefresh.dataset.syncAction = 'refresh'; btnRefresh.textContent = 'Refresh report';
    var btnCandidate = document.createElement('a');
    btnCandidate.className = 'k-btn k-btn-quiet k-btn-sm';
    btnCandidate.dataset.syncAction = 'open-candidate'; btnCandidate.textContent = 'Review candidate';
    btnCandidate.hidden = true;
    var btnPublish = document.createElement('button');
    btnPublish.type = 'button'; btnPublish.className = 'k-btn k-btn-primary k-btn-sm';
    btnPublish.dataset.syncAction = 'publish'; btnPublish.textContent = 'Approve & publish';
    btnPublish.hidden = true;
    actions.appendChild(btnRefresh); actions.appendChild(btnCandidate); actions.appendChild(btnPublish);
    root.replaceChildren(statusEl, actions);
    function render(state, message) { statusEl.dataset.syncState = state; statusEl.textContent = message; }
    function headers(extra) {
      var h = typeof window.__workspaceMutationHeaders === 'function'
        ? window.__workspaceMutationHeaders() : (window.__workspaceMutationHeaders || {'Content-Type': 'application/json'});
      Object.keys(extra || {}).forEach(function (key) { h[key] = extra[key]; });
      return h;
    }
    function update(status) {
      if (status.model_status !== 'available') {
        render('unavailable', 'Model binding unavailable · Refresh report to bind candidate');
        btnCandidate.hidden = true; btnPublish.hidden = true;
      } else if (status.candidate_review) {
        btnCandidate.hidden = false;
        btnCandidate.href = status.candidate_review.url || ('/reports/' + encodeURIComponent(status.ticker || '') + '?artifact_id=' + encodeURIComponent(status.candidate_artifact_id) + '&standalone=1');
        if (status.candidate_review.ready) {
          render('candidate_ready', 'Candidate ' + status.candidate_artifact_id + ' verified · Ready to approve & publish');
          btnPublish.hidden = false;
        } else {
          render('candidate_blocked', 'Candidate blocked: '
            + ((status.candidate_review.reason_codes || []).join(', ') || 'verification failed'));
          btnPublish.hidden = true;
        }
      } else if (status.report_stale) {
        render('stale', 'Report out of sync with saved model (run #'
          + ((status.model_binding && status.model_binding.row_id) || '?') + ') · Refresh report to prepare candidate');
        btnCandidate.hidden = true; btnPublish.hidden = true;
      } else {
        render('current', 'Report in sync with model (run #'
          + ((status.model_binding && status.model_binding.row_id) || '?') + ')');
        btnCandidate.hidden = true; btnPublish.hidden = true;
      }
    }
    function revalidate() {
      if (!TICKER || busy || disposed) return Promise.resolve();
      var url = SERVER_URL + '/api/report-sync/' + encodeURIComponent(TICKER)
        + (candidateArtifactId ? '?candidate_artifact_id=' + encodeURIComponent(candidateArtifactId) : '');
      return transport(url, {headers: lastEtag ? {'If-None-Match': '"' + lastEtag + '"'} : {}})
        .then(function (r) {
          if (disposed) return;
          if (r.status === 304 && lastStatus) { update(lastStatus); return; }
          return r.json().then(function (status) {
            if (disposed) return;
            lastEtag = status.etag || null; lastStatus = status; update(status);
          });
        }).catch(function () {
          if (disposed) return;
          render('error', 'Report sync status unavailable · Retry refresh');
        });
    }
    function response(r) { return r.json().then(function (body) {
      return {ok: r.ok, status: r.status, body: body};
    }); }
    var disposed = false;
    var pollTimer = null;
    function pollRefresh() {
      if (disposed) return;
      if (pollTimer) clearTimeout(pollTimer);
      pollTimer = setTimeout(function () {
        if (disposed) return;
        var suffix = lastStatus && lastStatus.current_artifact_id
          ? '?expected_current_artifact_id=' + encodeURIComponent(lastStatus.current_artifact_id) : '';
        transport(SERVER_URL + '/api/report-sync/' + encodeURIComponent(TICKER) + '/candidate' + suffix, {
          method: 'POST', headers: headers(lastEtag ? {'If-Match': '"' + lastEtag + '"'} : {})
        }).then(response).then(function (res) {
          if (disposed) return;
          if (res.status === 200 || res.status === 201) {
            candidateArtifactId = res.body.artifact_id;
            CCAction.receipt(btnRefresh, res.body.reused ? '✓ Candidate reused' : '✓ Candidate ready');
            busy = false; CCAction.release(btnRefresh); revalidate();
          } else if (res.status === 202) {
            pollRefresh();
          } else {
            render('error', (res.body && res.body.error) || ('Refresh failed (' + res.status + ')'));
            busy = false; CCAction.release(btnRefresh); revalidate();
          }
        }).catch(function () {
          if (disposed) return;
          render('error', 'Refresh failed · Check the report service');
          busy = false; CCAction.release(btnRefresh); revalidate();
        });
      }, 1000);
    }
    btnRefresh.addEventListener('click', function () {
      if (busy || disposed) return;
      busy = true; CCAction.busy(btnRefresh, 'Refreshing…');
      var suffix = lastStatus && lastStatus.current_artifact_id
        ? '?expected_current_artifact_id=' + encodeURIComponent(lastStatus.current_artifact_id) : '';
      transport(SERVER_URL + '/api/report-sync/' + encodeURIComponent(TICKER) + '/candidate' + suffix, {
        method: 'POST', headers: headers(lastEtag ? {'If-Match': '"' + lastEtag + '"'} : {})
      }).then(response).then(function (res) {
        if (disposed) return;
        if (res.status === 200 || res.status === 201) {
          candidateArtifactId = res.body.artifact_id;
          CCAction.receipt(btnRefresh, res.body.reused ? '✓ Candidate reused' : '✓ Candidate ready');
          busy = false; CCAction.release(btnRefresh); revalidate();
        } else if (res.status === 202) {
          render('running', 'Candidate refresh in progress…');
          btnCandidate.hidden = true; btnPublish.hidden = true;
          pollRefresh();
        } else {
          render('error', (res.body && res.body.error) || ('Refresh failed (' + res.status + ')'));
          busy = false; CCAction.release(btnRefresh); revalidate();
        }
      }).catch(function () {
        if (disposed) return;
        render('error', 'Refresh failed · Check the report service');
        busy = false; CCAction.release(btnRefresh); revalidate();
      });
    });
    btnPublish.addEventListener('click', function () {
      if (busy || disposed) return;
      busy = true; CCAction.busy(btnPublish, 'Publishing…');
      var ownerActionId = 'pub-' + TICKER + '-' + (candidateArtifactId || '') + '-'
        + String((lastStatus && lastStatus.current_artifact_id) || 'none');
      transport(SERVER_URL + '/api/report-sync/' + encodeURIComponent(TICKER) + '/publish', {
        method: 'POST', headers: headers(Object.assign({'Content-Type': 'application/json'},
          lastEtag ? {'If-Match': '"' + lastEtag + '"'} : {})),
        body: JSON.stringify({artifact_id: candidateArtifactId,
          expected_current_artifact_id: lastStatus ? lastStatus.current_artifact_id : null,
          owner_action_id: ownerActionId})
      }).then(response).then(function (res) {
        if (disposed) return;
        if (res.status === 200 || res.status === 201) {
          candidateArtifactId = null; CCAction.receipt(btnPublish, '✓ Published');
          window.dispatchEvent(new CustomEvent('report-candidate-published', {detail: {
            ticker: TICKER, artifact_id: res.body.artifact_id
          }}));
        } else render('error', (res.body && res.body.error) || 'publish conflict');
      }).catch(function () {
        if (disposed) return;
        render('error', 'Publish failed · Check the report service');
      })
        .finally(function () {
          busy = false; CCAction.release(btnPublish);
          if (!disposed) revalidate();
        });
    });
    function onModelSaved(event) {
      if (!event.detail || event.detail.ticker === TICKER) revalidate();
    }
    window.addEventListener('focus', revalidate);
    window.addEventListener('dcf-model-saved', onModelSaved);
    function destroy() {
      disposed = true;
      if (pollTimer) {
        clearTimeout(pollTimer);
        pollTimer = null;
      }
      window.removeEventListener('focus', revalidate);
      window.removeEventListener('dcf-model-saved', onModelSaved);
    }
    if (/^https?:$/.test(window.location.protocol) || boot.server_url) revalidate();
    return {revalidate: revalidate, destroy: destroy};
  };
  var standalone = window.initDcfEditor(document.getElementById('dcf-edit'));
  if (standalone) window.dcfSetDriver = standalone.setDriver;
})();
"""
