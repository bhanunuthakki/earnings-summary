"""Review → Triage panel — the dedicated home for parked comments (S11).

The comment classifier is closed under no-fit (Instrument Paradigm §1): a
directive it can't map to an actionable router lands at the ``needs_triage``
terminal instead of being force-bucketed into ``ask_question``. S5 shipped that
terminal but routed it to the append-only ``directives/data_fixes.md`` backlog;
this panel is the surface that follow-on always implied — a queryable list of
the open parked items where the owner disposes of each one.

It is a LENS, not a new record type (Instrument Paradigm corollary "one item
model, many lenses"): it reads the same ``analyst_notes`` spine the Journal
reads, filtered to the triage discriminator (``source='comment'`` notes whose
``context_json['intent'] == 'needs_triage'``; see ``user_state.notes``). Three
dispositions, all on the ordinary note lifecycle:

  * **Route** — pick the real intent the classifier missed; the note re-types
    and leaves the queue (the comment store stays system-of-record — the route
    handler best-effort updates the comment too).
  * **Resolve** — handled out-of-band; close it (optional resolution note).
  * **Dismiss** — not actionable; archive it.

Rendered through the S1 control kit (``panel_toolbar`` / ``.p-table`` /
``.k-chip`` / ``.k-pill`` / ``.k-btn`` — no raw hex, guard-clean) with the
anchor + verbatim text inline and the full context (report date, tab, selected
text) behind an S4 ``CCOverlay`` drill-in. Served by ``/api/panel/triage``;
``?fragment=list`` returns just the table the panel JS refreshes after an action.
"""

from __future__ import annotations

import sqlite3
from html import escape
from pathlib import Path

from identity import DEFAULT_USER_ID
from ui import living_grid as lg
from ui.controls import panel_toolbar, ticker_label
from user_state.notes import ROUTABLE_INTENTS, AnalystNoteRow, list_triage_notes

# Routable-intent → human label for the route picker (keys are the
# user_state.notes.ROUTABLE_INTENTS vocabulary; an unknown intent falls back to
# its raw token so the picker never silently drops an option).
_INTENT_LABELS: dict[str, str] = {
    "ask_question": "Question",
    "edit_thesis": "Thesis edit",
    "edit_structured": "Structured edit",
    "drop_kpi": "Drop KPI",
    "extract_kpi": "Extract KPI",
    "curate_peers": "Curate peers",
    "fix_data": "Data fix",
    "rewrite_section": "Rewrite section",
}

# Token-only scoped styles (guard-clean: color via palette vars / color-mix,
# radius via --radius, type via the --fs-* scale; width/inset px are layout, not
# guard-scoped). The drawer is an S4 .k-overlay positioned as a right rail.
_PANEL_STYLE = """<style>
.tri-sub { color: var(--muted); font-size: var(--fs-caption); margin: 0 0 var(--sp-3);
  line-height: 1.55; max-width: 70ch; }
.tri-anchor { font-family: var(--mono); font-size: var(--fs-caption); color: var(--muted);
  white-space: nowrap; }
.tri-when { color: var(--muted); font-variant-numeric: tabular-nums; white-space: nowrap; }
.tri-text { background: none; border: none; padding: 0; text-align: left; cursor: pointer;
  font: inherit; color: var(--fg); }
.tri-text:hover { color: var(--accent); text-decoration: underline; }
.tri-acts { display: flex; gap: var(--sp-2); align-items: center; flex-wrap: wrap; }
/* Route picker: the kit's <select> baseline owns border/radius/chevron/focus —
   this only adds dense-row layout (caption type), never re-skins the control. */
.tri-route { font-size: var(--fs-caption); padding-top: 3px; padding-bottom: 3px; }
.tri-empty { color: var(--muted); padding: var(--sp-4) 0; }
/* Embedded (composite-console) section heading — the console band owns the
   tab title, so the panel_toolbar collapses to a section-level h3. */
.tri-h { font-size: var(--fs-title); font-weight: 600; color: var(--fg);
  margin: var(--sp-4) 0 var(--sp-1); display: flex; align-items: baseline; gap: var(--sp-2); }
/* In-row Resolve editor (replaces window.prompt — the blocking OS modal the
   ledger banned in PR9 survived here). Same sizing family as the ledger's
   in-card rewrite textarea. */
.tri-resolve-ta { width: 100%; min-height: 48px; resize: vertical;
  font-family: var(--sans); font-size: var(--fs-body); margin-bottom: var(--sp-2); }
/* S4 drill-in: a right-side .k-overlay (look + open motion from the kit). Sized
   to its CONTENT — a parked comment is a short disposition form, not a report —
   with a viewport cap that only turns on scroll when a comment runs long. The
   old rule pinned BOTH top+bottom insets, forcing a full-height rail: a ~120px
   card stretched to ~800px left ~85% dead space under every short comment (the
   "too much space" §3.1 never caught because drawer height is per-surface
   layout, not guard-scoped).

   The `display:flex` MUST be scoped to :not([hidden]): an ID selector (1,0,0)
   outranks the kit's `.k-overlay[hidden]{display:none}` (0,2,0), so a bare
   `#triage-drawer{display:flex}` re-shows the drawer regardless of its `hidden`
   attribute — it rode open+empty on every load and CCOverlay's close (which
   toggles `hidden`) could never dismiss it ("Parked comment cannot be
   minimized", owner 2026-07-17). Gating on :not([hidden]) keeps the kit's
   hide-when-hidden contract intact while still content-sizing when shown. */
#triage-drawer { top: var(--sp-3); right: var(--sp-3);
  width: min(420px, 92vw);
  max-height: calc(100vh - var(--sp-3) * 2);
  max-height: calc(100dvh - var(--sp-3) * 2);
  padding: var(--sp-4); overflow: auto; gap: var(--sp-3); }
#triage-drawer:not([hidden]) { display: flex; flex-direction: column; }
.tri-d-bar { display: flex; align-items: baseline; gap: var(--sp-2); }
.tri-d-bar h3 { font-size: var(--fs-title); font-weight: 600; margin: 0; margin-right: auto; }
/* Drawer close glyph — the §3 convention: top type step, muted → fg, no border
   (CCOverlay owns the dismissal wiring). */
.tri-d-close { background: none; border: none; cursor: pointer; color: var(--muted);
  font-size: var(--fs-display); line-height: 1; padding: 0 var(--sp-1); }
.tri-d-close:hover { color: var(--fg); }
.tri-d-meta { color: var(--muted); font-size: var(--fs-caption); font-family: var(--mono);
  line-height: 1.6; }
.tri-d-sel { color: var(--fg-soft); border-left: 2px solid var(--border);
  padding-left: var(--sp-3); }
.tri-d-body { font-size: var(--fs-body); line-height: 1.6; color: var(--fg); white-space: pre-wrap; }
/* The drawer's disposition strip is a footer: a hairline lifts it off the
   comment body so Route/Resolve/Dismiss don't crowd the text. */
#tri-d-acts { border-top: 1px solid var(--hairline); padding-top: var(--sp-3); }
</style>"""


def _truncate(text: str, limit: int = 160) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _route_select() -> str:
    opts = '<option value="">Route to&hellip;</option>' + "".join(
        f'<option value="{escape(i)}">{escape(_INTENT_LABELS.get(i, i))}</option>'
        for i in ROUTABLE_INTENTS
    )
    return (
        f'<select class="tri-route" data-act="route" title="Route to a real intent">{opts}</select>'
    )


def _anchor_text(n: AnalystNoteRow) -> str:
    if not n.anchor_type:
        return "—"
    key = f" · {n.anchor_key}" if n.anchor_key else ""
    return f"{n.anchor_type}{key}"


def _row(n: AnalystNoteRow) -> str:
    ctx = n.context or {}
    ticker_cell = ticker_label(n.ticker) if n.ticker else '<span class="k-chip">PORTFOLIO</span>'
    body = n.body or ""
    # Detail fields ride as data-* so the drill-in reads them client-side
    # (textContent, never innerHTML) without a second fetch.
    data = (
        f'data-note-id="{n.id}" '
        f'data-ticker="{escape(n.ticker or "", quote=True)}" '
        f'data-anchor="{escape(_anchor_text(n), quote=True)}" '
        f'data-report-date="{escape(str(ctx.get("report_date") or ""), quote=True)}" '
        f'data-tab="{escape(str(ctx.get("tab") or ""), quote=True)}" '
        f'data-selected="{escape(str(ctx.get("selected_text") or ""), quote=True)}" '
        f'data-created="{escape(n.created_at.date().isoformat(), quote=True)}" '
        f'data-body="{escape(body, quote=True)}"'
    )
    # The second-pass route suggestion (user_state.triage_suggest): a stored
    # low-confidence verdict leads the row as ONE one-tap button — the owner
    # confirms a pick instead of operating an 8-option menu. High-confidence
    # verdicts auto-routed at sweep time and never reach this row.
    sugg = ctx.get("route_suggestion")
    sugg_btn = ""
    if isinstance(sugg, dict):
        si = str(sugg.get("intent") or "")
        if si in _INTENT_LABELS:
            reason = str(sugg.get("reason") or "")
            sugg_btn = (
                '<button type="button" class="k-btn k-btn-primary k-btn-sm" '
                f'data-act="route-suggested" data-intent="{escape(si, quote=True)}" '
                f'title="{escape(reason, quote=True)}">'
                f"Route to {escape(_INTENT_LABELS[si])}</button>"
            )
    # The living-grid filter key (data-text); the sort keys reuse the drill-in
    # data-* already on the row (data-created / data-ticker / data-anchor).
    filt = lg.data_text(f"{n.ticker or ''} {_anchor_text(n)} {body}")
    return (
        f"<tr{filt} {data}>"
        f'<td class="tri-when">{escape(n.created_at.date().isoformat())}</td>'
        f"<td>{ticker_cell}</td>"
        f'<td><span class="tri-anchor">{escape(_anchor_text(n))}</span></td>'
        f'<td><button type="button" class="tri-text" data-act="open" '
        f'title="Open detail">{escape(_truncate(body))}</button></td>'
        '<td><div class="tri-acts">'
        f"{sugg_btn}"
        f"{_route_select()}"
        '<button type="button" class="k-btn k-btn-quiet" data-act="resolve">Resolve</button>'
        '<button type="button" class="k-btn k-btn-quiet" data-act="dismiss">Dismiss</button>'
        "</div></td>"
        "</tr>"
    )


def render_triage_list(db_path: Path, *, user_id: str = DEFAULT_USER_ID) -> str:
    """Just the parked-comment table (the fragment the panel JS refreshes after
    an action). Degrades to a quiet empty state on a missing/pre-0074 DB."""
    try:
        notes = list_triage_notes(user_id=user_id, db_path=db_path)
    except sqlite3.Error:
        notes = []
    if not notes:
        return (
            '<p class="tri-empty">Nothing to triage. Comments the classifier cannot '
            "route land here for disposition; the queue is clear.</p>"
        )
    body = "".join(_row(n) for n in notes)
    return (
        lg.grid_open()
        + lg.filter_bar(len(notes), noun="parked", placeholder="Filter by name / anchor / text…")
        + '<table class="p-table"><thead><tr>'
        + lg.th("When", "created", "text", num=False)
        + lg.th("Name", "ticker", "text", num=False)
        + lg.th("Anchor", "anchor", "text", num=False)
        + "<th>Comment</th><th>Disposition</th>"
        + "</tr></thead><tbody>"
        + f"{body}</tbody></table>"
        + lg.grid_close()
    )


def render_triage_panel(
    db_path: Path,
    *,
    user_id: str = DEFAULT_USER_ID,
    embedded: bool = False,
) -> str:
    """The Triage tab fragment: one operating band + the parked-comment table +
    the S4 drill-in drawer. Pure read over ``analyst_notes``; the row
    dispositions POST the ordinary ``/api/notes`` lifecycle routes.

    ``embedded=True`` (the composite Ledger console) collapses the tab-level
    ``panel_toolbar`` to a section heading — the console's single band already
    names and jumps to this section (chrome merge)."""
    try:
        count = len(list_triage_notes(user_id=user_id, db_path=db_path))
    except sqlite3.Error:
        count = 0
    count_chip = f'<span class="k-chip" id="tri-count">{count} open</span>'
    if embedded:
        toolbar = f'<h3 class="tri-h">Triage {count_chip}</h3>'
    else:
        toolbar = panel_toolbar("Triage", filters=count_chip)
    table = render_triage_list(db_path, user_id=user_id)
    return f"""{_PANEL_STYLE}
{toolbar}
<p class="tri-sub">Comments the classifier could not map to an actionable router
(unmappable or conditional directives) park here instead of being force-bucketed.
Route each to the real intent it meant, resolve it once handled, or dismiss it.</p>
<div id="triage-root">
  <div id="triage-list">{table}</div>
  <div class="k-overlay" id="triage-drawer" role="dialog" aria-modal="true"
       aria-label="Triage detail" hidden>
    <div class="tri-d-bar">
      <h3>Parked comment</h3>
      <button type="button" class="tri-d-close" id="triage-drawer-close"
              aria-label="Close">&times;</button>
    </div>
    <div class="tri-d-meta" id="tri-d-meta"></div>
    <div class="tri-d-sel" id="tri-d-sel" hidden></div>
    <div class="tri-d-body" id="tri-d-body"></div>
    <div class="tri-acts" data-note-id="" id="tri-d-acts">
      {_route_select()}
      <button type="button" class="k-btn k-btn-quiet" data-act="resolve">Resolve</button>
      <button type="button" class="k-btn k-btn-quiet" data-act="dismiss">Dismiss</button>
    </div>
  </div>
</div>
<script>
(function () {{
  var root = document.getElementById('triage-root');
  if (!root || root.dataset.wired) return;
  root.dataset.wired = '1';
  var drawer = document.getElementById('triage-drawer');
  var ov = (window.CCOverlay && drawer) ? window.CCOverlay.register(drawer, {{
    scrim: true, trapFocus: true, restoreFocus: true,
    priority: window.CCOverlay.PRIORITY.PEEK, motion: 'slide-right',
    closeId: 'triage-drawer-close', label: 'Triage detail'
  }}) : null;
  function refresh() {{
    fetch('/api/panel/triage?fragment=list').then(function (r) {{ return r.text(); }})
      .then(function (html) {{
        document.getElementById('triage-list').innerHTML = html;
        var n = document.querySelectorAll('#triage-list tr[data-note-id]').length;
        var c = document.getElementById('tri-count');
        if (c) c.textContent = n + ' open';
      }});
  }}
  function post(url, payload, btn) {{
    if (btn) btn.disabled = true;
    fetch(url, {{
      method: 'POST', headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify(payload || {{}})
    }}).then(function (r) {{
      if (r.ok) {{ if (ov) ov.close(); refresh(); }}
      else if (btn) btn.disabled = false;
    }}).catch(function () {{ if (btn) btn.disabled = false; }});
  }}
  // In-place Resolve editor (replaces window.prompt): a table row grows a
  // sibling row with the textarea; the drawer's action strip appends it inline.
  function beginResolve(holder, id) {{
    if (holder.getAttribute('data-editing') === '1') return;
    holder.setAttribute('data-editing', '1');
    var inner =
      '<textarea class="tri-resolve-ta" rows="2" placeholder="Resolution note (optional)"></textarea>'
      + '<div class="tri-acts">'
      + '<button type="button" class="k-btn k-btn-primary k-btn-sm" data-tri-resolve-save>Resolve</button>'
      + '<button type="button" class="k-btn k-btn-quiet k-btn-sm" data-tri-resolve-cancel>Cancel</button>'
      + '</div>';
    var ed;
    if (holder.tagName === 'TR') {{
      ed = document.createElement('tr');
      ed.innerHTML = '<td colspan="5">' + inner + '</td>';
      holder.parentNode.insertBefore(ed, holder.nextSibling);
    }} else {{
      ed = document.createElement('div');
      ed.innerHTML = inner;
      holder.appendChild(ed);
    }}
    var ta = ed.querySelector('.tri-resolve-ta');
    if (ta) ta.focus();
    ed.querySelector('[data-tri-resolve-cancel]').addEventListener('click', function () {{
      ed.remove(); holder.removeAttribute('data-editing');
    }});
    ed.querySelector('[data-tri-resolve-save]').addEventListener('click', function () {{
      var note = (ta && ta.value || '').trim();
      post('/api/notes/' + id + '/resolve', note ? {{resolution_note: note}} : {{}}, this);
    }});
  }}
  function openDrawer(holder) {{
    var meta = [];
    var t = holder.getAttribute('data-ticker');
    meta.push(t ? t : 'PORTFOLIO');
    var anc = holder.getAttribute('data-anchor'); if (anc && anc !== '—') meta.push(anc);
    var rd = holder.getAttribute('data-report-date'); if (rd) meta.push('report ' + rd);
    var tab = holder.getAttribute('data-tab'); if (tab) meta.push('tab ' + tab);
    var cr = holder.getAttribute('data-created'); if (cr) meta.push('filed ' + cr);
    document.getElementById('tri-d-meta').textContent = meta.join('  ·  ');
    var selEl = document.getElementById('tri-d-sel');
    var sel = holder.getAttribute('data-selected');
    if (sel) {{ selEl.textContent = '"' + sel + '"'; selEl.hidden = false; }}
    else {{ selEl.textContent = ''; selEl.hidden = true; }}
    document.getElementById('tri-d-body').textContent = holder.getAttribute('data-body') || '';
    document.getElementById('tri-d-acts').setAttribute(
      'data-note-id', holder.getAttribute('data-note-id') || '');
    var rsel = document.querySelector('#tri-d-acts select[data-act="route"]');
    if (rsel) rsel.value = '';
    if (ov) ov.open();
  }}
  root.addEventListener('click', function (ev) {{
    var btn = ev.target.closest('[data-act]');
    if (!btn) return;
    var holder = btn.closest('[data-note-id]');
    if (!holder) return;
    var act = btn.getAttribute('data-act');
    if (act === 'open') {{ openDrawer(holder); return; }}
    var id = holder.getAttribute('data-note-id');
    if (!id) return;
    if (act === 'resolve') {{
      beginResolve(holder, id);
    }} else if (act === 'dismiss') {{
      post('/api/notes/' + id + '/archive', {{}}, btn);
    }} else if (act === 'route-suggested') {{
      post('/api/notes/' + id + '/route', {{intent: btn.getAttribute('data-intent')}}, btn);
    }}
  }});
  root.addEventListener('change', function (ev) {{
    var sel = ev.target.closest('select[data-act="route"]');
    if (!sel || !sel.value) return;
    var holder = sel.closest('[data-note-id]');
    if (!holder || !holder.getAttribute('data-note-id')) return;
    post('/api/notes/' + holder.getAttribute('data-note-id') + '/route', {{intent: sel.value}}, sel);
  }});
}})();
</script>"""
