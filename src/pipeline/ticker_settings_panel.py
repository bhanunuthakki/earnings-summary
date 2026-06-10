"""Ticker-settings fragment for the settings drawer (P3.4).

The ``ticker_settings`` table (0067) holds per-ticker dashboard-owned knobs —
today just ``bypass_budget``, the persistent "always ignore LLM budget caps
for this ticker" flag. The flag was previously settable only from the
per-ticker Refresh panel ("always ignore" checkbox) or raw API; this drawer
section lists every ticker that has a row and offers a small set-form for
any other symbol, both wired to the existing
``POST /api/ticker-settings/<T>`` endpoint.
"""

from __future__ import annotations

import sqlite3
from html import escape
from pathlib import Path

_SCRIPT = """<script>
(function () {
  function post(t, value, cell) {
    fetch('/api/ticker-settings/' + encodeURIComponent(t), {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({bypass_budget: value})
    }).then(function (r) { return r.json(); }).then(function (j) {
      if (cell) cell.textContent = j.bypass_budget ? 'on' : 'off';
    }).catch(function () { if (cell) cell.textContent = 'error'; });
  }
  document.querySelectorAll('.ts-toggle').forEach(function (cb) {
    cb.addEventListener('change', function () {
      var t = cb.getAttribute('data-ticker');
      post(t, cb.checked, cb.closest('tr') ? cb.closest('tr').querySelector('.ts-state') : null);
    });
  });
  var addBtn = document.getElementById('ts-add-btn');
  if (addBtn) addBtn.addEventListener('click', function () {
    var inp = document.getElementById('ts-add-ticker');
    var chk = document.getElementById('ts-add-bypass');
    var t = (inp.value || '').trim().toUpperCase();
    if (!t) return;
    post(t, chk.checked, null);
    var note = document.getElementById('ts-add-note');
    if (note) note.textContent = 'saved ' + t + ' — reopen the drawer to refresh the list';
  });
})();
</script>"""


def render_ticker_settings_panel(db_path: Path) -> str:
    """Drawer fragment: existing per-ticker settings rows + a set-form."""
    rows = _load_rows(db_path)
    if rows is None:
        return (
            '<section class="panel"><h2>Ticker settings</h2>'
            '<p class="muted">No <code>ticker_settings</code> table — '
            "run <code>alembic upgrade head</code>.</p></section>"
        )
    body = "".join(
        "<tr>"
        f"<td>{escape(t)}</td>"
        f'<td class="ts-state">{"on" if bypass else "off"}</td>'
        f'<td><input type="checkbox" class="ts-toggle" data-ticker="{escape(t)}"'
        f'{" checked" if bypass else ""} aria-label="Bypass budget for {escape(t)}"></td>'
        f'<td class="num">{escape(updated[:10])}</td>'
        "</tr>"
        for t, bypass, updated in rows
    )
    table = (
        "<table><thead><tr><th>Ticker</th><th>Bypass budget</th><th>Toggle</th>"
        '<th class="num">Updated</th></tr></thead>'
        f"<tbody>{body}</tbody></table>"
        if rows
        else '<p class="muted">No per-ticker overrides set.</p>'
    )
    return (
        '<section class="panel"><h2>Ticker settings</h2>'
        '<p class="sub">Persistent per-ticker overrides. <strong>Bypass budget</strong> '
        "makes every LLM build for the ticker ignore the per-purpose budget caps "
        "(the one-shot bypass lives on the per-ticker Refresh panel).</p>"
        f"{table}"
        '<div style="margin-top:12px; display:flex; gap:8px; align-items:center;">'
        '<input id="ts-add-ticker" placeholder="TICKER" style="width:110px; padding:6px 8px;" '
        'aria-label="Ticker symbol">'
        '<label style="font-size:12.5px;"><input type="checkbox" id="ts-add-bypass"> '
        "bypass budget</label>"
        '<button id="ts-add-btn" type="button" class="tcc-refresh">Save</button>'
        '<span id="ts-add-note" class="muted" style="font-size:12px;"></span>'
        "</div>"
        "</section>" + _SCRIPT
    )


def _load_rows(db_path: Path) -> list[tuple[str, bool, str]] | None:
    """(ticker, bypass_budget, updated_at) rows, or None when table/DB absent."""
    if not db_path.exists():
        return None
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.Error:
        return None
    try:
        rows = conn.execute(
            "SELECT ticker, bypass_budget, COALESCE(updated_at, '') AS updated_at "
            "FROM ticker_settings ORDER BY ticker"
        ).fetchall()
        return [(str(r[0]), bool(r[1]), str(r[2])) for r in rows]
    except sqlite3.Error:
        return None
    finally:
        conn.close()
