"""Tests for the DCF workbook lifecycle: seeder (build from scratch),
refresher (preserve INPUTS, recompute everything else), and the contract
that the resulting workbook is parseable by `workbook_reader`.

The big property under test: the user can edit Forecast.INPUTS cells in
Excel between refreshes, and those edits propagate through to the
`dcf_runs` row that feeds the briefs. We exercise this end-to-end by
simulating a user edit (Y1 growth bumped from auto-derived to 25%) and
checking that the refreshed Valuation reflects the new assumption.
"""

from __future__ import annotations

import json
from pathlib import Path

import openpyxl
import pytest
from openpyxl.worksheet.worksheet import Worksheet

from dcf import forecast, refresher, seeder
from dcf.workbook_reader import read_valuation

# A miniature FMP-shaped fixture. 8 quarters so we can compute TTM + prior TTM
# (for Y1 growth derivation).
_INCOME_RECORDS: list[dict[str, object]] = [
    {
        "fiscalYear": "2025",  # str variant — coercion test
        "period": "Q4",
        "date": "2025-12-31",
        "revenue": 1_100_000_000,
        "costOfRevenue": 440_000_000,
        "grossProfit": 660_000_000,
        "operatingIncome": 220_000_000,
        "netIncome": 165_000_000,
        "epsDiluted": 1.65,
        "weightedAverageShsOutDil": 100_000_000,
    },
    {
        "fiscalYear": 2025,
        "period": "Q3",
        "date": "2025-09-30",
        "revenue": 1_050_000_000,
        "operatingIncome": 200_000_000,
        "netIncome": 150_000_000,
        "weightedAverageShsOutDil": 100_000_000,
    },
    {
        "fiscalYear": 2025,
        "period": "Q2",
        "date": "2025-06-30",
        "revenue": 1_000_000_000,
        "weightedAverageShsOutDil": 100_000_000,
    },
    {
        "fiscalYear": 2025,
        "period": "Q1",
        "date": "2025-03-31",
        "revenue": 950_000_000,
        "weightedAverageShsOutDil": 100_000_000,
    },
    # Prior TTM — 4 quarters of 2024 at ~10% lower run-rate so derived Y1
    # growth lands near 10%.
    {"fiscalYear": 2024, "period": "Q4", "date": "2024-12-31", "revenue": 1_000_000_000},
    {"fiscalYear": 2024, "period": "Q3", "date": "2024-09-30", "revenue": 950_000_000},
    {"fiscalYear": 2024, "period": "Q2", "date": "2024-06-30", "revenue": 900_000_000},
    {"fiscalYear": 2024, "period": "Q1", "date": "2024-03-31", "revenue": 850_000_000},
]
_BALANCE_RECORDS: list[dict[str, object]] = [
    {
        "fiscalYear": 2025,
        "period": "Q4",
        "date": "2025-12-31",
        "totalAssets": 5_000_000_000,
        "totalStockholdersEquity": 3_000_000_000,
        "longTermDebt": 800_000_000,
    },
]
_CASHFLOW_RECORDS: list[dict[str, object]] = [
    {
        "fiscalYear": 2025,
        "period": "Q4",
        "date": "2025-12-31",
        "operatingCashFlow": 250_000_000,
        "capitalExpenditure": -50_000_000,
        "freeCashFlow": 200_000_000,
        "netIncome": 165_000_000,
    },
    {
        "fiscalYear": 2025,
        "period": "Q3",
        "date": "2025-09-30",
        "freeCashFlow": 180_000_000,
    },
    {
        "fiscalYear": 2025,
        "period": "Q2",
        "date": "2025-06-30",
        "freeCashFlow": 170_000_000,
    },
    {
        "fiscalYear": 2025,
        "period": "Q1",
        "date": "2025-03-31",
        "freeCashFlow": 150_000_000,
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


# ---------------------------------------------------------------------------
# Forecast module — derivation + projection math
# ---------------------------------------------------------------------------


def test_derive_initial_inputs_from_history() -> None:
    inputs = forecast.derive_initial_inputs(
        _INCOME_RECORDS,
        _CASHFLOW_RECORDS,
        terminal_growth_pct=0.025,
        forecast_years=5,
    )
    # TTM revenue (sum of first 4 quarters): 1100 + 1050 + 1000 + 950 = 4100M
    assert inputs.base_revenue_M == pytest.approx(4100.0, rel=1e-3)
    # Prior TTM: 1000 + 950 + 900 + 850 = 3700M -> growth 4100/3700 - 1 = 10.81%
    assert inputs.y1_growth_pct == pytest.approx(0.108, abs=0.005)
    # TTM FCF: 200 + 180 + 170 + 150 = 700M; margin = 700/4100 = 17.07%
    assert inputs.fcf_margin_pct == pytest.approx(0.171, abs=0.005)
    assert inputs.diluted_shares_M == pytest.approx(100.0)
    assert inputs.terminal_growth_pct == 0.025
    assert inputs.forecast_years == 5


def test_compute_projections_linear_decay() -> None:
    inputs = forecast.ForecastInputs(
        base_revenue_M=1000.0,
        y1_growth_pct=0.20,
        terminal_growth_pct=0.04,
        fcf_margin_pct=0.10,
        diluted_shares_M=100.0,
        forecast_years=5,
    )
    proj = forecast.compute_projections(inputs, base_year=2026)
    assert proj.years == [2027, 2028, 2029, 2030, 2031]
    # Year 1: 1000 * 1.20 = 1200; FCF = 120
    assert proj.revenue_M[0] == pytest.approx(1200.0)
    assert proj.fcf_M[0] == pytest.approx(120.0)
    # Year 5 growth = 4%, so revenue Y5 / revenue Y4 = 1.04 (terminal)
    assert proj.revenue_M[-1] / proj.revenue_M[-2] == pytest.approx(1.04, abs=1e-3)


def test_compute_projections_handles_negative_margin() -> None:
    """AMZN-style negative TTM FCF must not crash; produces negative projected FCF."""
    inputs = forecast.ForecastInputs(
        base_revenue_M=1000.0,
        y1_growth_pct=0.15,
        terminal_growth_pct=0.025,
        fcf_margin_pct=-0.05,
        diluted_shares_M=100.0,
        forecast_years=5,
    )
    proj = forecast.compute_projections(inputs, base_year=2026)
    assert all(fcf < 0 for fcf in proj.fcf_M)


# ---------------------------------------------------------------------------
# Seeder
# ---------------------------------------------------------------------------


def test_seeder_builds_three_sheets_from_scratch(
    tmp_path: Path, fmp_dir: Path
) -> None:
    out = tmp_path / "TEST.xlsx"
    seeder.seed_workbook("TEST", fmp_dir, out, base_year=2026)

    wb = openpyxl.load_workbook(str(out), data_only=True)
    assert wb.sheetnames == [
        seeder.HISTORICALS_SHEET,
        seeder.FORECAST_SHEET,
        seeder.VALUATION_SHEET,
    ]


def test_seeder_derives_forecast_inputs_from_history(
    tmp_path: Path, fmp_dir: Path
) -> None:
    out = tmp_path / "TEST.xlsx"
    seeder.seed_workbook("TEST", fmp_dir, out, base_year=2026)

    wb = openpyxl.load_workbook(str(out), data_only=True)
    inputs = forecast.read_inputs_from_sheet(wb[seeder.FORECAST_SHEET])
    # Same values as the standalone derive_initial_inputs test — confirms the
    # seeder is wiring the derivation correctly.
    assert inputs.base_revenue_M == pytest.approx(4100.0, rel=1e-3)
    assert inputs.y1_growth_pct == pytest.approx(0.108, abs=0.005)
    assert inputs.fcf_margin_pct == pytest.approx(0.171, abs=0.005)


def test_seeded_workbook_parses_via_workbook_reader(
    tmp_path: Path, fmp_dir: Path
) -> None:
    """The whole point: workbook_reader must read what the seeder writes,
    so the PV calc + dcf_runs persist + briefs chain stays unbroken."""
    out = tmp_path / "TEST.xlsx"
    seeder.seed_workbook("TEST", fmp_dir, out, base_year=2026)

    snap = read_valuation(out, valuation_year=2026)
    # Base year (2026) is the actual; 2027-2031 are forecasts.
    assert snap.latest_actual_year == 2026
    assert snap.forecast_years == [2027, 2028, 2029, 2030, 2031]
    # All forecast years carry numeric FCFs.
    assert all(isinstance(snap.fcf_by_year[y], float) for y in snap.forecast_years)
    # Diluted shares baseline is the user input.
    assert snap.shares_by_year[2026] == pytest.approx(100.0)


def test_seeder_refuses_to_overwrite_without_force(
    tmp_path: Path, fmp_dir: Path
) -> None:
    out = tmp_path / "TEST.xlsx"
    out.write_text("existing")
    with pytest.raises(seeder.SeederError, match="refusing to overwrite"):
        seeder.seed_workbook("TEST", fmp_dir, out)


def test_seeder_force_overwrites(tmp_path: Path, fmp_dir: Path) -> None:
    out = tmp_path / "TEST.xlsx"
    out.write_text("existing")
    seeder.seed_workbook("TEST", fmp_dir, out, force=True)
    assert out.read_bytes()[:2] == b"PK"


def test_seeder_missing_fmp_raises(tmp_path: Path) -> None:
    empty_fmp = tmp_path / "fmp"
    empty_fmp.mkdir()
    with pytest.raises(seeder.SeederError, match="no income statement records"):
        seeder.seed_workbook("NOPE", empty_fmp, tmp_path / "NOPE.xlsx")


# ---------------------------------------------------------------------------
# Refresher — INPUTS preservation contract
# ---------------------------------------------------------------------------


def test_refresh_preserves_user_edited_inputs(
    tmp_path: Path, fmp_dir: Path
) -> None:
    """The core user-edit contract: open the workbook in Excel, change a
    Forecast INPUT cell, save. Re-running the refresher must read that
    edited value back and use it (not re-derive from FMP)."""
    out = tmp_path / "TEST.xlsx"
    seeder.seed_workbook("TEST", fmp_dir, out, base_year=2026)

    # Simulate the user editing Y1 growth from auto-derived ~11% to 25%.
    wb = openpyxl.load_workbook(str(out))
    forecast_ws = wb[seeder.FORECAST_SHEET]
    for r in range(1, 20):
        if forecast_ws.cell(row=r, column=1).value == "Y1 Revenue Growth %":
            forecast_ws.cell(row=r, column=2, value=0.25)
            break
    wb.save(str(out))

    result = refresher.refresh_historicals(out, fmp_dir, ticker="TEST", base_year=2026)

    # The edited value MUST round-trip through the refresher.
    assert result.forecast_inputs.y1_growth_pct == pytest.approx(0.25)
    # And the projection MUST reflect it: Y1 revenue = 4100 * 1.25 = 5125; FCF
    # at ~17.1% margin ≈ 876.
    assert result.projections.revenue_M[0] == pytest.approx(5125.0, abs=1.0)
    assert result.projections.fcf_M[0] == pytest.approx(876.0, abs=10.0)


def test_refresh_updates_valuation_fcf(tmp_path: Path, fmp_dir: Path) -> None:
    """After a user-input edit, workbook_reader must see the new FCFs in
    Valuation — that's what feeds the PV calc and dcf_runs."""
    out = tmp_path / "TEST.xlsx"
    seeder.seed_workbook("TEST", fmp_dir, out, base_year=2026)

    fcf_before = read_valuation(out, valuation_year=2026).fcf_by_year[2027]

    # Bump FCF margin from ~17% to 30%.
    wb = openpyxl.load_workbook(str(out))
    forecast_ws = wb[seeder.FORECAST_SHEET]
    for r in range(1, 20):
        if forecast_ws.cell(row=r, column=1).value == "FCF Margin %":
            forecast_ws.cell(row=r, column=2, value=0.30)
            break
    wb.save(str(out))

    refresher.refresh_historicals(out, fmp_dir, ticker="TEST", base_year=2026)
    fcf_after = read_valuation(out, valuation_year=2026).fcf_by_year[2027]

    # Margin nearly doubled → 2027 FCF should be ~1.75x the original.
    assert fcf_after > fcf_before * 1.5


def test_refresh_recomputes_historicals_from_latest_fmp(
    tmp_path: Path, fmp_dir: Path
) -> None:
    """Historicals reflects current FMP, even if FMP changed between seed
    and refresh. Forecast INPUTS are NOT re-derived (preserve user edits)."""
    out = tmp_path / "TEST.xlsx"
    seeder.seed_workbook("TEST", fmp_dir, out, base_year=2026)

    # Mutate the FMP file's revenue for the newest quarter.
    income_path = fmp_dir / "TEST_income_statement_quarterly.json"
    income = json.loads(income_path.read_text())
    income[0]["revenue"] = 9_999_000_000
    income_path.write_text(json.dumps(income))

    refresher.refresh_historicals(out, fmp_dir, ticker="TEST", base_year=2026)

    wb = openpyxl.load_workbook(str(out), data_only=True)
    hist = wb[seeder.HISTORICALS_SHEET]
    revenue_row = _find_label_row(hist, "Revenue")
    # Find the Q4 2025 column — it's the rightmost quarter in our test data.
    q4_col = None
    for c in range(2, hist.max_column + 1):
        if hist.cell(row=1, column=c).value == "Q4 2025":
            q4_col = c
            break
    assert q4_col is not None
    assert hist.cell(row=revenue_row, column=q4_col).value == pytest.approx(9999.0)

    # INPUTS should NOT have changed — Base Revenue still reflects seed-time
    # TTM, not the post-refresh FMP value.
    inputs = forecast.read_inputs_from_sheet(wb[seeder.FORECAST_SHEET])
    assert inputs.base_revenue_M == pytest.approx(4100.0, rel=1e-3)


def test_refresh_is_idempotent_when_inputs_unchanged(
    tmp_path: Path, fmp_dir: Path
) -> None:
    out = tmp_path / "TEST.xlsx"
    seeder.seed_workbook("TEST", fmp_dir, out, base_year=2026)

    refresher.refresh_historicals(out, fmp_dir, ticker="TEST", base_year=2026)
    inputs_first = _read_inputs(out)
    fcf_first = read_valuation(out, valuation_year=2026).fcf_by_year[2027]

    refresher.refresh_historicals(out, fmp_dir, ticker="TEST", base_year=2026)
    inputs_second = _read_inputs(out)
    fcf_second = read_valuation(out, valuation_year=2026).fcf_by_year[2027]

    assert inputs_first == inputs_second
    assert fcf_first == fcf_second


def test_refresher_rejects_workbook_missing_forecast_sheet(
    tmp_path: Path, fmp_dir: Path
) -> None:
    dest = tmp_path / "TEST.xlsx"
    wb = openpyxl.Workbook()
    sheet = wb.active
    assert sheet is not None
    sheet.title = "Valuation"
    wb.save(str(dest))
    with pytest.raises(refresher.RefresherError, match="missing required sheets"):
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


def _read_inputs(path: Path) -> forecast.ForecastInputs:
    wb = openpyxl.load_workbook(str(path), data_only=True)
    inputs = forecast.read_inputs_from_sheet(wb[seeder.FORECAST_SHEET])
    wb.close()
    return inputs
