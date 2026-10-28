"""The Worldview review surface (P2) for durable Tenets.

Rendered as a flag-gated section inside the Ledger panel. Shows current Tenets
and the owner control for adding or revising a Tenet. Behind ``LEDGER_WORLDVIEW``
(default off); returns "" when off.

Pure render + token-only styles (guard-clean). Writes go through the
``/api/tenets`` routes to ``synthesis.tenets``. Current Tenets may also enter
governed reasoning through the separately gated Worldview anchor.
"""

from __future__ import annotations

import os
from html import escape
from pathlib import Path
from typing import cast

from pipeline.analysis_styles import ANALYSIS_STYLE
from synthesis.insights import InsightRow
from synthesis.tenets import list_tenets
from ui.prose import render_prose

_ON = frozenset({"1", "true", "yes", "on"})


def worldview_enabled() -> bool:
    return os.environ.get("LEDGER_WORLDVIEW", "0").strip().lower() in _ON


_WORLDVIEW_JS = """<script>(function(){
  if(window.__worldviewWired){ return; }
  window.__worldviewWired = true;
  var reloadGeneration = 0;
  var reloadController = null;
  function reload(){
    var request = ++reloadGeneration;
    if(reloadController){ reloadController.abort(); }
    reloadController = new AbortController();
    (window.uiFetch || fetch)('/api/panel/musings?fragment=worldview', {signal:reloadController.signal})
      .then(function(r){ if(!r.ok){ throw new Error('Read unavailable'); } return r.text(); })
      .then(function(h){
        if(request !== reloadGeneration){ return; }
        var el=document.getElementById('worldview'); if(el){ el.outerHTML=h; }
      }).catch(function(){
        if(request !== reloadGeneration){ return; }
        var el=document.getElementById('worldview'); if(!el){ return; }
        var prior=el.querySelector('[data-refresh-error]'); if(prior){ prior.remove(); }
        var feedback=document.createElement('div'); feedback.className='k-well';
        feedback.setAttribute('data-refresh-error',''); feedback.setAttribute('role','status');
        feedback.textContent='Could not refresh. Previous entries remain visible. ';
        var retry=document.createElement('button'); retry.type='button';
        retry.className='k-btn k-btn-quiet k-btn-sm'; retry.textContent='Retry refresh';
        retry.addEventListener('click',reload); feedback.appendChild(retry); el.prepend(feedback);
      });
  }
  document.addEventListener('click', function(e){
    var add=e.target.closest('#wv-add-btn');
    if(add){
      var ta=document.getElementById('wv-tenet-text');
      var sc=document.getElementById('wv-tenet-scope');
      var st=document.getElementById('wv-status');
      var body=(ta&&ta.value||'').trim(); if(!body){ if(ta){ ta.focus(); } return; }
      CCAction.busy(add); if(st){ st.textContent='Adding...'; }
      fetch('/api/tenets',{method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({body_md:body, scope_key:(sc&&sc.value||'').trim()})})
        .then(function(r){ return r.json(); }).then(function(){ reload(); })
        .catch(function(){ CCAction.release(add); if(st){ st.textContent='Could not reach the server.'; } });
      return;
    }
    var act=e.target.closest('[data-tenet-action]');
    if(act){
      var card=act.closest('[data-tenet-id]'); if(!card){ return; }
      CCAction.busy(act);
      fetch('/api/tenets/'+card.getAttribute('data-tenet-id')+'/'+act.getAttribute('data-tenet-action'),
        {method:'POST'})
        .then(function(r){ return r.json().catch(function(){ return null; }).then(function(j){ return {ok:r.ok, body:j}; }); })
        .then(function(res){
          var text=(res.body && res.body.receipt) || (res.ok ? 'Saved.' : "Didn't save — retry.");
          if(window.__ledgerEmitSettled){ window.__ledgerEmitSettled(act, res.ok, text); }
          if(res.ok){ reload(); } else { CCAction.release(act); }
        })
        .catch(function(){
          CCAction.release(act);
          if(window.__ledgerEmitSettled){ window.__ledgerEmitSettled(act, false, 'Could not reach the server.'); }
        });
    }
  });
})();</script>"""


def _from_n(t: InsightRow) -> str:
    n = len(t.source_note_ids)
    return f"from {n} musing{'' if n == 1 else 's'}" if n else "owner-stated"


def _tenets_in_tension(t: InsightRow) -> list[int]:
    raw = t.meta.get("tensions")
    items = cast("list[object]", raw) if isinstance(raw, list) else []
    return [x for x in items if isinstance(x, int)]


def _current_card(t: InsightRow) -> str:
    prov = "you" if t.provenance == "owner" else "distilled"
    return (
        '<div class="ledger-stance">'
        '<div class="ledger-stance-head">'
        f'<span class="wv-scope">{escape(t.scope_key)}</span>'
        f'<span class="wv-prov">{prov}</span>'
        f'<span class="ledger-stance-meta">{_from_n(t)}</span>'
        "</div>"
        f'<div class="ledger-body">{render_prose(t.body_md)}</div>'
        "</div>"
    )


def render_proposed_tenet_card(t: InsightRow) -> str:
    tension = _tenets_in_tension(t)
    tension_badge = '<span class="wv-tension">tension</span>' if tension else ""
    tension_note = (
        '<div class="wv-tension-note">Overlaps a standing Tenet on this topic — '
        "approving revises it (supersede chain).</div>"
        if tension
        else ""
    )
    return (
        f'<div class="ledger-musing" data-tenet-id="{t.id}">'
        '<div class="ledger-musing-head">'
        f'<span class="wv-scope">{escape(t.scope_key)}</span>'
        f'{tension_badge}<span class="k-chip k-chip-warn wv-proposed-badge">proposed</span>'
        f'<span class="ledger-stance-meta">{_from_n(t)}</span></div>'
        f'<div class="ledger-body">{render_prose(t.body_md)}</div>'
        f"{tension_note}"
        '<p class="ledger-consequence"><strong>Approve -&gt;</strong> becomes a standing soft '
        "prior in your decision prompts. <strong>Reject -&gt;</strong> retired, not adopted.</p>"
        '<div class="ledger-cap-row">'
        '<button type="button" class="k-btn k-btn-sm k-btn-primary" '
        'data-tenet-action="approve" title="Adopts this as a standing Tenet — a durable belief '
        'the coach injects into future advice.">Approve</button>'
        '<button type="button" class="k-btn k-btn-sm k-btn-danger" '
        'data-tenet-action="reject" title="Retires this proposed Tenet — it will not be '
        'adopted.">Reject</button>'
        "</div></div>"
    )


def _add_box() -> str:
    return (
        '<div class="wv-add">'
        '<textarea id="wv-tenet-text" rows="2" '
        "placeholder=\"A belief about HOW you invest — e.g. 'I sell my winners too early; "
        "let a working thesis run.'\"></textarea>"
        '<input id="wv-tenet-scope" type="text" '
        'placeholder="topic slug (optional) — e.g. exit-discipline; reuse one to revise" />'
        '<div class="wv-add-row">'
        '<button type="button" class="k-btn k-btn-primary k-btn-sm" id="wv-add-btn">Add Tenet</button>'
        '<span class="wv-status" id="wv-status"></span>'
        "</div></div>"
    )


def render_worldview_body(db_path: Path | str | None) -> str:
    """The inner Worldview content (re-fetched after every action)."""
    proposed = list_tenets(status="proposed", db_path=db_path)
    current = list_tenets(status="current", db_path=db_path)
    parts: list[str] = ['<div id="worldview">', _add_box()]
    if proposed:
        parts.append('<h4 class="ledger-sec-h">Proposed — approve to adopt</h4>')
        parts.append("".join(render_proposed_tenet_card(t) for t in proposed))
    if current:
        parts.append('<h4 class="ledger-sec-h">Your Worldview</h4>')
        parts.append("".join(_current_card(t) for t in current))
    elif not proposed:
        parts.append('<p class="ledger-empty">No Tenets yet. Add a process Tenet above.</p>')
    parts.append("</div>")
    return "".join(parts)


_WORLDVIEW_TUTORIAL = "Standing process Tenets. Revisions preserve prior versions."


def render_worldview_section(db_path: Path | str | None) -> str:
    """The Worldview section — empty string when ``LEDGER_WORLDVIEW`` is off.

    The tutorial appears as a visible paragraph when there are no Tenets.
    Existing Tenets move it into the heading's title."""
    if not worldview_enabled():
        return ""
    body = render_worldview_body(db_path)
    if "ledger-empty" in body:
        heading = '<h3 class="ledger-sec-h">Worldview</h3>'
        sub = f'<p class="ledger-sec-sub">{escape(_WORLDVIEW_TUTORIAL)}</p>'
    else:
        heading = f'<h3 class="ledger-sec-h" title="{escape(_WORLDVIEW_TUTORIAL, quote=True)}">Worldview</h3>'
        sub = ""
    return ANALYSIS_STYLE + heading + sub + body + _WORLDVIEW_JS
