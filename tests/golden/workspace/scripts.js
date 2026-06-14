
(function () {
  'use strict';

  // ---- Tab switching (UX9 grouped tabs) -----------------------------------
  // The top bar's .tab[data-tab=<group>] buttons swap the
  // .tab-group-pane[data-tab-group] panes; inside each group a pill row of
  // .subtab[data-subtab=<section>] buttons swaps the .subtab-pane[data-tab]
  // section panes. Section panes keep the legacy per-section data-tab ids so
  // saved comment anchors + cross-links keep resolving after the grouping.
  var topTabs = document.querySelectorAll('.tabs .tab[data-tab]');
  topTabs.forEach(function (btn) {
    btn.addEventListener('click', function () {
      var id = btn.getAttribute('data-tab');
      topTabs.forEach(function (t) {
        t.classList.toggle('active', t === btn);
      });
      document.querySelectorAll('.tab-group-pane[data-tab-group]').forEach(function (p) {
        p.classList.toggle('active', p.getAttribute('data-tab-group') === id);
      });
    });
  });

  document.querySelectorAll('.tab-group-pane').forEach(function (pane) {
    var pills = pane.querySelectorAll('.subtab[data-subtab]');
    pills.forEach(function (pill) {
      pill.addEventListener('click', function () {
        var id = pill.getAttribute('data-subtab');
        pills.forEach(function (p) {
          p.classList.toggle('active', p === pill);
        });
        pane.querySelectorAll('.subtab-pane[data-tab]').forEach(function (sp) {
          sp.classList.toggle('active', sp.getAttribute('data-tab') === id);
        });
      });
    });
  });

  // Activate a tab by section OR group id. Section ids ("earnings", "bear",
  // ...) resolve through their pill (activating the owning group first);
  // single-section groups reuse the section id as the group id, so the
  // top-bar fallback covers them. Returns true when something matched.
  function activateSection(id) {
    if (!id) return false;
    var pill = document.querySelector('.subtab[data-subtab="' + id + '"]');
    if (pill) {
      var pane = pill.closest('.tab-group-pane');
      var gid = pane ? pane.getAttribute('data-tab-group') : null;
      var groupBtn = gid
        ? document.querySelector('.tabs .tab[data-tab="' + gid + '"]')
        : null;
      if (groupBtn) groupBtn.click();
      pill.click();
      return true;
    }
    var topBtn = document.querySelector('.tabs .tab[data-tab="' + id + '"]');
    if (topBtn) { topBtn.click(); return true; }
    return false;
  }

  // (Q&A accordion: now native <details class="qa-row"> — no JS. P4.1.)

  // ---- Cross-tab links (P4.3) ---------------------------------------------
  // <a data-xtab="bear" data-anchor="panel-failure-modes"> switches to the
  // named section's tab and scrolls the anchor panel into view (or the top
  // when no anchor). Authored by workspace_html._xlink_html.
  document.querySelectorAll('a[data-xtab]').forEach(function (link) {
    link.addEventListener('click', function (ev) {
      ev.preventDefault();
      activateSection(link.getAttribute('data-xtab'));
      var anchorId = link.getAttribute('data-anchor');
      var target = anchorId ? document.getElementById(anchorId) : null;
      if (target) {
        target.scrollIntoView({behavior: 'smooth', block: 'start'});
        target.classList.add('xlink-flash');
        setTimeout(function () { target.classList.remove('xlink-flash'); }, 1600);
      } else {
        var root = document.querySelector('.l1-root');
        if (root) root.scrollTop = 0;
      }
    });
  });

  // ---- Deep links: #tab=<section-or-group id> ------------------------------
  // Old per-section links (#tab=earnings) land on the right group + pill.
  function applyHash() {
    var m = /^#tab=([\w-]+)$/.exec(location.hash || '');
    if (m) activateSection(m[1]);
  }
  window.addEventListener('hashchange', applyHash);
  applyHash();

  // ---- Quarter selector ---------------------------------------------------
  document.querySelectorAll('[data-quarter-group]').forEach(function (group) {
    var groupId = group.getAttribute('data-quarter-group');
    group.querySelectorAll('button[data-quarter]').forEach(function (btn) {
      btn.addEventListener('click', function () {
        var q = btn.getAttribute('data-quarter');
        group.querySelectorAll('button[data-quarter]').forEach(function (b) {
          b.classList.toggle('active', b === btn);
        });
        document
          .querySelectorAll('[data-quarter-card][data-quarter-group="' + groupId + '"]')
          .forEach(function (card) {
            var match = card.getAttribute('data-quarter') === q;
            card.style.display = match ? '' : 'none';
          });
      });
    });
  });

  // ---- Financials line-item drill-down -----------------------------------
  // Click a .fin-row.drillable to toggle the .fin-drill row whose id matches
  // data-drill-target. Updates the ▶ chevron to ▼ when open.
  document.querySelectorAll('.fin-row.drillable').forEach(function (row) {
    row.addEventListener('click', function () {
      var targetId = row.getAttribute('data-drill-target');
      if (!targetId) return;
      var target = document.getElementById(targetId);
      if (!target) return;
      var isOpen = target.style.display !== 'none';
      target.style.display = isOpen ? 'none' : '';
      var chev = row.querySelector('.fin-chev');
      if (chev) chev.textContent = isOpen ? '▶' : '▼';
    });
  });

  // ---- Initial highlight: ensure the first top tab is active if none set --
  var anyActive = document.querySelector('.tabs .tab.active');
  if (!anyActive && topTabs.length) topTabs[0].click();
})();


(function () {
  if (window.CCOverlay) return;

  // ---- in-memory open-surface stack (ephemeral; NOT cc_state sessionStorage) ----
  var surfaces = [];     // every registered MODAL surface
  var dismissers = [];   // non-modal Escape-only closers: fn() -> bool (closed?)
  var seqCounter = 0;    // recency tie-break for equal priorities

  // ---- ONE shared scrim (S1's .k-scrim look; CCOverlay owns z-index + click) ----
  var scrim = document.createElement('div');
  scrim.className = 'k-scrim';
  scrim.id = 'cc-overlay-scrim';
  scrim.hidden = true;
  scrim.setAttribute('aria-hidden', 'true');
  function ensureScrim() {
    if (!scrim.parentNode && document.body) document.body.appendChild(scrim);
  }
  scrim.addEventListener('click', function () {
    var s = topScrimSurface();
    if (s) doClose(s);
  });

  function reduceMotion() {
    return !!(window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches);
  }

  function zOf(el) {
    var z = el ? parseInt(getComputedStyle(el).zIndex, 10) : 0;
    return isNaN(z) ? 0 : z;
  }

  function focusableIn(c) {
    if (!c) return [];
    return Array.prototype.slice.call(c.querySelectorAll(
      'button:not([disabled]),[href],input:not([disabled]),select:not([disabled]),' +
      'textarea:not([disabled]),[tabindex]:not([tabindex="-1"])'
    )).filter(function (el) { return el.offsetParent !== null || el === document.activeElement; });
  }

  // The top open MODAL surface BY PRIORITY (palette > peek > drawer > dock) —
  // never merely the most-recently-opened. Ties (same priority) fall back to
  // recency so two equal surfaces still resolve deterministically.
  function topModalSurface() {
    var best = null;
    for (var i = 0; i < surfaces.length; i++) {
      var s = surfaces[i];
      if (!s.isOpen || !s.opts.modal) continue;
      if (!best ||
          s.opts.priority > best.opts.priority ||
          (s.opts.priority === best.opts.priority && s.seq > best.seq)) {
        best = s;
      }
    }
    return best;
  }

  // The scrim sits beneath the VISUALLY topmost scrim-requesting surface, so
  // resolve by computed z-index (a surface may opt out of the scrim while a
  // lower one keeps it).
  function topScrimSurface() {
    var best = null, bestZ = -1;
    for (var i = 0; i < surfaces.length; i++) {
      var s = surfaces[i];
      if (!s.isOpen || !s.opts.modal || !s.opts.scrim) continue;
      var z = zOf(s.el);
      if (!best || z >= bestZ) { best = s; bestZ = z; }
    }
    return best;
  }

  // Snap the scrim under the top scrim-requesting surface, or hide it. The
  // FADE-out on the last surface leaving is driven concurrently from doClose
  // (so scrim + surface animate together), not here.
  function syncScrim() {
    var s = topScrimSurface();
    if (s) {
      ensureScrim();
      scrim.classList.remove('cc-scrim-out');
      scrim.style.zIndex = String(zOf(s.el) - 1);
      scrim.style.background = (s.opts.scrimOpacity != null)
        ? 'rgba(0, 0, 0, ' + s.opts.scrimOpacity + ')' : '';
      scrim.hidden = false;
    } else {
      scrim.classList.remove('cc-scrim-out');
      scrim.hidden = true;
    }
  }

  // The symmetric close: animate the surface out along its open axis, then
  // hide. Falls straight through when motion is off / disabled.
  function animateOut(el, motion, done) {
    if (!el || motion === 'none' || reduceMotion()) { done(); return; }
    var mcls = 'cc-m-' + motion;
    el.classList.add('cc-anim-out', mcls);
    var finished = false;
    function fin() {
      if (finished) return; finished = true;
      el.removeEventListener('transitionend', onEnd);
      el.classList.remove('cc-anim-out', mcls);
      done();
    }
    function onEnd(e) { if (e.target === el) fin(); }
    el.addEventListener('transitionend', onEnd);
    setTimeout(fin, 240);  // fallback if transitionend never fires
  }

  function doOpen(s) {
    if (s.isOpen) return;
    // Mutual exclusion: opening a grouped surface closes its open siblings.
    if (s.opts.group) {
      for (var i = 0; i < surfaces.length; i++) {
        var o = surfaces[i];
        if (o !== s && o.isOpen && o.opts.group === s.opts.group) doClose(o);
      }
    }
    if (s.opts.restoreFocus) s.opener = document.activeElement;
    s.isOpen = true;
    s.seq = ++seqCounter;
    if (s.el) {
      s.el.classList.remove('cc-anim-out', 'cc-m-rise', 'cc-m-slide-right', 'cc-m-pop');
      // A persistent surface (e.g. the dock) drives its own visibility via a
      // data-attr/CSS — CCOverlay only tracks it for Escape/scrim — so it opts
      // out of the [hidden] toggle.
      if (s.opts.toggleHidden !== false) s.el.hidden = false;
    }
    syncScrim();
    if (s.opts.onOpen) { try { s.opts.onOpen(); } catch (e) {} }
    // Focus: closeId by default; a surface that drives its own focus passes
    // autofocus:false (and focuses in onOpen); autofocus:'<id>' overrides.
    if (s.opts.autofocus !== false) {
      var target = null;
      if (typeof s.opts.autofocus === 'string') target = document.getElementById(s.opts.autofocus);
      if (!target && s.opts.closeId) target = document.getElementById(s.opts.closeId);
      if (!target && s.el) target = focusableIn(s.el)[0] || null;
      if (target && target.focus) { try { target.focus(); } catch (e) {} }
    }
  }

  function doClose(s) {
    if (!s.isOpen) return;  // idempotent — re-entrant onClose calls are no-ops
    s.isOpen = false;
    var el = s.el;
    // With s.isOpen now false, this reflects who still needs the scrim AFTER s
    // leaves. If nobody does, fade the scrim out concurrently with the surface.
    var stillScrim = topScrimSurface();
    if (s.opts.scrim && !stillScrim && !scrim.hidden && !reduceMotion()) {
      scrim.classList.add('cc-scrim-out');
    }
    function finish() {
      if (el && s.opts.toggleHidden !== false) el.hidden = true;
      if (stillScrim) syncScrim();  // reposition under the now-top surface
      else { scrim.classList.remove('cc-scrim-out'); scrim.hidden = true; }
      if (s.opts.onClose) { try { s.opts.onClose(); } catch (e) {} }
      if (s.opts.restoreFocus && s.opener && s.opener.focus) {
        try { s.opener.focus(); } catch (e) {}
        s.opener = null;
      }
    }
    animateOut(el, s.opts.motion, finish);
  }

  // ---- ONE keydown listener: Escape (priority-resolved) + Tab (focus trap) --
  document.addEventListener('keydown', function (ev) {
    if (ev.key === 'Escape') {
      // Non-modal popovers (cite-marks / source-chip / hover) claim Escape
      // first — they are the innermost, lightest layer.
      for (var i = dismissers.length - 1; i >= 0; i--) {
        var closed = false;
        try { closed = dismissers[i](); } catch (e) {}
        if (closed) { ev.preventDefault(); return; }
      }
      var top = topModalSurface();
      if (top) { ev.preventDefault(); doClose(top); }
      return;
    }
    if (ev.key === 'Tab') {
      var m = topModalSurface();
      if (!m || !m.opts.trapFocus || !m.el) return;
      var els = focusableIn(m.el);
      if (!els.length) return;
      var first = els[0], last = els[els.length - 1];
      if (ev.shiftKey) {
        if (document.activeElement === first || !m.el.contains(document.activeElement)) {
          ev.preventDefault(); last.focus();
        }
      } else if (document.activeElement === last) {
        ev.preventDefault(); first.focus();
      }
    }
  });

  function register(el, opts) {
    opts = opts || {};
    if (opts.modal === undefined) opts.modal = true;
    opts.priority = opts.priority || 0;
    opts.scrim = !!opts.scrim;
    opts.trapFocus = !!opts.trapFocus;
    opts.restoreFocus = opts.restoreFocus !== false;  // default: restore
    opts.motion = opts.motion || 'rise';
    var s = { el: el, opts: opts, isOpen: false, seq: 0, opener: null };
    surfaces.push(s);
    // The close control (x): auto-wire its click to dismiss. A surface whose
    // close control is a multi-state toggle (e.g. the dock's collapse-one-level
    // x) declares closeId for the contract + default focus but passes
    // wireClose:false to drive close from its own listener instead.
    if (opts.closeId && opts.wireClose !== false) {
      var btn = document.getElementById(opts.closeId);
      if (btn) {
        btn.addEventListener('click', function (e) {
          if (!s.isOpen) return;
          if (e && e.preventDefault) e.preventDefault();
          doClose(s);
        });
      }
    }
    return {
      open: function () { doOpen(s); },
      close: function () { doClose(s); },
      isOpen: function () { return s.isOpen; },
      el: el,
    };
  }

  window.CCOverlay = {
    register: register,
    // Non-modal Escape-only dismissal for phrasing-content popovers. fn() must
    // close at most ONE open popover and return whether it did.
    addPopoverDismisser: function (fn) { if (typeof fn === 'function') dismissers.push(fn); },
    // Priority ladder (matches the z-order): higher wins Escape.
    PRIORITY: { DOCK: 10, DRAWER: 30, PEEK: 40, PALETTE: 50 },
  };
})();


(function () {
  if (window.__ccSrcChipEsc || !window.CCOverlay) return;
  window.__ccSrcChipEsc = true;
  window.CCOverlay.addPopoverDismisser(function () {
    var open = document.querySelectorAll('details.src-pop[open]');
    if (open.length) { open[open.length - 1].removeAttribute('open'); return true; }
    return false;
  });
})();


(function() {
  // ---------------------------------------------------------------
  // Boot data — embedded by the renderer.
  // ---------------------------------------------------------------
  function readJson(id) {
    var el = document.getElementById(id);
    if (!el) return null;
    try { return JSON.parse(el.textContent); } catch (e) { return null; }
  }
  var boot = readJson('workspace-boot');
  var commentStore = readJson('workspace-comments') || {comments: []};
  if (!boot) return;  // No boot data — comments feature disabled.

  var SERVER_URL = boot.server_url || 'http://localhost:7421';
  var TICKER = boot.ticker;
  var REPORT_DATE = boot.report_date;

  // Allow the chat module to share boot + comment refs.
  window.__workspaceCommentBoot = boot;
  window.__workspaceCommentStore = commentStore;

  // ---------------------------------------------------------------
  // Draft autosave — survive tab close / refresh / server-down with
  // unposted text. Drafts are keyed by (ticker, report_date, anchor)
  // and cleared on a successful POST. localStorage only — no server
  // round-trip. See test_workspace_comments_drafts.py.
  // ---------------------------------------------------------------
  function draftKey(anchor) {
    if (!anchor) return null;
    return 'cmt-draft:' + TICKER + ':' + REPORT_DATE
         + ':' + anchor.type + ':' + (anchor.key || '');
  }
  function saveDraft(anchor, text) {
    var k = draftKey(anchor);
    if (!k) return;
    try {
      if (text && text.length) localStorage.setItem(k, text);
      else localStorage.removeItem(k);
    } catch (e) { /* quota / disabled — silent */ }
  }
  function loadDraft(anchor) {
    var k = draftKey(anchor);
    if (!k) return '';
    try { return localStorage.getItem(k) || ''; }
    catch (e) { return ''; }
  }
  function clearDraft(anchor) {
    var k = draftKey(anchor);
    if (!k) return;
    try { localStorage.removeItem(k); } catch (e) { /* silent */ }
  }

  // ---------------------------------------------------------------
  // Outbox — when a POST fails (server down, network blip), the
  // payload is queued in localStorage and retried on a timer / focus
  // / online event until it lands. Distinct from draft autosave:
  // drafts are unposted text the user is still composing; outbox
  // entries are *posts the user already committed to* that we owe
  // them durability for. See test_workspace_comments_outbox.py.
  // ---------------------------------------------------------------
  var OUTBOX_KEY = 'cmt-outbox';
  var OUTBOX_MAX_AGE_MS = 7 * 24 * 60 * 60 * 1000;  // drop entries older than 7d
  var OUTBOX_FLUSH_INTERVAL_MS = 15000;
  var outboxFlushing = false;

  function loadOutbox() {
    try { return JSON.parse(localStorage.getItem(OUTBOX_KEY) || '[]') || []; }
    catch (e) { return []; }
  }
  function saveOutbox(items) {
    try { localStorage.setItem(OUTBOX_KEY, JSON.stringify(items)); }
    catch (e) { /* quota / disabled — silent */ }
  }
  function enqueueOutbox(payload) {
    var items = loadOutbox();
    items.push({
      id: 'q_' + Date.now() + '_' + Math.random().toString(36).slice(2, 8),
      payload: payload,
      ts: Date.now()
    });
    saveOutbox(items);
    updateOutboxBadge();
  }
  function updateOutboxBadge() {
    var badge = document.getElementById('cmt-outbox-badge');
    if (!badge) return;
    var n = loadOutbox().length;
    badge.textContent = n ? ('Queued: ' + n) : '';
    badge.style.display = n ? 'inline-block' : 'none';
  }

  // Sequentially POST queued entries. Stops on the first failure so
  // ordering is preserved and we don't hammer a still-down server.
  // Re-entrancy guard (outboxFlushing) keeps the timer + focus event
  // from doubling up on the same in-flight flush.
  function flushOutbox() {
    if (outboxFlushing) return Promise.resolve();
    outboxFlushing = true;
    return (async function() {
      try {
        var items = loadOutbox();
        var now = Date.now();
        var fresh = items.filter(function(it) {
          return (now - (it.ts || 0)) < OUTBOX_MAX_AGE_MS;
        });
        if (fresh.length !== items.length) {
          console.warn('cmt-outbox: dropped ' + (items.length - fresh.length) + ' expired entries');
          saveOutbox(fresh);
        }
        for (var i = 0; i < fresh.length; i++) {
          var it = fresh[i];
          var r;
          try {
            r = await fetch(SERVER_URL + '/comments', {
              method: 'POST',
              headers: {'Content-Type': 'application/json'},
              body: JSON.stringify(it.payload)
            });
          } catch (err) {
            break;  // still offline — leave the remainder for the next tick
          }
          if (!r.ok) break;  // server-side error — don't drop, retry later
          var created = await r.json();
          commentStore.comments.push(created);
          var remaining = loadOutbox().filter(function(x) { return x.id !== it.id; });
          saveOutbox(remaining);
          clearDraft(it.payload.anchor);
        }
        updateOutboxBadge();
        renderList();
        renderPins();
      } finally {
        outboxFlushing = false;
      }
    })();
  }

  // Wake-up triggers. setInterval keeps a steady cadence; focus + online
  // catch the user-driven recovery moments. window.__flushOutbox is exposed
  // for the health-pill (just below) to call on detected server recovery
  // without needing to know our internals.
  setInterval(flushOutbox, OUTBOX_FLUSH_INTERVAL_MS);
  window.addEventListener('online', flushOutbox);
  window.addEventListener('focus', flushOutbox);
  window.__flushOutbox = flushOutbox;

  // ---------------------------------------------------------------
  // Health pill — periodic GET /healthz tells the user up-front
  // whether the server is reachable, instead of finding out only
  // when they click Post. Drives the green/red pill in the sidebar
  // header and an inline "offline" banner above the textarea.
  //
  // On the offline → online edge we kick a flushOutbox() rather than
  // waiting for the next 15s tick — the user sees the queue drain
  // moments after the server is back. See test_workspace_comments_health.py.
  // ---------------------------------------------------------------
  var HEALTH_POLL_MS = 10000;
  var healthState = 'unknown';  // 'online' | 'offline' | 'unknown'

  function setHealthState(next) {
    var prev = healthState;
    if (next === prev) return;
    healthState = next;
    renderHealthPill();
    renderOfflineBanner();
    // Edge: offline → online → drain the queue immediately.
    if (prev === 'offline' && next === 'online' && typeof window.__flushOutbox === 'function') {
      window.__flushOutbox();
    }
  }

  function renderHealthPill() {
    var pill = document.getElementById('cmt-health-pill');
    if (!pill) return;
    pill.className = 'cmt-health-pill cmt-health-' + healthState;
    pill.title = healthState === 'online'
      ? 'Server reachable.'
      : healthState === 'offline'
        ? 'Server unreachable — new comments will queue locally and sync on recovery.'
        : 'Checking server status…';
    pill.textContent = healthState === 'online' ? '● Online'
                      : healthState === 'offline' ? '● Offline'
                      : '○ …';
  }

  function renderOfflineBanner() {
    var banner = document.getElementById('cmt-offline-banner');
    if (!banner) return;
    banner.style.display = healthState === 'offline' ? 'block' : 'none';
  }

  function pollHealth() {
    // cache:no-store so a stale 200 doesn't mask a server that died
    // between the last poll and now. Manual AbortController timeout
    // keeps an unresponsive socket from blocking the next tick.
    // Returns the underlying promise so callers (Fix 3 tests, manual
    // debug) can await state-settling, instead of fire-and-forget.
    var ctrl = new AbortController();
    var killer = setTimeout(function() { ctrl.abort(); }, 5000);
    return fetch(SERVER_URL + '/healthz', {cache: 'no-store', signal: ctrl.signal})
      .then(function(r) {
        clearTimeout(killer);
        setHealthState(r.ok ? 'online' : 'offline');
      })
      .catch(function() {
        clearTimeout(killer);
        setHealthState('offline');
      });
  }
  setInterval(pollHealth, HEALTH_POLL_MS);
  window.addEventListener('focus', pollHealth);
  window.__pollCommentHealth = pollHealth;  // for Fix 3 tests + manual debug

  // ---------------------------------------------------------------
  // Pin rendering — annotate each [data-commentable] element with a
  // pin button + count of open comments.
  // ---------------------------------------------------------------
  function commentsForAnchor(type, key) {
    return commentStore.comments.filter(function(c) {
      return c.anchor && c.anchor.type === type && c.anchor.key === key;
    });
  }

  function renderPins() {
    var nodes = document.querySelectorAll('[data-commentable="true"]');
    nodes.forEach(function(node) {
      // Avoid double-pinning
      if (node.querySelector(':scope > .cmt-pin-host')) return;
      var type = node.getAttribute('data-anchor-type');
      var key = node.getAttribute('data-anchor-key');
      if (!type || !key) return;
      var pin = document.createElement('div');
      pin.className = 'cmt-pin-host';
      pin.innerHTML = pinMarkup(commentsForAnchor(type, key));
      node.appendChild(pin);
      pin.addEventListener('click', function(ev) {
        ev.stopPropagation();
        openSidebar(type, key, node);
      });
    });
  }

  function pinMarkup(commentList) {
    var openCount = commentList.filter(function(c) { return c.status === 'open'; }).length;
    var totalCount = commentList.length;
    var cls = 'cmt-pin';
    if (openCount > 0) cls += ' has-open';
    else if (totalCount > 0) cls += ' all-addressed';
    var label = totalCount ? totalCount : '+';
    var title = totalCount
      ? (openCount + ' open · ' + (totalCount - openCount) + ' addressed')
      : 'Comment';
    return '<button class="' + cls + '" title="' + title + '" type="button">' + label + '</button>';
  }

  // ---------------------------------------------------------------
  // Sidebar — static shell rendered by the Python template. Opens
  // when a pin / mark / floater-button is activated; lists comments
  // for that anchor + a "new comment" form. Dismiss via the × button
  // or Escape — no outside-click listener (it raced with mousedown-
  // triggered opens from the selection floater and closed the
  // sidebar on the same gesture that opened it).
  // ---------------------------------------------------------------
  var sidebar = document.getElementById('cmt-sidebar');
  var currentAnchor = null;
  if (sidebar) {
    sidebar.querySelector('.cmt-close').addEventListener('click', closeSidebar);
    sidebar.querySelector('#cmt-form').addEventListener('submit', onSubmit);
    var saveNoteBtn = sidebar.querySelector('#cmt-save-note');
    if (saveNoteBtn) saveNoteBtn.addEventListener('click', onSaveNote);
    // Autosave the draft on every keystroke so a tab close / refresh /
    // server-down outage doesn't lose typed-but-unposted text.
    var draftArea = sidebar.querySelector('#cmt-form [name="comment"]');
    if (draftArea) {
      draftArea.addEventListener('input', function() {
        saveDraft(currentAnchor, draftArea.value);
      });
    }
    // Inject the outbox-status badge into the sidebar header. Server-
    // rendered shell stays minimal; the badge is dynamic anyway. Hidden
    // when empty so it doesn't clutter the header in the happy path.
    var head = sidebar.querySelector('.cmt-sidebar-head');
    if (head && !document.getElementById('cmt-outbox-badge')) {
      var badge = document.createElement('span');
      badge.id = 'cmt-outbox-badge';
      badge.className = 'cmt-outbox-badge';
      badge.style.display = 'none';
      badge.title = 'Comments queued locally — will retry until the server is back.';
      head.appendChild(badge);
      updateOutboxBadge();
    }
    // Health pill (Fix 3) — same header, sits left of the close button
    // so the user sees server status at a glance whenever the sidebar
    // is open.
    if (head && !document.getElementById('cmt-health-pill')) {
      var pill = document.createElement('span');
      pill.id = 'cmt-health-pill';
      pill.className = 'cmt-health-pill cmt-health-unknown';
      pill.textContent = '○ …';
      // Insert before the close button so it sits at the right edge
      // of the header content, not after the close glyph.
      var closeBtn = head.querySelector('.cmt-close');
      if (closeBtn) head.insertBefore(pill, closeBtn);
      else head.appendChild(pill);
    }
    // Offline banner — above the textarea, shown only when health is
    // 'offline'. Tells the user their next submit will queue (not lose).
    var formEl = sidebar.querySelector('#cmt-form');
    if (formEl && !document.getElementById('cmt-offline-banner')) {
      var banner = document.createElement('div');
      banner.id = 'cmt-offline-banner';
      banner.className = 'cmt-offline-banner';
      banner.style.display = 'none';
      banner.textContent = 'Server offline — your comment will queue locally and sync on recovery.';
      formEl.insertBefore(banner, formEl.firstChild);
    }
    // Fire the first poll right away so the user doesn't wait 10s for
    // the initial state to populate.
    if (typeof pollHealth === 'function') pollHealth();
  }

  // Visual open/close — the push-sidebar's own .open class + width transition.
  function applyOpenVisual(open) {
    if (!sidebar) return;
    sidebar.setAttribute('aria-hidden', open ? 'false' : 'true');
    sidebar.classList.toggle('open', open);
    if (open) document.documentElement.style.setProperty('--sidebar-open-width', '380px');
    else document.documentElement.style.removeProperty('--sidebar-open-width');
  }

  // CCOverlay registration (S4, Law 3): a gesture push-sidebar. scrim:false is
  // a DELIBERATE, DECLARED carve-out — the no-outside-click dismissal is
  // load-bearing (an outside-click listener raced the floater's mousedown-open
  // and closed the sidebar on the same gesture; see the comment above the
  // sidebar wiring). motion:'none' + toggleHidden:false (its own .open class +
  // width transition drive visuals); grouped 'report-sidebar' so opening it
  // closes the chat sidebar (replaces the window.__close* handshake). Escape is
  // CCOverlay's one listener — no per-sidebar keydown.
  var cmtOv = window.CCOverlay && window.CCOverlay.register(sidebar, {
    modal: true, priority: 30, scrim: false, trapFocus: false, restoreFocus: true,
    motion: 'none', toggleHidden: false, autofocus: false,
    group: 'report-sidebar', closeId: 'cmt-close', wireClose: false,
    onOpen: function() { applyOpenVisual(true); },
    onClose: function() { applyOpenVisual(false); currentAnchor = null; }
  });

  // Single entry point — pins call with a humanAnchor label, floater /
  // marks supply their own free-text label.
  function openWithAnchor(anchor, label) {
    if (!sidebar) return;
    currentAnchor = anchor;
    // open() handles the visual open + one-open-at-a-time (closes chat via the
    // 'report-sidebar' group); idempotent when already open (content still
    // refreshes below for the new anchor).
    if (cmtOv) cmtOv.open(); else applyOpenVisual(true);
    document.getElementById('cmt-anchor-label').textContent = label;
    renderList();
    // Rehydrate the draft for this anchor (if any). Hint that a draft
    // is restored so the user knows where the text came from.
    var area = sidebar.querySelector('#cmt-form [name="comment"]');
    if (area) {
      var draft = loadDraft(anchor);
      area.value = draft;
      hint(draft ? 'Draft restored.' : '');
    }
  }

  function openSidebar(type, key, anchorNode) {
    // Capture the stable doorway handle (S12) when the anchored cell carries
    // one, so the comment — and the note it mirrors — re-binds across a metric
    // rename even though `key` (the display name) is what moved.
    var anchor = {
      type: type, key: key,
      tab: anchorNode.getAttribute('data-anchor-tab'),
      fact_ref: anchorNode.getAttribute('data-fact-ref') || null
    };
    openWithAnchor(anchor, humanAnchor(anchor));
  }

  function closeSidebar() {
    if (cmtOv) cmtOv.close(); else { applyOpenVisual(false); currentAnchor = null; }
  }

  function humanAnchor(a) {
    return (a.type.replace(/_/g, ' ') + ' · ' + a.key).substring(0, 80);
  }

  function renderList() {
    var list = document.getElementById('cmt-list');
    if (!list || !currentAnchor) return;
    var items = commentsForAnchor(currentAnchor.type, currentAnchor.key);
    if (items.length === 0) {
      list.innerHTML = '<div class="cmt-empty">No comments yet on this element.</div>';
      return;
    }
    list.innerHTML = items.map(renderCommentCard).join('');
    // Wire dismiss / mark-addressed buttons (only when server is reachable).
    list.querySelectorAll('[data-cmt-action]').forEach(function(btn) {
      btn.addEventListener('click', function() {
        var id = btn.getAttribute('data-cmt-id');
        var action = btn.getAttribute('data-cmt-action');
        updateComment(id, action);
      });
    });
  }

  function renderCommentCard(c) {
    var statusClass = 'cmt-status-' + c.status;
    var head = '<div class="cmt-card-head">'
      + '<span class="cmt-status ' + statusClass + '">' + c.status + '</span>'
      + (c.intent ? '<span class="cmt-intent">' + c.intent + '</span>' : '')
      + '<span class="cmt-time">' + (c.created_at || '').substring(0, 16).replace('T', ' ') + '</span>'
      + '</div>';
    var body = '<div class="cmt-body">' + escapeHtml(c.comment) + '</div>';
    var resolution = c.resolution_note
      ? '<div class="cmt-resolution"><strong>Resolved:</strong> ' + escapeHtml(c.resolution_note) + '</div>'
      : '';
    var thread = '';
    if (c.follow_up_thread && c.follow_up_thread.length) {
      thread = '<div class="cmt-thread">' + c.follow_up_thread.map(function(t) {
        return '<div class="cmt-thread-turn cmt-role-' + t.role + '">'
          + '<span class="cmt-thread-role">' + t.role + '</span>'
          + '<span class="cmt-thread-text">' + escapeHtml(t.text) + '</span>'
          + '</div>';
      }).join('') + '</div>';
    }
    var actions = '';
    if (c.status === 'open') {
      actions = '<div class="cmt-actions">'
        + '<button data-cmt-id="' + c.id + '" data-cmt-action="dismissed">Dismiss</button>'
        + '<button data-cmt-id="' + c.id + '" data-cmt-action="addressed">Mark addressed</button>'
        + '</div>';
    }
    return '<div class="cmt-card">' + head + body + resolution + thread + actions + '</div>';
  }

  function escapeHtml(s) {
    if (s == null) return '';
    return String(s).replace(/[&<>"']/g, function(ch) {
      return {'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[ch];
    });
  }

  // ---------------------------------------------------------------
  // Server I/O — POST new comment / update status. Falls back to
  // a warning + clipboard copy when the server isn't running.
  // ---------------------------------------------------------------
  function onSubmit(ev) {
    ev.preventDefault();
    if (!currentAnchor) return;
    var form = ev.target;
    var text = form.comment.value.trim();
    if (!text) return;
    var intent = form.intent.value || null;
    var payload = {
      ticker: TICKER,
      report_date: REPORT_DATE,
      anchor: currentAnchor,
      comment: text,
      intent: intent
    };
    // Snapshot the anchor at submit-time so a late-arriving response
    // clears the correct draft even if the user has since opened a
    // different anchor in the sidebar.
    var anchorAtSubmit = currentAnchor;
    fetch(SERVER_URL + '/comments', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(payload)
    }).then(function(r) { return r.json(); }).then(function(created) {
      commentStore.comments.push(created);
      form.reset();
      clearDraft(anchorAtSubmit);
      renderList();
      renderPins();
      hint('Posted.');
    }).catch(function(err) {
      // Server-down path: park the payload in the outbox so the timer
      // / focus / online flush will keep retrying without losing it.
      // Clear the draft + textarea so the user gets visual confirmation
      // the post is "in flight" — the Queued badge is the live status.
      enqueueOutbox(payload);
      clearDraft(anchorAtSubmit);
      form.reset();
      renderList();
      hint('Queued — will retry when server is back. (' + loadOutbox().length + ' total)');
      console.warn(err);
    });
  }

  // P4.5 "add note" capture: save the textarea straight into the analyst
  // journal (analyst_notes via /api/notes) anchored to the open section —
  // a durable thought, not a processor instruction.
  function onSaveNote() {
    if (!currentAnchor) return;
    var form = document.getElementById('cmt-form');
    var text = form.comment.value.trim();
    if (!text) { hint('Write the note text above first.'); return; }
    var kind = form.note_kind ? form.note_kind.value : 'observation';
    var anchorAtSubmit = currentAnchor;
    fetch(SERVER_URL + '/api/notes', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        ticker: TICKER,
        kind: kind,
        body: text,
        anchor_type: anchorAtSubmit.type,
        anchor_key: anchorAtSubmit.key,
        fact_ref: anchorAtSubmit.fact_ref || null,
        context: {report_date: REPORT_DATE, tab: anchorAtSubmit.tab || null}
      })
    }).then(function(r) {
      if (!r.ok) throw new Error('notes HTTP ' + r.status);
      form.comment.value = '';
      clearDraft(anchorAtSubmit);
      hint('Saved to journal ✓');
    }).catch(function() {
      hint('Server unreachable — journal capture needs the research server.');
    });
  }

  function updateComment(id, status) {
    fetch(SERVER_URL + '/comments/' + id, {
      method: 'PATCH',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ticker: TICKER, report_date: REPORT_DATE, status: status})
    }).then(function(r) { return r.json(); }).then(function(updated) {
      for (var i = 0; i < commentStore.comments.length; i++) {
        if (commentStore.comments[i].id === id) commentStore.comments[i] = updated;
      }
      renderList();
      renderPins();
    }).catch(function() {
      hint('Server unreachable — cannot update.');
    });
  }

  function hint(msg) {
    var el = document.getElementById('cmt-form-hint');
    if (el) el.textContent = msg;
  }

  // ---------------------------------------------------------------
  // Free-text commenting (Google-Docs style)
  // ---------------------------------------------------------------
  var floater = null;
  function ensureFloater() {
    if (floater) return floater;
    floater = document.createElement('div');
    floater.className = 'cmt-floater';
    floater.style.display = 'none';
    floater.innerHTML = '<button type="button" class="cmt-floater-btn">+ Comment</button>';
    document.body.appendChild(floater);
    floater.querySelector('button').addEventListener('mousedown', function(ev) {
      ev.preventDefault();
      ev.stopPropagation();
      onFloaterClick();
    });
    return floater;
  }
  function hideFloater() { if (floater) floater.style.display = 'none'; }

  function onSelectionChange() {
    var sel = window.getSelection();
    if (!sel || sel.isCollapsed) return hideFloater();
    var text = (sel.toString() || '').trim();
    if (text.length < 2) return hideFloater();
    var node = sel.anchorNode;
    while (node && node !== document.body) {
      if (node.classList) {
        if (node.classList.contains('cmt-sidebar') ||
            node.classList.contains('cmt-floater') ||
            node.classList.contains('chat-drawer') ||
            node.classList.contains('chat-sidebar')) return hideFloater();
      }
      node = node.parentNode;
    }
    var range = sel.getRangeAt(0);
    var rect = range.getBoundingClientRect();
    if (rect.width === 0 && rect.height === 0) return hideFloater();
    ensureFloater();
    floater.style.display = 'block';
    floater.style.left = Math.round(rect.left + window.scrollX + rect.width / 2 - 56) + 'px';
    floater.style.top = Math.round(rect.bottom + window.scrollY + 6) + 'px';
  }

  function onFloaterClick() {
    var sel = window.getSelection();
    if (!sel || sel.isCollapsed) return hideFloater();
    var text = (sel.toString() || '').trim();
    if (!text) return hideFloater();
    var anchorNode = sel.anchorNode;
    var anchorEl = (anchorNode && anchorNode.nodeType === 1) ? anchorNode
      : (anchorNode && anchorNode.parentElement) || document.body;
    var landmark = findLandmark(anchorEl);
    var occurrence = countOccurrencesBefore(landmark.scope, text, sel.getRangeAt(0));
    var tabAttr = anchorEl.closest ? anchorEl.closest('[data-tab]') : null;
    var anchor = {
      type: 'free_text',
      key: text.substring(0, 200),
      tab: tabAttr ? tabAttr.getAttribute('data-tab') : null,
      parent_landmark: landmark.label,
      occurrence_index: occurrence
    };
    hideFloater();
    openSidebarForAnchor(anchor);
  }

  function findLandmark(el) {
    var cur = el;
    while (cur && cur !== document.body) {
      if (cur.classList && cur.classList.contains('panel')) {
        var title = cur.querySelector(':scope > .panel-head .panel-title');
        var t = (title && title.textContent || '').trim();
        if (t) return {label: 'panel: ' + t, scope: cur};
      }
      if (cur.classList && cur.classList.contains('tab-body')) {
        var tab = cur.closest('[data-tab]');
        var tabName = (tab && tab.getAttribute('data-tab')) || 'unknown';
        return {label: 'tab: ' + tabName, scope: cur};
      }
      cur = cur.parentNode;
    }
    return {label: 'document', scope: document.body};
  }

  function countOccurrencesBefore(scope, needle, range) {
    var pre = range.cloneRange();
    pre.selectNodeContents(scope);
    pre.setEnd(range.startContainer, range.startOffset);
    var before = pre.toString();
    var count = 0;
    var seek = 0;
    while ((seek = before.indexOf(needle, seek)) !== -1) {
      count++;
      seek += needle.length;
    }
    return count;
  }

  function openSidebarForAnchor(anchor) {
    var label = anchor.type === 'free_text'
      ? ((anchor.parent_landmark || 'document') + ' · "' +
         anchor.key.substring(0, 60) + (anchor.key.length > 60 ? '…' : '') + '"')
      : humanAnchor(anchor);
    openWithAnchor(anchor, label);
  }

  function renderFreeTextHighlights() {
    document.querySelectorAll('mark.cmt-highlight').forEach(function(m) {
      var parent = m.parentNode; if (!parent) return;
      while (m.firstChild) parent.insertBefore(m.firstChild, m);
      parent.removeChild(m);
      parent.normalize();
    });
    var freeText = commentStore.comments.filter(function(c) {
      return c.anchor && c.anchor.type === 'free_text';
    });
    freeText.forEach(highlightFreeText);
  }

  function highlightFreeText(c) {
    var scope = locateLandmarkScope(c.anchor.parent_landmark || '', c.anchor.tab);
    if (!scope) return;
    var ranges = findTextRanges(scope, c.anchor.key);
    if (ranges.length === 0) return;
    var pick = ranges[Math.min(c.anchor.occurrence_index || 0, ranges.length - 1)];
    var mark = document.createElement('mark');
    mark.className = 'cmt-highlight';
    if (c.status !== 'open') mark.classList.add('addressed');
    mark.setAttribute('data-cmt-id', c.id);
    mark.setAttribute('title', 'Comment · click to view');
    try { pick.surroundContents(mark); } catch (_) { return; }
    mark.addEventListener('click', function(ev) {
      ev.stopPropagation();
      openSidebarForAnchor(c.anchor);
    });
  }

  function locateLandmarkScope(label, tab) {
    if (!label) return document.body;
    if (label.indexOf('panel: ') === 0) {
      var title = label.substring(7);
      var panels = document.querySelectorAll('.panel');
      for (var i = 0; i < panels.length; i++) {
        var t = panels[i].querySelector(':scope > .panel-head .panel-title');
        if (t && (t.textContent || '').trim() === title) return panels[i];
      }
      return null;
    }
    if (label.indexOf('tab: ') === 0) {
      var name = label.substring(5);
      var pane = document.querySelector('[data-tab="' + name + '"].tab-pane');
      return pane || null;
    }
    if (tab) {
      var fallback = document.querySelector('[data-tab="' + tab + '"].tab-pane');
      if (fallback) return fallback;
    }
    return document.body;
  }

  function findTextRanges(scope, needle) {
    var out = [];
    var walker = document.createTreeWalker(scope, NodeFilter.SHOW_TEXT, null);
    while (walker.nextNode()) {
      var node = walker.currentNode;
      var text = node.nodeValue;
      var pos = 0;
      while ((pos = text.indexOf(needle, pos)) !== -1) {
        var r = document.createRange();
        r.setStart(node, pos);
        r.setEnd(node, pos + needle.length);
        out.push(r);
        pos += needle.length;
      }
    }
    return out;
  }

  // ---------------------------------------------------------------
  // Init
  // ---------------------------------------------------------------
  function bootAll() {
    renderPins();
    renderFreeTextHighlights();
  }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', bootAll);
  } else {
    bootAll();
  }
  document.addEventListener('click', function(ev) {
    if (ev.target && ev.target.matches('.tab')) {
      setTimeout(bootAll, 0);
    }
  });
  document.addEventListener('mouseup', function() {
    setTimeout(onSelectionChange, 0);
  });
  document.addEventListener('selectionchange', function() {
    var sel = window.getSelection();
    if (!sel || sel.isCollapsed) hideFloater();
  });
  document.addEventListener('scroll', hideFloater, true);
  // The selection floater is a transient popover — Escape-only via CCOverlay's
  // one keydown (no second listener, no scrim/trap). It claims Escape first
  // (innermost layer); a further Escape then closes the sidebar through its
  // CCOverlay registration. The sidebar's own close (x) stays wired to closeSidebar.
  if (window.CCOverlay) {
    window.CCOverlay.addPopoverDismisser(function() {
      if (floater && floater.style.display !== 'none') { hideFloater(); return true; }
      return false;
    });
  }

  // Re-render highlights after a successful POST so new free_text
  // comments light up without a page reload.
  var origRenderPins = renderPins;
  renderPins = function() {
    origRenderPins();
    renderFreeTextHighlights();
  };
})();


(function () {
  if (window.ccCiteMarks) return;
  function esc(s) {
    return String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;')
      .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }
  function popHtml(c) {
    var html = '<span class="cite-pop-label">' + esc(c.label || 'source') + '</span>';
    var meta = [];
    if (c.kind) meta.push(esc(c.kind));
    if (typeof c.confidence === 'number') {
      meta.push('confidence ' + Math.round(c.confidence * 100) + '%');
    }
    if (meta.length) html += '<span class="cite-pop-meta">' + meta.join(' &middot; ') + '</span>';
    return '<span class="cite-pop" role="tooltip">' + html + '</span>';
  }
  function linkify(html, items, opts) {
    var base = (opts && opts.hrefBase) || '';
    var map = {};
    (items || []).forEach(function (c) { if (c && c.n) map[String(c.n)] = c; });
    return String(html).replace(/\[(\d{1,2})\]/g, function (m, n) {
      var c = map[n];
      if (!c) return m;
      var href = c.href || c.source_url || '';
      if (href && !/^https?:/.test(href)) href = base + href;
      var mark = href
        ? '<a class="cite-mark" href="' + esc(href) + '" target="_blank" rel="noopener">[' + n + ']</a>'
        : '<span class="cite-mark">[' + n + ']</span>';
      return '<span class="cite-wrap" tabindex="0">' + mark + popHtml(c) + '</span>';
    });
  }
  function unverifiedChipHtml(claims) {
    var bad = (claims || []).filter(function (c) { return c && c.supported === false; });
    if (!bad.length) return '';
    var titles = bad.map(function (c) { return c.text || ''; }).filter(Boolean).join('\n');
    return '<span class="cite-unverified" title="' + esc(titles) + '">&#9888; '
      + bad.length + ' unverified claim' + (bad.length === 1 ? '' : 's') + '</span>';
  }
  window.ccCiteMarks = { linkify: linkify, unverifiedChipHtml: unverifiedChipHtml };
  // Escape-only dismissal (Law 3 / design_language §3.1): a cite popover is
  // phrasing content revealed on :focus-within — NOT a modal, so it must not
  // gain a scrim or focus trap. Register a CCOverlay dismisser that blurs the
  // focused .cite-wrap; the :hover variant just leaves on mouseout. Runs once
  // per document (the ccCiteMarks guard above), and only when CCOverlay is
  // present (e.g. the shell + the report iframe).
  if (window.CCOverlay) {
    window.CCOverlay.addPopoverDismisser(function () {
      var ae = document.activeElement;
      if (ae && ae.closest && ae.closest('.cite-wrap')) { ae.blur(); return true; }
      return false;
    });
  }
})();

(function() {
  // Wait until the comments module has set up boot data.
  function init() {
    var boot = window.__workspaceCommentBoot;
    if (!boot) {
      setTimeout(init, 100);
      return;
    }
    var SERVER_URL = boot.server_url || 'http://localhost:7421';
    var TICKER = boot.ticker;
    var REPORT_DATE = boot.report_date;

    // The chat panel is now a push-sidebar (flex sibling of .l1-root),
    // mirroring the comments sidebar — see _chat_drawer_shell +
    // _comment_sidebar_shell in workspace_html.py. The floating
    // .chat-drawer keeps only the launcher toggle; the panel content
    // lives in .chat-sidebar and slides the document aside when open.
    var sidebar = document.getElementById('chat-sidebar');
    var toggle = document.getElementById('chat-toggle');
    if (!sidebar || !toggle) return;
    var threadEl = document.getElementById('chat-thread');
    var form = document.getElementById('chat-form');
    var hintEl = document.getElementById('chat-hint');
    // Kept in sync with `.chat-sidebar.open { width }` in the CSS so the
    // floating toggle (positioned via --sidebar-open-width) rides the
    // sidebar's left edge.
    var CHAT_WIDTH = '460px';

    // Visual open/close — the push-sidebar's own .open class + width transition.
    function applyOpen(open) {
      sidebar.setAttribute('aria-hidden', open ? 'false' : 'true');
      sidebar.classList.toggle('open', open);
      toggle.classList.toggle('open', open);
      if (open) {
        document.documentElement.style.setProperty('--sidebar-open-width', CHAT_WIDTH);
        form.message.focus();
      } else {
        document.documentElement.style.removeProperty('--sidebar-open-width');
      }
    }

    // Dismissal (x + Esc) and one-open-at-a-time with the comments sidebar are
    // CCOverlay's now (S4, Law 3): a push-sidebar gesture surface — scrim:false
    // (the report stays readable beside it), motion:'none' + toggleHidden:false
    // (its own .open class + width transition drive visuals), grouped
    // 'report-sidebar' so opening one closes the other. This replaces the
    // cross-document window.__close* handshake AND the per-sidebar Escape
    // keydown — the open-surface stack now owns both.
    var chatOv = window.CCOverlay && window.CCOverlay.register(sidebar, {
      modal: true, priority: 30, scrim: false, trapFocus: false, restoreFocus: true,
      motion: 'none', toggleHidden: false, autofocus: false,
      group: 'report-sidebar', closeId: 'chat-close', wireClose: false,
      onOpen: function() { applyOpen(true); },
      onClose: function() { applyOpen(false); }
    });
    function setOpen(open) {
      if (!chatOv) { applyOpen(open); return; }  // degrade if the primitive is absent
      if (open) chatOv.open(); else chatOv.close();
    }

    toggle.addEventListener('click', function() { setOpen(sidebar.getAttribute('aria-hidden') === 'true'); });
    sidebar.querySelector('.chat-close').addEventListener('click', function() { setOpen(false); });

    // Cmd+Enter / Ctrl+Enter submits
    form.message.addEventListener('keydown', function(ev) {
      if ((ev.metaKey || ev.ctrlKey) && ev.key === 'Enter') form.requestSubmit();
    });

    // ----- Load existing thread -----
    fetch(SERVER_URL + '/chat/' + TICKER + '?report_date=' + REPORT_DATE)
      .then(function(r) { return r.json(); })
      .then(function(data) {
        (data.thread || []).forEach(function(t) {
          appendTurn(t.role, t.text, t.proposed_diff || null);
        });
      })
      .catch(function() {
        appendTurn('system', 'The research server is not reachable, so chat is offline.', null);
      });

    // ----- Submit handler -----
    // lastSpec: the most recent data-view spec this drawer rendered. Sent
    // as context_spec so short follow-ups ("now annual", "add MELI")
    // refine the view instead of starting over — same contract as the
    // Ask tab's thread.
    var lastSpec = null;
    form.addEventListener('submit', function(ev) {
      ev.preventDefault();
      var msg = form.message.value.trim();
      if (!msg) return;
      form.message.value = '';
      appendTurn('user', msg, null);
      var assistantEl = appendTurn('assistant', '', null);
      var streamEl = assistantEl.querySelector('.chat-text');
      var citations = [];
      var claims = [];
      hintEl.textContent = 'Working…';

      // SSE via fetch + ReadableStream (EventSource doesn't support POST)
      fetch(SERVER_URL + '/chat/' + TICKER, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({report_date: REPORT_DATE, message: msg, context_spec: lastSpec}),
      }).then(function(resp) {
        if (!resp.ok || !resp.body) throw new Error('chat HTTP ' + resp.status);
        var reader = resp.body.getReader();
        var decoder = new TextDecoder();
        var buffer = '';
        function pump() {
          return reader.read().then(function(result) {
            if (result.done) {
              streamEl.innerHTML = renderMarkdown(streamEl.textContent);
              if (citations.length || claims.length) {
                streamEl.innerHTML = linkifyCites(streamEl.innerHTML, citations);
                appendCiteRow(assistantEl, citations, claims);
              }
              hintEl.textContent = 'Cmd+Enter to send';
              return;
            }
            buffer += decoder.decode(result.value, {stream: true});
            var parts = buffer.split('\n\n');
            buffer = parts.pop();
            parts.forEach(function(frame) {
              var line = frame.replace(/^data:\s*/, '');
              try {
                var ev = JSON.parse(line);
                if (ev.type === 'stage') {
                  // Real progress from the engine: compiling/running a
                  // data view, or off researching in prose.
                  hintEl.textContent =
                    ev.stage === 'compiling' ? 'Compiling the view…'
                    : ev.stage === 'running' ? 'Running the view…'
                    : (ev.note || 'Researching — can take ~30s…');
                } else if (ev.type === 'delta') {
                  streamEl.textContent += ev.text;
                  threadEl.scrollTop = threadEl.scrollHeight;
                } else if (ev.type === 'fragment') {
                  // A metric question routed to the data path: the engine
                  // streams a rendered view fragment instead of prose.
                  var frag = document.createElement('div');
                  frag.className = 'chat-fragment';
                  frag.innerHTML = ev.html || '';
                  assistantEl.appendChild(frag);
                  threadEl.scrollTop = threadEl.scrollHeight;
                  if (ev.spec) lastSpec = ev.spec;
                } else if (ev.type === 'final') {
                  // Data turns carry no deltas — the final's message line
                  // ("4 series · yoy · quarterly") is the turn's text.
                  if (!streamEl.textContent) streamEl.textContent = ev.text || '';
                } else if (ev.type === 'citations') {
                  // Grounded narrative answers (Ask v3): numbered evidence
                  // the answer cited — rendered as chips at stream close.
                  citations = ev.items || [];
                  claims = ev.claims || [];
                } else if (ev.type === 'diff_proposal') {
                  appendDiffButton(assistantEl, ev.diff);
                } else if (ev.type === 'error') {
                  streamEl.textContent += '\n\n[ERROR] ' + ev.error;
                }
              } catch (_) { /* ignore */ }
            });
            return pump();
          });
        }
        return pump();
      }).catch(function(err) {
        streamEl.textContent = '[ERROR] ' + err.message;
        hintEl.textContent = 'The research server is not reachable - chat is offline.';
      });
    });

    // ----- Fact doorway (Law 2 — every datum is a doorway) -----
    // A KPI cell rendered as a .fact-doorway carries a stable fact_ref handle
    // (kpi:{ticker}:{def_id}) on its row. Clicking it opens this chat on the
    // EXACT series: we submit the handle alongside the clean label, and
    // ask.grounding's fast-path resolves it by PK (the name phrase-match is the
    // fallback). The handle rides in the question text so resolution never
    // depends on re-typing the metric's fragile display name.
    document.addEventListener('click', function(ev) {
      var dw = ev.target.closest && ev.target.closest('.fact-doorway');
      if (!dw) return;
      var host = dw.closest('[data-fact-ref]');
      var ref = host && host.getAttribute('data-fact-ref');
      if (!ref) return;
      ev.preventDefault();
      setOpen(true);
      var label = (dw.textContent || '').replace(/\s+/g, ' ').trim();
      form.message.value = label ? (label + ' — ' + ref) : ref;
      form.requestSubmit();
    });

    function appendTurn(role, text, diff) {
      var t = document.createElement('div');
      t.className = 'chat-turn chat-role-' + role;
      t.innerHTML = ''
        + '<div class="chat-role-tag">' + role + '</div>'
        + '<div class="chat-text">' + (text ? renderMarkdown(text) : '') + '</div>';
      threadEl.appendChild(t);
      threadEl.scrollTop = threadEl.scrollHeight;
      if (diff) appendDiffButton(t, diff);
      return t;
    }

    function appendDiffButton(turnEl, diff) {
      var wrap = document.createElement('div');
      wrap.className = 'chat-diff';
      wrap.innerHTML = ''
        + '<div class="chat-diff-summary"><strong>Proposed edit:</strong> ' + escapeHtml(diff.summary || '—') + '</div>'
        + '<div class="chat-diff-path"><code>' + escapeHtml(diff.target_file || '') + ' · ' + escapeHtml(diff.target_path || '') + '</code></div>'
        + '<div class="chat-diff-actions">'
        + '  <button type="button" data-action="preview">Preview</button>'
        + '  <button type="button" data-action="apply">Apply</button>'
        + '</div>';
      turnEl.appendChild(wrap);
      wrap.querySelectorAll('button').forEach(function(btn) {
        btn.addEventListener('click', function() {
          var dryRun = btn.getAttribute('data-action') === 'preview';
          fetch(SERVER_URL + '/chat/' + TICKER + '/apply', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({diff: diff, dry_run: dryRun}),
          }).then(function(r) { return r.json(); }).then(function(res) {
            var msg = (res.applied ? '✓ Applied: ' : (res.dry_run ? '↗ Preview: ' : '✗ ')) +
              (res.summary || '') + (res.error ? ' — ' + res.error : '');
            var note = document.createElement('div');
            note.className = 'chat-diff-note';
            note.textContent = msg;
            wrap.appendChild(note);
            if (res.applied) wrap.classList.add('applied');
          });
        });
      });
    }

    // ----- Citation chips (grounded answers, Ask v3) -----
    // The report opens via file://, so viewer hrefs (/source/<doc_id>…)
    // must be absolute against the research server.
    function citeHref(c) {
      var href = (c && (c.href || c.source_url)) || '';
      if (!href) return '';
      return /^https?:/.test(href) ? href : (SERVER_URL + href);
    }
    function linkifyCites(html, items) {
      // Shared inline cite chips (ui.cite_marks) — hrefBase makes the
      // /source/<doc_id> viewer links absolute against the research server.
      if (!window.ccCiteMarks || !(items || []).length) return html;
      return window.ccCiteMarks.linkify(html, items, {hrefBase: SERVER_URL});
    }
    function appendCiteRow(turnEl, items, claims) {
      var chips = items.map(function(c) {
        var href = citeHref(c);
        if (!href) return '';
        return '<a class="chat-cite" href="' + escapeHtml(href) + '" target="_blank">['
          + escapeHtml(String(c.n)) + '] ' + escapeHtml(c.label || 'source') + '</a>';
      }).join('');
      var warn = window.ccCiteMarks ? window.ccCiteMarks.unverifiedChipHtml(claims) : '';
      if (!chips && !warn) return;
      var row = document.createElement('div');
      row.className = 'chat-cite-row';
      row.innerHTML = chips + warn;
      turnEl.appendChild(row);
    }

    // ----- Tiny Markdown renderer (basic) -----
    function renderMarkdown(text) {
      if (!text) return '';
      var html = escapeHtml(text);
      // code blocks
      html = html.replace(/```([\w]*)\n([\s\S]*?)```/g, function(_, lang, body) {
        return '<pre class="chat-code"><code>' + body + '</code></pre>';
      });
      // inline code
      html = html.replace(/`([^`]+)`/g, '<code>$1</code>');
      // bold
      html = html.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
      // bullets
      html = html.replace(/(?:^|\n)([-*])\s+(.+)/g, function(_, _bullet, body) {
        return '\n<li>' + body + '</li>';
      });
      html = html.replace(/(<li>[\s\S]+?<\/li>)+/g, function(block) { return '<ul>' + block + '</ul>'; });
      // paragraphs
      html = html.split(/\n\n+/).map(function(p) {
        if (/^<(pre|ul|h\d|table)/.test(p)) return p;
        return '<p>' + p.replace(/\n/g, '<br>') + '</p>';
      }).join('');
      return html;
    }

    function escapeHtml(s) {
      if (s == null) return '';
      return String(s).replace(/[&<>"']/g, function(ch) {
        return {'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[ch];
      });
    }
  }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();


(function () {
  var root = document.getElementById('dcf-edit');
  if (!root) return;
  function readJson(id) {
    var el = document.getElementById(id);
    if (!el) return null;
    try { return JSON.parse(el.textContent); } catch (e) { return null; }
  }
  var boot = readJson('workspace-boot') || {};
  var SERVER_URL = boot.server_url || 'http://localhost:7421';
  var TICKER = root.getAttribute('data-dcf-ticker') || boot.ticker;

  var elToggle = document.getElementById('dcf-edit-toggle');
  var elBody = document.getElementById('dcf-edit-body');
  var elStatus = document.getElementById('dcf-edit-status');
  var elControls = document.getElementById('dcf-edit-controls');
  var elScenarios = document.getElementById('dcf-edit-scenarios');
  var elHeatmap = document.getElementById('dcf-edit-heatmap');
  var elReset = document.getElementById('dcf-edit-reset');
  var elSave = document.getElementById('dcf-edit-save');

  var loaded = null;   // canonical inputs as last fetched / saved
  var model = null;    // working copy with live edits
  var ready = false;
  var debounceTimer = null;

  // Rate-like fields edit as percent (x100); the rest are raw numbers.
  var SCALARS = [
    {key: 'wacc', label: 'WACC', pct: true, step: 0.1},
    {key: 'near_op_margin', label: 'Near op margin', pct: true, step: 0.5},
    {key: 'terminal_op_margin', label: 'Term op margin', pct: true, step: 0.5},
    {key: 'exit_multiple', label: 'Exit multiple', pct: false, step: 0.5},
    {key: 'terminal_growth_g', label: 'Terminal g', pct: true, step: 0.1},
    {key: 'tax_rate', label: 'Tax rate', pct: true, step: 0.5}
  ];
  var DRIVERS = [
    {key: 'beta', label: 'Beta', pct: false, step: 0.05},
    {key: 'risk_free_rate', label: 'Risk-free', pct: true, step: 0.1},
    {key: 'equity_risk_premium', label: 'ERP', pct: true, step: 0.1},
    {key: 'cost_of_debt', label: 'Cost of debt', pct: true, step: 0.1}
  ];

  function setStatus(msg, tone) {
    elStatus.textContent = msg || '';
    elStatus.className = 'dcf-edit-status' + (tone ? ' is-' + tone : '');
  }
  function fmtMoney(x) {
    if (x === null || x === undefined || isNaN(x)) return '—';
    return '$' + Number(x).toFixed(2);
  }
  function fmtPct(x) { return (Number(x) * 100).toFixed(1) + '%'; }
  function fmtMult(x) { return Number(x).toFixed(1) + 'x'; }

  // The CAPM derivation, identical to redesign.read_inputs: editing a driver
  // re-derives WACC so the preview stays consistent (a direct WACC edit is a
  // preview-only override that the durable save expresses via the drivers).
  function deriveWacc(m) {
    var ke = m.risk_free_rate + m.beta * m.equity_risk_premium;
    var akd = m.cost_of_debt * (1 - m.tax_rate);
    var mcap = m.current_price * m.diluted_shares_m;
    var denom = mcap + m.total_debt_m;
    var ew = denom > 0 ? mcap / denom : 1.0;
    return ew * ke + (1 - ew) * akd;
  }

  function numField(spec, value, onChange) {
    var wrap = document.createElement('div');
    wrap.className = 'dcf-edit-field';
    var lab = document.createElement('label');
    lab.textContent = spec.label + (spec.pct ? ' (%)' : '');
    var inp = document.createElement('input');
    inp.type = 'number';
    inp.step = String(spec.step);
    inp.value = spec.pct ? (Number(value) * 100).toFixed(2) : String(value);
    inp.addEventListener('input', function () {
      var raw = parseFloat(inp.value);
      if (isNaN(raw)) return;
      onChange(spec.pct ? raw / 100 : raw);
    });
    wrap.appendChild(lab);
    wrap.appendChild(inp);
    return {wrap: wrap, input: inp};
  }

  function group(title) {
    var g = document.createElement('div');
    g.className = 'dcf-edit-group';
    var t = document.createElement('div');
    t.className = 'dcf-edit-group-title';
    t.textContent = title;
    g.appendChild(t);
    return g;
  }

  var waccInput = null;  // kept so driver edits can refresh the WACC display

  function buildControls() {
    elControls.textContent = '';

    // Terminal + valuation levers.
    var gVal = group('Terminal & valuation');
    var methodWrap = document.createElement('div');
    methodWrap.className = 'dcf-edit-field';
    var mlab = document.createElement('label');
    mlab.textContent = 'Terminal method';
    var sel = document.createElement('select');
    ['Exit multiple', 'Perpetuity'].forEach(function (opt) {
      var o = document.createElement('option');
      o.value = opt; o.textContent = opt;
      if (model.terminal_method === opt) o.selected = true;
      sel.appendChild(o);
    });
    sel.addEventListener('change', function () {
      model.terminal_method = sel.value;
      scheduleRecompute();
    });
    methodWrap.appendChild(mlab);
    methodWrap.appendChild(sel);
    var fieldsVal = document.createElement('div');
    fieldsVal.className = 'dcf-edit-fields';
    fieldsVal.appendChild(methodWrap);
    SCALARS.forEach(function (spec) {
      var f = numField(spec, model[spec.key], function (v) {
        model[spec.key] = v;
        scheduleRecompute();
      });
      if (spec.key === 'wacc') waccInput = f.input;
      fieldsVal.appendChild(f.wrap);
    });
    gVal.appendChild(fieldsVal);
    elControls.appendChild(gVal);

    // CAPM drivers — editing one re-derives WACC (durable path).
    var gCapm = group('WACC drivers (re-derive WACC)');
    var fieldsCapm = document.createElement('div');
    fieldsCapm.className = 'dcf-edit-fields';
    DRIVERS.forEach(function (spec) {
      var f = numField(spec, model[spec.key], function (v) {
        model[spec.key] = v;
        model.wacc = deriveWacc(model);
        if (waccInput) waccInput.value = (model.wacc * 100).toFixed(2);
        scheduleRecompute();
      });
      fieldsCapm.appendChild(f.wrap);
    });
    gCapm.appendChild(fieldsCapm);
    elControls.appendChild(gCapm);

    // Per-segment growth.
    var segs = model.segments || [];
    if (segs.length) {
      var gSeg = group('Segment growth (near / terminal)');
      var grid = document.createElement('div');
      grid.className = 'dcf-seg-grid';
      var h0 = document.createElement('div'); h0.className = 'dcf-seg-head'; h0.textContent = '';
      var h1 = document.createElement('div'); h1.className = 'dcf-seg-head'; h1.textContent = 'near %';
      var h2 = document.createElement('div'); h2.className = 'dcf-seg-head'; h2.textContent = 'term %';
      grid.appendChild(h0); grid.appendChild(h1); grid.appendChild(h2);
      segs.forEach(function (name) {
        var nm = document.createElement('div');
        nm.className = 'dcf-seg-name'; nm.textContent = name; nm.title = name;
        grid.appendChild(nm);
        grid.appendChild(segInput(model.near_growth_by_segment, name));
        grid.appendChild(segInput(model.terminal_growth_by_segment, name));
      });
      gSeg.appendChild(grid);
      elControls.appendChild(gSeg);
    }
  }

  function segInput(mapRef, name) {
    var inp = document.createElement('input');
    inp.type = 'number'; inp.step = '0.5';
    inp.value = (Number(mapRef[name]) * 100).toFixed(2);
    inp.addEventListener('input', function () {
      var raw = parseFloat(inp.value);
      if (isNaN(raw)) return;
      mapRef[name] = raw / 100;
      scheduleRecompute();
    });
    return inp;
  }

  function renderScenarios(data) {
    elScenarios.textContent = '';
    var price = data.current_price;
    var sc = data.scenarios || {};
    [['bear', 'Bear'], ['base', 'Base'], ['bull', 'Bull']].forEach(function (pair) {
      var key = pair[0];
      var cell = document.createElement('div');
      cell.className = 'dcf-scn' + (key === 'base' ? ' base' : '');
      var lab = document.createElement('div');
      lab.className = 'dcf-scn-label'; lab.textContent = pair[1];
      var val = document.createElement('div');
      val.className = 'dcf-scn-val'; val.textContent = fmtMoney(sc[key]);
      var up = document.createElement('div');
      var fv = sc[key];
      if (fv !== null && fv !== undefined && price) {
        var pct = (fv - price) / price * 100;
        up.className = 'dcf-scn-up ' + (pct >= 0 ? 'pos' : 'neg');
        up.textContent = (pct >= 0 ? '+' : '') + pct.toFixed(0) + '%';
      } else {
        up.className = 'dcf-scn-up muted'; up.textContent = '—';
      }
      cell.appendChild(lab); cell.appendChild(val); cell.appendChild(up);
      elScenarios.appendChild(cell);
    });
  }

  function renderHeatmap(sens) {
    elHeatmap.textContent = '';
    if (!sens || !sens.values) return;
    var price = sens.current_price || 0;
    var cap = document.createElement('div');
    cap.className = 'dcf-hm-cap';
    cap.textContent = 'Fair value / share - exit multiple (rows) x WACC (cols); '
      + 'green above price';
    elHeatmap.appendChild(cap);
    var tbl = document.createElement('table');
    tbl.className = 'dcf-hm-table';
    var thead = document.createElement('thead');
    var hr = document.createElement('tr');
    var corner = document.createElement('th');
    corner.className = 'dcf-hm-axis'; corner.textContent = 'mult \\ WACC';
    hr.appendChild(corner);
    sens.wacc_axis.forEach(function (w) {
      var th = document.createElement('th');
      th.textContent = fmtPct(w);
      hr.appendChild(th);
    });
    thead.appendChild(hr);
    tbl.appendChild(thead);
    var tbody = document.createElement('tbody');
    var mid = Math.floor(sens.values.length / 2);
    sens.values.forEach(function (row, i) {
      var tr = document.createElement('tr');
      var rh = document.createElement('th');
      rh.textContent = fmtMult(sens.multiple_axis[i]);
      tr.appendChild(rh);
      row.forEach(function (v, j) {
        var td = document.createElement('td');
        td.textContent = fmtMoney(v);
        var rel = price > 0 ? (v - price) / price : 0;
        var mag = Math.min(1, Math.abs(rel) / 0.5);
        var tone = rel >= 0 ? 'var(--ok)' : 'var(--bad)';
        var tint = Math.round(8 + mag * 30);
        td.style.background = 'color-mix(in srgb, ' + tone + ' ' + tint + '%, transparent)';
        td.title = fmtMult(sens.multiple_axis[i]) + ' · ' + fmtPct(sens.wacc_axis[j])
          + ' → ' + fmtMoney(v);
        if (i === mid && j === mid) td.className = 'base';
        tr.appendChild(td);
      });
      tbody.appendChild(tr);
    });
    tbl.appendChild(tbody);
    elHeatmap.appendChild(tbl);
  }

  function recompute() {
    if (!ready) return;
    setStatus('Recomputing…');
    fetch(SERVER_URL + '/api/dcf/recompute', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({inputs: model})
    }).then(function (r) {
      return r.json().then(function (j) { return {ok: r.ok, status: r.status, body: j}; });
    }).then(function (res) {
      if (!res.ok) {
        setStatus((res.body && res.body.error) || ('recompute failed (' + res.status + ')'), 'bad');
        return;
      }
      renderScenarios(res.body);
      renderHeatmap(res.body.sensitivity);
      var ou = res.body.over_under_pct;
      if (ou !== null && ou !== undefined) {
        var pct = (ou * 100);
        setStatus('Base ' + fmtMoney(res.body.fair_value_per_share_usd) + ' · '
          + (pct >= 0 ? 'over' : 'under') + ' by ' + Math.abs(pct).toFixed(0)
          + '% vs price · WACC ' + fmtPct(res.body.wacc), '');
      } else {
        setStatus('Base ' + fmtMoney(res.body.fair_value_per_share_usd)
          + ' · WACC ' + fmtPct(res.body.wacc), '');
      }
    }).catch(function () {
      setStatus('Research server offline — start comments_server to recompute.', 'bad');
    });
  }

  function scheduleRecompute() {
    if (debounceTimer) clearTimeout(debounceTimer);
    debounceTimer = setTimeout(recompute, 280);
  }

  function load() {
    setStatus('Loading model…');
    fetch(SERVER_URL + '/api/dcf/inputs/' + encodeURIComponent(TICKER))
      .then(function (r) {
        if (r.status === 404) { setStatus('No editable DCF model for this ticker.', ''); return null; }
        return r.json().then(function (j) { return {ok: r.ok, body: j}; });
      }).then(function (res) {
        if (!res) return;
        if (!res.ok || !res.body || !res.body.inputs) {
          setStatus((res.body && res.body.error) || 'Could not load the model.', 'bad');
          return;
        }
        loaded = res.body.inputs;
        model = JSON.parse(JSON.stringify(loaded));
        ready = true;
        buildControls();
        recompute();
      }).catch(function () {
        setStatus('Research server offline — start comments_server to edit.', 'bad');
      });
  }

  elToggle.addEventListener('click', function () {
    var open = elBody.hidden;
    elBody.hidden = !open;
    elToggle.setAttribute('aria-expanded', open ? 'true' : 'false');
    if (open && !ready && loaded === null) load();
  });

  elReset.addEventListener('click', function () {
    if (!loaded) return;
    model = JSON.parse(JSON.stringify(loaded));
    buildControls();
    recompute();
  });

  elSave.addEventListener('click', function () {
    if (!ready) return;
    elSave.disabled = true;
    setStatus('Saving…');
    fetch(SERVER_URL + '/api/dcf/save', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ticker: TICKER, inputs: model})
    }).then(function (r) {
      return r.json().then(function (j) { return {ok: r.ok, status: r.status, body: j}; });
    }).then(function (res) {
      elSave.disabled = false;
      if (!res.ok) {
        setStatus((res.body && res.body.error) || ('save failed (' + res.status + ')'), 'bad');
        return;
      }
      // Adopt the canonical saved inputs (WACC re-derived from saved drivers) as
      // the new reset baseline, then re-render from the persisted state.
      if (res.body.inputs) {
        loaded = res.body.inputs;
        model = JSON.parse(JSON.stringify(loaded));
        buildControls();
      }
      if (res.body.sensitivity) { renderScenarios(res.body); renderHeatmap(res.body.sensitivity); }
      setStatus('Saved to model ✓ · override ledger updated (Opus baseline untouched).', 'ok');
    }).catch(function () {
      elSave.disabled = false;
      setStatus('Research server offline — could not save.', 'bad');
    });
  });
})();
