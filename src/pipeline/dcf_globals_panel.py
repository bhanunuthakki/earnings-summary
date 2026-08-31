"""Global DCF assumptions fragment for the settings drawer.

The ``global_dcf_assumptions`` table (migration 0112) holds the single editable
default for the macro inputs that should be the same across every DCF model —
risk-free rate, equity/market risk premium, and a default tax rate. This drawer
section edits them in one place (wired to ``POST /api/dcf-globals``) and, for
each field, lists which tickers currently pin their own value, so it's clear
what a global change will and won't move.

Precedence is **per-ticker override > this global default > model fallback**;
the override scan reads the same per-ticker assumption JSONs the builders do.
"""

from __future__ import annotations

import json
from html import escape
from pathlib import Path
from typing import cast

from dcf import global_assumptions as ga
from pipeline.operations_styles import DCF_GLOBALS_STYLE

# Field → (human label, "= X%" example). Order is the render order.
_FIELDS: tuple[tuple[str, str], ...] = (
    ("risk_free_rate", "Risk-free rate"),
    ("equity_risk_premium", "Equity / market risk premium"),
    ("tax_rate", "Default tax rate"),
)

# Per global field, the assumption-JSON keys that count as a per-ticker override,
# keyed by model. FCFF keys live under the nested "redesign" block; bank /
# platform keys are top-level in their own files.
_FCFF_KEY = {
    "risk_free_rate": "risk_free_rate",
    "equity_risk_premium": "equity_risk_premium",
    "tax_rate": "tax_rate",
}
_BANK_KEY = {"risk_free_rate": "rf", "equity_risk_premium": "erp", "tax_rate": "tax"}


def _load_json(path: Path) -> dict[str, object]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return cast("dict[str, object]", data) if isinstance(data, dict) else {}


def _scan_overrides(data_dir: Path) -> dict[str, list[str]]:
    """{global_field: ["TICKER (model)", ...]} — which names pin their own value
    for each field, so the panel can show what the global won't move."""
    out: dict[str, list[str]] = {f: [] for f, _ in _FIELDS}

    # FCFF: data/dcf_assumptions/<T>.json -> redesign.<key>
    for p in sorted((data_dir / "dcf_assumptions").glob("*.json")):
        redesign = _load_json(p).get("redesign")
        if not isinstance(redesign, dict):
            continue
        for field, key in _FCFF_KEY.items():
            if redesign.get(key) is not None:
                out[field].append(f"{p.stem} (FCFF)")

    # Bank + platform: data/bank_assumptions/<T>.json (+ <T>_platform.json -> tax)
    for p in sorted((data_dir / "bank_assumptions").glob("*.json")):
        body = _load_json(p)
        if p.stem.endswith("_platform"):
            if body.get("tax") is not None:
                out["tax_rate"].append(f"{p.stem.removesuffix('_platform')} (platform)")
            continue
        if p.stem.endswith("_sotp"):
            continue  # fintech SOTP has no rf/erp/tax inputs
        for field, key in _BANK_KEY.items():
            if body.get(key) is not None:
                out[field].append(f"{p.stem} (bank)")
    return out


_SCRIPT = """<script>
(function () {
  document.querySelectorAll('.dcfg-row[data-field]').forEach(function (row) {
    var btn = row.querySelector('.dcfg-save');
    if (!btn) return;
    btn.addEventListener('click', function () {
      var field = row.getAttribute('data-field');
      var msg = row.querySelector('.dcfg-msg');
      var val = parseFloat(row.querySelector('.dcfg-input').value);
      msg.textContent = 'saving\\u2026';
      fetch('/api/dcf-globals', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({field: field, value: val})
      }).then(function (res) {
        return res.json().catch(function () { return {}; }).then(function (b) {
          msg.textContent = res.ok ? 'saved \\u2713' : ('error: ' + (b.error || res.status));
        });
      }).catch(function () { msg.textContent = 'network error'; });
    });
  });

  var rb = document.getElementById('dcfg-rebuild');
  if (rb) rb.addEventListener('click', function () {
    var msg = document.getElementById('dcfg-rebuild-msg');
    var log = document.getElementById('dcfg-rebuild-log');
    if (!window.confirm('Rebuild every DCF-maintained name? This re-runs ~all DCF '
        + 'models and can take several minutes.')) return;
    CCAction.busy(rb, 'Starting\\u2026');
    msg.textContent = '';
    log.hidden = false; log.textContent = '';
    fetch('/actions/rebuild-dcfs', {
      method: 'POST', headers: {'Content-Type': 'application/json'}, body: '{}'
    }).then(function (r) {
      return r.json().then(function (j) { return {ok: r.ok, j: j}; });
    }).then(function (res) {
      if (!res.ok) {
        msg.textContent = 'error: ' + (res.j.error || 'failed to start rebuild');
        CCAction.release(rb);
        return;
      }
      msg.textContent = 'running (' + res.j.job_id + ')\\u2026';
      var es = new EventSource(res.j.stream_url);
      es.onmessage = function (ev) {
        var f = {}; try { f = JSON.parse(ev.data); } catch (e) { return; }
        if (f.event === 'log') { log.textContent += f.line + '\\n'; log.scrollTop = log.scrollHeight; }
        if (f.event === 'done') {
          log.textContent += '=== done (exit ' + f.exit_code + ') ===\\n';
          if (f.exit_code === 0) {
            CCAction.receipt(rb, '\\u2713 Rebuild complete — DCF models refreshed');
            window.dispatchEvent(new CustomEvent('work-os:investment-evidence-updated', {
              detail: {kind: 'dcf', scope: 'all'}
            }));
          } else {
            msg.textContent = 'rebuild finished with errors (exit ' + f.exit_code + ') — see log above';
            CCAction.release(rb);
          }
          es.close();
        }
      };
      es.onerror = function () {
        es.close();
        msg.textContent = 'stream ended unexpectedly — rebuild may still be running server-side';
        CCAction.release(rb);
      };
    }).catch(function () { msg.textContent = 'network error'; CCAction.release(rb); });
  });
})();
</script>"""


def _row_html(field: str, label: str, value: float, overrides: list[str]) -> str:
    pct = f"{value * 100:.3f}".rstrip("0").rstrip(".")
    if overrides:
        n = len(overrides)
        shown = overrides[:12]
        more = f" … +{n - len(shown)} more" if n > len(shown) else ""
        ov = (
            f'<div class="dcfg-overrides muted">Pinned by {n} name{"" if n == 1 else "s"} '
            "(the global will not move these): " + escape(", ".join(shown)) + more + "</div>"
        )
    else:
        ov = '<div class="dcfg-overrides muted">No per-ticker overrides — all names track this global.</div>'
    return (
        f'<div class="dcfg-row" data-field="{escape(field)}">'
        '<div class="dcfg-controls">'
        f'<label class="k-label dcfg-label" for="dcfg-{escape(field)}">{escape(label)}</label>'
        f'<input id="dcfg-{escape(field)}" class="dcfg-input" type="number" min="0" max="1" '
        f'step="0.001" value="{value:.4f}" '
        f'aria-label="{escape(label)} (decimal ratio)" '
        f'title="Decimal ratio, e.g. 0.043 = 4.3%">'
        f'<span class="muted dcfg-pct">= {escape(pct)}%</span>'
        '<button type="button" class="dcfg-save k-btn k-btn-quiet k-btn-sm" '
        f'title="Apply this global {escape(label)} to every DCF model">Save</button>'
        '<span class="dcfg-msg muted"></span>'
        "</div>"
        f"{ov}"
        "</div>"
    )


def render_dcf_globals_panel(db_path: Path) -> str:
    """Drawer fragment: the three editable global macro inputs + their
    per-ticker override lists. Reads the effective values (seed defaults
    overlaid with stored) so it renders even before the seed migration runs."""
    data_dir = db_path.parent
    values = ga.get_all(db_path=db_path)
    overrides = _scan_overrides(data_dir)
    rows = "".join(
        _row_html(field, label, float(values.get(field, 0.0)), overrides.get(field, []))
        for field, label in _FIELDS
    )
    return (
        DCF_GLOBALS_STYLE + '<section class="panel"><h2>Global DCF assumptions</h2>'
        '<p class="sub">One editable default for the macro inputs shared across every DCF model. '
        "Saving updates the default used by the next build; a per-ticker value still wins where set "
        "(listed under each field). Values are decimal ratios "
        "(<code>0.043</code> = 4.3%).</p>"
        f"{rows}"
        '<p class="muted dcfg-reach">'
        "Reach: risk-free &amp; ERP drive the discount rate for the FCFF and bank (CAPM) models, "
        "and tax drives NOPAT there and in the NU platform model. The fintech SOTP and NU platform "
        "models use an explicit cost of equity, so risk-free/ERP reach them only via their opt-in "
        "CAPM setting; the holdco NAV model is multiples-based, so these are informational for it.</p>"
        '<div class="dcfg-controls dcfg-rebuild-controls">'
        '<button type="button" id="dcfg-rebuild" class="k-btn k-btn-primary" '
        'title="Re-run every DCF model so a global change propagates into the workbooks and dcf_runs">'
        "Rebuild affected models</button>"
        '<span id="dcfg-rebuild-msg" class="muted dcfg-rebuild-msg"></span>'
        "</div>"
        '<pre id="dcfg-rebuild-log" class="dcfg-rebuild-log" hidden></pre>'
        "</section>" + _SCRIPT
    )
