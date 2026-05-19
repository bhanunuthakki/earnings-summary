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
  // Sidebar — opens when a pin is clicked, lists comments for that
  // anchor + a "new comment" form.
  // ---------------------------------------------------------------
  var sidebar = null;
  var currentAnchor = null;

  function ensureSidebar() {
    if (sidebar) return sidebar;
    sidebar = document.createElement('aside');
    sidebar.className = 'cmt-sidebar';
    sidebar.setAttribute('aria-hidden', 'true');
    sidebar.innerHTML = ''
      + '<header class="cmt-sidebar-head">'
      + '  <div>'
      + '    <div class="cmt-sidebar-title">Comments</div>'
      + '    <div class="cmt-sidebar-sub" id="cmt-anchor-label"></div>'
      + '  </div>'
      + '  <button class="cmt-close" type="button" aria-label="close">×</button>'
      + '</header>'
      + '<div class="cmt-list" id="cmt-list"></div>'
      + '<form class="cmt-form" id="cmt-form">'
      + '  <textarea name="comment" placeholder="Write a comment…" rows="3" required></textarea>'
      + '  <div class="cmt-form-row">'
      + '    <select name="intent" title="What should the processor do?">'
      + '      <option value="">Auto-classify</option>'
      + '      <option value="drop_kpi">Drop this KPI</option>'
      + '      <option value="edit_thesis">Edit thesis</option>'
      + '      <option value="ask_question">Ask question</option>'
      + '      <option value="fix_data">Flag data issue</option>'
      + '      <option value="rewrite_section">Rewrite this section</option>'
      + '    </select>'
      + '    <button type="submit">Post</button>'
      + '  </div>'
      + '  <div class="cmt-form-hint" id="cmt-form-hint"></div>'
      + '</form>';
    document.body.appendChild(sidebar);
    sidebar.querySelector('.cmt-close').addEventListener('click', closeSidebar);
    sidebar.querySelector('#cmt-form').addEventListener('submit', onSubmit);
    return sidebar;
  }

  function openSidebar(type, key, anchorNode) {
    ensureSidebar();
    currentAnchor = {type: type, key: key, tab: anchorNode.getAttribute('data-anchor-tab')};
    sidebar.setAttribute('aria-hidden', 'false');
    sidebar.classList.add('open');
    document.getElementById('cmt-anchor-label').textContent = humanAnchor(currentAnchor);
    renderList();
  }

  function closeSidebar() {
    if (!sidebar) return;
    sidebar.setAttribute('aria-hidden', 'true');
    sidebar.classList.remove('open');
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
  // Init
  // ---------------------------------------------------------------
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', renderPins);
  } else {
    renderPins();
  }

  // Re-render pins when tabs switch (anchors in hidden tabs were
  // skipped on first paint if their DOM isn't constructed yet — in our
  // case they are, but this also catches dynamic re-renders).
  document.addEventListener('click', function(ev) {
    if (ev.target && ev.target.matches('.tab')) {
      setTimeout(renderPins, 0);
    }
  });

  // ESC closes sidebar.
  document.addEventListener('keydown', function(ev) {
    if (ev.key === 'Escape') closeSidebar();
  });
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
  position: fixed; top: 0; right: 0; bottom: 0; width: 420px;
  background: var(--bg-elev, var(--panel));
  border-left: 1px solid var(--hairline);
  box-shadow: -8px 0 24px rgba(0, 0, 0, 0.4);
  transform: translateX(100%);
  transition: transform 0.18s ease;
  z-index: 100;
  display: flex; flex-direction: column;
}
.cmt-sidebar.open { transform: translateX(0); }
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
"""
