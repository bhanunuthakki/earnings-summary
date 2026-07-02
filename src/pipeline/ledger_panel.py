"""The Ledger flat musings panel — read your captured stream of consciousness.

Wave A (the dogfood read-back): a newest-first list of captured musings
(``kind='musing'``, ``source='capture'``) plus an at-desk quick-capture box that
POSTs to ``/api/capture/text``. The Telegram bot is the primary, on-the-go mouth;
this panel is the desk mouth + the read-back that proves capture earns its keep.
Theme synthesis + FTS search land in Wave B.

Pure read over the analyst_notes spine; token-only styles (guard-clean).
"""

from __future__ import annotations

from html import escape
from pathlib import Path
from typing import cast

from identity import DEFAULT_USER_ID
from onmymind.feed import (
    LADDER_LABELS,
    FeedItem,
    load_feed,
    onmymind_enabled,
)
from pipeline.worldview_panel import render_worldview_section
from research.proposals import (
    ResearchProposal,
    ResearchTask,
    list_proposals,
    list_tasks,
    research_run_enabled,
)
from synthesis.insights import InsightRow, list_insights
from ui.controls import ticker_label
from ui.prose import render_prose
from ui.time import stamp_html
from user_state.notes import AnalystNoteRow, list_notes

_PANEL_STYLE = """<style>
.ledger-cap { background: var(--surface); border-radius: var(--radius); padding: var(--sp-3) var(--sp-4); margin-bottom: var(--sp-4); }
.ledger-cap textarea { width: 100%; min-height: 64px; resize: vertical; font-family: var(--sans); font-size: var(--fs-body); }
.ledger-cap-row { display: flex; align-items: center; gap: var(--sp-2); margin-top: var(--sp-2); }
.ledger-cap-status { font-size: var(--fs-caption); color: var(--muted); }
.ledger-musing { background: var(--surface); border-radius: var(--radius); padding: var(--sp-3) var(--sp-4); margin-bottom: var(--sp-2); }
.ledger-musing-head { display: flex; align-items: baseline; gap: var(--sp-2); margin-bottom: var(--sp-1); }
.ledger-when { color: var(--muted); font-family: var(--mono); font-size: var(--fs-micro); margin-left: auto; white-space: nowrap; }
.ledger-chan { font-size: var(--fs-micro); color: var(--muted); text-transform: uppercase; letter-spacing: 0.05em; }
.ledger-needs { color: var(--warn); font-size: var(--fs-micro); font-weight: 600; text-transform: uppercase; letter-spacing: 0.05em; }
.ledger-unattr { color: var(--muted); font-size: var(--fs-micro); text-transform: uppercase; letter-spacing: 0.05em; }
.ledger-body { font-size: var(--fs-body); line-height: 1.55; color: var(--fg-soft); overflow-wrap: anywhere; }
.ledger-body > :first-child { margin-top: 0; }
.ledger-body > :last-child { margin-bottom: 0; }
.ledger-empty { color: var(--muted); font-style: italic; padding: var(--sp-3) 0; }
.ledger-sec-h { font-size: var(--fs-section); font-weight: 600; color: var(--fg); margin: var(--sp-4) 0 var(--sp-1); }
.ledger-sec-sub { font-size: var(--fs-caption); color: var(--muted); margin: 0 0 var(--sp-3); }
.ledger-stance { background: var(--surface); border-left: 3px solid var(--accent); border-radius: var(--radius); padding: var(--sp-3) var(--sp-4); margin-bottom: var(--sp-2); }
.ledger-stance-head { display: flex; align-items: baseline; gap: var(--sp-2); margin-bottom: var(--sp-1); }
.ledger-stance-meta { color: var(--muted); font-size: var(--fs-micro); margin-left: auto; }
</style>"""

_CAPTURE_JS = """<script>(function(){
  var btn=document.getElementById('ledger-cap-btn');
  var ta=document.getElementById('ledger-cap-text');
  var st=document.getElementById('ledger-cap-status');
  if(!btn||!ta){ return; }
  function send(){
    var text=(ta.value||'').trim();
    if(!text){ ta.focus(); return; }
    btn.disabled=true; if(st){ st.textContent='Capturing...'; }
    fetch('/api/capture/text',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({text:text})})
      .then(function(r){ return r.json(); })
      .then(function(res){
        ta.value='';
        var tag = (res && res.ticker) ? (' - '+res.ticker) : ((res && res.needs_ticker) ? ' - needs ticker' : '');
        if(st){ st.textContent='Captured'+tag; }
        var list=document.getElementById('ledger-list');
        if(list){ fetch('/api/panel/musings?fragment=list').then(function(r){return r.text();}).then(function(h){ list.innerHTML=h; }); }
      })
      .catch(function(){ if(st){ st.textContent='Could not reach the server - try again.'; } })
      .finally(function(){ btn.disabled=false; setTimeout(function(){ if(st){ st.textContent=''; } },4000); });
  }
  btn.addEventListener('click', send);
  ta.addEventListener('keydown', function(e){ if((e.metaKey||e.ctrlKey) && e.key==='Enter'){ send(); } });
})();</script>"""


def _capture_box() -> str:
    return (
        '<div class="ledger-cap">'
        '<textarea id="ledger-cap-text" rows="3" '
        'placeholder="Think out loud - a musing, a wondering, a worry. Mention a name and it links itself. '
        '(Cmd/Ctrl+Enter to capture)"></textarea>'
        '<div class="ledger-cap-row">'
        '<button type="button" class="k-btn k-btn-primary k-btn-sm" id="ledger-cap-btn">Capture</button>'
        '<span class="ledger-cap-status" id="ledger-cap-status"></span>'
        "</div></div>" + _CAPTURE_JS
    )


def _musing_card(row: AnalystNoteRow) -> str:
    ctx = row.context or {}
    channel = str(ctx.get("channel") or "")
    chan = f'<span class="ledger-chan">{escape(channel)}</span>' if channel else ""
    when = stamp_html(row.created_at, css="ledger-when")
    if row.ticker:
        ident = ticker_label(row.ticker)
    elif ctx.get("needs_ticker"):
        cands = ctx.get("ticker_candidates")
        names = (
            ", ".join(escape(str(c)) for c in cast("list[object]", cands))
            if isinstance(cands, list)
            else ""
        )
        label = f"needs ticker: {names}" if names else "needs ticker"
        ident = f'<span class="ledger-needs">{label}</span>'
    else:
        ident = '<span class="ledger-unattr">unattributed</span>'
    return (
        '<div class="ledger-musing">'
        f'<div class="ledger-musing-head">{ident}{chan}{when}</div>'
        f'<div class="ledger-body">{render_prose(row.body)}</div>'
        "</div>"
    )


def _stance_card(insight: InsightRow) -> str:
    count = len(insight.source_note_ids)
    plural = "" if count == 1 else "s"
    return (
        '<div class="ledger-stance">'
        '<div class="ledger-stance-head">'
        f"{ticker_label(insight.scope_key)}"
        f'<span class="ledger-stance-meta">from {count} musing{plural}</span></div>'
        f'<div class="ledger-body">{render_prose(insight.body_md)}</div>'
        "</div>"
    )


def _stance_section(db_path: Path | str | None) -> str:
    """The synthesized per-holding stances ("what you think now"), each grounded
    in the musings it cites. Empty until the synthesis stage has run."""
    stances = list_insights(kind="stance", db_path=db_path)
    if not stances:
        return ""
    cards = "".join(_stance_card(s) for s in stances)
    return (
        '<h3 class="ledger-sec-h">What you think now</h3>'
        '<p class="ledger-sec-sub">Your current stance per holding, synthesized from your '
        "musings and grounded in the ones it cites.</p>" + cards
    )


_RESEARCH_VERBS: tuple[tuple[str, str, str], ...] = (
    ("approve", "Approve", "k-btn-primary"),
    ("further", "Research further", ""),
    ("steer", "Steer", ""),
    ("reject", "Reject", "k-btn-danger"),
)

_RESEARCH_JS = """<script>(function(){
  if(window.__ledgerResearchWired){ return; }
  window.__ledgerResearchWired = true;
  function reload(){
    fetch('/api/panel/musings?fragment=research').then(function(r){return r.text();})
      .then(function(h){ var el=document.getElementById('ledger-research'); if(el){ el.outerHTML=h; } });
  }
  document.addEventListener('click', function(e){
    var run=e.target.closest('[data-run-task]');
    if(run){
      run.disabled=true; run.textContent='Researching...';
      fetch('/api/research/task/'+run.getAttribute('data-run-task')+'/run',{method:'POST'})
        .then(function(r){ if(!r.ok){ throw new Error(); } return r.json(); })
        .then(function(){ reload(); })
        .catch(function(){ run.disabled=false; run.textContent='Research it'; });
      return;
    }
    var act=e.target.closest('[data-verb]');
    if(act){
      var verb=act.getAttribute('data-verb'); var body={};
      // One card per RUN: the button acts on every proposal the run drafted.
      var pids=(act.getAttribute('data-pids')||act.getAttribute('data-pid')||'').split(',');
      if(verb==='steer'){ var dir=window.prompt('How should I steer this research?'); if(!dir){ return; } body.steer_text=dir; }
      Promise.all(pids.filter(Boolean).map(function(pid){
        return fetch('/api/research/proposal/'+pid+'/'+verb,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
      })).then(function(){ reload(); });
    }
  });
})();</script>"""


def _task_chip(task: ResearchTask) -> str:
    ident = (
        ticker_label(task.ticker)
        if task.ticker
        else '<span class="ledger-unattr">unattributed</span>'
    )
    if research_run_enabled():
        action = (
            '<button type="button" class="k-btn k-btn-primary k-btn-sm" '
            f'data-run-task="{task.id}">Research it</button>'
        )
    else:
        action = (
            '<span class="ledger-cap-status">detection only — set '
            "LEDGER_RESEARCH_RUN=1 to research</span>"
        )
    return (
        '<div class="ledger-musing">'
        f'<div class="ledger-musing-head">{ident}<span class="ledger-chan">wondering</span></div>'
        f'<div class="ledger-body">{escape(task.claim)}</div>'
        f'<div class="ledger-cap-row">{action}</div></div>'
    )


def _view_words(prop: ResearchProposal) -> tuple[str, str]:
    """A view proposal in the owner's words, rebuilt from the structured spec
    at render time (so pre-fix rows read clean too). Falls back to stored text."""
    try:
        import json as _json

        from research.view_artifact import describe_view_spec

        spec = _json.loads(prop.artifact_json or "{}")
        if spec.get("metrics"):
            return describe_view_spec(cast("dict[str, object]", spec))
    except Exception:
        pass
    return prop.title, prop.body_md


def _proposal_group_card(group: list[ResearchProposal]) -> str:
    """ONE card per research run (task), however many artifacts it drafted.

    The engine emits a memo plus companion artifacts (e.g. a saved-view draft)
    as separate proposal rows; rendering each as its own card with its own
    button row read as a duplicate ("Why is there this duplicate?" —
    2026-07-02). The memo is the card; companions become one compact line
    each; ONE action row acts on every proposal in the group. A view-only
    group speaks the owner's language (no ref grammar) and gets view verbs —
    Save / Discard — not memo verbs."""
    primary = next((p for p in group if p.kind == "memo"), group[0])
    companions = [p for p in group if p.id != primary.id]
    ident = ticker_label(primary.ticker) if primary.ticker else ""
    pids = ",".join(str(p.id) for p in group)
    if primary.kind == "view":
        title, body_text = _view_words(primary)
        body = f"<p>{escape(body_text)}</p>".replace("\n\n", "</p><p>")
        meta = "saved view"
        verbs: tuple[tuple[str, str, str], ...] = (
            ("approve", "Save view", "k-btn-primary"),
            ("reject", "Discard", "k-btn-danger"),
        )
    else:
        title, body = primary.title, render_prose(primary.body_md)
        meta = " · ".join(p for p in (primary.budget_tier, primary.kind) if p)
        verbs = _RESEARCH_VERBS
    footer = "".join(
        f'<button type="button" class="k-btn k-btn-sm {cls}" '
        f'data-verb="{verb}" data-pids="{pids}">{escape(label)}</button>'
        for verb, label, cls in verbs
    )
    rider = "".join(
        f'<p class="ledger-sec-sub">Also drafted: {escape(_view_words(c)[0]) if c.kind == "view" else escape(c.title)} '
        "(approve applies it too).</p>"
        for c in companions
    )
    return (
        '<div class="ledger-stance">'
        '<div class="ledger-stance-head">'
        f'{ident}<span class="ledger-stance-meta">{escape(meta)}</span></div>'
        f'<div class="ledger-body"><strong>{escape(title)}</strong>'
        f"{body}"
        f"{rider}"
        f'<div class="ledger-cap-row">{footer}</div></div>'
    )


def render_ledger_research_list(db_path: Path | str | None) -> str:
    """The research inbox fragment (re-fetched after a run / action): one card
    per RUN (proposals grouped by task), then the open wonderings."""
    proposals = list_proposals(status="pending", db_path=db_path)
    tasks = list_tasks(status="proposed", db_path=db_path)
    if not proposals and not tasks:
        return (
            '<div id="ledger-research"><p class="ledger-empty">No open wonderings or '
            'proposals yet. Capture a wondering — "do NU\'s margins still hold?" — and it '
            "shows up here to research.</p></div>"
        )
    groups: dict[int, list[ResearchProposal]] = {}
    for p in proposals:
        groups.setdefault(p.task_id if p.task_id is not None else -p.id, []).append(p)
    parts: list[str] = []
    if groups:
        parts.append('<h4 class="ledger-sec-h">Proposals to review</h4>')
        parts.append("".join(_proposal_group_card(g) for g in groups.values()))
    if tasks:
        parts.append('<h4 class="ledger-sec-h">Open wonderings</h4>')
        parts.append("".join(_task_chip(t) for t in tasks))
    return f'<div id="ledger-research">{"".join(parts)}</div>'


def _tap_health_line(db_path: Path | str | None) -> str:
    """One muted line of tap liveness — distinguishes 'no wonderings lately'
    from 'the tap is broken/dormant' (they were indistinguishable before)."""
    try:
        from capture.audit import recent_tap_counts

        c = recent_tap_counts(days=7, db_path=db_path)
    except Exception:
        return ""
    total = sum(c.values())
    if total == 0:
        return (
            '<p class="ledger-sec-sub">Tap health (7d): no musings tapped — '
            "capture something and this line should move.</p>"
        )
    bits = [f"{total} tapped", f"{c['chip']} chips"]
    filtered = c["regex"] + c["trust_zone"]
    if filtered:
        bits.append(f"{filtered} pre-gate filtered")
    if c["llm_no"]:
        bits.append(f"{c['llm_no']} classifier-no")
    if c["error"]:
        bits.append(f"{c['error']} errors")
    return f'<p class="ledger-sec-sub">Tap health (7d): {" · ".join(bits)}.</p>'


def _research_section(db_path: Path | str | None) -> str:
    return (
        '<h3 class="ledger-sec-h">Research</h3>'
        '<p class="ledger-sec-sub">Wonderings I detected in your musings, and the inert '
        "proposals they produced — approve, dig further, steer, or reject. Nothing acts "
        "until you say so.</p>"
        + _tap_health_line(db_path)
        + render_ledger_research_list(db_path)
        + _RESEARCH_JS
    )


_RECONCILE_VERDICTS: tuple[tuple[str, str, str], ...] = (
    ("live", "Still live", "k-btn-primary"),
    ("superseded", "Superseded", ""),
    ("resolved-rejected", "Rejected", "k-btn-danger"),
    ("done", "Played out", ""),
)

_RECONCILE_JS = """<script>(function(){
  if(window.__ledgerReconcileWired){ return; }
  window.__ledgerReconcileWired = true;
  function reload(){
    fetch('/api/panel/musings?fragment=reconcile').then(function(r){return r.text();})
      .then(function(h){ var el=document.getElementById('ledger-reconcile'); if(el){ el.outerHTML=h; } });
  }
  document.addEventListener('click', function(e){
    var v=e.target.closest('[data-rec-verdict]');
    if(v){
      fetch('/api/reconcile/'+v.getAttribute('data-rec-kind')+'/'+v.getAttribute('data-rec-id')
            +'/'+v.getAttribute('data-rec-verdict'),{method:'POST'})
        .then(function(){ reload(); });
      return;
    }
    var f=e.target.closest('[data-falsifier-action]');
    if(f){
      var action=f.getAttribute('data-falsifier-action'); var body={action:action};
      if(action==='edit'){
        var txt=window.prompt('Your falsifier, in your own words:');
        if(!txt){ return; } body.text=txt;
      }
      fetch('/api/reconcile/falsifier/'+f.getAttribute('data-rec-id'),
            {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)})
        .then(function(){ reload(); });
    }
  });
})();</script>"""


def _missing_falsifier_line(db_path: Path | str | None) -> str:
    """One dense line for live held positions with no falsifier — no tripwire
    coverage is an irreducible owner ask; 'add' routes to the same
    /api/reconcile/falsifier/<id> edit action the ratify queue uses."""
    from synthesis.reconcile import list_missing_falsifiers

    try:
        gaps = list_missing_falsifiers(db_path)
    except Exception:
        gaps = []
    if not gaps:
        return ""
    asks = " · ".join(
        f"{escape(gap.label)} — "
        f'<button type="button" class="k-btn k-btn-sm k-btn-primary" '
        f'data-falsifier-action="edit" data-rec-id="{gap.item_id}">add</button>'
        for gap in gaps
    )
    lead = (
        "1 live decision needs a falsifier"
        if len(gaps) == 1
        else f"{len(gaps)} live decisions need a falsifier"
    )
    return f'<div class="ledger-musing"><div class="ledger-body">{lead}: {asks}</div></div>'


def render_reconcile_list(db_path: Path | str | None) -> str:
    """The seed-reconciliation fragment — one-tap verdicts until the list is empty.
    Degrades to the empty state on a pre-0130 DB (no decided_by column yet)."""
    from synthesis.reconcile import list_unreconciled

    try:
        items = list_unreconciled(db_path)
    except Exception:
        items = []
    missing_line = _missing_falsifier_line(db_path)
    if not items and not missing_line:
        return (
            '<div id="ledger-reconcile"><p class="ledger-empty">Corpus reconciled — '
            "nothing awaiting a verdict.</p></div>"
        )
    cards: list[str] = [missing_line] if missing_line else []
    for item in items:
        if item.kind == "falsifier":
            buttons = "".join(
                f'<button type="button" class="k-btn k-btn-sm {cls}" '
                f'data-falsifier-action="{action}" data-rec-id="{item.item_id}">{label}</button>'
                for action, label, cls in (
                    ("ratify", "Ratify as mine", "k-btn-primary"),
                    ("edit", "Rewrite", ""),
                    ("drop", "Drop", "k-btn-danger"),
                )
            )
            head = f"{escape(item.label)}<span class='ledger-chan'>inferred falsifier</span>"
        else:
            rec_kind = "note" if item.kind == "note" else "theme"
            buttons = "".join(
                f'<button type="button" class="k-btn k-btn-sm {cls}" '
                f'data-rec-kind="{rec_kind}" data-rec-id="{item.item_id}" '
                f'data-rec-verdict="{verdict}">{label}</button>'
                for verdict, label, cls in _RECONCILE_VERDICTS
            )
            tag = item.label if item.kind == "theme" else (item.source_ref or item.label)
            head = f"<span class='ledger-chan'>{escape(tag)}</span>"
        cards.append(
            '<div class="ledger-musing">'
            f'<div class="ledger-musing-head">{head}</div>'
            f'<div class="ledger-body">{escape(item.body[:400])}</div>'
            f'<div class="ledger-cap-row">{buttons}</div></div>'
        )
    return f'<div id="ledger-reconcile">{"".join(cards)}</div>'


def _auto_reconcile_line(db_path: Path | str | None) -> str:
    """The 'derive, don't ask' receipt: what software already resolved, so the
    queue only ever shows the irreducible owner-only residue."""
    try:
        from synthesis.auto_reconcile import auto_reconciled_summary

        c = auto_reconciled_summary(db_path)
    except Exception:
        return ""
    total = sum(c.values())
    if total == 0:
        return ""
    return (
        f'<p class="ledger-sec-sub">Auto-resolved {total} for you: '
        f"{c['auto_done']} played out · {c['auto_live']} kept live · "
        f"{c['auto_dropped']} moot falsifiers dropped (positions closed).</p>"
    )


def _reconcile_section(db_path: Path | str | None) -> str:
    items = render_reconcile_list(db_path)
    auto_line = _auto_reconcile_line(db_path)
    if "ledger-empty" in items and auto_line:
        # Nothing needs the owner — one receipt line, no section ceremony.
        return f'<h3 class="ledger-sec-h">Reconcile</h3>{auto_line}'
    return (
        '<h3 class="ledger-sec-h">Reconcile</h3>'
        '<p class="ledger-sec-sub">Only what genuinely needs you — falsifiers I would '
        "quote back at you must be in your own words.</p>" + auto_line + items + _RECONCILE_JS
    )


# ---------------------------------------------------------------------------
# On My Mind — the reverse-chron capture feed (P1). Absorbs the Wondering flag as
# an inline badge; carries the dismiss/save/discuss/incorporate ladder. Behind
# LEDGER_ONMYMIND (default off). Additive: it does not touch the Research /
# Reconcile / stance sections the parallel session owns.
# ---------------------------------------------------------------------------

_ONMYMIND_STYLE = """<style>
.om-type { font-size: var(--fs-micro); color: var(--muted); text-transform: uppercase; letter-spacing: 0.05em; }
.om-wondering { font-size: var(--fs-micro); font-weight: 600; color: var(--warn); text-transform: uppercase; letter-spacing: 0.05em; }
.om-ladder { font-size: var(--fs-micro); font-weight: 600; color: var(--accent); text-transform: uppercase; letter-spacing: 0.05em; }
.om-ladder:empty { display: none; }
.om-actions { flex-wrap: wrap; }
.om-body a { overflow-wrap: anywhere; }
#onmymind-more { margin-top: var(--sp-2); }
</style>"""

_ONMYMIND_JS = """<script>(function(){
  if(window.__onMyMindWired){ return; }
  window.__onMyMindWired = true;
  document.addEventListener('click', function(e){
    var act=e.target.closest('[data-om-verb]');
    if(act){
      var card=act.closest('[data-om-id]'); if(!card){ return; }
      var id=card.getAttribute('data-om-id'); var verb=act.getAttribute('data-om-verb');
      act.disabled=true;
      fetch('/api/onmymind/'+id+'/'+verb,{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'})
        .then(function(r){ return r.json(); })
        .then(function(res){
          if(!res || res.ok===false){ act.disabled=false; return; }
          if(res.removed){ if(card.parentNode){ card.parentNode.removeChild(card); } return; }
          var badge=card.querySelector('.om-ladder');
          if(badge){ badge.textContent=res.ladder_label||''; }
          if(res.thread_url){ window.open(res.thread_url,'_blank','noopener'); }
        })
        .catch(function(){ act.disabled=false; });
      return;
    }
    var more=e.target.closest('[data-om-more]');
    if(more){
      var cur=more.getAttribute('data-om-more'); if(!cur){ return; }
      more.disabled=true;
      fetch('/api/panel/musings?fragment=onmymind&cursor='+encodeURIComponent(cur))
        .then(function(r){ return r.text(); })
        .then(function(h){ var el=document.getElementById('onmymind-more'); if(el){ el.outerHTML=h; } })
        .catch(function(){ more.disabled=false; });
    }
  });
})();</script>"""

# (verb, label, button-class) — the ladder rungs, incorporate-first (the payoff
# action), dismiss last (destructive, danger-styled).
_LADDER_BUTTONS: tuple[tuple[str, str, str], ...] = (
    ("incorporate", "Incorporate", "k-btn-primary"),
    ("discuss", "Discuss", ""),
    ("save", "Save for later", ""),
    ("dismiss", "Dismiss", "k-btn-danger"),
)


def _feed_body(item: FeedItem) -> str:
    """A musing renders as prose; a reading renders by its shape — a link as an
    anchor (owner-provided URL, opened in a new tab), a doc as its filename +
    caption."""
    note = item.note
    ctx = note.context or {}
    if item.item_type == "link":
        url = str(ctx.get("url") or note.body)
        return (
            f'<a href="{escape(url, quote=True)}" target="_blank" '
            f'rel="noopener noreferrer">{escape(url)}</a>'
        )
    if item.item_type == "doc":
        name = str(ctx.get("file_name") or note.body)
        caption = str(ctx.get("caption") or "")
        head = f"<strong>{escape(name)}</strong>"
        return head + (f"<div>{escape(caption)}</div>" if caption else "")
    return render_prose(note.body)


def _feed_card(item: FeedItem) -> str:
    note = item.note
    ctx = note.context or {}
    if note.ticker:
        ident = ticker_label(note.ticker)
    elif item.item_type in ("doc", "link"):
        ident = '<span class="ledger-unattr">reading</span>'
    else:
        ident = '<span class="ledger-unattr">unattributed</span>'
    if item.item_type == "musing":
        channel = str(ctx.get("channel") or "")
        type_chip = f'<span class="ledger-chan">{escape(channel)}</span>' if channel else ""
    else:
        type_chip = f'<span class="om-type">{escape(item.item_type)}</span>'
    wondering = '<span class="om-wondering">wondering</span>' if item.wondering else ""
    ladder_label = LADDER_LABELS.get(item.ladder or "", "")
    ladder_badge = f'<span class="om-ladder">{escape(ladder_label)}</span>'
    when = stamp_html(note.created_at, css="ledger-when")
    buttons = "".join(
        f'<button type="button" class="k-btn k-btn-sm {cls}" data-om-verb="{verb}">{escape(label)}</button>'
        for verb, label, cls in _LADDER_BUTTONS
    )
    return (
        f'<div class="ledger-musing om-item" data-om-id="{note.id}">'
        f'<div class="ledger-musing-head">{ident}{type_chip}{wondering}{ladder_badge}{when}</div>'
        f'<div class="ledger-body om-body">{_feed_body(item)}</div>'
        f'<div class="ledger-cap-row om-actions">{buttons}</div>'
        "</div>"
    )


def _more_div(next_cursor: str | None) -> str:
    """The keyset 'Load more' control (empty terminal div on the last page)."""
    if not next_cursor:
        return '<div id="onmymind-more"></div>'
    return (
        '<div id="onmymind-more">'
        '<button type="button" class="k-btn k-btn-sm" '
        f'data-om-more="{escape(next_cursor, quote=True)}">Load more</button></div>'
    )


def render_onmymind_list(
    db_path: Path | str | None,
    *,
    cursor: str | None = None,
    user_id: str = DEFAULT_USER_ID,
) -> str:
    """One keyset page of feed cards + the 'Load more' control. ``cursor=None`` is
    the first page (rendered inside ``#onmymind-list``); a cursor is a subsequent
    page whose HTML replaces the current ``#onmymind-more`` in place."""
    page = load_feed(cursor=cursor, user_id=user_id, db_path=db_path)
    if not page.items and cursor is None:
        return (
            '<p class="ledger-empty">Nothing on your mind yet — capture a thought, '
            "or send a reading (a link, a deck) to your Telegram bot.</p>"
        )
    cards = "".join(_feed_card(i) for i in page.items)
    return cards + _more_div(page.next_cursor)


def _onmymind_section(db_path: Path | str | None, *, user_id: str = DEFAULT_USER_ID) -> str:
    """The On My Mind feed section — empty string when the flag is off (the panel
    then keeps its plain Musings list unchanged)."""
    if not onmymind_enabled():
        return ""
    return (
        _ONMYMIND_STYLE + '<h3 class="ledger-sec-h">On My Mind</h3>'
        '<p class="ledger-sec-sub">What you\'re thinking about and reading, newest first. '
        "Dismiss it, save it for later, talk it through, or send it into research.</p>"
        '<div id="onmymind-list">'
        + render_onmymind_list(db_path, user_id=user_id)
        + "</div>"
        + _ONMYMIND_JS
    )


def render_ledger_list(db_path: Path | str | None, *, user_id: str = DEFAULT_USER_ID) -> str:
    """The musings list fragment (re-fetched after a capture)."""
    rows = list_notes(user_id=user_id, kind="musing", db_path=db_path, limit=200)
    if not rows:
        return (
            '<div id="ledger-list"><p class="ledger-empty">No musings yet - capture a '
            "thought above, or send one (voice or text) to your Telegram bot.</p></div>"
        )
    body = "".join(_musing_card(r) for r in rows)
    return f'<div id="ledger-list">{body}</div>'


def render_ledger_panel(db_path: Path | str | None, *, user_id: str = DEFAULT_USER_ID) -> str:
    """The Ledger tab: capture box + newest-first musings.

    When ``LEDGER_ONMYMIND`` is on, the On My Mind feed (musings + readings, with
    the action ladder) is the front-of-funnel section and the plain Musings list is
    suppressed — On My Mind subsumes it. Off, the panel is unchanged.
    """
    onmymind = _onmymind_section(db_path, user_id=user_id)
    # On My Mind is the broader feed (readings too, + the ladder); when it's live
    # the plain musings list below would just duplicate it, so drop it.
    musings_block = (
        ""
        if onmymind
        else '<h3 class="ledger-sec-h">Musings</h3>' + render_ledger_list(db_path, user_id=user_id)
    )
    return (
        _PANEL_STYLE + '<section class="panel"><h2>Ledger</h2>'
        '<p class="sub">Your captured stream of consciousness. Talk or type a musing - '
        "to your Telegram bot on the go, or here at the desk; it lands linked to a name "
        "and you read it back below.</p>"
        + _capture_box()
        + onmymind
        + render_worldview_section(db_path)
        + _stance_section(db_path)
        + _research_section(db_path)
        + _reconcile_section(db_path)
        + musings_block
        + "</section>"
    )
