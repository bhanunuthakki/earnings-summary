"""Parse an IR historical-data spreadsheet into canonical KPI series.

Config-driven and layout-tolerant: per sheet we auto-detect the date-header row
(the row in the first dozen with the most quarter-end dates), map each data
column to a `period_end`, then for each configured `SheetKpi` pick the row whose
label-cell contains the configured substring AND carries numeric data, and read
its last N quarters. Headers mix Excel datetimes and "dd/mm/yyyy" strings, so
`_parse_period` normalizes both.

Returns ``{kpi_name: {period_end: value}}`` with `scale` already applied.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
from dataclasses import asdict, dataclass, field, replace
from io import BytesIO
from pathlib import Path
from typing import Literal, cast

import openpyxl
from openpyxl.utils import get_column_letter

from ir_pipeline.config import IrConfig, SheetKpi
from models.facts import FactLocator, LocatorKind, SpreadsheetCellRef
from pipeline.invocation_fingerprint import payload_sha256

_HEADER_SCAN_ROWS = 14


@dataclass(frozen=True)
class SheetDataRow:
    """One labeled numeric row discovered in an IR spreadsheet.

    The capture-every-number config builder enumerates these to map EVERY data
    row (not just curated tier KPIs). ``samples`` are the two most-recent numeric
    cells — enough to infer unit/scale (a 0.095 cell is a decimal percent).
    """

    sheet: str
    label: str
    samples: list[float] = field(default_factory=list[float])


def _parse_period(cell: object) -> _dt.datetime | None:
    """Normalize a header cell to a quarter-end datetime, or None if not a date."""
    if isinstance(cell, _dt.datetime):
        return cell
    if isinstance(cell, _dt.date):
        return _dt.datetime(cell.year, cell.month, cell.day)
    if isinstance(cell, str):
        s = cell.strip()
        if s.count("/") == 2:  # dd/mm/yyyy
            d, m, y = s.split("/")
            try:
                return _dt.datetime(int(y), int(m), int(d))
            except ValueError:
                return None
        if len(s) >= 10 and s[4] == "-" and s[7] == "-":  # yyyy-mm-dd...
            try:
                return _dt.datetime(int(s[:4]), int(s[5:7]), int(s[8:10]))
            except ValueError:
                return None
    return None


def _header_row(rows: list[tuple[object, ...]]) -> tuple[int, dict[int, _dt.datetime]]:
    """Return (row index, {col_index: period_end}) for the best date-header row."""
    best_idx, best_map = -1, {}
    for i in range(min(_HEADER_SCAN_ROWS, len(rows))):
        col_map = {ci: p for ci, c in enumerate(rows[i]) if (p := _parse_period(c)) is not None}
        if len(col_map) > len(best_map):
            best_idx, best_map = i, col_map
    return best_idx, best_map


# Shared builder seam: callers outside this module should use the public name
# rather than reaching into the parser's private implementation.
header_row = _header_row


def _label_matches(cell: object, want: str, *, exact: bool) -> bool:
    """Whether a label cell matches ``want`` (already lower-cased).

    Substring (``exact=False``) is the forgiving analyst mode — a short hand-picked
    label finds the full cell. ``exact=True`` (capture rows) requires the trimmed
    cell to equal ``want`` so an auto-derived bare label ("NII") can't also grab a
    superstring row ("Risk-adjusted NII").
    """
    if not isinstance(cell, str):
        return False
    lab = cell.strip().lower()
    return lab == want if exact else want in lab


def _find_data_row(
    rows: list[tuple[object, ...]],
    label_col: int,
    label_substr: str,
    date_cols: list[int],
    *,
    exact: bool = False,
) -> int | None:
    """Row index whose label cell matches and that carries the most numeric data.

    ``exact`` switches ``row_label`` from substring to exact-cell matching (capture
    rows). When several rows share the same exact label (real sheets repeat a label
    across sub-blocks), the one with the most numeric cells wins — a deterministic
    "richest representative" pick for a genuinely ambiguous duplicate.
    """
    want = label_substr.strip().lower()
    best_idx, best_count = None, 0
    for ri, r in enumerate(rows):
        if label_col >= len(r):
            continue
        if not _label_matches(r[label_col], want, exact=exact):
            continue
        n = sum(1 for c in date_cols if c < len(r) and isinstance(r[c], (int, float)))
        if n > best_count:
            best_idx, best_count = ri, n
    return best_idx


SpreadsheetValue = str | int | float | _dt.datetime | _dt.date | _dt.time | _dt.timedelta | None


SpreadsheetCellDisposition = Literal[
    "selected",
    "nonnumeric",
    "unsupported_value",
    "unknown_period",
    "outside_window",
    "overwritten_period",
    "overwritten_configured_name",
    "sheet_missing",
    "header_missing",
    "row_missing",
]


@dataclass(frozen=True)
class SpreadsheetCellRead:
    """One configured source selection or skip, without semantic admission."""

    spec: SheetKpi
    raw_label: str | None
    locator: FactLocator | None
    header: SpreadsheetCellRef | None
    period_end: _dt.datetime | None
    raw_value: SpreadsheetValue
    scaled_value: float | None
    disposition: SpreadsheetCellDisposition
    representation: Literal["openpyxl_data_only", "unavailable_openpyxl_type"] = (
        "openpyxl_data_only"
    )


@dataclass(frozen=True)
class SpreadsheetParseResult:
    """Bounded configured-row capture; this is not workbook completeness."""

    workbook_sha256: str
    workbook_bytes_size: int
    config_sha256: str
    max_quarters: int
    cells: tuple[SpreadsheetCellRead, ...]
    configured_name_order: tuple[str, ...]
    parser_name: Literal["ir_pipeline.spreadsheet"] = "ir_pipeline.spreadsheet"
    parser_version: Literal["source-handoff.v1"] = "source-handoff.v1"

    def selected_series(self) -> dict[str, dict[_dt.datetime, SpreadsheetCellRead]]:
        series: dict[str, dict[_dt.datetime, SpreadsheetCellRead]] = {
            name: {} for name in self.configured_name_order
        }
        for cell in self.cells:
            if cell.disposition == "selected" and cell.period_end is not None:
                series[cell.spec.kpi_name][cell.period_end] = cell
        return {
            name: {period: values[period] for period in sorted(values)}
            for name, values in series.items()
        }

    def numeric_projection(self) -> dict[str, dict[_dt.datetime, float]]:
        return {
            name: {
                period: cell.scaled_value
                for period, cell in series.items()
                if cell.scaled_value is not None
            }
            for name, series in self.selected_series().items()
        }


def parse_spreadsheet_source(
    path: Path, config: IrConfig, max_quarters: int = 8
) -> SpreadsheetParseResult:
    """Carry the exact parsed bytes and openpyxl cell context to the writer.

    Values retain the existing float(value) * scale operation. Data-only values
    are available Python values, not verbatim XML tokens or evaluated formulas.
    """
    raw = path.read_bytes()
    wb = openpyxl.load_workbook(BytesIO(raw), data_only=True, read_only=True)
    sheet_cache: dict[str, tuple[list[tuple[object, ...]], int, dict[int, _dt.datetime]]] = {}
    cells: list[SpreadsheetCellRead] = []
    winners: dict[str, dict[_dt.datetime, int]] = {}
    try:
        for spec in config.spreadsheet_kpis:
            if spec.sheet not in wb.sheetnames:
                cells.append(
                    SpreadsheetCellRead(spec, None, None, None, None, None, None, "sheet_missing")
                )
                continue
            if spec.sheet not in sheet_cache:
                rows = cast(
                    "list[tuple[object, ...]]",
                    [tuple(row) for row in wb[spec.sheet].iter_rows(values_only=True)],
                )
                header_index, col_map = _header_row(rows)
                sheet_cache[spec.sheet] = rows, header_index, col_map
            rows, header_index, col_map = sheet_cache[spec.sheet]
            if not col_map:
                cells.append(
                    SpreadsheetCellRead(spec, None, None, None, None, None, None, "header_missing")
                )
                continue
            date_cols = sorted(col_map)
            ri = _find_data_row(
                rows, config.label_col, spec.row_label, date_cols, exact=spec.exact_label
            )
            if ri is None:
                # Retain the first matching rejected row without selecting it.
                ri = next(
                    (
                        index
                        for index, row in enumerate(rows)
                        if config.label_col < len(row)
                        and _label_matches(
                            row[config.label_col],
                            spec.row_label.strip().lower(),
                            exact=spec.exact_label,
                        )
                    ),
                    None,
                )
            if ri is None:
                cells.append(
                    SpreadsheetCellRead(spec, None, None, None, None, None, None, "row_missing")
                )
                continue
            row = rows[ri]
            label = row[config.label_col]
            raw_label = label if isinstance(label, str) else None
            series: dict[_dt.datetime, int] = {}
            for ci in range(max(len(row), len(rows[header_index]))):
                val = row[ci] if ci < len(row) else None
                supported = val is None or isinstance(
                    val, (str, int, float, _dt.date, _dt.time, _dt.timedelta)
                )
                numeric = isinstance(val, (int, float)) and not isinstance(val, bool)
                if ci not in col_map and (ci == config.label_col or not numeric):
                    continue
                period = col_map.get(ci)
                scaled = (
                    float(val) * spec.scale if isinstance(val, (int, float)) and numeric else None
                )
                address = f"{get_column_letter(ci + 1)}{ri + 1}"
                header = SpreadsheetCellRef(
                    sheet_name=spec.sheet,
                    cell_address=f"{get_column_letter(ci + 1)}{header_index + 1}",
                )
                disposition: SpreadsheetCellDisposition = (
                    "unsupported_value"
                    if not supported
                    else "unknown_period"
                    if period is None
                    else "nonnumeric"
                    if not numeric
                    else "selected"
                )
                index = len(cells)
                cells.append(
                    SpreadsheetCellRead(
                        spec,
                        raw_label,
                        FactLocator(
                            kind=LocatorKind.SPREADSHEET_CELL,
                            spreadsheet_cell=SpreadsheetCellRef(
                                sheet_name=spec.sheet, cell_address=address
                            ),
                        ),
                        header,
                        period,
                        val
                        if val is None
                        or isinstance(val, (str, int, float, _dt.date, _dt.time, _dt.timedelta))
                        else None,
                        scaled,
                        disposition,
                        "openpyxl_data_only" if supported else "unavailable_openpyxl_type",
                    )
                )
                if period is not None and numeric:
                    if period in series:
                        prior = series[period]
                        cells[prior] = replace(cells[prior], disposition="overwritten_period")
                    series[period] = index
            if series:
                recent = sorted(series)[-max_quarters:]
                for period, index in series.items():
                    if period not in recent:
                        cells[index] = replace(cells[index], disposition="outside_window")
                for index in winners.get(spec.kpi_name, {}).values():
                    cells[index] = replace(cells[index], disposition="overwritten_configured_name")
                winners[spec.kpi_name] = {period: series[period] for period in recent}
    finally:
        wb.close()
    return SpreadsheetParseResult(
        workbook_sha256=hashlib.sha256(raw).hexdigest(),
        workbook_bytes_size=len(raw),
        config_sha256=payload_sha256(asdict(config)),
        max_quarters=max_quarters,
        cells=tuple(cells),
        configured_name_order=tuple(winners),
    )


def parse_spreadsheet(
    path: Path, config: IrConfig, max_quarters: int = 8
) -> dict[str, dict[_dt.datetime, float]]:
    """Compatibility numeric projection of the source-preserving parse."""
    return parse_spreadsheet_source(path, config, max_quarters).numeric_projection()


def enumerate_numeric_rows(path: Path, label_col: int = 1) -> list[SheetDataRow]:
    """Every labeled numeric data row across the workbook's dated sheets.

    The structured companion to ``config_builder.dump_sheet_structure`` (which
    renders the same scan as text for the LLM): only sheets with a detectable
    quarter-date header are walked, and only rows whose label cell is a non-empty
    string AND that carry numeric data in the date columns are returned. The
    capture config builder maps each of these to a ``SheetKpi`` — so the row set
    is deterministic (no LLM gate decides which rows survive). ``samples`` holds
    up to the two most-recent numeric cells for unit/scale inference.
    """
    wb = openpyxl.load_workbook(str(path), data_only=True, read_only=True)
    found: list[SheetDataRow] = []
    for sheet in wb.sheetnames:
        rows = cast(
            "list[tuple[object, ...]]",
            [tuple(r) for r in wb[sheet].iter_rows(values_only=True)],
        )
        _, col_map = _header_row(rows)
        if not col_map:
            continue
        recent = sorted(col_map)[-2:]
        for r in rows:
            lab = r[label_col] if label_col < len(r) else None
            if not (isinstance(lab, str) and lab.strip()):
                continue
            samples: list[float] = []
            for c in recent:
                cell = r[c] if c < len(r) else None
                if isinstance(cell, (int, float)) and not isinstance(cell, bool):
                    samples.append(float(cell))
            if samples:
                found.append(SheetDataRow(sheet=sheet, label=lab.strip(), samples=samples))
    wb.close()
    return found
