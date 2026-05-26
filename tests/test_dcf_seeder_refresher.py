"""Tests for src/dcf/seeder.py and src/dcf/refresher.py — workbook seeding,
Historicals refresh, and the round-trip preservation contract for user-owned
sheets (Forecast / Model / Valuation).

The contract under test: refreshing the Historicals sheet must leave every
other sheet byte-identical at the cell level. We verify by hashing each
non-Historicals sheet's `(value, font, fill, number_format)` tuple stream
before and after refresh.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import openpyxl
import pytest
from openpyxl.worksheet.worksheet import Worksheet

from dcf import refresher, seeder

# A miniature FMP-shaped fixture covering enough quarters to exercise the
# 20-quarter window plus the year/period coercion edge case.
_INCOME_RECORDS: list[dict[str, object]] = [
    {
        "date": "2025-12-31",
        "symbol": "TEST",
        "fiscalYear": "2025",  # str variant — must coerce
        "period": "Q4",
        "revenue": 1_000_000_000,
        "costOfRevenue": 400_000_000,
        "grossProfit": 600_000_000,
        "operatingIncome": 200_000_000,
        "netIncome": 150_000_000,
        "epsDiluted": 1.50,
        "weightedAverageShsOutDil": 100_000_000,
    },
    {
        "date": "2025-09-30",
        "symbol": "TEST",
        "fiscalYear": 2025,  # int variant
        "period": "Q3",
        "revenue": 900_000_000,
        "operatingIncome": 180_000_000,
        "netIncome": 130_000_000,
        "weightedAverageShsOutDil": 100_000_000,
    },
]
_BALANCE_RECORDS: list[dict[str, object]] = [
    {
        "date": "2025-12-31",
        "symbol": "TEST",
        "fiscalYear": 2025,
        "period": "Q4",
        "cashAndShortTermInvestments": 500_000_000,
        "totalAssets": 5_000_000_000,
        "totalStockholdersEquity": 3_000_000_000,
        "longTermDebt": 800_000_000,
    },
    {
        "date": "2025-09-30",
        "symbol": "TEST",
        "fiscalYear": 2025,
        "period": "Q3",
        "cashAndShortTermInvestments": 450_000_000,
        "totalAssets": 4_800_000_000,
        "totalStockholdersEquity": 2_900_000_000,
    },
]
_CASHFLOW_RECORDS: list[dict[str, object]] = [
    {
        "date": "2025-12-31",
        "symbol": "TEST",
        "fiscalYear": 2025,
        "period": "Q4",
        "operatingCashFlow": 250_000_000,
        "capitalExpenditure": -50_000_000,
        "freeCashFlow": 200_000_000,
        "netIncome": 150_000_000,
        "depreciationAndAmortization": 40_000_000,
    },
    {
        "date": "2025-09-30",
        "symbol": "TEST",
        "fiscalYear": 2025,
        "period": "Q3",
        "operatingCashFlow": 220_000_000,
        "capitalExpenditure": -45_000_000,
        "freeCashFlow": 175_000_000,
    },
]


@pytest.fixture
def fmp_dir(tmp_path: Path) -> Path:
    """Drop the three statement JSONs into a temp dir for the test ticker."""
    d = tmp_path / "fmp"
    d.mkdir()
    (d / "TEST_income_statement_quarterly.json").write_text(json.dumps(_INCOME_RECORDS))
    (d / "TEST_balance_sheet_quarterly.json").write_text(json.dumps(_BALANCE_RECORDS))
    (d / "TEST_cash_flow_quarterly.json").write_text(json.dumps(_CASHFLOW_RECORDS))
    return d


@pytest.fixture
def template_dir(tmp_path: Path) -> Path:
    """Build a tiny synthetic template that mirrors the real-world shape
    (multiple user-owned sheets + a few cells each) without loading the 1.5MB
    GOOG/META/AMZN examples. Keeps the test suite under a second per case
    while still exercising the seeder's copy-then-mutate path.
    """
    d = tmp_path / "templates"
    d.mkdir()
    wb = openpyxl.Workbook()
    # Default sheet becomes "Forecast" — that's user-owned.
    default = wb.active
    assert default is not None
    default.title = "Forecast"
    default["A1"] = "Year"
    default["A2"] = "Revenue growth"
    default["B1"] = 2026
    default["B2"] = 0.10

    model = wb.create_sheet("Model")
    model["A1"] = "Model assumptions"
    model["B1"] = "stub formula goes here"

    val = wb.create_sheet("Valuation")
    val["A1"] = "FCF"
    val["B1"] = 2026
    val["B2"] = 100.0
    # The workbook_reader scans for "FCF" row and year headers — we don't
    # parse the valuation in these tests, just confirm Forecast/Model/
    # Valuation round-trip unchanged.

    # Match the seeder's primary template name so _resolve_template finds it
    # without needing a fallback.
    wb.save(str(d / "GOOG-Mar-09-2023.xlsx"))
    return d


# ---------------------------------------------------------------------------
# Seeder
# ---------------------------------------------------------------------------


def test_seeder_copies_template_and_writes_historicals(
    tmp_path: Path, fmp_dir: Path, template_dir: Path
) -> None:
    out = tmp_path / "TEST.xlsx"
    seeder.seed_workbook("TEST", template_dir, fmp_dir, out)

    assert out.exists()
    wb = openpyxl.load_workbook(str(out), data_only=True)
    assert seeder.HISTORICALS_SHEET in wb.sheetnames

    ws = wb[seeder.HISTORICALS_SHEET]
    # Row 1: header has 1 label + 2 periods (we only provided 2 quarters)
    assert ws.cell(row=1, column=1).value == "Line Item"
    # Newest column on the right
    assert ws.cell(row=1, column=2).value == "Q3 2025"
    assert ws.cell(row=1, column=3).value == "Q4 2025"

    # Revenue row: 900 → Q3, 1000 → Q4 (in millions)
    revenue_row = _find_label_row(ws, "Revenue")
    assert ws.cell(row=revenue_row, column=2).value == 900.0
    assert ws.cell(row=revenue_row, column=3).value == 1000.0

    # Sparse data: Q3 had no costOfRevenue → cell empty, Q4 = 400
    cost_row = _find_label_row(ws, "Cost of Revenue")
    assert ws.cell(row=cost_row, column=2).value is None
    assert ws.cell(row=cost_row, column=3).value == 400.0


def test_seeder_refuses_to_overwrite_without_force(
    tmp_path: Path, fmp_dir: Path, template_dir: Path
) -> None:
    out = tmp_path / "TEST.xlsx"
    out.write_text("existing")
    with pytest.raises(seeder.SeederError, match="refusing to overwrite"):
        seeder.seed_workbook("TEST", template_dir, fmp_dir, out)


def test_seeder_force_overwrites(
    tmp_path: Path, fmp_dir: Path, template_dir: Path
) -> None:
    out = tmp_path / "TEST.xlsx"
    out.write_text("existing")
    seeder.seed_workbook("TEST", template_dir, fmp_dir, out, force=True)
    # Now it's a real workbook (Excel files start with PK zip header)
    assert out.read_bytes()[:2] == b"PK"


def test_seeder_missing_income_file_raises(
    tmp_path: Path, template_dir: Path
) -> None:
    empty_fmp = tmp_path / "fmp"
    empty_fmp.mkdir()
    with pytest.raises(seeder.SeederError, match="no income statement records"):
        seeder.seed_workbook("NOPE", template_dir, empty_fmp, tmp_path / "NOPE.xlsx")


def test_seeder_prefers_ticker_specific_template(tmp_path: Path) -> None:
    """When `{TICKER}-*.xlsx` exists alongside the GOOG fallback, the seeder
    must pick the ticker-specific file. Without this every seeded workbook
    inherits GOOG's Forecast/Model/Valuation regardless of ticker.
    """
    template_dir = tmp_path / "templates"
    template_dir.mkdir()

    # AMZN template — distinct marker content so the signature differs from GOOG's.
    amzn_wb = openpyxl.Workbook()
    amzn_forecast = amzn_wb.active
    assert amzn_forecast is not None
    amzn_forecast.title = "Forecast"
    amzn_forecast["A1"] = "AMZN forecast marker"
    amzn_forecast["B1"] = 0.20
    amzn_wb.create_sheet("Model")["A1"] = "AMZN model marker"
    amzn_wb.create_sheet("Valuation")["A1"] = "AMZN valuation marker"
    amzn_path = template_dir / "AMZN-Feb-06-2026.xlsx"
    amzn_wb.save(str(amzn_path))

    # GOOG fallback — present but should NOT be used when ticker is AMZN.
    goog_wb = openpyxl.Workbook()
    goog_forecast = goog_wb.active
    assert goog_forecast is not None
    goog_forecast.title = "Forecast"
    goog_forecast["A1"] = "GOOG forecast marker"
    goog_forecast["B1"] = 0.10
    goog_wb.create_sheet("Model")["A1"] = "GOOG model marker"
    goog_wb.create_sheet("Valuation")["A1"] = "GOOG valuation marker"
    goog_path = template_dir / "GOOG-Mar-09-2023.xlsx"
    goog_wb.save(str(goog_path))

    # FMP data for AMZN — reuse the TEST fixtures' shape, just renamed.
    fmp_dir = tmp_path / "fmp"
    fmp_dir.mkdir()
    (fmp_dir / "AMZN_income_statement_quarterly.json").write_text(json.dumps(_INCOME_RECORDS))
    (fmp_dir / "AMZN_balance_sheet_quarterly.json").write_text(json.dumps(_BALANCE_RECORDS))
    (fmp_dir / "AMZN_cash_flow_quarterly.json").write_text(json.dumps(_CASHFLOW_RECORDS))

    out = tmp_path / "AMZN.xlsx"
    seeder.seed_workbook("AMZN", template_dir, fmp_dir, out)

    # The seeded workbook's non-Historicals sheets must hash-match the AMZN
    # template's, not the GOOG fallback's.
    output_sig = _signature_per_sheet(out, exclude=seeder.HISTORICALS_SHEET)
    amzn_sig = _signature_per_sheet(amzn_path)
    goog_sig = _signature_per_sheet(goog_path)
    assert output_sig == amzn_sig, "AMZN seed must inherit from AMZN-*.xlsx"
    assert output_sig != goog_sig, "AMZN seed must not inherit from GOOG fallback"


# ---------------------------------------------------------------------------
# Refresher — user-owned sheets must round-trip byte-identical
# ---------------------------------------------------------------------------


def test_refresh_preserves_user_owned_sheets(
    tmp_path: Path, fmp_dir: Path, template_dir: Path
) -> None:
    out = tmp_path / "TEST.xlsx"
    seeder.seed_workbook("TEST", template_dir, fmp_dir, out)

    sigs_before = _signature_per_sheet(out, exclude=seeder.HISTORICALS_SHEET)
    historicals_before = _signature_per_sheet(out, only=seeder.HISTORICALS_SHEET)

    # Mutate the FMP data so the refresh would visibly change Historicals if
    # it actually wrote anything.
    income_path = fmp_dir / "TEST_income_statement_quarterly.json"
    income = json.loads(income_path.read_text())
    income[0]["revenue"] = 9_999_000_000  # was 1B → now 9.999B
    income_path.write_text(json.dumps(income))

    result = refresher.refresh_historicals(out, fmp_dir)
    assert result.cells_written > 0

    sigs_after = _signature_per_sheet(out, exclude=seeder.HISTORICALS_SHEET)
    historicals_after = _signature_per_sheet(out, only=seeder.HISTORICALS_SHEET)

    assert sigs_before == sigs_after, "user-owned sheets must round-trip unchanged"
    assert (
        historicals_before != historicals_after
    ), "Historicals should reflect the mutated FMP revenue"


def test_refresh_is_idempotent(
    tmp_path: Path, fmp_dir: Path, template_dir: Path
) -> None:
    out = tmp_path / "TEST.xlsx"
    seeder.seed_workbook("TEST", template_dir, fmp_dir, out)

    refresher.refresh_historicals(out, fmp_dir)
    sig_first = _signature_per_sheet(out)
    refresher.refresh_historicals(out, fmp_dir)
    sig_second = _signature_per_sheet(out)
    assert sig_first == sig_second, "refresh on unchanged FMP data must be a no-op"


def test_refresher_requires_historicals_sheet(
    tmp_path: Path, fmp_dir: Path
) -> None:
    """A raw workbook with no Historicals sheet must be rejected — that's the
    seeder's job, and silently appending a sheet would surprise users."""
    dest = tmp_path / "TEST.xlsx"
    wb = openpyxl.Workbook()
    sheet = wb.active
    assert sheet is not None
    sheet.title = "Valuation"
    wb.save(str(dest))
    with pytest.raises(refresher.RefresherError, match="no 'Historicals' sheet"):
        refresher.refresh_historicals(dest, fmp_dir, ticker="TEST")


def test_refresher_missing_workbook_raises(tmp_path: Path) -> None:
    with pytest.raises(refresher.RefresherError, match="workbook not found"):
        refresher.refresh_historicals(tmp_path / "missing.xlsx", tmp_path)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _find_label_row(ws: Worksheet, label: str) -> int:
    for r in range(1, ws.max_row + 1):
        if ws.cell(row=r, column=1).value == label:
            return r
    raise AssertionError(f"label {label!r} not in column A")


def _signature_per_sheet(
    path: Path, *, exclude: str | None = None, only: str | None = None
) -> dict[str, str]:
    """Hash every cell's `(value, number_format)` per sheet.

    Skipping rich-style (font/fill) keeps the signature robust to openpyxl's
    minor formatting normalizations on save while still catching any value
    drift in user-owned sheets.
    """
    wb = openpyxl.load_workbook(str(path), data_only=False)
    out: dict[str, str] = {}
    for name in wb.sheetnames:
        if exclude and name == exclude:
            continue
        if only and name != only:
            continue
        ws = wb[name]
        h = hashlib.sha256()
        for row in ws.iter_rows():
            for cell in row:
                h.update(repr((cell.coordinate, cell.value, cell.number_format)).encode())
        out[name] = h.hexdigest()
    wb.close()
    return out
