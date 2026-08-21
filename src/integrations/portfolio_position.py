"""BHA-74 policy adapter over the canonical typed Portfolio Tracker v1 client."""

from __future__ import annotations

import os
import re
from collections.abc import Callable
from datetime import UTC, date, datetime
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from typing import Literal, Protocol, TypeGuard, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from integrations.portfolio_tracker_v1 import (
    HealthV1,
    PortfolioSnapshotV1,
    PositionLotV1,
    PositionsV1Result,
    TrackerV1Client,
    TransactionV1,
    V1Fetch,
    V1Warning,
)


class PositionProvenance(BaseModel):
    """Identity and freshness facts attached to every canonical read."""

    source_identity: str
    snapshot_as_of: date | None
    account_coverage: int
    snapshot_account_coverage: int = 0
    included_account_ids: list[int] = Field(default_factory=list[int])
    excluded_account_ids: list[int] = Field(default_factory=list[int])
    lagging_account_ids: list[int] = Field(default_factory=list[int])
    is_stale: bool
    schema_version: str = "unknown"
    is_partial: bool = False
    currency: str = "unknown"
    warnings: list[V1Warning] = Field(default_factory=list[V1Warning])


class PortfolioPositionAccount(BaseModel):
    """Position-lot facts normalized for consumers outside the report package."""

    account_name: str
    quantity: float
    cost_basis: float | None = None
    cost_basis_source: str | None = None
    market_value: float | None = None
    unrealized_pnl: float | None = None
    unrealized_pct: float | None = None
    snapshot_date: date | None = None


class SnapshotTransaction(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    ticker: str
    date: date
    account_name: str
    type: str
    quantity: float
    amount: float


class SnapshotDecision(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    ticker: str
    decision_date: date
    action: str
    confidence: str | None = None
    thesis: str
    linked_brief_path: str | None = None
    outcome_status: str | None = None
    outcome_date: date | None = None
    outcome_notes: str | None = None


def _empty_snapshot_transactions() -> list[SnapshotTransaction]:
    return []


def _empty_snapshot_decisions() -> list[SnapshotDecision]:
    return []


def supported_schema_major(schema_version: str) -> bool:
    match = re.fullmatch(r"(\d+)\.\d+\.\d+", schema_version)
    return match is not None and int(match.group(1)) == 1


class PortfolioPositionResult(BaseModel):
    """A source state is never inferred from the absence of a position."""

    state: Literal["held", "not_held", "source_unavailable"]
    accounts: list[PortfolioPositionAccount] = Field(default_factory=list[PortfolioPositionAccount])
    total_quantity: float = 0.0
    total_cost_basis: float | None = None
    total_market_value: float | None = None
    total_unrealized_pnl: float | None = None
    total_unrealized_pct: float | None = None
    position_as_of: date | None = None
    provenance: PositionProvenance | None = None
    recent_transactions: list[SnapshotTransaction] = Field(
        default_factory=_empty_snapshot_transactions
    )
    open_decisions: list[SnapshotDecision] = Field(default_factory=_empty_snapshot_decisions)
    closed_decisions: list[SnapshotDecision] = Field(default_factory=_empty_snapshot_decisions)
    history_state: Literal["available", "partial", "unavailable"] = "unavailable"
    history_error: str | None = None
    error_code: str | None = None
    error_detail: str | None = None


class TrackerReader(Protocol):
    def probe_v1(self) -> V1Fetch[HealthV1]: ...

    def get_portfolio_snapshot(self) -> V1Fetch[PortfolioSnapshotV1]: ...


class PortfolioPositionAdapter:
    """Adapt one typed tracker reader into an auditable tri-state position."""

    def __init__(
        self, client: TrackerReader, *, source_identity: str = "portfolio_tracker_api_v1"
    ) -> None:
        self._client = client
        self._source_identity = source_identity

    def resolve(self, ticker: str) -> PortfolioPositionResult:
        normalized = ticker.upper().strip()
        health_fetch = self._client.probe_v1()
        if not health_fetch.available or health_fetch.data is None:
            return _unavailable("health_unavailable", health_fetch.error)
        health = health_fetch.data
        if (
            health.status != "ok"
            or not health.database_ok
            or health.active_account_count < 1
            or health.is_stale
        ):
            return _unavailable(
                "health_invalid", "tracker health does not prove a usable portfolio snapshot"
            )
        if health.generated_at.tzinfo is None or health.generated_at > datetime.now(UTC):
            return _unavailable("health_clock_invalid", "tracker health timestamp is incoherent")

        snapshot_fetch = self._client.get_portfolio_snapshot()
        if not snapshot_fetch.available or snapshot_fetch.data is None:
            return _unavailable("portfolio_snapshot_unavailable", snapshot_fetch.error)
        snapshot = snapshot_fetch.data
        meta = snapshot.meta
        if not supported_schema_major(meta.schema_version):
            return _unavailable(
                "incompatible_schema_version",
                "portfolio snapshot schema major is unsupported",
            )
        if not meta.currency.strip():
            return _unavailable("currency_missing", "portfolio snapshot currency is required")
        if any(
            account.value_currency.strip() != meta.currency.strip() for account in snapshot.accounts
        ):
            return _unavailable(
                "currency_mismatch", "portfolio account currency does not match snapshot currency"
            )
        if (meta.as_of is None) != (health.latest_snapshot_date is None):
            return _unavailable(
                "snapshot_date_incomplete",
                "health and portfolio snapshot must both provide the snapshot date",
            )
        if meta.as_of is None and health.latest_snapshot_date is None:
            return _unavailable(
                "snapshot_date_missing",
                "portfolio snapshot has no observation date; current position is unproven",
            )
        if meta.as_of != health.latest_snapshot_date:
            return _unavailable(
                "snapshot_date_mismatch",
                "health and portfolio snapshot do not describe the same snapshot date",
            )
        equity_error = validate_equity_evidence(snapshot)
        if equity_error is not None:
            return _unavailable(*equity_error)
        positions = PositionsV1Result(
            snapshot_date=meta.as_of,
            total_market_value=snapshot.total_market_value,
            positions=snapshot.positions,
            by_tax_treatment=snapshot.by_tax_treatment,
            notes=[],
        )
        coverage_error = validate_snapshot_account_coverage(snapshot, health)
        if coverage_error is not None:
            return _unavailable(*coverage_error)
        reconciliation_error = validate_positions_snapshot(positions)
        if reconciliation_error is not None:
            return _unavailable(*reconciliation_error)
        matching_positions = [
            item
            for item in positions.positions
            if item.ticker and item.ticker.upper() == normalized
        ]
        if len(matching_positions) > 1:
            return _unavailable(
                "duplicate_ticker_position", "tracker snapshot contains duplicate ticker positions"
            )
        position = matching_positions[0] if matching_positions else None
        raw_partial = meta.is_partial or snapshot.equity_fraction.is_partial
        lagging_account_ids = list(meta.account_coverage.lagging_account_ids)
        provenance = PositionProvenance(
            source_identity=self._source_identity,
            snapshot_as_of=meta.as_of or health.latest_snapshot_date,
            account_coverage=len(position.accounts) if position is not None else 0,
            snapshot_account_coverage=len(meta.account_coverage.included_account_ids),
            included_account_ids=list(meta.account_coverage.included_account_ids),
            excluded_account_ids=list(meta.account_coverage.excluded_account_ids),
            lagging_account_ids=lagging_account_ids,
            is_stale=health.is_stale or meta.is_stale or snapshot.equity_fraction.is_stale,
            schema_version=meta.schema_version,
            # An explicitly lagging account makes the whole position result
            # incomplete even when the provider forgot to set is_partial.
            is_partial=raw_partial or bool(lagging_account_ids),
            currency=meta.currency,
            warnings=(
                list(meta.warnings)
                + list(snapshot.equity_fraction.warnings)
                + [warning for account in snapshot.accounts for warning in account.warnings]
            ),
        )
        if raw_partial:
            return _unavailable(
                "portfolio_snapshot_partial",
                "partial provider evidence cannot be presented as a complete position",
                provenance=provenance,
            )
        if lagging_account_ids:
            ids = ", ".join(str(account_id) for account_id in lagging_account_ids)
            return _unavailable(
                "portfolio_snapshot_account_coverage_lagging",
                "portfolio snapshot account coverage is lagging for account ids: "
                f"{ids}; current held/not-held status is unproven",
                provenance=provenance,
            )
        if provenance.is_stale:
            return _unavailable(
                "portfolio_snapshot_stale",
                "stale portfolio evidence cannot be presented as a current position",
                provenance=provenance,
            )
        history_state, history_error, transactions = self._history(normalized)
        if position is None or position.quantity <= 0:
            if provenance.is_stale:
                return _unavailable(
                    "stale_snapshot_no_position",
                    "stale portfolio evidence cannot prove that the ticker is not held",
                    provenance=provenance,
                )
            return PortfolioPositionResult(
                state="not_held",
                position_as_of=provenance.snapshot_as_of,
                provenance=provenance,
                recent_transactions=transactions,
                history_state=history_state,
                history_error=history_error,
            )

        account_ids = [account.account_id for account in position.accounts]
        if len(account_ids) != len(set(account_ids)):
            return _unavailable(
                "duplicate_account_snapshot",
                "tracker position contains duplicate account evidence",
                provenance=provenance,
            )
        if not _position_lots_reconcile(
            position.quantity,
            position.market_value,
            position.cost_basis,
            position.unrealized_pnl,
            position.accounts,
        ):
            return _unavailable(
                "position_lot_reconciliation_failed",
                "aggregate position does not reconcile to attributable account lots",
                provenance=provenance,
            )
        accounts = [
            PortfolioPositionAccount(
                account_name=account.account_name,
                quantity=float(account.quantity),
                cost_basis=float(account.cost_basis) if account.cost_basis is not None else None,
                cost_basis_source=account.cost_basis_source,
                market_value=float(account.market_value)
                if account.market_value is not None
                else None,
                unrealized_pnl=(
                    float(account.market_value - account.cost_basis)
                    if account.market_value is not None and account.cost_basis is not None
                    else None
                ),
                unrealized_pct=(
                    float((account.market_value - account.cost_basis) / account.cost_basis)
                    if account.market_value is not None
                    and account.cost_basis is not None
                    and account.cost_basis > 0
                    else None
                ),
                snapshot_date=provenance.snapshot_as_of,
            )
            for account in position.accounts
        ]
        cost_basis = float(position.cost_basis) if position.cost_basis is not None else None
        unrealized_pnl = (
            float(position.unrealized_pnl) if position.unrealized_pnl is not None else None
        )
        return PortfolioPositionResult(
            state="held",
            accounts=accounts,
            total_quantity=float(position.quantity),
            total_cost_basis=cost_basis,
            total_market_value=float(position.market_value)
            if position.market_value is not None
            else None,
            total_unrealized_pnl=unrealized_pnl,
            total_unrealized_pct=(
                unrealized_pnl / cost_basis
                if unrealized_pnl is not None and cost_basis is not None and cost_basis > 0
                else None
            ),
            position_as_of=provenance.snapshot_as_of,
            provenance=provenance,
            recent_transactions=transactions,
            history_state=history_state,
            history_error=history_error,
        )

    def _history(
        self, ticker: str
    ) -> tuple[
        Literal["available", "partial", "unavailable"], str | None, list[SnapshotTransaction]
    ]:
        """Read canonical transactions; never turn an unsupported history read into empty history."""

        fetch_transactions = getattr(self._client, "get_all_transactions", None)
        if not callable(fetch_transactions):
            return "partial", "transaction history capability is not exposed by this client", []
        typed_fetch = cast("Callable[[], V1Fetch[list[TransactionV1]]]", fetch_transactions)
        fetched = typed_fetch()
        if not fetched.available or fetched.data is None:
            return "partial", fetched.error or "transaction history is unavailable", []
        return (
            "partial",
            "open and closed decision history is not part of the tracker v1 read contract",
            [
                SnapshotTransaction(
                    ticker=item.ticker.upper(),
                    date=item.date,
                    account_name=item.account_name,
                    type=item.type,
                    quantity=float(item.quantity),
                    amount=float(item.amount),
                )
                for item in fetched.data
                if item.ticker and item.ticker.upper() == ticker
            ],
        )


class ImmutableTrackerSnapshot(BaseModel):
    """An explicitly configured, immutable replacement for a live read."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    source_identity: str = "portfolio_tracker_snapshot"
    health: HealthV1
    portfolio_snapshot: PortfolioSnapshotV1
    recent_transactions: list[SnapshotTransaction] = Field(
        default_factory=list[SnapshotTransaction]
    )
    open_decisions: list[SnapshotDecision] = Field(default_factory=list[SnapshotDecision])
    closed_decisions: list[SnapshotDecision] = Field(default_factory=list[SnapshotDecision])
    history_state: Literal["available", "partial", "unavailable"] | None = None
    history_error: str | None = None


def resolve_configured_position(ticker: str) -> PortfolioPositionResult:
    """Resolve only an explicit API endpoint or immutable snapshot.

    This deliberately does not discover sibling checkouts: detached report builds
    must not change meaning because a developer happens to have another repo nearby.
    """
    snapshot_path = os.environ.get("PORTFOLIO_TRACKER_SNAPSHOT_PATH")
    if snapshot_path:
        return _resolve_snapshot(ticker, Path(snapshot_path))
    base_url = os.environ.get("PORTFOLIO_TRACKER_API_URL")
    if not base_url:
        return _unavailable(
            "tracker_not_configured",
            "no PORTFOLIO_TRACKER_API_URL or PORTFOLIO_TRACKER_SNAPSHOT_PATH is configured",
        )
    return PortfolioPositionAdapter(TrackerV1Client(base_url=base_url)).resolve(ticker)


def _resolve_snapshot(ticker: str, path: Path) -> PortfolioPositionResult:
    try:
        with path.open("rb") as handle:
            payload = handle.read(1_048_577)
        if len(payload) > 1_048_576:
            return _unavailable("snapshot_unavailable", "configured tracker snapshot is too large")
        snapshot = ImmutableTrackerSnapshot.model_validate_json(payload)
    except (OSError, ValidationError, ValueError):
        return _unavailable("snapshot_invalid", "configured tracker snapshot failed validation")
    if not supported_schema_major(snapshot.portfolio_snapshot.meta.schema_version):
        return _unavailable(
            "incompatible_schema_version",
            "configured tracker snapshot schema major is unsupported",
        )
    adapter = PortfolioPositionAdapter(
        _SnapshotReader(snapshot),
        source_identity=f"{snapshot.source_identity}:sha256:{sha256(payload).hexdigest()}",
    )
    resolved = adapter.resolve(ticker)
    normalized = ticker.upper().strip()
    try:
        recent_transactions = [
            item for item in snapshot.recent_transactions if item.ticker.upper() == normalized
        ]
        open_decisions = [
            item for item in snapshot.open_decisions if item.ticker.upper() == normalized
        ]
        closed_decisions = [
            item for item in snapshot.closed_decisions if item.ticker.upper() == normalized
        ]
    except (AttributeError, TypeError):
        return _unavailable(
            "snapshot_history_invalid",
            "immutable history ticker identity is invalid",
        )
    return resolved.model_copy(
        update={
            "recent_transactions": recent_transactions,
            "open_decisions": open_decisions,
            "closed_decisions": closed_decisions,
            "history_state": snapshot.history_state
            or (
                "available"
                if recent_transactions and open_decisions and closed_decisions
                else "partial"
            ),
            "history_error": snapshot.history_error
            or (
                None
                if recent_transactions and open_decisions and closed_decisions
                else "transaction, open-decision, and closed-decision history is not complete in this snapshot"
            ),
        }
    )


def _position_lots_reconcile(
    quantity: Decimal,
    market_value: Decimal | None,
    cost_basis: Decimal | None,
    unrealized_pnl: Decimal | None,
    accounts: list[PositionLotV1],
) -> bool:
    """Reject a held position whose attributable lots disagree with it."""

    lots = accounts
    if not _position_structure_reconcile(quantity, market_value, lots):
        return False
    if quantity == 0 and any(lot.cost_basis not in (None, Decimal(0)) for lot in lots):
        return False
    if quantity == 0 and unrealized_pnl not in (None, Decimal(0)):
        return False
    cost_values = [lot.cost_basis for lot in lots]
    if cost_basis is None:
        if any(value is not None for value in cost_values):
            return False
    else:
        numeric_cost_values = [value for value in cost_values if value is not None]
        if (
            len(numeric_cost_values) != len(cost_values)
            or sum(numeric_cost_values, Decimal(0)) != cost_basis
        ):
            return False
    if unrealized_pnl is None:
        if market_value is not None and cost_basis is not None:
            return False
    elif market_value is None or cost_basis is None or unrealized_pnl != market_value - cost_basis:
        return False
    lot_pnl = [
        lot.market_value - lot.cost_basis
        for lot in lots
        if lot.market_value is not None and lot.cost_basis is not None
    ]
    return not (
        unrealized_pnl is not None
        and (len(lot_pnl) != len(lots) or sum(lot_pnl, Decimal(0)) != unrealized_pnl)
    )


def _position_structure_reconcile(
    quantity: Decimal, market_value: Decimal | None, accounts: list[PositionLotV1]
) -> bool:
    """Validate quantity and market-value structure without optional cost facts."""

    if quantity < 0 or any(lot.quantity < 0 for lot in accounts):
        return False
    if quantity == 0 and any(lot.market_value not in (None, Decimal(0)) for lot in accounts):
        return False
    if sum((lot.quantity for lot in accounts), Decimal(0)) != quantity:
        return False
    market_values = [lot.market_value for lot in accounts]
    if market_value is None:
        return all(value is None for value in market_values)
    numeric_market_values = [value for value in market_values if value is not None]
    return (
        len(numeric_market_values) == len(market_values)
        and sum(numeric_market_values, Decimal(0)) == market_value
    )


def _portfolio_total_reconcile(positions: PositionsV1Result) -> bool:
    if any(
        not _position_structure_reconcile(
            item.quantity,
            item.market_value,
            item.accounts,
        )
        for item in positions.positions
    ):
        return False
    values = [item.market_value for item in positions.positions]
    if any(value is None for value in values):
        return False
    return (
        sum((value for value in values if value is not None), Decimal(0))
        == positions.total_market_value
    )


def validate_positions_snapshot(
    positions: PositionsV1Result,
) -> tuple[str, str] | None:
    """Validate every row before any ticker absence decision is made."""

    if any(
        item.quantity < 0 or any(lot.quantity < 0 for lot in item.accounts)
        for item in positions.positions
    ):
        return (
            "negative_quantity_unsupported",
            "negative positions require an explicit typed short-position contract",
        )
    if any(
        len({lot.account_id for lot in item.accounts}) != len(item.accounts)
        for item in positions.positions
    ):
        return (
            "duplicate_account_snapshot",
            "tracker position contains duplicate account evidence",
        )
    if not _portfolio_total_reconcile(positions):
        return (
            "portfolio_total_reconciliation_failed",
            "portfolio total market value does not reconcile to its positions",
        )
    return None


def validate_equity_evidence(snapshot: PortfolioSnapshotV1) -> tuple[str, str] | None:
    """Validate the typed equity fact before using any ticker absence."""

    evidence = snapshot.equity_fraction
    if evidence.unit != "fraction":
        return "equity_unit_invalid", "equity evidence must use the fraction unit"
    if evidence.holdings_as_of != snapshot.meta.as_of:
        return "equity_snapshot_date_mismatch", "equity holdings date must match envelope as_of"
    fraction = evidence.equity_fraction
    if not _finite_decimal(evidence.denominator_value) or evidence.denominator_value <= 0:
        return "equity_denominator_invalid", "equity evidence requires a positive denominator"
    if fraction is None:
        return "equity_fraction_missing", "equity evidence fraction is required"
    if not _finite_decimal(fraction):
        return "equity_fraction_missing", "equity evidence fraction is required"
    if not _finite_decimal(evidence.equity_value):
        return "equity_value_invalid", "equity evidence value is required and finite"
    if evidence.equity_value < 0:
        return "equity_value_invalid", "equity evidence value cannot be negative"
    if snapshot.total_market_value != evidence.denominator_value:
        return (
            "equity_denominator_mismatch",
            "equity denominator does not match the portfolio total market value",
        )
    if not Decimal(0) <= fraction <= Decimal(1):
        return "equity_fraction_invalid", "equity fraction must be between zero and one"
    recomputed = evidence.equity_value / evidence.denominator_value
    if abs(recomputed - fraction) > Decimal("0.000001"):
        return (
            "equity_fraction_reconciliation_failed",
            "equity fraction does not reconcile to values",
        )
    return None


def _finite_decimal(value: object) -> TypeGuard[Decimal]:
    return isinstance(value, Decimal) and value.is_finite()


def validate_snapshot_account_coverage(
    snapshot: PortfolioSnapshotV1, health: HealthV1
) -> tuple[str, str] | None:
    """Bind every position lot to an included, active envelope account."""

    account_ids = [account.account_id for account in snapshot.accounts]
    if len(account_ids) != len(set(account_ids)):
        return "account_coverage_invalid", "portfolio snapshot repeats an account id"
    coverage = snapshot.meta.account_coverage
    included_ids = coverage.included_account_ids
    excluded_ids = coverage.excluded_account_ids
    if (
        len(included_ids) != len(set(included_ids))
        or len(excluded_ids) != len(set(excluded_ids))
        or set(included_ids).intersection(excluded_ids)
    ):
        return (
            "account_coverage_invalid",
            "portfolio envelope included and excluded account ids must be unique and disjoint",
        )
    included = set(included_ids)
    active = {
        account.account_id
        for account in snapshot.accounts
        if account.active and account.included_in_totals
    }
    if len(active) != health.active_account_count or not active.issubset(included):
        return (
            "account_coverage_invalid",
            "health active account count does not match included active envelope accounts",
        )
    lagging = set(coverage.lagging_account_ids)
    if len(lagging) != len(coverage.lagging_account_ids) or not lagging.issubset(active):
        return (
            "account_coverage_invalid",
            "lagging account ids must be unique active included accounts",
        )
    if snapshot.meta.as_of is not None:
        for account in snapshot.accounts:
            if not account.active or not account.included_in_totals:
                continue
            if account.account_id not in included:
                continue
            if account.account_id in lagging:
                if account.holdings_as_of is None and account.value is None:
                    return (
                        "account_coverage_invalid",
                        f"empty included account {account.account_id} cannot be marked lagging",
                    )
                continue
            if account.holdings_as_of is None and account.value is None:
                if (
                    health.is_stale
                    or snapshot.meta.is_partial
                    or snapshot.meta.is_stale
                    or snapshot.equity_fraction.is_partial
                    or snapshot.equity_fraction.is_stale
                ):
                    return (
                        "account_coverage_invalid",
                        f"empty account {account.account_id} is not valid in a partial or stale envelope",
                    )
                continue
            if account.holdings_as_of is None:
                return (
                    "account_coverage_invalid",
                    f"active account {account.account_id} has value without a holdings date",
                )
            if account.holdings_as_of != snapshot.meta.as_of and account.account_id not in lagging:
                return (
                    "account_coverage_invalid",
                    f"active account {account.account_id} holdings date is not covered by "
                    "the portfolio snapshot envelope",
                )
    equity_included = snapshot.equity_fraction.included_account_ids
    equity_excluded = snapshot.equity_fraction.excluded_account_ids
    if (
        len(equity_included) != len(set(equity_included))
        or len(equity_excluded) != len(set(equity_excluded))
        or set(equity_included).intersection(equity_excluded)
    ):
        return (
            "equity_account_coverage_invalid",
            "equity included and excluded account ids must be unique and disjoint",
        )
    if set(equity_included) != active:
        return (
            "equity_account_coverage_invalid",
            "equity included accounts must exactly match included active envelope accounts",
        )
    referenced = {lot.account_id for position in snapshot.positions for lot in position.accounts}
    if not referenced.issubset(included):
        return (
            "position_account_not_in_snapshot",
            "position lot references an account outside envelope coverage",
        )
    if not referenced.issubset(active):
        return (
            "position_account_inactive_or_excluded",
            "position lot references an inactive or excluded account",
        )
    return None


class _SnapshotReader:
    def __init__(self, snapshot: ImmutableTrackerSnapshot) -> None:
        self._snapshot = snapshot

    def probe_v1(self) -> V1Fetch[HealthV1]:
        return V1Fetch(available=True, endpoint="snapshot:health", data=self._snapshot.health)

    def get_portfolio_snapshot(self) -> V1Fetch[PortfolioSnapshotV1]:
        return V1Fetch(
            available=True,
            endpoint="snapshot:portfolio-snapshot",
            data=self._snapshot.portfolio_snapshot,
        )


def _unavailable(
    code: str,
    detail: str | None,
    *,
    provenance: PositionProvenance | None = None,
) -> PortfolioPositionResult:
    return PortfolioPositionResult(
        state="source_unavailable", provenance=provenance, error_code=code, error_detail=detail
    )


__all__ = [
    "ImmutableTrackerSnapshot",
    "PortfolioPositionAccount",
    "PortfolioPositionAdapter",
    "PortfolioPositionResult",
    "PositionProvenance",
    "SnapshotDecision",
    "SnapshotTransaction",
    "resolve_configured_position",
    "supported_schema_major",
    "validate_equity_evidence",
    "validate_positions_snapshot",
    "validate_snapshot_account_coverage",
]
