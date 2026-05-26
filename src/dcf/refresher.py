"""Refresh the Historicals sheet of an existing DCF workbook from FMP data.

Each quarter the user expects their `dcf/<TICKER>.xlsx` Historicals tab to
match the latest filings; everything else (Forecast / Model / Valuation /
custom segment sheets) is hand-edited and must round-trip byte-identical
through a refresh. The contract:

  * Sheet name list before == after.
  * Every non-Historicals cell's `(value, font, fill, number_format)`
    is preserved.
  * Historicals is dropped and rebuilt — no per-cell merging — to keep the
    same fixed layout the seeder writes.

The refresher refuses to operate on a workbook lacking a Historicals sheet;
that's the seeder's job. Callers should dispatch on existence of the
workbook (`refresh_dcf.py`).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import openpyxl

from dcf.seeder import HISTORICALS_SHEET, SeederError, write_historicals_sheet


class RefresherError(Exception):
    """Refresh cannot proceed (workbook missing, no Historicals sheet, etc.)."""


@dataclass(frozen=True)
class RefreshResult:
    workbook_path: str
    cells_written: int
    sheets: list[str]


def refresh_historicals(
    workbook_path: Path, fmp_quarterly_dir: Path, ticker: str | None = None
) -> RefreshResult:
    """Open the workbook, rewrite its Historicals sheet from FMP, return a summary.

    `ticker` defaults to the workbook's filename stem (`dcf/META.xlsx` → META).
    Raises `RefresherError` if the workbook is missing or has no Historicals
    sheet to refresh.
    """
    if not workbook_path.exists():
        raise RefresherError(f"workbook not found: {workbook_path}")

    resolved_ticker = (ticker or workbook_path.stem).upper()

    # Sheet-name guard: only refresh workbooks the seeder has already touched.
    # Avoids silently appending a Historicals sheet to a user-curated template.
    wb_check = openpyxl.load_workbook(str(workbook_path), read_only=True)
    sheets_before = list(wb_check.sheetnames)
    wb_check.close()
    if HISTORICALS_SHEET not in sheets_before:
        raise RefresherError(
            f"{workbook_path.name} has no '{HISTORICALS_SHEET}' sheet — run seeder first"
        )

    try:
        cells = write_historicals_sheet(workbook_path, fmp_quarterly_dir, resolved_ticker)
    except SeederError as e:
        raise RefresherError(str(e)) from e

    # Confirm the sheet list survived the round-trip exactly. Anything else
    # means our write code corrupted the workbook structure.
    wb_after = openpyxl.load_workbook(str(workbook_path), read_only=True)
    sheets_after = list(wb_after.sheetnames)
    wb_after.close()
    if sheets_after != sheets_before:
        raise RefresherError(
            f"sheet list changed during refresh: before={sheets_before} after={sheets_after}"
        )

    return RefreshResult(
        workbook_path=str(workbook_path),
        cells_written=cells,
        sheets=sheets_after,
    )
