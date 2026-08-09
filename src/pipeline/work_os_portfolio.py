"""Typed, portfolio-only hydration model for the Work OS shell.

The research database defines which companies belong to the portfolio UI. The
Portfolio Tracker enriches those names with current weights and market values
when it is available; a tracker failure never removes research companies or
leaks transport errors into the page.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict

from integrations.portfolio_tracker_client import LivePortfolio, LivePosition
from pipeline.research_cockpit import CockpitRow


class WorkOsPortfolioCompany(BaseModel):
    """One governed portfolio company rendered by Cockpit and Company Desk."""

    model_config = ConfigDict(frozen=True)

    ticker: str
    name: str
    current_weight_pct: float | None = None
    market_value: float | None = None
    cost_basis: float | None = None
    unrealized_pnl: float | None = None
    price: float | None = None
    day_move_pct: float | None = None
    fair_value: float | None = None
    fair_value_gap_pct: float | None = None
    thesis_status: str | None = None
    pending_alerts: int = 0
    pending_tier1_alerts: int = 0
    new_documents: int = 0
    next_earnings: str | None = None
    research_refreshed_at: str | None = None
    report_url: str | None = None


class WorkOsPortfolioAction(BaseModel):
    """One material, portfolio-company action eligible for the Cockpit queue."""

    model_config = ConfigDict(frozen=True)

    ticker: str
    headline: str
    detail: str
    tone: Literal["bad", "warn", "ok"]


class WorkOsPortfolioHydration(BaseModel):
    """Fail-closed response consumed by the prototype hydration runtime."""

    model_config = ConfigDict(frozen=True)

    status: Literal["ok", "degraded"]
    generated_at: str
    as_of: str | None = None
    total_market_value: float | None = None
    companies: list[WorkOsPortfolioCompany]
    actions: list[WorkOsPortfolioAction]
    warnings: list[str]


def _live_by_ticker(live: LivePortfolio) -> dict[str, LivePosition]:
    if not live.available:
        return {}
    return {
        position.ticker.strip().upper(): position
        for position in live.positions
        if position.ticker and position.ticker.strip()
    }


def _company(row: CockpitRow, live_position: LivePosition | None) -> WorkOsPortfolioCompany:
    ticker = row.base.ticker.strip().upper()
    return WorkOsPortfolioCompany(
        ticker=ticker,
        name=row.name or ticker,
        current_weight_pct=(
            live_position.percent_of_portfolio if live_position is not None else None
        ),
        market_value=live_position.market_value if live_position is not None else None,
        cost_basis=live_position.cost_basis if live_position is not None else None,
        unrealized_pnl=live_position.unrealized_pnl if live_position is not None else None,
        price=row.price,
        day_move_pct=row.day_move_pct,
        fair_value=row.fair_value,
        fair_value_gap_pct=row.fv_gap_pct,
        thesis_status=row.base.breach_status,
        pending_alerts=row.pending_alerts,
        pending_tier1_alerts=row.pending_tier1_alerts,
        new_documents=row.new_docs,
        next_earnings=row.next_earnings,
        research_refreshed_at=row.base.last_build_at or row.base.fmp_last_pulled,
        report_url=f"/reports/{ticker}" if row.base.last_build_at else None,
    )


def _action(row: CockpitRow) -> WorkOsPortfolioAction | None:
    ticker = row.base.ticker.strip().upper()
    if row.pending_tier1_alerts:
        return WorkOsPortfolioAction(
            ticker=ticker,
            headline="Review thesis-decisive alert",
            detail=f"{row.pending_tier1_alerts} falsifier or registered threshold breach",
            tone="bad",
        )
    status = (row.base.breach_status or "").strip().lower()
    if status and status not in {"intact", "pass", "passing", "ok"}:
        return WorkOsPortfolioAction(
            ticker=ticker,
            headline=f"Review {status} thesis state",
            detail=row.rule_summary or "Open the governed company brief and decision thresholds",
            tone="warn",
        )
    if row.pending_alerts:
        return WorkOsPortfolioAction(
            ticker=ticker,
            headline=f"Review {row.pending_alerts} pending alert"
            + ("s" if row.pending_alerts != 1 else ""),
            detail="Material company evidence is waiting for review",
            tone="warn",
        )
    if row.new_docs:
        return WorkOsPortfolioAction(
            ticker=ticker,
            headline=f"Review {row.new_docs} new document" + ("s" if row.new_docs != 1 else ""),
            detail="New governed research material is available",
            tone="ok",
        )
    return None


def build_work_os_portfolio(
    rows: Sequence[CockpitRow],
    live: LivePortfolio,
    *,
    generated_at: datetime | None = None,
) -> WorkOsPortfolioHydration:
    """Join research portfolio rows to optional current tracker positions."""

    built_at = (generated_at or datetime.now(UTC)).astimezone(UTC)
    positions = _live_by_ticker(live)
    companies = [_company(row, positions.get(row.base.ticker.strip().upper())) for row in rows]
    actions = [action for row in rows if (action := _action(row)) is not None][:3]
    warnings: list[str] = []
    if not live.available:
        warnings.append("portfolio_tracker_unavailable")
    elif live.is_stale:
        warnings.append("portfolio_tracker_stale")
    if live.is_partial:
        warnings.append("portfolio_tracker_partial")
    return WorkOsPortfolioHydration(
        status="ok" if live.available and not live.is_stale and not live.is_partial else "degraded",
        generated_at=built_at.isoformat().replace("+00:00", "Z"),
        as_of=live.as_of,
        total_market_value=live.total_market_value if live.available else None,
        companies=companies,
        actions=actions,
        warnings=warnings,
    )
