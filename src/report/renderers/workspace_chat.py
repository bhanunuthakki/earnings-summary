"""JS + CSS for the in-report chat drawer.

Vanilla JS. Reads boot data shared with workspace_comments (window
`__workspaceCommentBoot`). Streams via SSE from `comments_server.py`
endpoint `/chat/<ticker>` — the unified ask engine with this report's
ticker context pack. Renders Markdown responses (basic — code fences +
bold + lists), live data-view fragments (`fragment` events, when a metric
question routes to the ViewSpec path), and an "Apply this change" button
when the response includes a `diff_proposal` event.
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
              if (citations.length) {
                streamEl.innerHTML = linkifyCites(streamEl.innerHTML, citations);
                appendCiteRow(assistantEl, citations);
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
      var map = {};
      items.forEach(function(c) { if (c && c.n) map[String(c.n)] = c; });
      return html.replace(/\[(\d{1,2})\]/g, function(m, n) {
        var c = map[n];
        var href = c ? citeHref(c) : '';
        if (!href) return m;
        return '<a class="chat-cite-mark" href="' + escapeHtml(href) + '" target="_blank" title="'
          + escapeHtml(c.label || '') + '">[' + n + ']</a>';
      });
    }
    function appendCiteRow(turnEl, items) {
      var chips = items.map(function(c) {
        var href = citeHref(c);
        if (!href) return '';
        return '<a class="chat-cite" href="' + escapeHtml(href) + '" target="_blank">['
          + escapeHtml(String(c.n)) + '] ' + escapeHtml(c.label || 'source') + '</a>';
      }).join('');
      if (!chips) return;
      var row = document.createElement('div');
      row.className = 'chat-cite-row';
      row.innerHTML = chips;
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
/* Push-sidebar — flex sibling to .l1-root, mirrors .cmt-sidebar so chat
   slides the document aside instead of floating over it. The floating
   .chat-drawer above keeps only the launcher toggle. Width matches
   CHAT_WIDTH in the JS so the toggle rides the sidebar's left edge. */
.chat-sidebar {
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
.chat-sidebar.open {
  width: 460px;
  border-left-width: 1px;
  transition: width 0.2s ease, border-left-width 0s;
  box-shadow: -4px 0 20px rgba(0, 0, 0, 0.35);
}
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
.chat-fragment {
  margin-top: 8px; overflow-x: auto; max-width: 100%;
  border-top: 1px solid var(--hairline); padding-top: 8px;
}
.chat-cite-row { margin-top: 8px; display: flex; gap: 6px; flex-wrap: wrap; }
.chat-cite {
  font-size: 11px; color: var(--link, #6db3ff); border: 1px solid var(--hairline);
  border-radius: 999px; padding: 2px 9px; text-decoration: none;
}
.chat-cite:hover { border-color: var(--link, #6db3ff); }
.chat-text a.chat-cite-mark {
  color: var(--link, #6db3ff); text-decoration: none;
  font-size: 0.85em; vertical-align: super;
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
