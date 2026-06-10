"""Portfolio theme page renderer for the command-center shell.

Master build P2.1 — the advisor's data foundation. The page leads with the
tracker's analytics (TWR vs SPY / QQQ / policy with the policy mix, risk stats
vs SPY, allocation + concentration cuts, per-position dollar alpha), then the
live positions / % of book / taxable breakdown / latest transactions, then the
cached ``cross_portfolio_synthesis`` lens memo. Every number in the analytics
sections comes from the tracker's API verbatim — benchmark math is never
rebuilt here (directive architecture rule); the only client-side arithmetic is
display formatting and the portfolio-minus-benchmark readout of two API values.

Degrades gracefully: tracker fully offline → ONE "tracker offline" note (with
the start hint) + the synthesis; tracker up but an analytics endpoint failing →
the other sections still render and the failed ones are named in a footnote.

Reuses the dark panel/table/kpi-strip CSS vocabulary the shell already defines;
the analytics-only additions (legend chips, allocation bars, the benchmark
chart) ship as a fragment-local ``<style>`` block keyed off the shared token
variables, and the chart's series colors come from ``ui.tokens.CHART_SERIES``.
"""

from __future__ import annotations

from collections.abc import Callable
from html import escape
from pathlib import Path

from integrations.portfolio_tracker_client import (
    TAX_BUCKETS,
    AllocationBucket,
    BetaStats,
    LivePortfolio,
    PerformancePoint,
    PerformanceSeries,
    PolicyMix,
    PortfolioAnalytics,
    PositionAlpha,
    Positioning,
    fetch_live_portfolio,
    fetch_portfolio_analytics,
)
from ui.tokens import CHART_SERIES

_TAX_LABELS: dict[str, str] = {
    "taxable": "Taxable",
    "tax_deferred": "Tax-deferred",
    "tax_free": "Tax-free",
    "unknown": "Unknown",
}

# Greek letters for the stat labels, via chr() so the source stays ASCII
# (RUF001 ambiguous-unicode) — same idiom as workspace_charts._RSQUO.
_ALPHA = chr(0x03B1)
_SIGMA = chr(0x03C3)


def render_portfolio_panel(db_path: Path, *, api_url: str | None = None) -> str:
    """The Portfolio theme page fragment: tracker analytics (performance / risk /
    positioning / alpha), the live positions/taxable view, then the cached
    cross-portfolio synthesis memo."""
    # Lazy imports keep the analytical builder out of this module's import graph
    # until the panel is actually requested.
    from pipeline.analytical_dashboard import build_analytical_dashboard
    from pipeline.analytical_dashboard_html import render_panel_fragment

    analytics = fetch_portfolio_analytics(api_url=api_url)
    live = fetch_live_portfolio(api_url=api_url)
    dash = build_analytical_dashboard(db_path, sections={"portfolio_synthesis"})
    synthesis = render_panel_fragment(dash, "portfolio") or ""
    return compose_portfolio_page(analytics, live, synthesis)


def compose_portfolio_page(
    analytics: PortfolioAnalytics, live: LivePortfolio, synthesis: str
) -> str:
    """Pure page assembly (testable without network or DB).

    Tracker fully down → the live section's single offline note carries the
    start hint for the whole page (no duplicate per-section offline panels).
    Tracker up but ALL analytics endpoints failing (e.g. an older tracker
    build) → one quiet note instead of five dead sections.
    """
    parts: list[str] = []
    if analytics.available:
        parts.append(render_portfolio_analytics_sections(analytics))
    elif live.available:
        first_error = next(iter(analytics.errors.values()), "no analytics payloads")
        parts.append(
            '<section class="panel"><h2>Portfolio analytics</h2>'
            '<p class="muted">The tracker is reachable but its analytics endpoints aren\'t — '
            f"{escape(first_error)}.</p></section>"
        )
    parts.append(render_live_portfolio_section(live))
    parts.append(synthesis)
    return "".join(parts)


# ---------------------------------------------------------------------------
# Tracker analytics sections (master build P2.1). Pure HTML assembly over the
# already-parsed PortfolioAnalytics — no network, no benchmark math.
# ---------------------------------------------------------------------------

# Styling only the analytics sections need; everything else reuses the shell's
# panel/kpi/table vocabulary. Colors key off the shared token variables so a
# palette change in ui/tokens.py propagates here untouched.
_ANALYTICS_CSS = """<style>
.pf-legend { display: flex; gap: 18px; flex-wrap: wrap; margin: 2px 0 10px; font-size: 12.5px; }
.pf-chip { display: inline-flex; align-items: center; gap: 6px; color: var(--muted); }
.pf-chip strong { color: var(--fg); font-variant-numeric: tabular-nums; }
.pf-swatch { width: 10px; height: 10px; border-radius: 2px; display: inline-block; }
.pf-chart { width: 100%; height: auto; display: block; }
.pf-policy { font-size: 12px; margin: 10px 0 0; }
.pf-warn { color: var(--warn); }
.pf-alloc-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr));
  gap: 10px 32px; margin-top: 4px; }
.pf-alloc-row { display: grid; grid-template-columns: minmax(110px, 1.3fr) 2fr 52px 76px;
  gap: 10px; align-items: center; font-size: 12.5px; padding: 3px 0; }
.pf-alloc-label { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.pf-bar { background: var(--hairline); border-radius: 3px; height: 10px; overflow: hidden; }
.pf-bar-fill { background: var(--accent); opacity: 0.75; height: 100%; display: block; }
.pf-alloc-pct { text-align: right; font-variant-numeric: tabular-nums; }
.pf-alloc-val { text-align: right; font-variant-numeric: tabular-nums; font-size: 11.5px; }
.pf-flag { color: var(--warn); margin-left: 4px; cursor: help; }
.pf-total td { font-weight: 600; border-top: 2px solid var(--border); }
.pf-degraded { font-size: 12px; }
</style>"""

_SECTION_LABELS: dict[str, str] = {
    "performance": "Performance vs benchmarks",
    "beta": "Risk vs benchmark",
    "positioning": "Positioning",
    "position_alpha": "Per-position alpha",
    "policy": "Policy mix",
}

# Chart/legend series: label, stroke, stroke-width, value extractor. The book's
# line is the bright foreground token; benchmarks use the shared categorical
# chart palette (Okabe-Ito) so they read apart without semantic green/red.
_CHART_SPECS: tuple[tuple[str, str, float, Callable[[PerformancePoint], float | None]], ...] = (
    ("Portfolio", "var(--fg)", 2.4, lambda p: p.portfolio_return_pct),
    ("SPY", CHART_SERIES[1], 1.3, lambda p: p.spy_return_pct),
    ("QQQ", CHART_SERIES[3], 1.3, lambda p: p.qqq_return_pct),
    ("Policy", CHART_SERIES[5], 1.3, lambda p: p.policy_return_pct),
)


def render_portfolio_analytics_sections(a: PortfolioAnalytics) -> str:
    """Every analytics section that loaded, in page order, plus one footnote
    naming the sections that didn't (instead of five dead panels)."""
    out: list[str] = [_ANALYTICS_CSS]
    if a.performance is not None:
        out.append(_performance_section(a.performance, a.policy))
    if a.beta is not None:
        out.append(_risk_section(a.beta))
    if a.positioning is not None:
        out.append(_positioning_section(a.positioning))
    if a.position_alpha is not None:
        out.append(_alpha_section(a.position_alpha))
    failed = [label for key, label in _SECTION_LABELS.items() if key in a.errors]
    if failed:
        out.append(
            '<p class="muted pf-degraded">Unavailable from the tracker right now: '
            f"{escape(', '.join(failed))}.</p>"
        )
    return "".join(out)


def _performance_section(perf: PerformanceSeries, policy: PolicyMix | None) -> str:
    window = f"{perf.start_date or '?'} → {perf.end_date or '?'}"
    head = (
        '<section class="panel"><h2>Performance vs benchmarks</h2>'
        '<p class="sub">Time-weighted return (Modified Dietz) from the tracker · each '
        "benchmark is a synthetic book receiving the same external cashflows · net external "
        f"inflow {_money(perf.net_external_cashflow_in)} over the window.</p>"
    )
    if not perf.points:
        return (
            f"{head}"
            '<p class="muted">Tracker returned no performance history for the window.</p>'
            f"{_policy_line(policy)}</section>"
        )

    finals: dict[str, float | None] = {
        label: next((v for p in reversed(perf.points) if (v := get(p)) is not None), None)
        for label, _color, _sw, get in _CHART_SPECS
    }
    cards: list[str] = [
        _kpi_card(
            "Portfolio TWR",
            _pct(finals["Portfolio"], signed=True),
            sub=window,
            tone=_tone(finals["Portfolio"]),
        )
    ]
    # Display delta of two tracker-computed returns (the dollar/regression alpha
    # readouts come from /position-alpha and /beta — never recomputed here).
    excess = (
        finals["Portfolio"] - finals["SPY"]
        if finals["Portfolio"] is not None and finals["SPY"] is not None
        else None
    )
    cards.append(_kpi_card("vs SPY", _pp(excess), sub="excess return", tone=_tone(excess)))
    for bench in ("SPY", "QQQ", "Policy"):
        if finals[bench] is not None:
            cards.append(_kpi_card(bench, _pct(finals[bench], signed=True), sub="cashflow-matched"))
    warn = (
        '<p class="muted">⚠ The window start value looks incomplete (backfill unreliable) — '
        "early benchmark gaps may overstate or understate relative performance.</p>"
        if perf.backfill_start_unreliable
        else ""
    )
    return (
        f"{head}"
        f'<div class="kpi-strip">{"".join(cards)}</div>'
        f"{_chart_legend(perf.points)}"
        f"{_benchmark_chart(perf.points)}"
        f"{_policy_line(policy)}"
        f"{warn}</section>"
    )


def _chart_legend(points: list[PerformancePoint]) -> str:
    chips: list[str] = []
    for label, color, _sw, get in _CHART_SPECS:
        final = next((v for p in reversed(points) if (v := get(p)) is not None), None)
        if final is None:
            continue
        chips.append(
            f'<span class="pf-chip"><span class="pf-swatch" style="background:{color}"></span>'
            f"{escape(label)} <strong>{_pct(final, signed=True)}</strong></span>"
        )
    return f'<div class="pf-legend">{"".join(chips)}</div>' if chips else ""


def _benchmark_chart(points: list[PerformancePoint]) -> str:
    """Static multi-series SVG of cumulative window return %. Presentation only:
    the values are plotted exactly as the tracker returned them (a light stride
    keeps the fragment small on year-long daily series; endpoints always kept)."""
    if len(points) > 240:
        stride = -(-len(points) // 240)  # ceil division
        sampled = points[::stride]
        if sampled[-1] is not points[-1]:
            sampled.append(points[-1])
        points = sampled
    series: list[tuple[str, str, float, list[tuple[int, float]]]] = []
    for label, color, sw, get in _CHART_SPECS:
        coords = [(i, v) for i, p in enumerate(points) if (v := get(p)) is not None]
        if len(coords) >= 2:
            series.append((label, color, sw, coords))
    if not series:
        return ""

    all_vals = [v for _label, _color, _sw, coords in series for _i, v in coords]
    lo = min(min(all_vals), 0.0)  # keep the 0% line in frame
    hi = max(max(all_vals), 0.0)
    pad = (hi - lo or 1.0) * 0.08
    y0, y1 = lo - pad, hi + pad
    width, height = 860.0, 240.0
    pad_t, pad_r, pad_b, pad_l = 10.0, 14.0, 22.0, 46.0
    plot_w, plot_h = width - pad_l - pad_r, height - pad_t - pad_b
    n = len(points)

    def x_of(i: int) -> float:
        return pad_l + (i / max(n - 1, 1)) * plot_w

    def y_of(v: float) -> float:
        return pad_t + plot_h - ((v - y0) / (y1 - y0)) * plot_h

    parts: list[str] = [
        f'<svg class="pf-chart" viewBox="0 0 {width:.0f} {height:.0f}" role="img" '
        'aria-label="Cumulative time-weighted return vs SPY, QQQ, and policy benchmarks">'
    ]
    for frac in (0.0, 1 / 3, 2 / 3, 1.0):
        tick = y0 + frac * (y1 - y0)
        ty = y_of(tick)
        parts.append(
            f'<line x1="{pad_l:.1f}" x2="{pad_l + plot_w:.1f}" y1="{ty:.1f}" y2="{ty:.1f}" '
            'stroke="var(--border)" stroke-width="0.5" stroke-dasharray="2 3" />'
        )
        parts.append(
            f'<text x="{pad_l - 6:.1f}" y="{ty + 3:.1f}" text-anchor="end" font-size="9.5" '
            f'fill="var(--muted)" font-family="var(--mono)">{tick:.0f}%</text>'
        )
    if y0 < 0.0 < y1:
        zy = y_of(0.0)
        parts.append(
            f'<line x1="{pad_l:.1f}" x2="{pad_l + plot_w:.1f}" y1="{zy:.1f}" y2="{zy:.1f}" '
            'stroke="var(--border-2)" stroke-width="0.8" />'
        )
    anchors = {0: "start", n // 2: "middle", n - 1: "end"}
    for i, anchor in anchors.items():
        parts.append(
            f'<text x="{x_of(i):.1f}" y="{height - 6:.1f}" text-anchor="{anchor}" '
            'font-size="9.5" fill="var(--muted)" font-family="var(--mono)">'
            f"{escape(points[i].date)}</text>"
        )
    for label, color, sw, coords in series:
        d = " ".join(
            ("M" if j == 0 else "L") + f"{x_of(i):.1f},{y_of(v):.1f}"
            for j, (i, v) in enumerate(coords)
        )
        parts.append(
            f'<path d="{d}" fill="none" stroke="{color}" stroke-width="{sw}" '
            f'stroke-linejoin="round" stroke-linecap="round"><title>{escape(label)}</title>'
            "</path>"
        )
    parts.append("</svg>")
    return "".join(parts)


def _policy_line(policy: PolicyMix | None) -> str:
    """The policy benchmark's target mix, as context for the policy line/cards."""
    if policy is None or not policy.weights:
        return ""
    chips = " · ".join(
        f"{escape(w.ticker)} {_pct(w.weight_pct, decimals=0)}" for w in policy.weights
    )
    warn = ""
    if not policy.is_balanced:
        warn = (
            f' <span class="pf-warn">(weights sum to {_pct(policy.total_pct, decimals=0)} '
            "— unbalanced)</span>"
        )
    return f'<p class="pf-policy muted">Policy mix: {chips}{warn}</p>'


def _risk_section(b: BetaStats) -> str:
    bench = b.benchmark or "SPY"
    rf = f" · risk-free {_pct_frac(b.risk_free_annual)}" if b.risk_free_annual is not None else ""
    samples = f" · {b.sample_size} daily samples" if b.sample_size is not None else ""
    cards = [
        _kpi_card(f"Beta vs {bench}", _ratio(b.beta)),
        _kpi_card(
            "Alpha (ann.)",
            _pct(b.alpha_annualized_pct, signed=True),
            tone=_tone(b.alpha_annualized_pct),
        ),
        _kpi_card("Sharpe", _ratio(b.sharpe)),
        _kpi_card("Sortino", _ratio(b.sortino)),
        _kpi_card("Info ratio", _ratio(b.information_ratio)),
        _kpi_card("Tracking error", _pct_frac(b.tracking_error_annualized), sub="annualized"),
        _kpi_card(
            f"Portfolio {_SIGMA}",
            _pct_frac(b.portfolio_volatility_annualized),
            sub=f"{bench} {_SIGMA} {_pct_frac(b.benchmark_volatility_annualized)}",
        ),
        _kpi_card("R²", _ratio(b.r_squared)),
    ]
    notes = f'<p class="muted">{escape("; ".join(b.notes))}</p>' if b.notes else ""
    return (
        '<section class="panel"><h2>Risk &amp; efficiency</h2>'
        f'<p class="sub">Daily-return regression vs {escape(bench)} from the tracker · '
        f"{escape(b.start_date or '?')} → {escape(b.end_date or '?')}{samples}{rf}.</p>"
        f'<div class="kpi-strip">{"".join(cards)}</div>'
        f"{notes}</section>"
    )


def _positioning_section(pos: Positioning) -> str:
    cards: list[str] = []
    conc = pos.concentration
    if conc is not None:
        if conc.num_positions is not None:
            cards.append(_kpi_card("Positions", str(conc.num_positions)))
        cards.append(_kpi_card("Top 1", _pct(conc.top1_weight_pct), sub="of book"))
        cards.append(_kpi_card("Top 5", _pct(conc.top5_weight_pct), sub="of book"))
        cards.append(_kpi_card("Top 10", _pct(conc.top10_weight_pct), sub="of book"))
        if conc.hhi is not None:
            cards.append(_kpi_card("HHI", f"{conc.hhi:,.0f}", sub="of 10,000"))
        if conc.effective_holdings is not None:
            cards.append(
                _kpi_card(
                    "Effective holdings",
                    _ratio(conc.effective_holdings, decimals=1),
                    sub="equal-position equivalent",
                )
            )
    if pos.weighted_avg_correlation_spy is not None:
        cards.append(
            _kpi_card(
                "Avg corr vs SPY",
                _ratio(pos.weighted_avg_correlation_spy),
                sub="value-weighted",
            )
        )
    blocks = "".join(
        _alloc_block(title, buckets)
        for title, buckets in (
            ("By asset type", pos.by_asset_type),
            ("By sector", pos.by_sector),
            ("By region", pos.by_region),
            ("By account type", pos.by_account_type),
        )
    )
    strip = f'<div class="kpi-strip">{"".join(cards)}</div>' if cards else ""
    grid = f'<div class="pf-alloc-grid">{blocks}</div>' if blocks else ""
    return (
        '<section class="panel"><h2>Positioning &amp; concentration</h2>'
        f'<p class="sub">Snapshot {escape(pos.snapshot_date or "?")} · '
        f"book {_money(pos.total_value)} · weights from the tracker's classification.</p>"
        f"{strip}{grid}</section>"
    )


def _alloc_block(title: str, buckets: list[AllocationBucket]) -> str:
    if not buckets:
        return ""
    rows: list[str] = []
    for b in sorted(buckets, key=lambda x: -(x.weight_pct or 0.0)):
        width = max(0.0, min(100.0, b.weight_pct or 0.0))
        tip = f"{b.label} · {b.count} name(s)" if b.count is not None else b.label
        rows.append(
            '<div class="pf-alloc-row">'
            f'<span class="pf-alloc-label" title="{escape(tip)}">{escape(b.label)}</span>'
            f'<span class="pf-bar"><span class="pf-bar-fill" style="width:{width:.1f}%">'
            "</span></span>"
            f'<span class="pf-alloc-pct">{_pct(b.weight_pct)}</span>'
            f'<span class="pf-alloc-val muted">{_money(b.value)}</span>'
            "</div>"
        )
    return f'<div class="pf-alloc-block"><h3 class="panel-h3">{escape(title)}</h3>{"".join(rows)}</div>'


def _alpha_section(pa: PositionAlpha) -> str:
    head = (
        '<section class="panel"><h2>Per-position alpha</h2>'
        f'<p class="sub">{escape(pa.start_date or "?")} → {escape(pa.end_date or "?")} · '
        "dollar alpha vs a counterfactual that routes each position's exact buys/sells into "
        f"the benchmark on the same days ({_ALPHA} = actual P&amp;L - benchmark P&amp;L).</p>"
    )
    if not pa.rows:
        return f'{head}<p class="muted">Tracker returned no positions for the window.</p></section>'
    show_policy = pa.has_policy
    policy_th = f'<th class="num">{_ALPHA} vs policy</th>' if show_policy else ""
    rows: list[str] = []
    for r in sorted(pa.rows, key=lambda x: (x.alpha is None, -(x.alpha or 0.0))):
        ticker = r.ticker or "—"
        ticker_cell = (
            f'<a href="../research/{escape(ticker)}/" class="ticker-link">{escape(ticker)}</a>'
            if r.ticker
            else "—"
        )
        if r.incomplete:
            ticker_cell += (
                '<span class="pf-flag" title="window start could not be fully reconstructed '
                '— row is approximate">⚠</span>'
            )
        policy_td = _money_cell(r.alpha_vs_policy, colored=True) if show_policy else ""
        rows.append(
            "<tr>"
            f"<td>{ticker_cell}</td>"
            f"<td>{escape(r.name or '—')}</td>"
            f"{_money_cell(r.value_at_end)}"
            f"{_money_cell(r.actual_pl, colored=True)}"
            f"{_money_cell(r.spy_counterfactual_pl)}"
            f"{_money_cell(r.alpha, colored=True)}"
            f"{_money_cell(r.alpha_vs_qqq, colored=True)}"
            f"{policy_td}"
            "</tr>"
        )
    policy_total = _money_cell(pa.total_alpha_vs_policy, colored=True) if show_policy else ""
    totals = (
        '<tr class="pf-total"><td>Total</td><td></td><td class="num"></td>'
        f"{_money_cell(pa.total_actual_pl, colored=True)}"
        f"{_money_cell(pa.total_spy_pl)}"
        f"{_money_cell(pa.total_alpha, colored=True)}"
        f"{_money_cell(pa.total_alpha_vs_qqq, colored=True)}"
        f"{policy_total}</tr>"
    )
    return (
        f"{head}"
        '<table class="alpha-table"><thead><tr>'
        "<th>Ticker</th><th>Name</th>"
        '<th class="num">Value</th><th class="num">P&amp;L</th>'
        f'<th class="num">SPY P&amp;L</th><th class="num">{_ALPHA} vs SPY</th>'
        f'<th class="num">{_ALPHA} vs QQQ</th>{policy_th}'
        "</tr></thead><tbody>"
        f"{''.join(rows)}"
        f"</tbody><tfoot>{totals}</tfoot></table></section>"
    )


def _kpi_card(label: str, value: str, *, sub: str = "", tone: str = "") -> str:
    sub_html = f'<div class="kpi-sub">{escape(sub)}</div>' if sub else ""
    cls = f" {tone}" if tone else ""
    return (
        f'<div class="kpi-card{cls}"><div class="kpi-label">{escape(label)}</div>'
        f'<div class="kpi-value">{value}</div>{sub_html}</div>'
    )


def _tone(v: float | None) -> str:
    """kpi-card / table-cell modifier: green when favorable, red when not."""
    if v is None:
        return ""
    return "pos" if v >= 0 else "neg"


def _money_cell(v: float | None, *, colored: bool = False) -> str:
    if v is None:
        return '<td class="num muted">—</td>'
    cls = "num" + (f" {_tone(v)}" if colored else "")
    return f'<td class="{cls}">{_money(v)}</td>'


def _pct(v: float | None, *, signed: bool = False, decimals: int = 1) -> str:
    """A value already in PERCENT units (the tracker's ``*_pct`` fields)."""
    if v is None:
        return "—"
    return f"{v:+.{decimals}f}%" if signed else f"{v:.{decimals}f}%"


def _pp(v: float | None) -> str:
    """A spread of two percent values, in signed percentage points."""
    return "—" if v is None else f"{v:+.1f}pp"


def _pct_frac(v: float | None, decimals: int = 1) -> str:
    """A FRACTION (0.18 = 18%) rendered as percent — the tracker's volatility /
    tracking-error / risk-free fields, unlike its ``*_pct`` fields."""
    return "—" if v is None else f"{v * 100.0:.{decimals}f}%"


def _ratio(v: float | None, decimals: int = 2) -> str:
    return "—" if v is None else f"{v:.{decimals}f}"


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
