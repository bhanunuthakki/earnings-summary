"""Cheap shell for the consolidated Provenance diagnostics console.

The System theme exposes one Provenance tab with Coverage kept first and an
anchor navigation band. Each diagnostic already has a standalone panel route,
so this assembler defers those database-heavy builders until their section is
revealed instead of serially building all eleven before first paint.
"""

from __future__ import annotations

from html import escape
from typing import TYPE_CHECKING

from identity import DEFAULT_USER_ID
from ui.controls import panel_toolbar

if TYPE_CHECKING:
    from pathlib import Path

# (anchor id, navigation label, standalone panel id). The display order is the
# product contract: Coverage remains prominent and leads.
_SectionSpec = tuple[str, str, str]
_SECTIONS: tuple[_SectionSpec, ...] = (
    ("coverage", "Coverage", "section_coverage"),
    ("validation", "Validation", "validation"),
    ("evals", "Evals", "evals"),
    ("model_eval", "Optimizer", "model_eval"),
    ("ir_coverage", "IR Docs", "ir_coverage"),
    ("source_calls", "Data Cache", "source_calls"),
    ("cron_health", "Cron Health", "cron_health"),
    ("dcf_coverage", "DCF Coverage", "dcf_coverage"),
    ("restatements", "Restatements", "restatements"),
    ("overrides", "Overrides", "overrides"),
    ("credibility", "Credibility", "credibility"),
)


def render_provenance_panel(
    db_path: Path,
    repo_root: Path,
    *,
    user_id: str = DEFAULT_USER_ID,
) -> str:
    """Render the consolidated shell without executing diagnostic builders.

    The arguments remain part of the public renderer contract; the standalone
    routes own their corresponding database/repository inputs.
    """
    del db_path, repo_root, user_id
    nav = "".join(
        f'<button type="button" class="k-chip k-chip-btn" '
        f'data-prov-jump="prov-{anchor}">{escape(label)}</button>'
        for anchor, label, _panel_id in _SECTIONS
    )
    toolbar = panel_toolbar(
        "Provenance",
        filters=nav,
        suppress_title=True,
    )
    body = "".join(
        f'<div class="prov-sec" id="prov-{anchor}">'
        f'<div class="cc-loading" hx-get="/api/panel/{panel_id}" '
        'hx-trigger="revealed" hx-swap="innerHTML">'
        f'<span class="muted">Loading {escape(label)}...</span>'
        "</div></div>"
        for anchor, label, panel_id in _SECTIONS
    )
    return f'<div class="prov-console">{toolbar}{body}</div><script>{_PROV_NAV_JS}</script>'


# One guarded document-level listener (re-injected fragments never double-wire)
# that scrolls without changing location.hash. The shell uses the hash as its
# panel router, so normal href anchors would navigate away from Provenance.
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
