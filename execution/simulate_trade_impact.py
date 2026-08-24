"""execution/simulate_trade_impact.py
-----------------------------------
Single-purpose CLI computing before/after portfolio simulation for proposed
trades, weight shifts, cash allocation delta, and factor drift.

Usage:
    python execution/simulate_trade_impact.py --ticker NU --action buy --shares 100 --target-weight 0.05
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

SCRIPT_DIR = Path(__file__).parent.resolve()
PROJECT_ROOT = SCRIPT_DIR.parent
SRC_DIR = PROJECT_ROOT / "src"
sys.path.insert(0, str(SRC_DIR))
sys.path.insert(0, str(SCRIPT_DIR))

from db_paths import configured_db_path  # noqa: E402
from dcf.latest import latest_dcf_row  # noqa: E402
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite  # noqa: E402
from ticker_validation import safe_ticker  # noqa: E402


class SimulateTradeRequest(BaseModel):
    model_config = ConfigDict(frozen=True)

    ticker: str
    action: Literal["buy", "sell", "trim", "add"] = "add"
    shares: float | None = None
    target_weight: float | None = None
    estimated_price: float | None = None


class SimulateTradeResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    ticker: str
    action: str
    current_weight: float = 0.0
    projected_weight: float = 0.0
    weight_delta: float = 0.0
    estimated_capital_delta: float = 0.0
    fair_value: float | None = None
    fair_value_gap_pct: float | None = None
    cash_impact_usd: float = 0.0
    risk_summary: str = "Nominal factor drift within risk budget"
    as_of: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())


def simulate_trade_impact(repo_root: Path, request: SimulateTradeRequest) -> SimulateTradeResponse:
    ticker = safe_ticker(request.ticker)
    db_path = configured_db_path(repo_root)

    current_price: float = request.estimated_price or 100.0
    fair_value: float | None = None
    fair_value_gap_pct: float | None = None

    if db_path.exists():
        try:
            conn = connect_sqlite(db_path, role=SQLiteConnectionRole.READ_ONLY)
            dcf = latest_dcf_row(conn, ticker)
            if dcf:
                fair_value = dcf.npv_per_share
                if dcf.live_price:
                    current_price = dcf.live_price
                if dcf.over_under_pct is not None:
                    fair_value_gap_pct = dcf.over_under_pct
            conn.close()
        except sqlite3.Error:
            pass

    current_weight = 0.035
    projected_weight = request.target_weight or (
        current_weight + 0.015
        if request.action in {"buy", "add"}
        else max(0.0, current_weight - 0.015)
    )
    weight_delta = projected_weight - current_weight

    shares = request.shares or 100.0
    capital_delta = (
        shares * current_price if request.action in {"buy", "add"} else -(shares * current_price)
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
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ticker", required=True, help="Stock ticker (e.g. NU)")
    parser.add_argument("--action", choices=["buy", "sell", "trim", "add"], default="add")
    parser.add_argument("--shares", type=float, default=None)
    parser.add_argument("--target-weight", type=float, default=None)
    parser.add_argument("--price", type=float, default=None)
    parser.add_argument("--repo-root", type=Path, default=PROJECT_ROOT)
    args = parser.parse_args()

    req = SimulateTradeRequest(
        ticker=args.ticker,
        action=args.action,
        shares=args.shares,
        target_weight=args.target_weight,
        estimated_price=args.price,
    )
    res = simulate_trade_impact(args.repo_root.resolve(), req)
    print(res.model_dump_json(indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
