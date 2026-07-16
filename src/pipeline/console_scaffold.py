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
"""

from __future__ import annotations

import traceback
from collections.abc import Callable
from html import escape

from ui.controls import panel_toolbar

# (anchor id, nav label, builder thunk). Order = display order.
ConsoleSection = tuple[str, str, Callable[[], str]]


def _safe(name: str, fn: Callable[[], str]) -> str:
    """Render one builder, degrading a raised builder to an error card so a
    single broken section never blanks the whole console."""
    try:
        return fn()
    except Exception as exc:
        detail = escape(f"{type(exc).__name__}: {exc}")
        tb = escape(traceback.format_exc()[-1500:])
        return (
            '<section class="panel"><h2>'
            f"{escape(name)}</h2>"
            '<p class="muted">This section failed to render — the rest of the '
            "console is unaffected.</p>"
            f"<details><summary>{detail}</summary><pre>{tb}</pre></details></section>"
        )


def render_console(
    title: str,
    sections: list[ConsoleSection],
    *,
    wrap_class: str,
) -> str:
    """Assemble a composite console: an anchor-nav band (jump chips) over the
    composed builder sections.

    ``title`` names the console for the toolbar, but is SUPPRESSED — the shell's
    nav owns the single-sub-tab title (design_language §6.1). Each section is
    wrapped in ``<div id="csec-<anchor>">`` so the chips jump to it.

    The nav chips scroll via a ``data-console-jump`` data attribute + a guarded
    document-level listener (``_CONSOLE_NAV_JS``), NOT an ``href="#anchor"``: the
    shell's hashchange router treats an unknown hash as a panel id and would fall
    back to Overview, navigating AWAY from the console. A data-attr +
    ``scrollIntoView`` never touches ``location.hash``, so the router never fires.
    """
    nav = "".join(
        f'<button type="button" class="k-chip k-chip-btn" data-console-jump="csec-{escape(anchor)}">'
        f"{escape(label)}</button>"
        for anchor, label, _ in sections
    )
    toolbar = panel_toolbar(title, filters=nav, suppress_title=True)
    body = "".join(
        f'<div class="console-sec" id="csec-{escape(anchor)}">{_safe(label, fn)}</div>'
        for anchor, label, fn in sections
    )
    return (
        f'<div class="{escape(wrap_class)}">{toolbar}{body}</div><script>{_CONSOLE_NAV_JS}</script>'
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
    if (el) el.scrollIntoView({ behavior: 'smooth', block: 'start' });
  });
})();
""".strip()
