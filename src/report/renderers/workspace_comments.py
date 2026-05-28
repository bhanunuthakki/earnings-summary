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
  }

  // Single entry point — pins call with a humanAnchor label, floater /
  // marks supply their own free-text label.
  function openWithAnchor(anchor, label) {
    if (!sidebar) return;
    currentAnchor = anchor;
    sidebar.setAttribute('aria-hidden', 'false');
    sidebar.classList.add('open');
    document.documentElement.style.setProperty('--sidebar-open-width', '380px');
    document.getElementById('cmt-anchor-label').textContent = label;
    renderList();
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
    fetch(SERVER_URL + '/comments', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(payload)
    }).then(function(r) { return r.json(); }).then(function(created) {
      commentStore.comments.push(created);
      form.reset();
      renderList();
      renderPins();
      hint('Posted.');
    }).catch(function(err) {
      hint('Server unreachable — start with: python execution/comments_server.py --ticker ' + TICKER);
      console.warn(err);
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
            node.classList.contains('chat-drawer')) return hideFloater();
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
.cmt-form textarea {
  width: 100%; box-sizing: border-box;
  background: var(--panel-alt); color: var(--ink);
  border: 1px solid var(--hairline); border-radius: 4px;
  padding: 8px 10px; font-family: var(--font-body); font-size: 13px;
  resize: vertical;
}
.cmt-form-row { display: flex; gap: 8px; margin-top: 8px; align-items: center; }
.cmt-form select {
  flex: 1; background: var(--panel-alt); color: var(--ink);
  border: 1px solid var(--hairline); border-radius: 4px;
  padding: 6px 8px; font-size: 12px;
}
.cmt-form button[type="submit"] {
  background: var(--accent, #6db3ff); color: #0d1117;
  border: none; padding: 6px 14px; border-radius: 4px;
  font-weight: 600; font-size: 12px; cursor: pointer;
}
.cmt-form-hint { font-size: 11px; color: var(--muted); margin-top: 6px; min-height: 14px; }

/* Floating "+ Comment" button on text selection (Google-Docs style) */
.cmt-floater { position: absolute; z-index: 110; pointer-events: auto; }
.cmt-floater-btn {
  background: var(--accent, #6db3ff); color: #0d1117;
  border: none; border-radius: 14px;
  padding: 6px 12px; font-size: 11.5px; font-weight: 600;
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
