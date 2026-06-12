
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

  // Single entry point — pins call with a humanAnchor label, floater /
  // marks supply their own free-text label.
  function openWithAnchor(anchor, label) {
    if (!sidebar) return;
    // One-open-at-a-time: opening a comment collapses the chat sidebar.
    if (window.__closeChatSidebar) window.__closeChatSidebar();
    currentAnchor = anchor;
    sidebar.setAttribute('aria-hidden', 'false');
    sidebar.classList.add('open');
    document.documentElement.style.setProperty('--sidebar-open-width', '380px');
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
    var anchor = {type: type, key: key, tab: anchorNode.getAttribute('data-anchor-tab')};
    openWithAnchor(anchor, humanAnchor(anchor));
  }

  function closeSidebar() {
    if (!sidebar) return;
    sidebar.setAttribute('aria-hidden', 'true');
    sidebar.classList.remove('open');
    document.documentElement.style.removeProperty('--sidebar-open-width');
    currentAnchor = null;
  }
  // Let the chat module collapse this sidebar when chat opens.
  window.__closeCommentSidebar = closeSidebar;

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
  document.addEventListener('keydown', function(ev) {
    if (ev.key === 'Escape') { hideFloater(); closeSidebar(); }
  });

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

    function setOpen(open) {
      // One-open-at-a-time: opening chat collapses the comments sidebar.
      if (open && window.__closeCommentSidebar) window.__closeCommentSidebar();
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
    // Let the comments module collapse chat when a comment is opened.
    window.__closeChatSidebar = function() { setOpen(false); };

    toggle.addEventListener('click', function() { setOpen(sidebar.getAttribute('aria-hidden') === 'true'); });
    sidebar.querySelector('.chat-close').addEventListener('click', function() { setOpen(false); });
    document.addEventListener('keydown', function(ev) { if (ev.key === 'Escape') setOpen(false); });

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
