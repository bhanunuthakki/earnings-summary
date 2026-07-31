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

from dashboard.inbox_rank import SEMANTIC_ADVISOR_MEMO, note_semantic_kind
from identity import DEFAULT_USER_ID
from journal_links import (
    TARGET_DECISION,
    TARGET_POSITION,
    LinkTarget,
    ReconciliationItem,
    linkable_targets_for_tickers,
    pending_reconciliation,
    targets_for_notes,
)
from ui.prose import render_prose
from user_state.notes import NOTE_KINDS, AnalystNoteRow, list_notes

_PANEL_STYLE = """<style>
.jr-filters { display:flex; gap:8px; align-items:center; margin:4px 0 14px; flex-wrap:wrap; }
/* Inputs/selects: skinned by the shared control kit (ui/controls.py). */
.jr-filters input { width:90px; text-transform:uppercase; }
.jr-count { color:var(--muted); font-size:var(--fs-caption); margin-left:auto; }
.jr-note { border:1px solid var(--border); border-radius:var(--radius);
  background:var(--surface); padding:10px 14px; margin-bottom:10px; }
.jr-head { display:flex; gap:8px; align-items:baseline; flex-wrap:wrap; margin-bottom:6px; }
/* kind tag → quiet .k-chip (dashboards stay category-quiet, §2); ticker →
   .k-tick-sym; the linked-object chip → .k-chip-mono. All from the shared kit. */
.jr-status { font-size:var(--fs-caption); text-transform:uppercase; letter-spacing:.04em; }
.jr-status-open { color:var(--warn); }
.jr-status-resolved { color:var(--ok); }
.jr-status-superseded, .jr-status-archived { color:var(--muted); }
.jr-when, .jr-src { color:var(--muted); font-size:var(--fs-caption); font-family:var(--mono); }
.jr-body { font-size:var(--fs-body); line-height:1.5; color:var(--fg-soft); }
/* Note bodies render through ui.prose (markdown → block HTML); collapse the
   first/last paragraph margins so a one-line note keeps its old tight box. */
.jr-body > :first-child { margin-top:0; }
.jr-body > :last-child { margin-bottom:0; }
.jr-body p { margin:0 0 8px; }
.jr-body ul { margin:0 0 8px; padding-left:20px; }
.jr-body li { margin-bottom:3px; }
.jr-resolution { margin-top:6px; font-size:var(--fs-caption); color:var(--muted); }
.jr-anchor { color:var(--muted); font-size:var(--fs-caption); font-family:var(--mono); }
.jr-actions { display:flex; gap:6px; margin-top:8px; flex-wrap:wrap; align-items:center; }
.jr-actions select { padding:3px 9px; }
.jr-note-new { margin:0 0 16px; }
.jr-note-new textarea { width:100%; box-sizing:border-box; min-height:54px; }
.jr-note-new .jr-row { display:flex; gap:8px; margin-top:6px; }
.jr-empty { color:var(--muted); padding:18px 0; }
.jr-hint { color:var(--muted); font-size:var(--fs-caption); margin-top:10px; }
/* Embedded (composite-console) section heading — the console band owns the
   tab title, so the <h2> collapses to a section-level h3. */
.jr-h { font-size:var(--fs-title); font-weight:600; color:var(--fg);
  margin:var(--sp-4) 0 var(--sp-1); }
/* In-card Resolve / Supersede editor (replaces window.prompt — the blocking
   OS modal the ledger banned in PR9 survived here). */
.jr-edit-ta { width:100%; box-sizing:border-box; min-height:48px; resize:vertical;
  font-family:var(--sans); font-size:var(--fs-body); margin-top:8px; }
/* S15 links: the linked-object chip is a .k-chip-mono (+ .k-chip-warn when the
   linked object has concluded); only the link-control row layout is local. */
.jr-link-box { display:inline-flex; align-items:center; gap:6px; }
.jr-link-box select { max-width:300px; }
.jr-auto { display:inline-flex; align-items:center; gap:4px;
  font-size:var(--fs-caption); color:var(--muted); }
/* Pending reconciliation strip. */
.jr-rec-sec { margin:0 0 16px; }
.jr-rec-head { display:flex; align-items:baseline; gap:8px; margin-bottom:8px; }
/* Reconciliation card = the kit warn well (.k-well .k-well-warn); .jr-rec keeps
   only layout (padding/margin) and is the parent hook for .jr-rec-row. */
.jr-rec { padding:9px 12px; margin-bottom:8px; }
.jr-rec .jr-rec-row { display:flex; gap:8px; align-items:baseline; flex-wrap:wrap; }
.jr-rec-concl { font-size:var(--fs-caption); color:var(--warn); margin:4px 0 6px; }
/* Machine-authored silo (S11): advisor/synthesis memos demote out of the
   owner's journal into a collapsed, recessed section — the journal analog of
   the inbox's identity-based synthesis demotion. */
.jr-synthesis { margin-top:16px; border-top:1px solid var(--border); padding-top:10px; }
.jr-synthesis > summary { cursor:pointer; display:flex; align-items:baseline; gap:8px;
  padding:4px 0; list-style:none; }
.jr-synthesis > summary::-webkit-details-marker { display:none; }
/* Demoted memo card: the --paper inset surface alone signals 'recessed'; no
   dashed edge (not in the de-emphasis ladder — hairline/border/border-2 only). */
.jr-synth-note { border-radius:var(--radius);
  background:var(--paper); padding:10px 14px; margin-bottom:8px; }
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
            chips.append(f'<span class="k-chip k-chip-mono">→ {escape(kind)} #{target_id}</span>')
            continue
        state = f" — {escape(target.conclusion)}" if target.conclusion else ""
        cls = "k-chip k-chip-mono k-chip-warn" if target.concluded else "k-chip k-chip-mono"
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
        f'<span class="k-tick-sym">{escape(n.ticker)}</span>'
        if n.ticker
        else '<span class="k-chip">PORTFOLIO</span>'
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
            link_controls = (
                '<button type="button" class="k-btn k-btn-quiet k-btn-sm" '
                'data-act="unlink">Unlink</button>'
            )
        elif link_options:
            link_controls = (
                '<span class="jr-link-box">'
                f'<select data-role="link-target">{_link_options(link_options)}</select>'
                '<label class="jr-auto"><input type="checkbox" data-role="link-auto">'
                "auto-resolve</label>"
                '<button type="button" class="k-btn k-btn-quiet k-btn-sm" data-act="link">Link</button>'
                "</span>"
            )
        else:
            link_controls = ""
        actions = (
            f'<div class="jr-actions" data-note-id="{n.id}">'
            '<button type="button" class="k-btn k-btn-quiet k-btn-sm" data-act="resolve">Resolve</button>'
            '<button type="button" class="k-btn k-btn-quiet k-btn-sm" data-act="supersede">Supersede</button>'
            '<button type="button" class="k-btn k-btn-quiet k-btn-sm" data-act="archive">Archive</button>'
            f'<select data-act="reclassify" title="Reclassify kind">{kind_opts}</select>'
            f"{link_controls}"
            "</div>"
        )
    return (
        f'<div class="jr-note" data-note="{n.id}">'
        '<div class="jr-head">'
        f'<span class="k-chip">{escape(n.kind)}</span>'
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


def _is_synthesis(n: AnalystNoteRow) -> bool:
    """Identity test (Law 1) — a machine-authored advisor/synthesis memo, read
    through the SAME resolver the inbox uses (``note_semantic_kind``), never by
    re-sniffing the source table. The journal-silo demotion (S11) builds on the
    S3 identity model; it does not re-cut it."""
    return note_semantic_kind(n.source, n.source_ref, n.context) == SEMANTIC_ADVISOR_MEMO


def _synthesis_card(n: AnalystNoteRow) -> str:
    """A demoted, read-oriented card for a machine-authored memo: the body, an
    Open-in-Memos link to its real home, and Archive (the only lifecycle action
    that fits — a regenerated memo is never superseded/reclassified by hand).
    Matches the inbox contract (open-memo → the Memos surface; dismiss →
    /api/notes/<id>/archive)."""
    ticker_html = (
        f'<span class="k-tick-sym">{escape(n.ticker)}</span>'
        if n.ticker
        else '<span class="k-chip">PORTFOLIO</span>'
    )
    return (
        f'<div class="jr-synth-note" data-note="{n.id}">'
        '<div class="jr-head">'
        '<span class="k-chip">Advisor memo</span>'
        f"{ticker_html}"
        f'<span class="jr-when">{escape(n.created_at.date().isoformat())}</span>'
        "</div>"
        f'<div class="jr-body">{render_prose(n.body)}</div>'
        f'<div class="jr-actions" data-note-id="{n.id}">'
        '<a class="k-btn k-btn-quiet k-btn-sm" href="#advisor_memos" '
        'title="Open the advisor Memos surface">Open in Memos</a>'
        '<button type="button" class="k-btn k-btn-quiet k-btn-sm" data-act="archive">Archive</button>'
        "</div></div>"
    )


def _synthesis_silo(notes: list[AnalystNoteRow]) -> str:
    """Collapse the machine-authored memos into one recessed, closed-by-default
    section below the owner's journal — the literal #1 complaint (an advisor
    memo crowding the owner's own thinking) fixed for the journal too."""
    cards = "".join(_synthesis_card(n) for n in notes)
    n = len(notes)
    plural = "s" if n != 1 else ""
    return (
        '<details class="jr-synthesis">'
        '<summary><span class="k-label">Advisor synthesis</span>'
        f'<span class="jr-count">{n} machine-authored memo{plural}</span></summary>'
        f"{cards}</details>"
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
    # Two silos (S11): the owner's own journal vs machine-authored advisor
    # synthesis, split by IDENTITY (not source table). The owner silo keeps the
    # full lifecycle; the synthesis silo demotes into a collapsed section so a
    # generated memo never crowds the analyst's own thinking.
    owner = [n for n in notes if not _is_synthesis(n)]
    synthesis = [n for n in notes if _is_synthesis(n)]
    targets = targets_for_notes(owner, db_path=db_path)
    # Link dropdowns: one targets fetch per distinct ticker among the OPEN
    # unlinked owner notes (the only cards that render the control).
    linkable_tickers = {
        n.ticker
        for n in owner
        if n.status == "open" and n.ticker and n.decision_id is None and n.position_entry_id is None
    }
    options_by_ticker = linkable_targets_for_tickers(
        tickers=linkable_tickers,
        db_path=db_path,
        user_id=user_id,
    )
    owner_html = (
        "".join(
            _note_card(n, targets=targets, link_options=options_by_ticker.get(n.ticker or "", []))
            for n in owner
        )
        or '<div class="jr-empty">No notes of your own match this filter.</div>'
    )
    parts = [owner_html]
    if synthesis:
        parts.append(_synthesis_silo(synthesis))
    return "".join(parts)


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
    ticker_html = (
        f'<span class="k-tick-sym">{escape(n.ticker)}</span>'
        if n.ticker
        else '<span class="k-chip">PORTFOLIO</span>'
    )
    return (
        f'<div class="jr-rec k-well k-well-warn" data-note-id="{n.id}" '
        f'data-suggest="{escape(item.suggested_resolution, quote=True)}">'
        '<div class="jr-rec-row">'
        f'<span class="k-chip">{escape(n.kind)}</span>'
        f"{ticker_html}"
        f'<span class="jr-when">{escape(n.created_at.date().isoformat())}</span>'
        "</div>"
        f'<div class="jr-body">{render_prose(n.body)}</div>'
        f'<div class="jr-rec-concl">{escape(t.kind)} #{t.target_id} · '
        f"{escape(t.label)} — {escape(t.conclusion or 'concluded')}</div>"
        '<div class="jr-actions">'
        '<button type="button" class="k-btn k-btn-quiet k-btn-sm" '
        'data-act="rec-resolve">Resolve with conclusion</button>'
        '<button type="button" class="k-btn k-btn-quiet k-btn-sm" data-act="unlink" '
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
    embedded: bool = False,
) -> str:
    """The Research → Journal tab fragment: capture form + reconciliation
    strip + filters + list.

    ``embedded=True`` (the composite Ledger console) collapses the tab-level
    ``<h2>`` to a section heading — the console's single band already names
    and jumps to this section (chrome merge)."""
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
    heading = '<h3 class="jr-h">Journal</h3>' if embedded else "<h2>Journal</h2>"
    return f"""{_PANEL_STYLE}
{heading}
<div id="jr-root">
<form class="jr-note-new" id="jr-new">
  <textarea name="body" placeholder="New note&hellip; (a watch item, a question to answer, an assumption to check)"></textarea>
  <div class="jr-row">
    <select name="kind">{new_kind_opts}</select>
    <input name="ticker" placeholder="TICKER" title="Blank = portfolio-level note">
    <button type="submit" class="k-btn k-btn-primary">Add note</button>
  </div>
</form>
<div id="jr-reconcile">{reconcile}</div>
<form class="jr-filters" id="jr-filters">
  <input name="ticker" placeholder="TICKER" value="{ticker_val}">
  <select name="kind">{kind_opts}</select>
  <select name="status">{status_opts}</select>
  <button type="submit" class="k-btn k-btn-quiet">Filter</button>
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
  // In-card editor for resolve / rec-resolve / supersede (replaces the
  // blocking window.prompt modals): textarea + kit Save/Cancel appended to
  // the card, pre-filled where a suggestion exists.
  function beginEdit(holder, id, act, prefill, placeholder, required) {{
    if (holder.getAttribute('data-editing') === '1') return;
    holder.setAttribute('data-editing', '1');
    var ed = document.createElement('div');
    var ta = document.createElement('textarea');
    ta.className = 'jr-edit-ta'; ta.rows = 2; ta.placeholder = placeholder; ta.value = prefill;
    var row = document.createElement('div'); row.className = 'jr-actions';
    var save = document.createElement('button');
    save.type = 'button'; save.className = 'k-btn k-btn-primary k-btn-sm';
    save.textContent = act === 'supersede' ? 'Supersede' : 'Resolve';
    var cancel = document.createElement('button');
    cancel.type = 'button'; cancel.className = 'k-btn k-btn-quiet k-btn-sm';
    cancel.textContent = 'Cancel';
    row.appendChild(save); row.appendChild(cancel);
    ed.appendChild(ta); ed.appendChild(row);
    holder.appendChild(ed);
    ta.focus(); ta.setSelectionRange(ta.value.length, ta.value.length);
    cancel.addEventListener('click', function () {{
      ed.remove(); holder.removeAttribute('data-editing');
    }});
    save.addEventListener('click', function () {{
      var txt = ta.value.trim();
      if (required && !txt) {{ ta.focus(); return; }}
      CCAction.busy(save, 'Saving\\u2026');
      var payload = {{}};
      if (act === 'supersede') payload.body = txt;
      else if (txt) payload.resolution_note = txt;
      fetch('/api/notes/' + id + '/' + act, {{
        method: 'POST', headers: {{'Content-Type': 'application/json'}},
        body: JSON.stringify(payload)
      }}).then(function (r) {{ if (r.ok) refresh(); else CCAction.release(save); }})
        .catch(function () {{ CCAction.release(save); }});
    }});
  }}
  root.addEventListener('click', function (ev) {{
    var btn = ev.target.closest('button[data-act]');
    if (!btn) return;
    var holder = btn.closest('[data-note-id]');
    if (!holder) return;
    var id = holder.getAttribute('data-note-id');
    var act = btn.getAttribute('data-act');
    if (act === 'resolve') {{
      beginEdit(holder, id, 'resolve', '', 'Resolution note (optional)', false);
      return;
    }}
    if (act === 'rec-resolve') {{
      beginEdit(holder, id, 'resolve', holder.getAttribute('data-suggest') || '',
                'Resolution note', false);
      return;
    }}
    if (act === 'supersede') {{
      beginEdit(holder, id, 'supersede', '', 'Replacement note text', true);
      return;
    }}
    var payload = {{}};
    if (act === 'link') {{
      var sel = holder.querySelector('select[data-role="link-target"]');
      if (!sel || !sel.value) return;
      var parts = sel.value.split(':');
      if (parts[0] === 'decision') payload.decision_id = parseInt(parts[1], 10);
      else payload.position_entry_id = parseInt(parts[1], 10);
      var auto = holder.querySelector('input[data-role="link-auto"]');
      payload.auto_resolve = !!(auto && auto.checked);
    }}
    CCAction.busy(btn);
    fetch('/api/notes/' + id + '/' + act, {{
      method: 'POST', headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify(payload)
    }}).then(function (r) {{ if (r.ok) refresh(); else CCAction.release(btn); }})
      .catch(function () {{ CCAction.release(btn); }});
  }});
  root.addEventListener('change', function (ev) {{
    var sel = ev.target.closest('select[data-act="reclassify"]');
    if (!sel) return;
    var holder = sel.closest('[data-note-id]');
    if (!holder) return;
    CCAction.busy(sel);
    fetch('/api/notes/' + holder.getAttribute('data-note-id') + '/reclassify', {{
      method: 'POST', headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{kind: sel.value}})
    }}).then(function (r) {{ if (r.ok) refresh(); else CCAction.release(sel); }})
      .catch(function () {{ CCAction.release(sel); }});
  }});
}})();
</script>"""
