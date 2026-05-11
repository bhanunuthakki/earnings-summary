"""Batch baseline DCF across the portfolio + watchlist.

For each ticker, derive a default 10-year DCF profile from existing kpi_facts:
  - Starting revenue growth = trailing 4-quarter avg Revenue YoY (USD), capped
    to a reasonable band [0%, 50%] to avoid extreme inputs from base-effect
    spikes (e.g., a small ticker's 200% YoY).
  - Growth decays linearly to `terminal_growth` over the 10-year horizon.
  - FCF margin = trailing 4-quarter avg Net Income Margin (GAAP), capped to
    [0.05, 0.50] (most tech/large-caps fall in here; others get the cap).
  - WACC = 0.09, terminal_growth = 0.025 — config-tunable.

This produces a comparable baseline across names. Bespoke per-name profiles
are authored in `dcf/<TICKER>.xlsx` workbooks and ingested via
`execution/refresh_dcf.py` for serious valuation work; this batch path just
gives a starting point in dcf_runs for every name.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from decimal import Decimal

from compute.dcf import run_dcf_for_ticker


@dataclass(frozen=True)
class DefaultProfile:
    """Per-ticker derived inputs. None = couldn't compute, ticker is skipped."""

    ticker: str
    base_growth: float | None
    fcf_margin: float | None
    notes: str


_HORIZON_YEARS = 10
_GROWTH_FLOOR = 0.0
_GROWTH_CEILING = 0.50
_FCF_MARGIN_FLOOR = 0.05
_FCF_MARGIN_CEILING = 0.50


def _trailing_avg_kpi(
    conn: sqlite3.Connection, *, ticker: str, kpi_name: str, n_periods: int = 4
) -> Decimal | None:
    """Average the most-recent n quarterly observations of `kpi_name` for ticker."""
    cur = conn.execute(
        "SELECT kf.value FROM kpi_facts kf "
        "JOIN kpi_definitions kd ON kd.id = kf.kpi_definition_id "
        "WHERE kf.ticker = ? AND kd.name = ? "
        "ORDER BY kf.period_end DESC LIMIT ?",
        (ticker.upper(), kpi_name, n_periods),
    )
    rows = cur.fetchall()
    if not rows:
        return None
    values = [Decimal(str(r["value"])) for r in rows]
    return sum(values) / Decimal(len(values))


def _clamp(value: Decimal, *, lo: float, hi: float) -> float:
    """Bound the value to [lo, hi]."""
    v = float(value)
    return max(lo, min(hi, v))


def derive_default_profile(
    conn: sqlite3.Connection, ticker: str
) -> DefaultProfile:
    """Compute a default DCF profile for ticker from kpi_facts. Returns notes if any input is missing."""
    growth_pct = _trailing_avg_kpi(conn, ticker=ticker, kpi_name="Revenue YoY Growth (USD)")
    margin_pct = _trailing_avg_kpi(conn, ticker=ticker, kpi_name="Net Income Margin (GAAP)")

    if growth_pct is None and margin_pct is None:
        return DefaultProfile(
            ticker=ticker.upper(), base_growth=None, fcf_margin=None,
            notes="no derived KPI data",
        )
    if growth_pct is None:
        return DefaultProfile(
            ticker=ticker.upper(), base_growth=None, fcf_margin=None,
            notes="missing Revenue YoY Growth (USD)",
        )
    if margin_pct is None:
        return DefaultProfile(
            ticker=ticker.upper(), base_growth=None, fcf_margin=None,
            notes="missing Net Income Margin (GAAP)",
        )

    growth = _clamp(growth_pct / Decimal(100), lo=_GROWTH_FLOOR, hi=_GROWTH_CEILING)
    margin = _clamp(margin_pct / Decimal(100), lo=_FCF_MARGIN_FLOOR, hi=_FCF_MARGIN_CEILING)

    return DefaultProfile(
        ticker=ticker.upper(), base_growth=growth, fcf_margin=margin,
        notes=(
            f"baseline DCF; growth from trailing 4Q Revenue YoY USD "
            f"(raw avg={growth_pct:.2f}%, clamped={growth*100:.2f}%); "
            f"fcf_margin proxied from Net Income Margin "
            f"(raw avg={margin_pct:.2f}%, clamped={margin*100:.2f}%)"
        ),
    )


def linear_decay_to_terminal(
    base_growth: float, terminal_growth: float, *, horizon_years: int = _HORIZON_YEARS
) -> list[float]:
    """Linearly decay base_growth to terminal_growth over horizon_years."""
    if horizon_years <= 0:
        return []
    if horizon_years == 1:
        return [base_growth]
    step = (base_growth - terminal_growth) / (horizon_years - 1)
    return [base_growth - step * i for i in range(horizon_years)]


@dataclass(frozen=True)
class BatchDcfResult:
    """One ticker's batch outcome."""

    ticker: str
    dcf_run_id: int | None
    base_revenue_billions: float | None
    npv_billions: float | None
    npv_per_share: float | None
    skipped_reason: str | None


def run_batch_dcf_for_ticker(
    conn: sqlite3.Connection,
    *,
    ticker: str,
    wacc: float = 0.09,
    terminal_growth: float = 0.025,
    run_id: str | None = None,
) -> BatchDcfResult:
    """End-to-end batch DCF for one ticker. Skips when inputs are missing."""
    profile = derive_default_profile(conn, ticker)
    if profile.base_growth is None or profile.fcf_margin is None:
        return BatchDcfResult(
            ticker=ticker.upper(), dcf_run_id=None,
            base_revenue_billions=None, npv_billions=None, npv_per_share=None,
            skipped_reason=profile.notes,
        )
    growths = linear_decay_to_terminal(
        profile.base_growth, terminal_growth, horizon_years=_HORIZON_YEARS
    )
    inputs, result, row_id = run_dcf_for_ticker(
        conn, ticker=ticker, revenue_growths=growths,
        fcf_margin=profile.fcf_margin, wacc=wacc, terminal_growth=terminal_growth,
        notes=profile.notes, run_id=run_id,
    )
    return BatchDcfResult(
        ticker=ticker.upper(), dcf_run_id=row_id,
        base_revenue_billions=float(inputs.base_revenue) / 1e9,
        npv_billions=float(result.npv) / 1e9,
        npv_per_share=(
            float(result.npv_per_share) if result.npv_per_share is not None else None
        ),
        skipped_reason=None,
    )
