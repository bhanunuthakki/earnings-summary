"""Research → Journal panel (P4.5 + S15 links): the analyst_notes lifecycle UI.

The journal is the durable record of the analyst's thinking
(``analyst_notes``, alembic 0074). Until now every surface that showed
notes was read-only (the P1.3 holding rail, the P4.4 digest/report/alert
resurfacing); this panel is where the lifecycle happens:

  * list + filter (ticker / kind / status),
  * resolve (with an optional resolution note),
  * reclassify (change the kind),
  * supersede (a correcting follow-up note, chained — never an in-place
    rewrite),
  * archive (no longer relevant),
  * link / unlink (S15, alembic 0093): attach a note to the decision or
    position stint it is about, optionally opting into auto-resolve when
    that object concludes.

A **Pending reconciliation** strip sits above the list: open notes whose
linked decision has been graded or whose linked position has exited, each
with a one-click resolve (pre-filled with the conclusion) or unlink ("the
note outlives its decision"). The same items resurface in the Home-rail
inbox via ``dashboard.inbox``.

The fragment is served by ``/api/panel/journal`` (lazy command-center tab;
``?fragment=list`` and ``?fragment=reconcile`` refresh the two dynamic
regions) and the actions POST to the ``/api/notes`` REST routes on
comments_server. All interaction is a single delegated listener on the
panel container so the list survives its own innerHTML refreshes.
"""

from __future__ import annotations

import sqlite3
from html import escape
from pathlib import Path

from identity import DEFAULT_USER_ID
from journal_links import (
    TARGET_DECISION,
    TARGET_POSITION,
    LinkTarget,
    ReconciliationItem,
    linkable_targets,
    pending_reconciliation,
    targets_for_notes,
)
from ui.prose import render_prose
from user_state.notes import NOTE_KINDS, AnalystNoteRow, list_notes

_PANEL_STYLE = """<style>
.jr-filters { display:flex; gap:8px; align-items:center; margin:4px 0 14px; flex-wrap:wrap; }
/* Inputs/selects: skinned by the shared control kit (ui/controls.py). */
.jr-filters input { width:90px; text-transform:uppercase; }
.jr-filters button { background:var(--accent-soft); color:var(--accent);
  border:1px solid var(--accent); border-radius:var(--radius); padding:5px 12px;
  font-size:var(--fs-body); cursor:pointer; }
.jr-count { color:var(--muted); font-size:var(--fs-caption); margin-left:auto; }
.jr-note { border:1px solid var(--border); border-radius:var(--radius);
  background:var(--surface); padding:10px 14px; margin-bottom:10px; }
.jr-head { display:flex; gap:8px; align-items:baseline; flex-wrap:wrap; margin-bottom:6px; }
.jr-kind { font-size:var(--fs-micro); font-weight:600;
  text-transform:uppercase; letter-spacing:.05em; color:var(--accent);
  border:1px solid color-mix(in srgb, var(--accent) 45%, transparent);
  border-radius:var(--radius-full); padding:0 6px; }
.jr-ticker { font-family:var(--mono); font-weight:600; }
.jr-status { font-size:var(--fs-micro); text-transform:uppercase; letter-spacing:.04em; }
.jr-status-open { color:var(--warn); }
.jr-status-resolved { color:var(--ok); }
.jr-status-superseded, .jr-status-archived { color:var(--muted); }
.jr-when, .jr-src { color:var(--muted); font-size:var(--fs-micro); font-family:var(--mono); }
.jr-body { font-size:var(--fs-body); line-height:1.5; color:var(--fg-soft); }
/* Note bodies render through ui.prose (markdown → block HTML); collapse the
   first/last paragraph margins so a one-line note keeps its old tight box. */
.jr-body > :first-child { margin-top:0; }
.jr-body > :last-child { margin-bottom:0; }
.jr-body p { margin:0 0 8px; }
.jr-body ul { margin:0 0 8px; padding-left:20px; }
.jr-body li { margin-bottom:3px; }
.jr-resolution { margin-top:6px; font-size:var(--fs-caption); color:var(--muted); }
.jr-anchor { color:var(--muted); font-size:var(--fs-micro); font-family:var(--mono); }
.jr-actions { display:flex; gap:6px; margin-top:8px; flex-wrap:wrap; align-items:center; }
.jr-actions select { font-size:var(--fs-caption); padding:3px 9px; }
.jr-actions button { background:transparent; color:var(--muted);
  border:1px solid var(--border); border-radius:var(--radius); padding:3px 9px;
  font-size:var(--fs-caption); cursor:pointer; }
.jr-actions button:hover { border-color:var(--accent); color:var(--accent); }
.jr-note-new { margin:0 0 16px; }
.jr-note-new textarea { width:100%; box-sizing:border-box; min-height:54px; }
.jr-note-new .jr-row { display:flex; gap:8px; margin-top:6px; }
.jr-empty { color:var(--muted); padding:18px 0; }
.jr-hint { color:var(--muted); font-size:var(--fs-caption); margin-top:10px; }
/* S15 links: the linked-object chip on a card + the link controls. */
.jr-link { display:inline-flex; align-items:center; gap:4px;
  font-size:var(--fs-micro); font-family:var(--mono); color:var(--muted);
  border:1px solid var(--border); border-radius:var(--radius-full); padding:0 8px; }
.jr-link.is-concluded { color:var(--warn);
  border-color:color-mix(in srgb, var(--warn) 45%, transparent); }
.jr-link-box { display:inline-flex; align-items:center; gap:6px; }
.jr-link-box select { max-width:300px; }
.jr-auto { display:inline-flex; align-items:center; gap:4px;
  font-size:var(--fs-micro); color:var(--muted); }
/* Pending reconciliation strip. */
.jr-rec-sec { margin:0 0 16px; }
.jr-rec-head { display:flex; align-items:baseline; gap:8px; margin-bottom:8px; }
.jr-rec { border:1px solid color-mix(in srgb, var(--warn) 35%, transparent);
  border-radius:var(--radius); background:var(--surface);
  padding:9px 12px; margin-bottom:8px; }
.jr-rec .jr-rec-row { display:flex; gap:8px; align-items:baseline; flex-wrap:wrap; }
.jr-rec-concl { font-size:var(--fs-caption); color:var(--warn); margin:4px 0 6px; }
</style>"""

_STATUS_FILTERS = ("open", "resolved", "superseded", "archived", "all")


def _link_chip(n: AnalystNoteRow, targets: dict[tuple[str, int], LinkTarget]) -> str:
    """The linked-object chip(s) on one card — concluded links turn warn-toned
    (the visual cue that the note is awaiting reconciliation)."""
    chips: list[str] = []
    for kind, target_id in (
        (TARGET_DECISION, n.decision_id),
        (TARGET_POSITION, n.position_entry_id),
    ):
        if target_id is None:
            continue
        target = targets.get((kind, target_id))
        if target is None:
            chips.append(f'<span class="jr-link">→ {escape(kind)} #{target_id}</span>')
            continue
        state = f" — {escape(target.conclusion)}" if target.conclusion else ""
        cls = "jr-link is-concluded" if target.concluded else "jr-link"
        auto = " · auto-resolve" if n.link_auto_resolve and not target.concluded else ""
        chips.append(
            f'<span class="{cls}" title="{escape(target.ticker)} {escape(target.label)}">'
            f"→ {escape(kind)} #{target_id}{state}{escape(auto)}</span>"
        )
    return "".join(chips)


def _link_options(options: list[LinkTarget]) -> str:
    """The link dropdown's option list, grouped decisions-then-positions."""
    decision_opts: list[str] = []
    position_opts: list[str] = []
    for t in options:
        label = f"#{t.target_id} · {t.label}" + (f" · {t.conclusion}" if t.conclusion else "")
        opt = f'<option value="{escape(t.kind)}:{t.target_id}">{escape(label)}</option>'
        (decision_opts if t.kind == TARGET_DECISION else position_opts).append(opt)
    out = '<option value="">link to&hellip;</option>'
    if decision_opts:
        out += f'<optgroup label="Decisions">{"".join(decision_opts)}</optgroup>'
    if position_opts:
        out += f'<optgroup label="Position stints">{"".join(position_opts)}</optgroup>'
    return out


def _note_card(
    n: AnalystNoteRow,
    *,
    targets: dict[tuple[str, int], LinkTarget],
    link_options: list[LinkTarget],
) -> str:
    ticker_html = (
        f'<span class="jr-ticker">{escape(n.ticker)}</span>'
        if n.ticker
        else '<span class="jr-ticker" style="color:var(--muted)">PORTFOLIO</span>'
    )
    anchor = ""
    if n.anchor_type:
        key = f" · {escape(n.anchor_key)}" if n.anchor_key else ""
        anchor = f'<span class="jr-anchor">@ {escape(n.anchor_type)}{key}</span>'
    resolution = ""
    if n.resolution_note:
        resolution = f'<div class="jr-resolution">↳ {escape(n.resolution_note)}</div>'
    elif n.supersedes_id:
        resolution = f'<div class="jr-resolution">supersedes note #{n.supersedes_id}</div>'
    actions = ""
    if n.status == "open":
        kind_opts = "".join(
            f'<option value="{escape(k)}"{" selected" if k == n.kind else ""}>{escape(k)}</option>'
            for k in NOTE_KINDS
        )
        linked = n.decision_id is not None or n.position_entry_id is not None
        if linked:
            link_controls = '<button type="button" data-act="unlink">Unlink</button>'
        elif link_options:
            link_controls = (
                '<span class="jr-link-box">'
                f'<select data-role="link-target">{_link_options(link_options)}</select>'
                '<label class="jr-auto"><input type="checkbox" data-role="link-auto">'
                "auto-resolve</label>"
                '<button type="button" data-act="link">Link</button>'
                "</span>"
            )
        else:
            link_controls = ""
        actions = (
            f'<div class="jr-actions" data-note-id="{n.id}">'
            '<button type="button" data-act="resolve">Resolve</button>'
            '<button type="button" data-act="supersede">Supersede</button>'
            '<button type="button" data-act="archive">Archive</button>'
            f'<select data-act="reclassify" title="Reclassify kind">{kind_opts}</select>'
            f"{link_controls}"
            "</div>"
        )
    return (
        f'<div class="jr-note" data-note="{n.id}">'
        '<div class="jr-head">'
        f'<span class="jr-kind">{escape(n.kind)}</span>'
        f"{ticker_html}"
        f'<span class="jr-status jr-status-{escape(n.status)}">{escape(n.status)}</span>'
        f'<span class="jr-when">{escape(n.created_at.date().isoformat())}</span>'
        f'<span class="jr-src">{escape(n.source)}</span>'
        f"{anchor}"
        f"{_link_chip(n, targets)}"
        "</div>"
        f'<div class="jr-body">{render_prose(n.body)}</div>'
        f"{resolution}"
        f"{actions}"
        "</div>"
    )


def render_journal_list(
    db_path: Path,
    *,
    user_id: str = DEFAULT_USER_ID,
    ticker: str | None = None,
    kind: str | None = None,
    status: str = "open",
) -> str:
    """Just the filtered note list (the fragment the panel JS refreshes).

    Unknown kind/status filter values are dropped rather than erroring —
    this is a viewer, and stray query params shouldn't 500 the tab."""
    list_status = None if status == "all" else status
    if list_status is not None and list_status not in _STATUS_FILTERS:
        list_status = "open"
    if kind is not None and kind not in NOTE_KINDS:
        kind = None
    try:
        notes = list_notes(
            user_id=user_id,
            ticker=ticker or None,
            kind=kind or None,
            status=list_status,
            db_path=db_path,
        )
    except sqlite3.Error:  # missing DB / pre-0074 schema degrades to empty
        notes = []
    if not notes:
        return (
            '<div class="jr-empty">No notes match this filter. Notes arrive from '
            "report comments, chat, alert reviews, advisor memos — or directly "
            "from the form above.</div>"
        )
    targets = targets_for_notes(notes, db_path=db_path)
    # Link dropdowns: one targets fetch per distinct ticker among the OPEN
    # unlinked notes (the only cards that render the control).
    options_by_ticker: dict[str, list[LinkTarget]] = {}
    for n in notes:
        if (
            n.status == "open"
            and n.ticker
            and n.decision_id is None
            and n.position_entry_id is None
            and n.ticker not in options_by_ticker
        ):
            options_by_ticker[n.ticker] = linkable_targets(
                ticker=n.ticker, db_path=db_path, user_id=user_id
            )
    return "".join(
        _note_card(
            n,
            targets=targets,
            link_options=options_by_ticker.get(n.ticker or "", []),
        )
        for n in notes
    )


def render_reconciliation_list(
    db_path: Path,
    *,
    user_id: str = DEFAULT_USER_ID,
    ticker: str | None = None,
) -> str:
    """The pending-reconciliation strip: open notes whose linked decision /
    position has concluded. Empty string when there is nothing to reconcile
    (the section disappears entirely)."""
    items = pending_reconciliation(db_path=db_path, user_id=user_id, ticker=ticker)
    if not items:
        return ""
    cards = "".join(_reconciliation_card(item) for item in items)
    return (
        '<section class="jr-rec-sec">'
        '<div class="jr-rec-head">'
        '<span class="k-chip k-chip-warn">pending reconciliation</span>'
        f'<span class="jr-count">{len(items)} open '
        f"note{'s' if len(items) != 1 else ''} whose linked object concluded</span>"
        "</div>"
        f"{cards}"
        "</section>"
    )


def _reconciliation_card(item: ReconciliationItem) -> str:
    n = item.note
    t = item.target
    ticker_html = f'<span class="jr-ticker">{escape(n.ticker or "PORTFOLIO")}</span>'
    return (
        f'<div class="jr-rec" data-note-id="{n.id}" '
        f'data-suggest="{escape(item.suggested_resolution, quote=True)}">'
        '<div class="jr-rec-row">'
        f'<span class="jr-kind">{escape(n.kind)}</span>'
        f"{ticker_html}"
        f'<span class="jr-when">{escape(n.created_at.date().isoformat())}</span>'
        "</div>"
        f'<div class="jr-body">{render_prose(n.body)}</div>'
        f'<div class="jr-rec-concl">{escape(t.kind)} #{t.target_id} · '
        f"{escape(t.label)} — {escape(t.conclusion or 'concluded')}</div>"
        '<div class="jr-actions">'
        '<button type="button" data-act="rec-resolve">Resolve with conclusion</button>'
        '<button type="button" data-act="unlink" '
        'title="Keep the note open; detach it from the concluded object">'
        "Keep open (unlink)</button>"
        "</div></div>"
    )


def render_journal_panel(
    db_path: Path,
    *,
    user_id: str = DEFAULT_USER_ID,
    ticker: str | None = None,
    kind: str | None = None,
    status: str = "open",
) -> str:
    """The Research → Journal tab fragment: capture form + reconciliation
    strip + filters + list."""
    if status not in _STATUS_FILTERS:
        status = "open"
    kind_opts = '<option value="">any kind</option>' + "".join(
        f'<option value="{escape(k)}"{" selected" if k == kind else ""}>{escape(k)}</option>'
        for k in NOTE_KINDS
    )
    status_opts = "".join(
        f'<option value="{escape(s)}"{" selected" if s == status else ""}>{escape(s)}</option>'
        for s in _STATUS_FILTERS
    )
    new_kind_opts = "".join(f'<option value="{escape(k)}">{escape(k)}</option>' for k in NOTE_KINDS)
    note_list = render_journal_list(
        db_path, user_id=user_id, ticker=ticker, kind=kind, status=status
    )
    reconcile = render_reconciliation_list(db_path, user_id=user_id, ticker=ticker)
    ticker_val = escape(ticker or "")
    return f"""{_PANEL_STYLE}
<h2>Journal</h2>
<div id="jr-root">
<form class="jr-note-new" id="jr-new">
  <textarea name="body" placeholder="New note&hellip; (a watch item, a question to answer, an assumption to check)"></textarea>
  <div class="jr-row">
    <select name="kind">{new_kind_opts}</select>
    <input name="ticker" placeholder="TICKER" title="Blank = portfolio-level note">
    <button type="submit">Add note</button>
  </div>
</form>
<div id="jr-reconcile">{reconcile}</div>
<form class="jr-filters" id="jr-filters">
  <input name="ticker" placeholder="TICKER" value="{ticker_val}">
  <select name="kind">{kind_opts}</select>
  <select name="status">{status_opts}</select>
  <button type="submit">Filter</button>
  <span class="jr-count" id="jr-count"></span>
</form>
<div id="jr-list">{note_list}</div>
<p class="jr-hint">Resolve closes an item (optionally with what answered it);
supersede records a correction as a new chained note; archive drops it from
live recall. Link attaches a note to the decision or position stint it is
about — when that object concludes, the note auto-resolves (if you opted in)
or surfaces above for reconciliation. Superseded and archived notes are kept
forever — memory is the point.</p>
</div>
<script>
(function () {{
  var root = document.getElementById('jr-root');
  if (!root || root.dataset.wired) return;
  root.dataset.wired = '1';
  function refresh() {{
    var f = document.getElementById('jr-filters');
    var qs = new URLSearchParams({{
      ticker: f.ticker.value.trim().toUpperCase(),
      kind: f.kind.value, status: f.status.value, fragment: 'list'
    }});
    fetch('/api/panel/journal?' + qs).then(function (r) {{ return r.text(); }})
      .then(function (html) {{ document.getElementById('jr-list').innerHTML = html; }});
    var rq = new URLSearchParams({{
      ticker: f.ticker.value.trim().toUpperCase(), fragment: 'reconcile'
    }});
    fetch('/api/panel/journal?' + rq).then(function (r) {{ return r.text(); }})
      .then(function (html) {{ document.getElementById('jr-reconcile').innerHTML = html; }});
  }}
  document.getElementById('jr-filters').addEventListener('submit', function (ev) {{
    ev.preventDefault(); refresh();
  }});
  document.getElementById('jr-new').addEventListener('submit', function (ev) {{
    ev.preventDefault();
    var form = ev.target;
    var body = form.body.value.trim();
    if (!body) return;
    fetch('/api/notes', {{
      method: 'POST', headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{
        body: body, kind: form.kind.value,
        ticker: form.ticker.value.trim().toUpperCase() || null
      }})
    }}).then(function (r) {{
      if (r.ok) {{ form.body.value = ''; form.ticker.value = ''; refresh(); }}
    }});
  }});
  root.addEventListener('click', function (ev) {{
    var btn = ev.target.closest('button[data-act]');
    if (!btn) return;
    var holder = btn.closest('[data-note-id]');
    if (!holder) return;
    var id = holder.getAttribute('data-note-id');
    var act = btn.getAttribute('data-act');
    var payload = {{}};
    if (act === 'resolve') {{
      var note = window.prompt('Resolution note (optional):', '');
      if (note === null) return;
      if (note.trim()) payload.resolution_note = note.trim();
    }}
    if (act === 'rec-resolve') {{
      var suggested = holder.getAttribute('data-suggest') || '';
      var rec = window.prompt('Resolution note:', suggested);
      if (rec === null) return;
      act = 'resolve';
      if (rec.trim()) payload.resolution_note = rec.trim();
    }}
    if (act === 'supersede') {{
      var text = window.prompt('Replacement note text:', '');
      if (text === null || !text.trim()) return;
      payload.body = text.trim();
    }}
    if (act === 'link') {{
      var sel = holder.querySelector('select[data-role="link-target"]');
      if (!sel || !sel.value) return;
      var parts = sel.value.split(':');
      if (parts[0] === 'decision') payload.decision_id = parseInt(parts[1], 10);
      else payload.position_entry_id = parseInt(parts[1], 10);
      var auto = holder.querySelector('input[data-role="link-auto"]');
      payload.auto_resolve = !!(auto && auto.checked);
    }}
    fetch('/api/notes/' + id + '/' + act, {{
      method: 'POST', headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify(payload)
    }}).then(function (r) {{ if (r.ok) refresh(); }});
  }});
  root.addEventListener('change', function (ev) {{
    var sel = ev.target.closest('select[data-act="reclassify"]');
    if (!sel) return;
    var holder = sel.closest('[data-note-id]');
    if (!holder) return;
    fetch('/api/notes/' + holder.getAttribute('data-note-id') + '/reclassify', {{
      method: 'POST', headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{kind: sel.value}})
    }}).then(function (r) {{ if (r.ok) refresh(); }});
  }});
}})();
</script>"""
