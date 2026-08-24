"""Read-only, governed last-known portfolio snapshot adapter.

The Work OS may show a locally configured immutable tracker snapshot only after
the live tracker has failed.  This module deliberately has no discovery or
writer path: an operator must configure the exact artifact through
``PORTFOLIO_TRACKER_SNAPSHOT_PATH`` and malformed, partial, or unreconciled
evidence is rejected without exposing filesystem or transport diagnostics.
"""

from __future__ import annotations

import os
from hashlib import sha256
from pathlib import Path

from pydantic import BaseModel, ConfigDict, ValidationError

from integrations.portfolio_position import (
    ImmutableTrackerSnapshot,
    supported_schema_major,
    validate_equity_evidence,
    validate_positions_snapshot,
    validate_snapshot_account_coverage,
)
from integrations.portfolio_tracker_client import LivePortfolio, LivePosition
from integrations.portfolio_tracker_v1 import PositionsV1Result

_MAX_SNAPSHOT_BYTES = 1_048_576


class OfflinePortfolioSnapshot(BaseModel):
    """A validated local portfolio read with content-addressed provenance."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    source_identity: str
    as_of: str
    portfolio: LivePortfolio


def read_configured_offline_portfolio_snapshot(
    snapshot_path: Path | None = None,
) -> OfflinePortfolioSnapshot | None:
    """Return a complete, configured local snapshot or ``None``.

    Absence, I/O failure, malformed JSON, partial evidence, and every
    reconciliation failure intentionally share the same fail-closed result.
    Callers receive no path, account, or exception text to render.
    """

    configured = snapshot_path or _configured_snapshot_path()
    if configured is None:
        return None
    try:
        payload = _read_bounded(configured)
        snapshot = ImmutableTrackerSnapshot.model_validate_json(payload)
    except (OSError, ValidationError, ValueError):
        return None
    return _validated_snapshot(snapshot, payload)


def _configured_snapshot_path() -> Path | None:
    raw_path = os.environ.get("PORTFOLIO_TRACKER_SNAPSHOT_PATH")
    return Path(raw_path) if raw_path else None


def _read_bounded(path: Path) -> bytes:
    with path.open("rb") as handle:
        payload = handle.read(_MAX_SNAPSHOT_BYTES + 1)
    if len(payload) > _MAX_SNAPSHOT_BYTES:
        raise ValueError("snapshot exceeds adapter bound")
    return payload


def _validated_snapshot(
    snapshot: ImmutableTrackerSnapshot,
    payload: bytes,
) -> OfflinePortfolioSnapshot | None:
    """Adapt only evidence that still reconciles as one complete snapshot."""

    health = snapshot.health
    portfolio_snapshot = snapshot.portfolio_snapshot
    meta = portfolio_snapshot.meta
    if (
        not snapshot.source_identity.strip()
        or health.status != "ok"
        or not health.database_ok
        or health.active_account_count < 1
        or health.is_stale
        or meta.is_partial
        or portfolio_snapshot.equity_fraction.is_partial
        or not supported_schema_major(meta.schema_version)
        or not meta.currency.strip()
        or meta.as_of is None
        or health.latest_snapshot_date != meta.as_of
    ):
        return None
    if any(
        account.value_currency.strip() != meta.currency.strip()
        for account in portfolio_snapshot.accounts
    ):
        return None
    if validate_equity_evidence(portfolio_snapshot) is not None:
        return None
    if validate_snapshot_account_coverage(portfolio_snapshot, health) is not None:
        return None
    positions = PositionsV1Result(
        snapshot_date=meta.as_of,
        total_market_value=portfolio_snapshot.total_market_value,
        positions=portfolio_snapshot.positions,
        by_tax_treatment=portfolio_snapshot.by_tax_treatment,
        notes=[],
    )
    if validate_positions_snapshot(positions) is not None:
        return None
    tickers = [
        position.ticker.strip().upper() for position in positions.positions if position.ticker
    ]
    if len(tickers) != len(set(tickers)):
        return None
    positions_live = [
        LivePosition(
            ticker=position.ticker.strip().upper() if position.ticker else None,
            name=position.name,
            quantity=float(position.quantity),
            market_value=float(position.market_value)
            if position.market_value is not None
            else None,
            cost_basis=float(position.cost_basis) if position.cost_basis is not None else None,
            unrealized_pnl=(
                float(position.unrealized_pnl) if position.unrealized_pnl is not None else None
            ),
            percent_of_portfolio=(
                float(position.percent_of_portfolio)
                if position.percent_of_portfolio is not None
                else None
            ),
        )
        for position in positions.positions
    ]
    source_identity = f"{snapshot.source_identity}:sha256:{sha256(payload).hexdigest()}"
    return OfflinePortfolioSnapshot(
        source_identity=source_identity,
        as_of=meta.as_of.isoformat(),
        portfolio=LivePortfolio(
            available=True,
            api_url="snapshot://governed-local",
            total_market_value=float(positions.total_market_value),
            positions=positions_live,
            as_of=meta.as_of.isoformat(),
            envelope_warnings=["portfolio_offline_snapshot"],
        ),
    )


__all__ = ["OfflinePortfolioSnapshot", "read_configured_offline_portfolio_snapshot"]
