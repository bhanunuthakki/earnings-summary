"""Pre-§1 callout: the user's position in this ticker right now.

Reads from the companion `portfolio-tracker` project's SQLite (sibling
directory). When that project isn't installed, the section returns
status=NOT_APPLICABLE and the renderer skips it. When the user doesn't
hold the ticker AND has no decisions logged on it, same thing —
no point in pasting an empty "you don't own this" block into every
research brief.

What the renderer shows when populated:

    Your position in {TICKER}
    Total: {qty} sh · cost ${total_cost} · value ${total_value} ({+/-pct}%)
    Per account: <table>
    Recent activity: <last 5 transactions>
    Your thesis: <last 1-2 trade decisions>
"""

from __future__ import annotations

import sqlite3
from datetime import date, timedelta
from pathlib import Path

from report.models import (
    PortfolioPositionAccountRow,
    PortfolioPositionDecision,
    PortfolioPositionSection,
    PortfolioPositionTransaction,
    SectionStatus,
)
from report.sections._common import open_portfolio_tracker_db


def build(ticker: str, repo_root: Path) -> PortfolioPositionSection:
    """Return the position section for `ticker`. NOT_APPLICABLE when
    portfolio-tracker isn't installed or the user has zero exposure to
    the name."""
    conn = open_portfolio_tracker_db(repo_root)
    if conn is None:
        return PortfolioPositionSection(status=SectionStatus.NOT_APPLICABLE)

    try:
        accounts = _holding_accounts(conn, ticker)
        transactions = _recent_transactions(conn, ticker)
        decisions = _open_decisions(conn, ticker)
    finally:
        conn.close()

    if not accounts and not transactions and not decisions:
        # No exposure whatsoever — hide the section.
        return PortfolioPositionSection(status=SectionStatus.NOT_APPLICABLE)

    total_qty = sum(a.quantity for a in accounts)
    cost_known = [a.cost_basis for a in accounts if a.cost_basis is not None]
    total_cost = sum(cost_known) if cost_known else None
    value_known = [a.market_value for a in accounts if a.market_value is not None]
    total_value = sum(value_known) if value_known else None
    total_pnl: float | None = None
    total_pct: float | None = None
    if total_cost is not None and total_value is not None:
        total_pnl = total_value - total_cost
        total_pct = (total_pnl / total_cost) if total_cost > 0 else None

    return PortfolioPositionSection(
        status=SectionStatus.OK,
        held=bool(accounts),
        accounts=accounts,
        total_quantity=total_qty,
        total_cost_basis=total_cost,
        total_market_value=total_value,
        total_unrealized_pnl=total_pnl,
        total_unrealized_pct=total_pct,
        recent_transactions=transactions,
        open_decisions=decisions,
    )


def _holding_accounts(
    conn: sqlite3.Connection, ticker: str
) -> list[PortfolioPositionAccountRow]:
    """Latest snapshot per (account, security) for the ticker, with the
    cost-basis-override merge logic (override wins when present)."""
    ticker_norm = ticker.upper().strip()
    rows = conn.execute(
        """
        WITH latest AS (
            SELECT hs.account_id,
                   hs.security_id,
                   hs.quantity,
                   hs.institution_value,
                   hs.cost_basis,
                   ROW_NUMBER() OVER (
                       PARTITION BY hs.account_id, hs.security_id
                       ORDER BY hs.snapshot_date DESC
                   ) AS rn
            FROM holdings_snapshots hs
            JOIN securities s ON s.security_id = hs.security_id
            WHERE UPPER(s.ticker) = ?
        )
        SELECT l.account_id,
               a.name AS account_name,
               l.quantity,
               l.institution_value,
               l.cost_basis,
               cbo.total_cost_basis AS override_cost,
               cbo.source AS override_source
        FROM latest l
        JOIN accounts a ON a.account_id = l.account_id
        LEFT JOIN cost_basis_overrides cbo
               ON cbo.account_id = l.account_id
              AND cbo.security_id = l.security_id
        WHERE l.rn = 1
          AND l.quantity > 0
        """,
        (ticker_norm,),
    ).fetchall()
    out: list[PortfolioPositionAccountRow] = []
    for r in rows:
        # Override wins when present, including over $0 broker values.
        effective_cost: float | None
        cost_source: str | None
        if r["override_cost"] is not None:
            effective_cost = float(r["override_cost"])
            cost_source = r["override_source"]
        elif r["cost_basis"] is not None:
            effective_cost = float(r["cost_basis"])
            cost_source = None
        else:
            effective_cost = None
            cost_source = None
        qty = float(r["quantity"] or 0)
        mv = float(r["institution_value"]) if r["institution_value"] is not None else None
        pnl: float | None = None
        pct: float | None = None
        if mv is not None and effective_cost is not None:
            pnl = mv - effective_cost
            pct = (pnl / effective_cost) if effective_cost > 0 else None
        out.append(
            PortfolioPositionAccountRow(
                account_name=r["account_name"],
                quantity=qty,
                cost_basis=effective_cost,
                cost_basis_source=cost_source,
                market_value=mv,
                unrealized_pnl=pnl,
                unrealized_pct=pct,
            )
        )
    return out


def _recent_transactions(
    conn: sqlite3.Connection, ticker: str, limit: int = 5
) -> list[PortfolioPositionTransaction]:
    rows = conn.execute(
        """
        SELECT t.date,
               a.name AS account_name,
               t.type,
               t.quantity,
               t.amount
        FROM investment_transactions t
        JOIN accounts a ON a.account_id = t.account_id
        JOIN securities s ON s.security_id = t.security_id
        WHERE UPPER(s.ticker) = ?
          AND t.type IN ('buy', 'sell')
        ORDER BY t.date DESC, t.plaid_investment_transaction_id DESC
        LIMIT ?
        """,
        (ticker.upper().strip(), limit),
    ).fetchall()
    return [
        PortfolioPositionTransaction(
            date=_parse_iso_date(r["date"]),
            account_name=r["account_name"],
            type=r["type"],
            quantity=float(r["quantity"] or 0),
            amount=float(r["amount"] or 0),
        )
        for r in rows
    ]


def _open_decisions(
    conn: sqlite3.Connection, ticker: str, lookback_days: int = 365
) -> list[PortfolioPositionDecision]:
    """Trade decisions on this ticker from the last `lookback_days` days
    where outcome_status is NULL (still open) or 'open'."""
    cutoff = (date.today() - timedelta(days=lookback_days)).isoformat()
    rows = conn.execute(
        """
        SELECT decision_date, action, confidence, thesis, linked_brief_path,
               outcome_status
        FROM trade_decisions
        WHERE UPPER(ticker) = ?
          AND decision_date >= ?
          AND (outcome_status IS NULL OR outcome_status = 'open')
        ORDER BY decision_date DESC
        LIMIT 5
        """,
        (ticker.upper().strip(), cutoff),
    ).fetchall()
    return [
        PortfolioPositionDecision(
            decision_date=_parse_iso_date(r["decision_date"]),
            action=r["action"],
            confidence=r["confidence"],
            thesis=r["thesis"],
            linked_brief_path=r["linked_brief_path"],
        )
        for r in rows
    ]


def _parse_iso_date(s: str | None) -> date:
    if s is None:
        return date.today()
    return date.fromisoformat(s[:10])
