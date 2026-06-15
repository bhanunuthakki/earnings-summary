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
        f'<div class="dcfg-row" data-field="{escape(field)}" style="margin-bottom:16px;">'
        '<div style="display:flex; gap:8px; align-items:center; flex-wrap:wrap;">'
        f'<label style="min-width:220px; font-weight:600;" for="dcfg-{escape(field)}">{escape(label)}</label>'
        f'<input id="dcfg-{escape(field)}" class="dcfg-input" type="number" min="0" max="1" '
        f'step="0.001" value="{value:.4f}" style="width:90px; padding:6px 8px;" '
        f'aria-label="{escape(label)} (decimal ratio)" '
        f'title="Decimal ratio, e.g. 0.043 = 4.3%">'
        f'<span class="muted" style="min-width:64px;">= {escape(pct)}%</span>'
        '<button type="button" class="dcfg-save tcc-refresh" '
        f'title="Apply this global {escape(label)} to every DCF model">Save</button>'
        '<span class="dcfg-msg muted" style="font-size:var(--fs-caption);"></span>'
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
        '<section class="panel"><h2>Global DCF assumptions</h2>'
        '<p class="sub">One editable default for the macro inputs shared across every DCF model. '
        "Saving updates the default used by the next build; a per-ticker value still wins where set "
        "(listed under each field). Values are decimal ratios "
        "(<code>0.043</code> = 4.3%).</p>"
        f"{rows}"
        '<p class="muted" style="font-size:var(--fs-caption); margin-top:10px;">'
        "Reach: risk-free &amp; ERP drive the discount rate for the FCFF and bank (CAPM) models, "
        "and tax drives NOPAT there and in the NU platform model. The fintech SOTP and NU platform "
        "models use an explicit cost of equity, so risk-free/ERP reach them only via their opt-in "
        "CAPM setting; the holdco NAV model is multiples-based, so these are informational for it. "
        "Use 'Rebuild affected models' (Maintenance) to re-run the DCFs after a change.</p>"
        "</section>" + _SCRIPT
    )
