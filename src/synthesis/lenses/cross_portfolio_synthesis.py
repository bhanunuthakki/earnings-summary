"""cross_portfolio_synthesis lens — weekly cross-ticker synthesis.

Finds patterns no single-ticker brief can see: convergence clusters,
capital-allocation reads, the week's most-look name.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from ._shared import (
    Lens,
    LensContext,
    read_holdings_json,
    sha8,
)

_PROMPT_CROSS_PORTFOLIO = """You are writing the weekly cross-portfolio synthesis memo. The analyst
holds 11 portfolio names + tracks 63 watchlist names. Your job: find
cross-ticker patterns no per-ticker brief can see.

**Portfolio holdings + latest data:**
{portfolio_summary}

**Recent insider activity (cross-ticker, last 30d):**
{insider_summary}

**Predictions outcomes (cross-ticker, last 90d):**
{predictions_summary}

Produce a 500-700 word memo with these sections:

## This week's most-look name
ONE holding that warrants the deepest look this week. Be specific about
WHY — what data point moved, what hypothesis was tested, what action
might be warranted. NOT the worst-performing or best-performing — the
one where the analytical picture changed most.

## Thematic convergence clusters
Identify 1-3 clusters of holdings exposed to the same thesis driver. For
each cluster: name the holdings, name the shared driver, and name the
joint downside if the driver fails. Examples: "GOOG / META / AMZN — capex
absorption" or "JPM / HDB / IBN — credit cycle exposure."

## Capital allocation suggestions
Across the portfolio, what 1-3 SIZE adjustments would the data suggest?
"ADD 0.5% to META — bear-case failure mode #2 was refuted this quarter" or
"TRIM 1% from NVDA — insider cluster + DCF +25% over fair." Be specific.

## What I'd want to spend more time on
ONE specific analytical question worth a deeper investigation this week.

Voice: portfolio manager writing to themselves — terse, opinion-bearing,
linking specific data points to capital allocation. No hedging language
without a specific reason.
"""


def _live_position_sizing(tickers: list[str]) -> dict[str, str]:
    """Per-ticker live position sizing from the companion tracker, keyed by upper
    ticker → "X% of portfolio · $Y · held in <treatments>". Empty dict when the
    tracker is unreachable, so the synthesis stays grounded when it's up and
    degrades silently when it isn't (lens runs from cron, tracker may be down)."""
    try:
        from integrations.portfolio_tracker_client import fetch_live_portfolio

        live = fetch_live_portfolio()
    except Exception:  # pragma: no cover - any import/transport failure → skip
        return {}
    if not live.available:
        return {}
    out: dict[str, str] = {}
    for p in live.positions:
        if not p.ticker:
            continue
        treatments = sorted({lot.tax_treatment for lot in p.accounts})
        pct = f"{p.percent_of_portfolio:.1f}%" if p.percent_of_portfolio is not None else "?"
        mv = f"${p.market_value:,.0f}" if p.market_value is not None else "?"
        out[p.ticker.upper()] = (
            f"{pct} of portfolio · {mv} · held in {', '.join(treatments) or 'unknown'}"
        )
    return out


def _ctx_cross_portfolio(ticker: str | None, repo_root: Path) -> LensContext | None:
    # ticker is None for portfolio scope; load all portfolio holdings
    db = repo_root / "data" / "portfolio.db"
    if not db.exists():
        return None
    conn = sqlite3.connect(str(db))
    try:
        conn.row_factory = sqlite3.Row
        tickers = [
            r[0]
            for r in conn.execute(
                "SELECT ticker FROM tracked_companies WHERE archived_at IS NULL AND list_type = 'portfolio' ORDER BY ticker"
            )
        ]
        # Per-ticker snapshot: thesis, dcf, latest bear case head, recent insider count
        live_by_ticker = _live_position_sizing(tickers)
        port_lines: list[str] = []
        for t in tickers:
            dcf = conn.execute(
                "SELECT npv_per_share, live_price, over_under_pct FROM dcf_runs "
                "WHERE ticker = ? AND (segment_name IS NULL OR segment_name = '') "
                "ORDER BY valuation_date DESC LIMIT 1",
                (t,),
            ).fetchone()
            insiders = conn.execute(
                "SELECT COUNT(*) FROM insider_transactions "
                "WHERE ticker = ? AND transaction_date >= date('now', '-30 days') "
                "AND transaction_type IN ('open_market_buy','open_market_sell') "
                "AND is_10b5_1 = 0",
                (t,),
            ).fetchone()
            bear = conn.execute(
                "SELECT content_md FROM llm_artifacts "
                "WHERE ticker = ? AND purpose = 'bear_case' AND superseded_by_id IS NULL "
                "ORDER BY generated_at DESC LIMIT 1",
                (t,),
            ).fetchone()
            h = read_holdings_json(t, repo_root)
            thesis = str(h.get("thesis") or "")[:200]
            ou_str = f"{float(dcf[2]) * 100:+.1f}%" if dcf and dcf[2] is not None else "-"
            bear_head = (
                str(bear[0] or "")[:300].replace("\n", " ") if bear else "(no bear case cached)"
            )
            live = live_by_ticker.get(t.upper())
            live_line = f"- Live position: {live}\n" if live else ""
            port_lines.append(
                f"### {t}\n"
                f"- Thesis: {thesis}\n"
                f"{live_line}"
                f"- DCF over/under: {ou_str}\n"
                f"- Discretionary insider trades last 30d: {int(insiders[0]) if insiders else 0}\n"
                f"- Latest bear case head: {bear_head}\n"
            )
        port_summary = "\n".join(port_lines) if port_lines else "(no portfolio holdings)"
        insider_rows = conn.execute(
            """
            SELECT it.ticker, it.transaction_date, it.insider_name, it.insider_title,
                   it.transaction_type, it.transaction_value
            FROM insider_transactions it
            INNER JOIN tracked_companies tc ON tc.ticker = it.ticker
            WHERE tc.list_type = 'portfolio' AND tc.archived_at IS NULL
              AND it.transaction_date >= date('now', '-30 days')
              AND it.transaction_type IN ('open_market_buy','open_market_sell')
              AND it.is_10b5_1 = 0
            ORDER BY it.transaction_value DESC NULLS LAST
            LIMIT 30
            """
        ).fetchall()
        insider_summary = (
            "\n".join(
                f"- {r[0]} · {str(r[1])[:10]} · {r[2]} ({r[3] or '?'}) · "
                f"{r[4].replace('_', ' ')} · ${float(r[5] or 0) / 1e6:.1f}M"
                for r in insider_rows
            )
            or "(no discretionary insider activity)"
        )
        if (
            conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='predictions'"
            ).fetchone()
            is not None
        ):
            pred_rows = conn.execute(
                """
                SELECT p.ticker, p.source_kind, p.outcome, COUNT(*) FROM predictions p
                INNER JOIN tracked_companies tc ON tc.ticker = p.ticker
                WHERE tc.list_type = 'portfolio'
                  AND p.evaluated_at >= date('now', '-90 days')
                GROUP BY p.ticker, p.source_kind, p.outcome
                """
            ).fetchall()
            predictions_summary = (
                "\n".join(f"- {r[0]} · {r[1]} · {r[2]} x {r[3]}" for r in pred_rows)
                or "(no graded predictions in last 90d)"
            )
        else:
            predictions_summary = "(predictions table not yet populated)"
    finally:
        conn.close()

    return LensContext(
        ticker=None,
        template_kwargs={
            "portfolio_summary": port_summary,
            "insider_summary": insider_summary,
            "predictions_summary": predictions_summary,
        },
        cache_inputs=[
            "portfolio",
            sha8(port_summary),
            sha8(insider_summary),
            sha8(predictions_summary),
        ],
        source_doc_ids=[],
        parent_artifact_ids=[],
    )


LENS = Lens(
    name="cross_portfolio_synthesis",
    model="claude-opus-4-7",  # cross-portfolio benefits from Opus's wider sector knowledge
    scope="portfolio",
    prompt_template=_PROMPT_CROSS_PORTFOLIO,
    build_context=_ctx_cross_portfolio,
)
