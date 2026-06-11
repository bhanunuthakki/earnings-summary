"""The Home tab's Ask dock (Ask v4): the conversational engine, docked.

A collapsible chat dock pinned to the Home panel's bottom-right corner so a
question never requires leaving the landing tab. Same engine and SSE
contract as the Ask tab (``POST /api/ask/stream``: stage/delta/fragment/
final/citations/error frames); the dock renders a compact thread — prose
with citation chips, view fragments inline — and keeps a client-side
history tail for narrative continuity.

The ⇗ pop-out stashes that history under ``sessionStorage['cc-ask-thread']``
(plus any pending input under the palette's ``cc-ask-q`` key) and jumps to
``#explore`` — the Ask panel replays the turns at wire-up, so the
conversation continues in the full tab. Collapsed/open state persists in
``localStorage``. Markup is fully self-contained (own ids, own CSS, own
IIFE) and lives only in the Home panel's DOM, so it disappears with the
panel when another tab is active.
"""

from __future__ import annotations

_DOCK_CSS = """
.ask-dock { position:fixed; right:18px; bottom:14px; width:400px; max-width:calc(100vw - 36px);
  z-index:60; border:1px solid var(--border); border-radius:var(--radius);
  background:var(--surface); box-shadow:0 16px 48px rgba(0,0,0,0.55);
  display:flex; flex-direction:column; overflow:hidden; }
.ask-dock-head { display:flex; align-items:center; gap:8px; width:100%; padding:9px 12px;
  background:var(--paper, #1a1d23); border:none; cursor:pointer; text-align:left; }
.ask-dock-title { color:var(--accent); font-weight:600; font-size:var(--fs-body); }
.ask-dock-hint { color:var(--muted); font-size:var(--fs-caption); flex:1; overflow:hidden;
  white-space:nowrap; text-overflow:ellipsis; }
.ask-dock-pop { color:var(--muted); font-size:13px; padding:0 2px; text-decoration:none;
  cursor:pointer; }
.ask-dock-pop:hover { color:var(--accent); }
.ask-dock-body { display:flex; flex-direction:column; max-height:54vh; }
.ask-dock[data-collapsed="1"] .ask-dock-body { display:none; }
.ask-dock-thread { overflow-y:auto; padding:10px 12px; display:flex; flex-direction:column;
  gap:8px; min-height:60px; }
.ask-dock-empty { color:var(--muted); font-size:var(--fs-caption); }
.ask-dock-user { align-self:flex-end; max-width:80%; background:var(--accent-soft);
  border:1px solid var(--accent); color:var(--fg);
  border-radius:var(--radius) var(--radius) 4px var(--radius); padding:5px 10px;
  font-size:var(--fs-caption); }
.ask-dock-asst { align-self:stretch; border:1px solid var(--border);
  background:var(--paper, #1a1d23); border-radius:var(--radius); padding:8px 10px;
  font-size:var(--fs-caption); color:var(--fg); overflow-x:auto; }
.ask-dock-asst p { margin:0 0 6px; } .ask-dock-asst p:last-child { margin-bottom:0; }
.ask-dock-asst ul { margin:0 0 6px 16px; padding:0; }
.ask-dock-busy { color:var(--muted); font-size:var(--fs-caption); }
.ask-dock-busy .dots::after { content:'…'; animation: askdockdots 1.2s steps(4, end) infinite; }
@keyframes askdockdots { 0% { content:''; } 25% { content:'.'; } 50% { content:'..'; }
  75% { content:'...'; } }
.ask-dock-err { color:var(--bad); }
.ask-dock-cites { margin-top:6px; display:flex; gap:5px; flex-wrap:wrap; }
.ask-dock-cite { font-size:10.5px; color:var(--accent); border:1px solid var(--border);
  border-radius:var(--radius-full); padding:1px 7px; text-decoration:none; }
.ask-dock-cite:hover { border-color:var(--accent); }
.ask-dock-form { display:flex; gap:6px; padding:8px 10px; border-top:1px solid var(--border); }
.ask-dock-form input { flex:1; background:var(--paper, #1a1d23); color:var(--fg);
  border:1px solid var(--border); border-radius:var(--radius); padding:7px 10px;
  font-size:var(--fs-caption); }
.ask-dock-form input:focus { outline:none; border-color:var(--accent); }
.ask-dock-form button { background:var(--accent-soft); color:var(--accent);
  border:1px solid var(--accent); border-radius:var(--radius); padding:7px 12px;
  font-size:var(--fs-caption); cursor:pointer; }
"""

# Raw string: JS regexes and \n splits pass through verbatim.
_DOCK_JS = r"""
(function () {
  var dock = document.getElementById('ask-dock');
  if (!dock || dock.dataset.wired) return;
  dock.dataset.wired = '1';
  var thread = document.getElementById('ask-dock-thread');
  var form = document.getElementById('ask-dock-form');
  var input = document.getElementById('ask-dock-q');
  var toggle = document.getElementById('ask-dock-toggle');
  var pop = document.getElementById('ask-dock-pop');
  var history = [];
  var lastSpec = null;
  var busy = false;

  function esc(s) {
    return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  }
  function md(text) {
    var html = esc(text || '');
    html = html.replace(/`([^`]+)`/g, '<code>$1</code>');
    html = html.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
    html = html.replace(/(?:^|\n)[-*]\s+(.+)/g, '\n<li>$1</li>');
    html = html.replace(/(<li>[\s\S]+?<\/li>)+/g, function (b) { return '<ul>' + b + '</ul>'; });
    return html.split(/\n\n+/).map(function (p) {
      if (/^<(ul)/.test(p)) return p;
      return '<p>' + p.replace(/\n/g, '<br>') + '</p>';
    }).join('');
  }
  function remember(role, text) {
    history.push({role: role, text: String(text || '').slice(0, 1200)});
    if (history.length > 12) history = history.slice(-12);
  }
  function scroll() { thread.scrollTop = thread.scrollHeight; }
  function setOpen(open) {
    dock.dataset.collapsed = open ? '0' : '1';
    try { localStorage.setItem('askDockOpen', open ? '1' : '0'); } catch (e) {}
    if (open) input.focus();
  }
  toggle.addEventListener('click', function (ev) {
    if (ev.target === pop) return;
    setOpen(dock.dataset.collapsed === '1');
  });
  try { if (localStorage.getItem('askDockOpen') === '1') setOpen(true); } catch (e) {}

  // ⇗ pop-out: hand the dock conversation to the full Ask tab (same
  // sessionStorage contract as the shell palette's cc-ask-q handoff).
  pop.addEventListener('click', function (ev) {
    ev.stopPropagation();
    try {
      if (history.length) sessionStorage.setItem('cc-ask-thread', JSON.stringify(history));
      var pending = input.value.trim();
      if (pending) sessionStorage.setItem('cc-ask-q', pending);
    } catch (e) {}
    location.hash = '#explore';
    window.dispatchEvent(new Event('cc-ask-q'));
  });

  function citeRow(card, items) {
    var chips = (items || []).map(function (c) {
      var href = (c && (c.href || c.source_url)) || '';
      if (!href) return '';
      return '<a class="ask-dock-cite" href="' + esc(href) + '" target="_blank">['
        + esc(String(c.n)) + '] ' + esc(c.label || 'source') + '</a>';
    }).join('');
    if (!chips) return;
    var row = document.createElement('div');
    row.className = 'ask-dock-cites';
    row.innerHTML = chips;
    card.appendChild(row);
  }

  form.addEventListener('submit', function (ev) {
    ev.preventDefault();
    if (busy) return;
    var q = input.value.trim();
    if (!q) return;
    busy = true;
    input.value = '';
    var empty = thread.querySelector('.ask-dock-empty');
    if (empty) empty.remove();
    var user = document.createElement('div');
    user.className = 'ask-dock-user';
    user.textContent = q;
    thread.appendChild(user);
    remember('user', q);
    var card = document.createElement('div');
    card.className = 'ask-dock-asst';
    card.innerHTML = '<span class="ask-dock-busy">working<span class="dots"></span></span>';
    thread.appendChild(card);
    scroll();

    var prose = null;
    var proseText = '';
    var citations = [];
    var frag = null;
    var finalEv = null;
    var errored = false;

    function busyLine(t) {
      var b = card.querySelector('.ask-dock-busy');
      if (b) b.innerHTML = esc(t) + '<span class="dots"></span>';
    }
    function ensureProse() {
      if (prose) return prose;
      card.innerHTML = '';
      prose = document.createElement('div');
      card.appendChild(prose);
      return prose;
    }
    function handle(ev2) {
      if (ev2.type === 'stage') {
        busyLine(ev2.note || (ev2.stage === 'compiling' ? 'compiling the view'
          : ev2.stage === 'running' ? 'running the view' : 'researching'));
      } else if (ev2.type === 'delta') {
        ensureProse().textContent = (proseText += ev2.text || '');
        scroll();
      } else if (ev2.type === 'fragment') {
        frag = ev2;
      } else if (ev2.type === 'final') {
        finalEv = ev2;
      } else if (ev2.type === 'citations') {
        citations = ev2.items || [];
      } else if (ev2.type === 'error') {
        errored = true;
        card.innerHTML = '<span class="ask-dock-err">' + esc(ev2.error || 'failed — try again') + '</span>';
      }
    }
    function finish() {
      busy = false;
      if (errored) { scroll(); return; }
      if (frag) {
        lastSpec = frag.spec || lastSpec;
        var msg = (finalEv && finalEv.text) || 'done';
        card.innerHTML = '<div class="ask-dock-busy">' + esc(msg) + '</div>' + (frag.html || '');
        remember('assistant', msg);
      } else if (finalEv) {
        var text = finalEv.text || proseText || '';
        card.innerHTML = md(text);
        citeRow(card, citations);
        remember('assistant', text);
      } else {
        card.innerHTML = '<span class="ask-dock-err">no answer — try again</span>';
      }
      scroll();
    }

    fetch('/api/ask/stream', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({query: q, tickers: [], context_spec: lastSpec,
                            history: history.slice(0, -1)})
    }).then(function (r) {
      if (!r.ok || !r.body) throw new Error('HTTP ' + r.status);
      var reader = r.body.getReader();
      var decoder = new TextDecoder();
      var buffer = '';
      function pump() {
        return reader.read().then(function (res) {
          if (res.done) { finish(); return; }
          buffer += decoder.decode(res.value, {stream: true});
          var parts = buffer.split('\n\n');
          buffer = parts.pop();
          parts.forEach(function (frame) {
            var line = frame.replace(/^data:\s*/, '');
            if (!line) return;
            try { handle(JSON.parse(line)); } catch (e) {}
          });
          return pump();
        });
      }
      return pump();
    }).catch(function () {
      busy = false;
      card.innerHTML = '<span class="ask-dock-err">network error — try again</span>';
      scroll();
    });
  });
})();
"""


def render_ask_dock() -> str:
    """The dock fragment — markup + scoped CSS + its IIFE, ready to append
    to the Home panel."""
    return f"""<style>{_DOCK_CSS}</style>
<div class="ask-dock" id="ask-dock" data-collapsed="1">
  <button type="button" class="ask-dock-head" id="ask-dock-toggle">
    <span class="ask-dock-title">Ask</span>
    <span class="ask-dock-hint">tables for metric questions &middot; cited answers for open ones</span>
    <span class="ask-dock-pop" id="ask-dock-pop" title="Continue in the Ask tab">&#x21D7;</span>
  </button>
  <div class="ask-dock-body" id="ask-dock-body">
    <div class="ask-dock-thread" id="ask-dock-thread">
      <span class="ask-dock-empty">Ask about any tracked name without leaving Home.</span>
    </div>
    <form class="ask-dock-form" id="ask-dock-form">
      <input id="ask-dock-q" placeholder="Ask&hellip;" autocomplete="off">
      <button type="submit">Ask</button>
    </form>
  </div>
</div>
<script>{_DOCK_JS}</script>"""


__all__ = ["render_ask_dock"]
