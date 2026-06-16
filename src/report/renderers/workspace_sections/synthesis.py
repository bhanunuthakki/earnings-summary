"""Synthesis tab: cached cross-section lens artifacts.

Split out of ``workspace_html.py`` (S13 renderer modularization). The
public entry point and output contract live in ``workspace_html``;
names here keep their original (underscore) spellings and are exported
via ``__all__`` for the package-internal imports and the back-compat
re-exports in ``workspace_html``."""

from __future__ import annotations

from io import StringIO

from report.models import SectionStatus, SynthesisSection
from report.renderers.workspace_sections._shared import _empty_panel, _esc, _render_markdown

__all__ = [
    "_LENS_LABELS",
    "_synthesis_tab",
]


_LENS_LABELS: dict[str, tuple[str, str]] = {
    # name -> (display_label, one-line description)
    "five_min_reread": (
        "5-min reread",
        "What changed · recommended action · what would change my mind",
    ),
    "thesis_drift_qoq": ("Thesis drift Q/Q", "How this quarter engaged with the prior bear case"),
    "bull_case": ("Bull case", "What would have to happen for this to work spectacularly"),
    "reverse_dcf": ("Reverse DCF", "What the market is implying vs the thesis"),
    "underweighted_facts": ("Underweighted facts", "5 things consensus is missing this quarter"),
    "catalyst_calendar": ("Catalyst calendar", "Upcoming catalysts + bingo card"),
    "filing_diff_narrative": ("Filing diff narrative", "10-K Item 1A year-over-year changes"),
    "footnote_anomaly": ("Footnote anomalies", "Item 8 footnote signals consensus ignores"),
}


def _synthesis_tab(body: StringIO, section: SynthesisSection | None) -> None:
    """§14 — render cached lens artifacts as collapsible panels.

    The first lens (typically five_min_reread per LENS_ORDER) renders OPEN by
    default — it's the decision-layer artifact. Subsequent lenses render
    collapsed via <details>. Each panel header carries the lens label +
    age + model used.
    """
    body.write('<div class="tab-body">')
    if section is None or section.status != SectionStatus.OK or not section.lenses:
        body.write(
            '<div class="row-split"><div>'
            '<div class="eyebrow">Cross-section synthesis</div>'
            '<h2 class="section-title">Synthesis</h2></div></div>'
        )
        _empty_panel(
            body,
            "Analytical lenses",
            "No analytical lenses are cached for this name yet — they are "
            "written on the next full analysis build.",
            reason="none cached",
        )
        body.write("</div>")
        return

    body.write('<div class="row-split"><div>')
    body.write('<div class="eyebrow">Cross-section synthesis</div>')
    body.write(f'<h2 class="section-title">Synthesis · {len(section.lenses)} lens artifacts</h2>')
    body.write('<p class="sub">Cached cross-section analytical reads.</p>')
    body.write("</div></div>")

    for idx, lens in enumerate(section.lenses):
        label, sub = _LENS_LABELS.get(lens.name, (lens.name, ""))
        is_first = idx == 0
        age_str = ""
        if lens.generated_at:
            from datetime import UTC as _UTC
            from datetime import datetime as _dt

            age = _dt.now(_UTC) - lens.generated_at
            if age.days >= 1:
                age_str = f" · {age.days}d ago"
            else:
                age_str = f" · {int(age.total_seconds() / 3600)}h ago"
        # Filled status markers ride the control kit's .k-pill (dirty = warn,
        # stale = neutral bare) — no bespoke per-surface pill skin.
        warn = (
            ' <span class="k-pill k-pill-warn">DIRTY</span>'
            if lens.is_dirty
            else (' <span class="k-pill">STALE</span>' if lens.is_stale else "")
        )
        model_str = f" · {lens.model}" if lens.model else ""

        # First lens (5-min reread typically) renders OPEN; the rest use the
        # canonical <details class="panel"> collapse idiom (P4.1).
        head_inner = (
            f'<span class="panel-title">{_esc(label)}{warn}</span>'
            f'<span class="panel-sub">{_esc(sub)}{age_str}{model_str}</span>'
        )
        lens_cls = f"panel lens-panel lens-{_esc(lens.name)}"
        # L12: a lens that authored inline [n] markers carries the chip payloads
        # to resolve them (else .citations is empty → render_prose unchanged).
        prose = _render_markdown(lens.content_md, citations=lens.citations)
        if is_first:
            body.write(
                f'<div class="{lens_cls}"><div class="panel-head">{head_inner}</div>'
                f'<div class="prose-pad lens-body">{prose}</div>'
                "</div>"
            )
        else:
            body.write(
                f'<details class="{lens_cls}"><summary class="panel-head">{head_inner}</summary>'
                f'<div class="prose-pad lens-body">{prose}</div>'
                "</details>"
            )

    body.write("</div>")
