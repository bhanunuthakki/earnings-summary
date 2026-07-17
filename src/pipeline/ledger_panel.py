"""The Ledger flat musings panel — read your captured stream of consciousness.

Wave A (the dogfood read-back): a newest-first list of captured musings
(``kind='musing'``, ``source='capture'``) plus an at-desk quick-capture box that
POSTs to ``/api/capture/text``. The Telegram bot is the primary, on-the-go mouth;
this panel is the desk mouth + the read-back that proves capture earns its keep.
Theme synthesis + FTS search land in Wave B.

Pure read over the analyst_notes spine; token-only styles (guard-clean).
"""

from __future__ import annotations

import re
from html import escape
from pathlib import Path
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from synthesis.reconcile import ReconcileItem

from identity import DEFAULT_USER_ID
from onmymind.feed import (
    LADDER_LABELS,
    FeedItem,
    load_feed,
    onmymind_enabled,
)
from onmymind.respond import is_answerable_capture
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

# The Portfolio > Decisions panel hash — interpolated rather than written as
# one '#decisions...' literal: the token guard's hex scan reads '#dec' as a
# raw color (tests/test_ui_controls.py scans every value literal in a
# CSS-emitting module). See src/pipeline/open_loops.py _DECISIONS_HASH for
# the same idiom.
_DECISIONS_PANEL = "decisions_record"
_DECISIONS_HASH = f"/#{_DECISIONS_PANEL}"

_PANEL_STYLE = """<style>
.ledger-cap { background: var(--surface); border-radius: var(--radius); padding: var(--sp-2) var(--sp-3); margin-bottom: var(--sp-3); }
.ledger-cap textarea { width: 100%; min-height: 44px; resize: vertical; font-family: var(--sans); font-size: var(--fs-body); }
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
.ledger-stance { background: var(--surface); border-left: 3px solid var(--border-2); border-radius: var(--radius); padding: var(--sp-3) var(--sp-4); margin-bottom: var(--sp-2); }
.ledger-stance-head { display: flex; align-items: baseline; gap: var(--sp-2); margin-bottom: var(--sp-1); }
.ledger-stance-meta { color: var(--muted); font-size: var(--fs-micro); margin-left: auto; }
.ledger-coach-card { background: var(--surface); border-left: 3px solid var(--border-2); border-radius: var(--radius); padding: var(--sp-3) var(--sp-4); margin-bottom: var(--sp-2); position: relative; }
.ledger-coach-body { font-size: var(--fs-body); line-height: 1.55; color: var(--fg-soft); white-space: normal; }
.ledger-coach-row { display: flex; align-items: center; gap: var(--sp-2); margin-top: var(--sp-2); }
.ledger-coach-row input { flex: 1; font-family: var(--sans); font-size: var(--fs-body); }
.ledger-coach-x { position: absolute; top: var(--sp-2); right: var(--sp-2); }
.ledger-coach-receipt { color: var(--fg-soft); font-size: var(--fs-caption); }
/* Ratify receipt (consequence receipts PR) — a transient one-line notice
   above the Reconcile list; a sibling of #ledger-reconcile so the fragment
   reload's outerHTML swap never clobbers it before it's read. */
.ledger-receipt { color: var(--fg-soft); font-size: var(--fs-caption);
  padding: var(--sp-2) 0; }
/* Armed-falsifiers table — dense, token-only; no new color intent beyond
   the existing muted/fg vocabulary. */
.ledger-armed-h { font-size: var(--fs-caption); font-weight: 600; color: var(--fg);
  margin: var(--sp-3) 0 var(--sp-1); text-transform: uppercase; letter-spacing: 0.05em; }
.ledger-armed-table { width: 100%; border-collapse: collapse; font-size: var(--fs-caption); }
.ledger-armed-table th { text-align: left; color: var(--muted); font-weight: 600;
  padding: var(--sp-1) var(--sp-2); border-bottom: 1px solid var(--border); }
.ledger-armed-table td { padding: var(--sp-1) var(--sp-2); border-bottom: 1px solid var(--hairline); }
.ledger-armed-ticker { font-family: var(--mono); font-weight: 600; }
.ledger-armed-since { color: var(--muted); white-space: nowrap; }
.ledger-armed-num a { color: var(--muted); text-decoration: none; }
.ledger-armed-num a:hover { color: var(--accent); }
/* Jump-chip toolbar (PR9) — mirrors the Provenance console's anchor-nav band;
   one operating row above the sections, wraps on narrow widths. */
.ledger-jump-toolbar { display: flex; flex-wrap: wrap; gap: var(--sp-2); margin-bottom: var(--sp-4); }
/* Set-ticker chips (PR9) — the needs_ticker musing card's one-tap attribution
   row; reuses .ledger-cap-row's flex layout via the extra class below. */
.ledger-set-ticker { flex-wrap: wrap; }
/* In-card Rewrite / Steer textareas (PR9, replaces window.prompt) — same
   sizing family as the capture box's own textarea. */
.ledger-rewrite-ta, .ledger-steer-ta { width: 100%; min-height: 56px; resize: vertical;
  font-family: var(--sans); font-size: var(--fs-body); margin-bottom: var(--sp-2); }
/* Queues (overhaul P4): the four machinery sections (research / reconcile /
   worldview / stances) collapse into ONE block below the feed, so the tab reads
   conversation-first. Closed by default; a count on the summary surfaces pending
   work without re-inflating the wall of sections. Token-only. */
.ledger-queues { margin-top: var(--sp-4); border-top: 1px solid var(--border); }
.ledger-queues-sum { cursor: pointer; padding: var(--sp-3) 0; font-size: var(--fs-section); font-weight: 600; color: var(--fg); list-style: none; display: flex; align-items: baseline; gap: var(--sp-2); }
.ledger-queues-sum::-webkit-details-marker { display: none; }
.ledger-queues-sum::before { content: "\\25B8"; color: var(--muted); font-size: var(--fs-caption); }
.ledger-queues[open] .ledger-queues-sum::before { content: "\\25BE"; }
.ledger-queues-hint { font-size: var(--fs-caption); font-weight: 400; color: var(--muted); }
.ledger-queues-count { font-size: var(--fs-caption); font-weight: 600; color: var(--accent); }
.ledger-queues-body { padding-top: var(--sp-2); }
</style>"""

_CAPTURE_JS = """<script>(function(){
  var btn=document.getElementById('ledger-cap-btn');
  var ta=document.getElementById('ledger-cap-text');
  var st=document.getElementById('ledger-cap-status');
  var coach=document.getElementById('ledger-cap-coach');
  if(!btn||!ta){ return; }

  function esc(s){
    return String(s==null?'':s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
  }
  // Plain-text challenge/receipt render: escape, then newlines -> <br> only
  // (no markdown). DUPLICATED (not shared) in command_center_shell.py SHELL_JS
  // trayRenderCoach() per the repo's duplicate-simple-shared-logic preference
  // — see feedback_duplicate_simple_shared_logic.md.
  function renderCoach(res){
    if(!coach){ return; }
    if(res && res.pledge_challenge){
      var body=esc(res.pledge_challenge).replace(/\\n/g,'<br>');
      coach.innerHTML =
        '<div class="ledger-coach-card">'
        +'<button type="button" class="k-btn k-btn-sm k-btn-quiet ledger-coach-x" '
        +'data-coach-dismiss title="dismiss">&times;</button>'
        +'<div class="ledger-coach-body">'+body+'</div>'
        +'<div class="ledger-coach-row">'
        +'<input type="text" id="ledger-coach-annotate" '
        +'placeholder="conviction + falsifier \\u2014 one line completes the record">'
        +'<button type="button" class="k-btn k-btn-primary k-btn-sm" id="ledger-coach-send">Send</button>'
        +'</div></div>';
      var sendBtn=document.getElementById('ledger-coach-send');
      var input=document.getElementById('ledger-coach-annotate');
      if(sendBtn && input){
        sendBtn.addEventListener('click', function(){
          var note=(input.value||'').trim();
          if(!note){ input.focus(); return; }
          sendBtn.disabled=true;
          fetch('/api/capture/text',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({text:note})})
            .then(function(r){ return r.json(); })
            .then(function(res2){ renderCoach(res2); })
            .catch(function(){ sendBtn.disabled=false; });
        });
      }
      return;
    }
    if(res && res.annotated_decision_id){
      coach.innerHTML =
        '<div class="ledger-coach-card">'
        +'<button type="button" class="k-btn k-btn-sm k-btn-quiet ledger-coach-x" '
        +'data-coach-dismiss title="dismiss">&times;</button>'
        +'<div class="ledger-coach-receipt">Noted \\u2014 recorded on decision '
        +'<a href="__DECISIONS_HASH__">#' + esc(res.annotated_decision_id) + '</a></div></div>';
      return;
    }
    coach.innerHTML='';
  }
  if(coach){
    coach.addEventListener('click', function(e){
      if(e.target.closest('[data-coach-dismiss]')){
        coach.innerHTML='';
      }
    });
  }

  // Patch one feed card in place from the server (fragment=card) — used to
  // paint the freshly-captured card and, later, to swap the "Answering..."
  // block for the stored answer once the background answer lands.
  function patchCard(id){
    fetch('/api/panel/musings?fragment=card&note='+encodeURIComponent(id))
      .then(function(r){ return r.ok ? r.text() : ''; })
      .then(function(h){
        var cur=document.getElementById('om-note-'+id);
        if(cur && h){ cur.outerHTML=h; }
      });
  }
  // The answer is generated on a background thread server-side (the capture
  // POST returns immediately); poll the note until the answer (or a cleared
  // pending flag) arrives, then repaint just that card. ~60s cap.
  function pollAnswer(id, n){
    if(n>24){ patchCard(id); return; }
    setTimeout(function(){
      fetch('/api/onmymind/'+id+'/answer')
        .then(function(r){ return r.json(); })
        .then(function(res){
          if(res && res.pending && !res.answer){ pollAnswer(id, n+1); return; }
          patchCard(id);
          if(st && res && res.answer){ st.textContent='Answered'; setTimeout(function(){ st.textContent=''; },4000); }
        })
        .catch(function(){ pollAnswer(id, n+1); });
    }, 2500);
  }
  function send(){
    var text=(ta.value||'').trim();
    if(!text){ ta.focus(); return; }
    btn.disabled=true; if(st){ st.textContent='Capturing...'; }
    fetch('/api/capture/text',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({text:text})})
      .then(function(r){ return r.json(); })
      .then(function(res){
        ta.value='';
        if(res && (res.pledge_challenge || res.annotated_decision_id)){
          renderCoach(res);
        } else if(res && (res.answer || res.answering)){
          if(st){ st.textContent=res.answer?'Answered':'Captured - answering...'; }
        } else {
          var tag = (res && res.ticker) ? (' - '+res.ticker) : ((res && res.needs_ticker) ? ' - needs ticker' : '');
          if(st){ st.textContent='Captured'+tag; }
        }
        // Prepend just the new card (fragment=card) instead of repainting the
        // whole list — a full repaint would destroy any open inline chat on
        // the cards below. Legacy (flag-off) list keeps its full refresh.
        var om=document.getElementById('onmymind-list');
        if(om && res && res.note_id){
          fetch('/api/panel/musings?fragment=card&note='+encodeURIComponent(res.note_id))
            .then(function(r){ return r.ok ? r.text() : ''; })
            .then(function(h){
              if(h){
                var empty=om.querySelector('.ledger-empty');
                if(empty){ empty.remove(); }
                om.insertAdjacentHTML('afterbegin', h);
              } else {
                fetch('/api/panel/musings?fragment=onmymind').then(function(r){return r.text();}).then(function(h2){ om.innerHTML=h2; });
              }
              if(res.answering){ pollAnswer(res.note_id, 0); }
            });
          return;
        }
        if(om){ fetch('/api/panel/musings?fragment=onmymind').then(function(r){return r.text();}).then(function(h){ om.innerHTML=h; }); return; }
        var list=document.getElementById('ledger-list');
        if(list){ fetch('/api/panel/musings?fragment=list').then(function(r){return r.text();}).then(function(h){ list.innerHTML=h; }); }
      })
      .catch(function(){ if(st){ st.textContent='Could not reach the server - try again.'; } })
      .finally(function(){ btn.disabled=false; setTimeout(function(){ if(st){ st.textContent=''; } },4000); });
  }
  btn.addEventListener('click', send);
  ta.addEventListener('keydown', function(e){ if((e.metaKey||e.ctrlKey) && e.key==='Enter'){ send(); } });
})();</script>"""
# The receipt link's href is spliced in via .replace() on a plain placeholder
# token, not an f-string brace hole — _CAPTURE_JS is one big JS block and
# f-string-escaping every JS {..} would be fragile. Two ordinary string
# literals join at import time, same spirit as open_loops.py's interpolated
# _DECISIONS_HASH (never a single '#dec...' literal for the hex-scan guard).
_CAPTURE_JS = _CAPTURE_JS.replace("__DECISIONS_HASH__", _DECISIONS_HASH)


def _capture_box() -> str:
    return (
        '<div class="ledger-cap">'
        '<textarea id="ledger-cap-text" rows="2" '
        'placeholder="Think out loud - a musing, a wondering, a worry. Mention a name and it links itself. '
        '(Cmd/Ctrl+Enter to capture)"></textarea>'
        '<div class="ledger-cap-row">'
        '<button type="button" class="k-btn k-btn-primary k-btn-sm" id="ledger-cap-btn">Capture</button>'
        '<span class="ledger-cap-status" id="ledger-cap-status"></span>'
        "</div></div>"
        # Mounted between the cap row and the list (W2 spec): the entry-coach
        # card (pledge_challenge) or the annotation receipt renders here,
        # empty otherwise.
        '<div id="ledger-cap-coach"></div>' + _CAPTURE_JS
    )


def _ticker_candidate_chips(cands: object) -> str:
    """One-click set-ticker chips (PR9) for a ``needs_ticker`` musing — each
    button POSTs the new ``set_ticker`` lifecycle action for the one candidate
    it names. Empty string when there are no candidates to offer (the plain
    'needs ticker' badge is the only ident in that case)."""
    if not isinstance(cands, list):
        return ""
    tickers = [str(c).strip().upper() for c in cast("list[object]", cands) if str(c).strip()]
    if not tickers:
        return ""
    buttons = "".join(
        f'<button type="button" class="k-chip k-chip-btn" '
        f'data-set-ticker="{escape(t)}">{escape(t)}</button>'
        for t in tickers
    )
    return f'<div class="ledger-cap-row ledger-set-ticker">{buttons}</div>'


def _musing_card(row: AnalystNoteRow) -> str:
    ctx = row.context or {}
    channel = str(ctx.get("channel") or "")
    chan = f'<span class="ledger-chan">{escape(channel)}</span>' if channel else ""
    when = stamp_html(row.created_at, css="ledger-when")
    chips = ""
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
        chips = _ticker_candidate_chips(cands)
    else:
        ident = '<span class="ledger-unattr">unattributed</span>'
    return (
        f'<div class="ledger-musing" data-note-id="{row.id}">'
        f'<div class="ledger-musing-head">{ident}{chan}{when}</div>'
        f'<div class="ledger-body">{render_prose(row.body)}</div>'
        f"{chips}"
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
  // The run endpoint returns immediately ({started:true}) and researches on a
  // server thread — poll the task's status until it leaves 'running'. A
  // finished run shows up in the reloaded fragment; a revert to 'proposed'
  // means the run failed and was rolled back (retryable).
  function pollRun(tid, btn, n){
    if(n>120){ btn.textContent='Still running - check back'; return; }
    setTimeout(function(){
      fetch('/api/research/task/'+tid+'/status')
        .then(function(r){ if(!r.ok){ throw new Error(); } return r.json(); })
        .then(function(res){
          var s=res && res.status;
          if(s==='running'){ pollRun(tid, btn, n+1); return; }
          if(s==='proposed'){
            btn.disabled=false; btn.textContent='Research it';
            btn.title='The run failed and was rolled back - tap to retry.';
            return;
          }
          reload();
        })
        .catch(function(){ pollRun(tid, btn, n+1); });
    }, 5000);
  }
  function rowButtons(el){
    var row=el.closest('.ledger-cap-row');
    return row ? row.querySelectorAll('button') : [el];
  }
  function setRow(el, disabled){
    var btns=rowButtons(el);
    for(var i=0;i<btns.length;i++){ btns[i].disabled=disabled; }
  }
  document.addEventListener('click', function(e){
    var run=e.target.closest('[data-run-task]');
    if(run){
      var tid=run.getAttribute('data-run-task');
      run.disabled=true; run.textContent='Researching...';
      fetch('/api/research/task/'+tid+'/run',{method:'POST'})
        .then(function(r){ if(!r.ok){ throw new Error(); } return r.json(); })
        .then(function(){ pollRun(tid, run, 0); })
        .catch(function(){ run.disabled=false; run.textContent='Research it'; });
      return;
    }
    var rej=e.target.closest('[data-reject-task]');
    if(rej){
      rej.disabled=true; rej.textContent='Dismissing...';
      fetch('/api/research/task/'+rej.getAttribute('data-reject-task')+'/reject',{method:'POST'})
        .then(function(r){ if(!r.ok){ throw new Error(); } return r.json(); })
        .then(function(){ reload(); })
        .catch(function(){ rej.disabled=false; rej.textContent='Dismiss'; });
      return;
    }
    var act=e.target.closest('[data-verb]');
    if(act){
      var verb=act.getAttribute('data-verb');
      // One card per RUN: the button acts on every proposal the run drafted.
      var pids=(act.getAttribute('data-pids')||act.getAttribute('data-pid')||'').split(',');
      if(verb==='steer'){ beginSteer(act, pids); return; }
      setRow(act, true);
      send(pids, verb, {}, act);
      return;
    }
    // Backlink from a research item to the musing that spawned it (owner
    // feedback 2026-07-14: "no apparent link to the musing"). Router-safe
    // scroll (no hash change) + a brief highlight; no-op if the note is on
    // another page of the feed.
    var back=e.target.closest('[data-goto-note]');
    if(back){
      e.preventDefault();
      var card=document.getElementById('om-note-'+back.getAttribute('data-goto-note'));
      if(card){
        card.scrollIntoView({behavior:'smooth', block:'center'});
        card.classList.add('om-flash');
        setTimeout(function(){ card.classList.remove('om-flash'); }, 1600);
      }
    }
  });
  function send(pids, verb, body, src){
    Promise.all(pids.filter(Boolean).map(function(pid){
      return fetch('/api/research/proposal/'+pid+'/'+verb,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
    })).then(function(){ reload(); })
      .catch(function(){ if(src){ setRow(src, false); } });
  }
  // In-card Steer (PR9, replaces window.prompt): the steering direction is an
  // owner utterance — it gets a real textarea inside the proposal card (the
  // Rewrite in-card-swap idiom), never a single-line unstyled OS modal.
  function beginSteer(act, pids){
    var card=act.closest('[data-prop-card]');
    if(!card || card.getAttribute('data-editing')==='1'){ return; }
    card.setAttribute('data-editing','1');
    var row=act.closest('.ledger-cap-row');
    var ed=document.createElement('div');
    ed.innerHTML=
      '<textarea class="ledger-rewrite-ta" rows="2" '
      +'placeholder="How should I steer this research?"></textarea>'
      +'<div class="ledger-cap-row">'
      +'<button type="button" class="k-btn k-btn-primary k-btn-sm" data-steer-save>Steer</button>'
      +'<button type="button" class="k-btn k-btn-quiet k-btn-sm" data-steer-cancel>Cancel</button>'
      +'</div>';
    row.parentNode.insertBefore(ed, row.nextSibling);
    var ta=ed.querySelector('.ledger-rewrite-ta');
    if(ta){ ta.focus(); }
    ed.querySelector('[data-steer-cancel]').addEventListener('click', function(){
      ed.remove(); card.removeAttribute('data-editing');
    });
    ed.querySelector('[data-steer-save]').addEventListener('click', function(){
      var dir=(ta&&ta.value||'').trim();
      if(!dir){ if(ta){ ta.focus(); } return; }
      this.disabled=true;
      send(pids, 'steer', {steer_text: dir}, this);
    });
  }
})();</script>"""


def _task_chip(task: ResearchTask) -> str:
    ident = (
        ticker_label(task.ticker)
        if task.ticker
        else '<span class="ledger-unattr">unattributed</span>'
    )
    dismiss = (
        '<button type="button" class="k-btn k-btn-danger k-btn-sm" '
        f'data-reject-task="{task.id}">Dismiss</button>'
    )
    # The "runs are off" state is explained ONCE at the section level (see
    # _research_section's owner-voice muted line) — no per-card env-var leak.
    if research_run_enabled():
        action = (
            '<button type="button" class="k-btn k-btn-primary k-btn-sm" '
            f'data-run-task="{task.id}">Research it</button>' + dismiss
        )
    else:
        action = dismiss
    backlink = (
        '<button type="button" class="k-chip k-chip-btn ledger-backlink" '
        f'data-goto-note="{task.note_id}" title="Jump to the note that raised this">'
        "↩ from your note</button>"
        if task.note_id
        else ""
    )
    return (
        '<div class="ledger-musing">'
        f'<div class="ledger-musing-head">{ident}<span class="ledger-chan">wondering</span>{backlink}</div>'
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
    # Doorway back to the musing that seeded this run (owner feedback 2026-07-14).
    # Direct attribute read: ResearchProposal.source_note_ids is a real field
    # now (the old getattr always returned None — the column was never mapped,
    # so the backlink was dead by construction).
    source_ids = primary.source_note_ids
    first_note = source_ids[0] if source_ids else None
    backlink = (
        '<button type="button" class="k-chip k-chip-btn ledger-backlink" '
        f'data-goto-note="{first_note}" title="Jump to the note that seeded this">'
        "↩ from your note</button>"
        if first_note
        else ""
    )
    return (
        f'<div class="ledger-stance" data-prop-card="{pids}">'
        '<div class="ledger-stance-head">'
        f'{ident}<span class="ledger-stance-meta">{escape(meta)}</span>{backlink}</div>'
        f'<div class="ledger-body ledger-editable-body"><strong>{escape(title)}</strong>'
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
    if c["engage"]:
        bits.append(f"{c['engage']} briefs")
    if c["trust_zone"]:
        bits.append(f"{c['trust_zone']} pre-gate filtered")
    if c["observation"]:
        bits.append(f"{c['observation']} observations")
    if c["error"]:
        bits.append(f"{c['error']} errors")
    return f'<p class="ledger-sec-sub">Tap health (7d): {" · ".join(bits)}.</p>'


def _research_runs_off_line() -> str:
    """The owner-voice explanation for why wonderings sit undetonated — ONE
    section-level line, replacing the old per-card 'set LEDGER_RESEARCH_RUN=1'
    dev-syntax leak (a directive owner copy should never carry)."""
    if research_run_enabled():
        return ""
    return (
        '<p class="ledger-sec-sub">Research runs are off — wonderings are '
        "collected and run when you enable research.</p>"
    )


_RESEARCH_TUTORIAL = (
    "Wonderings I detected in your musings, and the inert proposals they "
    "produced — approve, dig further, steer, or reject. Nothing acts until "
    "you say so."
)


def _research_section(db_path: Path | str | None) -> str:
    list_html = render_ledger_research_list(db_path)
    # PR9 "no section ceremony": the tutorial sentence is a visible <p> only
    # while the list is empty; once real proposals/wonderings exist it folds
    # into the heading's title= instead of repeating on every visit.
    if "ledger-empty" in list_html:
        heading = '<h3 class="ledger-sec-h">Research</h3>'
        sub = f'<p class="ledger-sec-sub">{escape(_RESEARCH_TUTORIAL)}</p>'
    else:
        heading = f'<h3 class="ledger-sec-h" title="{escape(_RESEARCH_TUTORIAL, quote=True)}">Research</h3>'
        sub = ""
    return (
        heading
        + sub
        + _research_runs_off_line()
        + _tap_health_line(db_path)
        + list_html
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
  // The ratify receipt ("armed"/"queued for arming") renders into
  // #ledger-receipt — a SIBLING of #ledger-reconcile the reload() swap above
  // never touches, so the notice survives the list refresh underneath it.
  function showReceipt(text){
    var el=document.getElementById('ledger-receipt');
    if(!el){ return; }
    el.textContent=text;
    el.hidden=false;
  }
  function esc(s){
    return String(s==null?'':s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
  }
  // In-card Rewrite (PR9, replaces window.prompt): swaps the falsifier card's
  // body for a textarea PRE-FILLED with the inferred text already on the
  // card, plus kit Save/Cancel. No overlay — the card being edited stays
  // visible by construction (the capture-box in-card-swap idiom).
  function beginRewrite(card){
    var body=card.querySelector('.ledger-editable-body');
    if(!body || card.getAttribute('data-editing')==='1'){ return; }
    card.setAttribute('data-editing','1');
    var original=body.innerHTML;
    var current=body.textContent||'';
    body.innerHTML=
      '<textarea class="ledger-rewrite-ta" rows="3">'+esc(current)+'</textarea>'
      +'<div class="ledger-cap-row">'
      +'<button type="button" class="k-btn k-btn-primary k-btn-sm" data-rewrite-save>Save</button>'
      +'<button type="button" class="k-btn k-btn-quiet k-btn-sm" data-rewrite-cancel>Cancel</button>'
      +'</div>';
    function restore(){
      body.innerHTML=original;
      card.removeAttribute('data-editing');
    }
    var ta=body.querySelector('.ledger-rewrite-ta');
    if(ta){ ta.focus(); ta.setSelectionRange(ta.value.length, ta.value.length); }
    body.querySelector('[data-rewrite-cancel]').addEventListener('click', restore);
    body.querySelector('[data-rewrite-save]').addEventListener('click', function(){
      var txt=(ta&&ta.value||'').trim();
      if(!txt){ if(ta){ ta.focus(); } return; }
      this.disabled=true;
      var recId=card.getAttribute('data-rec-card');
      fetch('/api/reconcile/falsifier/'+recId,
            {method:'POST',headers:{'Content-Type':'application/json'},
             body:JSON.stringify({action:'edit', text:txt})})
        .then(function(r){ return r.json(); })
        .then(function(res){
          if(res && res.receipt){ showReceipt(res.receipt); }
          reload();
        });
    });
  }
  document.addEventListener('click', function(e){
    var v=e.target.closest('[data-rec-verdict]');
    if(v){
      v.disabled=true;
      fetch('/api/reconcile/'+v.getAttribute('data-rec-kind')+'/'+v.getAttribute('data-rec-id')
            +'/'+v.getAttribute('data-rec-verdict'),{method:'POST'})
        .then(function(){ reload(); })
        .catch(function(){ v.disabled=false; });
      return;
    }
    var f=e.target.closest('[data-falsifier-action]');
    if(f){
      var action=f.getAttribute('data-falsifier-action');
      if(action==='edit'){
        var card=f.closest('[data-rec-card]');
        if(card){ beginRewrite(card); }
        return;
      }
      f.disabled=true;
      fetch('/api/reconcile/falsifier/'+f.getAttribute('data-rec-id'),
            {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action:action})})
        .then(function(r){ return r.json(); })
        .then(function(res){
          if(res && res.receipt){ showReceipt(res.receipt); }
          reload();
        })
        .catch(function(){ f.disabled=false; });
    }
  });
})();</script>"""


def _missing_falsifier_card(gap: ReconcileItem) -> str:
    """One 'Add falsifier' gap card — its OWN ``data-rec-card`` with an empty
    editable body, so 'Add falsifier' opens the same in-card editor the ratify
    queue uses and Save POSTs ``{action:'edit', text}`` to
    /api/reconcile/falsifier/<decision_id>. Shared by ``_missing_falsifier_line``
    (the Queues-block list) and ``_reconcile_packet_items`` (one pk-item each).

    (Bug fix 2026-07-14: the old dense single line wrapped every 'add' in ONE
    div with no ``data-rec-card``, so ``beginRewrite``'s
    ``closest('[data-rec-card]')`` returned null and the button silently did
    nothing.)"""
    return (
        f'<div class="ledger-musing" data-rec-card="{gap.item_id}">'
        f'<div class="ledger-musing-head">{escape(gap.label)}'
        "<span class='ledger-chan'>needs falsifier</span></div>"
        # Empty editable body → the in-card editor opens blank for the owner to
        # author the tripwire in their own words (beginRewrite reads this node).
        '<div class="ledger-body ledger-editable-body"></div>'
        '<div class="ledger-cap-row">'
        '<button type="button" class="k-btn k-btn-sm k-btn-primary" '
        f'data-falsifier-action="edit" data-rec-id="{gap.item_id}">Add falsifier</button>'
        "</div></div>"
    )


def _missing_falsifier_line(db_path: Path | str | None) -> str:
    """Live held positions with no falsifier — no tripwire coverage is an
    irreducible owner ask. Renders the lead line + one ``_missing_falsifier_card``
    per gap (each its own ``data-rec-card``)."""
    from synthesis.reconcile import list_missing_falsifiers

    try:
        gaps = list_missing_falsifiers(db_path)
    except Exception:
        gaps = []
    if not gaps:
        return ""
    lead = (
        "1 live decision needs a falsifier"
        if len(gaps) == 1
        else f"{len(gaps)} live decisions need a falsifier"
    )
    cards = "".join(_missing_falsifier_card(gap) for gap in gaps)
    return f'<div class="ledger-missing-lead muted">{escape(lead)}:</div>{cards}'


def _reconcile_card(item: ReconcileItem) -> str:
    """ONE reconcile row's markup — an inferred-falsifier ratify/rewrite/drop
    card, or a note/theme one-tap verdict card. Carries its own
    ``data-rec-card``/``data-rec-id`` + action hooks and NO wrapping div or
    ``id`` — shared by ``render_reconcile_list`` (inside the one
    ``#ledger-reconcile`` container) and ``_reconcile_packet_items`` (one
    ``pk-item`` per row). ``_RECONCILE_JS``'s document-level click delegation
    fires for these buttons regardless of which container holds them."""
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
        # data-rec-card + .ledger-editable-body: the in-card Rewrite swap
        # (PR9) locates this exact card + the body div carrying the
        # already-inferred text it pre-fills the textarea with.
        return (
            f'<div class="ledger-musing" data-rec-card="{item.item_id}">'
            f'<div class="ledger-musing-head">{head}</div>'
            f'<div class="ledger-body ledger-editable-body">{escape(item.body[:400])}</div>'
            f'<div class="ledger-cap-row">{buttons}</div></div>'
        )
    rec_kind = "note" if item.kind == "note" else "theme"
    buttons = "".join(
        f'<button type="button" class="k-btn k-btn-sm {cls}" '
        f'data-rec-kind="{rec_kind}" data-rec-id="{item.item_id}" '
        f'data-rec-verdict="{verdict}">{label}</button>'
        for verdict, label, cls in _RECONCILE_VERDICTS
    )
    tag = item.label if item.kind == "theme" else (item.source_ref or item.label)
    head = f"<span class='ledger-chan'>{escape(tag)}</span>"
    return (
        '<div class="ledger-musing">'
        f'<div class="ledger-musing-head">{head}</div>'
        f'<div class="ledger-body">{escape(item.body[:400])}</div>'
        f'<div class="ledger-cap-row">{buttons}</div></div>'
    )


def render_reconcile_list(db_path: Path | str | None) -> str:
    """The seed-reconciliation fragment — one-tap verdicts until the list is empty.
    Degrades to the empty state on a pre-0130 DB (no decided_by column yet).

    ONE ``#ledger-reconcile`` container wrapping the missing-falsifier gap cards
    then a ``_reconcile_card`` per unreconciled row — this is the Queues-block /
    ``?fragment=reconcile`` output that ``_RECONCILE_JS``'s ``reload()`` swaps."""
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
    cards.extend(_reconcile_card(item) for item in items)
    return f'<div id="ledger-reconcile">{"".join(cards)}</div>'


def _reconcile_packet_items(db_path: Path | str | None) -> list[str]:
    """The reconcile queue as ONE packet fragment PER ROW — the missing-falsifier
    gap cards then the unreconciled verdict/falsifier cards, each its own
    ``data-rec-card`` and NO ``id="ledger-reconcile"`` (the real container lives
    in the Queues block on the same page; a second copy of the id would collide
    and ``reload()`` would target the wrong one).

    Splitting per row is what makes the packet walk count and settle each item
    individually — before this the whole ``render_reconcile_list`` blob was
    appended as a single ``pk-item``, so N rows counted as 1 and the first
    row-action's settle-detector advanced past the rest of the batch. Each
    source read degrades independently (a broken read drops its rows, never the
    packet)."""
    from synthesis.reconcile import list_missing_falsifiers, list_unreconciled

    fragments: list[str] = []
    try:
        gaps = list_missing_falsifiers(db_path)
    except Exception:
        gaps = []
    fragments.extend(_missing_falsifier_card(gap) for gap in gaps)
    try:
        items = list_unreconciled(db_path)
    except Exception:
        items = []
    fragments.extend(_reconcile_card(item) for item in items)
    return fragments


def _condition_text(cond: object) -> str:
    """One falsifier condition's display text — the owner's own ``note`` when
    the extractor kept one, else the structured metric/op/threshold shape."""
    from decision_conditions import DecisionCondition

    if isinstance(cond, DecisionCondition):
        if cond.note:
            return cond.note
        op_word = {"lt": "<", "le": "<=", "gt": ">", "ge": ">=", "eq": "="}.get(cond.op, cond.op)
        return f"{cond.metric} {op_word} {cond.threshold:g} {cond.unit}"
    return str(cond)


def render_armed_falsifiers_table(db_path: Path | str | None) -> str:
    """The Reconcile section's "Armed falsifiers (N)" table — one row per open
    owner decision condition, read through :func:`decision_conditions.
    load_all_open_decisions` (the SAME per-ticker accessor
    ``DecisionConditionTrigger.scan`` evaluates — never the position-lifecycle
    snapshot, which is a display-only summary the trigger never reads).

    Hide-don't-stub: N=0 renders nothing (the caller decides whether a
    receipt line alone still earns the section). Best-effort — any read
    failure degrades to the empty string, never a 500."""
    if db_path is None:
        # decision_conditions.py's whole module contracts on a concrete
        # Path|str (unlike synthesis.reconcile's Path|str|None-with-default
        # convention this panel otherwise follows) — no default DB to fall
        # back to here, so an absent path degrades like every other read
        # failure below.
        return ""
    try:
        from decision_conditions import load_all_open_decisions

        decisions = load_all_open_decisions(db_path)
    except Exception:
        return ""
    rows: list[tuple[str, str, str, int]] = []  # (ticker, falsifier, since, decision_id)
    for d in decisions:
        for cond in d.conditions:
            rows.append((d.ticker, _condition_text(cond), d.made_at, d.decision_id))
    if not rows:
        return ""
    body = "".join(
        "<tr>"
        f'<td class="ledger-armed-ticker">{escape(ticker)}</td>'
        f'<td class="ledger-armed-falsifier" title="{escape(text)}">'
        f"{escape(text[:100])}{'…' if len(text) > 100 else ''}</td>"
        f'<td class="ledger-armed-since">{stamp_html(since, mode="date", css="")}</td>'
        f'<td class="ledger-armed-num"><a href="{_DECISIONS_HASH}">#{decision_id}</a></td>'
        "</tr>"
        for ticker, text, since, decision_id in rows
    )
    return (
        f'<h4 class="ledger-armed-h">Armed falsifiers ({len(rows)})</h4>'
        '<table class="ledger-armed-table"><thead><tr>'
        "<th>Ticker</th><th>Falsifier</th><th>Since</th><th>Decision</th>"
        f"</tr></thead><tbody>{body}</tbody></table>"
    )


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
    # A ratify receipt (the arming-status line) renders into this div, a
    # SIBLING of #ledger-reconcile — not inside it — so the reload's
    # outerHTML swap on #ledger-reconcile (or a direct ?fragment=reconcile
    # refetch) can never clobber it before the owner reads it.
    receipt_div = '<div id="ledger-receipt" class="ledger-receipt" hidden></div>'
    armed_table = render_armed_falsifiers_table(db_path)
    if "ledger-empty" in items and auto_line:
        # Nothing needs the owner's reconcile verdicts — but armed tripwires
        # are still the between-reconciles reason to visit, so the table (if
        # non-empty) survives the section's collapse to a receipt line.
        return f'<h3 class="ledger-sec-h">Reconcile</h3>{auto_line}{receipt_div}{armed_table}'
    return (
        '<h3 class="ledger-sec-h">Reconcile</h3>'
        '<p class="ledger-sec-sub">Only what genuinely needs you — falsifiers I would '
        "quote back at you must be in your own words.</p>"
        + auto_line
        + receipt_div
        + items
        + armed_table
        + _RECONCILE_JS
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
/* Backlink from a research item to its source note + the brief highlight the
   scroll applies on arrival (owner feedback 2026-07-14). */
.ledger-backlink { margin-left: auto; }
.om-flash { animation: om-flash-kf 1.6s ease-out; }
@keyframes om-flash-kf {
  0%, 100% { background: transparent; }
  20% { background: color-mix(in srgb, var(--accent) 16%, transparent); }
}
.om-body a { overflow-wrap: anywhere; }
.om-brief { margin-top: var(--sp-2); }
.om-brief summary { font-size: var(--fs-caption); font-weight: 600; color: var(--accent); cursor: pointer; }
.om-brief-body { margin-top: var(--sp-2); padding: var(--sp-2) var(--sp-3); border-left: 3px solid var(--border-2); font-size: var(--fs-caption); color: var(--fg-soft); }
.om-brief-takeaways { margin: 0 0 var(--sp-2); padding-left: var(--sp-4); }
.om-brief-line { margin: var(--sp-1) 0; }
.om-brief-src { margin: var(--sp-2) 0 0; color: var(--muted); font-size: var(--fs-micro); }
/* The inline answer (overhaul): the Ledger's response to a question-shaped
   capture, generated once at capture time and stored on the note. A quiet
   accent-bordered block under the captured thought — distinct from the thought
   itself, token-only. */
.om-answer { margin-top: var(--sp-2); padding: var(--sp-2) var(--sp-3); border-left: 3px solid var(--accent); background: var(--accent-soft); border-radius: var(--radius); font-size: var(--fs-caption); line-height: 1.55; color: var(--fg-soft); }
.om-answer > :first-child { margin-top: 0; }
.om-answer > :last-child { margin-bottom: 0; }
.om-answer-label { display: block; font-size: var(--fs-micro); font-weight: 600; color: var(--accent); text-transform: uppercase; letter-spacing: 0.05em; margin-bottom: var(--sp-1); }
/* The answer is being generated on a background thread — an honest quiet
   in-progress state the poll swaps for the real answer when it lands. */
.om-answer-pending { color: var(--muted); font-style: italic; }
/* Inline chat (overhaul P3): "Ask more" / "Discuss" expands a real thread with
   the Ask brain right inside the card — no navigation, no popup. Token-only. */
.om-chat { margin-top: var(--sp-2); border-top: 1px solid var(--hairline); padding-top: var(--sp-2); }
.om-chat-thread { display: flex; flex-direction: column; gap: var(--sp-2); margin-bottom: var(--sp-2); }
.om-chat-msg { font-size: var(--fs-caption); line-height: 1.5; padding: var(--sp-2) var(--sp-3); border-radius: var(--radius); max-width: 90%; overflow-wrap: anywhere; }
.om-chat-user { align-self: flex-end; background: var(--accent-soft); color: var(--fg); }
.om-chat-assistant { align-self: flex-start; background: var(--surface); color: var(--fg-soft); }
.om-chat-pending { color: var(--muted); }
.om-chat-input { flex: 1; font-family: var(--sans); font-size: var(--fs-body); }
/* The universal reply box (Phase B) — one input per card, routed by the
   reply-intent classifier; the receipt bubble is the acted-path acknowledgement. */
.om-reply-input { flex: 1; font-family: var(--sans); font-size: var(--fs-body); }
.om-chat-receipt { color: var(--accent); font-weight: 600; }
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


# The universal reply box (Phase B): every card carries ONE input. The first
# send classifies via /api/onmymind/<id>/reply (FAST tier): an action intent
# executes through the same act_on_feed_item core the old buttons used and
# paints a receipt bubble; 'question' streams a real Ask turn in-card
# (/api/ask/stream — stage/delta frames, never a frozen '...'); once a chat
# session is live on a card, later sends stream directly (no re-classify).
_OM_CHAT_JS = """<script>(function(){
  if(window.__omChatWired){ return; }
  window.__omChatWired = true;
  var states={};
  function esc(s){ return String(s==null?'':s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }
  function bubble(role, text){
    return '<div class="om-chat-msg om-chat-'+role+'">'+esc(text).replace(/\\n/g,'<br>')+'</div>';
  }
  function cardState(card){
    var id=card.getAttribute('data-om-id');
    if(!states[id]){
      var bodyEl=card.querySelector('.om-body');
      var ansEl=card.querySelector('.om-answer');
      var seededQ=bodyEl?(bodyEl.textContent||'').trim():'';
      var seededA=ansEl?(ansEl.textContent||'').replace(/^\\s*Answer\\s*/,'').trim():'';
      states[id]={ session_id:null, history:(seededA?[{role:'user',text:seededQ},{role:'assistant',text:seededA}]:[]) };
    }
    return states[id];
  }
  function ensureThread(card){
    var thread=card.querySelector('.om-chat-thread');
    if(thread){ return thread; }
    thread=document.createElement('div');
    thread.className='om-chat-thread om-chat';
    var row=card.querySelector('.om-actions');
    if(row && row.parentNode){ row.parentNode.insertBefore(thread, row); }
    else { card.appendChild(thread); }
    return thread;
  }
  function streamTurn(card, q, pend, done){
    var state=cardState(card);
    var thread=ensureThread(card);
    var ticker=card.getAttribute('data-om-ticker')||'';
    var payload={ query:q };
    if(ticker){ payload.tickers=[ticker]; }
    if(state.session_id){ payload.session_id=state.session_id; }
    else if(state.history.length){ payload.history=state.history; }
    var acc=''; var finalText=null; var errText=null;
    function handle(ev){
      if(!ev || !ev.type){ return; }
      if(ev.type==='session'){ if(ev.session_id){ state.session_id=ev.session_id; state.history=[]; } return; }
      if(ev.type==='stage'){ if(!acc){ pend.textContent=(ev.stage||'working')+'...'; } return; }
      if(ev.type==='delta' && ev.text){
        acc+=ev.text;
        pend.classList.remove('om-chat-pending');
        pend.innerHTML=esc(acc).replace(/\\n/g,'<br>');
        return;
      }
      if(ev.type==='final'){ finalText=(ev.text||ev.message||acc); return; }
      if(ev.type==='error'){ errText=ev.error||'Something went wrong.'; return; }
    }
    function finish(){
      var out=errText || finalText || acc || 'No answer came back.';
      pend.remove();
      thread.insertAdjacentHTML('beforeend', bubble('assistant', out));
      done();
    }
    fetch('/api/ask/stream',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)})
      .then(function(r){
        if(!r.ok || !r.body){ throw new Error('bad response'); }
        var reader=r.body.getReader();
        var dec=new TextDecoder();
        var buf='';
        function pump(){
          return reader.read().then(function(step){
            if(step.done){ finish(); return; }
            buf+=dec.decode(step.value,{stream:true});
            var idx;
            while((idx=buf.indexOf('\\n\\n'))!==-1){
              var frame=buf.slice(0,idx); buf=buf.slice(idx+2);
              if(frame.indexOf('data: ')!==0){ continue; }
              var ev=null;
              try{ ev=JSON.parse(frame.slice(6)); }catch(err){ ev=null; }
              if(ev){ handle(ev); }
            }
            return pump();
          });
        }
        return pump();
      })
      .catch(function(){
        pend.remove();
        thread.insertAdjacentHTML('beforeend', bubble('assistant','Could not reach the server - try again.'));
        done();
      });
  }
  function sendReply(card){
    var input=card.querySelector('.om-reply-input');
    var btn=card.querySelector('[data-om-reply]');
    if(!input){ return; }
    var q=(input.value||'').trim();
    if(!q){ input.focus(); return; }
    input.value='';
    if(btn){ btn.disabled=true; }
    var thread=ensureThread(card);
    thread.insertAdjacentHTML('beforeend', bubble('user', q));
    var pend=document.createElement('div');
    pend.className='om-chat-msg om-chat-assistant om-chat-pending';
    pend.textContent='...'; thread.appendChild(pend);
    function done(){ if(btn){ btn.disabled=false; } input.focus(); }
    var state=cardState(card);
    if(state.session_id){ streamTurn(card, q, pend, done); return; }
    var id=card.getAttribute('data-om-id');
    fetch('/api/onmymind/'+id+'/reply',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({text:q})})
      .then(function(r){ if(!r.ok){ throw new Error(); } return r.json(); })
      .then(function(res){
        if(res && res.mode==='acted'){
          pend.classList.remove('om-chat-pending');
          pend.classList.add('om-chat-receipt');
          pend.textContent=res.receipt||'Done.';
          var badge=card.querySelector('.om-ladder');
          if(badge && res.ladder_label){ badge.textContent=res.ladder_label; }
          if(res.removed){
            setTimeout(function(){ if(card.parentNode){ card.parentNode.removeChild(card); } }, 900);
          }
          done();
          return;
        }
        // mode 'chat' (or anything unexpected): converse.
        streamTurn(card, q, pend, done);
      })
      .catch(function(){
        pend.remove();
        ensureThread(card).insertAdjacentHTML('beforeend', bubble('assistant','Could not reach the server - try again.'));
        done();
      });
  }
  document.addEventListener('click', function(e){
    var b=e.target.closest('[data-om-reply]');
    if(!b){ return; }
    var card=b.closest('[data-om-id]'); if(!card){ return; }
    sendReply(card);
  });
  document.addEventListener('keydown', function(e){
    if(e.key!=='Enter'){ return; }
    var input=e.target && e.target.closest ? e.target.closest('.om-reply-input') : null;
    if(!input){ return; }
    var card=input.closest('[data-om-id]'); if(!card){ return; }
    e.preventDefault();
    sendReply(card);
  });
})();</script>"""


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


def engage_brief_block(ctx: dict[str, object]) -> str:
    """The attached artifact brief (``context['engage_brief']``) as a collapsible block —
    takeaways + bull/bear, plus the stress layer (falsifiers / second-order / book)."""
    brief = ctx.get("engage_brief")
    if not isinstance(brief, dict):
        return ""
    b = cast("dict[str, object]", brief)
    mode = str(b.get("mode") or "brief")
    parts: list[str] = []
    takeaways = b.get("takeaways")
    if isinstance(takeaways, list):
        lis = "".join(
            f"<li>{escape(str(t))}</li>" for t in cast("list[object]", takeaways) if str(t).strip()
        )
        if lis:
            parts.append(f'<ul class="om-brief-takeaways">{lis}</ul>')
    rows: tuple[tuple[str, str], ...] = (("Bull", "bull"), ("Bear", "bear"))
    if mode == "stress":
        rows = (
            *rows,
            ("What would change your mind", "changes_mind"),
            ("Second-order", "second_order"),
            ("Your book", "portfolio_map"),
        )
    for label, key in rows:
        val = str(b.get(key) or "").strip()
        if val:
            parts.append(
                f'<p class="om-brief-line"><strong>{escape(label)}:</strong> {escape(val)}</p>'
            )
    if not parts:
        return ""
    src = str(b.get("source") or "")
    src_line = f'<p class="om-brief-src">from {escape(src)}</p>' if src else ""
    summary = "Stress-test attached" if mode == "stress" else "Brief attached"
    return (
        f'<details class="om-brief"><summary>{summary}</summary>'
        f'<div class="om-brief-body">{"".join(parts)}{src_line}</div></details>'
    )


def _answer_block(ctx: dict[str, object]) -> str:
    """The Ledger's inline answer to a question-shaped capture (the overhaul).

    Generated once at capture time by ``onmymind.respond.answer_capture`` and
    stored on the note (``context['ledger_answer']``); this renders the stored
    text with NO LLM on the read path. While the background answer thread is
    still working (``ledger_answer_pending``) an honest "Answering…" state
    renders instead — the capture JS polls and swaps the card when it lands.
    Empty string when the capture wasn't a question (no answer stored)."""
    ans = ctx.get("ledger_answer")
    if not isinstance(ans, dict):
        if ctx.get("ledger_answer_pending"):
            return (
                '<div class="om-answer om-answer-pending">'
                '<span class="om-answer-label">Answer</span>Answering…</div>'
            )
        return ""
    text = str(cast("dict[str, object]", ans).get("text") or "").strip()
    if not text:
        return ""
    return (
        '<div class="om-answer"><span class="om-answer-label">Answer</span>'
        f"{render_prose(text)}</div>"
    )


def _is_question_item(item: FeedItem) -> bool:
    """A card is a question when the answer core already answered it (a stored
    ``ledger_answer``) or its text still reads as a question (answers off / a
    pre-overhaul capture). Questions get the inline chat, not the filing ladder."""
    if isinstance((item.note.context or {}).get("ledger_answer"), dict):
        return True
    return item.item_type == "musing" and is_answerable_capture(item.note.body)


def _verb_button(verb: str, label: str, cls: str) -> str:
    return f'<button type="button" class="k-btn k-btn-sm {cls}" data-om-verb="{verb}">{escape(label)}</button>'


def _reply_placeholder(item: FeedItem) -> str:
    """The reply box's nudge, phrased by what the card IS — the one remaining
    per-type contextualization now that the verb menus are gone."""
    if item.item_type in ("doc", "link"):
        return "Reply - research it, save it, or ask about it..."
    if _is_question_item(item):
        return "Ask a follow-up..."
    if item.wondering:
        return "Reply - research it, or just talk it through..."
    return "Reply - save it, send to research, or talk it through..."


def _feed_actions(item: FeedItem) -> str:
    """The card's action row (Phase B): ONE universal interaction — a reply box
    routed by the ``ledger_reply_intent`` classifier — plus Dismiss. The per-type
    verb menus (Research it / Save / Worldview / Ask more…) made the owner the
    router; now "dig into this" / "keep this" / "what changed?" all land in the
    same box and the machine routes them (act / converse / note)."""
    return (
        f'<input type="text" class="om-reply-input" '
        f'placeholder="{escape(_reply_placeholder(item), quote=True)}">'
        '<button type="button" class="k-btn k-btn-primary k-btn-sm" data-om-reply>Send</button>'
        + _verb_button("dismiss", "Dismiss", "k-btn-danger")
    )


def _feed_card(item: FeedItem) -> str:
    note = item.note
    ctx = note.context or {}
    chips = ""
    if note.ticker:
        ident = ticker_label(note.ticker)
    elif item.item_type in ("doc", "link"):
        ident = '<span class="ledger-unattr">reading</span>'
    elif ctx.get("needs_ticker"):
        # Restore the set-ticker attribution path in feed mode (bug fix
        # 2026-07-14: the chips existed only on the legacy _musing_card, so a
        # needs_ticker capture had NO way to be attributed once the feed flag
        # was on). Mirrors _musing_card's needs-ticker branch.
        cands = ctx.get("ticker_candidates")
        names = (
            ", ".join(escape(str(c)) for c in cast("list[object]", cands))
            if isinstance(cands, list)
            else ""
        )
        label = f"needs ticker: {names}" if names else "needs ticker"
        ident = f'<span class="ledger-needs">{label}</span>'
        chips = _ticker_candidate_chips(cands)
    else:
        ident = '<span class="ledger-unattr">unattributed</span>'
    if item.item_type == "musing":
        channel = str(ctx.get("channel") or "")
        type_chip = f'<span class="ledger-chan">{escape(channel)}</span>' if channel else ""
    else:
        type_chip = f'<span class="om-type">{escape(item.item_type)}</span>'
    wondering = '<span class="om-wondering">wondering</span>' if item.wondering else ""
    ladder_label = LADDER_LABELS.get(item.ladder or "", "")
    if item.ladder == "incorporated":
        # Wave B (B7): "in research" is a doorway, not an inert badge — the
        # existing data-ledger-jump listener opens the Queues block and scrolls
        # to the Research section. Keeps .om-ladder so the post-action label
        # repaint still finds it. Other ladder values stay inert spans.
        ladder_badge = (
            '<button type="button" class="k-chip k-chip-btn om-ladder" '
            'data-ledger-jump="ledger-jump-research" '
            f'title="Open the research queue">{escape(ladder_label)}</button>'
        )
    else:
        ladder_badge = f'<span class="om-ladder">{escape(ladder_label)}</span>'
    when = stamp_html(note.created_at, css="ledger-when")
    return (
        f'<div class="ledger-musing om-item" id="om-note-{note.id}" data-om-id="{note.id}" '
        f'data-note-id="{note.id}" '
        f'data-om-ticker="{escape(note.ticker or "", quote=True)}">'
        f'<div class="ledger-musing-head">{ident}{type_chip}{wondering}{ladder_badge}{when}</div>'
        f'<div class="ledger-body om-body">{_feed_body(item)}</div>'
        f"{_answer_block(ctx)}"
        f"{engage_brief_block(ctx)}"
        f"{chips}"
        f'<div class="ledger-cap-row om-actions">{_feed_actions(item)}</div>'
        "</div>"
    )


def render_feed_card(item: FeedItem) -> str:
    """One feed card's HTML — the ``fragment=card`` read behind the card-level
    refreshes (set-ticker, the freshly-captured prepend, the answer-poll swap)."""
    return _feed_card(item)


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


_ONMYMIND_TUTORIAL = (
    "What you're thinking about and reading, newest first. Dismiss it, save it "
    "for later, talk it through, or send it into research."
)


def _onmymind_section(db_path: Path | str | None, *, user_id: str = DEFAULT_USER_ID) -> str:
    """The On My Mind feed section — empty string when the flag is off (the panel
    then keeps its plain Musings list unchanged).

    The tutorial sentence (PR9 "no section ceremony") renders as a visible
    ``<p>`` only while the feed is empty (there's nothing else to look at, so
    the explanation earns its place); once real cards exist it folds into the
    heading's ``title=`` instead of repeating itself under every visit."""
    if not onmymind_enabled():
        return ""
    list_html = render_onmymind_list(db_path, user_id=user_id)
    if "ledger-empty" in list_html:
        heading = '<h3 class="ledger-sec-h">On My Mind</h3>'
        sub = f'<p class="ledger-sec-sub">{escape(_ONMYMIND_TUTORIAL)}</p>'
    else:
        heading = f'<h3 class="ledger-sec-h" title="{escape(_ONMYMIND_TUTORIAL, quote=True)}">On My Mind</h3>'
        sub = ""
    return (
        _ONMYMIND_STYLE
        + heading
        + sub
        + '<div id="onmymind-list">'
        + list_html
        + "</div>"
        + _ONMYMIND_JS
        + _OM_CHAT_JS
        # Set-ticker chips now render on feed cards too (needs_ticker branch of
        # _feed_card), so the listener must be wired in feed mode as well.
        + _SET_TICKER_JS
    )


# One guarded listener for the set-ticker chips (PR9): POSTs the new
# set_ticker lifecycle action, then re-fetches the list fragment — the
# existing list-refresh path _CAPTURE_JS already uses after a capture.
_SET_TICKER_JS = """<script>(function(){
  if(window.__ledgerSetTickerWired){ return; }
  window.__ledgerSetTickerWired = true;
  document.addEventListener('click', function(e){
    var btn=e.target.closest('[data-set-ticker]');
    if(!btn){ return; }
    var card=btn.closest('[data-note-id]'); if(!card){ return; }
    var noteId=card.getAttribute('data-note-id');
    var ticker=btn.getAttribute('data-set-ticker');
    btn.disabled=true;
    fetch('/api/notes/'+noteId+'/set_ticker',{method:'POST',
      headers:{'Content-Type':'application/json'},body:JSON.stringify({ticker:ticker})})
      .then(function(r){ if(!r.ok){ throw new Error(); } return r.json(); })
      .then(function(){
        // Patch THIS card in place (fragment=card). A whole-list repaint here
        // would destroy any open inline chat on a neighboring card. The legacy
        // (flag-off) list has no per-card fragment — fall back to its refresh.
        var om=document.getElementById('onmymind-list');
        if(om){
          fetch('/api/panel/musings?fragment=card&note='+encodeURIComponent(noteId))
            .then(function(r){ return r.ok ? r.text() : ''; })
            .then(function(h){
              var cur=document.getElementById('om-note-'+noteId);
              if(cur && h){ cur.outerHTML=h; return; }
              fetch('/api/panel/musings?fragment=onmymind').then(function(r){return r.text();}).then(function(h2){ om.innerHTML=h2; });
            });
          return;
        }
        var list=document.getElementById('ledger-list');
        if(list){ fetch('/api/panel/musings?fragment=list').then(function(r){return r.text();}).then(function(h){ list.innerHTML=h; }); }
      })
      .catch(function(){ btn.disabled=false; });
  });
})();</script>"""


def render_ledger_list(db_path: Path | str | None, *, user_id: str = DEFAULT_USER_ID) -> str:
    """The musings list fragment (re-fetched after a capture)."""
    rows = list_notes(user_id=user_id, kind="musing", db_path=db_path, limit=200)
    if not rows:
        return (
            '<div id="ledger-list"><p class="ledger-empty">No musings yet - capture a '
            "thought above, or send one (voice or text) to your Telegram bot.</p></div>"
        )
    body = "".join(_musing_card(r) for r in rows)
    return f'<div id="ledger-list">{body}</div>{_SET_TICKER_JS}'


def _jump_chip_counts(db_path: Path | str | None) -> dict[str, int]:
    """Pending counts for the jump-chip toolbar's Research / Reconcile /
    Worldview chips, reusing ``pipeline.open_loops``'s own cheap, independently-
    guarded queries (never a duplicate SQL string) — each degrades to 0 on any
    read failure so a chip count can never break the panel."""
    from pipeline.open_loops import (
        _pending_proposal_count,  # pyright: ignore[reportPrivateUsage]
        _proposed_tenet_count,  # pyright: ignore[reportPrivateUsage]
        _reconcile_count,  # pyright: ignore[reportPrivateUsage]
    )

    counts: dict[str, int] = {}
    try:
        counts["reconcile"] = _reconcile_count(db_path)
    except Exception:
        counts["reconcile"] = 0
    try:
        counts["research"] = _pending_proposal_count(db_path)
    except Exception:
        counts["research"] = 0
    try:
        counts["worldview"] = _proposed_tenet_count(db_path)
    except Exception:
        counts["worldview"] = 0
    return counts


# ---------------------------------------------------------------------------
# The packet walk (Phase C): a bounded "N need you" session over everything
# awaiting an owner verdict — research proposals, proposed Tenets, triage
# suggestions, the reconcile queue — one item at a time, ending in "Clear".
# The owner's proven behavior is binge-clearing bounded packets (the Sunday
# Telegram packet); an infinite feed with collapsed queues has no completion
# semantics, so nothing ever feels finished. Cards REUSE the exact builders
# their home sections render, so the existing document-delegated action
# handlers work unchanged inside the walk.
# ---------------------------------------------------------------------------

_PACKET_STYLE = """<style>
.ledger-packet { background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius); padding: var(--sp-3) var(--sp-4); margin-bottom: var(--sp-4); }
.pk-band { display: flex; align-items: baseline; gap: var(--sp-3); }
.pk-count { font-size: var(--fs-body); font-weight: 600; color: var(--fg); }
.pk-hint { font-size: var(--fs-caption); color: var(--muted); }
.pk-progress { display: flex; align-items: baseline; gap: var(--sp-2); font-size: var(--fs-caption); color: var(--muted); margin: var(--sp-2) 0; }
.pk-item > .ledger-musing, .pk-item > .ledger-stance { margin-bottom: 0; }
.pk-clear { color: var(--ok); font-weight: 600; padding: var(--sp-3) 0; }
</style>"""

_PACKET_JS = """<script>(function(){
  if(window.__ledgerPacketWired){ return; }
  window.__ledgerPacketWired = true;
  function items(root){ return root.querySelectorAll('.pk-item'); }
  function show(root, i){
    var list=items(root);
    for(var k=0;k<list.length;k++){ list[k].hidden=(k!==i); }
    var pos=root.querySelector('[data-pk-pos]');
    if(pos){ pos.textContent=String(Math.min(i+1, list.length)); }
    var clear=root.querySelector('.pk-clear');
    if(clear){ clear.hidden=(i<list.length); }
    if(i>=list.length){
      var prog=root.querySelector('.pk-progress');
      if(prog){ prog.hidden=true; }
    }
    root.setAttribute('data-pk-i', String(i));
  }
  function advance(root){
    var i=parseInt(root.getAttribute('data-pk-i')||'0',10);
    show(root, i+1);
  }
  document.addEventListener('click', function(e){
    var root=e.target && e.target.closest ? e.target.closest('.ledger-packet') : null;
    if(!root){ return; }
    if(e.target.closest('[data-pk-start]')){
      var band=root.querySelector('.pk-band'); if(band){ band.hidden=true; }
      var stage=root.querySelector('.pk-stage'); if(stage){ stage.hidden=false; }
      show(root, 0);
      return;
    }
    if(e.target.closest('[data-pk-skip]')){ advance(root); return; }
    if(e.target.closest('[data-pk-close]')){
      var st=root.querySelector('.pk-stage'); if(st){ st.hidden=true; }
      var bd=root.querySelector('.pk-band'); if(bd){ bd.hidden=false; }
      return;
    }
    // Triage mini-card actions (these cards have no home-section handler).
    var tr=e.target.closest('[data-pk-route]');
    if(tr){
      tr.disabled=true;
      fetch('/api/notes/'+tr.getAttribute('data-note-id')+'/route',
        {method:'POST',headers:{'Content-Type':'application/json'},
         body:JSON.stringify({intent:tr.getAttribute('data-intent')})})
        .then(function(r){ if(r.ok){ advance(root); } else { tr.disabled=false; } })
        .catch(function(){ tr.disabled=false; });
      return;
    }
    var td=e.target.closest('[data-pk-dismiss]');
    if(td){
      td.disabled=true;
      fetch('/api/notes/'+td.getAttribute('data-note-id')+'/archive',
        {method:'POST',headers:{'Content-Type':'application/json'},body:'{}'})
        .then(function(r){ if(r.ok){ advance(root); } else { td.disabled=false; } })
        .catch(function(){ td.disabled=false; });
      return;
    }
    // A settling action on a reused card (proposal verbs except the Steer
    // opener, tenet approve/reject, reconcile verdicts, falsifier ratify/drop,
    // the in-card Steer/Rewrite saves) advances the walk after a beat — the
    // card's own handler runs first via the same event.
    var settle=e.target.closest(
      '[data-verb]:not([data-verb="steer"]),[data-tenet-action],[data-rec-verdict],'
      +'[data-falsifier-action="ratify"],[data-falsifier-action="drop"],'
      +'[data-steer-save],[data-rewrite-save]');
    if(settle){ setTimeout(function(){ advance(root); }, 900); }
  });
})();</script>"""


def _triage_packet_card(note: AnalystNoteRow) -> str:
    """A parked comment as a packet item: the body + the one-tap suggested
    route (when the second pass left one) + Dismiss. These buttons carry
    ``data-pk-*`` hooks — the packet JS posts them directly (the Triage
    panel's own listener is scoped to its root and can't see this copy)."""
    ctx = note.context or {}
    sugg = ctx.get("route_suggestion")
    route_btn = ""
    if isinstance(sugg, dict):
        si = str(cast("dict[str, object]", sugg).get("intent") or "")
        if si:
            from pipeline.triage_panel import _INTENT_LABELS  # pyright: ignore[reportPrivateUsage]

            if si in _INTENT_LABELS:
                route_btn = (
                    '<button type="button" class="k-btn k-btn-primary k-btn-sm" '
                    f'data-pk-route data-note-id="{note.id}" '
                    f'data-intent="{escape(si, quote=True)}">'
                    f"Route to {escape(_INTENT_LABELS[si])}</button>"
                )
    ident = ticker_label(note.ticker) if note.ticker else '<span class="k-chip">PORTFOLIO</span>'
    return (
        '<div class="ledger-musing">'
        f'<div class="ledger-musing-head">{ident}<span class="ledger-chan">parked comment</span></div>'
        f'<div class="ledger-body">{escape(note.body[:400])}</div>'
        '<div class="ledger-cap-row">'
        f"{route_btn}"
        '<button type="button" class="k-btn k-btn-danger k-btn-sm" '
        f'data-pk-dismiss data-note-id="{note.id}">Dismiss</button>'
        "</div></div>"
    )


def _packet_items(db_path: Path | str | None) -> list[str]:
    """Everything awaiting an owner verdict, one card per item, each reusing
    its home section's builder. Every source degrades independently — a broken
    read drops its items, never the packet."""
    items: list[str] = []
    try:
        proposals = list_proposals(status="pending", db_path=db_path)
        groups: dict[int, list[ResearchProposal]] = {}
        for p in proposals:
            groups.setdefault(p.task_id if p.task_id is not None else -p.id, []).append(p)
        items.extend(_proposal_group_card(g) for g in groups.values())
    except Exception:
        pass
    try:
        from pipeline.worldview_panel import (
            _proposed_card,  # pyright: ignore[reportPrivateUsage]
            worldview_enabled,
        )
        from synthesis.tenets import list_tenets

        if worldview_enabled():
            items.extend(_proposed_card(t) for t in list_tenets(status="proposed", db_path=db_path))
    except Exception:
        pass
    try:
        from user_state.notes import list_triage_notes

        items.extend(
            _triage_packet_card(n)
            for n in list_triage_notes(db_path=db_path)
            if isinstance((n.context or {}).get("route_suggestion"), dict)
        )
    except Exception:
        pass
    try:
        # One pk-item PER reconcile row (gap cards + verdict/falsifier cards),
        # NOT the whole #ledger-reconcile blob as a single item — see
        # _reconcile_packet_items for why the old id-replace one-item hack broke
        # the count and the settle-advance.
        reconcile_fragments = _reconcile_packet_items(db_path)
        items.extend(reconcile_fragments)
    except Exception:
        pass
    return items


def _packet_section(db_path: Path | str | None) -> str:
    """The bounded "N need you" walk — empty string when nothing needs the
    owner (the packet only exists when it can end in Clear)."""
    items = _packet_items(db_path)
    if not items:
        return ""
    n = len(items)
    noun = "needs" if n == 1 else "need"
    cards = "".join(f'<div class="pk-item" hidden>{card}</div>' for card in items)
    return (
        _PACKET_STYLE
        + '<div class="ledger-packet" id="ledger-packet" data-pk-i="0">'
        + '<div class="pk-band">'
        + f'<span class="pk-count">{n} {noun} you</span>'
        + '<button type="button" class="k-btn k-btn-primary k-btn-sm" data-pk-start>Start</button>'
        + '<span class="pk-hint">one at a time, ends in Clear</span>'
        + "</div>"
        + '<div class="pk-stage" hidden>'
        + '<div class="pk-progress"><span data-pk-pos>1</span>'
        + f"<span>of {n}</span>"
        + '<button type="button" class="k-btn k-btn-quiet k-btn-sm" data-pk-skip>Skip</button>'
        + '<button type="button" class="k-btn k-btn-quiet k-btn-sm" data-pk-close>Close</button>'
        + "</div>"
        + cards
        + '<div class="pk-clear" hidden>Clear — nothing else needs you.</div>'
        + "</div></div>"
        + _PACKET_JS
    )


# (anchor id, chip label) — mirrors the Provenance console's anchor-nav
# contract (data-prov-jump / scrollIntoView, never an href="#anchor": the
# shell's hashchange router treats an unknown hash as a panel id and would
# navigate away to Overview). See _JUMP_NAV_JS below.
_JUMP_SECTIONS: tuple[tuple[str, str], ...] = (
    ("capture", "Capture"),
    ("onmymind", "On My Mind"),
    ("worldview", "Worldview"),
    ("stances", "Stances"),
    ("research", "Research"),
    ("reconcile", "Reconcile"),
)

_JUMP_NAV_JS = """
(function () {
  if (window.__ledgerJumpNav) return;
  window.__ledgerJumpNav = true;
  document.addEventListener('click', function (ev) {
    var b = ev.target && ev.target.closest ? ev.target.closest('[data-ledger-jump]') : null;
    if (!b) return;
    ev.preventDefault();
    var el = document.getElementById(b.getAttribute('data-ledger-jump'));
    if (!el) return;
    // A chip to a section now living inside the collapsed Queues block must
    // open it first, or scrollIntoView lands on a hidden element.
    var d = el.closest('details');
    if (d && !d.open) d.open = true;
    el.scrollIntoView({ behavior: 'smooth', block: 'start' });
  });
})();
""".strip()


def _jump_chips(counts: dict[str, int], *, onmymind_on: bool) -> str:
    # A jump chip must point at a section that actually renders — a chip to a
    # suppressed section is the broken doorway the audit fought. When On My Mind
    # is off, that section is empty and the plain Musings list is the front feed,
    # so the chip becomes "Musings" -> ledger-jump-musings instead.
    sections = [
        (anchor, label) for anchor, label in _JUMP_SECTIONS if anchor != "onmymind" or onmymind_on
    ]
    if not onmymind_on:
        sections.append(("musings", "Musings"))
    return "".join(
        f'<button type="button" class="k-chip k-chip-btn" data-ledger-jump="ledger-jump-{anchor}">'
        f"{escape(label)}"
        + (f' <span class="k-chip-mono">{counts[anchor]}</span>' if counts.get(anchor) else "")
        + "</button>"
        for anchor, label in sections
    )


def _jump_chip_toolbar(counts: dict[str, int], *, onmymind_on: bool) -> str:
    chips = _jump_chips(counts, onmymind_on=onmymind_on)
    return f'<div class="ledger-jump-toolbar">{chips}</div><script>{_JUMP_NAV_JS}</script>'


def render_ledger_jump_chips(db_path: Path | str | None) -> str:
    """The feed's jump chips + their nav listener, bare (no toolbar wrapper),
    for the composite Ledger console's merged band (``render_ledger_console``
    passes them as ``extra_nav`` and calls the feed ``embedded=True`` so the
    chips render exactly once). Same chips, counts, and ``data-ledger-jump``
    contract as the standalone panel's own toolbar."""
    counts = _jump_chip_counts(db_path)
    chips = _jump_chips(counts, onmymind_on=onmymind_enabled())
    return f"{chips}<script>{_JUMP_NAV_JS}</script>"


def render_ledger_panel(
    db_path: Path | str | None, *, user_id: str = DEFAULT_USER_ID, embedded: bool = False
) -> str:
    """The Ledger tab: capture box + newest-first musings.

    When ``LEDGER_ONMYMIND`` is on, the On My Mind feed (musings + readings, with
    the action ladder) is the front-of-funnel section and the plain Musings list is
    suppressed — On My Mind subsumes it. Off, the panel is unchanged.

    A jump-chip toolbar (PR9) precedes the sections — each chip scrolls to its
    anchor div (never an href hash; see ``_JUMP_NAV_JS``) and carries a pending
    count where a queue exists (Research / Reconcile / Worldview), reusing
    ``pipeline.open_loops``'s own cheap queries rather than duplicating SQL.

    ``embedded=True`` suppresses that internal toolbar: inside the composite
    Ledger console the band already carries the same chips
    (``render_ledger_jump_chips`` via ``render_console``'s ``extra_nav``), and
    two stacked chip bands were exactly the double-chrome the Phase-5 verifier
    flagged. Standalone rendering (``/api/panel/musings`` fragments, the legacy
    non-console path) is unchanged by the default.
    """
    onmymind = _onmymind_section(db_path, user_id=user_id)
    # On My Mind is the broader feed (readings too, + the ladder); when it's live
    # the plain musings list below would just duplicate it, so drop it.
    musings_block = (
        ""
        if onmymind
        else '<h3 class="ledger-sec-h">Musings</h3>' + render_ledger_list(db_path, user_id=user_id)
    )
    counts = _jump_chip_counts(db_path)
    # PR9 "no section ceremony": the panel tutorial line is a visible <p> only
    # while there's nothing captured yet (the front-of-funnel content — On My
    # Mind when live, else the plain Musings list — is empty); once real
    # captures exist it folds into <h2 title=> instead of repeating forever.
    front_of_funnel = onmymind or musings_block
    if embedded:
        # Composite Ledger console: the merged nav band's "Capture" chip already
        # names and jumps to this leading section (render_ledger_console drops
        # the feed's own section chip via nav_exclude), so a "Ledger" <h2>
        # directly under a band titled "Ledger" was a redundant repeat — the
        # front-of-funnel space the owner flagged. Triage/Journal keep their
        # distinct section h3s; only the leading feed sheds the echo.
        h2 = ""
        panel_sub = ""
    elif "ledger-empty" in front_of_funnel:
        h2 = "<h2>Ledger</h2>"
        panel_sub = (
            '<p class="sub">Your captured stream of consciousness. Talk or type a musing - '
            "to your Telegram bot on the go, or here at the desk; it lands linked to a name "
            "and you read it back below.</p>"
        )
    else:
        h2 = (
            '<h2 title="Your captured stream of consciousness — talk or type a musing, '
            "to your Telegram bot on the go or here at the desk; it lands linked to a name "
            'and you read it back below.">Ledger</h2>'
        )
        panel_sub = ""
    # P4 whole-tab reorg: capture + the conversational feed are the front door;
    # the four machinery sections fold into ONE collapsible "Queues" block below,
    # closed by default with a pending-count so real work is signposted but no
    # longer a wall of stacked sections. The jump chips still reach each section
    # (the nav JS opens the Queues block first — see _JUMP_NAV_JS).
    queue_pending = (
        counts.get("research", 0) + counts.get("reconcile", 0) + counts.get("worldview", 0)
    )
    queues_body = (
        f'<div id="ledger-jump-worldview">{render_worldview_section(db_path)}</div>'
        f'<div id="ledger-jump-stances">{_stance_section(db_path)}</div>'
        f'<div id="ledger-jump-research">{_research_section(db_path)}</div>'
        f'<div id="ledger-jump-reconcile">{_reconcile_section(db_path)}</div>'
    )
    # B11 (wave B): the summary must reflect what's inside — "N pending" alone
    # hid ~184 armed falsifiers behind a closed block. The armed count is read
    # off the ALREADY-rendered table header ("Armed falsifiers (N)"), so no
    # second query and no drift from what actually rendered.
    armed_m = re.search(r"Armed falsifiers \((\d+)\)", queues_body)
    armed_n = int(armed_m.group(1)) if armed_m else 0
    badges = []
    if queue_pending:
        badges.append(f"{queue_pending} pending")
    if armed_n:
        plural = "s" if armed_n != 1 else ""
        badges.append(f"{armed_n} armed falsifier{plural}")
    count_badge = "".join(f'<span class="ledger-queues-count">{b}</span>' for b in badges)
    queues = (
        '<details class="ledger-queues" id="ledger-queues">'
        '<summary class="ledger-queues-sum">Queues'
        f"{count_badge}"
        '<span class="ledger-queues-hint">research · reconcile · worldview · stances</span>'
        "</summary>"
        '<div class="ledger-queues-body">' + queues_body + "</div></details>"
    )
    return (
        _PANEL_STYLE
        + f'<section class="panel">{h2}'
        + panel_sub
        + ("" if embedded else _jump_chip_toolbar(counts, onmymind_on=bool(onmymind)))
        + f'<div id="ledger-jump-capture">{_capture_box()}</div>'
        # The bounded packet walk (Phase C) leads the feed: a finite "N need
        # you" session with completion semantics, before the open-ended stream.
        + _packet_section(db_path)
        + f'<div id="ledger-jump-onmymind">{onmymind}</div>'
        + f'<div id="ledger-jump-musings">{musings_block}</div>'
        + queues
        + "</section>"
    )
