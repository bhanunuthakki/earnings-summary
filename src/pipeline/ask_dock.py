"""The persistent Ask dock (Ask v5): the conversational engine, docked in
the shell chrome.

A chat dock rendered ONCE into the command-center shell's body — outside the
``.cc-panels`` panel-swap container — so the conversation survives tab
switches instead of vanishing with the Home panel. Three states, with
explicit header controls, persisted via the shared client store
(``CCState`` key ``dockMode``; the pre-S14 ``askDockMode`` /
boolean ``askDockOpen`` keys migrate on first read):

* **min** — a slim bottom-right pill (just the Ask title; ▁ minimizes,
  clicking the pill restores the last expanded state);
* **float** — the 400px floating card pinned bottom-right (the classic dock);
* **split** (◫) — a fixed right-side column under the top bar; the shell
  reflows beside it (``body[data-ask-split="1"] .cc-panels`` gains a right
  margin over the standard ``--transition`` timing), a side-by-side copilot
  usable while browsing any panel or report. Esc (and the x control) exit
  split — but the shell's overlays (palette, peek, hover card, drawers) keep
  first claim on Escape *structurally*: the dock registers with
  ``window.CCOverlay`` (S4) at the lowest priority, so its dismissal resolves
  only after every shell overlay. ``scrim:false`` is a declared carve-out (a
  page-darkening scrim would defeat a side-by-side copilot — same rationale as
  the report comments sidebar). There is no second ``keydown`` listener and no
  hardcoded overlay-id registry anymore; the open-surface stack owns Escape.

Same engine and SSE contract as the Ask tab (``POST /api/ask/stream``:
stage/delta/fragment/final/citations/error/session frames); the dock renders
a compact thread — prose with citation chips, view fragments inline — and
uses server-side session persistence when available.  Grounded answers get
the shared inline cite marks (``ui.cite_marks``): deltas stream as plain
text, then the trailing citations event re-renders the finished prose with
superscript [n] chips whose popover carries the evidence label + S2
confidence %, plus an "⚠ N unverified" chip when the claim audit flagged
unsupported claims.  The ``⇿`` button opens a thread-list overlay: resume,
rename (double-click), or delete past threads; a **New thread** button
starts a fresh session.

The ⇗ pop-out stashes history under the store's ``askThread`` key (plus any
pending input under the palette's ``askQ`` key), minimizes the dock, and
jumps to ``#explore`` — the Ask panel replays the turns at wire-up, so the
conversation continues in the full tab. The dock's own persistence
(``askSessionId`` server-thread id, ``askTail`` replay tail) rides the same
store; ``CC_STATE_JS`` is inlined by the shell before this dock's IIFE.

Stacking: the dock sits at z-index 35 — above the panels and the sticky top
bar (30), below the drawers (38/39), peek (44/45), hover card (46), and
palette (48/49) — so every shell overlay covers it, in split mode included.
Markup is fully self-contained (own ids, own CSS, own IIFE).
"""

from __future__ import annotations

from ui.cite_marks import CITE_MARKS_CSS, CITE_MARKS_JS

_DOCK_CSS = """
.ask-dock { position:fixed; right:18px; bottom:14px; bottom:calc(14px + env(safe-area-inset-bottom, 0px)); width:400px; max-width:calc(100vw - 36px);
  z-index:35; border:1px solid var(--border); border-radius:var(--radius);
  background:var(--surface); box-shadow:var(--shadow-pop);
  display:flex; flex-direction:column; overflow:hidden; }
.ask-dock-head { display:flex; align-items:center; gap:8px; width:100%; padding:9px 12px;
  background:var(--paper); cursor:pointer; }
.ask-dock-title { color:var(--accent); font-weight:600; font-size:var(--fs-body); }
.ask-dock-hint { color:var(--muted); font-size:var(--fs-caption); flex:1; overflow:hidden;
  white-space:nowrap; text-overflow:ellipsis; }
.ask-dock-ctl { color:var(--muted); font-size:var(--fs-body); padding:0 2px;
  text-decoration:none; cursor:pointer; background:none; border:none; font:inherit; line-height:1; }
.ask-dock-ctl:hover { color:var(--accent); }
.ask-dock-body { display:flex; flex-direction:column; max-height:54vh; max-height:54dvh; position:relative; }
/* min — the slim pill: title only, body folded, controls hidden (the pill
   itself is the restore control). */
.ask-dock[data-mode="min"] { width:auto; }
.ask-dock[data-mode="min"] .ask-dock-body,
.ask-dock[data-mode="min"] .ask-dock-hint,
.ask-dock[data-mode="min"] .ask-dock-ctl { display:none; }
/* split — a full-height right column under the top bar (the dock JS keeps
   --ask-dock-top synced to the topbar's measured height); the panels reflow
   beside it via the body[data-ask-split] rule below. */
.ask-dock[data-mode="split"] { top:var(--ask-dock-top, 52px); right:0; bottom:0; width:420px;
  max-width:100vw; border:none; border-left:1px solid var(--border); border-radius:0;
  box-shadow:var(--shadow-pop); }
.ask-dock[data-mode="split"] .ask-dock-body { flex:1; min-height:0; max-height:none; }
.ask-dock[data-mode="split"] .ask-dock-thread { flex:1; }
.ask-dock[data-mode="split"] .ask-dock-splitbtn { color:var(--accent); }
/* The shell reflow: the transition lives on the base state so entering AND
   exiting split animate over the standard timing. */
.cc-panels { transition: margin-right var(--transition); }
body[data-ask-split="1"] .cc-panels { margin-right:440px; }
/* Narrow screens: the column overlays instead of crushing the panels. */
@media (max-width: 900px) {
  body[data-ask-split="1"] .cc-panels { margin-right:0; }
}
.ask-dock-thread { overflow-y:auto; padding:10px 12px; display:flex; flex-direction:column;
  gap:8px; min-height:60px; }
.ask-dock-empty { color:var(--muted); font-size:var(--fs-caption); }
.ask-dock-user { align-self:flex-end; max-width:80%; background:var(--accent-soft);
  border:1px solid var(--accent); color:var(--fg);
  border-radius:var(--radius); padding:5px 10px;
  font-size:var(--fs-body); }
.ask-dock-asst { align-self:stretch; border:1px solid var(--border);
  background:var(--paper); border-radius:var(--radius); padding:8px 10px;
  font-size:var(--fs-body); color:var(--fg); overflow-x:auto; }
.ask-dock-asst p { margin:0 0 6px; } .ask-dock-asst p:last-child { margin-bottom:0; }
.ask-dock-asst ul { margin:0 0 6px 16px; padding:0; }
.ask-dock-busy { color:var(--muted); font-size:var(--fs-caption); }
.ask-dock-busy .dots::after { content:'…'; animation: askdockdots 1.2s steps(4, end) infinite; }
@keyframes askdockdots { 0% { content:''; } 25% { content:'.'; } 50% { content:'..'; }
  75% { content:'...'; } }
.ask-dock-err { color:var(--bad); }
.ask-dock-cites { margin-top:6px; display:flex; gap:5px; flex-wrap:wrap; }
.ask-dock-form { display:flex; gap:6px; padding:8px 10px; border-top:1px solid var(--border); }
/* Input + submit button skinned by the shared control kit (ui/controls.py):
   the input drops its font-size override so it inherits the --fs-body kit
   baseline and matches the .k-btn beside it (cf. explore_panel .ask-inputrow). */
.ask-dock-form input { flex:1; padding:7px 10px; }
/* Thread list overlay — covers the .ask-dock-body while open. */
.ask-dock-threads { position:absolute; inset:0; background:var(--surface);
  display:flex; flex-direction:column; z-index:1; }
.ask-dock-threads[hidden] { display:none; }
.ask-dock-threads-head { display:flex; align-items:center; gap:8px;
  padding:8px 12px; border-bottom:1px solid var(--border); background:var(--paper); }
.ask-dock-threads-title { font-size:var(--fs-caption); font-weight:600;
  color:var(--fg); flex:1; }
.ask-dock-threads-list { flex:1; overflow-y:auto; padding:6px 0; }
.ask-dock-thread-row { display:flex; align-items:center; gap:6px;
  padding:7px 12px; cursor:pointer; }
.ask-dock-thread-row:hover { background:var(--paper); }
.ask-dock-thread-row[data-active="1"] { background:var(--accent-soft); }
.ask-dock-thread-title { flex:1; font-size:var(--fs-caption); color:var(--fg);
  overflow:hidden; white-space:nowrap; text-overflow:ellipsis; }
.ask-dock-thread-date { font-size:var(--fs-caption); color:var(--muted);
  white-space:nowrap; flex-shrink:0; }
.ask-dock-thread-del { font-size:var(--fs-caption); color:var(--muted);
  background:none; border:none; cursor:pointer; padding:2px 4px;
  flex-shrink:0; line-height:1; }
.ask-dock-thread-del:hover { color:var(--bad); }
/* Layout only: the kit's <input> baseline (border / background / radius /
   focus ring) owns the chrome — no hand-set solid --accent border. */
.ask-dock-thread-rename { flex:1; font-size:var(--fs-caption); padding:2px 6px; }
.ask-dock-threads-empty { color:var(--muted); font-size:var(--fs-caption);
  padding:16px 12px; }
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
  var minBtn = document.getElementById('ask-dock-min');
  var splitBtn = document.getElementById('ask-dock-split');
  var threadsBtn = document.getElementById('ask-dock-threads-btn');
  var threadsPanel = document.getElementById('ask-dock-threads');
  var threadsList = document.getElementById('ask-dock-threads-list');
  var threadsClose = document.getElementById('ask-dock-threads-close');
  var newThreadBtn = document.getElementById('ask-dock-new-thread');
  // State rides the shared client store (S14 PR2): CCState keys — the
  // legacy cc-ask-dock-tail / cc-ask-session-id names migrate on first read.
  var TAIL_KEY = 'askTail';
  var SID_KEY = 'askSessionId';
  var history = [];
  var lastSpec = null;
  var busy = false;
  var expandedMode = 'float';  // the state a pill click restores
  var currentSessionId = null;

  // Restore session id from the store so a reload re-attaches to the same
  // thread without asking the server for a new one.
  currentSessionId = window.CCState.get(SID_KEY) || null;

  function esc(s) {
    return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  }
  // PINNED INLINE-SUBSET MIRROR of ui.prose.render_prose (the one server prose
  // boundary). This is the ONE sanctioned client renderer: Ask streams tokens
  // and threads cite-marks through this same string client-side, so it cannot
  // be server-rendered. Keep it in rough inline parity (code/bold/bullets/
  // paragraphs); the server side in src/ui/prose.py is canonical. Do NOT grow a
  // fourth markdown renderer — see directives/design_language.md "Rendered prose".
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
    window.CCState.setJSON(TAIL_KEY, history);
  }
  function scroll() { thread.scrollTop = thread.scrollHeight; }

  // Split mode pins the column under the top bar — measure it rather than
  // hardcode (the bar's height is content-driven and can wrap on resize).
  function syncTop() {
    var bar = document.querySelector('.cc-topbar');
    if (bar) dock.style.setProperty('--ask-dock-top', bar.offsetHeight + 'px');
  }

  // CCOverlay registration (S4). The dock is a PERSISTENT / gesture surface,
  // not a blocking modal: scrim:false (a page-darkening scrim would defeat a
  // side-by-side copilot — the same declared carve-out as the report comments
  // sidebar) and the LOWEST priority, so every shell overlay (palette / peek /
  // drawers) keeps first claim on Escape — STRUCTURALLY, replacing the old
  // hardcoded ['cc-palette', …] id registry. toggleHidden:false: the dock's
  // visibility is data-mode/CSS, never [hidden]. Escape (and the x) exit
  // split -> float via onClose.
  var splitOv = window.CCOverlay && window.CCOverlay.register(dock, {
    modal: true, priority: window.CCOverlay.PRIORITY.DOCK,
    scrim: false, trapFocus: false, restoreFocus: false,
    motion: 'none', toggleHidden: false, autofocus: false,
    closeId: 'ask-dock-close', wireClose: false,
    onClose: function () { if (dock.dataset.mode === 'split') setMode('float'); }
  });

  function setMode(mode, skipFocus) {
    if (mode !== 'float' && mode !== 'split') mode = 'min';
    var wasSplit = dock.dataset.mode === 'split';
    dock.dataset.mode = mode;
    if (mode !== 'min') expandedMode = mode;
    if (mode === 'split') {
      syncTop();
      document.body.setAttribute('data-ask-split', '1');
      if (splitOv) splitOv.open();
    } else {
      document.body.removeAttribute('data-ask-split');
      if (wasSplit && splitOv) splitOv.close();
    }
    window.CCState.set('dockMode', mode);
    if (mode !== 'min' && !skipFocus) input.focus();
  }
  window.addEventListener('resize', function () {
    if (dock.dataset.mode === 'split') syncTop();
  });

  // Header click toggles min <-> the last expanded state; the inline controls
  // (▁ / ◫ / ⇗ / ⇿) are explicit and excluded from the toggle.
  toggle.addEventListener('click', function (ev) {
    if (ev.target.closest && ev.target.closest('.ask-dock-ctl')) return;
    setMode(dock.dataset.mode === 'min' ? expandedMode : 'min');
  });
  minBtn.addEventListener('click', function (ev) {
    ev.stopPropagation();
    setMode('min');
  });
  splitBtn.addEventListener('click', function (ev) {
    ev.stopPropagation();
    setMode(dock.dataset.mode === 'split' ? 'float' : 'split');
  });
  // The x collapses one level: split -> float, float -> min. In split, Escape
  // resolves the same exit through CCOverlay's priority stack (this handler's
  // mode check defers to it), so the dock keeps NO second keydown listener and
  // NO hardcoded overlay-id registry — the stack decides who owns Escape.
  var closeBtn = document.getElementById('ask-dock-close');
  if (closeBtn) closeBtn.addEventListener('click', function (ev) {
    ev.stopPropagation();
    setMode(dock.dataset.mode === 'split' ? 'float' : 'min');
  });

  // ⇗ pop-out: hand the dock conversation to the full Ask tab (same store
  // contract as the shell palette's askQ handoff; the event NAME stays
  // 'cc-ask-q' — it's the poke contract, not a storage key), then minimize
  // so the thread doesn't render twice beside the Ask panel.
  pop.addEventListener('click', function (ev) {
    ev.stopPropagation();
    if (history.length) window.CCState.setJSON('askThread', history);
    var pending = input.value.trim();
    if (pending) window.CCState.set('askQ', pending);
    setMode('min', true);
    location.hash = '#explore';
    window.dispatchEvent(new Event('cc-ask-q'));
  });

  // ---------------------------------------------------------------------------
  // Thread list overlay (⇿)
  // ---------------------------------------------------------------------------

  function fmtDate(iso) {
    if (!iso) return '';
    try {
      var d = new Date(iso.replace(' ', 'T') + 'Z');
      var now = new Date();
      var diffMs = now - d;
      var diffDays = Math.floor(diffMs / 86400000);
      if (diffDays === 0) return 'today';
      if (diffDays === 1) return 'yesterday';
      if (diffDays < 7) return diffDays + 'd ago';
      return d.toLocaleDateString(undefined, {month: 'short', day: 'numeric'});
    } catch (e) { return ''; }
  }

  // The thread-list (⇿) is a contained sub-overlay over the dock body: no
  // scrim (it opaquely covers the body), but Escape now dismisses it through
  // CCOverlay — at a priority above the dock-split so Escape closes threads
  // first, then exits split. CCOverlay toggles its [hidden]; this drives the
  // close from the ⇿ / x controls (wireClose:false).
  var threadsOv = threadsPanel && window.CCOverlay && window.CCOverlay.register(threadsPanel, {
    modal: true, priority: window.CCOverlay.PRIORITY.DOCK + 5,
    scrim: false, trapFocus: false, restoreFocus: true,
    motion: 'none', closeId: 'ask-dock-threads-close', wireClose: false, autofocus: false
  });
  function openThreads() {
    if (threadsOv) threadsOv.open(); else threadsPanel.hidden = false;
    loadThreadsList();
  }
  function closeThreads() {
    if (threadsOv) threadsOv.close(); else threadsPanel.hidden = true;
  }

  threadsBtn.addEventListener('click', function (ev) {
    ev.stopPropagation();
    if (threadsOv ? threadsOv.isOpen() : !threadsPanel.hidden) { closeThreads(); return; }
    openThreads();
  });
  threadsClose.addEventListener('click', function (ev) {
    ev.stopPropagation();
    closeThreads();
  });

  function loadThreadsList() {
    threadsList.innerHTML = '';
    fetch('/api/ask/sessions?limit=40')
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (data) {
        if (!data || !data.sessions || !data.sessions.length) {
          threadsList.innerHTML = '<div class="ask-dock-threads-empty">No saved threads yet.</div>';
          return;
        }
        data.sessions.forEach(function (sess) {
          threadsList.appendChild(buildThreadRow(sess));
        });
      })
      .catch(function () {
        threadsList.innerHTML = '<div class="ask-dock-threads-empty">Could not load threads.</div>';
      });
  }

  function buildThreadRow(sess) {
    var row = document.createElement('div');
    row.className = 'ask-dock-thread-row';
    row.dataset.sid = sess.id;
    if (sess.id === currentSessionId) row.dataset.active = '1';

    var titleEl = document.createElement('span');
    titleEl.className = 'ask-dock-thread-title';
    titleEl.textContent = sess.title || '(untitled)';
    titleEl.title = 'Double-click to rename';

    var dateEl = document.createElement('span');
    dateEl.className = 'ask-dock-thread-date';
    dateEl.textContent = fmtDate(sess.updated_at);

    var delBtn = document.createElement('button');
    delBtn.className = 'ask-dock-thread-del';
    delBtn.type = 'button';
    delBtn.title = 'Delete thread';
    delBtn.innerHTML = '&#x2715;';

    row.appendChild(titleEl);
    row.appendChild(dateEl);
    row.appendChild(delBtn);

    // Resume on click (but not on the delete button).
    row.addEventListener('click', function (ev) {
      if (ev.target === delBtn) return;
      resumeThread(sess.id);
    });

    // Inline rename on double-click.
    titleEl.addEventListener('dblclick', function (ev) {
      ev.stopPropagation();
      startInlineRename(row, titleEl, sess.id);
    });

    delBtn.addEventListener('click', function (ev) {
      ev.stopPropagation();
      deleteThread(sess.id, row);
    });

    return row;
  }

  function startInlineRename(row, titleEl, sid) {
    var inp = document.createElement('input');
    inp.className = 'ask-dock-thread-rename';
    inp.value = titleEl.textContent === '(untitled)' ? '' : titleEl.textContent;
    inp.placeholder = 'Thread title';
    row.replaceChild(inp, titleEl);
    inp.focus();
    inp.select();

    function commit() {
      var newTitle = inp.value.trim();
      if (!newTitle) { row.replaceChild(titleEl, inp); return; }
      fetch('/api/ask/sessions/' + encodeURIComponent(sid), {
        method: 'PATCH',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({title: newTitle})
      }).then(function (r) {
        if (r.ok) titleEl.textContent = newTitle;
        row.replaceChild(titleEl, inp);
      }).catch(function () { row.replaceChild(titleEl, inp); });
    }
    inp.addEventListener('blur', commit);
    inp.addEventListener('keydown', function (ev) {
      if (ev.key === 'Enter') { ev.preventDefault(); inp.blur(); }
      if (ev.key === 'Escape') { row.replaceChild(titleEl, inp); }
    });
  }

  function resumeThread(sid) {
    closeThreads();
    fetch('/api/ask/sessions/' + encodeURIComponent(sid))
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (data) {
        if (!data) return;
        currentSessionId = sid;
        window.CCState.set(SID_KEY, sid);
        history = [];
        window.CCState.del(TAIL_KEY);
        // Render the stored turns.
        thread.innerHTML = '';
        var turns = data.turns || [];
        if (!turns.length) {
          thread.innerHTML = '<span class="ask-dock-empty">Ask about any tracked name without leaving this tab.</span>';
          return;
        }
        turns.forEach(function (t) {
          var el = document.createElement('div');
          if (t.role === 'user') {
            el.className = 'ask-dock-user';
            el.textContent = t.text;
            remember('user', t.text);
          } else {
            el.className = 'ask-dock-asst';
            el.innerHTML = linkifyProse(md(t.text), t.citations || []);
            citeRow(el, t.citations || [], []);
            remember('assistant', t.text);
          }
          thread.appendChild(el);
        });
        scroll();
      })
      .catch(function () {});
  }

  function deleteThread(sid, row) {
    fetch('/api/ask/sessions/' + encodeURIComponent(sid), {method: 'DELETE'})
      .then(function (r) {
        if (r.ok || r.status === 204) {
          row.remove();
          if (sid === currentSessionId) { startNewThread(); }
        }
      })
      .catch(function () {});
  }

  function startNewThread() {
    currentSessionId = null;
    window.CCState.del(SID_KEY);
    history = [];
    window.CCState.del(TAIL_KEY);
    thread.innerHTML = '<span class="ask-dock-empty">Ask about any tracked name without leaving this tab.</span>';
    closeThreads();
    input.focus();
  }

  newThreadBtn.addEventListener('click', function (ev) {
    ev.stopPropagation();
    startNewThread();
  });

  // ---------------------------------------------------------------------------
  // Replay persisted tail
  // ---------------------------------------------------------------------------

  try {
    var tail = window.CCState.getJSON(TAIL_KEY);
    if (tail && tail.length) {
      history = tail.slice(-12);
      var empty = thread.querySelector('.ask-dock-empty');
      if (empty) empty.remove();
      history.forEach(function (turn) {
        var el = document.createElement('div');
        if (turn.role === 'user') {
          el.className = 'ask-dock-user';
          el.textContent = turn.text;
        } else {
          el.className = 'ask-dock-asst';
          el.innerHTML = md(turn.text);
        }
        thread.appendChild(el);
      });
      scroll();
    }
  } catch (e) {}

  // ---------------------------------------------------------------------------
  // Boot state
  // ---------------------------------------------------------------------------

  // The store handles the legacy reads (askDockMode, and the pre-dock
  // boolean askDockOpen which maps open -> float) — see KEYS in cc_state.
  var boot = window.CCState.get('dockMode');
  if (boot !== 'min' && boot !== 'float' && boot !== 'split') boot = 'min';
  setMode(boot, true);

  // ---------------------------------------------------------------------------
  // Citation row helper
  // ---------------------------------------------------------------------------

  function citeRow(card, items, claims) {
    var chips = (items || []).map(function (c) {
      var href = (c && (c.href || c.source_url)) || '';
      if (!href) return '';
      return '<a class="k-chip k-chip-accent" href="' + esc(href) + '" target="_blank">['
        + esc(String(c.n)) + '] ' + esc(c.label || 'source') + '</a>';
    }).join('');
    var warn = window.ccCiteMarks ? window.ccCiteMarks.unverifiedChipHtml(claims) : '';
    if (!chips && !warn) return;
    var row = document.createElement('div');
    row.className = 'ask-dock-cites';
    row.innerHTML = chips + warn;
    card.appendChild(row);
  }
  // Inline superscript cite chips (S8): upgrade the finished prose in place
  // — markers streamed as plain text, chips attach at stream close.
  function linkifyProse(html, items) {
    if (!window.ccCiteMarks || !(items || []).length) return html;
    return window.ccCiteMarks.linkify(html, items);
  }

  // ---------------------------------------------------------------------------
  // Submit handler
  // ---------------------------------------------------------------------------

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
    var claims = [];
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
      if (ev2.type === 'session') {
        // Server assigned a session — capture it for future turns.
        if (ev2.session_id) {
          currentSessionId = ev2.session_id;
          window.CCState.set(SID_KEY, ev2.session_id);
        }
      } else if (ev2.type === 'stage') {
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
        claims = ev2.claims || [];
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
        card.innerHTML = linkifyProse(md(text), citations);
        citeRow(card, citations, claims);
        remember('assistant', text);
      } else {
        card.innerHTML = '<span class="ask-dock-err">no answer — try again</span>';
      }
      scroll();
    }

    fetch('/api/ask/stream', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({query: q, tickers: [], context_spec: lastSpec,
                            history: history.slice(0, -1),
                            session_id: currentSessionId})
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
    """The dock fragment — markup + scoped CSS + its IIFE, rendered once into
    the shell chrome (outside the panel-swap container)."""
    return f"""<style>{_DOCK_CSS}{CITE_MARKS_CSS}</style>
<script>{CITE_MARKS_JS}</script>
<div class="ask-dock" id="ask-dock" data-mode="min">
  <div class="ask-dock-head" id="ask-dock-toggle">
    <span class="ask-dock-title">Ask</span>
    <span class="ask-dock-hint">tables for metric questions &middot; cited answers for open ones</span>
    <button type="button" class="ask-dock-ctl" id="ask-dock-threads-btn"
      title="Browse saved threads" aria-label="Browse threads">&#x21C6;</button>
    <button type="button" class="ask-dock-ctl" id="ask-dock-min" title="Minimize"
      aria-label="Minimize">&#x2581;</button>
    <button type="button" class="ask-dock-ctl ask-dock-splitbtn" id="ask-dock-split"
      title="Split view beside the page (Esc exits)" aria-label="Toggle split view">&#x25EB;</button>
    <button type="button" class="ask-dock-ctl" id="ask-dock-pop" title="Continue in the Ask tab"
      aria-label="Continue in the Ask tab">&#x21D7;</button>
    <button type="button" class="ask-dock-ctl" id="ask-dock-close"
      title="Collapse (Esc in split)" aria-label="Collapse the dock">&times;</button>
  </div>
  <div class="ask-dock-body" id="ask-dock-body">
    <div id="ask-dock-threads" class="ask-dock-threads" hidden>
      <div class="ask-dock-threads-head">
        <span class="ask-dock-threads-title">Saved threads</span>
        <button type="button" id="ask-dock-new-thread" class="k-btn k-btn-quiet k-btn-sm">
          + New thread</button>
        <button type="button" class="ask-dock-ctl" id="ask-dock-threads-close"
          title="Close" aria-label="Close threads">&#x2715;</button>
      </div>
      <div id="ask-dock-threads-list" class="ask-dock-threads-list"></div>
    </div>
    <div class="ask-dock-thread" id="ask-dock-thread">
      <span class="ask-dock-empty">Ask about any tracked name without leaving this tab.</span>
    </div>
    <form class="ask-dock-form" id="ask-dock-form">
      <input id="ask-dock-q" placeholder="Ask&hellip;" autocomplete="off">
      <button type="submit" class="k-btn k-btn-primary k-btn-sm">Ask</button>
    </form>
  </div>
</div>
<script>{_DOCK_JS}</script>"""


__all__ = ["render_ask_dock"]
