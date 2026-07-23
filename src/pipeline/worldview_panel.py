"""The Worldview review surface (P2) — the durable Tenets, not yet injected.

Rendered as a flag-gated section inside the Ledger panel (front-of-funnel capture
→ durable belief in one place). Shows the live Worldview (current Tenets), the
approval queue (machine-distilled ``proposed`` Tenets with one-tap approve/reject),
distill-time **tensions**, plus an owner "add a Tenet" box and a "Distill from
flagged musings" tap. Behind ``LEDGER_WORLDVIEW`` (default off); returns "" when
off, so the Ledger tab is unchanged until the owner enables it.

Pure render + token-only styles (guard-clean). All writes go through the
``/api/tenets`` routes → ``synthesis.tenets`` / ``synthesis.tenet_distill``.
"""

from __future__ import annotations

import os
from datetime import timedelta
from html import escape
from pathlib import Path
from typing import cast

from synthesis.insights import InsightRow, list_insights
from synthesis.tenets import list_tenets
from ui.controls import chip_tone_class
from ui.prose import render_prose
from user_state._db import now_naive_utc, parse_dt

_ON = frozenset({"1", "true", "yes", "on"})

# The "Adopted this week" strip (B4): a Tenet/stance that landed 'current'
# straight from a session distill (no owner tap) within this trailing window.
_ADOPTED_PROVENANCE = "session_distill"
_ADOPTED_WINDOW_DAYS = 7


def worldview_enabled() -> bool:
    return os.environ.get("LEDGER_WORLDVIEW", "0").strip().lower() in _ON


_WORLDVIEW_STYLE = """<style>
.wv-add { background: var(--surface); border-radius: var(--radius); padding: var(--sp-3) var(--sp-4); margin-bottom: var(--sp-3); }
.wv-add textarea { width: 100%; min-height: 48px; resize: vertical; font-family: var(--sans); font-size: var(--fs-body); }
.wv-add input { width: 100%; margin-top: var(--sp-2); font-family: var(--sans); font-size: var(--fs-caption); }
.wv-add-row { display: flex; align-items: center; gap: var(--sp-2); margin-top: var(--sp-2); flex-wrap: wrap; }
.wv-status { font-size: var(--fs-caption); color: var(--muted); }
.wv-scope { font-family: var(--mono); font-size: var(--fs-caption); color: var(--muted); text-transform: lowercase; }
.wv-prov { font-size: var(--fs-caption); color: var(--muted); text-transform: uppercase; letter-spacing: 0.05em; }
.wv-proposed-badge { font-size: var(--fs-caption); font-weight: 600; color: var(--accent); text-transform: uppercase; letter-spacing: 0.05em; }
.wv-tension { font-size: var(--fs-caption); font-weight: 600; color: var(--warn); text-transform: uppercase; letter-spacing: 0.05em; }
.wv-tension-note { font-size: var(--fs-caption); color: var(--warn); margin-top: var(--sp-1); }
</style>"""

_WORLDVIEW_JS = """<script>(function(){
  if(window.__worldviewWired){ return; }
  window.__worldviewWired = true;
  function reload(){
    fetch('/api/panel/musings?fragment=worldview').then(function(r){return r.text();})
      .then(function(h){ var el=document.getElementById('worldview'); if(el){ el.outerHTML=h; } });
  }
  document.addEventListener('click', function(e){
    var add=e.target.closest('#wv-add-btn');
    if(add){
      var ta=document.getElementById('wv-tenet-text');
      var sc=document.getElementById('wv-tenet-scope');
      var st=document.getElementById('wv-status');
      var body=(ta&&ta.value||'').trim(); if(!body){ if(ta){ ta.focus(); } return; }
      add.disabled=true; if(st){ st.textContent='Adding...'; }
      fetch('/api/tenets',{method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({body_md:body, scope_key:(sc&&sc.value||'').trim()})})
        .then(function(r){ return r.json(); }).then(function(){ reload(); })
        .catch(function(){ add.disabled=false; if(st){ st.textContent='Could not reach the server.'; } });
      return;
    }
    var distill=e.target.closest('#wv-distill-btn');
    if(distill){
      var st2=document.getElementById('wv-status');
      distill.disabled=true; if(st2){ st2.textContent='Distilling from flagged musings...'; }
      fetch('/api/tenets/distill',{method:'POST'})
        .then(function(r){ return r.json(); })
        .then(function(res){ if(st2&&res){ st2.textContent=(res.proposed||0)+' proposed from '+(res.candidates||0)+' flagged.'; } reload(); })
        .catch(function(){ distill.disabled=false; if(st2){ st2.textContent='Distill failed.'; } });
      return;
    }
    var act=e.target.closest('[data-tenet-action]');
    if(act){
      var card=act.closest('[data-tenet-id]'); if(!card){ return; }
      act.disabled=true;
      fetch('/api/tenets/'+card.getAttribute('data-tenet-id')+'/'+act.getAttribute('data-tenet-action'),
        {method:'POST'})
        .then(function(r){ return r.json().catch(function(){ return null; }).then(function(j){ return {ok:r.ok, body:j}; }); })
        .then(function(res){
          var text=(res.body && res.body.receipt) || (res.ok ? 'Saved.' : "Didn't save — retry.");
          if(window.__ledgerEmitSettled){ window.__ledgerEmitSettled(act, res.ok, text); }
          if(res.ok){ reload(); } else { act.disabled=false; }
        })
        .catch(function(){
          act.disabled=false;
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


def _accountability_chip(t: InsightRow) -> str:
    """Compact receipts chip on a current Tenet's card — "upheld 4 / violated
    2 · ~-$4.2k" — when the weekly accountability pass
    (``synthesis.tenet_accountability``) has a persisted verdict for this
    Tenet. Warn tone when at least one violation is recorded; empty string
    (no chip at all) when there's no verdict yet, or the verdict cites
    neither upheld nor violated decisions — "nothing to receipt yet" is not
    a status worth a chip."""
    acc_raw = t.meta.get("accountability")
    if not isinstance(acc_raw, dict):
        return ""
    acc = cast("dict[str, object]", acc_raw)
    upheld_raw = acc.get("upheld")
    violated_raw = acc.get("violated")
    upheld = len(upheld_raw) if isinstance(upheld_raw, list) else 0
    violated = len(violated_raw) if isinstance(violated_raw, list) else 0
    if not upheld and not violated:
        return ""
    tone = "warn" if violated > 0 else "ok"
    cost_txt = ""
    cost_raw = acc.get("est_cost_usd")
    if isinstance(cost_raw, (int, float)) and not isinstance(cost_raw, bool) and cost_raw != 0:
        cost = float(cost_raw)
        mag_txt = f"{abs(cost) / 1000:.1f}k" if abs(cost) >= 1000 else f"{abs(cost):,.0f}"
        cost_txt = f" &middot; ~{'-' if cost < 0 else '+'}${mag_txt}"
    return (
        f'<span class="k-chip{chip_tone_class(tone)}">upheld {upheld} / '
        f"violated {violated}{cost_txt}</span>"
    )


def _current_card(t: InsightRow) -> str:
    prov = "you" if t.provenance == "owner" else "distilled"
    chip = _accountability_chip(t)
    return (
        '<div class="ledger-stance">'
        '<div class="ledger-stance-head">'
        f'<span class="wv-scope">{escape(t.scope_key)}</span>'
        f'<span class="wv-prov">{prov}</span>'
        f'<span class="ledger-stance-meta">{_from_n(t)}</span>'
        f"{chip}</div>"
        f'<div class="ledger-body">{render_prose(t.body_md)}</div>'
        "</div>"
    )


def _proposed_card(t: InsightRow) -> str:
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
        f'{tension_badge}<span class="wv-proposed-badge">proposed</span>'
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


def _excerpt(text: str, limit: int = 140) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[:limit].rstrip() + "..."


def _adopted_this_week(db_path: Path | str | None) -> list[InsightRow]:
    """Current Tenet/stance rows auto-adopted from a session distill within
    the trailing window — the "receipts, not surprises" strip that pairs
    every silent auto-adopt with a visible, revertible card. A row with an
    unparseable ``as_of`` is skipped rather than crashing the panel."""
    cutoff = now_naive_utc() - timedelta(days=_ADOPTED_WINDOW_DAYS)
    found: list[InsightRow] = []
    for kind in ("tenet", "stance"):
        for row in list_insights(kind=kind, status="current", db_path=db_path):
            if row.provenance != _ADOPTED_PROVENANCE:
                continue
            try:
                as_of = parse_dt(row.as_of)
            except (ValueError, TypeError):
                continue
            if as_of >= cutoff:
                found.append(row)
    found.sort(key=lambda r: r.as_of, reverse=True)
    return found


def _adopted_card(t: InsightRow) -> str:
    return (
        f'<div class="ledger-stance" data-tenet-id="{t.id}">'
        '<div class="ledger-stance-head">'
        f'<span class="wv-scope">{escape(t.scope_key)}</span>'
        '<span class="k-chip k-chip-mono">from session</span>'
        f'<span class="ledger-stance-meta">since {escape(t.as_of[:10])}</span></div>'
        f'<div class="ledger-body">{render_prose(_excerpt(t.body_md))}</div>'
        '<div class="ledger-cap-row">'
        '<button type="button" class="k-btn k-btn-quiet k-btn-sm" data-tenet-action="revert" '
        'title="Revert — restores your prior belief">Revert</button>'
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
        '<button type="button" class="k-btn k-btn-sm" id="wv-distill-btn">'
        "Distill from flagged musings</button>"
        '<span class="wv-status" id="wv-status"></span>'
        "</div></div>"
    )


def render_worldview_body(db_path: Path | str | None) -> str:
    """The inner Worldview content (re-fetched after every action)."""
    proposed = list_tenets(status="proposed", db_path=db_path)
    current = list_tenets(status="current", db_path=db_path)
    adopted = _adopted_this_week(db_path)
    parts: list[str] = ['<div id="worldview">', _add_box()]
    if adopted:
        parts.append('<h4 class="ledger-sec-h">Adopted this week</h4>')
        parts.append("".join(_adopted_card(t) for t in adopted))
    if proposed:
        parts.append('<h4 class="ledger-sec-h">Proposed — approve to adopt</h4>')
        parts.append("".join(_proposed_card(t) for t in proposed))
    if current:
        parts.append('<h4 class="ledger-sec-h">Your Worldview</h4>')
        parts.append("".join(_current_card(t) for t in current))
    elif not proposed:
        parts.append(
            '<p class="ledger-empty">No Tenets yet — state a belief about how you invest '
            "above, or distil one from the musings you've flagged.</p>"
        )
    parts.append("</div>")
    return "".join(parts)


_WORLDVIEW_TUTORIAL = (
    "The durable beliefs about how you invest — Tenets — distilled from what's "
    "on your mind. You approve what the system proposes; contradictions with a "
    "standing Tenet are flagged, never overwritten."
)


def render_worldview_section(db_path: Path | str | None) -> str:
    """The Worldview section — empty string when ``LEDGER_WORLDVIEW`` is off.

    PR9 "no section ceremony": the tutorial sentence is a visible ``<p>`` only
    while there are no Tenets yet (proposed or current) — once real Tenets
    exist it folds into the heading's ``title=`` instead of repeating on
    every visit."""
    if not worldview_enabled():
        return ""
    body = render_worldview_body(db_path)
    if "ledger-empty" in body:
        heading = '<h3 class="ledger-sec-h">Worldview</h3>'
        sub = f'<p class="ledger-sec-sub">{escape(_WORLDVIEW_TUTORIAL)}</p>'
    else:
        heading = f'<h3 class="ledger-sec-h" title="{escape(_WORLDVIEW_TUTORIAL, quote=True)}">Worldview</h3>'
        sub = ""
    return _WORLDVIEW_STYLE + heading + sub + body + _WORLDVIEW_JS
