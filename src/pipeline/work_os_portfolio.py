"""Typed, portfolio-only hydration model for the Work OS shell.

The research database defines which companies belong to the portfolio UI. The
Portfolio Tracker enriches those names with current weights and market values
when it is available; a tracker failure never removes research companies or
leaks transport errors into the page.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict

from integrations.portfolio_tracker_client import LivePortfolio, LivePosition
from pipeline.research_cockpit import CockpitRow
from pipeline.work_os_earnings import EarningsReadoutSummary


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
    earnings_route: str | None = None
    earnings_label: str | None = None
    latest_earnings_readout: EarningsReadoutSummary | None = None


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
    tracker_state: Literal["current", "stale", "partial", "unavailable"]
    tracker_detail: str
    generated_at: str
    as_of: str | None = None
    total_market_value: float | None = None
    companies: list[WorkOsPortfolioCompany]
    earnings_readouts: list[EarningsReadoutSummary]
    actions: list[WorkOsPortfolioAction]
    warnings: list[str]


_PUBLIC_WARNING_CODE = re.compile(r"^[a-z][a-z0-9_]{0,119}$")


def _public_warning_codes(*warning_sets: Sequence[str]) -> list[str]:
    """Keep stable provider/readout warning codes while excluding transport text."""

    seen: set[str] = set()
    warnings: list[str] = []
    for warning_set in warning_sets:
        for warning in warning_set:
            if not _PUBLIC_WARNING_CODE.fullmatch(warning) or warning in seen:
                continue
            seen.add(warning)
            warnings.append(warning)
    return warnings


def _live_by_ticker(live: LivePortfolio) -> dict[str, LivePosition]:
    if not live.available:
        return {}
    return {
        position.ticker.strip().upper(): position
        for position in live.positions
        if position.ticker and position.ticker.strip()
    }


def _company(
    row: CockpitRow,
    live_position: LivePosition | None,
    latest_earnings_readout: EarningsReadoutSummary | None,
) -> WorkOsPortfolioCompany:
    ticker = row.base.ticker.strip().upper()
    if latest_earnings_readout is not None:
        earnings_route = latest_earnings_readout.route
        earnings_label = f"{latest_earnings_readout.period_label} readout →"
    elif row.base.last_transcript and row.base.last_transcript.period_end:
        earnings_route = f"/api/peek/earnings-readout?ticker={ticker}"
        earnings_label = "Generate readout →"
    elif row.next_earnings:
        earnings_route = f"/api/peek/earnings-prep?ticker={ticker}"
        earnings_label = f"ER {row.next_earnings}"
    else:
        earnings_route = None
        earnings_label = None
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
        earnings_route=earnings_route,
        earnings_label=earnings_label,
        latest_earnings_readout=latest_earnings_readout,
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
    latest_readouts: Mapping[str, EarningsReadoutSummary] | None = None,
    readout_warnings: Sequence[str] = (),
    generated_at: datetime | None = None,
) -> WorkOsPortfolioHydration:
    """Join research portfolio rows to optional current tracker positions."""

    built_at = (generated_at or datetime.now(UTC)).astimezone(UTC)
    positions = _live_by_ticker(live)
    readouts = latest_readouts or {}
    companies = [
        _company(
            row,
            positions.get(row.base.ticker.strip().upper()),
            readouts.get(row.base.ticker.strip().upper()),
        )
        for row in rows
    ]
    actions = [action for row in rows if (action := _action(row)) is not None][:3]
    tracker_warnings: list[str] = list(live.envelope_warnings)
    if not live.available:
        tracker_warnings.append("portfolio_tracker_unavailable")
    elif live.is_stale:
        tracker_warnings.append("portfolio_tracker_stale")
    if live.is_partial:
        tracker_warnings.append("portfolio_tracker_partial")
    warnings = _public_warning_codes(readout_warnings, tracker_warnings)
    tracker_state: Literal["current", "stale", "partial", "unavailable"]
    if not live.available:
        tracker_state = "unavailable"
    elif live.is_stale:
        tracker_state = "stale"
    elif live.is_partial:
        tracker_state = "partial"
    else:
        tracker_state = "current"
    if tracker_state == "unavailable":
        tracker_detail = "Tracker unavailable · research data only"
    elif live.as_of:
        tracker_detail = f"Tracker connected · {tracker_state} · As of {live.as_of}"
    else:
        tracker_detail = f"Live tracker connected · {tracker_state} · observation date unavailable"
    return WorkOsPortfolioHydration(
        status=(
            "ok"
            if live.available and not live.is_stale and not live.is_partial and not warnings
            else "degraded"
        ),
        tracker_state=tracker_state,
        tracker_detail=tracker_detail,
        generated_at=built_at.isoformat().replace("+00:00", "Z"),
        as_of=live.as_of,
        total_market_value=live.total_market_value if live.available else None,
        companies=companies,
        earnings_readouts=sorted(
            readouts.values(),
            key=lambda readout: (readout.fiscal_period, readout.ticker),
            reverse=True,
        ),
        actions=actions,
        warnings=warnings,
    )
