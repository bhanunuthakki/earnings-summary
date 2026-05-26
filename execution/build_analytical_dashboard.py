"""Build the analytical portfolio dashboard HTML.

Reads cross-ticker data from the unified data model and emits a single
self-contained HTML file showing:

  - Trigger ladder (every holding positioned on the SELL ↔ HOLD ↔ ADD rail)
  - Cross-ticker insider activity (last 90d, conviction-scored)
  - Predictions outcomes (SayDo + bear-case + risk-factor materialization)
  - Per-holding KPI strip (when populated)

Output: output/dashboard/<date>_portfolio_dashboard.html

Usage:
    python execution/build_analytical_dashboard.py
    python execution/build_analytical_dashboard.py --since-days 30
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import UTC, datetime
from html import escape
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from pipeline.analytical_dashboard import (  # noqa: E402
    AnalyticalDashboard,
    InsiderEventRow,
    LlmBudgetPanel,
    LlmBudgetRow,
    PortfolioLensRow,
    PredictionOutcomeRow,
    TriggerLadderRow,
    build_analytical_dashboard,
)

log = logging.getLogger("build_analytical_dashboard")


_TRIGGER_TONE: dict[str, str] = {
    "sell": "tone-sell",
    "trim": "tone-trim",
    "hold": "tone-hold",
    "initiate_candidate": "tone-init",
    "unknown": "tone-muted",
}


def render_html(dash: AnalyticalDashboard, *, generated_at: datetime) -> str:
    parts: list[str] = [
        _PAGE_HEAD.format(
            generated_at=escape(generated_at.isoformat(timespec="seconds"))
        ),
        # Top of page: portfolio-wide synthesis when cached
        _portfolio_synthesis_section(dash.portfolio_synthesis_md),
        _per_ticker_reread_section(dash.per_ticker_reread),
        _trigger_ladder_section(dash.trigger_ladder),
        _insider_events_section(dash.insider_events),
        _predictions_section(dash.prediction_outcomes),
        _llm_budget_section(dash.llm_budgets),
        _PAGE_FOOT,
    ]
    return "".join(parts)


def _llm_budget_section(panel: LlmBudgetPanel) -> str:
    """LLM Spend & Budget panel — per-purpose progress bars + MTD totals.

    Empty-state hint when the budget tables haven't been migrated yet so
    the dashboard works on older repos without a hard failure."""
    if not panel.rows:
        return (
            '<section class="panel"><h2>LLM spend & budget</h2>'
            '<p class="muted">No budget data. Run <code>python -m alembic upgrade head</code> '
            'to install migration 0052, then revisit.</p>'
            "</section>"
        )
    out: list[str] = [
        '<section class="panel"><h2>LLM spend & budget</h2>',
        f'<p class="sub">Per-purpose monthly caps · {escape(panel.month_label)} · '
        f'edit via <code>python execution/manage_llm_budget.py --set &lt;purpose&gt; --cap &lt;usd&gt;</code></p>',
        '<table class="budget-table"><thead><tr>',
        '<th>Purpose</th><th class="num">Spend</th><th class="num">Cap</th>',
        '<th>Burn</th><th class="num">Headroom</th><th>Block</th>',
        '</tr></thead><tbody>',
    ]
    for r in panel.rows:
        out.append(_budget_row_html(r))
    out.append('</tbody></table>')
    pct = (
        100.0 * panel.total_spend_mtd_usd / panel.projected_month_end_usd
        if panel.projected_month_end_usd > 0
        else 0.0
    )
    out.append(
        '<p class="budget-footer">'
        f'<strong>MTD total:</strong> ${panel.total_spend_mtd_usd:,.2f} · '
        f'<strong>Projected month-end:</strong> ${panel.projected_month_end_usd:,.2f} '
        f'<span class="muted">(MTD = {pct:.0f}% of projection)</span>'
        '</p></section>'
    )
    return "".join(out)


def _budget_row_html(r: LlmBudgetRow) -> str:
    """One progress-bar row. Colour bands:
      OVER (red)     — headroom_pct <= 0
      WARN (amber)   — headroom_pct < (1 - warn_threshold_pct)
      OK (green)     — otherwise
    """
    burn_pct = max(0.0, min(1.0, 1.0 - r.headroom_pct))
    if r.headroom_pct <= 0:
        bar_tone = "burn-over"
    elif r.headroom_pct < (1.0 - r.warn_threshold_pct):
        bar_tone = "burn-warn"
    else:
        bar_tone = "burn-ok"
    block_label = "HARD" if r.hard_block else "soft"
    block_class = "block-hard" if r.hard_block else "block-soft"
    bar_width_pct = int(burn_pct * 100)
    # Render >100% as a full bar with the "over" tone — visual cap, the
    # number column still shows the real headroom_pct so the over-spend
    # is auditable.
    return (
        f'<tr>'
        f'<td><code>{escape(r.purpose)}</code></td>'
        f'<td class="num">${r.current_spend_usd:,.2f}</td>'
        f'<td class="num">${r.monthly_cap_usd:,.2f}</td>'
        f'<td class="burn-cell"><div class="burn-bar">'
        f'<div class="burn-fill {bar_tone}" style="width: {min(100, bar_width_pct)}%"></div>'
        f'</div></td>'
        f'<td class="num">{r.headroom_pct * 100:+.0f}%</td>'
        f'<td class="{block_class}">{block_label}</td>'
        '</tr>'
    )


def _portfolio_synthesis_section(content_md: str | None) -> str:
    """The cross_portfolio_synthesis lens output as the lead panel."""
    if not content_md:
        return (
            '<section class="panel"><h2>Portfolio synthesis</h2>'
            '<p class="muted">No cross-portfolio synthesis cached. Run:</p>'
            '<pre class="cli-hint">python execution/run_lens.py --lens cross_portfolio_synthesis</pre>'
            "</section>"
        )
    # Render markdown minimally — preserve headers + bold + bullets
    rendered = _light_markdown_to_html(content_md)
    return (
        '<section class="panel synthesis-panel">'
        '<h2>Portfolio synthesis</h2>'
        '<p class="sub">Cross-ticker patterns · this week\'s deeper-look · capital-allocation suggestions.</p>'
        f'<div class="synthesis-body">{rendered}</div>'
        "</section>"
    )


def _per_ticker_reread_section(rows: list[PortfolioLensRow]) -> str:
    """Compact 5-min reread cards for every holding that has one cached."""
    if not rows:
        return (
            '<section class="panel"><h2>Per-holding 5-min rereads</h2>'
            '<p class="muted">No per-holding rereads cached. Generate via <code>python execution/run_lens.py --tickers AMZN,GOOG,META --lens five_min_reread</code> (or --all for every lens).</p>'
            "</section>"
        )
    out: list[str] = [
        '<section class="panel"><h2>Per-holding 5-min rereads</h2>',
        '<p class="sub">Decision-oriented per-ticker artifact. Click a ticker for the full memo.</p>',
        '<div class="reread-grid">',
    ]
    for r in rows:
        rendered = _light_markdown_to_html(r.content_md[:8000])
        out.append(
            f'<details class="reread-card"><summary>'
            f'<a href="../research/{escape(r.ticker)}/" class="ticker-link">{escape(r.ticker)}</a>'
            f'<span class="reread-stamp">{escape(r.generated_at[:10])}</span>'
            f"</summary>"
            f'<div class="reread-body">{rendered}</div>'
            "</details>"
        )
    out.append("</div></section>")
    return "".join(out)


def _light_markdown_to_html(md: str) -> str:
    """Cheap markdown subset: ##/### headers, **bold**, bullets, paragraphs.
    Avoids a full markdown library so the dashboard stays dependency-free.
    Handles enough to render lens outputs faithfully."""
    import re

    lines = md.splitlines()
    out: list[str] = []
    in_list = False
    for raw in lines:
        line = raw.rstrip()
        if not line.strip():
            if in_list:
                out.append("</ul>")
                in_list = False
            out.append("")
            continue
        if line.startswith("### "):
            if in_list:
                out.append("</ul>")
                in_list = False
            out.append(f"<h4>{escape(line[4:])}</h4>")
            continue
        if line.startswith("## "):
            if in_list:
                out.append("</ul>")
                in_list = False
            out.append(f"<h3>{escape(line[3:])}</h3>")
            continue
        if line.startswith("# "):
            if in_list:
                out.append("</ul>")
                in_list = False
            out.append(f"<h2>{escape(line[2:])}</h2>")
            continue
        # Horizontal rule
        if line.strip() in {"---", "***"}:
            if in_list:
                out.append("</ul>")
                in_list = False
            out.append("<hr>")
            continue
        # Bullet
        bullet_match = re.match(r"^[-*]\s+(.+)", line)
        if bullet_match:
            if not in_list:
                out.append("<ul>")
                in_list = True
            content = _inline_md(bullet_match.group(1))
            out.append(f"<li>{content}</li>")
            continue
        # Paragraph
        if in_list:
            out.append("</ul>")
            in_list = False
        out.append(f"<p>{_inline_md(line)}</p>")
    if in_list:
        out.append("</ul>")
    return "\n".join(out)


def _inline_md(text: str) -> str:
    import re

    s = escape(text)
    # Bold then italic — order matters
    s = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", s)
    s = re.sub(r"(?<!\*)\*([^*]+)\*(?!\*)", r"<em>\1</em>", s)
    s = re.sub(r"`([^`]+)`", r"<code>\1</code>", s)
    return s


def _trigger_ladder_section(rows: list[TriggerLadderRow]) -> str:
    if not rows:
        return (
            '<section class="panel"><h2>Trigger ladder</h2>'
            '<p class="muted">No DCF runs yet. Run <code>python execution/refresh_dcf.py --all-named</code>.</p></section>'
        )

    out: list[str] = [
        '<section class="panel"><h2>Trigger ladder</h2>',
        '<p class="sub">Every holding positioned by DCF over/under vs MoS bar. Sorted by absolute deviation.</p>',
        '<table class="trigger-table"><thead><tr>',
        "<th>Ticker</th><th>List</th><th>Verdict</th>",
        '<th class="num">Live</th><th class="num">Fair value</th>',
        '<th class="num">Over/under</th><th class="num">MoS bar</th>',
        "<th>Trigger</th></tr></thead><tbody>",
    ]
    for r in rows:
        tone = _TRIGGER_TONE.get(r.trigger_status or "unknown", "tone-muted")
        ou = f"{(r.over_under_pct or 0) * 100:+.1f}%" if r.over_under_pct is not None else "—"
        mos = f"{(r.mos_bar or 0) * 100:.0f}%" if r.mos_bar is not None else "—"
        out.append(
            f'<tr class="{tone}">'
            f'<td><a href="../research/{escape(r.ticker)}/" class="ticker-link">{escape(r.ticker)}</a></td>'
            f"<td>{escape(r.list_type)}</td>"
            f"<td>{escape(r.verdict or '—')}</td>"
            f'<td class="num">${r.live_price:.0f}</td>' if r.live_price is not None else '<td class="num muted">—</td>'
        )
        out.append(
            f'<td class="num">${r.dcf_fair_value:.0f}</td>' if r.dcf_fair_value is not None else '<td class="num muted">—</td>'
        )
        out.append(
            f'<td class="num">{ou}</td><td class="num">{mos}</td>'
            f'<td class="trigger-cell">{escape((r.trigger_status or "unknown").replace("_", " "))}</td>'
            "</tr>"
        )
    out.append("</tbody></table></section>")
    return "".join(out)


def _insider_events_section(rows: list[InsiderEventRow]) -> str:
    if not rows:
        return (
            '<section class="panel"><h2>Cross-ticker insider activity (last 90d)</h2>'
            '<p class="muted">No insider data. Run <code>python execution/backfill_insider_transactions.py --since 2024-01-01</code>.</p></section>'
        )
    out: list[str] = [
        '<section class="panel"><h2>Cross-ticker insider activity (last 90d)</h2>',
        '<p class="sub">Discretionary trades only · ranked by conviction signal · 10b5-1 sells filtered out.</p>',
        '<table class="insider-table"><thead><tr>',
        "<th>Date</th><th>Ticker</th><th>Insider</th><th>Role</th><th>Action</th>",
        '<th class="num">Shares</th><th class="num">Value</th><th class="num">Signal</th><th>Why</th>',
        "</tr></thead><tbody>",
    ]
    for r in rows:
        tone = "tx-buy" if "buy" in r.transaction_type else "tx-sell" if "sell" in r.transaction_type else ""
        signal_pct = int(r.signal_strength * 100)
        strength_tone = "signal-strong" if r.signal_strength >= 0.6 else "signal-medium" if r.signal_strength >= 0.3 else "signal-weak"
        out.append(
            f'<tr class="{tone}">'
            f"<td>{escape(r.transaction_date)}</td>"
            f'<td><a href="../research/{escape(r.ticker)}/" class="ticker-link">{escape(r.ticker)}</a></td>'
            f"<td>{escape(r.insider_name)}</td>"
            f"<td>{escape(r.insider_role or '?')}</td>"
            f"<td>{escape(r.transaction_type.replace('_', ' '))}</td>"
            f'<td class="num">{r.shares:,.0f}</td>'
        )
        if r.transaction_value is not None:
            v = r.transaction_value
            v_str = f"${v / 1e9:.1f}B" if v >= 1e9 else f"${v / 1e6:.1f}M" if v >= 1e6 else f"${v / 1e3:.0f}K"
            out.append(f'<td class="num">{v_str}</td>')
        else:
            out.append('<td class="num muted">—</td>')
        out.append(
            f'<td class="num {strength_tone}">{signal_pct}</td>'
            f"<td>{escape(r.rationale)}</td>"
            "</tr>"
        )
    out.append("</tbody></table></section>")
    return "".join(out)


def _predictions_section(rows: list[PredictionOutcomeRow]) -> str:
    if not rows:
        return ""
    # Aggregate by source_kind into a per-ticker grid
    from collections import defaultdict

    by_ticker: dict[str, dict[str, dict[str, int]]] = defaultdict(
        lambda: defaultdict(dict)
    )
    for r in rows:
        by_ticker[r.ticker][r.source_kind][r.outcome] = r.count

    out: list[str] = [
        '<section class="panel"><h2>Predictions outcomes (cross-ticker)</h2>',
        '<p class="sub">SayDo, LLM bear-case, risk-factor materialization tallied across all forward-looking sources.</p>',
        '<table class="predictions-table"><thead><tr>',
        "<th>Ticker</th><th>Source</th>",
        '<th class="num">Pending</th><th class="num">Met</th>',
        '<th class="num">Mixed</th><th class="num">Missed</th>',
        '<th class="num">Hit rate</th></tr></thead><tbody>',
    ]
    for ticker in sorted(by_ticker.keys()):
        for source_kind in sorted(by_ticker[ticker].keys()):
            outcomes = by_ticker[ticker][source_kind]
            graded = (outcomes.get("met", 0) + outcomes.get("missed", 0) + outcomes.get("mixed", 0))
            hit_rate = (
                f"{100 * outcomes.get('met', 0) / graded:.0f}%" if graded > 0 else "—"
            )
            out.append(
                "<tr>"
                f'<td><a href="../research/{escape(ticker)}/" class="ticker-link">{escape(ticker)}</a></td>'
                f"<td>{escape(source_kind)}</td>"
                f'<td class="num muted">{outcomes.get("pending", 0)}</td>'
                f'<td class="num pos">{outcomes.get("met", 0)}</td>'
                f'<td class="num">{outcomes.get("mixed", 0)}</td>'
                f'<td class="num neg">{outcomes.get("missed", 0)}</td>'
                f'<td class="num">{hit_rate}</td>'
                "</tr>"
            )
    out.append("</tbody></table></section>")
    return "".join(out)


_PAGE_HEAD = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Portfolio · analytical dashboard</title>
<style>
  body {{ margin: 0; padding: 24px; font-family: 'Inter', -apple-system, sans-serif; background: #0c0d10; color: #e5e5e2; line-height: 1.5; font-size: 14px; }}
  h1 {{ font-size: 24px; margin: 0 0 8px; font-weight: 600; }}
  h2 {{ font-size: 18px; margin: 0 0 6px; font-weight: 600; }}
  .stamp {{ color: #888; font-size: 12px; font-family: 'JetBrains Mono', monospace; margin-bottom: 24px; }}
  .panel {{ margin-bottom: 32px; background: #16171a; border: 1px solid #2a2c30; border-radius: 8px; padding: 18px 20px; }}
  .panel .sub {{ color: #999; font-size: 12px; margin: 0 0 16px; }}
  .muted {{ color: #888; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
  th {{ text-align: left; padding: 8px 10px; border-bottom: 2px solid #2a2c30; font-family: 'JetBrains Mono', monospace; font-size: 11px; text-transform: uppercase; letter-spacing: 0.5px; color: #888; font-weight: 600; }}
  td {{ padding: 8px 10px; border-bottom: 1px solid #1f2125; vertical-align: top; }}
  td.num {{ text-align: right; font-variant-numeric: tabular-nums; }}
  td.muted {{ color: #666; }}
  td.pos {{ color: #4ade80; }}
  td.neg {{ color: #f87171; }}
  .ticker-link {{ color: #f5f5f0; text-decoration: none; font-weight: 600; }}
  .ticker-link:hover {{ color: #aaa; }}
  tr.tone-sell {{ background: rgba(248, 113, 113, 0.06); }}
  tr.tone-trim {{ background: rgba(251, 191, 36, 0.04); }}
  tr.tone-init {{ background: rgba(74, 222, 128, 0.06); }}
  tr.tx-buy {{ background: rgba(74, 222, 128, 0.04); }}
  tr.tx-sell {{ background: rgba(248, 113, 113, 0.02); }}
  td.trigger-cell {{ font-family: 'JetBrains Mono', monospace; font-size: 11px; text-transform: uppercase; }}
  tr.tone-sell .trigger-cell {{ color: #f87171; }}
  tr.tone-trim .trigger-cell {{ color: #fbbf24; }}
  tr.tone-init .trigger-cell {{ color: #4ade80; }}
  td.signal-strong {{ color: #4ade80; font-weight: 600; }}
  td.signal-medium {{ color: #fbbf24; }}
  td.signal-weak {{ color: #888; }}
  /* Synthesis panel */
  .synthesis-panel {{ border-left: 3px solid #4ade80; }}
  .synthesis-body {{ font-size: 14px; line-height: 1.65; }}
  .synthesis-body h2, .synthesis-body h3, .synthesis-body h4 {{ color: #f5f5f0; margin-top: 1.2em; margin-bottom: 6px; }}
  .synthesis-body h2 {{ font-size: 18px; }}
  .synthesis-body h3 {{ font-size: 15px; }}
  .synthesis-body h4 {{ font-size: 13px; color: #4ade80; }}
  .synthesis-body strong {{ color: #f5f5f0; }}
  .synthesis-body code {{ background: #1f2125; padding: 1px 5px; border-radius: 3px; font-family: 'JetBrains Mono', monospace; font-size: 12px; }}
  .synthesis-body ul {{ padding-left: 22px; }}
  .synthesis-body li {{ margin-bottom: 4px; }}
  .synthesis-body hr {{ border: none; border-top: 1px solid #2a2c30; margin: 16px 0; }}
  /* Reread grid */
  .reread-grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(380px, 1fr)); gap: 12px; margin-top: 8px; }}
  .reread-card {{ background: #16171a; border: 1px solid #2a2c30; border-radius: 6px; padding: 12px 14px; }}
  .reread-card summary {{ cursor: pointer; list-style: none; display: flex; justify-content: space-between; align-items: baseline; font-size: 16px; font-weight: 600; }}
  .reread-card summary::-webkit-details-marker {{ display: none; }}
  .reread-card summary::before {{ content: '▸ '; color: #888; font-family: 'JetBrains Mono', monospace; }}
  .reread-card[open] summary::before {{ content: '▾ '; }}
  .reread-stamp {{ color: #888; font-size: 11px; font-family: 'JetBrains Mono', monospace; font-weight: 400; }}
  .reread-body {{ font-size: 13px; line-height: 1.55; margin-top: 10px; }}
  .reread-body h2, .reread-body h3, .reread-body h4 {{ color: #f5f5f0; margin: 10px 0 4px; }}
  .reread-body h2 {{ font-size: 14px; color: #4ade80; }}
  .reread-body h3 {{ font-size: 13px; }}
  .reread-body strong {{ color: #f5f5f0; }}
  .reread-body ul {{ padding-left: 18px; }}
  .reread-body hr {{ border: none; border-top: 1px solid #2a2c30; margin: 10px 0; }}
  .cli-hint {{ font-family: 'JetBrains Mono', monospace; font-size: 12px; padding: 10px 12px; background: #1f2125; border-radius: 4px; color: #4ade80; overflow-x: auto; margin: 6px 0 0; }}
  /* LLM budget panel */
  .budget-table td code {{ font-family: 'JetBrains Mono', monospace; font-size: 12px; color: #f5f5f0; background: transparent; padding: 0; }}
  .burn-cell {{ width: 200px; padding: 6px 10px; }}
  .burn-bar {{ width: 100%; height: 8px; background: #1f2125; border-radius: 4px; overflow: hidden; }}
  .burn-fill {{ height: 100%; transition: width 0.2s; }}
  .burn-ok {{ background: #4ade80; }}
  .burn-warn {{ background: #fbbf24; }}
  .burn-over {{ background: #f87171; }}
  .block-hard {{ font-family: 'JetBrains Mono', monospace; font-size: 11px; color: #f87171; font-weight: 600; }}
  .block-soft {{ font-family: 'JetBrains Mono', monospace; font-size: 11px; color: #888; }}
  .budget-footer {{ margin-top: 12px; font-size: 13px; color: #ccc; }}
  .budget-footer strong {{ color: #f5f5f0; }}
</style>
</head>
<body>
<h1>Portfolio · analytical dashboard</h1>
<div class="stamp">generated {generated_at}</div>
"""

_PAGE_FOOT = "</body></html>"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo-root", type=Path, default=PROJECT_ROOT, help="Repo root with data/portfolio.db."
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Output HTML path. Default: output/dashboard/<date>_portfolio_dashboard.html.",
    )
    parser.add_argument("--insider-window-days", type=int, default=90)
    parser.add_argument("--insider-top-n", type=int, default=25)
    args = parser.parse_args()

    db_path = args.repo_root / "data" / "portfolio.db"
    dash = build_analytical_dashboard(
        db_path,
        insider_window_days=args.insider_window_days,
        insider_top_n=args.insider_top_n,
    )
    html = render_html(dash, generated_at=datetime.now(UTC))

    if args.out is None:
        out_dir = args.repo_root / "output" / "dashboard"
        out_dir.mkdir(parents=True, exist_ok=True)
        args.out = out_dir / f"{datetime.now(UTC).strftime('%Y-%m-%d')}_portfolio_dashboard.html"

    args.out.write_text(html, encoding="utf-8")
    print(f"Wrote {args.out}")
    print(
        f"  trigger_ladder={len(dash.trigger_ladder)} insider_events={len(dash.insider_events)} "
        f"prediction_outcomes={len(dash.prediction_outcomes)} "
        f"llm_budgets={len(dash.llm_budgets.rows)}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
