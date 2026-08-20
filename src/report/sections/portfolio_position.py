"""Pre-§1 callout: the user's canonical position in this ticker right now.

Reads only an explicitly configured tracker API endpoint or immutable snapshot.
The report must not change meaning because a sibling checkout happens to exist.
An unavailable source is rendered as unavailable, not as a missing holding.

What the renderer shows when populated:

    Your position in {TICKER}
    Total: {qty} sh · cost ${total_cost} · value ${total_value} ({+/-pct}%)
    Per account: <table>
"""

from __future__ import annotations

import sqlite3
from datetime import date, timedelta
from pathlib import Path

from integrations.portfolio_position import resolve_configured_position
from report.models import (
    MissingReason,
    PortfolioPositionAccountRow,
    PortfolioPositionDecision,
    PortfolioPositionSection,
    PortfolioPositionTransaction,
    SectionStatus,
)
from report.render_clock import render_today


def _parse_snap_date(raw: object) -> date | None:
    """Parse a tracker snapshot_date (ISO string or date) into a date, or None."""
    if raw is None:
        return None
    if isinstance(raw, date):
        return raw
    try:
        return date.fromisoformat(str(raw)[:10])
    except ValueError:
        return None


def build(ticker: str, repo_root: Path) -> PortfolioPositionSection:
    """Build from an explicit canonical source, never sibling-checkout discovery."""
    del repo_root
    result = resolve_configured_position(ticker)
    provenance = result.provenance
    position_as_of = result.position_as_of or (provenance.snapshot_as_of if provenance else None)
    source_identity = provenance.source_identity if provenance else None
    source_account_coverage = provenance.account_coverage if provenance else None
    source_snapshot_account_coverage = provenance.snapshot_account_coverage if provenance else None
    source_included_account_ids = list(provenance.included_account_ids) if provenance else []
    source_excluded_account_ids = list(provenance.excluded_account_ids) if provenance else []
    source_lagging_account_ids = list(provenance.lagging_account_ids) if provenance else []
    source_is_stale = provenance.is_stale if provenance else None
    source_schema_version = provenance.schema_version if provenance else None
    source_is_partial = provenance.is_partial if provenance else None
    source_currency = provenance.currency if provenance else None
    source_warnings = (
        [
            {"code": warning.code, "message": warning.message, "scope": warning.scope}
            for warning in provenance.warnings
        ]
        if provenance
        else []
    )
    if result.state == "source_unavailable":
        return PortfolioPositionSection(
            status=SectionStatus.MISSING_DATA,
            held=False,
            missing=MissingReason(
                stage="portfolio_tracker",
                fix_command="configure PORTFOLIO_TRACKER_API_URL or PORTFOLIO_TRACKER_SNAPSHOT_PATH",
                detail=result.error_detail or "Portfolio tracker source is unavailable.",
            ),
            position_as_of=position_as_of,
            source_identity=source_identity,
            source_account_coverage=source_account_coverage,
            source_snapshot_account_coverage=source_snapshot_account_coverage,
            source_included_account_ids=source_included_account_ids,
            source_excluded_account_ids=source_excluded_account_ids,
            source_lagging_account_ids=source_lagging_account_ids,
            source_is_stale=source_is_stale,
            source_schema_version=source_schema_version,
            source_is_partial=source_is_partial,
            source_currency=source_currency,
            source_warnings=source_warnings,
            history_state=result.history_state,
            history_error=result.history_error,
            recent_transactions=[
                PortfolioPositionTransaction.model_validate(item.model_dump())
                for item in result.recent_transactions
            ],
            open_decisions=[
                PortfolioPositionDecision.model_validate(item.model_dump())
                for item in result.open_decisions
            ],
            closed_decisions=[
                PortfolioPositionDecision.model_validate(item.model_dump())
                for item in result.closed_decisions
            ],
        )
    if result.state == "not_held":
        return PortfolioPositionSection(
            status=(
                SectionStatus.PARTIAL
                if (
                    (provenance is not None and (provenance.is_partial or provenance.is_stale))
                    or result.history_state != "available"
                    or result.recent_transactions
                    or result.open_decisions
                    or result.closed_decisions
                )
                else SectionStatus.NOT_APPLICABLE
            ),
            held=False,
            position_as_of=position_as_of,
            source_identity=source_identity,
            source_account_coverage=source_account_coverage,
            source_snapshot_account_coverage=source_snapshot_account_coverage,
            source_included_account_ids=source_included_account_ids,
            source_excluded_account_ids=source_excluded_account_ids,
            source_lagging_account_ids=source_lagging_account_ids,
            source_is_stale=source_is_stale,
            source_schema_version=source_schema_version,
            source_is_partial=source_is_partial,
            source_currency=source_currency,
            source_warnings=source_warnings,
            history_state=result.history_state,
            history_error=result.history_error,
            recent_transactions=[
                PortfolioPositionTransaction.model_validate(item.model_dump())
                for item in result.recent_transactions
            ],
            open_decisions=[
                PortfolioPositionDecision.model_validate(item.model_dump())
                for item in result.open_decisions
            ],
            closed_decisions=[
                PortfolioPositionDecision.model_validate(item.model_dump())
                for item in result.closed_decisions
            ],
        )
    return PortfolioPositionSection(
        status=(
            SectionStatus.PARTIAL
            if provenance and (provenance.is_stale or provenance.is_partial)
            else SectionStatus.OK
        ),
        held=True,
        accounts=[
            PortfolioPositionAccountRow(
                account_name=account.account_name,
                quantity=account.quantity,
                cost_basis=account.cost_basis,
                cost_basis_source=account.cost_basis_source,
                market_value=account.market_value,
                unrealized_pnl=account.unrealized_pnl,
                unrealized_pct=account.unrealized_pct,
                snapshot_date=account.snapshot_date,
            )
            for account in result.accounts
        ],
        total_quantity=result.total_quantity,
        total_cost_basis=result.total_cost_basis,
        total_market_value=result.total_market_value,
        total_unrealized_pnl=result.total_unrealized_pnl,
        total_unrealized_pct=result.total_unrealized_pct,
        position_as_of=position_as_of,
        source_identity=source_identity,
        source_account_coverage=source_account_coverage,
        source_snapshot_account_coverage=source_snapshot_account_coverage,
        source_included_account_ids=source_included_account_ids,
        source_excluded_account_ids=source_excluded_account_ids,
        source_lagging_account_ids=source_lagging_account_ids,
        source_is_stale=source_is_stale,
        source_schema_version=source_schema_version,
        source_is_partial=source_is_partial,
        source_currency=source_currency,
        source_warnings=source_warnings,
        history_state=result.history_state,
        history_error=result.history_error,
        recent_transactions=[
            PortfolioPositionTransaction.model_validate(item.model_dump())
            for item in result.recent_transactions
        ],
        open_decisions=[
            PortfolioPositionDecision.model_validate(item.model_dump())
            for item in result.open_decisions
        ],
        closed_decisions=[
            PortfolioPositionDecision.model_validate(item.model_dump())
            for item in result.closed_decisions
        ],
    )


def _holding_accounts(conn: sqlite3.Connection, ticker: str) -> list[PortfolioPositionAccountRow]:
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
                   hs.snapshot_date,
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
               l.snapshot_date,
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
                snapshot_date=_parse_snap_date(r["snapshot_date"]),
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
    cutoff = (render_today() - timedelta(days=lookback_days)).isoformat()
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
            outcome_status=r["outcome_status"],
        )
        for r in rows
    ]


def _closed_decisions(
    conn: sqlite3.Connection, ticker: str, lookback_days: int = 730
) -> list[PortfolioPositionDecision]:
    """Track record: closed decisions on this ticker. outcome_status in
    ('validated', 'invalidated', 'partial'). Longer lookback than the
    open-decision query (~2y) so the user sees the full history of past
    calls when re-evaluating the name."""
    cutoff = (render_today() - timedelta(days=lookback_days)).isoformat()
    rows = conn.execute(
        """
        SELECT decision_date, action, confidence, thesis, linked_brief_path,
               outcome_status, outcome_date, outcome_notes
        FROM trade_decisions
        WHERE UPPER(ticker) = ?
          AND decision_date >= ?
          AND outcome_status IN ('validated', 'invalidated', 'partial')
        ORDER BY decision_date DESC
        LIMIT 10
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
            outcome_status=r["outcome_status"],
            outcome_date=_parse_iso_date(r["outcome_date"]) if r["outcome_date"] else None,
            outcome_notes=r["outcome_notes"],
        )
        for r in rows
    ]


def _parse_iso_date(s: str | None) -> date:
    if s is None:
        return render_today()
    return date.fromisoformat(s[:10])
