"""JS for the Investment Decision Card strip's disposition buttons (PRD
§8.1, P1.1). Vanilla JS, mirrors workspace_chat.py's boot-wait pattern: reads
boot data shared with workspace_comments (window ``__workspaceCommentBoot``)
for ``server_url``, and POSTs to
``/api/research/card/<artifact_id>/<verb>`` (comments_server.py — the ONE
action core, ``research.investment_decision_card.act_on_card``).

The strip itself is emitted at render time by
``report.renderers.workspace_sections.chrome._decision_card_strip``; this
module only wires the four ``.dc-act`` buttons it contains. No server
connection required for read-only display (the strip renders fully from the
cached artifact); the disposition buttons need
``python execution/comments_server.py`` running, same as comments/chat.
"""

JS = r"""
(function() {
  function init() {
    var boot = window.__workspaceCommentBoot;
    if (!boot) {
      setTimeout(init, 100);
      return;
    }
    var SERVER_URL = /^https?:$/.test(window.location.protocol)
      ? window.location.origin
      : (boot.server_url || 'http://localhost:7421');
    var MUTATION_HEADERS = window.__workspaceMutationHeaders || {'Content-Type': 'application/json'};

    document.querySelectorAll('.l1-decision-card .dc-act').forEach(function (btn) {
      btn.addEventListener('click', function () {
        var verb = btn.getAttribute('data-verb');
        var artifactId = btn.getAttribute('data-artifact-id');
        var card = btn.closest('.l1-decision-card');
        var statusEl = card ? card.querySelector('.dc-status') : null;
        if (!artifactId || artifactId === '0') {
          if (statusEl) statusEl.textContent = 'No artifact id — cannot record a disposition.';
          return;
        }
        var actions = card ? card.querySelectorAll('.dc-act') : [btn];
        actions.forEach(function (b) { b.disabled = true; });
        if (statusEl) statusEl.textContent = 'Recording ' + verb + '…';
        fetch(SERVER_URL + '/api/research/card/' + artifactId + '/' + verb, {
          method: 'POST',
          headers: MUTATION_HEADERS,
          body: JSON.stringify({})
        }).then(function (r) {
          return r.json().then(function (data) { return {ok: r.ok, data: data}; });
        }).then(function (res) {
          actions.forEach(function (b) { b.disabled = false; });
          if (!res.ok) {
            if (statusEl) statusEl.textContent = 'Failed: ' + (res.data.error || 'server error');
            return;
          }
          if (statusEl) statusEl.textContent = 'Recorded: ' + res.data.status;
        }).catch(function (err) {
          actions.forEach(function (b) { b.disabled = false; });
          if (statusEl) statusEl.textContent = 'Server unreachable — start comments_server.py.';
          console.warn(err);
        });
      });
    });
  }
  init();
})();
"""
