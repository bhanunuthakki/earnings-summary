"""Shared builders and helpers for the redesigned-DCF test files.

``tests/test_dcf_redesign.py`` (engine, builder and reader) and
``tests/test_dcf_redesign_refresh.py`` (refresh and apply-edits integration)
share these helpers. The two files are separate so ``pytest --dist=loadfile``
can run them on different workers.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import openpyxl
import pytest

from dcf import redesign
from tests.fixtures.dcf_fmp_payloads import write_canonical_statement_facts, write_fmp
from tests.kpi_semantic_support import admit_all_kpi_facts

PROJECT_ROOT = Path(__file__).resolve().parents[2]
BUILDER = PROJECT_ROOT / "execution" / "build_redesigned_dcf.py"
_schema_template: Path | None = None


def install_schema_template(
    migrated_db: Callable[..., Path], tmp_path_factory: pytest.TempPathFactory
) -> None:
    """Build the migrated schema once per test module for the fact seeders."""
    global _schema_template
    template = migrated_db(tmp_path_factory.mktemp("dcf_schema") / "template.sqlite")
    with sqlite3.connect(template) as conn:
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='fact_overrides'"
        ).fetchone()
    assert row is not None
    _schema_template = template


def schema_template() -> Path:
    assert _schema_template is not None
    return _schema_template


def build_usd(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, float]:
    """Build one USD single-segment workbook; read-only tests share it."""
    repo = tmp_path_factory.mktemp("redesign_usd")
    write_redesign_fmp(repo, "TESTCO")
    # Generic actuals must reconstruct without provider-shaped statement caches.
    for suffix in ("income_statement_quarterly", "balance_sheet_quarterly", "cash_flow_quarterly"):
        (repo / "data" / "historical" / "fmp" / f"TESTCO_{suffix}.json").unlink()
    dest = repo / "dcf" / "TESTCO.xlsx"
    builder_value = build_workbook(repo, "TESTCO", dest)
    return dest, builder_value


def build_semiannual(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, float]:
    """Build one semi-annual (Q2/Q4-only) workbook; reader tests share it."""
    repo = tmp_path_factory.mktemp("redesign_semi")
    write_fmp_semiannual(repo, "SEMICO")
    dest = repo / "dcf" / "SEMICO.xlsx"
    builder_value = build_workbook(repo, "SEMICO", dest)
    return dest, builder_value


def make_refresh_repo(tmp_path: Path) -> Path:
    """A repo_root with FMP, a dcf/ dir, and a dcf_runs DB — ready for refresh_one."""
    write_redesign_fmp(tmp_path, "TESTCO", segments=True)
    (tmp_path / "dcf").mkdir()
    return tmp_path


BUILDER = PROJECT_ROOT / "execution" / "build_redesigned_dcf.py"


REDESIGN_SHEETS = [
    "Cover",
    "Dashboard",
    "Assumptions",
    "Color Code",
    "WACC",
    "Model",
    "Financials",
    "Consensus",
    "Valuation",
    "Sensitivity",
    "Monte Carlo",
    "Statement Evidence",
]


BASE = redesign.RedesignInputs(
    segments=("Total company",),
    base_revenue_by_segment={"Total company": 1000.0},
    near_growth_by_segment={"Total company": 0.10},
    terminal_growth_by_segment={"Total company": 0.03},
    near_op_margin=0.20,
    terminal_op_margin=0.25,
    tax_rate=0.24,
    capex_2026_m=60.0,
    terminal_capex_da=1.05,
    da_ratio=0.05,
    consensus_years=5,
    wacc=0.09,
    beta=1.2,
    risk_free_rate=0.043,
    equity_risk_premium=0.045,
    cost_of_debt=0.045,
    terminal_method="Exit multiple",
    terminal_basis="EV/EBITDA",
    exit_multiple=12.0,
    terminal_growth_g=0.03,
    current_price=50.0,
    cash_m=100.0,
    total_debt_m=200.0,
    diluted_shares_m=100.0,
    fx_to_usd=1.0,
)


def write_statement_facts(
    repo: Path,
    ticker: str,
    income: list[dict[str, object]],
    balance: list[dict[str, object]],
    cashflow: list[dict[str, object]],
    *,
    currency: str,
    canonical_bindings: bool = True,
) -> None:
    write_canonical_statement_facts(
        schema_template(),
        repo,
        ticker,
        income,
        balance,
        cashflow,
        currency=currency,
        canonical_bindings=canonical_bindings,
    )


def write_redesign_fmp(
    repo: Path,
    ticker: str,
    *,
    currency: str = "USD",
    segments: bool = False,
    canonical_bindings: bool = True,
) -> None:
    """Delegate to the shared payload writer (tests/fixtures/dcf_fmp_payloads.py)."""
    write_fmp(
        schema_template(),
        repo,
        ticker,
        currency=currency,
        segments=segments,
        canonical_bindings=canonical_bindings,
    )


def write_fmp_semiannual(repo: Path, ticker: str) -> None:
    """Semi-annual filer (the BHP shape): each fiscal year reports only H1/H2 as
    Q2/Q4, which sum to the fiscal-year figure. Consensus is anchored just above the
    last full FY (its two halves) so near-term growth stays smooth and the
    value-of-record tracks the builder's mirror."""
    fmp = repo / "data" / "historical" / "fmp"
    fmp.mkdir(parents=True, exist_ok=True)
    inc: list[dict[str, object]] = []
    bal: list[dict[str, object]] = []
    cf: list[dict[str, object]] = []
    rev = 400.0
    for year in (2022, 2023, 2024, 2025):
        for q in ("Q2", "Q4"):  # only the two halves — never Q1/Q3
            rev *= 1.03
            inc.append(
                {
                    "fiscalYear": year,
                    "period": q,
                    "reportedCurrency": "USD",
                    "date": f"{year}-06-30",
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
                    "reportedCurrency": "USD",
                    "date": f"{year}-06-30",
                    "cashAndShortTermInvestments": rev * 0.30 * 1e6,
                    "totalCurrentAssets": rev * 0.60 * 1e6,
                    "propertyPlantEquipmentNet": rev * 0.50 * 1e6,
                    "totalAssets": rev * 1.50 * 1e6,
                    "totalCurrentLiabilities": rev * 0.30 * 1e6,
                    "longTermDebt": rev * 0.20 * 1e6,
                    "totalDebt": rev * 0.20 * 1e6,
                    "financeLeaseLiability": 0,
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
            [{"companyName": f"{ticker} Co", "beta": 1.0, "price": 50.0, "currency": "USD"}]
        ),
        encoding="utf-8",
    )
    (fmp / f"{ticker}_geo_segments_annual.json").write_text(
        json.dumps([{"fiscalYear": 2025, "period": "FY", "data": {"United States": rev}}]),
        encoding="utf-8",
    )
    # FY2025 actual = its two half-year revenues ($M); consensus continues ~8%/yr.
    base_fy_m = 400.0 * (1.03**7) + 400.0 * (1.03**8)  # the FY2025 Q2 + Q4 halves
    est = [
        {
            "date": f"{y}-06-30",
            "revenueAvg": base_fy_m * (1.08 ** (y - 2025)) * 1e6,
            "netIncomeAvg": base_fy_m * 0.09 * (1.08 ** (y - 2025)) * 1e6,
            "ebitdaAvg": base_fy_m * 0.20 * (1.08 ** (y - 2025)) * 1e6,
            "ebitAvg": base_fy_m * 0.12 * (1.08 ** (y - 2025)) * 1e6,
            "sgaExpenseAvg": base_fy_m * 0.15 * 1e6,
            "epsAvg": base_fy_m * 0.09 / 100 * (1.08 ** (y - 2025)),
        }
        for y in range(2026, 2031)
    ]
    (fmp / f"{ticker}_analyst_estimates_annual.json").write_text(json.dumps(est), encoding="utf-8")
    write_statement_facts(repo, ticker, inc, bal, cf, currency="USD")


def build_workbook(repo: Path, ticker: str, dest: Path) -> float:
    """Run the builder as a subprocess; return its value-of-record (RESULT line)."""
    env = dict(
        os.environ,
        DCF_TICKER=ticker,
        DCF_REPO_ROOT=str(repo),
        DCF_DEST=str(dest),
        EARNINGS_SUMMARY_DB_PATH=str(repo / "data" / "portfolio.db"),
    )
    proc = subprocess.run(
        [sys.executable, str(BUILDER)],
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    line = next((ln for ln in proc.stdout.splitlines() if ln.startswith("RESULT")), None)
    assert line is not None, proc.stdout
    return float(line.split("\t")[2])


def build_with_assumptions(
    repo: Path, ticker: str, redesign_block: dict[str, object], dest: Path
) -> float:
    """Write a data/dcf_assumptions/<T>.json redesign block, then run the builder."""
    adir = repo / "data" / "dcf_assumptions"
    adir.mkdir(parents=True, exist_ok=True)
    (adir / f"{ticker}.json").write_text(
        json.dumps({"ticker": ticker, "redesign": redesign_block}), encoding="utf-8"
    )
    return build_workbook(repo, ticker, dest)


GRID_CENTER = (8, 5)


class FakeLive:
    def __init__(self, price: float) -> None:
        self.price = price
        self.fetched_at = None


def fake_read(_repo: object, _ticker: object) -> FakeLive:
    return FakeLive(50.0)


def dashboard_cell(path: Path, row: int, col: int = 2) -> object:
    wb = openpyxl.load_workbook(str(path), data_only=False)
    try:
        return wb["Dashboard"].cell(row=row, column=col).value
    finally:
        wb.close()


KPI_SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL, source_type TEXT NOT NULL, doc_type TEXT NOT NULL,
    source_url TEXT, file_path TEXT NOT NULL, sha256 TEXT NOT NULL,
    fetched_at TIMESTAMP NOT NULL, fetch_status TEXT NOT NULL,
    raw_bytes_size INTEGER NOT NULL,
    source_quality_tier TEXT NOT NULL DEFAULT 'fmp_normalized'
);
CREATE TABLE IF NOT EXISTS kpi_definitions (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ticker TEXT NOT NULL, name TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS kpi_facts (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ticker TEXT NOT NULL,
    period_end TIMESTAMP NOT NULL, fiscal_period_type TEXT NOT NULL,
    kpi_definition_id INTEGER NOT NULL, value NUMERIC(24, 6) NOT NULL,
    unit TEXT NOT NULL, source_doc_id INTEGER NOT NULL
);
"""


def seed_kpi_fact(db: Path, ticker: str, name: str, value: float, unit: str) -> None:
    """Add the kpi/documents tables (the refresh DB only has dcf_runs) and a
    single latest-quarter KPI fact the inject route can resolve."""
    conn = sqlite3.connect(str(db))
    conn.executescript(KPI_SCHEMA)
    document = conn.execute(
        "INSERT INTO documents (ticker, source_type, doc_type, file_path, sha256, "
        "fetched_at, fetch_status, raw_bytes_size, source_quality_tier) VALUES "
        "(?, 'fmp', 'fmp_key_metrics', 'x', ?, '2026-01-01 00:00:00', 'ok', 1, "
        "'fmp_normalized')",
        (ticker, "0" * 64),
    )
    assert document.lastrowid is not None
    document_id = int(document.lastrowid)
    definition = conn.execute(
        "INSERT INTO kpi_definitions (ticker, name) VALUES (?, ?)", (ticker, name)
    )
    assert definition.lastrowid is not None
    definition_id = int(definition.lastrowid)
    conn.execute(
        "INSERT INTO kpi_facts (ticker, period_end, fiscal_period_type, kpi_definition_id, "
        "value, unit, source_doc_id) VALUES (?, '2025-09-30 00:00:00', 'Q3', ?, ?, ?, ?)",
        (ticker, definition_id, value, unit, document_id),
    )
    admit_all_kpi_facts(conn)
    conn.commit()
    conn.close()
