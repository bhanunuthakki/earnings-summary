"""Consolidated Provenance console — one assembler over the 8 diagnostics
builders (S10 PR2).

The System theme used to sprawl across an 8-tab strip (Coverage / IR Docs /
Data Cache / Cron Health / DCF Coverage / Evals / Validation / Restatements) —
the "provenance section illegible, consolidate it" complaint. This collapses
them into ONE "Provenance" tab: a single page composing each existing builder,
with the **Coverage matrix kept prominent** (it leads) and an anchor-nav band so
the long page stays navigable. Old ``#section_coverage`` / ``#validation`` / …
deep-links alias here (``command_center_shell._LEGACY_PANEL_REDIRECTS``).

Pure composition: every section is one of the existing ``render_*`` panel
builders, each of which already degrades to a quiet stub on missing data, so the
console is robust by construction. A builder that *raises* is caught and
rendered as a small error card rather than taking the whole console down. No new
CSS — the kit classes (``.k-chip``, ``panel_toolbar``) + each builder's own
styling carry the page.
"""

from __future__ import annotations

import traceback
from collections.abc import Callable
from html import escape
from typing import TYPE_CHECKING

from identity import DEFAULT_USER_ID
from ui.controls import panel_toolbar

if TYPE_CHECKING:
    from pathlib import Path

# (anchor id, nav label, builder thunk). Order = display order; Coverage leads
# (kept prominent per the owner), then the two data-quality surfaces
# (Validation, Evals), then the freshness/lineage builders.
_SectionSpec = tuple[str, str, Callable[[], str]]


def _safe(name: str, fn: Callable[[], str]) -> str:
    """Render one builder, degrading a raised builder to an error card so a
    single broken diagnostic never blanks the whole console."""
    try:
        return fn()
    except Exception as exc:
        detail = escape(f"{type(exc).__name__}: {exc}")
        tb = escape(traceback.format_exc()[-1500:])
        return (
            '<section class="panel"><h2>'
            f"{escape(name)}</h2>"
            '<p class="muted">This diagnostic failed to render — the rest of the '
            "console is unaffected.</p>"
            f"<details><summary>{detail}</summary><pre>{tb}</pre></details></section>"
        )


def render_provenance_panel(
    db_path: Path,
    repo_root: Path,
    *,
    user_id: str = DEFAULT_USER_ID,
) -> str:
    """The consolidated System → Provenance tab fragment.

    Composes the 8 diagnostics builders into one page, Coverage first. Each
    section is wrapped in ``<div id="prov-<id>">`` so the anchor-nav chips jump
    to it. Lazy-imported builders keep the shell's import graph light.
    """
    from pipeline.credibility_panel import render_credibility_panel
    from pipeline.cron_health_panel import render_cron_health_panel
    from pipeline.dcf_coverage_panel import render_dcf_coverage_panel
    from pipeline.evals_panel import render_evals_panel
    from pipeline.fact_overrides_panel import render_fact_overrides_panel
    from pipeline.ir_coverage_panel import render_ir_coverage_panel
    from pipeline.restatements_panel import render_restatements_panel
    from pipeline.section_coverage_panel import render_section_coverage_panel
    from pipeline.source_calls_panel import render_source_calls_panel
    from pipeline.validation_issues_panel import render_validation_panel

    sections: list[_SectionSpec] = [
        # Coverage leads — the owner: "Coverage already makes sense, keep it
        # prominent." Not buried in sub-nav anymore; it's the first surface.
        (
            "coverage",
            "Coverage",
            lambda: render_section_coverage_panel(db_path, repo_root, user_id=user_id),
        ),
        ("validation", "Validation", lambda: render_validation_panel(db_path)),
        ("evals", "Evals", lambda: render_evals_panel(db_path)),
        ("ir_coverage", "IR Docs", lambda: render_ir_coverage_panel(db_path)),
        ("source_calls", "Data Cache", lambda: render_source_calls_panel(db_path)),
        ("cron_health", "Cron Health", lambda: render_cron_health_panel(db_path)),
        ("dcf_coverage", "DCF Coverage", lambda: render_dcf_coverage_panel(db_path, repo_root)),
        ("restatements", "Restatements", lambda: render_restatements_panel(db_path)),
        # Overrides — the durable company-doc figures that supersede FMP at read
        # time (the provenance-override layer). Sits by Restatements: both are
        # "a more authoritative figure wins", one detected, one asserted.
        ("overrides", "Overrides", lambda: render_fact_overrides_panel(db_path)),
        # Credibility scores the confidence prior against the restatement +
        # disagreement ground truth — it follows Restatements, which is that
        # ground truth surfaced.
        ("credibility", "Credibility", lambda: render_credibility_panel(db_path, user_id=user_id)),
    ]

    # Anchor-nav band (one operating band, design_language §6.1): jump-to chips
    # for the long page. The nav owns the "Provenance" title, so it's suppressed.
    # Chips scroll via JS (data-prov-jump), NOT an href="#anchor": the shell's
    # hashchange router treats an unknown hash as a panel id and would fall back
    # to Overview — navigating AWAY from the console. A data-attr + scrollIntoView
    # never touches the hash, so the router never fires.
    nav = "".join(
        f'<button type="button" class="k-chip k-chip-btn" data-prov-jump="prov-{anchor}">'
        f"{escape(label)}</button>"
        for anchor, label, _ in sections
    )
    toolbar = panel_toolbar(
        "Provenance",
        filters=nav,
        suppress_title=True,
    )

    body = "".join(
        f'<div class="prov-sec" id="prov-{anchor}">{_safe(label, fn)}</div>'
        for anchor, label, fn in sections
    )
    return f'<div class="prov-console">{toolbar}{body}</div><script>{_PROV_NAV_JS}</script>'


# One guarded document-level listener (re-injected fragments never double-wire)
# that scrolls to a section without changing location.hash — see render_*'s note
# on why an href anchor would break the shell router.
_PROV_NAV_JS = """
(function () {
  if (window.__ccProvNav) return;
  window.__ccProvNav = true;
  document.addEventListener('click', function (ev) {
    var b = ev.target && ev.target.closest ? ev.target.closest('[data-prov-jump]') : null;
    if (!b) return;
    ev.preventDefault();
    var el = document.getElementById(b.getAttribute('data-prov-jump'));
    if (el) el.scrollIntoView({ behavior: 'smooth', block: 'start' });
  });
})();
""".strip()
