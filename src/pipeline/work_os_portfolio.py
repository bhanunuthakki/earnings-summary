"""Typed, portfolio-only hydration model for the Work OS shell.

The research database defines which companies belong to the portfolio UI. The
Portfolio Tracker enriches those names with current weights and market values
when it is available; a tracker failure never removes research companies or
leaks transport errors into the page.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from advisor.price_action_bands import (
    PriceActionBandProjection,
    resolve_price_action_bands,
)
from advisor.sizing_intent_review import load_sizing_intent_review_from_connection
from dcf.availability import resolve_dcf_route_artifact
from integrations.portfolio_allocation import (
    PortfolioAllocationProjection,
    unavailable_portfolio_allocation,
)
from integrations.portfolio_offline_snapshot import OfflinePortfolioSnapshot
from integrations.portfolio_tracker_client import LivePortfolio, LivePosition
from pipeline.research_cockpit import CockpitRow, PendingAlertRef
from pipeline.work_os_briefs import build_brief_library
from pipeline.work_os_earnings import EarningsReadoutSummary
from portfolio_risk_snapshot_store import RiskSnapshot

PriceActionBandState = Literal[
    "ratified",
    "draft",
    "derived",
    "partial",
    "stale",
    "unencoded",
    "unavailable",
]


class WorkOsPortfolioPriceActionBands(BaseModel):
    """Sanitized, human-facing price bands for one portfolio row.

    Checkpoint digests, source IDs, and owner identifiers remain behind the
    sizing-review doorway.  This DTO carries only the typed values needed to
    make the row readable without exposing internal provenance payloads.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    state: PriceActionBandState
    is_actionable: bool = False
    add_below: float | None = Field(default=None, gt=0)
    hold_low: float | None = Field(default=None, gt=0)
    hold_high: float | None = Field(default=None, gt=0)
    trim_above: float | None = Field(default=None, gt=0)
    sell_above: float | None = Field(default=None, gt=0)
    currency: str | None = None
    as_of: str | None = None
    review_url: str


_PRICE_ACTION_STATE_PRIORITY: dict[PriceActionBandState, int] = {
    "unavailable": 0,
    "unencoded": 1,
    "stale": 2,
    "derived": 3,
    "draft": 4,
    "partial": 5,
    "ratified": 6,
}


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
    dcf_url: str | None = None
    earnings_route: str | None = None
    earnings_label: str | None = None
    price_action_bands: WorkOsPortfolioPriceActionBands
    latest_earnings_readout: EarningsReadoutSummary | None = None


class WorkOsPortfolioAction(BaseModel):
    """One material, portfolio-company action eligible for the Cockpit queue.

    Identity/provenance fields are populated only when this bounded aggregate
    maps to exactly one persisted pending alert. Aggregate thesis/document
    rows remain explicitly unbound until their source identity contracts exist;
    no synthetic ID is emitted.
    """

    model_config = ConfigDict(frozen=True)

    ticker: str
    headline: str
    detail: str
    tone: Literal["bad", "warn", "ok"]
    action_id: str | None = None
    action_type: str | None = None
    lifecycle_state: Literal["pending"] | None = None
    source_ref: str | None = None
    evidence_ref: str | None = None


class WorkOsPortfolioResearchLinks(BaseModel):
    """Artifact-verified research doorways for one portfolio company."""

    model_config = ConfigDict(frozen=True)

    report_url: str | None = None
    dcf_url: str | None = None


class WorkOsAssetClassSplit(BaseModel):
    """Asset allocation with explicit availability and source provenance."""

    model_config = ConfigDict(frozen=True)

    availability: Literal["available", "unavailable"]
    source: Literal["instrument_registry"] = "instrument_registry"
    as_of: str | None = None
    reason: str | None = None
    weights_pct: dict[str, float] = Field(default_factory=dict)
    unclassified_weight_pct: float | None = None


class WorkOsRiskMetricSummary(BaseModel):
    """Whole-book risk metrics projected only from the persisted snapshot."""

    model_config = ConfigDict(frozen=True)

    availability: Literal["available", "unavailable"]
    source: Literal["portfolio_risk_snapshot"] = "portfolio_risk_snapshot"
    captured_at: str | None = None
    metric_version: str | None = None
    rebase_basis: str | None = None
    window_start: str | None = None
    window_end: str | None = None
    benchmark: str | None = None
    portfolio_beta: float | None = None
    sharpe_ratio: float | None = None
    tracking_error_annualized: float | None = None
    max_drawdown_pct: float | None = None


@dataclass(frozen=True)
class _AlertActionIdentity:
    """Internal conversion of an existing alert identity for the API model."""

    action_id: str
    action_type: str
    lifecycle_state: Literal["pending"]
    source_ref: str
    evidence_ref: str


class WorkOsPortfolioHydration(BaseModel):
    """Fail-closed response consumed by the prototype hydration runtime."""

    model_config = ConfigDict(frozen=True)

    status: Literal["ok", "degraded"]
    tracker_state: Literal["current", "stale", "partial", "unavailable", "offline_snapshot"]
    tracker_detail: str
    generated_at: str
    as_of: str | None = None
    total_market_value: float | None = None
    allocation: PortfolioAllocationProjection
    companies: list[WorkOsPortfolioCompany]
    earnings_readouts: list[EarningsReadoutSummary]
    actions: list[WorkOsPortfolioAction]
    warnings: list[str]
    asset_class_split: WorkOsAssetClassSplit
    risk_metric_summary: WorkOsRiskMetricSummary


_PUBLIC_WARNING_CODE = re.compile(r"^(?:[A-Z][A-Z0-9_]{0,119}|[a-z][a-z0-9_]{0,119})$")


def _risk_metric_summary(snapshot: RiskSnapshot | None) -> WorkOsRiskMetricSummary:
    if snapshot is None:
        return WorkOsRiskMetricSummary(availability="unavailable")
    return WorkOsRiskMetricSummary(
        availability="available",
        captured_at=snapshot.captured_at or None,
        metric_version=snapshot.metric_version,
        rebase_basis=snapshot.rebase_basis,
        window_start=snapshot.window_start,
        window_end=snapshot.window_end,
        benchmark=snapshot.benchmark,
        portfolio_beta=snapshot.beta,
        sharpe_ratio=snapshot.sharpe,
        tracking_error_annualized=snapshot.tracking_error_annualized,
        max_drawdown_pct=snapshot.max_drawdown_pct,
    )


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


def _price_action_band_dto(
    ticker: str,
    projection: PriceActionBandProjection,
) -> WorkOsPortfolioPriceActionBands:
    as_of = projection.as_of.isoformat() if projection.as_of is not None else None
    return WorkOsPortfolioPriceActionBands(
        state=projection.state.value,
        is_actionable=projection.is_actionable,
        add_below=projection.add_below,
        hold_low=projection.hold_low,
        hold_high=projection.hold_high,
        trim_above=projection.trim_above,
        sell_above=projection.sell_above,
        currency=projection.currency,
        as_of=as_of,
        review_url=f"/advisor/sizing-intents/{ticker}",
    )


def load_work_os_price_action_bands(
    conn: sqlite3.Connection,
    tickers: Sequence[str],
) -> dict[str, WorkOsPortfolioPriceActionBands]:
    """Load one sanitized sizing projection per ticker from ``conn``.

    The caller owns the request-scoped connection.  A missing source is
    unavailable; a present source with no entry is explicitly unencoded.
    When multiple intent kinds exist, deterministic state priority preserves
    the most human-useful ladder and then prefers the newest intent.
    """

    normalized_tickers = tuple(
        dict.fromkeys(ticker.strip().upper() for ticker in tickers if ticker.strip())
    )
    try:
        review = load_sizing_intent_review_from_connection(conn)
    except (OSError, TypeError, ValueError, sqlite3.Error):
        review = None

    output: dict[str, WorkOsPortfolioPriceActionBands] = {}
    for ticker in normalized_tickers:
        if review is None or not review.sizing_intent_source_available:
            projection = resolve_price_action_bands(owner_ratified=None, source_available=False)
        else:
            candidates = [
                entry for entry in review.entries if entry.intent.ticker.strip().upper() == ticker
            ]
            if candidates:
                selected = max(
                    candidates,
                    key=lambda entry: (
                        _PRICE_ACTION_STATE_PRIORITY[entry.price_action_bands.state.value],
                        entry.intent.updated_at,
                        entry.intent.id,
                    ),
                )
                projection = selected.price_action_bands
            else:
                projection = resolve_price_action_bands(owner_ratified=None)
        output[ticker] = _price_action_band_dto(ticker, projection)
    return output


def _company(
    row: CockpitRow,
    live_position: LivePosition | None,
    latest_earnings_readout: EarningsReadoutSummary | None,
    research_links: WorkOsPortfolioResearchLinks | None,
    price_action_bands: WorkOsPortfolioPriceActionBands | None,
) -> WorkOsPortfolioCompany:
    ticker = row.base.ticker.strip().upper()
    if price_action_bands is None:
        price_action_bands = _price_action_band_dto(
            ticker,
            resolve_price_action_bands(owner_ratified=None, source_available=False),
        )
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
        report_url=research_links.report_url if research_links is not None else None,
        dcf_url=research_links.dcf_url if research_links is not None else None,
        earnings_route=earnings_route,
        earnings_label=earnings_label,
        price_action_bands=price_action_bands,
        latest_earnings_readout=latest_earnings_readout,
    )


def _exact_alert_identity(
    row: CockpitRow,
    *,
    decisive: bool,
) -> _AlertActionIdentity | None:
    """Return identity only when one queue card has one matching alert.

    The queue remains an aggregate bounded to three cards. Attaching one
    alert ID to a card that summarizes multiple alerts would be false
    provenance, so ambiguous cards intentionally remain unbound.
    """
    refs = tuple(ref for ref in row.pending_alert_refs if ref.is_decisive is decisive)
    expected_count = row.pending_tier1_alerts if decisive else row.pending_alerts
    if expected_count != 1 or len(refs) != 1:
        return None
    ref: PendingAlertRef = refs[0]
    source_ref = f"alert:{ref.alert_id}"
    return _AlertActionIdentity(
        action_id=source_ref,
        action_type=ref.trigger_kind,
        lifecycle_state="pending",
        source_ref=source_ref,
        evidence_ref=ref.signature_sha,
    )


def _action(row: CockpitRow) -> WorkOsPortfolioAction | None:
    ticker = row.base.ticker.strip().upper()
    if row.pending_tier1_alerts:
        identity = _exact_alert_identity(row, decisive=True)
        return WorkOsPortfolioAction(
            ticker=ticker,
            headline="Review thesis-decisive alert",
            detail=f"{row.pending_tier1_alerts} falsifier or registered threshold breach",
            tone="bad",
            action_id=identity.action_id if identity is not None else None,
            action_type=identity.action_type if identity is not None else None,
            lifecycle_state=identity.lifecycle_state if identity is not None else None,
            source_ref=identity.source_ref if identity is not None else None,
            evidence_ref=identity.evidence_ref if identity is not None else None,
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
        identity = _exact_alert_identity(row, decisive=False)
        return WorkOsPortfolioAction(
            ticker=ticker,
            headline=f"Review {row.pending_alerts} pending alert"
            + ("s" if row.pending_alerts != 1 else ""),
            detail="Material company evidence is waiting for review",
            tone="warn",
            action_id=identity.action_id if identity is not None else None,
            action_type=identity.action_type if identity is not None else None,
            lifecycle_state=identity.lifecycle_state if identity is not None else None,
            source_ref=identity.source_ref if identity is not None else None,
            evidence_ref=identity.evidence_ref if identity is not None else None,
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
    allocation: PortfolioAllocationProjection,
    *,
    latest_readouts: Mapping[str, EarningsReadoutSummary] | None = None,
    research_links: Mapping[str, WorkOsPortfolioResearchLinks] | None = None,
    price_action_bands: Mapping[str, WorkOsPortfolioPriceActionBands] | None = None,
    readout_warnings: Sequence[str] = (),
    offline_snapshot: OfflinePortfolioSnapshot | None = None,
    risk_snapshot: RiskSnapshot | None = None,
    generated_at: datetime | None = None,
) -> WorkOsPortfolioHydration:
    """Join research portfolio rows to optional current tracker positions."""

    built_at = (generated_at or datetime.now(UTC)).astimezone(UTC)
    snapshot_active = not live.available and offline_snapshot is not None
    portfolio_source = (
        offline_snapshot.portfolio if offline_snapshot is not None and snapshot_active else live
    )
    positions = _live_by_ticker(portfolio_source)
    readouts = latest_readouts or {}
    verified_links = research_links or {}
    verified_price_action_bands = price_action_bands or {}
    companies = [
        _company(
            row,
            positions.get(row.base.ticker.strip().upper()),
            readouts.get(row.base.ticker.strip().upper()),
            verified_links.get(row.base.ticker.strip().upper()),
            verified_price_action_bands.get(row.base.ticker.strip().upper()),
        )
        for row in rows
    ]
    actions = [action for row in rows if (action := _action(row)) is not None][:3]
    tracker_warnings: list[str] = list(live.envelope_warnings)
    if snapshot_active:
        tracker_warnings.extend(portfolio_source.envelope_warnings)
    if not live.available:
        tracker_warnings.append("portfolio_tracker_unavailable")
    elif live.is_stale:
        tracker_warnings.append("portfolio_tracker_stale")
    if live.is_partial:
        tracker_warnings.append("portfolio_tracker_partial")
    if (
        portfolio_source.as_of
        and allocation.as_of
        and portfolio_source.as_of != allocation.as_of.isoformat()
    ):
        allocation = unavailable_portfolio_allocation("snapshot_date_mismatch")
    allocation_warnings: list[str] = []
    if allocation.state == "incomplete":
        allocation_warnings.append("portfolio_allocation_incomplete")
    elif allocation.state == "unavailable":
        allocation_warnings.append("portfolio_allocation_unavailable")
    warnings = _public_warning_codes(
        readout_warnings,
        tracker_warnings,
        allocation_warnings,
    )

    tracker_state: Literal["current", "stale", "partial", "unavailable", "offline_snapshot"]
    if snapshot_active:
        tracker_state = "offline_snapshot"
    elif not live.available:
        tracker_state = "unavailable"
    elif live.is_stale:
        tracker_state = "stale"
    elif live.is_partial:
        tracker_state = "partial"
    else:
        tracker_state = "current"
    if tracker_state == "offline_snapshot":
        tracker_detail = f"Offline snapshot · {portfolio_source.as_of}"
    elif tracker_state == "unavailable":
        tracker_detail = "Tracker unavailable · research data only"
    elif live.as_of:
        tracker_detail = f"Tracker connected · {tracker_state} · As of {live.as_of}"
    else:
        tracker_detail = f"Live tracker connected · {tracker_state} · observation date unavailable"
    unclassified_weight = (
        sum(position.percent_of_portfolio or 0.0 for position in portfolio_source.positions)
        if portfolio_source.available
        else None
    )
    asset_class_split = WorkOsAssetClassSplit(
        availability="unavailable",
        reason="Complete instrument asset-class and domicile metadata is unavailable",
        unclassified_weight_pct=(
            round(unclassified_weight, 2) if unclassified_weight is not None else None
        ),
    )
    return WorkOsPortfolioHydration(
        status=(
            "ok"
            if (
                live.available
                and not live.is_stale
                and not live.is_partial
                and allocation.state == "available"
                and not warnings
            )
            else "degraded"
        ),
        tracker_state=tracker_state,
        tracker_detail=tracker_detail,
        generated_at=built_at.isoformat().replace("+00:00", "Z"),
        as_of=portfolio_source.as_of if snapshot_active else live.as_of,
        total_market_value=(
            portfolio_source.total_market_value if portfolio_source.available else None
        ),
        allocation=allocation,
        companies=companies,
        earnings_readouts=sorted(
            readouts.values(),
            key=lambda readout: (readout.fiscal_period, readout.ticker),
            reverse=True,
        ),
        actions=actions,
        warnings=warnings,
        asset_class_split=asset_class_split,
        risk_metric_summary=_risk_metric_summary(risk_snapshot),
    )


def build_work_os_portfolio_research_links(
    rows: Sequence[CockpitRow],
    repo_root: Path,
    conn: sqlite3.Connection,
) -> dict[str, WorkOsPortfolioResearchLinks]:
    """Resolve only concrete brief and DCF artifacts for portfolio rows."""

    try:
        library = build_brief_library(
            repo_root,
            conn=conn,
            artifact_kind="full_brief",
            coverage_role="portfolio",
            limit=10_000,
        )
        report_urls = {
            item.ticker.strip().upper(): item.standalone_url
            for item in library.items
            if item.coverage_role == "portfolio" and item.standalone_url
        }
    except (OSError, ValueError, sqlite3.Error):
        report_urls = {}

    links: dict[str, WorkOsPortfolioResearchLinks] = {}
    for row in rows:
        ticker = row.base.ticker.strip().upper()
        dcf_url = None
        if not row.is_etf:
            try:
                if resolve_dcf_route_artifact(repo_root, ticker) is not None:
                    dcf_url = f"/dcf/{ticker}"
            except (OSError, TypeError, ValueError):
                dcf_url = None
        report_url = report_urls.get(ticker)
        if report_url or dcf_url:
            links[ticker] = WorkOsPortfolioResearchLinks(
                report_url=report_url,
                dcf_url=dcf_url,
            )
    return links
