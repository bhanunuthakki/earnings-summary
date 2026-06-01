"""Portfolio tab renderer for the command-center shell.

Assembles the live portfolio view (positions / % of book / taxable breakdown /
latest transactions, pulled from the companion portfolio-tracker REST API) on top
of the cached ``cross_portfolio_synthesis`` lens memo. Degrades to a "tracker
offline" note when the tracker isn't reachable — the synthesis still renders.

Reuses the dark panel/table/kpi-strip CSS vocabulary the shell already defines, so
there is no new styling here.
"""

from __future__ import annotations

from html import escape
from pathlib import Path

from integrations.portfolio_tracker_client import (
    TAX_BUCKETS,
    LivePortfolio,
    fetch_live_portfolio,
)

_TAX_LABELS: dict[str, str] = {
    "taxable": "Taxable",
    "tax_deferred": "Tax-deferred",
    "tax_free": "Tax-free",
    "unknown": "Unknown",
}


def render_portfolio_panel(db_path: Path, *, api_url: str | None = None) -> str:
    """The Portfolio tab fragment: the live positions/taxable view (from the
    tracker) followed by the cached cross-portfolio synthesis memo."""
    # Lazy imports keep the analytical builder out of this module's import graph
    # until the panel is actually requested.
    from pipeline.analytical_dashboard import build_analytical_dashboard
    from pipeline.analytical_dashboard_html import render_panel_fragment

    live = fetch_live_portfolio(api_url=api_url)
    dash = build_analytical_dashboard(db_path, sections={"portfolio_synthesis"})
    synthesis = render_panel_fragment(dash, "portfolio") or ""
    return render_live_portfolio_section(live) + synthesis


def render_live_portfolio_section(live: LivePortfolio) -> str:
    """The live-positions panel: total + taxable-bucket KPI strip, a positions
    table with % of portfolio, and the latest transactions. Renders an offline
    note (with the start hint) when the tracker is unreachable."""
    if not live.available:
        return (
            '<section class="panel"><h2>Live portfolio</h2>'
            '<p class="muted">Portfolio-tracker API not reachable at '
            f"<code>{escape(live.api_url)}</code>"
            f"{f' — {escape(live.error)}' if live.error else ''}. "
            "Start it alongside this app to see live positions, % of book, and the "
            "taxable breakdown:</p>"
            '<pre class="cli-hint">cd ../portfolio-tracker &amp;&amp; '
            "uvicorn portfolio_tracker.api.main:app --port 8000</pre>"
            "</section>"
        )
    if not live.positions:
        return (
            '<section class="panel"><h2>Live portfolio</h2>'
            '<p class="muted">Tracker reachable, but it reports no current holdings.</p></section>'
        )

    out: list[str] = [
        '<section class="panel"><h2>Live portfolio</h2>',
        '<p class="sub">Live positions from the companion portfolio-tracker · '
        "% of book and taxable status derived per account.</p>",
        _summary_strip(live),
        _positions_table(live),
    ]
    out.append("</section>")
    out.append(_transactions_section(live))
    return "".join(out)


def _summary_strip(live: LivePortfolio) -> str:
    cards = [
        '<div class="kpi-card"><div class="kpi-label">Total market value</div>'
        f'<div class="kpi-value">{_money(live.total_market_value)}</div></div>'
    ]
    total = live.total_market_value
    for bucket in TAX_BUCKETS:
        val = live.by_tax_treatment.get(bucket, 0.0)
        if val <= 0:
            continue
        pct = f"{100.0 * val / total:.0f}%" if total > 0 else "—"
        cards.append(
            f'<div class="kpi-card"><div class="kpi-label">{escape(_TAX_LABELS[bucket])}</div>'
            f'<div class="kpi-value">{_money(val)}</div>'
            f'<div class="kpi-sub">{pct} of book</div></div>'
        )
    return f'<div class="kpi-strip">{"".join(cards)}</div>'


def _positions_table(live: LivePortfolio) -> str:
    rows: list[str] = []
    # Largest position first.
    for p in sorted(live.positions, key=lambda x: -(x.market_value or 0.0)):
        treatments = sorted({lot.tax_treatment for lot in p.accounts})
        treat_str = ", ".join(_TAX_LABELS.get(t, t) for t in treatments) or "—"
        pnl = p.unrealized_pnl
        pnl_cell = (
            f'<td class="num {"pos" if pnl >= 0 else "neg"}">{_money(pnl)}</td>'
            if pnl is not None
            else '<td class="num muted">—</td>'
        )
        pct = f"{p.percent_of_portfolio:.1f}%" if p.percent_of_portfolio is not None else "—"
        ticker = p.ticker or "—"
        ticker_cell = (
            f'<a href="../research/{escape(ticker)}/" class="ticker-link">{escape(ticker)}</a>'
            if p.ticker
            else "—"
        )
        rows.append(
            "<tr>"
            f"<td>{ticker_cell}</td>"
            f"<td>{escape(p.name or '—')}</td>"
            f'<td class="num">{p.quantity:,.2f}</td>'
            f'<td class="num">{_money(p.market_value)}</td>'
            f'<td class="num">{pct}</td>'
            f"{pnl_cell}"
            f"<td>{escape(treat_str)}</td>"
            "</tr>"
        )
    return (
        '<table class="positions-table"><thead><tr>'
        "<th>Ticker</th><th>Name</th>"
        '<th class="num">Shares</th><th class="num">Market value</th>'
        '<th class="num">% of book</th><th class="num">Unrealized P&amp;L</th>'
        "<th>Tax treatment</th>"
        "</tr></thead><tbody>"
        f"{''.join(rows)}"
        "</tbody></table>"
    )


def _transactions_section(live: LivePortfolio) -> str:
    if not live.transactions:
        return ""
    rows: list[str] = []
    for t in live.transactions:
        kind = t.type + (f" · {t.subtype}" if t.subtype else "")
        qty = f"{t.quantity:,.2f}" if t.quantity is not None else "—"
        rows.append(
            "<tr>"
            f"<td>{escape(t.date[:10])}</td>"
            f"<td>{escape(t.ticker or '—')}</td>"
            f"<td>{escape(kind)}</td>"
            f'<td class="num">{qty}</td>'
            f'<td class="num">{_money(t.amount)}</td>'
            f"<td>{escape(t.account_name)}</td>"
            "</tr>"
        )
    return (
        '<section class="panel"><h2>Latest transactions</h2>'
        '<p class="sub">Most recent trades + cashflows across all linked accounts.</p>'
        '<table class="txn-table"><thead><tr>'
        "<th>Date</th><th>Ticker</th><th>Type</th>"
        '<th class="num">Shares</th><th class="num">Amount</th><th>Account</th>'
        "</tr></thead><tbody>"
        f"{''.join(rows)}"
        "</tbody></table></section>"
    )


def _money(v: float | None) -> str:
    if v is None:
        return "—"
    if abs(v) >= 1000:
        return f"${v:,.0f}"
    return f"${v:,.2f}"
