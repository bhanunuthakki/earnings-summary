"""Forecast assumption derivation + projection math for DCF workbooks.

The workbook's Forecast sheet has two zones:

  INPUTS    — user-editable. Seeded from the ticker's TTM history; the user
              edits per-cell as desired. The refresher does NOT overwrite
              these on subsequent runs — re-seed with `force=True` to reset
              from latest FMP.

  PROJECTED — program-owned table (Year / Revenue / FCF / Op Margin / Capex
              Intensity columns). Rewritten on every refresh from the current
              INPUTS via Python (not Excel formulas, so openpyxl reads stay
              deterministic regardless of whether the user opens the workbook
              in Excel between refreshes).

The projection model decomposes FCF into operating-leverage + capex-intensity
drivers, each ramping linearly from a Y1 current-state value to a Y5
normalized value:

  revenue_t      = revenue_{t-1} × (1 + growth_t)             # growth decays Y1→terminal
  op_margin_t    = linear ramp from y1_op_margin to y5_op_margin
  capex_intens_t = linear ramp from y1_capex_intensity to y5_capex_intensity
  after_tax_op_t = revenue_t × op_margin_t × (1 - tax_rate)
  fcf_t          = after_tax_op_t - revenue_t × capex_intens_t

This replaces a prior flat-FCF-margin model, which produced negative terminal
FCFs (and a negative terminal value, blowing the DCF) for tickers in a heavy
capex cycle (AMZN's AI buildout) or with structurally lumpy FCF (BN). The
decomposition lets the user see WHY the FCF moves — margin compression vs.
capex elevation — and normalize each driver independently.
"""

from __future__ import annotations

from dataclasses import dataclass

from openpyxl.styles import Font, PatternFill
from openpyxl.worksheet.worksheet import Worksheet

DEFAULT_FORECAST_YEARS = 5
DEFAULT_TERMINAL_GROWTH = 0.025
# Normalization defaults — applied when TTM-derived Y1 is unprofitable or
# capex-elevated. Mature-business steady-state shapes; user overrides per
# ticker as needed.
_Y5_OP_MARGIN_FLOOR = 0.15  # deeply-unprofitable Y1s still land at a viable terminal year
_Y5_OP_MARGIN_LIFT = 0.02  # ~200bps of operating leverage over 5y for growing names
_Y5_CAPEX_INTENSITY_DEFAULT = 0.06  # mature steady-state capex / revenue
_Y1_OP_MARGIN_CLAMP = (-0.50, 0.60)
_TAX_RATE_CLAMP = (0.15, 0.35)
_DEFAULT_TAX_RATE = 0.25
_DEFAULT_Y1_OP_MARGIN_FALLBACK = 0.10  # used when income data is missing
_DEFAULT_Y1_CAPEX_INTENSITY_FALLBACK = 0.05  # used when cashflow data is missing

_TTM_QUARTERS = 4
_MAX_SCAN_ROW = 50
_MILLIONS = 1_000_000.0

# Single source of truth for the INPUTS layout — every read and write path
# pulls labels and target rows from here.
_INPUT_LABEL_BASE_REVENUE = "Base Revenue (TTM, $M)"
_INPUT_LABEL_Y1_GROWTH = "Y1 Revenue Growth %"
_INPUT_LABEL_TERMINAL_GROWTH = "Terminal Revenue Growth %"
_INPUT_LABEL_Y1_OP_MARGIN = "Y1 Operating Margin %"
_INPUT_LABEL_Y5_OP_MARGIN = "Y5 Operating Margin %"
_INPUT_LABEL_Y1_CAPEX_INTENSITY = "Y1 Capex % of Revenue"
_INPUT_LABEL_Y5_CAPEX_INTENSITY = "Y5 Capex % of Revenue"
_INPUT_LABEL_TAX_RATE = "Tax Rate %"
_INPUT_LABEL_DILUTED_SHARES = "Diluted Shares (M)"
_INPUT_LABEL_FORECAST_YEARS = "Forecast Years"

# Accepted label variants for label-scan reads. Lowercased + stripped.
# Listed widest-to-tightest so even a user who shortens a label still parses.
_LABEL_ALIASES: dict[str, tuple[str, ...]] = {
    "base_revenue": ("base revenue (ttm, $m)", "base revenue"),
    "y1_growth": ("y1 revenue growth %", "y1 growth", "year 1 growth"),
    "terminal_growth": (
        "terminal revenue growth %",
        "terminal growth %",
        "terminal growth",
    ),
    "y1_op_margin": (
        "y1 operating margin %",
        "y1 op margin %",
        "y1 operating margin",
    ),
    "y5_op_margin": (
        "y5 operating margin %",
        "y5 op margin %",
        "y5 operating margin",
    ),
    "y1_capex_intensity": (
        "y1 capex % of revenue",
        "y1 capex intensity %",
        "y1 capex intensity",
    ),
    "y5_capex_intensity": (
        "y5 capex % of revenue",
        "y5 capex intensity %",
        "y5 capex intensity",
    ),
    "tax_rate": ("tax rate %", "tax rate"),
    "diluted_shares": ("diluted shares (m)", "diluted shares"),
    "forecast_years": ("forecast years", "horizon", "forecast horizon"),
}

# Fixed row positions for the INPUTS zone (column A = label, B = value).
_INPUT_ROWS: dict[str, tuple[int, str]] = {
    "base_revenue": (3, _INPUT_LABEL_BASE_REVENUE),
    "y1_growth": (4, _INPUT_LABEL_Y1_GROWTH),
    "terminal_growth": (5, _INPUT_LABEL_TERMINAL_GROWTH),
    "y1_op_margin": (6, _INPUT_LABEL_Y1_OP_MARGIN),
    "y5_op_margin": (7, _INPUT_LABEL_Y5_OP_MARGIN),
    "y1_capex_intensity": (8, _INPUT_LABEL_Y1_CAPEX_INTENSITY),
    "y5_capex_intensity": (9, _INPUT_LABEL_Y5_CAPEX_INTENSITY),
    "tax_rate": (10, _INPUT_LABEL_TAX_RATE),
    "diluted_shares": (11, _INPUT_LABEL_DILUTED_SHARES),
    "forecast_years": (12, _INPUT_LABEL_FORECAST_YEARS),
}
# Spacer at row 13, header at 14, spacer at 15, table starts at 16.
_PROJECTED_HEADER_ROW = 14
_PROJECTED_START_ROW = 16

# Visual cue — user-editable input cells get a light yellow fill so the user
# can spot them in Excel without reading the headers.
_INPUT_FILL = PatternFill(start_color="FFF7DC", end_color="FFF7DC", fill_type="solid")
_HEADER_FONT = Font(bold=True)
_NOTE_FONT = Font(italic=True, color="666666")


@dataclass(frozen=True)
class ForecastInputs:
    """User-editable assumptions. Units: USD millions where applicable;
    growth, margins, and tax rate are decimals (0.10 == 10%)."""

    base_revenue_M: float
    y1_growth_pct: float
    terminal_growth_pct: float
    y1_operating_margin_pct: float
    y5_operating_margin_pct: float
    y1_capex_intensity_pct: float
    y5_capex_intensity_pct: float
    tax_rate_pct: float
    diluted_shares_M: float
    forecast_years: int


@dataclass(frozen=True)
class ForecastProjections:
    """Year-by-year revenue + FCF stream computed from `ForecastInputs`.

    `operating_margin_pct` and `capex_intensity_pct` expose the per-year
    ramped drivers so the workbook can show them alongside the headline
    revenue + FCF rows (and so callers can audit what's behind the FCF).
    """

    years: list[int]
    revenue_M: list[float]
    fcf_M: list[float]
    operating_margin_pct: list[float]
    capex_intensity_pct: list[float]


class ForecastError(Exception):
    """Forecast inputs are missing or malformed in the workbook."""


def derive_initial_inputs(
    income_records: list[dict[str, object]],
    cashflow_records: list[dict[str, object]],
    *,
    terminal_growth_pct: float = DEFAULT_TERMINAL_GROWTH,
    forecast_years: int = DEFAULT_FORECAST_YEARS,
) -> ForecastInputs:
    """Derive starter assumptions from the ticker's TTM history.

    Records must be sorted newest-first (FMP's native order). Y1 driver
    values come from TTM ratios; Y5 driver values apply forward-looking
    normalization (margin recovery + capex normalization) so a heavy-capex
    or unprofitable Y1 still produces a viable terminal year. Falls back
    gracefully when data is incomplete:
      - <4 quarters of revenue: use what's available, scaled to TTM-equivalent.
      - prior TTM unavailable: Y1 growth defaults to terminal_growth_pct.
      - operating income missing: Y1 op margin defaults to 10% (amber flag).
      - capex missing: Y1 capex intensity defaults to 5%.
      - pretax non-positive: tax rate defaults to 25%.
      - shares count missing: defaults to 1000 (user will obviously override).
    """
    base_revenue_actual = _ttm_sum(income_records[:_TTM_QUARTERS], "revenue")
    base_revenue_M = (
        base_revenue_actual / _MILLIONS if base_revenue_actual is not None else 0.0
    )

    prior_revenue = _ttm_sum(
        income_records[_TTM_QUARTERS : 2 * _TTM_QUARTERS], "revenue"
    )
    if base_revenue_actual is not None and prior_revenue and prior_revenue > 0:
        y1_growth_pct = base_revenue_actual / prior_revenue - 1.0
    else:
        y1_growth_pct = terminal_growth_pct

    ttm_op_income = _ttm_sum(income_records[:_TTM_QUARTERS], "operatingIncome")
    if (
        ttm_op_income is not None
        and base_revenue_actual
        and base_revenue_actual > 0
    ):
        y1_op_margin = ttm_op_income / base_revenue_actual
    else:
        y1_op_margin = _DEFAULT_Y1_OP_MARGIN_FALLBACK
    y1_op_margin = max(_Y1_OP_MARGIN_CLAMP[0], min(y1_op_margin, _Y1_OP_MARGIN_CLAMP[1]))
    y5_op_margin = max(y1_op_margin + _Y5_OP_MARGIN_LIFT, _Y5_OP_MARGIN_FLOOR)

    ttm_capex = _ttm_sum(cashflow_records[:_TTM_QUARTERS], "capitalExpenditure")
    if (
        ttm_capex is not None
        and base_revenue_actual
        and base_revenue_actual > 0
    ):
        y1_capex_intensity = abs(ttm_capex) / base_revenue_actual
    else:
        y1_capex_intensity = _DEFAULT_Y1_CAPEX_INTENSITY_FALLBACK

    ttm_pretax = _ttm_sum(income_records[:_TTM_QUARTERS], "incomeBeforeTax")
    ttm_tax = _ttm_sum(income_records[:_TTM_QUARTERS], "incomeTaxExpense")
    if ttm_pretax is not None and ttm_pretax > 0 and ttm_tax is not None:
        tax_rate = ttm_tax / ttm_pretax
    else:
        tax_rate = _DEFAULT_TAX_RATE
    tax_rate = max(_TAX_RATE_CLAMP[0], min(tax_rate, _TAX_RATE_CLAMP[1]))

    shares_actual = _latest_value(income_records, "weightedAverageShsOutDil")
    diluted_shares_M = (
        shares_actual / _MILLIONS if shares_actual is not None else 1000.0
    )

    return ForecastInputs(
        base_revenue_M=round(base_revenue_M, 2),
        y1_growth_pct=round(y1_growth_pct, 4),
        terminal_growth_pct=round(terminal_growth_pct, 4),
        y1_operating_margin_pct=round(y1_op_margin, 4),
        y5_operating_margin_pct=round(y5_op_margin, 4),
        y1_capex_intensity_pct=round(y1_capex_intensity, 4),
        y5_capex_intensity_pct=round(_Y5_CAPEX_INTENSITY_DEFAULT, 4),
        tax_rate_pct=round(tax_rate, 4),
        diluted_shares_M=round(diluted_shares_M, 2),
        forecast_years=forecast_years,
    )


def compute_projections(
    inputs: ForecastInputs, base_year: int
) -> ForecastProjections:
    """Project revenue + FCF over `forecast_years` years starting at `base_year + 1`.

    Growth decays linearly Y1→terminal across the horizon. Operating margin
    and capex intensity also ramp linearly across the horizon (Y1→Y5 values),
    decomposing FCF into operating-leverage + capex-normalization drivers:

        revenue_t       = revenue_{t-1} × (1 + growth_t)
        op_margin_t     = linear ramp
        capex_intens_t  = linear ramp
        fcf_t           = revenue_t × op_margin_t × (1 - tax_rate)
                          - revenue_t × capex_intens_t

    Horizon is clamped to [1, 10] to defend against absurd user input.
    """
    horizon = max(1, min(inputs.forecast_years, 10))
    growths = _linear_decay(
        inputs.y1_growth_pct, inputs.terminal_growth_pct, horizon
    )
    op_margins = _linear_decay(
        inputs.y1_operating_margin_pct, inputs.y5_operating_margin_pct, horizon
    )
    capex_intensities = _linear_decay(
        inputs.y1_capex_intensity_pct, inputs.y5_capex_intensity_pct, horizon
    )
    years = list(range(base_year + 1, base_year + 1 + horizon))
    revenues: list[float] = []
    fcfs: list[float] = []
    rev = inputs.base_revenue_M
    one_minus_tax = 1.0 - inputs.tax_rate_pct
    for g, om, ci in zip(growths, op_margins, capex_intensities, strict=True):
        rev = rev * (1 + g)
        revenues.append(rev)
        after_tax_op = rev * om * one_minus_tax
        capex = rev * ci
        fcfs.append(after_tax_op - capex)
    return ForecastProjections(
        years=years,
        revenue_M=revenues,
        fcf_M=fcfs,
        operating_margin_pct=op_margins,
        capex_intensity_pct=capex_intensities,
    )


def write_inputs_section(ws: Worksheet, inputs: ForecastInputs) -> None:
    """Write the INPUTS zone. Yellow-fills the user-editable cells.

    Called by the seeder. The refresher must NOT call this on subsequent
    runs — preserving user edits is the contract.
    """
    ws.cell(row=1, column=1, value="FORECAST INPUTS - edit these cells").font = (
        _HEADER_FONT
    )
    ws.cell(
        row=2,
        column=1,
        value="(yellow-filled = user-editable; refresh preserves your edits)",
    ).font = _NOTE_FONT

    rows: list[tuple[int, str, float | int]] = [
        (
            _INPUT_ROWS["base_revenue"][0],
            _INPUT_LABEL_BASE_REVENUE,
            inputs.base_revenue_M,
        ),
        (_INPUT_ROWS["y1_growth"][0], _INPUT_LABEL_Y1_GROWTH, inputs.y1_growth_pct),
        (
            _INPUT_ROWS["terminal_growth"][0],
            _INPUT_LABEL_TERMINAL_GROWTH,
            inputs.terminal_growth_pct,
        ),
        (
            _INPUT_ROWS["y1_op_margin"][0],
            _INPUT_LABEL_Y1_OP_MARGIN,
            inputs.y1_operating_margin_pct,
        ),
        (
            _INPUT_ROWS["y5_op_margin"][0],
            _INPUT_LABEL_Y5_OP_MARGIN,
            inputs.y5_operating_margin_pct,
        ),
        (
            _INPUT_ROWS["y1_capex_intensity"][0],
            _INPUT_LABEL_Y1_CAPEX_INTENSITY,
            inputs.y1_capex_intensity_pct,
        ),
        (
            _INPUT_ROWS["y5_capex_intensity"][0],
            _INPUT_LABEL_Y5_CAPEX_INTENSITY,
            inputs.y5_capex_intensity_pct,
        ),
        (_INPUT_ROWS["tax_rate"][0], _INPUT_LABEL_TAX_RATE, inputs.tax_rate_pct),
        (
            _INPUT_ROWS["diluted_shares"][0],
            _INPUT_LABEL_DILUTED_SHARES,
            inputs.diluted_shares_M,
        ),
        (
            _INPUT_ROWS["forecast_years"][0],
            _INPUT_LABEL_FORECAST_YEARS,
            inputs.forecast_years,
        ),
    ]
    for row, label, value in rows:
        ws.cell(row=row, column=1, value=label)
        c = ws.cell(row=row, column=2, value=value)
        c.fill = _INPUT_FILL
        if "%" in label:
            c.number_format = "0.00%"
        elif "$M" in label or "(M)" in label:
            c.number_format = "#,##0.00"


def write_projections_section(
    ws: Worksheet, projections: ForecastProjections
) -> None:
    """Write/overwrite the PROJECTED table.

    Called by both seeder (first write) and refresher (rewrite from current
    inputs). The previous projections (if any) get cleared first so a
    shorter horizon doesn't leave stale columns. The table shows Year,
    Revenue, FCF, Operating Margin %, and Capex % rows so the user can see
    the per-year ramp without re-running the math by hand.
    """
    _clear_projections_section(ws)
    ws.cell(
        row=_PROJECTED_HEADER_ROW,
        column=1,
        value="PROJECTED - program-owned, recomputed each refresh",
    ).font = _HEADER_FONT

    ws.cell(row=_PROJECTED_START_ROW, column=1, value="Year").font = _HEADER_FONT
    ws.cell(row=_PROJECTED_START_ROW + 1, column=1, value="Revenue ($M)").font = (
        _HEADER_FONT
    )
    ws.cell(row=_PROJECTED_START_ROW + 2, column=1, value="FCF ($M)").font = (
        _HEADER_FONT
    )
    ws.cell(row=_PROJECTED_START_ROW + 3, column=1, value="Op Margin %").font = (
        _HEADER_FONT
    )
    ws.cell(row=_PROJECTED_START_ROW + 4, column=1, value="Capex % of Revenue").font = (
        _HEADER_FONT
    )

    for i, year in enumerate(projections.years):
        col = 2 + i
        ws.cell(row=_PROJECTED_START_ROW, column=col, value=year).font = _HEADER_FONT
        rev = ws.cell(
            row=_PROJECTED_START_ROW + 1, column=col, value=projections.revenue_M[i]
        )
        rev.number_format = "#,##0"
        fcf = ws.cell(
            row=_PROJECTED_START_ROW + 2, column=col, value=projections.fcf_M[i]
        )
        fcf.number_format = "#,##0"
        om = ws.cell(
            row=_PROJECTED_START_ROW + 3,
            column=col,
            value=projections.operating_margin_pct[i],
        )
        om.number_format = "0.00%"
        ci = ws.cell(
            row=_PROJECTED_START_ROW + 4,
            column=col,
            value=projections.capex_intensity_pct[i],
        )
        ci.number_format = "0.00%"


def read_inputs_from_sheet(ws: Worksheet) -> ForecastInputs:
    """Label-scan column A; pull values from column B.

    Raises `ForecastError` if any of the ten required inputs is missing or
    non-numeric. Tolerates label edits via the alias lists in `_LABEL_ALIASES`.
    """
    values: dict[str, float] = {}
    for key, aliases in _LABEL_ALIASES.items():
        val = _lookup_input_cell(ws, aliases)
        if val is None:
            raise ForecastError(
                f"Forecast sheet missing input row for {key!r} "
                f"(expected one of: {aliases})"
            )
        values[key] = val

    horizon_raw = values["forecast_years"]
    if horizon_raw < 1 or horizon_raw > 10:
        raise ForecastError(
            f"forecast_years out of range [1, 10]: got {horizon_raw}"
        )

    return ForecastInputs(
        base_revenue_M=values["base_revenue"],
        y1_growth_pct=values["y1_growth"],
        terminal_growth_pct=values["terminal_growth"],
        y1_operating_margin_pct=values["y1_op_margin"],
        y5_operating_margin_pct=values["y5_op_margin"],
        y1_capex_intensity_pct=values["y1_capex_intensity"],
        y5_capex_intensity_pct=values["y5_capex_intensity"],
        tax_rate_pct=values["tax_rate"],
        diluted_shares_M=values["diluted_shares"],
        forecast_years=int(horizon_raw),
    )


# ---------------------------------------------------------------------------
# Helpers (private)
# ---------------------------------------------------------------------------


def _ttm_sum(records: list[dict[str, object]], field: str) -> float | None:
    """Sum a numeric field across the first N records (newest-first FMP order)."""
    total = 0.0
    seen = 0
    for r in records:
        v = r.get(field)
        if isinstance(v, (int, float)):
            total += float(v)
            seen += 1
    return total if seen > 0 else None


def _latest_value(records: list[dict[str, object]], field: str) -> float | None:
    """Pull the most recent numeric value for `field`, walking newest-first."""
    for r in records:
        v = r.get(field)
        if isinstance(v, (int, float)):
            return float(v)
    return None


def _linear_decay(start: float, end: float, n: int) -> list[float]:
    if n <= 0:
        return []
    if n == 1:
        return [start]
    step = (end - start) / (n - 1)
    return [start + i * step for i in range(n)]


def _lookup_input_cell(ws: Worksheet, aliases: tuple[str, ...]) -> float | None:
    for row in range(1, _MAX_SCAN_ROW + 1):
        raw = ws.cell(row=row, column=1).value
        if not isinstance(raw, str):
            continue
        if raw.strip().lower() not in aliases:
            continue
        val = ws.cell(row=row, column=2).value
        if isinstance(val, (int, float)):
            return float(val)
    return None


def _clear_projections_section(ws: Worksheet) -> None:
    """Wipe rows from PROJECTED header down so a fresh write lands cleanly."""
    for row in range(_PROJECTED_HEADER_ROW, _PROJECTED_START_ROW + 6):
        for col in range(1, 15):
            ws.cell(row=row, column=col, value=None)
