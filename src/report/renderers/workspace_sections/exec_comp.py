"""Executive compensation tab: NEO packages + insider signals + alignment memo.

Split out of ``workspace_html.py`` (S13 renderer modularization). The
public entry point and output contract live in ``workspace_html``;
names here keep their original (underscore) spellings and are exported
via ``__all__`` for the package-internal imports and the back-compat
re-exports in ``workspace_html``."""

from __future__ import annotations

from io import StringIO

from report.models import ExecCompSectionModel, SectionStatus
from report.renderers.workspace_sections._shared import (
    _esc,
    _missing_panel,
    _panel_head,
    _render_markdown,
)
from ui import living_grid as lg

__all__ = [
    "_exec_comp_tab",
    "_fmt_usd_short",
]


def _exec_comp_tab(body: StringIO, section: ExecCompSectionModel | None) -> None:
    """§13 — NEO compensation packages + recent insider activity + alignment.

    Four panels in order:
      1. Alignment narrative (LLM-driven, may be absent on --no-llm runs)
      2. Anomaly flags ribbon
      3. NEO compensation table (latest year)
      4. Recent insider transactions ranked by conviction signal
    """
    body.write('<div class="tab-body">')
    if section is None or section.status == SectionStatus.MISSING_DATA:
        _missing_panel(
            body,
            section.status if section else SectionStatus.MISSING_DATA,
            section.missing if section else None,
            title="Executive comp & insider activity",
        )
        body.write("</div>")
        return

    body.write('<div class="row-split"><div>')
    body.write('<div class="eyebrow">Compensation &amp; Alignment</div>')
    yr_label = f"FY {section.fiscal_year_latest}" if section.fiscal_year_latest else "Latest data"
    body.write(
        f'<h2 class="section-title">Executive comp &amp; insider activity · {_esc(yr_label)}</h2>'
    )
    body.write("</div></div>")

    # 1. Alignment narrative
    if section.alignment_narrative_md:
        body.write(
            _panel_head(
                "Alignment read",
                sub="Do management's comp metrics reward the analyst's thesis?",
            )
        )
        body.write(
            f'<div class="prose-pad">{_render_markdown(section.alignment_narrative_md)}</div></div>'
        )
    # (No alignment narrative → no panel; P4.2 hide-don't-stub.)

    # 2. Anomaly flags
    if section.anomaly_flags:
        body.write(_panel_head("Flags") + '<ul class="flag-list">')
        for f in section.anomaly_flags:
            tone = "flag-positive" if "POSITIVE" in f else "flag-warn"
            body.write(f'<li class="{tone}">{_esc(f)}</li>')
        body.write("</ul></div>")

    # 3. NEO comp table
    if section.packages:
        body.write(
            _panel_head(
                "Named-Executive-Officer compensation",
                sub=f"{len(section.packages)} executives · {section.packages[0].currency}",
            )
            + lg.grid_open()
            + lg.filter_bar(len(section.packages), noun="executives")
            + '<table class="tbl"><thead><tr>'
            + lg.th("Executive", "exec", "text", num=False)
            + lg.th("Role", "role", "text", num=False)
            + lg.th("Salary", "salary", "num")
            + lg.th("Bonus (actual/target)", "bonus", "num")
            + lg.th("Equity grant", "equity", "num")
            + lg.th("Total granted", "total", "num")
            + lg.th("Realized vs granted", "rvg", "num")
            + "<th>Performance metrics (weight%)</th>"
            + lg.th("KPI match", "kpimatch", "text", num=False)
            + "</tr></thead><tbody>"
        )
        for pkg in section.packages:
            ceo_pill = ' <span class="ceo-pill k-chip k-chip-mono">CEO</span>' if pkg.is_ceo else ""
            data = (
                lg.data_text(
                    f"{pkg.executive_name} {pkg.role or ''} {pkg.performance_metrics_summary or ''}"
                )
                + lg.data_text_key("exec", pkg.executive_name)
                + lg.data_text_key("role", pkg.role)
                + lg.data_text_key("kpimatch", "yes" if pkg.metrics_have_thesis_kpi else "no")
                + lg.data_num("salary", pkg.base_salary)
                + lg.data_num("bonus", pkg.cash_bonus_actual)
                + lg.data_num("equity", pkg.equity_grant_value)
                + lg.data_num("total", pkg.total_comp_granted)
                + lg.data_num("rvg", pkg.realized_vs_granted_pct)
            )
            body.write(f"<tr{data}>")
            body.write(f"<td>{_esc(pkg.executive_name)}{ceo_pill}</td>")
            body.write(f"<td>{_esc(pkg.role or '?')}</td>")
            body.write(f'<td class="num">{_fmt_usd_short(pkg.base_salary)}</td>')
            bonus_pair = _fmt_usd_short(pkg.cash_bonus_actual)
            if pkg.cash_bonus_target is not None and pkg.cash_bonus_target != pkg.cash_bonus_actual:
                bonus_pair += (
                    f' <span class="muted">/ {_fmt_usd_short(pkg.cash_bonus_target)} tgt</span>'
                )
            body.write(f'<td class="num">{bonus_pair}</td>')
            body.write(f'<td class="num">{_fmt_usd_short(pkg.equity_grant_value)}</td>')
            body.write(f'<td class="num">{_fmt_usd_short(pkg.total_comp_granted)}</td>')
            rvg = pkg.realized_vs_granted_pct
            if rvg is not None:
                tone = "pos" if rvg > 0 else "neg" if rvg < 0 else ""
                body.write(f'<td class="num {tone}">{rvg * 100:+.0f}%</td>')
            else:
                body.write('<td class="num muted">-</td>')
            body.write(f"<td>{_esc(pkg.performance_metrics_summary or '(none disclosed)')}</td>")
            match = "✓ matches thesis" if pkg.metrics_have_thesis_kpi else "—"
            cls = "kpi-match" if pkg.metrics_have_thesis_kpi else "muted"
            body.write(f'<td class="{cls}">{_esc(match)}</td>')
            body.write("</tr>")
        body.write("</tbody>")
        # CEO pay ratio footer in a real <tfoot> so the living grid's sort/filter
        # (tbody-only) never moves it.
        ceo = next((p for p in section.packages if p.is_ceo), None)
        if ceo and ceo.ceo_pay_ratio:
            body.write(
                f'<tfoot><tr><td colspan="9" class="table-footer">CEO pay ratio: '
                f"<strong>{ceo.ceo_pay_ratio:.0f}x</strong> median employee comp "
                f"(S&amp;P 500 average ~300x).</td></tr></tfoot>"
            )
        body.write("</table>")
        body.write(lg.grid_close())
        body.write("</div>")

    # 4. Insider transactions
    if section.insider_signals:
        body.write(
            _panel_head(
                "Recent insider activity",
                sub=f"Top {len(section.insider_signals)} by conviction signal · last 12 months",
            )
            + lg.grid_open()
            + lg.filter_bar(len(section.insider_signals), noun="transactions")
            + '<table class="tbl insider-table"><thead><tr>'
            + lg.th("Date", "date", "text", num=False)
            + lg.th("Insider", "insider", "text", num=False)
            + lg.th("Role", "role", "text", num=False)
            + lg.th("Action", "action", "text", num=False)
            + lg.th("Shares", "shares", "num")
            + lg.th("Value", "value", "num")
            + lg.th("Signal", "signal", "num")
            + "<th>Why</th>"
            + "</tr></thead><tbody>"
        )
        for s in section.insider_signals:
            tone = (
                "tx-buy"
                if "buy" in s.transaction_type
                else "tx-sell"
                if "sell" in s.transaction_type
                else ""
            )
            strength_pct = f"{int(s.signal_strength * 100)}"
            strength_tone = (
                "signal-strong"
                if s.signal_strength >= 0.6
                else "signal-medium"
                if s.signal_strength >= 0.3
                else "signal-weak"
            )
            action = s.transaction_type.replace("_", " ")
            data = (
                lg.data_text(f"{s.transaction_date} {s.insider_name} {s.role or ''} {action}")
                + lg.data_text_key("date", s.transaction_date)
                + lg.data_text_key("insider", s.insider_name)
                + lg.data_text_key("role", s.role)
                + lg.data_text_key("action", action)
                + lg.data_num("shares", s.shares)
                + lg.data_num("value", s.transaction_value)
                + lg.data_num("signal", s.signal_strength)
            )
            body.write(f'<tr class="{tone}"{data}>')
            body.write(f"<td>{_esc(s.transaction_date)}</td>")
            body.write(f"<td>{_esc(s.insider_name)}</td>")
            body.write(f"<td>{_esc(s.role or '?')}</td>")
            body.write(f"<td>{_esc(action)}</td>")
            body.write(f'<td class="num">{s.shares:,.0f}</td>')
            body.write(f'<td class="num">{_fmt_usd_short(s.transaction_value)}</td>')
            body.write(f'<td class="num {strength_tone}">{strength_pct}</td>')
            body.write(f"<td>{_esc(s.rationale)}</td>")
            body.write("</tr>")
        body.write("</tbody></table>")
        body.write(lg.grid_close())
        body.write("</div>")

    body.write("</div>")


def _fmt_usd_short(v: float | None) -> str:
    """Compact USD formatter for the comp table."""
    if v is None:
        return '<span class="muted">-</span>'
    av = abs(v)
    if av >= 1e9:
        return f"${v / 1e9:.1f}B"
    if av >= 1e6:
        return f"${v / 1e6:.1f}M"
    if av >= 1e3:
        return f"${v / 1e3:.0f}K"
    return f"${v:,.0f}"
