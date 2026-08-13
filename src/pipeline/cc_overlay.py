"""The one transient-surface primitive — ``window.CCOverlay`` (S4, Law 3).

Every drawer, palette, peek, dock, sidebar, and popover used to hand-roll its
own dismissal: the command-center shell carried five ``close*`` functions, an
enumerated Escape ``switch``, and a Tab trap that each surface had to opt into;
the Ask dock kept a SECOND ``keydown`` listener plus a hardcoded
``['cc-palette', 'cc-peek', …]`` id registry that silently went stale; the
report iframe used a cross-document ``window.__close*`` handshake. Each surface
re-derived dismissal, and many lacked the full triad.

This module is the single Overlay abstraction the directive's Law 3 calls for:

    var handle = window.CCOverlay.register(el, {
      onClose, scrim, scrimOpacity, trapFocus, restoreFocus, modal, priority,
      group, motion, closeId, onOpen, autofocus, label,
    });
    handle.open(); handle.close(); handle.isOpen();

It guarantees, from ONE place per document:

* **One Escape listener.** Escape resolves to the top **modal** surface by its
  stored ``priority`` (palette > peek > drawer > dock), **not** by recency — a
  drawer opened after the palette never steals Escape from the palette. Ties
  break by recency. Non-modal popovers (cite-marks, source-chip, the ticker
  hover card) register a *dismisser* and are closed first, Escape-only, without
  a scrim/trap (their ``<details>`` / phrasing-content split-paragraph
  constraint is real — they must not become modal).
* **One scrim + one scrim-click listener.** A single ``.k-scrim`` element
  (S1's control-kit primitive) is shown beneath the topmost ``scrim:true``
  surface, its ``z-index`` set dynamically just under that surface; clicking it
  closes that surface. The per-surface scrim copies are gone.
* **Focus trap + restore.** ``trapFocus`` keeps Tab inside the surface;
  ``restoreFocus`` returns focus to the opener on close.
* **Delightful motion.** Open motion is the control kit's (``k-overlay-rise`` /
  each surface's own keyframe); CCOverlay adds the symmetric *close* — a
  tokenized settle-and-fade that mirrors the open direction (``motion`` =
  ``rise`` / ``slide-right`` / ``pop`` / ``none``), animating transform +
  opacity ONLY, over one ``--transition`` step, reduced-motion-safe.
* **Mutual exclusion by group.** Surfaces sharing a ``group`` string are
  one-open-at-a-time: opening one closes the others. This replaces the shell's
  explicit ``closeDrawer()``/``closePalette()`` cross-calls and the iframe's
  ``window.__close*`` globals — exclusivity becomes the stack's own behavior.

The kit (``ui.controls``) owns the LOOK + open motion of ``.k-scrim`` /
``.k-overlay``; CCOverlay owns dismissal + close motion (the close keyframes
live in ``CC_OVERLAY_CSS`` here, co-located with the JS that triggers them so
the report iframe — a separate document that can't import ``controls_css`` —
inlines the same pair). It does **not** join ``cc_state.py``'s ``cc:v1:*``
sessionStorage: the open-surface stack is in-memory and ephemeral by design (it
dies with the tab; nothing about which overlay is open should survive a reload).

History/Back-button dismissal is deliberately out of v1 (it collides with the
shell's ``hashchange``-closes-panels logic) — tracked, not built.
"""

from __future__ import annotations

# Close motion only — S1's controls.py owns the .k-scrim/.k-overlay look +
# OPEN motion (k-overlay-rise / k-overlay-fade). transform + opacity ONLY
# (never layout props), one --transition step, reduced-motion-safe. The
# .cc-m-* variant mirrors each surface's own open direction so dismissal reads
# as the reverse of appearance.
CC_OVERLAY_CSS = """
.cc-anim-out { transition: opacity var(--transition), transform var(--transition);
  opacity: 0; pointer-events: none; }
.cc-anim-out.cc-m-rise        { transform: translateY(6px); }
.cc-anim-out.cc-m-slide-right { transform: translateX(18px); }
.cc-anim-out.cc-m-pop         { transform: translateX(-50%) scale(0.985); }
.cc-scrim-out { transition: opacity var(--transition); opacity: 0; pointer-events: none; }
@media (prefers-reduced-motion: reduce) {
  .cc-anim-out, .cc-scrim-out { transition-duration: 0.01ms; }
}
"""

# Raw string: the JS uses no Python-format substitution.
CC_OVERLAY_JS = r"""
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
      // Default scrim alpha rides the .k-scrim class (var(--scrim)); a custom
      // scrimOpacity composes a neutral black veil at that alpha without a raw
      // rgba literal (token discipline — see tests/test_ui_controls.py color dim).
      scrim.style.background = (s.opts.scrimOpacity != null)
        ? 'color-mix(in srgb, black ' + (s.opts.scrimOpacity * 100) + '%, transparent)' : '';
      scrim.hidden = false;
    } else {
      scrim.classList.remove('cc-scrim-out');
      scrim.hidden = true;
    }
  }

  // The symmetric close: animate the surface out along its open axis, then
  // hide. Falls straight through when motion is off / disabled.
  function animateOut(el, motion, done) {
    if (!el || motion === 'none' || reduceMotion()) { done(); return function () {}; }
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
    var timer = setTimeout(fin, 240);  // fallback if transitionend never fires
    return function () {
      if (finished) return;
      finished = true;
      clearTimeout(timer);
      el.removeEventListener('transitionend', onEnd);
    };
  }

  function doOpen(s) {
    // Invalidate and cancel any asynchronous close before deciding whether the
    // surface is already open. A stale transition callback must never hide a
    // surface that has since been reopened.
    var reopeningInterruptedClose = !s.isOpen && !!s.cancelCloseAnimation;
    s.closeGeneration += 1;
    if (s.cancelCloseAnimation) {
      s.cancelCloseAnimation();
      s.cancelCloseAnimation = null;
    }
    if (s.isOpen) return;
    // Mutual exclusion: opening a grouped surface closes its open siblings.
    var inheritedOpener = null;
    if (s.opts.group) {
      for (var i = 0; i < surfaces.length; i++) {
        var o = surfaces[i];
        if (o !== s && o.isOpen && o.opts.group === s.opts.group) {
          if (!inheritedOpener && o.opener) inheritedOpener = o.opener;
          doClose(o);
        }
      }
    }
    if (s.opts.restoreFocus) {
      if (reopeningInterruptedClose && s.opener) {
        // Preserve the original external opener across close -> rapid reopen.
      } else if (inheritedOpener) {
        // A grouped handoff is one interaction: both surfaces restore to the
        // opener that launched the first sibling, never the hidden sibling.
        s.opener = inheritedOpener;
      } else {
        s.opener = document.activeElement;
      }
    }
    s.isOpen = true;
    s.seq = ++seqCounter;
    if (s.el) {
      s.el.classList.remove(
        'cc-anim-out', 'cc-m-rise', 'cc-m-slide-right', 'cc-m-pop', 'cc-m-fade'
      );
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
    var closeGeneration = ++s.closeGeneration;
    if (s.opts.onBeforeClose) { try { s.opts.onBeforeClose(); } catch (e) {} }
    var el = s.el;
    // With s.isOpen now false, this reflects who still needs the scrim AFTER s
    // leaves. If nobody does, fade the scrim out concurrently with the surface.
    var stillScrim = topScrimSurface();
    if (s.opts.scrim && !stillScrim && !scrim.hidden && !reduceMotion()) {
      scrim.classList.add('cc-scrim-out');
    }
    function finish() {
      if (s.closeGeneration !== closeGeneration || s.isOpen) return;
      s.cancelCloseAnimation = null;
      if (el && s.opts.toggleHidden !== false) el.hidden = true;
      // Recompute rather than trusting the close-start snapshot: a grouped
      // sibling may have opened during this surface's exit animation.
      syncScrim();
      if (s.opts.onClose) { try { s.opts.onClose(); } catch (e) {} }
      var currentTop = topModalSurface();
      if (!currentTop && s.opts.restoreFocus && s.opener && s.opener.focus) {
        try { s.opener.focus(); } catch (e) {}
      }
      s.opener = null;
    }
    s.cancelCloseAnimation = animateOut(el, s.opts.motion, finish);
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
    var s = {
      el: el, opts: opts, isOpen: false, seq: 0, opener: null,
      closeGeneration: 0, cancelCloseAnimation: null
    };
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
"""

__all__ = ["CC_OVERLAY_CSS", "CC_OVERLAY_JS"]
