"""Forecast assumption derivation + projection math for DCF workbooks.

The workbook's Forecast sheet has two zones:

  INPUTS    — user-editable. Seeded from the ticker's TTM history; the user
              edits per-cell as desired. The refresher does NOT overwrite
              these on subsequent runs — re-seed with `force=True` to reset
              from latest FMP.

  PROJECTED — program-owned table (Year / Revenue / FCF columns). Rewritten
              on every refresh from the current INPUTS via Python (not Excel
              formulas, so openpyxl reads stay deterministic regardless of
              whether the user opens the workbook in Excel between refreshes).

The projection model is intentionally simple — linear-decay revenue growth
to a terminal rate, flat FCF margin across the horizon. The user audits the
math by reading two cells; anything more sophisticated (segment-level
build-up, working-capital model, etc.) belongs in a user-maintained Model
sheet, not here.
"""

from __future__ import annotations

from dataclasses import dataclass

from openpyxl.styles import Font, PatternFill
from openpyxl.worksheet.worksheet import Worksheet

DEFAULT_FORECAST_YEARS = 5
DEFAULT_TERMINAL_GROWTH = 0.025
DEFAULT_FCF_MARGIN_FALLBACK = 0.15
_TTM_QUARTERS = 4
_MAX_SCAN_ROW = 50
_MILLIONS = 1_000_000.0

# Single source of truth for the INPUTS layout — every read and write path
# pulls labels and target rows from here.
_INPUT_LABEL_BASE_REVENUE = "Base Revenue (TTM, $M)"
_INPUT_LABEL_Y1_GROWTH = "Y1 Revenue Growth %"
_INPUT_LABEL_TERMINAL_GROWTH = "Terminal Revenue Growth %"
_INPUT_LABEL_FCF_MARGIN = "FCF Margin %"
_INPUT_LABEL_DILUTED_SHARES = "Diluted Shares (M)"
_INPUT_LABEL_FORECAST_YEARS = "Forecast Years"

# Accepted label variants for label-scan reads. Lowercased + stripped.
# Listed widest-to-tightest so even a user who shortens a label still parses.
_LABEL_ALIASES = {
    "base_revenue": ("base revenue (ttm, $m)", "base revenue"),
    "y1_growth": ("y1 revenue growth %", "y1 growth", "year 1 growth"),
    "terminal_growth": (
        "terminal revenue growth %",
        "terminal growth %",
        "terminal growth",
    ),
    "fcf_margin": ("fcf margin %", "fcf margin"),
    "diluted_shares": ("diluted shares (m)", "diluted shares"),
    "forecast_years": ("forecast years", "horizon", "forecast horizon"),
}

# Fixed row positions for the INPUTS zone (column A = label, B = value).
_INPUT_ROWS = {
    "base_revenue": (3, _INPUT_LABEL_BASE_REVENUE),
    "y1_growth": (4, _INPUT_LABEL_Y1_GROWTH),
    "terminal_growth": (5, _INPUT_LABEL_TERMINAL_GROWTH),
    "fcf_margin": (6, _INPUT_LABEL_FCF_MARGIN),
    "diluted_shares": (7, _INPUT_LABEL_DILUTED_SHARES),
    "forecast_years": (8, _INPUT_LABEL_FORECAST_YEARS),
}
_PROJECTED_START_ROW = 12  # 9-11 reserved for spacer + section header

# Visual cue — user-editable input cells get a light yellow fill so the user
# can spot them in Excel without reading the headers.
_INPUT_FILL = PatternFill(start_color="FFF7DC", end_color="FFF7DC", fill_type="solid")
_HEADER_FONT = Font(bold=True)
_NOTE_FONT = Font(italic=True, color="666666")


@dataclass(frozen=True)
class ForecastInputs:
    """User-editable assumptions. Units: USD millions where applicable;
    growth + margin are decimals (0.10 == 10%)."""

    base_revenue_M: float
    y1_growth_pct: float
    terminal_growth_pct: float
    fcf_margin_pct: float
    diluted_shares_M: float
    forecast_years: int


@dataclass(frozen=True)
class ForecastProjections:
    """Year-by-year revenue + FCF stream computed from `ForecastInputs`."""

    years: list[int]
    revenue_M: list[float]
    fcf_M: list[float]


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

    Records must be sorted newest-first (FMP's native order). Falls back
    gracefully when data is incomplete:
      - <4 quarters of revenue: use what's available, scaled to TTM-equivalent.
      - prior TTM unavailable: Y1 growth defaults to terminal_growth_pct.
      - FCF data missing: margin defaults to `DEFAULT_FCF_MARGIN_FALLBACK`
        (a deliberate amber-flag value so the user knows to override).
      - shares count missing: defaults to 1000 (user will obviously override;
        better than blowing up the seed).
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

    ttm_fcf = _ttm_sum(cashflow_records[:_TTM_QUARTERS], "freeCashFlow")
    if (
        ttm_fcf is not None
        and base_revenue_actual
        and base_revenue_actual > 0
    ):
        fcf_margin_pct = ttm_fcf / base_revenue_actual
    else:
        fcf_margin_pct = DEFAULT_FCF_MARGIN_FALLBACK

    shares_actual = _latest_value(income_records, "weightedAverageShsOutDil")
    diluted_shares_M = (
        shares_actual / _MILLIONS if shares_actual is not None else 1000.0
    )

    return ForecastInputs(
        base_revenue_M=round(base_revenue_M, 2),
        y1_growth_pct=round(y1_growth_pct, 4),
        terminal_growth_pct=round(terminal_growth_pct, 4),
        fcf_margin_pct=round(fcf_margin_pct, 4),
        diluted_shares_M=round(diluted_shares_M, 2),
        forecast_years=forecast_years,
    )


def compute_projections(
    inputs: ForecastInputs, base_year: int
) -> ForecastProjections:
    """Project revenue + FCF over `forecast_years` years starting at `base_year + 1`.

    Growth decays linearly from Y1 to terminal across the horizon. FCF is
    revenue × FCF margin (flat-margin assumption). Horizon is clamped to
    [1, 10] to defend against absurd user input.
    """
    horizon = max(1, min(inputs.forecast_years, 10))
    growths = _linear_decay(
        inputs.y1_growth_pct, inputs.terminal_growth_pct, horizon
    )
    years = list(range(base_year + 1, base_year + 1 + horizon))
    revenues: list[float] = []
    fcfs: list[float] = []
    rev = inputs.base_revenue_M
    for g in growths:
        rev = rev * (1 + g)
        revenues.append(rev)
        fcfs.append(rev * inputs.fcf_margin_pct)
    return ForecastProjections(years=years, revenue_M=revenues, fcf_M=fcfs)


def write_inputs_section(ws: Worksheet, inputs: ForecastInputs) -> None:
    """Write the INPUTS zone (rows 1-9). Yellow-fills the user-editable cells.

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
        (_INPUT_ROWS["base_revenue"][0], _INPUT_LABEL_BASE_REVENUE, inputs.base_revenue_M),
        (_INPUT_ROWS["y1_growth"][0], _INPUT_LABEL_Y1_GROWTH, inputs.y1_growth_pct),
        (
            _INPUT_ROWS["terminal_growth"][0],
            _INPUT_LABEL_TERMINAL_GROWTH,
            inputs.terminal_growth_pct,
        ),
        (_INPUT_ROWS["fcf_margin"][0], _INPUT_LABEL_FCF_MARGIN, inputs.fcf_margin_pct),
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
    """Write/overwrite the PROJECTED table starting at row 11.

    Called by both seeder (first write) and refresher (rewrite from current
    inputs). The previous projections (if any) get cleared first so a
    shorter horizon doesn't leave stale columns.
    """
    _clear_projections_section(ws)
    ws.cell(
        row=10,
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


def read_inputs_from_sheet(ws: Worksheet) -> ForecastInputs:
    """Label-scan column A; pull values from column B.

    Raises `ForecastError` if any of the six required inputs is missing or
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
        fcf_margin_pct=values["fcf_margin"],
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
    for row in range(10, _PROJECTED_START_ROW + 4):
        for col in range(1, 15):
            ws.cell(row=row, column=col, value=None)
