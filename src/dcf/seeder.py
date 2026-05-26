"""Seed a DCF workbook for a ticker by copying a template and populating Historicals.

Each named ticker gets a canonical workbook at `dcf/<TICKER>.xlsx` — the user
hand-edits Forecast / Model / Valuation; the system owns the Historicals sheet.
The seeder runs once per new ticker: copy a reference template, then write 20
quarters of standardized FMP financials into a freshly-created `Historicals`
sheet. Forecast / Model / Valuation are inherited verbatim from the template
and must be edited by the user before `refresh_dcf.py` can produce a valid run.

Source data: the standardized FMP statement files
(`{TICKER}_income_statement_quarterly.json`, `{TICKER}_balance_sheet_quarterly.json`,
`{TICKER}_cash_flow_quarterly.json`). These have stable camelCase field names
across all tickers, unlike the `as_reported_*` XBRL-tagged files whose key sets
diverge per company. The historicals row layout is fixed (~30 lines) and uses
label-scan lookups so a future schema bump doesn't break the refresher.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import openpyxl
from openpyxl.styles import Font
from openpyxl.workbook.workbook import Workbook
from openpyxl.worksheet.worksheet import Worksheet

HISTORICALS_SHEET = "Historicals"
DEFAULT_QUARTERS = 20
MILLIONS = 1_000_000.0

# Fallback chain when no per-ticker `{TICKER}-*.xlsx` example exists. Tried
# in order; first existing file wins. GOOG leads because it's the densest
# example workbook and the one most prompts/refresher labels were tuned against.
_TEMPLATE_FALLBACKS = ("GOOG-Mar-09-2023.xlsx", "META-Feb-02-2026.xlsx", "AMZN-Feb-06-2026.xlsx")


class SeederError(Exception):
    """Workbook seeding cannot proceed (missing inputs, refusing to overwrite)."""


@dataclass(frozen=True)
class _Row:
    """One Historicals line: (label, source-statement, FMP field, scale).

    `scale` is the divisor applied to raw FMP cents to land on the workbook
    convention. `MILLIONS` is the dominant case; `1.0` is for per-share /
    ratio fields that don't need rescaling.
    """

    label: str
    source: str  # 'income' | 'balance' | 'cashflow'
    field: str
    scale: float


# Row layout — fixed order so the refresher's label-scan lands on the same
# cells the seeder wrote. Section dividers are inserted at render time.
_INCOME_ROWS: tuple[_Row, ...] = (
    _Row("Revenue", "income", "revenue", MILLIONS),
    _Row("Cost of Revenue", "income", "costOfRevenue", MILLIONS),
    _Row("Gross Profit", "income", "grossProfit", MILLIONS),
    _Row("R&D Expense", "income", "researchAndDevelopmentExpenses", MILLIONS),
    _Row("SG&A Expense", "income", "sellingGeneralAndAdministrativeExpenses", MILLIONS),
    _Row("Operating Expenses", "income", "operatingExpenses", MILLIONS),
    _Row("Operating Income", "income", "operatingIncome", MILLIONS),
    _Row("Net Income", "income", "netIncome", MILLIONS),
    _Row("Diluted EPS", "income", "epsDiluted", 1.0),
    _Row("Diluted Shares (M)", "income", "weightedAverageShsOutDil", MILLIONS),
)
_BALANCE_ROWS: tuple[_Row, ...] = (
    _Row("Cash & ST Investments", "balance", "cashAndShortTermInvestments", MILLIONS),
    _Row("Receivables", "balance", "netReceivables", MILLIONS),
    _Row("Inventory", "balance", "inventory", MILLIONS),
    _Row("Total Current Assets", "balance", "totalCurrentAssets", MILLIONS),
    _Row("PP&E (net)", "balance", "propertyPlantEquipmentNet", MILLIONS),
    _Row("Goodwill", "balance", "goodwill", MILLIONS),
    _Row("Total Assets", "balance", "totalAssets", MILLIONS),
    _Row("Total Current Liabilities", "balance", "totalCurrentLiabilities", MILLIONS),
    _Row("Long-term Debt", "balance", "longTermDebt", MILLIONS),
    _Row("Total Equity", "balance", "totalStockholdersEquity", MILLIONS),
)
_CASHFLOW_ROWS: tuple[_Row, ...] = (
    _Row("Net Income (CF)", "cashflow", "netIncome", MILLIONS),
    _Row("D&A", "cashflow", "depreciationAndAmortization", MILLIONS),
    _Row("Stock-based Compensation", "cashflow", "stockBasedCompensation", MILLIONS),
    _Row("Change in Working Capital", "cashflow", "changeInWorkingCapital", MILLIONS),
    _Row("Operating Cash Flow", "cashflow", "operatingCashFlow", MILLIONS),
    _Row("Capex", "cashflow", "capitalExpenditure", MILLIONS),
    _Row("Free Cash Flow", "cashflow", "freeCashFlow", MILLIONS),
    _Row("Net Stock Issuance", "cashflow", "netCommonStockIssuance", MILLIONS),
    _Row("Dividends Paid", "cashflow", "commonDividendsPaid", MILLIONS),
    _Row("Net Change in Cash", "cashflow", "netChangeInCash", MILLIONS),
)


def seed_workbook(
    ticker: str,
    template_dir: Path,
    fmp_quarterly_dir: Path,
    output_path: Path,
    *,
    force: bool = False,
    quarters: int = DEFAULT_QUARTERS,
) -> None:
    """Copy a template to `output_path` and write a populated Historicals sheet.

    Raises `SeederError` if `output_path` exists and `force=False`, or if no
    template / FMP data is available. Never touches user-owned sheets.
    """
    ticker = ticker.upper()
    if output_path.exists() and not force:
        raise SeederError(
            f"refusing to overwrite existing workbook: {output_path} (pass force=True)"
        )

    template_path = _resolve_template(template_dir, ticker)
    quarters_data = _load_quarterly_records(ticker, fmp_quarterly_dir, quarters)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(template_path, output_path)

    wb = openpyxl.load_workbook(str(output_path))
    _write_historicals(wb, quarters_data)
    wb.save(str(output_path))


def write_historicals_sheet(workbook_path: Path, fmp_quarterly_dir: Path, ticker: str, *, quarters: int = DEFAULT_QUARTERS) -> int:
    """Open an existing workbook, rewrite ONLY its Historicals sheet, save.

    Returns the count of data cells written. Used by both the refresher and
    the seeder's `_write_historicals` so the layout stays in one place.
    """
    quarters_data = _load_quarterly_records(ticker.upper(), fmp_quarterly_dir, quarters)
    wb = openpyxl.load_workbook(str(workbook_path))
    cells = _write_historicals(wb, quarters_data)
    wb.save(str(workbook_path))
    return cells


def _resolve_template(template_dir: Path, ticker: str) -> Path:
    # Prefer a ticker-specific example so AMZN/META/etc. inherit their own
    # Forecast/Model/Valuation rather than GOOG's. Sort and pick the last so
    # if multiple snapshots exist (AMZN-Feb-06-2026.xlsx vs an older one), the
    # lexicographically-latest filename wins.
    ticker_matches = sorted(template_dir.glob(f"{ticker}-*.xlsx"))
    if ticker_matches:
        return ticker_matches[-1]
    for name in _TEMPLATE_FALLBACKS:
        candidate = template_dir / name
        if candidate.exists():
            return candidate
    raise SeederError(
        f"no template found in {template_dir} "
        f"(tried: {ticker}-*.xlsx, then {', '.join(_TEMPLATE_FALLBACKS)})"
    )


@dataclass(frozen=True)
class _QuarterRecord:
    """One quarter's data, merged across the 3 statement files."""

    period_label: str  # e.g. "Q1 2026"
    income: dict[str, object]
    balance: dict[str, object]
    cashflow: dict[str, object]


def _load_quarterly_records(
    ticker: str, fmp_quarterly_dir: Path, quarters: int
) -> list[_QuarterRecord]:
    """Build N quarters of merged income+balance+cashflow records, oldest first.

    FMP files are sorted newest-first; we take the first N from each and merge
    by (fiscalYear, period). Missing records on either side leave gaps — those
    quarters skip the affected cells but still appear in the period header.
    """
    income_raw = _read_fmp(fmp_quarterly_dir / f"{ticker}_income_statement_quarterly.json")
    balance_raw = _read_fmp(fmp_quarterly_dir / f"{ticker}_balance_sheet_quarterly.json")
    cashflow_raw = _read_fmp(fmp_quarterly_dir / f"{ticker}_cash_flow_quarterly.json")

    income_by_key = _index_by_period(income_raw)
    balance_by_key = _index_by_period(balance_raw)
    cashflow_by_key = _index_by_period(cashflow_raw)

    if not income_by_key:
        raise SeederError(f"no income statement records for {ticker}")

    # Use the income file's chronology as the canonical period ordering — it's
    # the densest (filed quarterly without fail) of the three.
    sorted_keys = sorted(income_by_key.keys(), reverse=True)[:quarters]
    sorted_keys.reverse()  # oldest → newest left-to-right

    records: list[_QuarterRecord] = []
    for key in sorted_keys:
        year, period = key
        records.append(
            _QuarterRecord(
                period_label=f"{period} {year}",
                income=income_by_key.get(key, {}),
                balance=balance_by_key.get(key, {}),
                cashflow=cashflow_by_key.get(key, {}),
            )
        )
    return records


def _read_fmp(path: Path) -> list[dict[str, object]]:
    if not path.exists():
        return []
    raw_obj: object = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw_obj, list):
        return []
    raw = cast("list[object]", raw_obj)
    return [cast("dict[str, object]", r) for r in raw if isinstance(r, dict)]


def _index_by_period(
    records: list[dict[str, object]],
) -> dict[tuple[int, str], dict[str, object]]:
    """Build a (year, period) → record map. `fiscalYear` ships as int OR str
    across FMP files for the same ticker, so coerce defensively.
    """
    out: dict[tuple[int, str], dict[str, object]] = {}
    for r in records:
        year_raw = r.get("fiscalYear")
        period = r.get("period")
        if not (isinstance(period, str) and period.startswith("Q")):
            continue
        try:
            year = int(year_raw) if isinstance(year_raw, (int, str)) else None
        except ValueError:
            year = None
        if year is not None:
            out[(year, period)] = r
    return out


def _write_historicals(wb: Workbook, quarters: list[_QuarterRecord]) -> int:
    """Drop and rebuild the Historicals sheet; return data cell count.

    The drop-and-recreate is deliberate: any prior cells left over from a
    bigger template (more quarters, extra rows) get cleared in one move.
    """
    if HISTORICALS_SHEET in wb.sheetnames:
        del wb[HISTORICALS_SHEET]
    ws = wb.create_sheet(HISTORICALS_SHEET)

    bold = Font(bold=True)
    n_cols = len(quarters)

    # Row 1: period header
    ws.cell(row=1, column=1, value="Line Item").font = bold
    for i, q in enumerate(quarters):
        c = ws.cell(row=1, column=2 + i, value=q.period_label)
        c.font = bold

    row = 2
    data_cells = 0
    for section, rows in (
        ("INCOME STATEMENT", _INCOME_ROWS),
        ("BALANCE SHEET", _BALANCE_ROWS),
        ("CASH FLOW", _CASHFLOW_ROWS),
    ):
        ws.cell(row=row, column=1, value=section).font = bold
        row += 1
        for spec in rows:
            ws.cell(row=row, column=1, value=spec.label)
            for i, q in enumerate(quarters):
                val = _extract_value(q, spec)
                if val is not None:
                    ws.cell(row=row, column=2 + i, value=val)
                    data_cells += 1
            row += 1
        row += 1  # spacer between sections

    ws.cell(row=row, column=1, value="Units: USD millions except per-share").font = Font(
        italic=True
    )

    _autosize_column_a(ws, n_cols)
    return data_cells


def _extract_value(q: _QuarterRecord, spec: _Row) -> float | None:
    if spec.source == "income":
        src = q.income
    elif spec.source == "balance":
        src = q.balance
    elif spec.source == "cashflow":
        src = q.cashflow
    else:
        return None
    raw = src.get(spec.field)
    if not isinstance(raw, (int, float)):
        return None
    return float(raw) / spec.scale


def _autosize_column_a(ws: Worksheet, n_cols: int) -> None:
    """Widen the label column and shrink data columns for readability."""
    from openpyxl.utils import get_column_letter

    ws.column_dimensions["A"].width = 32
    for i in range(n_cols):
        col_letter = get_column_letter(2 + i)
        ws.column_dimensions[col_letter].width = 12
