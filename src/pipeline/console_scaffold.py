"""Composite-console scaffold — the shared assembler for the Phase-5 IA
consolidation (Portfolio 8→3, Review 3→1).

In migration S10 the 8-tab System diagnostics strip collapsed into one
Provenance page that COMPOSES its existing builders behind an anchor-nav band
(``pipeline/provenance_panel.py``). Phase 5 replicates that move for two more
sections, so the composition mechanics — a jump-chip nav band, per-builder
error isolation, one guarded scroll listener — live here once instead of being
copy-pasted into every console.

Pure composition, no new CSS: the kit classes (``panel_toolbar`` + ``.k-chip``)
carry the band, and each composed builder keeps its own styling. A builder that
*raises* is caught and rendered as a small error card so one broken section
never blanks the whole console (same contract as the Provenance console).

The nav band renders ``panel_toolbar(sticky=True)`` (owner directive
2026-08-02): it pins ``position: sticky`` just below the shell topbar
(the ``.k-toolbar-sticky`` modifier in ``ui/controls.py``, offset from the
shell's topbar-height custom property) so a long console (Allocation,
Record, the Ledger console) keeps its jump chips reachable without
re-scrolling to the top, and its chips carry ``.k-chip-tab`` for the shared
underline-active look.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from html import escape, unescape

from ui.controls import panel_toolbar

log = logging.getLogger(__name__)

# (anchor id, nav label, builder thunk). Order = display order.
ConsoleSection = tuple[str, str, Callable[[], str]]

_HEADING_RE = re.compile(
    r"<h(?P<level>[1-3])(?P<attrs>[^>]*)>(?P<body>.*?)</h(?P=level)>",
    re.IGNORECASE | re.DOTALL,
)
_TAG_RE = re.compile(r"<[^>]+>")


def _hide_duplicate_heading(label: str, fragment: str) -> str:
    """Hide a fragment heading when the canonical card header already owns it."""

    normalized_label = " ".join(label.split()).casefold()
    for match in _HEADING_RE.finditer(fragment):
        text = unescape(_TAG_RE.sub("", match.group("body")))
        if " ".join(text.split()).casefold() != normalized_label:
            continue
        attrs = match.group("attrs")
        if re.search(r"(?:^|\s)hidden(?:\s|=|$)", attrs, re.IGNORECASE):
            return fragment
        replacement = (
            f"<h{match.group('level')}{attrs} hidden>"
            f"{match.group('body')}</h{match.group('level')}>"
        )
        return fragment[: match.start()] + replacement + fragment[match.end() :]
    return fragment


def _safe(name: str, fn: Callable[[], str]) -> str:
    """Render one builder, degrading a raised builder to an error card so a
    single broken section never blanks the whole console."""
    try:
        return fn()
    except Exception as exc:
        log.exception("console section failed to render: %s", name)
        detail = escape(f"{type(exc).__name__}: {exc}")
        return (
            '<section class="panel"><h2>'
            f"{escape(name)}</h2>"
            '<p class="muted">This section failed to render — the rest of the '
            "console is unaffected.</p>"
            f"<details><summary>{detail}</summary></details></section>"
        )


def render_console(
    title: str,
    sections: list[ConsoleSection],
    *,
    wrap_class: str,
    extra_nav: str = "",
    nav_exclude: tuple[str, ...] = (),
    heading_exclude: tuple[str, ...] = (),
    grid: bool = False,
    wide: tuple[str, ...] = (),
    deferred: dict[str, str] | None = None,
) -> str:
    """Assemble a composite console: an anchor-nav band (jump chips) over the
    composed builder sections.

    ``title`` names the console for the toolbar, but is SUPPRESSED — the shell's
    nav owns the single-sub-tab title (design_language §6.1). Each section is
    wrapped in ``<div id="csec-<anchor>">`` so the chips jump to it.

    ``extra_nav`` carries pre-rendered chips a composed builder contributes to
    the band (e.g. the Ledger feed's internal jump chips) — it LEADS the band
    because those chips point into the landing section, which precedes the
    later sections in page order. ``nav_exclude`` drops named anchors' own
    chips (the section still renders), for the landing section whose ``<h2>``
    sits directly under the band and would otherwise duplicate as a chip.
    ``heading_exclude`` omits the scaffold-owned card heading for a section
    whose identity is already carried by that merged band.

    The nav chips scroll via a ``data-console-jump`` data attribute + a guarded
    document-level listener (``_CONSOLE_NAV_JS``), NOT an ``href="#anchor"``: the
    shell's hashchange router treats an unknown hash as a panel id and would fall
    back to Overview, navigating AWAY from the console. A data-attr +
    ``scrollIntoView`` never touches ``location.hash``, so the router never fires.

    ``grid=True`` is the D1 page model (surface_density_jit_redesign.md): the
    sections lay out as a dense multi-column tile grid instead of a full-width
    vertical stack. ``wide`` names the anchors that span every column (the
    Band-1 brief, a landing section). The adopting console owns the
    ``.console-grid`` CSS — this scaffold stays styling-free.
    """
    nav = extra_nav + "".join(
        f'<button type="button" class="k-chip k-chip-btn k-chip-tab" '
        f'data-console-jump="csec-{escape(anchor)}">'
        f"{escape(label)}</button>"
        for anchor, label, _ in sections
        if anchor not in nav_exclude
    )
    toolbar = panel_toolbar(title, filters=nav, suppress_title=True, sticky=True)
    rendered_sections: list[str] = []
    for anchor, label, fn in sections:
        endpoint = (deferred or {}).get(anchor)
        fragment = (
            f'<div data-console-endpoint="{escape(endpoint, quote=True)}" '
            f'data-console-label="{escape(label, quote=True)}" aria-busy="true">'
            f'<p class="muted" role="status">Loading {escape(label)}…</p></div>'
            if endpoint
            else _hide_duplicate_heading(label, _safe(label, fn))
        )
        heading = (
            ""
            if anchor in heading_exclude
            else (
                '<header class="k-card-head"><div class="k-card-heading">'
                f'<h2 class="k-card-title">{escape(label)}</h2>'
                "</div></header>"
            )
        )
        rendered_sections.append(
            f'<article class="console-sec{" csec-wide" if grid and anchor in wide else ""} '
            f'k-card k-card-section" id="csec-{escape(anchor)}">'
            f"{heading}{fragment}</article>"
        )
    body = "".join(rendered_sections)
    if grid:
        body = f'<div class="console-grid">{body}</div>'
    return (
        f'<div class="{escape(wrap_class)}">{toolbar}{body}</div>'
        f"<script>{_CONSOLE_NAV_JS}\n{_CONSOLE_LOAD_JS if deferred else ''}</script>"
    )


# One guarded document-level listener (re-injected fragments never double-wire)
# that scrolls to a section without changing location.hash — see render_console's
# note on why an href anchor would break the shell router.
_CONSOLE_NAV_JS = """
(function () {
  if (window.__ccConsoleNav) return;
  window.__ccConsoleNav = true;
  document.addEventListener('click', function (ev) {
    var b = ev.target && ev.target.closest ? ev.target.closest('[data-console-jump]') : null;
    if (!b) return;
    ev.preventDefault();
    var el = document.getElementById(b.getAttribute('data-console-jump'));
    var reduce = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    if (el) el.scrollIntoView({ behavior: reduce ? 'auto' : 'smooth', block: 'start' });
  });
})();
""".strip()

# Only visible sections consume work. Leaving the console cancels its reads;
# provider calculations can finish on the server without later replacing UI.
_CONSOLE_LOAD_JS = """
(function () {
  var script = document.currentScript;
  var root = script && script.previousElementSibling;
  if (!root) return;
  var sections = Array.from(root.querySelectorAll('[data-console-endpoint]'));
  var active = new Map();
  var ready = new Set();
  var disposed = false;
  function visible() {
    return !document.hidden && root.isConnected && !root.closest('[hidden], [aria-hidden="true"]');
  }
  function eligible(section) {
    return visible() && !section.closest('[hidden], [aria-hidden="true"]');
  }
  function error(section) {
    section.removeAttribute('aria-busy');
    section.innerHTML = '<p class="muted" role="alert">This section is temporarily unavailable. '
      + '<button type="button" class="k-btn k-btn-quiet k-btn-sm" data-console-retry>Retry</button></p>';
  }
  function pump() {
    if (disposed || !visible()) return;
    for (var section of ready) {
      if (active.size >= 2) break;
      if (!eligible(section) || active.has(section) || section.dataset.consoleLoaded === '1') continue;
      ready.delete(section);
      load(section);
    }
  }
  async function load(section) {
    var controller = new AbortController();
    var state = {controller: controller, timer: 0};
    active.set(section, state);
    section.setAttribute('aria-busy', 'true');
    state.timer = window.setTimeout(function () { controller.abort(); }, 45000);
    try {
      var endpoint = section.dataset.consoleEndpoint;
      var url = new URL(endpoint, window.location.href);
      if (url.origin !== window.location.origin || !url.pathname.startsWith('/api/panel/')) throw new Error('Invalid section endpoint');
      var response = await (window.uiFetch || fetch)(endpoint, {signal: controller.signal, timeoutMs: 45000, headers: {Accept: 'text/html'}});
      if (!response.ok) throw new Error('HTTP ' + response.status);
      var html = await response.text();
      if (disposed || active.get(section) !== state || !eligible(section)) return;
      if (window.workOsMountHtml) window.workOsMountHtml(section, html, endpoint);
      else {
        section.innerHTML = html;
        section.querySelectorAll('script').forEach(function (old) {
          var replacement = document.createElement('script');
          if (old.src) replacement.src = old.src; else replacement.textContent = old.textContent;
          old.replaceWith(replacement);
        });
      }
      section.dataset.consoleLoaded = '1';
    } catch (_) {
      if (!disposed && active.get(section) === state && eligible(section)) error(section);
    } finally {
      window.clearTimeout(state.timer);
      if (active.get(section) === state) {
        active.delete(section);
        section.removeAttribute('aria-busy');
      }
      pump();
    }
  }
  var observer = typeof IntersectionObserver === 'function' ? new IntersectionObserver(function (entries) {
    entries.forEach(function (entry) { if (entry.isIntersecting) ready.add(entry.target); else ready.delete(entry.target); });
    pump();
  }, {rootMargin: '100px'}) : null;
  sections.forEach(function (section) { if (observer) observer.observe(section); else ready.add(section); });
  root.addEventListener('click', function (event) {
    var button = event.target.closest('[data-console-retry]');
    if (!button) return;
    var section = button.closest('[data-console-endpoint]');
    if (section) { ready.add(section); pump(); }
  });
  function check() {
    if (!root.isConnected) {
      disposed = true;
      if (observer) observer.disconnect();
      lifecycle.disconnect();
      document.removeEventListener('visibilitychange', check);
    }
    if (!visible()) {
      active.forEach(function (state, section) {
        active.delete(section); window.clearTimeout(state.timer); state.controller.abort();
        section.removeAttribute('aria-busy'); ready.add(section);
      });
    } else pump();
  }
  var lifecycle = new MutationObserver(check);
  lifecycle.observe(document.body, {childList: true, subtree: true, attributes: true, attributeFilter: ['hidden', 'aria-hidden']});
  document.addEventListener('visibilitychange', check);
  pump();
})();
""".strip()
