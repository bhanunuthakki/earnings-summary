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
