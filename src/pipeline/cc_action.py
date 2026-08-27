"""The one action-feedback primitive — ``window.CCAction``.

Every surface with a POST-action button (dismiss / approve / affirm / archive /
unmute / delete) used to hand-roll its own click feedback, and the 2026-07-30
sweep found five divergent patterns: declarative buttons with zero busy state, panels
that only ``disabled=true`` with no label change, panels that ``.remove()`` the
row instantly (a jarring pop-out), panels that dim in place, and buttons with
no feedback at all (double-submit possible). The owner's UX bar
(consequence-first actions, visible receipts) makes that drift a trust problem,
not a polish nit.

This module is the sibling of :mod:`pipeline.cc_overlay` for **in-flow
elements** (cards, rows, chips) rather than overlay surfaces:

    window.CCAction.busy(btn, 'Dismissing…');   // pressed state: disable +
                                                //   aria-busy + optional label
    window.CCAction.release(btn);               // restore label + enable
    window.CCAction.receipt(btn, '✓ Dismissed'); // terminal in-place receipt
    window.CCAction.leave(el, done);            // animated removal

``leave`` is the two-beat exit: (1) fade + settle — opacity/transform, one
``--transition`` step, same vocabulary as CCOverlay's close — then (2) a height
collapse so the list below *slides* up instead of jumping. The collapse
deliberately animates layout props: unlike an overlay (which floats), a row
removal IS a layout change, and animating it is the whole point — which is why
these classes live here and not in ``CC_OVERLAY_CSS`` (whose guard test rightly
bans layout props for overlay close motion). Both beats respect
``prefers-reduced-motion`` (instant removal).

The kit (``ui.controls``) owns the matching *button* look: ``.k-btn`` styles
``[aria-busy="true"]`` so controls driven by this primitive get a consistent
pressed state.

Like CCOverlay, this is inlined into BOTH documents (the live shell and the
offline ``file://`` report) so panel fragments can call ``window.CCAction``
unconditionally.
"""

from __future__ import annotations

from pipeline.work_os_styles import CC_ACTION_CSS

# Beat 1 (.cc-act-leave) is transform+opacity over the standard token; beat 2
# (.cc-act-collapse) transitions the JS-pinned inline height (auto→0 doesn't
# animate, so leave() measures first) plus margin/padding/border to zero.
# !important beats the pinned inline height, which is exactly the mechanism.
# Raw string: the JS uses no Python-format substitution.
CC_ACTION_JS = r"""
(function () {
  if (window.CCAction) return;

  function reduceMotion() {
    return !!(window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches);
  }

  // Pressed state: disable + aria-busy (the kit dims [aria-busy]) + optional
  // busy label. The original label is stashed once in data-cc-label so
  // busy → release round-trips even if called twice. The stash happens ONLY
  // when a label swap is requested: the control may be a <select> (triage's
  // route picker), where writing textContent back would destroy the options.
  function busy(btn, label) {
    if (!btn) return;
    if (label) {
      if (!btn.hasAttribute('data-cc-label')) {
        btn.setAttribute('data-cc-label', btn.textContent);
      }
      btn.textContent = label;
    }
    btn.disabled = true;
    btn.setAttribute('aria-busy', 'true');
  }

  function release(btn) {
    if (!btn) return;
    btn.disabled = false;
    btn.removeAttribute('aria-busy');
    var orig = btn.getAttribute('data-cc-label');
    if (orig !== null) {
      btn.textContent = orig;
      btn.removeAttribute('data-cc-label');
    }
  }

  // Terminal receipt: the button stays where it was, disabled, showing what
  // just happened ("✓ Dismissed") — for surfaces whose element stays in place.
  // Buttons only (writes textContent), never a <select>.
  function receipt(btn, text) {
    if (!btn) return;
    btn.disabled = true;
    btn.removeAttribute('aria-busy');  // settled, not in flight
    btn.textContent = text;
  }

  // Animated removal: fade+settle, then collapse the pinned height so
  // siblings slide up, then remove from the DOM and call done().
  // transitionend drives each beat with a timeout fallback (240ms > the
  // 150ms token) mirroring CCOverlay.animateOut.
  function leave(el, done) {
    if (!el || !el.parentNode) { if (done) done(); return; }
    function finish() {
      if (el.parentNode) el.parentNode.removeChild(el);
      if (done) done();
    }
    if (reduceMotion()) { finish(); return; }
    var phase = 0;
    var timer = null;
    function next() {
      if (phase === 0) {
        phase = 1;
        el.classList.add('cc-act-collapse');
        timer = setTimeout(next, 240);
      } else if (phase === 1) {
        phase = 2;
        finish();
      }
    }
    el.addEventListener('transitionend', function (e) {
      if (e.target !== el) return;
      if ((phase === 0 && e.propertyName === 'opacity') ||
          (phase === 1 && e.propertyName === 'height')) {
        clearTimeout(timer);
        next();
      }
    });
    // Pin the box height BEFORE animating so beat 2 has a concrete start.
    el.style.height = el.offsetHeight + 'px';
    el.style.overflow = 'hidden';
    void el.offsetHeight;  // commit the pin before the class flips
    el.classList.add('cc-act-leave');
    timer = setTimeout(next, 240);
  }

  window.CCAction = {
    busy: busy,
    release: release,
    receipt: receipt,
    leave: leave,
  };
})();
"""

__all__ = ["CC_ACTION_CSS", "CC_ACTION_JS"]
