"""Smoke test for the redesigned-DCF builder (execution/build_redesigned_dcf.py).

Generates a minimal FMP fixture for a fake ticker, runs the builder as a
subprocess (the way the driver invokes it), and asserts the workbook has the
nine expected sheets, the headline value cell, the Dashboard dropdowns, and no
column-A label that accidentally became a formula (the leading-'=' bug).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import openpyxl

BUILDER = Path(__file__).resolve().parents[1] / "execution" / "build_redesigned_dcf.py"
SHEETS = [
    "Cover",
    "Dashboard",
    "Color Code",
    "WACC",
    "Model",
    "Financials",
    "Consensus",
    "Valuation",
    "Monte Carlo",
]


def _write_fixture(repo: Path, ticker: str) -> None:
    fmp = repo / "data" / "historical" / "fmp"
    fmp.mkdir(parents=True, exist_ok=True)
    inc, bal, cf = [], [], []
    rev = 250.0
    for year in (2022, 2023, 2024, 2025):
        for q in ("Q1", "Q2", "Q3", "Q4"):
            rev *= 1.03
            inc.append(
                {
                    "fiscalYear": year,
                    "period": q,
                    "reportedCurrency": "USD",
                    "date": f"{year}-03-31",
                    "revenue": rev * 1e6,
                    "costOfRevenue": rev * 0.50 * 1e6,
                    "grossProfit": rev * 0.50 * 1e6,
                    "researchAndDevelopmentExpenses": rev * 0.12 * 1e6,
                    "sellingGeneralAndAdministrativeExpenses": rev * 0.15 * 1e6,
                    "operatingExpenses": rev * 0.40 * 1e6,
                    "operatingIncome": rev * 0.12 * 1e6,
                    "netIncome": rev * 0.09 * 1e6,
                    "weightedAverageShsOutDil": 100 * 1e6,
                }
            )
            bal.append(
                {
                    "fiscalYear": year,
                    "period": q,
                    "cashAndShortTermInvestments": rev * 0.30 * 1e6,
                    "totalCurrentAssets": rev * 0.60 * 1e6,
                    "propertyPlantEquipmentNet": rev * 0.50 * 1e6,
                    "totalAssets": rev * 1.50 * 1e6,
                    "totalCurrentLiabilities": rev * 0.30 * 1e6,
                    "longTermDebt": rev * 0.20 * 1e6,
                    "totalStockholdersEquity": rev * 0.80 * 1e6,
                }
            )
            cf.append(
                {
                    "fiscalYear": year,
                    "period": q,
                    "depreciationAndAmortization": rev * 0.08 * 1e6,
                    "stockBasedCompensation": rev * 0.05 * 1e6,
                    "changeInWorkingCapital": -rev * 0.01 * 1e6,
                    "operatingCashFlow": rev * 0.15 * 1e6,
                    "capitalExpenditure": -rev * 0.10 * 1e6,
                    "freeCashFlow": rev * 0.05 * 1e6,
                }
            )
    (fmp / f"{ticker}_income_statement_quarterly.json").write_text(
        json.dumps(inc), encoding="utf-8"
    )
    (fmp / f"{ticker}_balance_sheet_quarterly.json").write_text(json.dumps(bal), encoding="utf-8")
    (fmp / f"{ticker}_cash_flow_quarterly.json").write_text(json.dumps(cf), encoding="utf-8")
    (fmp / f"{ticker}_profile.json").write_text(
        json.dumps(
            [
                {
                    "companyName": "Test Co",
                    "sector": "Tech",
                    "beta": 1.2,
                    "price": 50.0,
                    "currency": "USD",
                }
            ]
        ),
        encoding="utf-8",
    )
    est = [
        {
            "date": f"{y}-12-31",
            "revenueAvg": 1100 * (1.10 ** (y - 2026)) * 1e6,
            "netIncomeAvg": 120 * (1.10 ** (y - 2026)) * 1e6,
            "ebitdaAvg": 200 * 1e6,
            "ebitAvg": 150 * 1e6,
            "sgaExpenseAvg": 160 * 1e6,
            "epsAvg": 1.2 * (1.10 ** (y - 2026)),
        }
        for y in range(2026, 2031)
    ]
    (fmp / f"{ticker}_analyst_estimates_annual.json").write_text(json.dumps(est), encoding="utf-8")


def test_builder_produces_valid_nine_sheet_workbook(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _write_fixture(repo, "TESTCO")
    dest = tmp_path / "TESTCO.xlsx"
    env = dict(os.environ, DCF_TICKER="TESTCO", DCF_REPO_ROOT=str(repo), DCF_DEST=str(dest))
    proc = subprocess.run(
        [sys.executable, str(BUILDER)],
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.startswith("RESULT\tTESTCO"), proc.stdout
    assert dest.exists()

    wb = openpyxl.load_workbook(str(dest))
    assert wb.sheetnames == SHEETS

    # the headline value cell exists on the Valuation walk
    val = wb["Valuation"]
    assert any(
        str(val.cell(r, 1).value).strip() == "VALUE / SHARE" for r in range(1, val.max_row + 1)
    )

    # the two terminal dropdowns survived onto the Dashboard
    dv = {str(d.sqref) for d in wb["Dashboard"].data_validations.dataValidation}
    assert "B43" in dv and "B44" in dv

    # no column-A label accidentally became a formula (the leading-'=' bug) on the
    # text-label sheets (the Monte Carlo sheet legitimately has formula bin labels)
    for name in ("Cover", "Dashboard", "Valuation", "Model"):
        ws = wb[name]
        for row in ws.iter_rows(min_col=1, max_col=1):
            v = row[0].value
            assert not (isinstance(v, str) and v.startswith("=")), (
                f"label formula at {name}!{row[0].coordinate}: {v}"
            )
