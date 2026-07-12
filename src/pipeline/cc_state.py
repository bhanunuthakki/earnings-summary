"""The command center's one client-side state container (S14 PR2).

``CC_STATE_JS`` is a vanilla-JS module string (no framework, no build step —
matching the shell's architecture) that ``render_shell`` inlines ONCE, before
the ask dock and ``SHELL_JS``, so every shell script and every lazily-injected
fragment shares the same ``window.CCState``.

It replaces the scattered raw ``sessionStorage`` / ``localStorage`` keys that
had accumulated across the shell, the ask dock, and the explore panel
(``cc-ask-q``, ``cc-view-id``, ``cc-ask-thread``, ``cc-ask-session-id``,
``cc-ask-dock-tail``, ``askDockMode``/``askDockOpen``,
``cc-drawer-sec:<endpoint>``) with one namespaced, versioned scheme —
``cc:v1:<key>`` — behind explicit getters/setters. Legacy keys migrate
forward on first read (value copied to the namespaced key, legacy deleted),
so nothing a user had stashed pre-S14 is lost. The full contract lives in
the comment block at the top of the JS itself.

The SWR panel-fragment cache (PR1) was born under the same namespace
(``cc:v1:panel:*``); its helpers move here so the store genuinely owns panel
cache metadata, quota eviction included.
"""

from __future__ import annotations

# Plain string (not an f-string) so its braces pass through untouched.
CC_STATE_JS = r"""
(function () {
  /* =========================================================================
     window.CCState — the command center's ONE client state container.

     Scheme: every value lives at 'cc:v1:<key>' in its declared backend.
       session  = this-tab working state (dies with the tab)
       local    = cross-session preferences (survive restarts)

     Keys (writer → reader):
       section       session  active section id          (shell activate → boot restore)
       tab           session  active panel id            (shell activate → boot restore)
       ticker        session  last holding ticker opened (shell activate → boot restore)
       askQ          session  pending Ask question       (palette/dock pop-out → explore)
       askViewId     session  pending saved-view id      (palette → explore)
       askThread     session  dock→Ask thread handoff    (dock pop-out → explore)
       askSessionId  session  server-side ask thread id  (dock → dock, across reloads)
       askTail       session  dock thread tail JSON      (dock → dock, across reloads)
       dockMode      local    min|float|split            (dock; legacy askDockMode/askDockOpen)
       drawer:<ep>   local    settings-drawer section open ('1'/'0'; legacy cc-drawer-sec:<ep>)
       lastContext   local    {ticker,panel,ts} for "Continue where you left off"
                              (navigation_ia §4 PR3) — written on every activation of a
                              NON-overview panel, read only by Overview's activation
                              to render its doorway line. 'local' (not session) so the
                              doorway survives a fresh tab, unlike section/tab/ticker
                              above, which are deliberately session-scoped.
       panel:<id>[:T] session  SWR fragment cache {etag,html,ts} via CCState.panel.*

     API:
       get(key) -> string|null      set(key, value)      del(key)
       getJSON(key) -> any|null     setJSON(key, value)
       panel.key(pid, ticker) / panel.get(storageKey) / panel.set(storageKey, entry)

     Migration: get() falls back to the key's legacy (pre-S14) names, copies
     the value forward, and deletes the legacy key; set()/del() also clear
     legacy names so a stale copy can never shadow the namespaced one. A
     future breaking change bumps NS to 'cc:v2:' and adds the v1 names as
     legacy entries here — same mechanism, one version at a time.

     Deliberately NOT in the store: the inbox's per-surface unread marks
     (ix-last-seen:*, shared with the /feed page) and the workspace report's
     comment drafts/outbox (a separate document with its own lifecycle).
     ========================================================================= */
  var NS = 'cc:v1:';

  // legacy: [{k: oldName, map?: fn}] — tried in order on a namespaced miss.
  var KEYS = {
    section:      { store: 'session' },
    tab:          { store: 'session' },
    ticker:       { store: 'session' },
    askQ:         { store: 'session', legacy: [{ k: 'cc-ask-q' }] },
    askViewId:    { store: 'session', legacy: [{ k: 'cc-view-id' }] },
    askThread:    { store: 'session', legacy: [{ k: 'cc-ask-thread' }] },
    askSessionId: { store: 'session', legacy: [{ k: 'cc-ask-session-id' }] },
    askTail:      { store: 'session', legacy: [{ k: 'cc-ask-dock-tail' }] },
    dockMode:     { store: 'local', legacy: [
      { k: 'askDockMode' },
      // The pre-dock boolean: open -> float, else min.
      { k: 'askDockOpen', map: function (v) { return v === '1' ? 'float' : 'min'; } }
    ] },
    // "Continue where you left off" (navigation_ia §4 PR3): local, not
    // session — the whole point is surviving into a fresh tab/reload.
    lastContext:  { store: 'local' }
  };

  function spec(key) {
    if (KEYS[key]) return KEYS[key];
    if (key.indexOf('drawer:') === 0) {
      return { store: 'local', legacy: [{ k: 'cc-drawer-sec:' + key.slice(7) }] };
    }
    return { store: 'session' };  // panel:* and anything future default here
  }

  function backend(name) {
    return name === 'local' ? window.localStorage : window.sessionStorage;
  }

  function get(key) {
    var s = spec(key);
    var st;
    try { st = backend(s.store); } catch (e) { return null; }
    try {
      var v = st.getItem(NS + key);
      if (v !== null) return v;
    } catch (e) { return null; }
    var legs = s.legacy || [];
    for (var i = 0; i < legs.length; i++) {
      try {
        var raw = st.getItem(legs[i].k);
        if (raw === null) continue;
        var mapped = legs[i].map ? legs[i].map(raw) : raw;
        set(key, mapped);  // migrate forward (also deletes the legacy names)
        return mapped;
      } catch (e) { /* storage unavailable — fall through */ }
    }
    return null;
  }

  function clearLegacy(s, st) {
    var legs = s.legacy || [];
    for (var i = 0; i < legs.length; i++) {
      try { st.removeItem(legs[i].k); } catch (e) {}
    }
  }

  function set(key, value) {
    var s = spec(key);
    try {
      var st = backend(s.store);
      st.setItem(NS + key, String(value));
      clearLegacy(s, st);
    } catch (e) { /* storage unavailable — state just won't persist */ }
  }

  function del(key) {
    var s = spec(key);
    try {
      var st = backend(s.store);
      st.removeItem(NS + key);
      clearLegacy(s, st);
    } catch (e) {}
  }

  function getJSON(key) {
    var raw = get(key);
    if (raw === null) return null;
    try { return JSON.parse(raw); } catch (e) { return null; }
  }

  function setJSON(key, value) { set(key, JSON.stringify(value)); }

  // ----- SWR panel-fragment cache (PR1's cc:v1:panel:* entries, verbatim
  // semantics — entries written before this store shipped stay valid). The
  // panel cache keeps raw storage-key addressing because one logical key
  // fans out per ticker and the loader iterates storage for eviction.
  var PANEL_PREFIX = NS + 'panel:';

  function panelKey(pid, ticker) {
    return PANEL_PREFIX + pid + (ticker ? ':' + ticker : '');
  }

  function panelGet(storageKey) {
    try {
      var raw = sessionStorage.getItem(storageKey);
      var entry = raw ? JSON.parse(raw) : null;
      return (entry && typeof entry.html === 'string') ? entry : null;
    } catch (e) { return null; }
  }

  function panelSet(storageKey, entry) {
    if (entry.html.length > 400000) return;  // quota is shared — skip outliers
    var v = JSON.stringify(entry);
    try { sessionStorage.setItem(storageKey, v); }
    catch (e) {
      // Quota pressure: evict every panel entry once, retry, then give up
      // quietly (SWR just stays cold; nothing user-visible breaks).
      try {
        var doomed = [];
        for (var i = 0; i < sessionStorage.length; i++) {
          var k = sessionStorage.key(i);
          if (k && k.indexOf(PANEL_PREFIX) === 0) doomed.push(k);
        }
        doomed.forEach(function (k) { sessionStorage.removeItem(k); });
        sessionStorage.setItem(storageKey, v);
      } catch (e2) { /* storage unavailable */ }
    }
  }

  window.CCState = {
    get: get, set: set, del: del,
    getJSON: getJSON, setJSON: setJSON,
    panel: { key: panelKey, get: panelGet, set: panelSet }
  };
})();
""".strip()
