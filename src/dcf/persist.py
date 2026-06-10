"""Upsert a DCF run into the `dcf_runs` table.

The table predates the new DCF subsystem (migration 0013) so we coexist
with the legacy columns: only the fields that map to the new flow are
populated. Legacy columns (base_revenue, revenue_growths_json, fcf_margin,
breakdown_json, segment_name) stay NULL on a Phase 3 write.

UNIQUE(ticker) is enforced (migration 0018) — we use INSERT OR REPLACE
to upsert.

Audit columns from migration 0024 (live_price, live_price_at,
over_under_pct, mos_bar_used, assumption_snapshot_json) are populated;
they're the whole point of this write.

over_under_pct is NOT a caller-supplied field: it is derived here, at the
single write chokepoint, from live_price + npv_per_share (see
derive_over_under). Four bespoke builders once hand-rolled it as percent
UPSIDE — wrong sign and scale, fixed in #368 — so writers no longer get to
supply their own value, and migration 0076 adds a DB CHECK enforcing the
same self-consistency on any raw write.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime

from dcf import valuation


@dataclass(frozen=True)
class DcfRunRow:
    """Fields the Phase 3 refresh writes to dcf_runs.

    over_under_pct is deliberately absent — upsert() derives it from
    live_price + npv_per_share so no caller can persist a value that
    violates the documented ratio convention.
    """

    ticker: str
    valuation_date: date
    horizon_years: int
    wacc: float
    npv: float  # enterprise value, USD millions
    npv_per_share: float  # USD
    shares_outstanding: float  # absolute count, not millions
    currency: str
    live_price: float | None
    live_price_at: datetime | None
    mos_bar_used: float | None
    assumption_snapshot_json: str
    notes: str | None = None
    run_id: str | None = None


def derive_over_under(live_price: float | None, npv_per_share: float) -> float | None:
    """(live - fair) / fair as a decimal ratio (the migration-0024 convention),
    or None when undefined: no live price, or a non-positive fair value (the
    #291 guard). The only producer of dcf_runs.over_under_pct values."""
    if live_price is None or live_price <= 0 or npv_per_share <= 0:
        return None
    return valuation.over_under_pct(live_price, npv_per_share)


def upsert(conn: sqlite3.Connection, row: DcfRunRow) -> None:
    """INSERT-OR-REPLACE the dcf_runs row keyed by ticker."""
    conn.execute(
        """
        INSERT OR REPLACE INTO dcf_runs (
            ticker, valuation_date, horizon_years,
            wacc, terminal_growth,
            npv, npv_per_share, shares_outstanding,
            currency, notes, run_id,
            live_price, live_price_at, over_under_pct,
            mos_bar_used, assumption_snapshot_json,
            revenue_growths_json, fcf_margin
        ) VALUES (
            :ticker, :valuation_date, :horizon_years,
            :wacc, 0,
            :npv, :npv_per_share, :shares_outstanding,
            :currency, :notes, :run_id,
            :live_price, :live_price_at, :over_under_pct,
            :mos_bar_used, :assumption_snapshot_json,
            '[]', 0
        )
        """,
        {
            "ticker": row.ticker.upper(),
            "valuation_date": row.valuation_date.isoformat(),
            "horizon_years": row.horizon_years,
            "wacc": row.wacc,
            "npv": row.npv,
            "npv_per_share": row.npv_per_share,
            "shares_outstanding": row.shares_outstanding,
            "currency": row.currency,
            "notes": row.notes,
            "run_id": row.run_id,
            "live_price": row.live_price,
            "live_price_at": row.live_price_at.isoformat() if row.live_price_at else None,
            "over_under_pct": derive_over_under(row.live_price, row.npv_per_share),
            "mos_bar_used": row.mos_bar_used,
            "assumption_snapshot_json": row.assumption_snapshot_json,
        },
    )
    conn.commit()


def build_assumption_snapshot(
    fcf_stream: list[float],
    forecast_years: list[int],
    wacc: float,
    terminal_multiple: float,
    diluted_shares_M: float,
    workbook_path: str,
    pv_fcf_stream: float,
    pv_terminal: float,
) -> str:
    """Serialize the inputs that fed the PV calc into a JSON string.

    Stored verbatim in dcf_runs.assumption_snapshot_json so successive
    refreshes can be diffed to see what changed between quarters.
    """
    payload = {
        "workbook_path": workbook_path,
        "wacc": wacc,
        "terminal_multiple": terminal_multiple,
        "diluted_shares_M": diluted_shares_M,
        "forecast_years": forecast_years,
        "fcf_stream_M": fcf_stream,
        "pv_fcf_stream_M": pv_fcf_stream,
        "pv_terminal_M": pv_terminal,
    }
    return json.dumps(payload, indent=2)
