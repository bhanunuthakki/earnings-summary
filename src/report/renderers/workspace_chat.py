"""JS + CSS for the in-report chat drawer.

Vanilla JS. Reads boot data shared with workspace_comments (window
`__workspaceCommentBoot`). Streams via SSE from `comments_server.py`
endpoint `/chat/<ticker>`. Renders Markdown responses (basic — code
fences + bold + lists). Shows an "Apply this change" button when the
response includes a `diff_proposal` event.
"""

JS = r"""
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

    // Drawer shell is emitted statically by the Python template — see
    // _chat_drawer_shell in workspace_html.py. Keeps the body's flex
    // layout (.l1-root | .cmt-sidebar) explicit in markup.
    var drawer = document.getElementById('chat-drawer');
    if (!drawer) return;
    var toggle = drawer.querySelector('.chat-toggle');
    var panel = drawer.querySelector('.chat-panel');
    var threadEl = drawer.querySelector('#chat-thread');
    var form = drawer.querySelector('#chat-form');
    var hintEl = drawer.querySelector('#chat-hint');

    function setOpen(open) {
      panel.setAttribute('aria-hidden', open ? 'false' : 'true');
      panel.classList.toggle('open', open);
      toggle.classList.toggle('open', open);
      if (open) form.message.focus();
    }
    toggle.addEventListener('click', function() { setOpen(panel.getAttribute('aria-hidden') === 'true'); });
    drawer.querySelector('.chat-close').addEventListener('click', function() { setOpen(false); });

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
        appendTurn('system', 'Server unreachable. Start with: python execution/comments_server.py', null);
      });

    // ----- Submit handler -----
    form.addEventListener('submit', function(ev) {
      ev.preventDefault();
      var msg = form.message.value.trim();
      if (!msg) return;
      form.message.value = '';
      appendTurn('user', msg, null);
      var assistantEl = appendTurn('assistant', '', null);
      var streamEl = assistantEl.querySelector('.chat-text');
      hintEl.textContent = 'Streaming…';

      // SSE via fetch + ReadableStream (EventSource doesn't support POST)
      fetch(SERVER_URL + '/chat/' + TICKER, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({report_date: REPORT_DATE, message: msg}),
      }).then(function(resp) {
        if (!resp.ok || !resp.body) throw new Error('chat HTTP ' + resp.status);
        var reader = resp.body.getReader();
        var decoder = new TextDecoder();
        var buffer = '';
        function pump() {
          return reader.read().then(function(result) {
            if (result.done) {
              streamEl.innerHTML = renderMarkdown(streamEl.textContent);
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
                if (ev.type === 'delta') {
                  streamEl.textContent += ev.text;
                  threadEl.scrollTop = threadEl.scrollHeight;
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
        hintEl.textContent = 'Server unreachable. Start: python execution/comments_server.py';
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
"""

CSS = r"""
/* ============================================================
   Chat drawer
   ============================================================ */
.chat-drawer {
  position: fixed; bottom: 16px;
  right: calc(var(--sidebar-open-width, 0px) + 16px);
  z-index: 95;
  transition: right 0.2s ease;
}
.chat-toggle {
  display: inline-flex; align-items: center; gap: 6px;
  padding: 8px 14px;
  background: var(--accent, #6db3ff); color: #0d1117;
  border: none; border-radius: 18px; cursor: pointer;
  font-weight: 600; font-size: 13px;
  box-shadow: 0 4px 12px rgba(0, 0, 0, 0.3);
}
.chat-toggle.open { background: var(--ink-muted); }
.chat-toggle-icon { font-family: var(--font-mono); }
.chat-panel {
  position: fixed;
  right: calc(var(--sidebar-open-width, 0px) + 16px);
  bottom: 70px;
  width: 480px; max-width: calc(100vw - 32px);
  height: 600px; max-height: calc(100vh - 100px);
  background: var(--bg-elev, var(--panel));
  border: 1px solid var(--hairline);
  border-radius: 8px; box-shadow: 0 12px 36px rgba(0, 0, 0, 0.5);
  display: none; flex-direction: column;
  transition: right 0.2s ease;
}
.chat-panel.open { display: flex; }
.chat-head {
  display: flex; align-items: flex-start; justify-content: space-between;
  padding: 12px 14px; border-bottom: 1px solid var(--hairline);
}
.chat-title { font-size: 13px; font-weight: 600; color: var(--ink); }
.chat-sub { font-size: 11px; color: var(--muted); margin-top: 2px; font-family: var(--font-mono); }
.chat-close {
  background: transparent; border: none; color: var(--ink-muted);
  font-size: 20px; line-height: 1; cursor: pointer; padding: 0 6px;
}
.chat-thread {
  flex: 1; overflow-y: auto; padding: 12px 14px;
  display: flex; flex-direction: column; gap: 10px;
}
.chat-turn {
  display: flex; flex-direction: column; gap: 4px;
  padding: 8px 10px; border-radius: 6px;
  font-size: 13px; line-height: 1.55;
}
.chat-role-tag {
  font-size: 10px; text-transform: uppercase; letter-spacing: 0.06em;
  font-weight: 600;
}
.chat-role-user { background: rgba(109, 179, 255, 0.08); border-left: 2px solid #6db3ff; }
.chat-role-user .chat-role-tag { color: #6db3ff; }
.chat-role-assistant { background: var(--panel-alt); border-left: 2px solid var(--hairline); }
.chat-role-assistant .chat-role-tag { color: var(--ink-muted); }
.chat-role-system { background: rgba(255, 196, 0, 0.06); border-left: 2px solid rgba(255, 196, 0, 0.5); }
.chat-role-system .chat-role-tag { color: #ffc400; }
.chat-text p { margin: 0 0 6px; }
.chat-text ul { margin: 4px 0 4px 18px; padding: 0; }
.chat-text li { margin: 2px 0; }
.chat-text code {
  font-family: var(--font-mono); font-size: 11.5px;
  background: rgba(255, 255, 255, 0.04); padding: 1px 4px; border-radius: 3px;
}
.chat-text pre.chat-code {
  background: rgba(0, 0, 0, 0.3); border: 1px solid var(--hairline);
  border-radius: 4px; padding: 8px 10px; overflow-x: auto;
  font-family: var(--font-mono); font-size: 11.5px; margin: 6px 0;
}
.chat-diff {
  margin-top: 8px; padding: 8px 10px;
  background: rgba(60, 200, 120, 0.06); border: 1px solid rgba(60, 200, 120, 0.3);
  border-radius: 4px; font-size: 12px;
}
.chat-diff.applied { background: rgba(60, 200, 120, 0.15); }
.chat-diff-summary { color: var(--ink); margin-bottom: 4px; }
.chat-diff-path code { background: transparent; padding: 0; color: var(--muted); }
.chat-diff-actions { display: flex; gap: 6px; margin-top: 6px; }
.chat-diff-actions button {
  background: transparent; border: 1px solid var(--hairline);
  color: var(--ink-muted); padding: 4px 10px; border-radius: 4px;
  font-size: 11px; cursor: pointer;
}
.chat-diff-actions button[data-action="apply"] {
  background: rgba(60, 200, 120, 0.2); color: #3cc878; border-color: rgba(60, 200, 120, 0.5);
}
.chat-diff-actions button:hover { filter: brightness(1.2); }
.chat-diff-note { margin-top: 6px; font-size: 11px; color: var(--ink-muted); }

.chat-form { padding: 10px 14px; border-top: 1px solid var(--hairline); }
.chat-form textarea {
  width: 100%; box-sizing: border-box;
  background: var(--panel-alt); color: var(--ink);
  border: 1px solid var(--hairline); border-radius: 4px;
  padding: 8px 10px; font-size: 13px; font-family: var(--font-body);
  resize: vertical;
}
.chat-form-row {
  display: flex; align-items: center; justify-content: space-between;
  margin-top: 6px;
}
.chat-hint { font-size: 11px; color: var(--muted); }
.chat-form button[type="submit"] {
  background: var(--accent, #6db3ff); color: #0d1117;
  border: none; padding: 6px 14px; border-radius: 4px;
  font-weight: 600; font-size: 12px; cursor: pointer;
}
"""
