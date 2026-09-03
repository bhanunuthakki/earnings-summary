"""execution/simulate_trade_impact.py
-----------------------------------
Single-purpose CLI computing before/after portfolio simulation for proposed
trades, weight shifts, cash allocation delta, and factor drift.

Usage:
    python execution/simulate_trade_impact.py --ticker NU --action buy --shares 100 --target-weight 0.05
"""

from __future__ import annotations

import argparse
import json
import math
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

SCRIPT_DIR = Path(__file__).parent.resolve()
PROJECT_ROOT = SCRIPT_DIR.parent
SRC_DIR = PROJECT_ROOT / "src"
sys.path.insert(0, str(SRC_DIR))
sys.path.insert(0, str(SCRIPT_DIR))

from db_paths import configured_db_path  # noqa: E402
from dcf.latest import latest_dcf_row  # noqa: E402
from dcf.valuation import over_under_pct  # noqa: E402
from portfolio_weights import (  # noqa: E402
    read_materialized_weight_snapshot,
)
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite  # noqa: E402
from ticker_validation import safe_ticker  # noqa: E402


class SimulationDataUnavailableError(RuntimeError):
    """An economic input has no attributable source."""


class SimulateTradeRequest(BaseModel):
    model_config = ConfigDict(frozen=True, allow_inf_nan=False)

    ticker: str
    action: Literal["buy", "sell", "trim", "add"] = "add"
    shares: float = Field(gt=0)
    target_weight: float = Field(ge=0, le=1)
    estimated_price: float | None = Field(default=None, gt=0)
    price_currency: Literal["USD"] | None = None
    price_as_of: datetime | None = None

    @model_validator(mode="after")
    def require_explicit_price_provenance(self) -> SimulateTradeRequest:
        if self.estimated_price is not None and (
            self.price_currency is None or self.price_as_of is None
        ):
            raise ValueError("explicit price requires price_currency and price_as_of")
        if self.estimated_price is None and (
            self.price_currency is not None or self.price_as_of is not None
        ):
            raise ValueError("price provenance requires estimated_price")
        if self.price_as_of is not None and self.price_as_of.utcoffset() is None:
            raise ValueError("price_as_of requires a timezone offset")
        return self


class SimulateTradeResponse(BaseModel):
    model_config = ConfigDict(frozen=True, allow_inf_nan=False)

    ticker: str
    action: str
    current_weight: float
    projected_weight: float
    weight_delta: float
    estimated_capital_delta: float
    fair_value: float | None = None
    fair_value_gap_pct: float | None = None
    cash_impact_usd: float
    weights_as_of: datetime
    price_source: Literal["request", "dcf"]
    price_source_ref: str
    price_currency: Literal["USD"]
    price_as_of: datetime
    risk_summary: str = (
        "Factor-risk impact is not modeled; target weight is supplied independently "
        "from the share-based cash delta"
    )
    as_of: datetime = Field(default_factory=lambda: datetime.now(UTC))


def simulate_trade_impact(repo_root: Path, request: SimulateTradeRequest) -> SimulateTradeResponse:
    ticker = safe_ticker(request.ticker)
    db_path = configured_db_path(repo_root)

    weight_snapshot = read_materialized_weight_snapshot(repo_root)
    if weight_snapshot is None:
        raise SimulationDataUnavailableError("portfolio_weights_unavailable")
    current_weight = weight_snapshot.weights.get(ticker, 0.0)

    current_price = request.estimated_price
    price_source: Literal["request", "dcf"] = "request"
    price_source_ref = "request"
    price_as_of = request.price_as_of.astimezone(UTC) if request.price_as_of is not None else None
    fair_value: float | None = None
    fair_value_gap_pct: float | None = None

    if db_path.exists():
        conn: sqlite3.Connection | None = None
        try:
            conn = connect_sqlite(db_path, role=SQLiteConnectionRole.READ_ONLY)
            dcf = latest_dcf_row(conn, ticker)
            if dcf:
                is_usd = dcf.currency is not None and dcf.currency.upper() == "USD"
                if (
                    is_usd
                    and dcf.npv_per_share is not None
                    and math.isfinite(dcf.npv_per_share)
                    and dcf.npv_per_share > 0
                ):
                    fair_value = dcf.npv_per_share
                if current_price is None and is_usd and dcf.live_price_at is not None:
                    try:
                        observed_at = datetime.fromisoformat(
                            dcf.live_price_at.replace("Z", "+00:00")
                        )
                    except ValueError:
                        observed_at = None
                    if (
                        observed_at is not None
                        and dcf.live_price is not None
                        and math.isfinite(dcf.live_price)
                        and dcf.live_price > 0
                    ):
                        current_price = dcf.live_price
                        price_source = "dcf"
                        price_source_ref = f"dcf_runs:{dcf.id}"
                        price_as_of = (
                            observed_at.replace(tzinfo=UTC)
                            if observed_at.tzinfo is None
                            else observed_at.astimezone(UTC)
                        )
        except sqlite3.Error:
            pass
        finally:
            if conn is not None:
                conn.close()

    if current_price is None or price_as_of is None:
        raise SimulationDataUnavailableError("price_unavailable")
    if fair_value is not None and fair_value > 0:
        fair_value_gap_pct = over_under_pct(current_price, fair_value)

    projected_weight = request.target_weight
    if request.action in {"buy", "add"} and projected_weight < current_weight:
        raise ValueError(f"{request.action} cannot reduce portfolio weight")
    if request.action in {"sell", "trim"} and projected_weight > current_weight:
        raise ValueError(f"{request.action} cannot increase portfolio weight")
    weight_delta = projected_weight - current_weight

    capital_delta = (
        request.shares * current_price
        if request.action in {"buy", "add"}
        else -(request.shares * current_price)
    )

    return SimulateTradeResponse(
        ticker=ticker,
        action=request.action,
        current_weight=current_weight,
        projected_weight=projected_weight,
        weight_delta=weight_delta,
        estimated_capital_delta=capital_delta,
        fair_value=fair_value,
        fair_value_gap_pct=fair_value_gap_pct,
        cash_impact_usd=-capital_delta,
        weights_as_of=weight_snapshot.computed_at,
        price_source=price_source,
        price_source_ref=price_source_ref,
        price_currency="USD",
        price_as_of=price_as_of,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ticker", required=True, help="Stock ticker (e.g. NU)")
    parser.add_argument("--action", choices=["buy", "sell", "trim", "add"], default="add")
    parser.add_argument("--shares", type=float, required=True)
    parser.add_argument("--target-weight", type=float, required=True)
    parser.add_argument("--price", type=float, default=None)
    parser.add_argument("--price-currency", choices=["USD"], default=None)
    parser.add_argument("--price-as-of", default=None)
    parser.add_argument("--repo-root", type=Path, default=PROJECT_ROOT)
    args = parser.parse_args()

    try:
        req = SimulateTradeRequest(
            ticker=args.ticker,
            action=args.action,
            shares=args.shares,
            target_weight=args.target_weight,
            estimated_price=args.price,
            price_currency=args.price_currency,
            price_as_of=args.price_as_of,
        )
        res = simulate_trade_impact(args.repo_root.resolve(), req)
    except (SimulationDataUnavailableError, ValidationError, ValueError) as exc:
        print(
            json.dumps({"event": "simulation_unavailable", "reason": str(exc)}),
            file=sys.stderr,
        )
        return 2
    print(res.model_dump_json(indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
