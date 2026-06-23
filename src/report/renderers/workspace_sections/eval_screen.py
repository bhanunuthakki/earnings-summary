"""Evaluation screen: quick-categorization table + peer comp (eval flavor).

Split out of ``workspace_html.py`` (S13 renderer modularization). The
public entry point and output contract live in ``workspace_html``;
names here keep their original (underscore) spellings and are exported
via ``__all__`` for the package-internal imports and the back-compat
re-exports in ``workspace_html``."""

from __future__ import annotations

from io import StringIO

from report.models import EvaluationSnapshotSection, SectionStatus
from report.renderers.workspace_sections._shared import _esc, _missing_panel, _panel_head
from report.sections.p3_data import PeerCompRow
from ui import living_grid as lg

__all__ = [
    "_eval_cell",
    "_eval_screen_panels",
    "_fmt_usd_compact",
    "_peer_comp_panel",
]


def _eval_screen_panels(
    body: StringIO,
    eval_snap: EvaluationSnapshotSection,
    peer_comp: list[PeerCompRow] | None,
) -> None:
    """The quick-categorization table + peer comparison — shared by the legacy
    Eval Screen tab and the Company tab's "numbers at a glance" block (PR7,
    where the evaluation flavor now lands)."""
    if eval_snap.status != SectionStatus.OK and not eval_snap.rows:
        _missing_panel(body, eval_snap.status, eval_snap.missing, title="Quick categorization")
        _peer_comp_panel(body, peer_comp or [])
        return

    fy = eval_snap.fiscal_years
    lfy_minus_2_lbl = f"FY{fy[0]}" if len(fy) >= 1 else "LFY-2"
    lfy_minus_1_lbl = f"FY{fy[1]}" if len(fy) >= 2 else "LFY-1"
    lfy_lbl = f"FY{fy[2]}" if len(fy) >= 3 else "LFY"

    body.write(
        _panel_head(
            "Numbers at a glance",
            sub=f"{len(eval_snap.rows)} metrics · {lfy_minus_2_lbl} -> TTM",
        )
        + '<div class="table-scroll"><table class="tbl tbl-nowrap"><thead><tr>'
        "<th>Metric</th>"
        f'<th class="num">{lfy_minus_2_lbl}</th>'
        f'<th class="num">{lfy_minus_1_lbl}</th>'
        f'<th class="num">{lfy_lbl}</th>'
        '<th class="num">TTM</th>'
        '<th class="num">3y CAGR</th>'
        "</tr></thead><tbody>"
    )
    for r in eval_snap.rows:
        body.write(f"<tr><td>{_esc(r.metric)}</td>")
        for v in (r.lfy_minus_2, r.lfy_minus_1, r.lfy, r.ttm):
            body.write(_eval_cell(v, r.unit, r.digits))
        if r.cagr_3y is not None:
            cls = "num pos" if r.cagr_3y >= 0 else "num neg"
            body.write(f'<td class="{cls}">{r.cagr_3y * 100:+.1f}%</td>')
        else:
            body.write('<td class="num muted">—</td>')
        body.write("</tr>")
    body.write("</tbody></table></div></div>")

    _peer_comp_panel(body, peer_comp or [])


def _peer_comp_panel(body: StringIO, rows: list[PeerCompRow]) -> None:
    """Comparable-company table for the Eval Screen — market cap, revenue,
    net margin, ROIC, selected by the P4.2 scored picker (named rival /
    same industry / similar scale), with the selection basis shown per row.

    Hidden entirely when nothing scores (P4.2 hide-don't-stub): an
    unexplained or wrong peer list is worse than none — the owner flagged
    the old alphabetical FMP slice as wrong.
    """
    if not rows:
        return
    # The peers panel carries a structured `peer_comp` anchor so a comment on
    # the comparable set is classified as `curate_peers` and routed to a
    # persisted peer-override artifact — not flattened into a memo (S5, the
    # owner's "these are shit peers, remove unless better" feedback).
    body.write(
        _panel_head(
            "Peer comparison",
            sub=f"{len(rows)} comparable{'s' if len(rows) != 1 else ''} · TTM key metrics",
            classes="peer-comp-panel",
            attrs=(
                'data-commentable="true" data-anchor-type="peer_comp" '
                'data-anchor-key="peer_comp" data-anchor-tab="company"'
            ),
        )
    )
    body.write(lg.grid_open())
    body.write(lg.filter_bar(len(rows), noun="peers"))
    body.write(
        '<table class="tbl tbl-nowrap"><thead><tr>'
        + lg.th("Ticker", "ticker", "text", num=False)
        + lg.th("Name", "name", "text", num=False)
        + "<th>Why</th>"
        + lg.th("Market cap", "mcap", "num")
        + lg.th("Revenue TTM", "rev", "num")
        + lg.th("Net margin TTM", "margin", "num")
        + lg.th("ROIC TTM", "roic", "num")
        + "</tr></thead><tbody>"
    )
    for r in rows:
        why = " · ".join(r.match_reasons) if r.match_reasons else "—"
        data = (
            lg.data_text(f"{r.peer_ticker} {r.peer_name or ''} {why}")
            + lg.data_text_key("ticker", r.peer_ticker)
            + lg.data_text_key("name", r.peer_name)
            + lg.data_num("mcap", r.market_cap_usd)
            + lg.data_num("rev", r.revenue_ttm_usd)
            + lg.data_num("margin", r.net_margin_ttm)
            + lg.data_num("roic", r.roic_ttm)
        )
        body.write(f"<tr{data}>")
        body.write(f'<td><strong class="mono">{_esc(r.peer_ticker)}</strong></td>')
        body.write(f"<td>{_esc(r.peer_name or '—')}</td>")
        body.write(f'<td class="muted xsmall">{_esc(why)}</td>')
        body.write(
            f'<td class="num">{_fmt_usd_compact(r.market_cap_usd)}</td>'
            if r.market_cap_usd is not None
            else '<td class="num muted">—</td>'
        )
        body.write(
            f'<td class="num">{_fmt_usd_compact(r.revenue_ttm_usd)}</td>'
            if r.revenue_ttm_usd is not None
            else '<td class="num muted">—</td>'
        )
        body.write(
            f'<td class="num">{r.net_margin_ttm * 100:.1f}%</td>'
            if r.net_margin_ttm is not None
            else '<td class="num muted">—</td>'
        )
        body.write(
            f'<td class="num">{r.roic_ttm * 100:.1f}%</td>'
            if r.roic_ttm is not None
            else '<td class="num muted">—</td>'
        )
        body.write("</tr>")
    body.write("</tbody></table>")
    body.write(lg.grid_close())
    body.write("</div>")


def _eval_cell(v: float | None, unit: str, digits: int) -> str:
    if v is None:
        return '<td class="num muted">—</td>'
    if unit == "%":
        return f'<td class="num">{v:.{digits}f}%</td>'
    if unit.startswith("USD M") and abs(v) >= 1000:
        return f'<td class="num">{v / 1000:.1f}B</td>'
    return f'<td class="num">{v:,.{digits}f}</td>'


def _fmt_usd_compact(v: float) -> str:
    """Compact USD: $1.2T, $850B, $45M. Used in the eval header."""
    abs_v = abs(v)
    if abs_v >= 1e12:
        return f"${v / 1e12:.1f}T"
    if abs_v >= 1e9:
        return f"${v / 1e9:.1f}B"
    if abs_v >= 1e6:
        return f"${v / 1e6:.0f}M"
    return f"${v:,.0f}"
