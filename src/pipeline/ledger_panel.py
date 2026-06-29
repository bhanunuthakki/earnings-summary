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
    """The Ledger tab: capture box + newest-first musings."""
    return (
        _PANEL_STYLE + '<section class="panel"><h2>Ledger</h2>'
        '<p class="sub">Your captured stream of consciousness. Talk or type a musing - '
        "to your Telegram bot on the go, or here at the desk; it lands linked to a name "
        "and you read it back below.</p>"
        + _capture_box()
        + _stance_section(db_path)
        + '<h3 class="ledger-sec-h">Musings</h3>'
        + render_ledger_list(db_path, user_id=user_id)
        + "</section>"
    )
