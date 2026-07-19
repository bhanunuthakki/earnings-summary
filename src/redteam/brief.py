"""Render the monthly Red Team Brief — one dense page of items, kit-composed
(monthly_red_team.md Phase 2: "one dense Red Team Brief (ui-kit composed):
each item = falsifiable attack + one question + a proposed rule/scenario
change").

Pure render function: no DB access here (``pipeline.red_team_panel`` owns the
DB read + composes this). PR5 shipped items read-only (status chip only). PR6
(this file) adds the forced-response actions: an open/deferred item gets
REFUTE / ACCEPT / DEFER buttons that POST
``/api/red_team/<id>/respond`` (``execution/comments_server.py``) and a
month-status header pill (OPEN n / CLOSED, from ``redteam.gate.month_status``).
Both routes through the ONE state machine (``redteam.response.respond``) — no
render-side business logic here, just kit-composed HTML + the click-to-fetch
wiring (the triage-panel idiom: a self-registering ``<script>`` tag that
refetches this same fragment on success).
"""

from __future__ import annotations

from html import escape

from redteam.gate import MonthStatus
from redteam.models import RedTeamItemRow
from ui.controls import panel_toolbar, ticker_label
from ui.prose import render_prose

# ---------------------------------------------------------------------------
# Self-registered style module (design_language / tests/test_ui_controls.py):
# layout + severity/status tone only — every color/size/radius rides a token,
# never a raw hex or off-scale value. Registered as "redteam/brief.py" in
# tests/test_ui_controls.py's REGISTERED set.
# ---------------------------------------------------------------------------
_BRIEF_CSS = """<style>
.rt-brief { display: flex; flex-direction: column; gap: var(--sp-3); }
.rt-empty { color: var(--muted); font-size: var(--fs-body); padding: var(--sp-4) 0; }
.rt-group-title { font-size: var(--fs-caption); font-weight: 600; color: var(--muted);
  text-transform: uppercase; letter-spacing: 0.06em; margin: var(--sp-2) 0 0; }
.rt-item { display: flex; flex-direction: column; gap: var(--sp-2); }
.rt-item-head { display: flex; align-items: center; gap: var(--sp-2); flex-wrap: wrap; }
.rt-item-cross { color: var(--fg-soft); font-size: var(--fs-body); font-weight: 600; }
.rt-item .prose { margin-top: var(--sp-1); }
.rt-item .prose p { margin: 0 0 var(--sp-2); }
.rt-actions { display: flex; gap: var(--sp-2); margin-top: var(--sp-1); flex-wrap: wrap; }
/* In-card Refute editor (replaces window.prompt — the ledger's PR9 idiom:
   ledger_panel.beginRewrite / journal_panel.beginEdit). Appended into the
   same .rt-actions holder the buttons live in, not a swap, so REFUTE stays
   visible for the double-open guard's benefit. */
.rt-refute-box { flex-basis: 100%; margin-top: var(--sp-1); }
.rt-refute-ta { width: 100%; box-sizing: border-box; min-height: 56px; resize: vertical;
  font-family: var(--sans); font-size: var(--fs-body); }
.rt-refute-row { display: flex; gap: var(--sp-2); margin-top: var(--sp-2); }
</style>"""

_SEVERITY_TONE: dict[str, str] = {"high": "bad", "med": "warn", "low": "ok"}
_STATUS_TONE: dict[str, str] = {
    "open": "accent",
    "refuted": "ok",
    "accepted": "ok",
    "deferred": "warn",
    "closed": "",
}
LENS_LABEL: dict[str, str] = {
    "shared_factor": "Shared Factor",
    "fx_translation": "FX Translation",
    "competitive_encroachment": "Competitive Encroachment",
    "model_vs_market": "Model vs Market",
    "behavioral_consistency": "Behavioral Consistency",
    "missed_upside": "Missed Upside",
    "profile_drift": "Profile Drift",
    "factor_block": "Factor Block",
    "style_drift": "Style Drift",
    "human_capital": "Human Capital",
}

# Items in these statuses are still answerable (PR6 state machine,
# redteam.response.respond) — everything else already has a terminal
# response and renders its status chip only, no action buttons.
_ANSWERABLE_STATUSES: frozenset[str] = frozenset({"open", "deferred"})


def _lens_chip(lens: str) -> str:
    label = escape(LENS_LABEL.get(lens, lens))
    return f'<span class="k-chip k-chip-mono">{label}</span>'


def _severity_pill(severity: str) -> str:
    tone = _SEVERITY_TONE.get(severity, "")
    cls = f"k-pill k-pill-{tone}".strip() if tone else "k-pill"
    return f'<span class="{cls}">{escape(severity.upper())}</span>'


def _status_chip(status: str) -> str:
    tone = _STATUS_TONE.get(status, "")
    cls = f"k-chip k-chip-{tone}".strip() if tone else "k-chip"
    return f'<span class="{cls}">{escape(status.upper())}</span>'


def _month_status_pill(month_status: MonthStatus | None) -> str:
    """The panel's month-status header pill: OPEN n / CLOSED. Empty string
    when there's no run to report on yet (matches the empty-state)."""
    if month_status is None or month_status.run_key is None:
        return ""
    if month_status.is_closed:
        return '<span class="k-pill k-pill-ok">CLOSED</span>'
    return f'<span class="k-pill k-pill-warn">OPEN {month_status.unresolved_count}</span>'


def _response_actions(item: RedTeamItemRow) -> str:
    """REFUTE / ACCEPT / DEFER buttons for an answerable item — empty string
    once the item has a terminal response (its status chip already says so).
    Kit button classes with the JS-hook class preserved alongside, per the
    design-language contract."""
    if item.status not in _ANSWERABLE_STATUSES:
        return ""
    return (
        f'<div class="rt-actions" data-item-id="{item.id}">'
        '<button type="button" class="ix-act k-btn k-btn-quiet k-btn-sm" '
        'data-rt-act="refute">Refute</button>'
        '<button type="button" class="ix-act k-btn k-btn-quiet k-btn-sm" '
        'data-rt-act="accept">Accept</button>'
        '<button type="button" class="ix-act k-btn k-btn-quiet k-btn-sm" '
        'data-rt-act="defer">Defer</button>'
        "</div>"
    )


def _render_item(item: RedTeamItemRow) -> str:
    tone = _SEVERITY_TONE.get(item.severity, "")
    well_cls = f"k-well k-well-{tone} rt-item".strip() if tone else "k-well rt-item"
    subject = (
        ticker_label(item.ticker)
        if item.ticker
        else '<span class="rt-item-cross">Cross-book</span>'
    )
    head = (
        f'<div class="rt-item-head">{_lens_chip(item.lens)}{_severity_pill(item.severity)}'
        f"{subject}{_status_chip(item.status)}</div>"
    )
    body_md = (
        f"**Attack:** {item.attack_md}\n\n"
        f"**Question:** {item.question_md}\n\n"
        f"**Proposed change:** {item.proposed_change_md}"
    )
    body = f'<div class="prose">{render_prose(body_md)}</div>'
    return f'<div class="{well_cls}">{head}{body}{_response_actions(item)}</div>'


def render_red_team_brief(
    items: list[RedTeamItemRow],
    *,
    run_key: str | None = None,
    month_status: MonthStatus | None = None,
) -> str:
    """One dense Red Team Brief fragment. Empty state when ``items`` is
    empty (no run yet, or the latest run produced nothing). ``month_status``
    (``redteam.gate.month_status``) drives the toolbar's OPEN/CLOSED pill;
    omitted it just doesn't render one (backward-compatible with PR5
    callers)."""
    title = f"Red Team — {run_key}" if run_key else "Red Team"
    toolbar = panel_toolbar(title, actions=_month_status_pill(month_status))
    if not items:
        empty = (
            '<p class="rt-empty">No red-team items yet. The First-Saturday run '
            "generates one adversarial attack per held name plus the three "
            "cross-book passes — see <code>execution/run_red_team.py</code>.</p>"
        )
        return f"{_BRIEF_CSS}{toolbar}{empty}"

    per_name = [i for i in items if i.kind == "per_name"]
    cross = [i for i in items if i.kind == "cross_book"]

    parts: list[str] = [
        _BRIEF_CSS,
        toolbar,
        '<div class="rt-brief">',
    ]
    if per_name:
        parts.append('<h3 class="rt-group-title">Per-name</h3>')
        parts.extend(_render_item(i) for i in per_name)
    if cross:
        parts.append('<h3 class="rt-group-title">Cross-book</h3>')
        parts.extend(_render_item(i) for i in cross)
    parts.append("</div>")
    parts.append(_ACTIONS_SCRIPT)
    return "".join(parts)


# ---------------------------------------------------------------------------
# Response-action wiring (PR6). Wires against the shell's own panel container
# (``[data-panel="red_team"]`` — command_center_shell.py mounts every panel
# fragment there) rather than an id of our own, so a click listener survives
# our own refresh(): only the container's innerHTML changes, the container
# node itself never does, and a dataset flag on it (not on this script's own
# <script> tag, which the shell's injectHtml-style re-creation would re-run)
# guards against double-wiring. On refresh, re-creating the freshly returned
# <script> tag mirrors the shell's own injectHtml() — innerHTML assignment
# does not execute embedded <script> tags, so they're rebuilt by hand; the
# dataset flag then makes that re-run a no-op, matching the shell's own
# fresh-tab-load behavior where the flag is naturally unset again.
# A 409 (second-defer-rejected / already-responded) surfaces the server's
# message and still refreshes, so the item's escalated/terminal state
# reflects immediately.
# ---------------------------------------------------------------------------
_ACTIONS_SCRIPT = """<script>
(function () {
  var container = document.querySelector('[data-panel="red_team"]');
  if (!container || container.dataset.rtWired) return;
  container.dataset.rtWired = '1';
  function refresh() {
    fetch('/api/panel/red_team').then(function (r) { return r.text(); })
      .then(function (html) {
        container.innerHTML = html;
        var scripts = container.querySelectorAll('script');
        for (var i = 0; i < scripts.length; i++) {
          var old = scripts[i];
          var s = document.createElement('script');
          if (old.src) { s.src = old.src; } else { s.textContent = old.textContent; }
          old.parentNode.replaceChild(s, old);
        }
      });
  }
  function post(id, payload) {
    fetch('/api/red_team/' + id + '/respond', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(payload)
    }).then(function (r) {
      if (r.ok) { refresh(); return; }
      r.json().catch(function () { return {}; }).then(function (body) {
        window.alert((body && body.error) || 'Could not record that response.');
        refresh();
      });
    });
  }
  // In-card Refute reasoning editor (PR-next, replaces window.prompt): appends
  // a textarea + kit Save/Cancel into the same .rt-actions holder the REFUTE
  // button lives in, so the item being refuted stays visible by construction
  // (the ledger's beginRewrite / journal's beginEdit idiom).
  function beginRefute(holder, id){
    if (holder.getAttribute('data-editing') === '1') return;
    holder.setAttribute('data-editing', '1');
    var ed = document.createElement('div');
    ed.className = 'rt-refute-box';
    var ta = document.createElement('textarea');
    ta.className = 'rt-refute-ta'; ta.rows = 3;
    ta.placeholder = 'Your reasoning (required to refute)';
    var row = document.createElement('div');
    row.className = 'rt-refute-row';
    var save = document.createElement('button');
    save.type = 'button'; save.className = 'k-btn k-btn-primary k-btn-sm';
    save.setAttribute('data-refute-save', '');
    save.textContent = 'Save';
    var cancel = document.createElement('button');
    cancel.type = 'button'; cancel.className = 'k-btn k-btn-quiet k-btn-sm';
    cancel.setAttribute('data-refute-cancel', '');
    cancel.textContent = 'Cancel';
    row.appendChild(save); row.appendChild(cancel);
    ed.appendChild(ta); ed.appendChild(row);
    holder.appendChild(ed);
    ta.focus();
    cancel.addEventListener('click', function () {
      ed.remove();
      holder.removeAttribute('data-editing');
    });
    save.addEventListener('click', function () {
      var txt = ta.value.trim();
      if (!txt) { ta.focus(); return; }
      save.disabled = true;
      post(id, {action: 'refute', response_md: txt});
    });
  }
  container.addEventListener('click', function (ev) {
    var btn = ev.target.closest('[data-rt-act]');
    if (!btn) return;
    var holder = btn.closest('[data-item-id]');
    if (!holder) return;
    var id = holder.getAttribute('data-item-id');
    var act = btn.getAttribute('data-rt-act');
    if (act === 'refute') { beginRefute(holder, id); return; }
    var payload = {action: act};
    if (act === 'defer') {
      if (!window.confirm(
        'Defer this item? A second defer on the same item is rejected — respond next time.'
      )) return;
    }
    post(id, payload);
  });
})();
</script>"""
