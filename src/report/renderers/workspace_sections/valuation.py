"""Valuation tab: chosen-multiple basis card with history band.

Split out of ``workspace_html.py`` (S13 renderer modularization). The
public entry point and output contract live in ``workspace_html``;
names here keep their original (underscore) spellings and are exported
via ``__all__`` for the package-internal imports and the back-compat
re-exports in ``workspace_html``."""

from __future__ import annotations

from io import StringIO

from report.models import SectionStatus, ValuationBasisSection
from report.renderers.workspace_charts import SparklineSize, sparkline
from report.renderers.workspace_sections._shared import (
    _empty_panel,
    _esc,
    _missing_panel,
    _panel_head,
    _render_markdown,
    _xlink_html,
)

__all__ = [
    "_TIMES",
    "_valuation_tab",
]


# Editorial typography: hoisted so call sites stay ruff-clean (RUF001).
# Built via chr() so the source stays ASCII-only.
_TIMES = chr(0x00D7)  # multiplication sign for WACC x g header


def _valuation_tab(body: StringIO, vb: ValuationBasisSection | None) -> None:
    """Render the valuation tab. Headline: Opus-picked multiple + current value.
    Detail: 12Q trend sparkline + min/median/max band + rich/cheap verdict +
    Opus rationale + optional target band notes.

    Pure consumer of `ValuationBasisSection` — no analytical logic here, no
    multiple computation, no LLM calls. All of that lives in
    `src/compute/valuation_basis.py` and `src/report/sections/valuation.py`.
    """
    body.write('<div class="tab-body">')
    body.write('<div class="eyebrow">Valuation · Opus-picked multiple · 12Q context</div>')
    if vb is None or vb.status != SectionStatus.OK:
        if vb is None:
            _empty_panel(
                body,
                "Valuation basis",
                "No valuation multiple has been worked up for this name yet.",
            )
        else:
            _missing_panel(body, vb.status, vb.missing, title="Valuation basis")
        body.write("</div>")
        return

    # Headline panel: chosen multiple, current value, rich/cheap.
    body.write(
        _panel_head(
            vb.multiple_name or "—",
            as_of=vb.current_period_end.isoformat() if vb.current_period_end else None,
            classes="valuation-headline",
        )
    )
    body.write('<div class="valuation-headline-row">')
    body.write(
        '<div class="valuation-current">'
        f'<div class="valuation-current-value">{_esc(vb.current_value_display or "—")}</div>'
        '<div class="valuation-current-label">current</div>'
        "</div>"
    )
    if vb.historical_median is not None:
        body.write(
            '<div class="valuation-band">'
            f'<div class="valuation-band-row">Range '
            f'<span class="mono">{vb.historical_min:.1f}x</span> &ndash; '
            f'<span class="mono">{vb.historical_max:.1f}x</span></div>'
            f'<div class="valuation-band-row">Median '
            f'<span class="mono">{vb.historical_median:.1f}x</span></div>'
            "</div>"
        )
    # PEG (only when the chosen multiple is P/E (NTM) and forward EPS growth is
    # positive — the compute layer leaves peg_ratio None elsewhere, so the row
    # self-skips for P/B banks, EV/EBITDA, FCF multiples, and unprofitable /
    # negative-growth names).
    if vb.peg_ratio is not None:
        growth_txt = f"{vb.peg_growth_pct:.1f}%" if vb.peg_growth_pct is not None else "—"
        pe_txt = vb.current_value_display or "—"
        body.write(
            '<div class="valuation-peg" '
            f'title="PEG = {_esc(pe_txt)} P/E (NTM) &divide; {_esc(growth_txt)} forward EPS growth">'
            f'<div class="valuation-peg-value">{vb.peg_ratio:.2f}</div>'
            '<div class="valuation-peg-label">PEG (NTM)</div>'
            f'<div class="valuation-peg-sub">{_esc(pe_txt)} &divide; {_esc(growth_txt)} fwd EPS growth</div>'
            "</div>"
        )
    if vb.rich_cheap_verdict:
        body.write(f'<div class="valuation-verdict">{_esc(vb.rich_cheap_verdict)}</div>')
    body.write("</div>")

    # Sparkline of 12Q history. Drop None values — sparkline doesn't
    # handle them (NTM history has gaps for periods where the
    # forward-4Q realized series isn't fully on file).
    hist_values = [h.value for h in vb.history if h.value is not None]
    if hist_values:
        body.write(
            f'<div class="valuation-spark">{sparkline(hist_values, size=SparklineSize.VALUATION)}</div>'
        )
        if vb.history:
            body.write(
                '<div class="valuation-spark-axis">'
                f"<span>{vb.history[0].period_end.isoformat() if vb.history[0].period_end else '—'}</span>"
                f'<span class="muted">{len(vb.history)}q trailing</span>'
                f"<span>{vb.history[-1].period_end.isoformat() if vb.history[-1].period_end else '—'}</span>"
                "</div>"
            )
    body.write("</div>")  # /headline panel

    # Rationale panel.
    if vb.rationale:
        body.write(
            _panel_head(
                "Why this multiple",
                sub="model rationale",
                links=_xlink_html("thesis", "thesis KPI drivers →", "panel-kpi-ledger"),
                attrs='data-commentable="true" data-anchor-type="valuation_rationale" '
                'data-anchor-key="valuation_rationale" data-anchor-tab="valuation"',
            )
            + f'<div class="prose-pad">{_render_markdown(vb.rationale)}</div>'
            "</div>"
        )

    # Target band / notes.
    if vb.notes:
        body.write(
            _panel_head("Target read", sub="where this should trade")
            + f'<div class="prose-pad">{_render_markdown(vb.notes)}</div>'
            "</div>"
        )

    body.write("</div>")
