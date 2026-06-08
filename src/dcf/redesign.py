"""Read a redesigned 9-sheet DCF workbook and recompute its value-of-record.

The redesigned workbook (``execution/build_redesigned_dcf.py``) is fully
formula-driven: openpyxl cannot evaluate its formulas, so this module reads the
*inputs* instead — the yellow Dashboard assumption cells (the user-owned layer)
plus the blue Financials actuals — and recomputes fair value in Python,
mirroring the in-sheet formulas exactly and bridging through
``dcf.valuation.compute_valuation``.

The projection here is the live counterpart of the builder's ``_project``
mirror: same segment-growth interpolation (hold ~3y, fade to terminal), same
margin ramp, same FCFF bridge, same EV-multiple / perpetuity terminal, same
``× FX`` conversion for non-USD reporters. The difference is that this reads
*arbitrary* (possibly user-edited) Dashboard inputs, so the persisted value
tracks whatever the user sees when they open the workbook.

Also provides the edit-preservation primitives the refresher uses:
``capture_dashboard`` snapshots the user-owned Dashboard inputs before a rebuild;
``inject_dashboard`` writes them back into a freshly-rebuilt workbook (refreshing
current price from the live quote).
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

import openpyxl
from openpyxl.utils.exceptions import InvalidFileException
from openpyxl.workbook.workbook import Workbook
from openpyxl.worksheet.worksheet import Worksheet

from dcf import valuation as val_mod

# --------------------------------------------------------------------------- #
# Structural constants — must track the builder's layout (the DB map + _project)
# --------------------------------------------------------------------------- #
DASHBOARD_SHEET = "Dashboard"
FINANCIALS_SHEET = "Financials"
CONSENSUS_SHEET = "Consensus"
VALUATION_SHEET = "Valuation"
MODEL_SHEET = "Model"
# Sheets that uniquely identify the redesigned format (vs the legacy
# Historicals/Forecast/Valuation seeder workbook).
_REDESIGN_MARKER_SHEETS = (DASHBOARD_SHEET, MODEL_SHEET, FINANCIALS_SHEET, CONSENSUS_SHEET)

N_FC = 10  # forecast years (matches the builder)
NWC_PCT = 0.005  # working-capital draw on incremental revenue (builder literal)

SEG_ROW0 = 20  # first Dashboard segment row
SEG_ROW_MAX = 27  # segments occupy rows 20..27 (the PROFITABILITY band sits at 28)

# Dashboard column-B input cells at fixed addresses (the builder's DB map).
_DB_MARGIN_NEAR = 29
_DB_MARGIN_TERM = 30
_DB_TAX = 31
_DB_CAPEX26 = 34
_DB_TERM_CAPEX_DA = 35
_DB_RF = 38
_DB_ERP = 39
_DB_BETA = 40
_DB_KD = 41
_DB_METHOD = 43
_DB_BASIS = 44
_DB_MULT = 45
_DB_TG = 46
_DB_PRICE = 48

# The numeric scalar inputs (Dashboard col B) preserved across a refresh, keyed by
# row. Current price (row 48) is deliberately excluded — it is a market datum the
# refresher always refreshes from the live quote, not a user assumption.
_PRESERVED_SCALAR_ROWS: tuple[int, ...] = (
    _DB_MARGIN_NEAR,
    _DB_MARGIN_TERM,
    _DB_TAX,
    _DB_CAPEX26,
    _DB_TERM_CAPEX_DA,
    _DB_RF,
    _DB_ERP,
    _DB_BETA,
    _DB_KD,
    _DB_MULT,
    _DB_TG,
)

PERPETUITY = "Perpetuity"
EXIT_MULTIPLE = "Exit multiple"


class RedesignError(Exception):
    """The workbook is not a readable redesigned-format DCF, or carries a
    degenerate assumption the engine cannot value."""


# --------------------------------------------------------------------------- #
# Dataclasses
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class RedesignInputs:
    """Everything ``value()`` needs, read from a redesigned workbook.

    Flows/stocks are in the workbook's reporting currency, millions; shares are
    millions; ``fx_to_usd`` converts the per-share value to USD (1.0 for USD
    reporters). ``consensus_years`` is the margin-ramp window (the builder's
    ``ncons``).
    """

    segments: tuple[str, ...]
    base_revenue_by_segment: dict[str, float]
    near_growth_by_segment: dict[str, float]
    terminal_growth_by_segment: dict[str, float]
    near_op_margin: float
    terminal_op_margin: float
    tax_rate: float
    capex_2026_m: float
    terminal_capex_da: float
    da_ratio: float
    consensus_years: int
    wacc: float
    beta: float
    risk_free_rate: float
    equity_risk_premium: float
    cost_of_debt: float
    terminal_method: str
    terminal_basis: str
    exit_multiple: float
    terminal_growth_g: float
    current_price: float
    cash_m: float
    total_debt_m: float
    diluted_shares_m: float
    fx_to_usd: float


@dataclass(frozen=True)
class RedesignValuation:
    """The recomputed value-of-record and the headline intermediates."""

    value_per_share_usd: float
    value_per_share_reporting: float
    operating_value_usd_m: float  # enterprise value, USD millions
    equity_value_usd_m: float
    fcff_stream_m: list[float]  # valuation FCF, reporting ccy
    forecast_revenue_m: list[float]
    wacc: float
    terminal_method: str
    terminal_basis: str
    exit_multiple: float
    fx_to_usd: float
    diluted_shares_m: float
    cash_m: float
    total_debt_m: float
    current_price: float


@dataclass(frozen=True)
class CapturedDashboard:
    """The user-owned Dashboard inputs, snapshotted for edit-preservation.

    ``segment_growth`` is keyed by segment *name* (not row) so it survives FMP
    adding/removing/reordering a segment between refreshes; ``scalars`` is keyed
    by Dashboard row. Current price is intentionally absent (refreshed live).
    """

    segment_growth: dict[str, tuple[float, float]]
    scalars: dict[int, float]
    terminal_method: str | None
    terminal_basis: str | None


# --------------------------------------------------------------------------- #
# Small typed cell helpers (openpyxl values are untyped unions)
# --------------------------------------------------------------------------- #
def _num(ws: Worksheet, row: int, col: int) -> float | None:
    v = ws.cell(row=row, column=col).value
    if isinstance(v, bool):  # bool is an int subclass — never a numeric input
        return None
    return float(v) if isinstance(v, (int, float)) else None


def _text(ws: Worksheet, row: int, col: int) -> str | None:
    v = ws.cell(row=row, column=col).value
    return v.strip() if isinstance(v, str) else None


# --------------------------------------------------------------------------- #
# Format detection
# --------------------------------------------------------------------------- #
def is_redesign_format(workbook_path: Path) -> bool:
    """True iff the workbook carries the redesigned-format marker sheets.

    Cheap and tolerant: a missing/corrupt file is simply "not redesign".
    """
    if not workbook_path.exists():
        return False
    try:
        wb = openpyxl.load_workbook(str(workbook_path), read_only=True)
    except (OSError, KeyError, ValueError, InvalidFileException):
        return False
    try:
        names = set(wb.sheetnames)
    finally:
        wb.close()
    return all(s in names for s in _REDESIGN_MARKER_SHEETS)


# --------------------------------------------------------------------------- #
# Financials sheet reader (blue actuals)
# --------------------------------------------------------------------------- #
_QUARTER_RE = re.compile(r"Q([1-4])\s+(\d{4})")


def _quarter_columns(fs: Worksheet) -> dict[int, tuple[int, int]]:
    """Map Financials column index -> (fiscal_year, quarter) from the row-1 headers."""
    out: dict[int, tuple[int, int]] = {}
    for col in range(2, fs.max_column + 1):
        label = fs.cell(row=1, column=col).value
        if not isinstance(label, str):
            continue
        m = _QUARTER_RE.match(label.strip())
        if m:
            out[col] = (int(m.group(2)), int(m.group(1)))
    return out


def _detect_fy_quarters(quarters_by_year: dict[int, set[int]]) -> set[int]:
    """The quarter numbers that make up ONE fiscal year for this issuer.

    Generalises "all four quarters" to any consistent cadence — a semi-annual
    filer (e.g. BHP) shows only ``{2, 4}``, whose two half-year columns sum to the
    fiscal year. The cadence is the largest quarter-set recurring across >=2
    fiscal years (so the current partial year never defines it); falls back to
    ``{1, 2, 3, 4}`` when history is too short to establish one.
    """
    counts: dict[frozenset[int], int] = defaultdict(int)
    for qs in quarters_by_year.values():
        if qs:
            counts[frozenset(qs)] += 1
    recurring = [qs for qs, n in counts.items() if n >= 2]
    return set(max(recurring, key=len)) if recurring else {1, 2, 3, 4}


def _latest_full_fy(qcols: dict[int, tuple[int, int]]) -> int:
    """The most recent fiscal year carrying this issuer's full period set (all four
    quarters for a quarterly filer; both halves for a semi-annual one)."""
    quarters_by_year: dict[int, set[int]] = defaultdict(set)
    for _col, (year, q) in qcols.items():
        quarters_by_year[year].add(q)
    cadence = _detect_fy_quarters(quarters_by_year)
    full = [y for y, qs in quarters_by_year.items() if cadence <= qs]
    if not full:
        raise RedesignError("Financials sheet has no complete fiscal year")
    return max(full)


def _find_row(fs: Worksheet, label: str) -> int | None:
    """First column-A row whose stripped label equals ``label`` exactly."""
    for row in range(1, fs.max_row + 1):
        a = fs.cell(row=row, column=1).value
        if isinstance(a, str) and a.strip() == label:
            return row
    return None


def _fy_sum(fs: Worksheet, row: int, fy_cols: list[int]) -> float:
    """Sum a fiscal year's period columns for a row (four quarters, or two halves
    for a semi-annual filer), skipping blanks."""
    total = 0.0
    for col in fy_cols:
        v = _num(fs, row, col)
        if v is not None:
            total += v
    return total


def _latest_quarter_value(fs: Worksheet, row: int, last_col: int) -> float:
    """The latest-quarter value for a row; scan left if the last column is blank."""
    for col in range(last_col, 1, -1):
        v = _num(fs, row, col)
        if v is not None:
            return v
    return 0.0


# --------------------------------------------------------------------------- #
# Reading the full input set
# --------------------------------------------------------------------------- #
def _read_segments(dsh: Worksheet) -> tuple[list[str], dict[str, float], dict[str, float]]:
    """Read the Dashboard segment rows: names + near/terminal growth (yellow)."""
    names: list[str] = []
    near: dict[str, float] = {}
    term: dict[str, float] = {}
    for row in range(SEG_ROW0, SEG_ROW_MAX + 1):
        name = _text(dsh, row, 1)
        g_near = _num(dsh, row, 2)
        if not name or g_near is None:
            continue
        g_term = _num(dsh, row, 3)
        names.append(name)
        near[name] = g_near
        term[name] = g_term if g_term is not None else g_near
    if not names:
        raise RedesignError("Dashboard has no segment rows (expected at row 20+)")
    return names, near, term


def _consensus_window(wb: Workbook) -> int:
    """Number of consensus fiscal-year columns on the Consensus sheet (the
    builder's ``ncons``), clamped to [2, N_FC]."""
    cs = wb[CONSENSUS_SHEET]
    count = 0
    for col in range(2, N_FC + 2):
        v = cs.cell(row=2, column=col).value
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            count += 1
    return max(2, min(N_FC, count))


_FX_RE = re.compile(r"\*\s*([0-9]*\.?[0-9]+)\s*$")


def _read_fx(vs: Worksheet) -> float:
    """Parse the ``× FX`` multiplier off the Valuation 'VALUE / SHARE' formula.

    The builder writes ``=B<eq>/B<sh>*<fx>`` (e.g. ``*1.0`` for USD, ``*0.145``
    for a DKK reporter). The workbook is the single source of truth for FX, so we
    read it back rather than re-deriving a currency table here.
    """
    for row in range(1, vs.max_row + 1):
        a = vs.cell(row=row, column=1).value
        if isinstance(a, str) and a.strip() == "VALUE / SHARE":
            formula = vs.cell(row=row, column=2).value
            if isinstance(formula, str):
                m = _FX_RE.search(formula)
                if m:
                    return float(m.group(1))
    return 1.0


def read_inputs(workbook_path: Path) -> RedesignInputs | None:
    """Read every input ``value()`` needs from a redesigned workbook.

    Returns ``None`` if the workbook is not redesigned-format (so callers can
    fall back to the legacy reader during migration). Raises ``RedesignError``
    if the workbook *is* redesigned-format but structurally unreadable.
    """
    if not is_redesign_format(workbook_path):
        return None
    wb = openpyxl.load_workbook(str(workbook_path), data_only=False)
    try:
        dsh = wb[DASHBOARD_SHEET]
        fs = wb[FINANCIALS_SHEET]
        vs = wb[VALUATION_SHEET]

        segments, near, term = _read_segments(dsh)

        near_margin = _num(dsh, _DB_MARGIN_NEAR, 2)
        term_margin = _num(dsh, _DB_MARGIN_TERM, 2)
        tax = _num(dsh, _DB_TAX, 2)
        capex26 = _num(dsh, _DB_CAPEX26, 2)
        term_capex_da = _num(dsh, _DB_TERM_CAPEX_DA, 2)
        rf = _num(dsh, _DB_RF, 2)
        erp = _num(dsh, _DB_ERP, 2)
        beta = _num(dsh, _DB_BETA, 2)
        kd = _num(dsh, _DB_KD, 2)
        exit_mult = _num(dsh, _DB_MULT, 2)
        tg = _num(dsh, _DB_TG, 2)
        price = _num(dsh, _DB_PRICE, 2)
        method = _text(dsh, _DB_METHOD, 2) or EXIT_MULTIPLE
        basis = _text(dsh, _DB_BASIS, 2) or "EV/EBITDA"

        required = {
            "near margin": near_margin,
            "terminal margin": term_margin,
            "tax": tax,
            "2026 capex": capex26,
            "terminal capex/D&A": term_capex_da,
            "risk-free rate": rf,
            "equity risk premium": erp,
            "beta": beta,
            "cost of debt": kd,
            "exit multiple": exit_mult,
            "terminal g": tg,
            "current price": price,
        }
        missing = [k for k, v in required.items() if v is None]
        if missing:
            raise RedesignError(f"Dashboard missing inputs: {', '.join(missing)}")

        qcols = _quarter_columns(fs)
        if not qcols:
            raise RedesignError("Financials sheet has no quarter columns")
        ly = _latest_full_fy(qcols)
        fy_cols = [c for c, (y, _q) in qcols.items() if y == ly]
        last_col = max(qcols)

        rev_row = _find_row(fs, "Revenue")
        da_row = _find_row(fs, "D&A")
        if rev_row is None or da_row is None:
            raise RedesignError("Financials sheet missing Revenue or D&A row")
        rev_ly = _fy_sum(fs, rev_row, fy_cols)
        if rev_ly <= 0:
            raise RedesignError(f"non-positive last-FY revenue ({rev_ly}) on Financials")
        da_ratio = _fy_sum(fs, da_row, fy_cols) / rev_ly

        base_rev: dict[str, float] = {}
        for s in segments:
            seg_row = _find_row(fs, s)
            # A single-segment model ("Total company") has no segment row — its
            # base revenue is total revenue, exactly as the builder's pseg_fy does.
            base_rev[s] = _fy_sum(fs, seg_row, fy_cols) if seg_row is not None else rev_ly

        cash_row = _find_row(fs, "Cash & ST Investments")
        debt_row = _find_row(fs, "Long-term Debt")
        shares_row = _find_row(fs, "Diluted Shares (M)")
        if cash_row is None or debt_row is None or shares_row is None:
            raise RedesignError("Financials sheet missing cash / debt / shares row")
        cash = _latest_quarter_value(fs, cash_row, last_col)
        debt = _latest_quarter_value(fs, debt_row, last_col)
        shares = _latest_quarter_value(fs, shares_row, last_col)
        if shares <= 0:
            raise RedesignError(f"non-positive diluted shares ({shares}) on Financials")

        ncons = _consensus_window(wb)
        fx = _read_fx(vs)
    finally:
        wb.close()

    # WACC mirrors the in-sheet WACC tab exactly: CAPM cost of equity, market-value
    # weights from the Dashboard price + Financials shares/debt.
    assert (
        rf is not None
        and erp is not None
        and beta is not None
        and kd is not None
        and tax is not None
        and price is not None
    )
    ke = rf + beta * erp
    after_tax_kd = kd * (1.0 - tax)
    market_cap = price * shares
    equity_weight = market_cap / (market_cap + debt) if (market_cap + debt) > 0 else 1.0
    wacc = equity_weight * ke + (1.0 - equity_weight) * after_tax_kd

    assert (
        near_margin is not None
        and term_margin is not None
        and capex26 is not None
        and term_capex_da is not None
        and exit_mult is not None
        and tg is not None
    )
    return RedesignInputs(
        segments=tuple(segments),
        base_revenue_by_segment=base_rev,
        near_growth_by_segment=near,
        terminal_growth_by_segment=term,
        near_op_margin=near_margin,
        terminal_op_margin=term_margin,
        tax_rate=tax,
        capex_2026_m=capex26,
        terminal_capex_da=term_capex_da,
        da_ratio=da_ratio,
        consensus_years=ncons,
        wacc=wacc,
        beta=beta,
        risk_free_rate=rf,
        equity_risk_premium=erp,
        cost_of_debt=kd,
        terminal_method=method,
        terminal_basis=basis,
        exit_multiple=exit_mult,
        terminal_growth_g=tg,
        current_price=price,
        cash_m=cash,
        total_debt_m=debt,
        diluted_shares_m=shares,
        fx_to_usd=fx,
    )


# --------------------------------------------------------------------------- #
# Projection — the live mirror of the builder's _project / in-sheet formulas
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class _ProjectedStreams:
    revenue: list[float]
    ebit: list[float]
    da: list[float]
    valuation_fcf: list[float]


def _project(inp: RedesignInputs) -> _ProjectedStreams:
    """Project N_FC years of revenue / EBIT / D&A / valuation-FCF.

    Mirrors the in-sheet formulas: per-segment growth holds near-term ~3y then
    fades to terminal over the 10y window; operating margin ramps near→terminal
    by the end of the consensus window; D&A = ratio × revenue; capex = D&A ×
    (capex/D&A), where the 2026 ratio = capex / first-year D&A and fades to the
    terminal ratio; ΔNWC = 0.5% of incremental revenue; valuation FCF =
    NOPAT + D&A − capex − ΔNWC (SBC nets out).
    """
    n_seg_steps = N_FC - 3
    ramp_steps = inp.consensus_years - 1

    def seg_growth(segment: str, j: int) -> float:
        g1 = inp.near_growth_by_segment[segment]
        gt = inp.terminal_growth_by_segment[segment]
        return g1 + (gt - g1) * max(0, j - 2) / n_seg_steps

    def op_margin(j: int) -> float:
        spread = inp.terminal_op_margin - inp.near_op_margin
        return inp.near_op_margin + spread * min(1.0, j / ramp_steps)

    revenue: list[float] = []
    ebit: list[float] = []
    da: list[float] = []
    seg = dict(inp.base_revenue_by_segment)
    for j in range(N_FC):
        for s in inp.segments:
            seg[s] *= 1.0 + seg_growth(s, j)
        rev = sum(seg.values())
        revenue.append(rev)
        ebit.append(rev * op_margin(j))
        da.append(rev * inp.da_ratio)

    # 2026 capex/D&A ratio uses the *model's* first-year D&A (matches the in-sheet
    # formula), fading linearly to the terminal ratio over the window.
    da_2026 = da[0] if da[0] else 1.0
    cda0 = inp.capex_2026_m / da_2026

    valuation_fcf: list[float] = []
    prev_rev = sum(inp.base_revenue_by_segment.values())
    for j in range(N_FC):
        capex_da = cda0 + (inp.terminal_capex_da - cda0) * j / (N_FC - 1)
        capex = da[j] * capex_da
        nopat = ebit[j] * (1.0 - inp.tax_rate)
        delta_nwc = (revenue[j] - prev_rev) * NWC_PCT
        valuation_fcf.append(nopat + da[j] - capex - delta_nwc)
        prev_rev = revenue[j]

    return _ProjectedStreams(revenue, ebit, da, valuation_fcf)


def _terminal_metrics(streams: _ProjectedStreams, tax_rate: float) -> val_mod.TerminalMetrics:
    """Terminal-year line items the exit multiple can apply to (reporting ccy)."""
    return val_mod.TerminalMetrics(
        revenue=streams.revenue[-1],
        ebit=streams.ebit[-1],
        ebitda=streams.ebit[-1] + streams.da[-1],
        fcf=streams.valuation_fcf[-1],
        net_income=streams.ebit[-1] * (1.0 - tax_rate),
    )


def value(inp: RedesignInputs) -> RedesignValuation:
    """Recompute the value-of-record from a redesigned workbook's inputs.

    Routes through ``compute_valuation`` (the Damodaran engine): the default
    exit-multiple terminal directly, and a perpetuity terminal as an equivalent
    EV/FCF multiple of (1+g)/(WACC−g). Multiplies the per-share result by FX to
    convert non-USD reporters to USD. Raises ``RedesignError`` only for a
    genuinely un-valuable assumption (perpetuity with WACC ≤ g).
    """
    streams = _project(inp)
    years = list(range(N_FC))  # discount exponents are positional, labels unused
    terminal = _terminal_metrics(streams, inp.tax_rate)

    if inp.terminal_method == PERPETUITY:
        if inp.wacc <= inp.terminal_growth_g:
            raise RedesignError(
                f"perpetuity terminal requires WACC ({inp.wacc:.4f}) > "
                f"terminal g ({inp.terminal_growth_g:.4f})"
            )
        basis = "EV/FCF"
        multiple = (1.0 + inp.terminal_growth_g) / (inp.wacc - inp.terminal_growth_g)
    else:
        basis = inp.terminal_basis
        multiple = inp.exit_multiple

    dv = val_mod.compute_valuation(
        streams.valuation_fcf,
        years,
        inp.wacc,
        basis=basis,
        terminal_multiple=multiple,
        terminal=terminal,
        cash_and_nonop=inp.cash_m,
        total_debt=inp.total_debt_m,
        diluted_shares_M=inp.diluted_shares_m,
    )

    return RedesignValuation(
        value_per_share_usd=dv.value_per_share * inp.fx_to_usd,
        value_per_share_reporting=dv.value_per_share,
        operating_value_usd_m=dv.operating_value * inp.fx_to_usd,
        equity_value_usd_m=dv.equity_value * inp.fx_to_usd,
        fcff_stream_m=list(streams.valuation_fcf),
        forecast_revenue_m=list(streams.revenue),
        wacc=inp.wacc,
        terminal_method=inp.terminal_method,
        terminal_basis=inp.terminal_basis,
        exit_multiple=inp.exit_multiple,
        fx_to_usd=inp.fx_to_usd,
        diluted_shares_m=inp.diluted_shares_m,
        cash_m=inp.cash_m,
        total_debt_m=inp.total_debt_m,
        current_price=inp.current_price,
    )


def read_and_value(workbook_path: Path) -> RedesignValuation | None:
    """Read a redesigned workbook and recompute its value-of-record.

    Returns ``None`` if the workbook is not redesigned-format.
    """
    inp = read_inputs(workbook_path)
    if inp is None:
        return None
    return value(inp)


# --------------------------------------------------------------------------- #
# Edit-preservation — capture the user-owned Dashboard, re-inject after rebuild
# --------------------------------------------------------------------------- #
def capture_dashboard(workbook_path: Path) -> CapturedDashboard | None:
    """Snapshot the user-owned Dashboard inputs (yellow cells) for preservation.

    Returns ``None`` if the workbook is missing or not redesigned-format (a fresh
    build has nothing to preserve). Current price is intentionally not captured —
    the refresher always refreshes it from the live quote.
    """
    if not is_redesign_format(workbook_path):
        return None
    wb = openpyxl.load_workbook(str(workbook_path), data_only=False)
    try:
        dsh = wb[DASHBOARD_SHEET]
        segment_growth: dict[str, tuple[float, float]] = {}
        for row in range(SEG_ROW0, SEG_ROW_MAX + 1):
            name = _text(dsh, row, 1)
            g_near = _num(dsh, row, 2)
            if not name or g_near is None:
                continue
            g_term = _num(dsh, row, 3)
            segment_growth[name] = (g_near, g_term if g_term is not None else g_near)
        scalars: dict[int, float] = {}
        for r in _PRESERVED_SCALAR_ROWS:
            v = _num(dsh, r, 2)
            if v is not None:
                scalars[r] = v
        method = _text(dsh, _DB_METHOD, 2)
        basis = _text(dsh, _DB_BASIS, 2)
    finally:
        wb.close()
    return CapturedDashboard(
        segment_growth=segment_growth,
        scalars=scalars,
        terminal_method=method,
        terminal_basis=basis,
    )


def inject_dashboard(
    workbook_path: Path,
    captured: CapturedDashboard | None,
    *,
    current_price: float | None,
) -> None:
    """Write preserved Dashboard inputs into a freshly-rebuilt workbook in place.

    Segment growth is re-applied by segment *name* (a renamed/added/removed
    segment keeps the fresh default); scalars by row; current price (always) from
    the live quote when supplied. A ``None`` capture still refreshes price.
    """
    wb = openpyxl.load_workbook(str(workbook_path), data_only=False)
    try:
        dsh = wb[DASHBOARD_SHEET]
        if captured is not None:
            for row in range(SEG_ROW0, SEG_ROW_MAX + 1):
                name = _text(dsh, row, 1)
                if name and name in captured.segment_growth:
                    g_near, g_term = captured.segment_growth[name]
                    dsh.cell(row=row, column=2, value=g_near)
                    dsh.cell(row=row, column=3, value=g_term)
            for r, v in captured.scalars.items():
                dsh.cell(row=r, column=2, value=v)
            if captured.terminal_method is not None:
                dsh.cell(row=_DB_METHOD, column=2, value=captured.terminal_method)
            if captured.terminal_basis is not None:
                dsh.cell(row=_DB_BASIS, column=2, value=captured.terminal_basis)
        if current_price is not None:
            dsh.cell(row=_DB_PRICE, column=2, value=current_price)
        wb.save(str(workbook_path))
    finally:
        wb.close()
