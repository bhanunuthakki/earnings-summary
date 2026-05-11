"""PV / per-share fair-value math.

Inputs:
  - 5y forecast FCF stream (USD millions)
  - terminal multiple (exit multiple on year-5 FCF)
  - WACC (decimal — 0.09 for 9%)
  - current diluted shares outstanding (millions)

Output:
  - PV of FCF stream
  - Terminal value (year-5 FCF × multiple, discounted to today)
  - Enterprise value = PV + PV(terminal)
  - Fair value per share = EV / current shares

Conventions:
  - Year 1 FCF discounted with t=1 (full year)
  - Terminal value occurs at year N, discounted with t=N
  - No mid-year convention adjustment
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PvResult:
    fcf_stream: list[float]  # 5 values, USD millions
    forecast_years: list[int]
    pv_fcf_stream: float  # Σ FCF_t / (1+wacc)^t, USD millions
    terminal_value_undiscounted: float  # year-N FCF × multiple, USD millions
    pv_terminal: float  # terminal discounted to today, USD millions
    enterprise_value: float  # pv_fcf_stream + pv_terminal, USD millions
    diluted_shares_M: float  # millions
    fair_value_per_share: float  # USD
    wacc: float
    terminal_multiple: float


def compute_pv_per_share(
    fcf_stream: list[float],
    forecast_years: list[int],
    terminal_multiple: float,
    wacc: float,
    diluted_shares_M: float,
) -> PvResult:
    """Run the DCF and return a fully-resolved PvResult.

    Raises ValueError on degenerate inputs (empty FCF, non-positive WACC or shares).
    """
    if not fcf_stream:
        raise ValueError("fcf_stream is empty")
    if len(forecast_years) != len(fcf_stream):
        raise ValueError(
            f"forecast_years length {len(forecast_years)} != fcf_stream length {len(fcf_stream)}"
        )
    if wacc <= 0:
        raise ValueError(f"wacc must be positive (got {wacc})")
    if diluted_shares_M <= 0:
        raise ValueError(f"diluted_shares_M must be positive (got {diluted_shares_M})")
    if terminal_multiple <= 0:
        raise ValueError(f"terminal_multiple must be positive (got {terminal_multiple})")

    n = len(fcf_stream)
    pv_fcf_stream = sum(fcf / (1.0 + wacc) ** (i + 1) for i, fcf in enumerate(fcf_stream))
    terminal_value_undiscounted = fcf_stream[-1] * terminal_multiple
    pv_terminal = terminal_value_undiscounted / (1.0 + wacc) ** n
    enterprise_value = pv_fcf_stream + pv_terminal
    fair_value_per_share = enterprise_value / diluted_shares_M

    return PvResult(
        fcf_stream=list(fcf_stream),
        forecast_years=list(forecast_years),
        pv_fcf_stream=pv_fcf_stream,
        terminal_value_undiscounted=terminal_value_undiscounted,
        pv_terminal=pv_terminal,
        enterprise_value=enterprise_value,
        diluted_shares_M=diluted_shares_M,
        fair_value_per_share=fair_value_per_share,
        wacc=wacc,
        terminal_multiple=terminal_multiple,
    )


def over_under_pct(live_price: float, fair_value_per_share: float) -> float:
    """Return (live - fair) / fair as a decimal.

    Positive = over-valued, negative = under-valued.

    Per the design's trim/sell ladder:
      over_under_pct > 0.10  → trim
      over_under_pct > 0.20  → sell
      over_under_pct < -mos_bar (per holdings JSON) → initiation candidate
    """
    if fair_value_per_share <= 0:
        raise ValueError(f"fair_value_per_share must be positive (got {fair_value_per_share})")
    return (live_price - fair_value_per_share) / fair_value_per_share
