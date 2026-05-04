"""DCF valuation — discount projected free cash flows to present value.

Two-stage Gordon Growth model:
1. Explicit projection over `horizon_years`, applying per-year revenue growth
   and a constant FCF margin to derive projected FCFs.
2. Terminal value via Gordon Growth at year (horizon + 1), discounted back.

Inputs are user-supplied (revenue growths, FCF margin, WACC, terminal growth).
Base revenue and shares-outstanding proxy come from financial_facts. Each run
writes a dcf_runs row for audit; the row's `notes` field can record any
analyst comments.

The shares-outstanding figure here uses `weighted_avg_shares_diluted` from
the most recent income statement as a proxy. That's an average over the
period, not point-in-time — adequate for first-cut valuations but not exact.
Future refinement: pull point-in-time shares from FMP profile or shares_float.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class DcfInputs:
    """All assumptions needed for one DCF run."""

    base_revenue: float
    revenue_growths: list[float]
    fcf_margin: float
    wacc: float
    terminal_growth: float
    shares_outstanding: float | None = None


@dataclass(frozen=True)
class DcfResult:
    """Outputs of one DCF run."""

    projected_fcfs: list[float]
    sum_of_discounted_fcfs: float
    terminal_value: float
    discounted_terminal: float
    npv: float
    npv_per_share: float | None


def compute_dcf(inputs: DcfInputs) -> DcfResult:
    """Two-stage DCF: explicit projection + Gordon Growth terminal."""
    if not inputs.revenue_growths:
        raise ValueError("revenue_growths must be non-empty")
    if inputs.wacc <= inputs.terminal_growth:
        raise ValueError(
            f"wacc ({inputs.wacc}) must exceed terminal_growth "
            f"({inputs.terminal_growth}) for Gordon Growth"
        )
    if inputs.fcf_margin < -1 or inputs.fcf_margin > 1:
        raise ValueError(f"fcf_margin {inputs.fcf_margin} outside [-1, 1]")

    rev = inputs.base_revenue
    projected_fcfs: list[float] = []
    sum_dcf = 0.0
    for i, growth in enumerate(inputs.revenue_growths, start=1):
        rev = rev * (1.0 + growth)
        fcf = rev * inputs.fcf_margin
        projected_fcfs.append(fcf)
        sum_dcf += fcf / (1.0 + inputs.wacc) ** i

    terminal_fcf = projected_fcfs[-1] * (1.0 + inputs.terminal_growth)
    terminal_value = terminal_fcf / (inputs.wacc - inputs.terminal_growth)
    discounted_terminal = terminal_value / (1.0 + inputs.wacc) ** len(inputs.revenue_growths)

    npv = sum_dcf + discounted_terminal
    npv_per_share = (
        npv / inputs.shares_outstanding
        if inputs.shares_outstanding and inputs.shares_outstanding > 0
        else None
    )

    return DcfResult(
        projected_fcfs=projected_fcfs,
        sum_of_discounted_fcfs=sum_dcf,
        terminal_value=terminal_value,
        discounted_terminal=discounted_terminal,
        npv=npv,
        npv_per_share=npv_per_share,
    )


def _latest_fact(conn: sqlite3.Connection, ticker: str, line_item: str) -> float | None:
    """Return the latest annual financial_facts.value for (ticker, line_item)."""
    cur = conn.execute(
        "SELECT value FROM financial_facts "
        "WHERE ticker = ? AND line_item = ? AND fiscal_period_type = 'FY' "
        "ORDER BY period_end DESC LIMIT 1",
        (ticker.upper(), line_item),
    )
    row = cur.fetchone()
    return float(row[0]) if row else None


def fetch_dcf_base_inputs(
    conn: sqlite3.Connection, ticker: str
) -> tuple[float | None, float | None]:
    """Read latest annual revenue and weighted_avg_shares_diluted for ticker."""
    revenue = _latest_fact(conn, ticker, "revenue")
    shares = _latest_fact(conn, ticker, "weighted_avg_shares_diluted")
    return revenue, shares


def persist_dcf_run(
    conn: sqlite3.Connection,
    ticker: str,
    inputs: DcfInputs,
    result: DcfResult,
    notes: str | None = None,
    run_id: str | None = None,
) -> int:
    """Insert one dcf_runs row; return the new row id."""
    cur = conn.execute(
        "INSERT INTO dcf_runs ("
        "ticker, valuation_date, horizon_years, base_revenue, "
        "revenue_growths_json, fcf_margin, wacc, terminal_growth, "
        "npv, npv_per_share, shares_outstanding, currency, notes, run_id"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            ticker.upper(),
            datetime.now().date().isoformat(),
            len(inputs.revenue_growths),
            str(inputs.base_revenue),
            json.dumps(inputs.revenue_growths),
            inputs.fcf_margin,
            inputs.wacc,
            inputs.terminal_growth,
            str(result.npv),
            str(result.npv_per_share) if result.npv_per_share is not None else None,
            str(inputs.shares_outstanding) if inputs.shares_outstanding is not None else None,
            None,
            notes,
            run_id,
        ),
    )
    conn.commit()
    return cur.lastrowid or 0


def run_dcf_for_ticker(
    conn: sqlite3.Connection,
    ticker: str,
    revenue_growths: list[float],
    fcf_margin: float,
    wacc: float,
    terminal_growth: float,
    notes: str | None = None,
    run_id: str | None = None,
) -> tuple[DcfInputs, DcfResult, int]:
    """End-to-end: fetch base inputs, compute DCF, persist. Returns (inputs, result, row_id)."""
    base_revenue, shares = fetch_dcf_base_inputs(conn, ticker)
    if base_revenue is None:
        raise ValueError(f"No FY revenue facts found for {ticker!r}")

    inputs = DcfInputs(
        base_revenue=base_revenue,
        revenue_growths=revenue_growths,
        fcf_margin=fcf_margin,
        wacc=wacc,
        terminal_growth=terminal_growth,
        shares_outstanding=shares,
    )
    result = compute_dcf(inputs)
    row_id = persist_dcf_run(conn, ticker, inputs, result, notes=notes, run_id=run_id)
    return (inputs, result, row_id)
