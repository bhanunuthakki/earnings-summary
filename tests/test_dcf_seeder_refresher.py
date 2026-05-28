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
import sqlite3
from pathlib import Path

import openpyxl
import pytest
from openpyxl.worksheet.worksheet import Worksheet

from dcf import forecast, refresher, seeder
from dcf.workbook_reader import read_valuation

# A miniature FMP-shaped fixture. 8 quarters so we can compute TTM + prior TTM
# (for Y1 growth derivation). The TTM quarters (first 4) carry the operating
# margin / capex / tax fields the FCF decomposition needs.
#
# TTM sums (for assertion math):
#   revenue           = 4_100M
#   operatingIncome   =   760M  → op_margin ≈ 18.5%
#   incomeTaxExpense  =   237M
#   incomeBeforeTax   =   950M  → tax_rate ≈ 24.9%
#   capitalExpenditure = -190M  → capex_intensity ≈ 4.6%
_INCOME_RECORDS: list[dict[str, object]] = [
    {
        "fiscalYear": "2025",  # str variant — coercion test
        "period": "Q4",
        "date": "2025-12-31",
        "revenue": 1_100_000_000,
        "costOfRevenue": 440_000_000,
        "grossProfit": 660_000_000,
        "operatingIncome": 220_000_000,
        "incomeBeforeTax": 280_000_000,
        "incomeTaxExpense": 70_000_000,
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
        "incomeBeforeTax": 250_000_000,
        "incomeTaxExpense": 62_000_000,
        "netIncome": 150_000_000,
        "weightedAverageShsOutDil": 100_000_000,
    },
    {
        "fiscalYear": 2025,
        "period": "Q2",
        "date": "2025-06-30",
        "revenue": 1_000_000_000,
        "operatingIncome": 180_000_000,
        "incomeBeforeTax": 220_000_000,
        "incomeTaxExpense": 55_000_000,
        "weightedAverageShsOutDil": 100_000_000,
    },
    {
        "fiscalYear": 2025,
        "period": "Q1",
        "date": "2025-03-31",
        "revenue": 950_000_000,
        "operatingIncome": 160_000_000,
        "incomeBeforeTax": 200_000_000,
        "incomeTaxExpense": 50_000_000,
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
        "capitalExpenditure": -55_000_000,
        "freeCashFlow": 180_000_000,
    },
    {
        "fiscalYear": 2025,
        "period": "Q2",
        "date": "2025-06-30",
        "capitalExpenditure": -45_000_000,
        "freeCashFlow": 170_000_000,
    },
    {
        "fiscalYear": 2025,
        "period": "Q1",
        "date": "2025-03-31",
        "capitalExpenditure": -40_000_000,
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


def _facts_db_fy(
    tmp_path: Path,
    ticker: str,
    line_items_by_year: dict[str, dict[int, float]],
    *,
    tier: str = "s1_provisional",
) -> Path:
    """Build a portfolio.db with documents + annual (FY) financial_facts rows.

    Mirrors the shape the S-1 extractor writes: all facts point at one
    `s1_provisional`-tier document (the lowest precedence). `line_items_by_year`
    maps a canonical line_item to {fiscal_year: value}; each value lands on a
    Dec-31 FY period. Exercises the no-FMP fallback the loader reads from.
    """
    p = tmp_path / "portfolio.db"
    conn = sqlite3.connect(str(p))
    try:
        conn.executescript(
            """
            CREATE TABLE documents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_quality_tier VARCHAR,
                fetched_at DATETIME
            );
            CREATE TABLE financial_facts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker VARCHAR NOT NULL,
                period_end DATETIME NOT NULL,
                fiscal_period_type VARCHAR NOT NULL,
                line_item VARCHAR NOT NULL,
                value NUMERIC(24,6) NOT NULL,
                currency VARCHAR(3),
                unit VARCHAR NOT NULL,
                source_doc_id INTEGER NOT NULL,
                confidence FLOAT NOT NULL DEFAULT 1.0
            );
            """
        )
        conn.execute(
            "INSERT INTO documents(id, source_quality_tier, fetched_at) VALUES (1, ?, ?)",
            (tier, "2026-05-01 00:00:00"),
        )
        for line_item, by_year in line_items_by_year.items():
            for year, value in by_year.items():
                conn.execute(
                    "INSERT INTO financial_facts(ticker, period_end, fiscal_period_type, "
                    "line_item, value, unit, source_doc_id) VALUES (?,?,?,?,?,?,1)",
                    (ticker, f"{year}-12-31 00:00:00", "FY", line_item, float(value), "actual"),
                )
        conn.commit()
    finally:
        conn.close()
    return p


# Two fiscal years of S-1-shaped annuals (actual dollars; EPS per-share; shares
# as a raw count) — modeled on FRVO's S-1 figures.
_S1_FACTS: dict[str, dict[int, float]] = {
    "revenue": {2024: 199_000.0, 2025: 138_000.0},
    "operating_income": {2024: -41_838_000.0, 2025: -48_806_000.0},
    "net_income": {2024: -41_110_000.0, 2025: -57_788_000.0},
    "eps_diluted": {2024: -3.31, 2025: -5.66},
    "weighted_avg_shares_diluted": {2024: 12_438_000.0, 2025: 12_462_000.0},
    "total_assets": {2024: 531_299_000.0, 2025: 1_365_168_000.0},
    "long_term_debt": {2024: 39_019_000.0, 2025: 172_837_000.0},
    "capital_expenditure": {2024: -178_693_000.0, 2025: -465_659_000.0},
    "free_cash_flow": {2024: -233_441_000.0, 2025: -497_416_000.0},
}


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
    # TTM op income: 220 + 200 + 180 + 160 = 760M; op margin = 760/4100 = 18.54%
    assert inputs.y1_operating_margin_pct == pytest.approx(0.1854, abs=0.001)
    # Y5 op margin = max(0.1854 + 0.02, 0.15) = 0.2054
    assert inputs.y5_operating_margin_pct == pytest.approx(0.2054, abs=0.001)
    # TTM capex: 50 + 55 + 45 + 40 = 190M; intensity = 190/4100 = 4.63%
    assert inputs.y1_capex_intensity_pct == pytest.approx(0.0463, abs=0.001)
    # Y5 capex intensity = 6% (default mature-steady-state)
    assert inputs.y5_capex_intensity_pct == pytest.approx(0.06)
    # TTM pretax: 280 + 250 + 220 + 200 = 950M; tax: 70 + 62 + 55 + 50 = 237M
    # → tax rate = 237/950 = 24.95% (inside [0.15, 0.35] clamp)
    assert inputs.tax_rate_pct == pytest.approx(0.2495, abs=0.001)
    assert inputs.diluted_shares_M == pytest.approx(100.0)
    assert inputs.terminal_growth_pct == 0.025
    assert inputs.forecast_years == 5


def test_compute_projections_linear_decay() -> None:
    inputs = forecast.ForecastInputs(
        base_revenue_M=1000.0,
        y1_growth_pct=0.20,
        terminal_growth_pct=0.04,
        y1_operating_margin_pct=0.10,
        y5_operating_margin_pct=0.20,
        y1_capex_intensity_pct=0.08,
        y5_capex_intensity_pct=0.04,
        tax_rate_pct=0.25,
        diluted_shares_M=100.0,
        forecast_years=5,
    )
    proj = forecast.compute_projections(inputs, base_year=2026)
    assert proj.years == [2027, 2028, 2029, 2030, 2031]
    # Year 1: 1000 * 1.20 = 1200
    assert proj.revenue_M[0] == pytest.approx(1200.0)
    # Year 5 growth = 4%, so revenue Y5 / revenue Y4 = 1.04 (terminal)
    assert proj.revenue_M[-1] / proj.revenue_M[-2] == pytest.approx(1.04, abs=1e-3)
    # Op margin ramps linearly from 0.10 → 0.20 across 5 years
    # → [0.10, 0.125, 0.15, 0.175, 0.20]
    assert proj.operating_margin_pct == pytest.approx([0.10, 0.125, 0.15, 0.175, 0.20])
    # Capex intensity ramps linearly from 0.08 → 0.04
    # → [0.08, 0.07, 0.06, 0.05, 0.04]
    assert proj.capex_intensity_pct == pytest.approx([0.08, 0.07, 0.06, 0.05, 0.04])
    # Year 1 FCF = 1200 * 0.10 * 0.75 - 1200 * 0.08 = 90 - 96 = -6
    assert proj.fcf_M[0] == pytest.approx(-6.0)
    # Year 5 FCF margin = 0.20 * 0.75 - 0.04 = 0.11 — positive terminal year
    assert proj.fcf_M[-1] == pytest.approx(proj.revenue_M[-1] * 0.11, rel=1e-6)


def test_amzn_style_negative_y1_fcf_normalizes_to_positive_y5() -> None:
    """The whole point of the decomposition: a heavy-capex Y1 (AMZN's AI
    buildout, BN's lumpy quarters) must still produce a positive Y5 FCF so
    the terminal value calc doesn't blow the DCF."""
    inputs = forecast.ForecastInputs(
        base_revenue_M=600_000.0,  # AMZN-ish TTM revenue, USD millions
        y1_growth_pct=0.10,
        terminal_growth_pct=0.025,
        y1_operating_margin_pct=0.10,  # depressed by AI capex amortization
        y5_operating_margin_pct=0.15,  # mature-business floor
        y1_capex_intensity_pct=0.20,  # elevated by AI buildout
        y5_capex_intensity_pct=0.06,  # normalized
        tax_rate_pct=0.25,
        diluted_shares_M=10_500.0,
        forecast_years=5,
    )
    proj = forecast.compute_projections(inputs, base_year=2026)
    # Y1 FCF margin = 0.10*0.75 - 0.20 = -12.5%  → negative
    assert proj.fcf_M[0] < 0
    # Y5 FCF margin = 0.15*0.75 - 0.06 = 5.25%  → positive (the whole point)
    assert proj.fcf_M[-1] > 0
    # Y5 FCF as % of Y5 revenue should be ~5.25%
    assert proj.fcf_M[-1] / proj.revenue_M[-1] == pytest.approx(0.0525, abs=0.001)


# ---------------------------------------------------------------------------
# Seeder
# ---------------------------------------------------------------------------


def test_seeder_builds_three_sheets_from_scratch(tmp_path: Path, fmp_dir: Path) -> None:
    out = tmp_path / "TEST.xlsx"
    seeder.seed_workbook("TEST", fmp_dir, out, base_year=2026)

    wb = openpyxl.load_workbook(str(out), data_only=True)
    assert wb.sheetnames == [
        seeder.HISTORICALS_SHEET,
        seeder.FORECAST_SHEET,
        seeder.VALUATION_SHEET,
    ]


def test_seeder_derives_forecast_inputs_from_history(tmp_path: Path, fmp_dir: Path) -> None:
    out = tmp_path / "TEST.xlsx"
    seeder.seed_workbook("TEST", fmp_dir, out, base_year=2026)

    wb = openpyxl.load_workbook(str(out), data_only=True)
    inputs = forecast.read_inputs_from_sheet(wb[seeder.FORECAST_SHEET])
    # Same values as the standalone derive_initial_inputs test — confirms the
    # seeder is wiring the derivation correctly.
    assert inputs.base_revenue_M == pytest.approx(4100.0, rel=1e-3)
    assert inputs.y1_growth_pct == pytest.approx(0.108, abs=0.005)
    assert inputs.y1_operating_margin_pct == pytest.approx(0.1854, abs=0.001)
    assert inputs.y5_operating_margin_pct == pytest.approx(0.2054, abs=0.001)
    assert inputs.y1_capex_intensity_pct == pytest.approx(0.0463, abs=0.001)
    assert inputs.y5_capex_intensity_pct == pytest.approx(0.06)
    assert inputs.tax_rate_pct == pytest.approx(0.2495, abs=0.001)


def test_seeded_workbook_parses_via_workbook_reader(tmp_path: Path, fmp_dir: Path) -> None:
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


def test_seeder_refuses_to_overwrite_without_force(tmp_path: Path, fmp_dir: Path) -> None:
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
# Seeder — financial_facts fallback (recently-IPO'd / S-1-anchored tickers)
# ---------------------------------------------------------------------------


def test_seeder_falls_back_to_financial_facts_when_fmp_absent(
    tmp_path: Path,
) -> None:
    """No FMP quarterly files → seed Historicals from annual financial_facts.

    One column per fiscal year; values scaled to $M (EPS unscaled, shares in
    millions). This is the S-1-anchored path the whole change exists for."""
    db = _facts_db_fy(tmp_path, "S1CO", _S1_FACTS)
    empty_fmp = tmp_path / "fmp"
    empty_fmp.mkdir()
    out = tmp_path / "S1CO.xlsx"

    seeder.seed_workbook("S1CO", empty_fmp, out, base_year=2026, db_path=db)

    wb = openpyxl.load_workbook(str(out), data_only=True)
    assert wb.sheetnames == [
        seeder.HISTORICALS_SHEET,
        seeder.FORECAST_SHEET,
        seeder.VALUATION_SHEET,
    ]
    hist = wb[seeder.HISTORICALS_SHEET]
    headers = [hist.cell(row=1, column=c).value for c in range(2, hist.max_column + 1)]
    assert headers == ["FY2024", "FY2025"]  # one column per fiscal year, oldest first

    rev_row = _find_label_row(hist, "Revenue")
    assert hist.cell(row=rev_row, column=2).value == pytest.approx(0.199)  # 199k → $M
    assert hist.cell(row=rev_row, column=3).value == pytest.approx(0.138)
    fcf_row = _find_label_row(hist, "Free Cash Flow")
    assert hist.cell(row=fcf_row, column=3).value == pytest.approx(-497.416)
    eps_row = _find_label_row(hist, "Diluted EPS")
    assert hist.cell(row=eps_row, column=3).value == pytest.approx(-5.66)  # per-share, unscaled
    shares_row = _find_label_row(hist, "Diluted Shares (M)")
    assert hist.cell(row=shares_row, column=3).value == pytest.approx(12.462)  # count → M


def test_seeder_fallback_derives_forecast_inputs_from_annuals(
    tmp_path: Path,
) -> None:
    """Forecast INPUTS come off the annuals: base revenue + shares from the
    latest FY, Y1 growth from the YoY of the two fiscal years (via the TTM-window
    padding)."""
    db = _facts_db_fy(tmp_path, "S1CO", _S1_FACTS)
    empty_fmp = tmp_path / "fmp"
    empty_fmp.mkdir()
    out = tmp_path / "S1CO.xlsx"

    seeder.seed_workbook("S1CO", empty_fmp, out, base_year=2026, db_path=db)

    wb = openpyxl.load_workbook(str(out), data_only=True)
    inputs = forecast.read_inputs_from_sheet(wb[seeder.FORECAST_SHEET])
    assert inputs.base_revenue_M == pytest.approx(0.14, abs=0.005)  # latest FY 138k
    assert inputs.diluted_shares_M == pytest.approx(12.46, abs=0.01)  # latest FY
    # Y1 growth = YoY of the two fiscal years: 138k / 199k - 1.
    assert inputs.y1_growth_pct == pytest.approx(138_000.0 / 199_000.0 - 1, abs=0.001)


def test_seeder_prefers_fmp_when_both_fmp_and_facts_present(tmp_path: Path, fmp_dir: Path) -> None:
    """FMP quarterly files are the primary source — facts are only a fallback.
    When both exist, the seeder uses FMP (quarterly columns, not FY)."""
    db = _facts_db_fy(tmp_path, "TEST", {"revenue": {2024: 1.0, 2025: 2.0}})
    out = tmp_path / "TEST.xlsx"

    seeder.seed_workbook("TEST", fmp_dir, out, base_year=2026, db_path=db)

    wb = openpyxl.load_workbook(str(out), data_only=True)
    hist = wb[seeder.HISTORICALS_SHEET]
    headers = [hist.cell(row=1, column=c).value for c in range(2, hist.max_column + 1)]
    assert "Q4 2025" in headers  # FMP quarterly path
    assert "FY2025" not in headers


def test_seeder_raises_when_db_has_no_rows_for_ticker(tmp_path: Path) -> None:
    """db_path given but no FY facts for THIS ticker → still raises (no silent
    empty workbook)."""
    db = _facts_db_fy(tmp_path, "OTHER", {"revenue": {2025: 1.0}})
    empty_fmp = tmp_path / "fmp"
    empty_fmp.mkdir()
    with pytest.raises(seeder.SeederError, match="no income statement records"):
        seeder.seed_workbook("MISSING", empty_fmp, tmp_path / "MISSING.xlsx", db_path=db)


def test_refresh_uses_financial_facts_fallback(tmp_path: Path) -> None:
    """The refresh path also falls back to facts: re-running on an existing
    S-1-seeded workbook (no FMP) re-writes Historicals and preserves INPUTS."""
    db = _facts_db_fy(tmp_path, "S1CO", _S1_FACTS)
    empty_fmp = tmp_path / "fmp"
    empty_fmp.mkdir()
    out = tmp_path / "S1CO.xlsx"
    seeder.seed_workbook("S1CO", empty_fmp, out, base_year=2026, db_path=db)

    # Simulate a user edit, then refresh via the facts fallback.
    wb = openpyxl.load_workbook(str(out))
    fws = wb[seeder.FORECAST_SHEET]
    for r in range(1, 25):
        if fws.cell(row=r, column=1).value == "Y1 Revenue Growth %":
            fws.cell(row=r, column=2, value=0.5)
            break
    wb.save(str(out))

    result = refresher.refresh_historicals(
        out, empty_fmp, ticker="S1CO", base_year=2026, db_path=db
    )
    assert result.historicals_cells_written > 0
    assert result.forecast_inputs.y1_growth_pct == pytest.approx(0.5)  # edit preserved

    wb2 = openpyxl.load_workbook(str(out), data_only=True)
    hist = wb2[seeder.HISTORICALS_SHEET]
    headers = [hist.cell(row=1, column=c).value for c in range(2, hist.max_column + 1)]
    assert headers == ["FY2024", "FY2025"]


# ---------------------------------------------------------------------------
# Refresher — INPUTS preservation contract
# ---------------------------------------------------------------------------


def test_refresh_preserves_user_edited_inputs(tmp_path: Path, fmp_dir: Path) -> None:
    """The core user-edit contract: open the workbook in Excel, change Forecast
    INPUT cells, save. Re-running the refresher must read those edited values
    back and use them (not re-derive from FMP). We edit one legacy field
    (Y1 growth) and a couple of the new driver fields (Y5 Op Margin, Tax Rate)
    so the contract covers both."""
    out = tmp_path / "TEST.xlsx"
    seeder.seed_workbook("TEST", fmp_dir, out, base_year=2026)

    # Simulate the user editing several inputs in Excel.
    edits = {
        "Y1 Revenue Growth %": 0.25,
        "Y5 Operating Margin %": 0.30,
        "Tax Rate %": 0.20,
    }
    wb = openpyxl.load_workbook(str(out))
    forecast_ws = wb[seeder.FORECAST_SHEET]
    for r in range(1, 25):
        label = forecast_ws.cell(row=r, column=1).value
        if isinstance(label, str) and label in edits:
            forecast_ws.cell(row=r, column=2, value=edits[label])
    wb.save(str(out))

    result = refresher.refresh_historicals(out, fmp_dir, ticker="TEST", base_year=2026)

    # All edited values MUST round-trip through the refresher.
    assert result.forecast_inputs.y1_growth_pct == pytest.approx(0.25)
    assert result.forecast_inputs.y5_operating_margin_pct == pytest.approx(0.30)
    assert result.forecast_inputs.tax_rate_pct == pytest.approx(0.20)
    # And the projection MUST reflect the growth edit: Y1 revenue = 4100 * 1.25 = 5125.
    assert result.projections.revenue_M[0] == pytest.approx(5125.0, abs=1.0)
    # Y1 FCF uses the auto-derived Y1 op_margin (18.54%) and capex (4.63%) with
    # the edited tax rate (20%):
    #   5125 * 0.1854 * 0.80 - 5125 * 0.0463 ≈ 760 - 237 = 523
    assert result.projections.fcf_M[0] == pytest.approx(523.0, abs=15.0)
    # The Y5 op-margin edit should bend the operating-margin ramp upward — the
    # last forecast year should land at the edited Y5 value (0.30), not the
    # auto-derived 0.2054.
    assert result.projections.operating_margin_pct[-1] == pytest.approx(0.30)


def test_refresh_updates_valuation_fcf(tmp_path: Path, fmp_dir: Path) -> None:
    """After a user-input edit, workbook_reader must see the new FCFs in
    Valuation — that's what feeds the PV calc and dcf_runs."""
    out = tmp_path / "TEST.xlsx"
    seeder.seed_workbook("TEST", fmp_dir, out, base_year=2026)

    fcf_before = read_valuation(out, valuation_year=2026).fcf_by_year[2027]

    # Bump Y1 operating margin from auto-derived ~18.5% to 40% — Y1 after-tax
    # operating income roughly doubles → Y1 FCF should jump well above 1.5x.
    wb = openpyxl.load_workbook(str(out))
    forecast_ws = wb[seeder.FORECAST_SHEET]
    for r in range(1, 25):
        if forecast_ws.cell(row=r, column=1).value == "Y1 Operating Margin %":
            forecast_ws.cell(row=r, column=2, value=0.40)
            break
    wb.save(str(out))

    refresher.refresh_historicals(out, fmp_dir, ticker="TEST", base_year=2026)
    fcf_after = read_valuation(out, valuation_year=2026).fcf_by_year[2027]

    # Y1 op margin more than doubled → 2027 FCF should jump well above 1.5x.
    assert fcf_after > fcf_before * 1.5


def test_refresh_recomputes_historicals_from_latest_fmp(tmp_path: Path, fmp_dir: Path) -> None:
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


def test_refresh_is_idempotent_when_inputs_unchanged(tmp_path: Path, fmp_dir: Path) -> None:
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


def test_refresher_rejects_workbook_missing_forecast_sheet(tmp_path: Path, fmp_dir: Path) -> None:
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
