"""Research → Journal panel (P4.5): the analyst_notes lifecycle UI.

The journal is the durable record of the analyst's thinking
(``analyst_notes``, alembic 0074). Until now every surface that showed
notes was read-only (the P1.3 holding rail, the P4.4 digest/report/alert
resurfacing); this panel is where the lifecycle happens:

  * list + filter (ticker / kind / status),
  * resolve (with an optional resolution note),
  * reclassify (change the kind),
  * supersede (a correcting follow-up note, chained — never an in-place
    rewrite),
  * archive (no longer relevant).

The fragment is served by ``/api/panel/journal`` (lazy command-center tab)
and the actions POST to the ``/api/notes`` REST routes on comments_server.
All interaction is a single delegated listener on the panel container so
the list survives its own innerHTML refreshes.
"""

from __future__ import annotations

import sqlite3
from html import escape
from pathlib import Path

from identity import DEFAULT_USER_ID
from user_state.notes import NOTE_KINDS, AnalystNoteRow, list_notes

_PANEL_STYLE = """<style>
.jr-filters { display:flex; gap:8px; align-items:center; margin:4px 0 14px; flex-wrap:wrap; }
/* Inputs/selects: skinned by the shared control kit (ui/controls.py). */
.jr-filters input { width:90px; text-transform:uppercase; }
.jr-filters button { background:var(--accent-soft); color:var(--accent);
  border:1px solid var(--accent); border-radius:var(--radius); padding:5px 12px;
  font-size:var(--fs-body); cursor:pointer; }
.jr-count { color:var(--muted); font-size:var(--fs-caption); margin-left:auto; }
.jr-note { border:1px solid var(--border,#2a2d31); border-radius:8px;
  background:#14161b; padding:10px 14px; margin-bottom:10px; }
.jr-head { display:flex; gap:8px; align-items:baseline; flex-wrap:wrap; margin-bottom:6px; }
.jr-kind { font-family:var(--mono,monospace); font-size:9.5px; font-weight:600;
  text-transform:uppercase; letter-spacing:.05em; color:#8aa8ff;
  border:1px solid #8aa8ff; border-radius:3px; padding:0 5px; }
.jr-ticker { font-family:var(--mono,monospace); font-weight:600; }
.jr-status { font-size:10.5px; text-transform:uppercase; letter-spacing:.04em; }
.jr-status-open { color:#f5c66a; }
.jr-status-resolved { color:#4ade80; }
.jr-status-superseded, .jr-status-archived { color:var(--muted,#9aa0a6); }
.jr-when, .jr-src { color:var(--muted,#9aa0a6); font-size:11px; font-family:var(--mono,monospace); }
.jr-body { font-size:13px; line-height:1.5; color:var(--fg-soft,#d5d6d2); }
.jr-resolution { margin-top:6px; font-size:12px; color:var(--muted,#9aa0a6); }
.jr-anchor { color:var(--muted,#9aa0a6); font-size:10.5px; font-family:var(--mono,monospace); }
.jr-actions { display:flex; gap:6px; margin-top:8px; flex-wrap:wrap; }
.jr-actions select { font-size:var(--fs-caption); padding:3px 9px; }
.jr-actions button { background:transparent; color:var(--muted);
  border:1px solid var(--border); border-radius:var(--radius); padding:3px 9px;
  font-size:var(--fs-caption); cursor:pointer; }
.jr-actions button:hover { border-color:var(--accent); color:var(--accent); }
.jr-note-new { margin:0 0 16px; }
.jr-note-new textarea { width:100%; box-sizing:border-box; min-height:54px; }
.jr-note-new .jr-row { display:flex; gap:8px; margin-top:6px; }
.jr-empty { color:var(--muted,#9aa0a6); padding:18px 0; }
.jr-hint { color:var(--muted,#9aa0a6); font-size:11.5px; margin-top:10px; }
</style>"""

_STATUS_FILTERS = ("open", "resolved", "superseded", "archived", "all")


def _note_card(n: AnalystNoteRow) -> str:
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
        actions = (
            f'<div class="jr-actions" data-note-id="{n.id}">'
            '<button type="button" data-act="resolve">Resolve</button>'
            '<button type="button" data-act="supersede">Supersede</button>'
            '<button type="button" data-act="archive">Archive</button>'
            f'<select data-act="reclassify" title="Reclassify kind">{kind_opts}</select>'
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
        "</div>"
        f'<div class="jr-body">{escape(n.body)}</div>'
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
    return "".join(_note_card(n) for n in notes)


def render_journal_panel(
    db_path: Path,
    *,
    user_id: str = DEFAULT_USER_ID,
    ticker: str | None = None,
    kind: str | None = None,
    status: str = "open",
) -> str:
    """The Research → Journal tab fragment: capture form + filters + list."""
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
live recall. Superseded and archived notes are kept forever — memory is the
point.</p>
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
    if (act === 'supersede') {{
      var text = window.prompt('Replacement note text:', '');
      if (text === null || !text.trim()) return;
      payload.body = text.trim();
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
