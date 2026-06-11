"""JS + CSS for the inline-comments + chat panel in the workspace renderer.

Kept as a Python module that exports two string constants (`JS` + `CSS`) so
the renderer can inline them without filesystem reads at render time. Same
pattern as `workspace_script.py` and `workspace_styles.py`.

The JS is vanilla — no framework, no build step. Reads boot data + initial
comment snapshot from two `<script type="application/json">` blocks the
renderer inlines. POSTs to / streams from `localhost:7421` for write
operations (via `comments_server.py`); falls back to a notice when the
server isn't running.
"""

JS = r"""
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
"""

CSS = r"""
/* ============================================================
   Inline comments — pin markers + sidebar + form
   ============================================================ */
[data-commentable="true"] { position: relative; }
.cmt-pin-host {
  position: absolute; top: 4px; right: 4px;
  pointer-events: none; z-index: 5;
}
.cmt-pin {
  pointer-events: auto;
  display: inline-flex; align-items: center; justify-content: center;
  min-width: 20px; height: 20px; padding: 0 6px;
  font-family: var(--font-mono); font-size: 10px; font-weight: 600;
  background: rgba(255, 255, 255, 0.04);
  border: 1px solid var(--hairline);
  border-radius: 10px;
  color: var(--ink-muted); cursor: pointer; opacity: 0.45;
  transition: opacity 0.15s, background 0.15s, border-color 0.15s;
}
[data-commentable="true"]:hover .cmt-pin { opacity: 1; }
.cmt-pin:hover { background: rgba(255, 255, 255, 0.1); border-color: var(--ink-muted); color: var(--ink); }
.cmt-pin.has-open { background: rgba(255, 196, 0, 0.18); border-color: rgba(255, 196, 0, 0.55); color: #ffc400; opacity: 1; }
.cmt-pin.all-addressed { background: rgba(60, 200, 120, 0.12); border-color: rgba(60, 200, 120, 0.4); color: #3cc878; opacity: 0.9; }

.cmt-sidebar {
  /* True push-sidebar: flex sibling to .l1-root */
  flex-shrink: 0;
  width: 0;
  height: 100vh;
  overflow: hidden;
  background: var(--bg-elev, var(--panel));
  border-left: 0 solid var(--hairline);
  transition: width 0.2s ease, border-left-width 0s 0.2s;
  display: flex; flex-direction: column;
  position: sticky; top: 0;
}
.cmt-sidebar.open {
  width: 380px;
  border-left-width: 1px;
  transition: width 0.2s ease, border-left-width 0s;
  box-shadow: -4px 0 20px rgba(0, 0, 0, 0.35);
}
.cmt-sidebar-head {
  display: flex; align-items: flex-start; justify-content: space-between;
  padding: 14px 16px; border-bottom: 1px solid var(--hairline);
}
.cmt-sidebar-title { font-size: 14px; font-weight: 600; color: var(--ink); }
.cmt-sidebar-sub {
  font-size: 11.5px; color: var(--muted); margin-top: 2px;
  font-family: var(--font-mono);
}
.cmt-close {
  background: transparent; border: none; color: var(--ink-muted);
  font-size: 22px; line-height: 1; padding: 0 6px; cursor: pointer;
}
.cmt-close:hover { color: var(--ink); }

/* Outbox status badge — shown in the sidebar header when the local
   queue is non-empty. Amber to signal "pending recovery", not error. */
.cmt-outbox-badge {
  display: inline-block;
  margin: 2px 8px 0 auto;
  padding: 2px 8px;
  font-size: 10.5px; font-weight: 600;
  font-family: var(--font-mono);
  background: rgba(255, 196, 0, 0.16);
  color: #ffc400;
  border: 1px solid rgba(255, 196, 0, 0.45);
  border-radius: 10px;
  text-transform: uppercase; letter-spacing: 0.04em;
}

/* Health pill — server reachability indicator in the sidebar header.
   Green when /healthz responds, red when it doesn't, muted while the
   first poll is still in flight. */
.cmt-health-pill {
  display: inline-block;
  margin: 2px 6px 0 6px;
  padding: 2px 8px;
  font-size: 10.5px; font-weight: 600;
  font-family: var(--font-mono);
  border-radius: 10px;
  text-transform: uppercase; letter-spacing: 0.04em;
  border: 1px solid var(--hairline);
  background: rgba(255, 255, 255, 0.04);
  color: var(--ink-muted);
}
.cmt-health-pill.cmt-health-online {
  color: #3cc878;
  background: rgba(60, 200, 120, 0.12);
  border-color: rgba(60, 200, 120, 0.4);
}
.cmt-health-pill.cmt-health-offline {
  color: #ff7070;
  background: rgba(255, 80, 80, 0.14);
  border-color: rgba(255, 80, 80, 0.45);
}

/* Offline banner inside the form — telegraphs the failure mode BEFORE
   the user submits, instead of after the click. */
.cmt-offline-banner {
  margin-bottom: 8px;
  padding: 6px 10px;
  font-size: 11.5px;
  color: #ffc8a0;
  background: rgba(255, 130, 60, 0.10);
  border: 1px solid rgba(255, 130, 60, 0.4);
  border-radius: 4px;
  line-height: 1.4;
}

.cmt-list { flex: 1; overflow-y: auto; padding: 12px 14px; }
.cmt-empty { color: var(--muted); font-size: 12px; padding: 8px 0; }
.cmt-card {
  background: var(--panel-alt);
  border: 1px solid var(--hairline);
  border-radius: 6px;
  padding: 10px 12px;
  margin-bottom: 10px;
  font-size: 13px;
}
.cmt-card-head {
  display: flex; align-items: center; gap: 6px;
  font-size: 10.5px; color: var(--muted);
  margin-bottom: 6px;
  text-transform: uppercase; letter-spacing: 0.04em;
}
.cmt-status { font-weight: 600; }
.cmt-status-open { color: #ffc400; }
.cmt-status-addressed { color: #3cc878; }
.cmt-status-dismissed { color: var(--muted); }
.cmt-intent { background: rgba(255, 255, 255, 0.05); padding: 1px 6px; border-radius: 3px; }
.cmt-time { margin-left: auto; font-family: var(--font-mono); }
.cmt-body { color: var(--ink); line-height: 1.5; white-space: pre-wrap; }
.cmt-resolution {
  margin-top: 8px; padding: 8px 10px;
  background: rgba(60, 200, 120, 0.08); border-left: 2px solid #3cc878;
  border-radius: 3px; font-size: 12px; color: var(--ink-muted);
}
.cmt-thread { margin-top: 8px; padding-top: 6px; border-top: 1px dashed var(--hairline); }
.cmt-thread-turn { display: flex; gap: 8px; padding: 4px 0; font-size: 12px; }
.cmt-thread-role { font-family: var(--font-mono); color: var(--muted); width: 60px; flex-shrink: 0; }
.cmt-thread-text { color: var(--ink); }
.cmt-role-assistant .cmt-thread-role { color: #6db3ff; }
.cmt-actions { margin-top: 8px; display: flex; gap: 6px; }
.cmt-actions button {
  background: transparent; border: 1px solid var(--hairline);
  color: var(--ink-muted); padding: 4px 10px; font-size: 11px;
  border-radius: 4px; cursor: pointer;
}
.cmt-actions button:hover { background: var(--panel); color: var(--ink); }

.cmt-form { padding: 12px 14px; border-top: 1px solid var(--hairline); background: var(--bg, var(--panel)); }
/* Textarea/select: skinned by the shared control kit (ui/controls.py). */
.cmt-form textarea { width: 100%; box-sizing: border-box; resize: vertical; }
.cmt-form-row { display: flex; gap: 8px; margin-top: 8px; align-items: center; }
.cmt-form select { flex: 1; font-size: var(--fs-caption); }
.cmt-form button[type="submit"] {
  background: var(--accent); color: var(--accent-contrast);
  border: none; padding: 6px 14px; border-radius: var(--radius);
  font-weight: 600; font-size: var(--fs-caption); cursor: pointer;
}
.cmt-form-hint { font-size: 11px; color: var(--muted); margin-top: 6px; min-height: 14px; }

/* Floating "+ Comment" button on text selection (Google-Docs style) */
.cmt-floater { position: absolute; z-index: 110; pointer-events: auto; }
.cmt-floater-btn {
  background: var(--accent); color: var(--accent-contrast);
  border: none; border-radius: var(--radius-full);
  padding: 6px 12px; font-size: var(--fs-caption); font-weight: 600;
  cursor: pointer; box-shadow: 0 3px 10px rgba(0, 0, 0, 0.35);
  white-space: nowrap;
}
.cmt-floater-btn:hover { filter: brightness(1.08); }

/* Free-text highlight (the underlined excerpt the user commented on) */
mark.cmt-highlight {
  background: rgba(255, 196, 0, 0.18);
  border-bottom: 2px solid rgba(255, 196, 0, 0.7);
  color: inherit;
  cursor: pointer;
  padding: 0 1px;
  border-radius: 1px;
  transition: background 0.12s;
}
mark.cmt-highlight:hover { background: rgba(255, 196, 0, 0.32); }
mark.cmt-highlight.addressed {
  background: rgba(60, 200, 120, 0.14);
  border-bottom-color: rgba(60, 200, 120, 0.6);
}
mark.cmt-highlight.addressed:hover { background: rgba(60, 200, 120, 0.24); }
"""
