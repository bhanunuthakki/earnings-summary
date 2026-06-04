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


# --------------------------------------------------------------------------- #
# Damodaran-style valuation: FCFF -> operating value -> equity bridge -> /share,
# with a company-specific EV exit multiple and an implied price-multiple readout.
# --------------------------------------------------------------------------- #
# Exit-multiple bases. EV-level (pair with the FCFF DCF's enterprise value); the
# basis selects which terminal-year line item the multiple applies to.
TERMINAL_BASES: tuple[str, ...] = ("EV/EBITDA", "EV/Sales", "EV/EBIT", "EV/FCF")
_BASIS_METRIC: dict[str, str] = {
    "EV/EBITDA": "ebitda",
    "EV/Sales": "revenue",
    "EV/EBIT": "ebit",
    "EV/FCF": "fcf",
}


@dataclass(frozen=True)
class TerminalMetrics:
    """Terminal-year line items (USD millions) the exit multiple can apply to.

    `net_income` is the earnings proxy for the implied-P/E readout (NOPAT when a
    full interest-bearing net income isn't modeled).
    """

    revenue: float
    ebit: float
    ebitda: float
    fcf: float
    net_income: float


@dataclass(frozen=True)
class DamodaranValuation:
    """The full DCF walk: PV(FCFF) + terminal EV -> operating value -> equity
    bridge -> value per share, plus the implied price multiples at that value."""

    basis: str  # one of TERMINAL_BASES
    terminal_multiple: float
    terminal_metric_value: float  # the terminal-year metric the multiple applied to
    pv_fcff: float  # USD millions
    terminal_ev: float  # terminal_metric_value * terminal_multiple, undiscounted
    pv_terminal: float
    operating_value: float  # pv_fcff + pv_terminal
    pv_terminal_pct: float  # pv_terminal / operating_value (Damodaran's terminal-weight check)
    cash_and_nonop: float
    total_debt: float
    equity_value: float  # operating_value + cash_and_nonop - total_debt
    diluted_shares_M: float
    value_per_share: float  # USD
    implied_pe: float | None  # equity_value / terminal net income (the price-multiple readout)
    implied_ps: float | None
    implied_pfcf: float | None
    wacc: float


def compute_valuation(
    fcff_stream: list[float],
    forecast_years: list[int],
    wacc: float,
    *,
    basis: str,
    terminal_multiple: float,
    terminal: TerminalMetrics,
    cash_and_nonop: float,
    total_debt: float,
    diluted_shares_M: float,
) -> DamodaranValuation:
    """Discount the FCFF stream + an EV-multiple terminal value to enterprise
    value, bridge to equity (+ cash & non-operating assets - debt), and divide by
    shares for value per share. Reports the implied price multiples at that value.

    All flow/stock inputs are USD millions; shares are millions; the result's
    value_per_share is USD. Raises ValueError on degenerate inputs.
    """
    if not fcff_stream:
        raise ValueError("fcff_stream is empty")
    if len(forecast_years) != len(fcff_stream):
        raise ValueError(
            f"forecast_years length {len(forecast_years)} != fcff_stream length {len(fcff_stream)}"
        )
    if wacc <= 0:
        raise ValueError(f"wacc must be positive (got {wacc})")
    if diluted_shares_M <= 0:
        raise ValueError(f"diluted_shares_M must be positive (got {diluted_shares_M})")
    if terminal_multiple <= 0:
        raise ValueError(f"terminal_multiple must be positive (got {terminal_multiple})")
    if basis not in _BASIS_METRIC:
        raise ValueError(f"unknown terminal basis {basis!r}; valid: {TERMINAL_BASES}")

    n = len(fcff_stream)
    pv_fcff = sum(f / (1.0 + wacc) ** (i + 1) for i, f in enumerate(fcff_stream))
    metric_value = {
        "ebitda": terminal.ebitda,
        "revenue": terminal.revenue,
        "ebit": terminal.ebit,
        "fcf": terminal.fcf,
    }[_BASIS_METRIC[basis]]
    terminal_ev = metric_value * terminal_multiple
    pv_terminal = terminal_ev / (1.0 + wacc) ** n
    operating_value = pv_fcff + pv_terminal
    equity_value = operating_value + cash_and_nonop - total_debt
    value_per_share = equity_value / diluted_shares_M

    def _implied(denom: float) -> float | None:
        return equity_value / denom if denom > 0 else None

    return DamodaranValuation(
        basis=basis,
        terminal_multiple=terminal_multiple,
        terminal_metric_value=metric_value,
        pv_fcff=pv_fcff,
        terminal_ev=terminal_ev,
        pv_terminal=pv_terminal,
        operating_value=operating_value,
        pv_terminal_pct=(pv_terminal / operating_value if operating_value else 0.0),
        cash_and_nonop=cash_and_nonop,
        total_debt=total_debt,
        equity_value=equity_value,
        diluted_shares_M=diluted_shares_M,
        value_per_share=value_per_share,
        implied_pe=_implied(terminal.net_income),
        implied_ps=_implied(terminal.revenue),
        implied_pfcf=_implied(terminal.fcf),
        wacc=wacc,
    )
