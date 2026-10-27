"""The Ledger flat musings panel — read your captured stream of consciousness.

Wave A (the dogfood read-back): a newest-first list of captured musings
(``kind='musing'``, ``source='capture'``) plus an at-desk quick-capture box that
POSTs to ``/api/capture/text``. Direct project chat can also record notes;
this panel is the desk mouth + the read-back that proves capture earns its keep.
Theme synthesis + FTS search land in Wave B.

Pure read over the analyst_notes spine; token-only styles (guard-clean).
"""

from __future__ import annotations

from html import escape
from pathlib import Path
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from synthesis.reconcile import ReconcileItem

from identity import DEFAULT_USER_ID
from pipeline.research_panel_styles import RESEARCH_PANEL_STYLE
from ui.controls import ticker_label
from ui.panel import panel_section
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

_PANEL_STYLE = RESEARCH_PANEL_STYLE

_FRAGMENT_REFRESH_JS = """
  function refreshLedgerFragment(target, url){
    var request=(target.__readGeneration||0)+1; target.__readGeneration=request;
    if(target.__readController){ target.__readController.abort(); }
    target.__readController=new AbortController();
    (window.uiFetch || fetch)(url,{signal:target.__readController.signal})
      .then(function(r){ if(!r.ok){ throw new Error('Read unavailable'); } return r.text(); })
      .then(function(h){ if(request===target.__readGeneration && target.isConnected){ target.innerHTML=h; } })
      .catch(function(){
        if(request!==target.__readGeneration || !target.isConnected){ return; }
        var prior=target.querySelector('[data-refresh-error]'); if(prior){ prior.remove(); }
        var feedback=document.createElement('div'); feedback.className='k-well';
        feedback.setAttribute('data-refresh-error',''); feedback.setAttribute('role','status');
        feedback.textContent='Saved, but the list could not refresh. Previous entries remain visible. ';
        var retry=document.createElement('button'); retry.type='button';
        retry.className='k-btn k-btn-quiet k-btn-sm'; retry.textContent='Retry refresh';
        retry.addEventListener('click',function(){ refreshLedgerFragment(target,url); });
        feedback.appendChild(retry); target.prepend(feedback);
      });
  }
"""

LEDGER_CAPTURE_JS = """<script>(function(){
  var btn=document.getElementById('ledger-cap-btn');
  var ta=document.getElementById('ledger-cap-text');
  var st=document.getElementById('ledger-cap-status');
  if(!btn||!ta){ return; }

  function send(){
    var originalText=ta.value||'';
    var text=originalText.trim();
    if(!text){ ta.focus(); return; }
    btn.disabled=true; if(st){ st.textContent='Capturing...'; }
    fetch('/api/capture/text',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({text:text})})
      .then(function(r){
        if(!r.ok){ throw new Error('Capture request failed'); }
        return r.json();
      })
      .then(function(res){
        if(!res || (res.status!=='landed' && res.status!=='duplicate')){
          throw new Error('Capture did not finish');
        }
        if(ta.value===originalText){ ta.value=''; }
        if(st){ st.textContent=res.status==='duplicate' ? 'Already captured' : 'Captured'; }
        var list=document.getElementById('ledger-list');
        if(list){ refreshLedgerFragment(list,'/api/panel/musings?fragment=list'); }
      })
      .catch(function(){ if(st){ st.textContent='Could not reach the server - try again.'; } })
      .finally(function(){ btn.disabled=false; setTimeout(function(){ if(st){ st.textContent=''; } },4000); });
  }
  btn.addEventListener('click', send);
  ta.addEventListener('keydown', function(e){ if((e.metaKey||e.ctrlKey) && e.key==='Enter'){ send(); } });
})();</script>"""
LEDGER_CAPTURE_JS = LEDGER_CAPTURE_JS.replace(
    "(function(){", "(function(){" + _FRAGMENT_REFRESH_JS, 1
)


def _capture_box() -> str:
    return (
        '<div class="ledger-cap">'
        '<textarea id="ledger-cap-text" rows="2" aria-label="Note or decision" '
        'placeholder="Record a note or decision. Mention a ticker to link it. '
        '(Cmd/Ctrl+Enter to capture)"></textarea>'
        '<div class="ledger-cap-row">'
        '<button type="button" class="k-btn k-btn-primary k-btn-sm" id="ledger-cap-btn">Capture</button>'
        '<span class="ledger-cap-status" id="ledger-cap-status" role="status" aria-live="polite"></span>'
        "</div></div>" + LEDGER_CAPTURE_JS
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
    ctx: dict[str, object] = row.context or {}
    channel = str(ctx.get("channel") or "")
    chan = f'<span class="ledger-chan">{escape(channel)}</span>' if channel else ""
    when = stamp_html(row.created_at, css="ledger-when")
    chips = ""
    if row.ticker:
        ident = ticker_label(row.ticker)
    elif ctx.get("needs_ticker"):
        cands: object = ctx.get("ticker_candidates")
        names = (
            ", ".join(escape(str(c)) for c in cast("list[object]", cands))
            if isinstance(cands, list)
            else ""
        )
        label = f"needs ticker: {names}" if names else "needs ticker"
        ident = f'<span class="ledger-needs">{label}</span>'
        chips = _ticker_candidate_chips(ctx.get("ticker_candidates"))
    else:
        ident = '<span class="ledger-unattr">unattributed</span>'
    return (
        f'<div class="ledger-musing" data-note-id="{row.id}">'
        f'<div class="ledger-musing-head">{ident}{chan}{when}</div>'
        f'<div class="ledger-body">{render_prose(row.body)}</div>'
        f"{chips}"
        "</div>"
    )


_RECONCILE_VERDICTS: tuple[tuple[str, str, str, str], ...] = (
    ("live", "Still live", "k-btn-primary", "Keeps this current — the coach can still cite it."),
    (
        "superseded",
        "Superseded",
        "",
        "Marks this replaced by a newer view — retired from the coach's context.",
    ),
    (
        "resolved-rejected",
        "Rejected",
        "k-btn-danger",
        "Marks this rejected — retired from the coach's context.",
    ),
    ("done", "Played out", "", "Marks this played out — retired from the coach's context."),
)

_RECONCILE_JS = """<script>(function(){
  if(window.__ledgerReconcileWired){ return; }
  window.__ledgerReconcileWired = true;
  var reloadGeneration = 0;
  var reloadController = null;
  function reload(){
    var request = ++reloadGeneration;
    if(reloadController){ reloadController.abort(); }
    reloadController = new AbortController();
    (window.uiFetch || fetch)('/api/panel/musings?fragment=reconcile', {signal:reloadController.signal})
      .then(function(r){ if(!r.ok){ throw new Error('Read unavailable'); } return r.text(); })
      .then(function(h){
        if(request !== reloadGeneration){ return; }
        var el=document.getElementById('ledger-reconcile'); if(el){ el.outerHTML=h; }
      }).catch(function(){
        if(request !== reloadGeneration){ return; }
        var el=document.getElementById('ledger-reconcile'); if(!el){ return; }
        var prior=el.querySelector('[data-refresh-error]'); if(prior){ prior.remove(); }
        var feedback=document.createElement('div'); feedback.className='k-well';
        feedback.setAttribute('data-refresh-error',''); feedback.setAttribute('role','status');
        feedback.textContent='Could not refresh. Previous entries remain visible. ';
        var retry=document.createElement('button'); retry.type='button';
        retry.className='k-btn k-btn-quiet k-btn-sm'; retry.textContent='Retry refresh';
        retry.addEventListener('click',reload); feedback.appendChild(retry); el.prepend(feedback);
      });
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
    // The card's OWN action row (Ratify/Rewrite/Drop, or the lone "Add
    // falsifier" trigger) is a sibling of .ledger-editable-body, never
    // touched by the body swap below — captured BEFORE the swap so it can't
    // be confused with the new Save/Cancel row the swap injects INSIDE body
    // (same .ledger-cap-row class, different node). Left visible, it stayed
    // clickable-but-inert mid-edit (e.g. re-clicking "Add falsifier" no-ops
    // on the data-editing guard) — indistinguishable from "nothing happened"
    // to the owner, who has no way to tell Save from the dead duplicate.
    var triggerRow=card.querySelector('.ledger-cap-row');
    card.setAttribute('data-editing','1');
    var original=body.innerHTML;
    var current=body.textContent||'';
    if(triggerRow){ triggerRow.hidden=true; }
    body.innerHTML=
      '<textarea class="ledger-rewrite-ta" rows="3">'+esc(current)+'</textarea>'
      +'<div class="ledger-cap-row">'
      +'<button type="button" class="k-btn k-btn-primary k-btn-sm" data-rewrite-save>Save</button>'
      +'<button type="button" class="k-btn k-btn-quiet k-btn-sm" data-rewrite-cancel>Cancel</button>'
      +'</div>';
    function restore(){
      body.innerHTML=original;
      card.removeAttribute('data-editing');
      if(triggerRow){ triggerRow.hidden=false; }
    }
    var ta=body.querySelector('.ledger-rewrite-ta');
    if(ta){ ta.focus(); ta.setSelectionRange(ta.value.length, ta.value.length); }
    body.querySelector('[data-rewrite-cancel]').addEventListener('click', restore);
    body.querySelector('[data-rewrite-save]').addEventListener('click', function(){
      var txt=(ta&&ta.value||'').trim();
      if(!txt){ if(ta){ ta.focus(); } return; }
      var saveBtn=this;
      saveBtn.disabled=true;
      var recId=card.getAttribute('data-rec-card');
      fetch('/api/reconcile/falsifier/'+recId,
            {method:'POST',headers:{'Content-Type':'application/json'},
             body:JSON.stringify({action:'edit', text:txt})})
        .then(function(r){ return r.json().catch(function(){ return null; }).then(function(j){ return {ok:r.ok, body:j}; }); })
        .then(function(res){
          var text=(res.body && res.body.receipt) || (res.ok ? 'Saved.' : "Didn't save — retry.");
          if(res.body && res.body.receipt){ showReceipt(res.body.receipt); }
          if(window.__ledgerEmitSettled){ window.__ledgerEmitSettled(saveBtn, res.ok, text); }
          if(res.ok){ reload(); } else { saveBtn.disabled=false; }
        })
        .catch(function(){
          saveBtn.disabled=false;
          if(window.__ledgerEmitSettled){ window.__ledgerEmitSettled(saveBtn, false, 'Could not reach the server.'); }
        });
    });
  }
  document.addEventListener('click', function(e){
    var v=e.target.closest('[data-rec-verdict]');
    if(v){
      CCAction.busy(v);
      fetch('/api/reconcile/'+v.getAttribute('data-rec-kind')+'/'+v.getAttribute('data-rec-id')
            +'/'+v.getAttribute('data-rec-verdict'),{method:'POST'})
        .then(function(r){ return r.json().catch(function(){ return null; }).then(function(j){ return {ok:r.ok, body:j}; }); })
        .then(function(res){
          var text=(res.body && res.body.receipt) || (res.ok ? 'Saved.' : "Didn't save — retry.");
          if(window.__ledgerEmitSettled){ window.__ledgerEmitSettled(v, res.ok, text); }
          if(res.ok){ reload(); } else { CCAction.release(v); }
        })
        .catch(function(){
          CCAction.release(v);
          if(window.__ledgerEmitSettled){ window.__ledgerEmitSettled(v, false, 'Could not reach the server.'); }
        });
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
      CCAction.busy(f);
      fetch('/api/reconcile/falsifier/'+f.getAttribute('data-rec-id'),
            {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action:action})})
        .then(function(r){ return r.json().catch(function(){ return null; }).then(function(j){ return {ok:r.ok, body:j}; }); })
        .then(function(res){
          var text=(res.body && res.body.receipt) || (res.ok ? 'Saved.' : "Didn't save — retry.");
          if(res.body && res.body.receipt){ showReceipt(res.body.receipt); }
          if(window.__ledgerEmitSettled){ window.__ledgerEmitSettled(f, res.ok, text); }
          if(res.ok){ reload(); } else { CCAction.release(f); }
        })
        .catch(function(){
          CCAction.release(f);
          if(window.__ledgerEmitSettled){ window.__ledgerEmitSettled(f, false, 'Could not reach the server.'); }
        });
    }
  });
})();</script>"""


def _missing_falsifier_card(gap: ReconcileItem) -> str:
    """One 'Add falsifier' gap card — its OWN ``data-rec-card`` with an empty
    editable body, so 'Add falsifier' opens the same in-card editor the ratify
    queue uses and Save POSTs ``{action:'edit', text}`` to
    /api/reconcile/falsifier/<decision_id>. Shared by ``_missing_falsifier_line``
    and the active reconcile list.

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
        '<p class="ledger-consequence">This position has no tripwire — write the condition that '
        "would prove the decision wrong and it arms one.</p>"
        '<div class="ledger-cap-row">'
        '<button type="button" class="k-btn k-btn-sm k-btn-primary" '
        f'data-falsifier-action="edit" data-rec-id="{gap.item_id}" '
        'title="Write and save the falsifier condition — arms a tripwire once saved.">'
        "Add falsifier</button>"
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
    card, or a note one-tap verdict card. Carries its own
    ``data-rec-card``/``data-rec-id`` + action hooks and NO wrapping div or
    ``id`` — shared by ``render_reconcile_list`` (inside the one
    ``#ledger-reconcile`` container). ``_RECONCILE_JS``'s document-level click
    delegation fires for these buttons."""
    if item.kind == "falsifier":
        buttons = "".join(
            f'<button type="button" class="k-btn k-btn-sm {cls}" '
            f'data-falsifier-action="{action}" data-rec-id="{item.item_id}" '
            f'title="{escape(hint, quote=True)}">{label}</button>'
            for action, label, cls, hint in (
                (
                    "ratify",
                    "Ratify as mine",
                    "k-btn-primary",
                    "Arms a tripwire — the coach can quote this and you'll be alerted if it triggers.",
                ),
                (
                    "edit",
                    "Rewrite",
                    "",
                    "Put this falsifier in your own words before it can be ratified.",
                ),
                (
                    "drop",
                    "Drop",
                    "k-btn-danger",
                    "Removes this falsifier — no tripwire will watch this decision.",
                ),
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
            '<p class="ledger-consequence"><strong>Ratify -&gt;</strong> arms a tripwire that alerts '
            "you if it triggers. <strong>Drop -&gt;</strong> no tripwire watches this decision.</p>"
            f'<div class="ledger-cap-row">{buttons}</div></div>'
        )
    rec_kind = "note"
    buttons = "".join(
        f'<button type="button" class="k-btn k-btn-sm {cls}" '
        f'data-rec-kind="{rec_kind}" data-rec-id="{item.item_id}" '
        f'data-rec-verdict="{verdict}" title="{escape(hint, quote=True)}">{label}</button>'
        for verdict, label, cls, hint in _RECONCILE_VERDICTS
    )
    tag = item.source_ref or item.label
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


# One guarded listener for the set-ticker chips (PR9): POSTs the new
# set_ticker lifecycle action, then re-fetches the list fragment — the
# existing list-refresh path LEDGER_CAPTURE_JS already uses after a capture.
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
        var list=document.getElementById('ledger-list');
        if(list){ refreshLedgerFragment(list,'/api/panel/musings?fragment=list'); }
      })
      .catch(function(){ btn.disabled=false; });
  });
})();</script>"""


_SET_TICKER_JS = _SET_TICKER_JS.replace("(function(){", "(function(){" + _FRAGMENT_REFRESH_JS, 1)


def render_ledger_list(db_path: Path | str | None, *, user_id: str = DEFAULT_USER_ID) -> str:
    """The musings list fragment (re-fetched after a capture)."""
    rows = list_notes(user_id=user_id, kind="musing", db_path=db_path, limit=200)
    if not rows:
        return (
            '<div id="ledger-list"><p class="ledger-empty">No notes yet - capture a '
            "thought above, or record it through a project agent.</p></div>"
        )
    body = "".join(_musing_card(r) for r in rows)
    return f'<div id="ledger-list">{body}</div>{_SET_TICKER_JS}'


def render_ledger_panel(
    db_path: Path | str | None, *, user_id: str = DEFAULT_USER_ID, embedded: bool = False
) -> str:
    """Record an owner note and show dated captures without a coaching queue."""
    return panel_section(
        "" if embedded else "<h2>Notes</h2>",
        f'<div id="ledger-jump-capture">{_capture_box()}</div>',
        f'<div id="ledger-jump-musings">{render_ledger_list(db_path, user_id=user_id)}</div>',
        style=_PANEL_STYLE,
    )
