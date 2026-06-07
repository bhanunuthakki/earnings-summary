"""Tests for the redesigned 9-sheet DCF: the reader/projection/value engine
(``src/dcf/redesign.py``) and the redesign refresh path in
``execution/refresh_dcf.py`` (rebuild-from-FMP with Dashboard edit-preservation).

Three layers:
  * Pure engine — construct ``RedesignInputs`` directly and assert the projection
    math (growth monotonicity, FX scaling, perpetuity terminal, the WACC>g guard).
    No workbook, no subprocess.
  * Reader — run the real builder as a subprocess (the way the driver/refresher
    do), then read it back: format detection, value-of-record parity with the
    builder's own ``_project`` mirror, FX for a non-USD reporter.
  * Refresh integration — drive ``refresh_dcf.refresh_one`` end-to-end: it rebuilds
    every sheet from FMP, PRESERVES the user's Dashboard inputs, recomputes the
    value, and upserts ``dcf_runs``. Plus the ``dcf_applicable=false`` skip and the
    negative-fair-value (#291) guard.
"""

from __future__ import annotations

import dataclasses
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import openpyxl
import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "execution"))
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import dcf_sheets  # noqa: E402
import refresh_dcf  # noqa: E402

from dcf import redesign  # noqa: E402

BUILDER = PROJECT_ROOT / "execution" / "build_redesigned_dcf.py"

REDESIGN_SHEETS = [
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

_DCF_RUNS_SCHEMA = """
CREATE TABLE dcf_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT UNIQUE,
    valuation_date TEXT, horizon_years INTEGER,
    wacc REAL, terminal_growth REAL,
    npv REAL, npv_per_share REAL, shares_outstanding REAL,
    currency TEXT, notes TEXT, run_id TEXT,
    live_price REAL, live_price_at TEXT, over_under_pct REAL,
    mos_bar_used REAL, assumption_snapshot_json TEXT,
    revenue_growths_json TEXT, fcf_margin REAL
);
"""


# --------------------------------------------------------------------------- #
# Pure engine — RedesignInputs -> value (no workbook)
# --------------------------------------------------------------------------- #
_BASE = redesign.RedesignInputs(
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


def test_value_is_deterministic() -> None:
    assert redesign.value(_BASE).value_per_share_usd == redesign.value(_BASE).value_per_share_usd


def test_higher_growth_raises_value() -> None:
    faster = dataclasses.replace(_BASE, near_growth_by_segment={"Total company": 0.20})
    assert redesign.value(faster).value_per_share_usd > redesign.value(_BASE).value_per_share_usd


def test_higher_margin_raises_value() -> None:
    richer = dataclasses.replace(_BASE, terminal_op_margin=0.35)
    assert redesign.value(richer).value_per_share_usd > redesign.value(_BASE).value_per_share_usd


def test_fx_scales_per_share_value_linearly() -> None:
    usd = redesign.value(_BASE)
    half = redesign.value(dataclasses.replace(_BASE, fx_to_usd=0.5))
    # Reporting-currency value is FX-independent; the USD value scales by FX.
    assert half.value_per_share_reporting == pytest.approx(usd.value_per_share_reporting)
    assert half.value_per_share_usd == pytest.approx(usd.value_per_share_usd * 0.5)


def test_exit_multiple_higher_than_perpetuity_basis_selects_metric() -> None:
    sales = redesign.value(dataclasses.replace(_BASE, terminal_basis="EV/Sales", exit_multiple=3.0))
    ebitda = redesign.value(_BASE)
    # Different bases price different terminal metrics — just assert both resolve
    # to finite, positive values (the basis plumbing reaches compute_valuation).
    assert sales.value_per_share_usd > 0
    assert ebitda.value_per_share_usd > 0


def test_perpetuity_terminal_resolves() -> None:
    perp = redesign.value(
        dataclasses.replace(_BASE, terminal_method="Perpetuity", terminal_growth_g=0.03)
    )
    assert perp.value_per_share_usd > 0
    assert perp.terminal_method == "Perpetuity"


def test_perpetuity_requires_wacc_above_terminal_g() -> None:
    bad = dataclasses.replace(
        _BASE, terminal_method="Perpetuity", wacc=0.025, terminal_growth_g=0.03
    )
    with pytest.raises(redesign.RedesignError, match="WACC"):
        redesign.value(bad)


def test_negative_margin_yields_negative_value_without_crashing() -> None:
    # Opex above revenue every year -> negative FCF -> negative value (no raise);
    # the refresher's #291 guard nulls over/under on a value like this.
    loss = dataclasses.replace(_BASE, near_op_margin=-0.5, terminal_op_margin=-0.5)
    assert redesign.value(loss).value_per_share_usd < 0


# --------------------------------------------------------------------------- #
# Fixtures: write FMP, run the real builder
# --------------------------------------------------------------------------- #
def _write_fmp(repo: Path, ticker: str, *, currency: str = "USD", segments: bool = False) -> None:
    """Minimal FMP fixture: 4 full fiscal years of growing quarterlies + a profile
    + forward estimates. Optionally a two-line product-segment file."""
    fmp = repo / "data" / "historical" / "fmp"
    fmp.mkdir(parents=True, exist_ok=True)
    inc: list[dict[str, object]] = []
    bal: list[dict[str, object]] = []
    cf: list[dict[str, object]] = []
    pseg: list[dict[str, object]] = []
    rev = 250.0
    for year in (2022, 2023, 2024, 2025):
        for q in ("Q1", "Q2", "Q3", "Q4"):
            rev *= 1.03
            inc.append(
                {
                    "fiscalYear": year,
                    "period": q,
                    "reportedCurrency": currency,
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
            if segments:
                pseg.append(
                    {
                        "fiscalYear": year,
                        "period": q,
                        "data": {"Cloud": rev * 0.6 * 1e6, "Devices": rev * 0.4 * 1e6},
                    }
                )
    (fmp / f"{ticker}_income_statement_quarterly.json").write_text(
        json.dumps(inc), encoding="utf-8"
    )
    (fmp / f"{ticker}_balance_sheet_quarterly.json").write_text(json.dumps(bal), encoding="utf-8")
    (fmp / f"{ticker}_cash_flow_quarterly.json").write_text(json.dumps(cf), encoding="utf-8")
    if segments:
        (fmp / f"{ticker}_product_segments_quarterly.json").write_text(
            json.dumps(pseg), encoding="utf-8"
        )
    (fmp / f"{ticker}_profile.json").write_text(
        json.dumps(
            [{"companyName": f"{ticker} Co", "beta": 1.2, "price": 50.0, "currency": "USD"}]
        ),
        encoding="utf-8",
    )
    # Consensus sits comfortably ABOVE the last-FY actual (~1.6bn) and grows, the
    # realistic shape — so the model's seeded near-term growth is a smooth
    # continuation and the value-of-record tracks the builder's mirror tightly.
    est = [
        {
            "date": f"{y}-12-31",
            "revenueAvg": 1750 * (1.08 ** (y - 2026)) * 1e6,
            "netIncomeAvg": 210 * (1.08 ** (y - 2026)) * 1e6,
            "ebitdaAvg": 320 * (1.08 ** (y - 2026)) * 1e6,
            "ebitAvg": 260 * (1.08 ** (y - 2026)) * 1e6,
            "sgaExpenseAvg": 240 * 1e6,
            "epsAvg": 2.1 * (1.08 ** (y - 2026)),
        }
        for y in range(2026, 2031)
    ]
    (fmp / f"{ticker}_analyst_estimates_annual.json").write_text(json.dumps(est), encoding="utf-8")


def _build(repo: Path, ticker: str, dest: Path) -> float:
    """Run the builder as a subprocess; return its value-of-record (RESULT line)."""
    env = dict(os.environ, DCF_TICKER=ticker, DCF_REPO_ROOT=str(repo), DCF_DEST=str(dest))
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


@pytest.fixture(scope="module")
def built_usd(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, float]:
    """Build one USD single-segment workbook once; read-only tests share it."""
    repo = tmp_path_factory.mktemp("redesign_usd")
    _write_fmp(repo, "TESTCO")
    dest = repo / "dcf" / "TESTCO.xlsx"
    builder_value = _build(repo, "TESTCO", dest)
    return dest, builder_value


# --------------------------------------------------------------------------- #
# Reader / format detection
# --------------------------------------------------------------------------- #
def test_is_redesign_format_true_for_builder_output(built_usd: tuple[Path, float]) -> None:
    dest, _ = built_usd
    assert redesign.is_redesign_format(dest) is True


def test_is_redesign_format_false_for_missing(tmp_path: Path) -> None:
    assert redesign.is_redesign_format(tmp_path / "nope.xlsx") is False


def test_is_redesign_format_false_for_legacy_three_sheet(tmp_path: Path) -> None:
    legacy = tmp_path / "legacy.xlsx"
    wb = openpyxl.Workbook()
    for i, name in enumerate(("Historicals", "Forecast", "Valuation")):
        if i == 0:
            ws = wb.active
            assert ws is not None
            ws.title = name
        else:
            wb.create_sheet(name)
    wb.save(str(legacy))
    assert redesign.is_redesign_format(legacy) is False
    assert redesign.read_inputs(legacy) is None
    assert redesign.read_and_value(legacy) is None


def test_read_and_value_matches_builder_mirror(built_usd: tuple[Path, float]) -> None:
    """The reader recomputes the value-of-record from the workbook's inputs; it
    must track the builder's own _project mirror closely (they differ only by the
    capex/D&A base nuance — the reader matches the in-sheet formula exactly)."""
    dest, builder_value = built_usd
    rv = redesign.read_and_value(dest)
    assert rv is not None
    assert rv.value_per_share_usd == pytest.approx(builder_value, rel=0.03)
    assert rv.fx_to_usd == 1.0


def test_reader_reads_dashboard_and_financials(built_usd: tuple[Path, float]) -> None:
    dest, _ = built_usd
    inp = redesign.read_inputs(dest)
    assert inp is not None
    assert inp.segments == ("Total company",)
    assert inp.diluted_shares_m == pytest.approx(100.0)
    assert 0.0 < inp.wacc < 0.25
    assert inp.consensus_years == 5  # 2026..2030 on the Consensus sheet


def test_fx_applied_for_non_usd_reporter(tmp_path: Path) -> None:
    """A non-USD reporter (EUR) gets a × FX multiplier off the VALUE/SHARE
    formula; the reporting-currency value is the USD value ÷ FX."""
    repo = tmp_path / "eur"
    _write_fmp(repo, "EURCO", currency="EUR")
    dest = repo / "EURCO.xlsx"
    _build(repo, "EURCO", dest)
    rv = redesign.read_and_value(dest)
    assert rv is not None
    assert rv.fx_to_usd != 1.0  # EUR -> a non-unity FX
    assert rv.value_per_share_usd == pytest.approx(rv.value_per_share_reporting * rv.fx_to_usd)


# --------------------------------------------------------------------------- #
# Edit-preservation refresh integration
# --------------------------------------------------------------------------- #
class _FakeLive:
    def __init__(self, price: float) -> None:
        self.price = price
        self.fetched_at = None


def _fake_read(_repo: object, _ticker: object) -> _FakeLive:
    return _FakeLive(50.0)


@pytest.fixture
def refresh_repo(tmp_path: Path) -> Path:
    """A repo_root with FMP, a dcf/ dir, and a dcf_runs DB — ready for refresh_one."""
    _write_fmp(tmp_path, "TESTCO", segments=True)
    (tmp_path / "dcf").mkdir()
    conn = sqlite3.connect(str(tmp_path / "data" / "portfolio.db"))
    conn.executescript(_DCF_RUNS_SCHEMA)
    conn.commit()
    conn.close()
    return tmp_path


def _dashboard_cell(path: Path, row: int, col: int = 2) -> object:
    wb = openpyxl.load_workbook(str(path), data_only=False)
    try:
        return wb["Dashboard"].cell(row=row, column=col).value
    finally:
        wb.close()


def test_refresh_redesign_seeds_then_persists(
    refresh_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A missing workbook builds fresh (redesign format) and persists dcf_runs."""
    monkeypatch.setattr(refresh_dcf.live_price_mod, "read_live_price", _fake_read)
    db = refresh_repo / "data" / "portfolio.db"
    res = refresh_dcf.refresh_one("TESTCO", refresh_repo, db, valuation_year=2026)
    assert res["status"] == "ok", res
    assert res["format"] == "redesign"
    dest = refresh_repo / "dcf" / "TESTCO.xlsx"
    assert dest.exists()
    wb = openpyxl.load_workbook(str(dest))
    assert wb.sheetnames == REDESIGN_SHEETS
    wb.close()
    conn = sqlite3.connect(str(db))
    row = conn.execute(
        "SELECT npv_per_share, live_price FROM dcf_runs WHERE ticker='TESTCO'"
    ).fetchone()
    conn.close()
    assert row is not None and row[0] is not None
    assert row[1] == pytest.approx(50.0)


def test_refresh_redesign_preserves_dashboard_edit_and_updates_actuals(
    refresh_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The core edit-preservation contract: edit a yellow Dashboard cell, change
    the underlying FMP actuals, refresh. The edit MUST survive, the actuals MUST
    update, and the dropdowns/charts MUST still be intact."""
    monkeypatch.setattr(refresh_dcf.live_price_mod, "read_live_price", _fake_read)
    db = refresh_repo / "data" / "portfolio.db"
    dest = refresh_repo / "dcf" / "TESTCO.xlsx"

    # Seed a workbook, then simulate a user editing the terminal margin (B30) and
    # the Cloud segment near-term growth (a segment row).
    refresh_dcf.refresh_one("TESTCO", refresh_repo, db, valuation_year=2026)
    wb = openpyxl.load_workbook(str(dest))
    dsh = wb["Dashboard"]
    dsh.cell(row=30, column=2, value=0.33)  # terminal op margin -> 33%
    for r in range(20, 28):  # Cloud segment near-term growth -> 50%
        if dsh.cell(row=r, column=1).value == "Cloud":
            dsh.cell(row=r, column=2, value=0.50)
    wb.save(str(dest))
    wb.close()

    # Mutate the latest-quarter FMP revenue so the rebuilt Financials must change.
    inc_path = (
        refresh_repo / "data" / "historical" / "fmp" / "TESTCO_income_statement_quarterly.json"
    )
    inc = json.loads(inc_path.read_text(encoding="utf-8"))
    newest = max(inc, key=lambda r: (int(r["fiscalYear"]), str(r["period"])))
    newest["revenue"] = 9_999 * 1e6
    inc_path.write_text(json.dumps(inc), encoding="utf-8")

    res = refresh_dcf.refresh_one("TESTCO", refresh_repo, db, valuation_year=2026)
    assert res["status"] == "ok", res
    assert res["inputs_preserved"] is True

    # The edits survived the rebuild.
    assert _dashboard_cell(dest, 30) == pytest.approx(0.33)
    wb2 = openpyxl.load_workbook(str(dest))
    dsh2 = wb2["Dashboard"]
    cloud = next(
        dsh2.cell(row=r, column=2).value
        for r in range(20, 28)
        if dsh2.cell(row=r, column=1).value == "Cloud"
    )
    assert cloud == pytest.approx(0.50)
    # Current price was refreshed (program-owned), not preserved.
    assert dsh2.cell(row=48, column=2).value == pytest.approx(50.0)
    # Dropdowns survived the inject load/save (proving complex features round-trip).
    dv = {str(d.sqref) for d in dsh2.data_validations.dataValidation}
    assert "B43" in dv and "B44" in dv
    assert wb2.sheetnames == REDESIGN_SHEETS  # all nine sheets intact after inject
    wb2.close()

    # The actuals updated: the rebuilt Financials carries the mutated revenue.
    wb3 = openpyxl.load_workbook(str(dest), data_only=False)
    fs = wb3["Financials"]
    rev_row = next(
        r for r in range(1, fs.max_row + 1) if str(fs.cell(r, 1).value).strip() == "Revenue"
    )
    rev_values: list[float] = []
    for c in range(2, fs.max_column + 1):
        v = fs.cell(row=rev_row, column=c).value
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            rev_values.append(float(v))
    wb3.close()
    assert any(abs(v - 9999.0) < 1.0 for v in rev_values)


def test_refresh_skips_dcf_not_applicable(
    refresh_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-bank financial (asset-manager/insurer) Opus flagged dcf_applicable=
    false skips before any build — only credit banks route to the bank model."""
    assumptions = refresh_repo / "data" / "dcf_assumptions"
    assumptions.mkdir(parents=True, exist_ok=True)
    (assumptions / "TESTCO.json").write_text(
        json.dumps({"redesign": {"dcf_applicable": False, "business_model": "asset_manager"}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(refresh_dcf.live_price_mod, "read_live_price", _fake_read)
    db = refresh_repo / "data" / "portfolio.db"
    res = refresh_dcf.refresh_one("TESTCO", refresh_repo, db, valuation_year=2026)
    assert res["status"] == "skipped"
    assert "asset_manager" in str(res["reason"])
    assert not (refresh_repo / "dcf" / "TESTCO.xlsx").exists()  # never built


def test_refresh_bank_dispatches_to_bank_model(
    refresh_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A credit bank (business_model=bank) is routed to the equity-side bank
    model (`_refresh_bank`), NOT skipped — it returns format='bank'."""
    assumptions = refresh_repo / "data" / "dcf_assumptions"
    assumptions.mkdir(parents=True, exist_ok=True)
    (assumptions / "TESTCO.json").write_text(
        json.dumps({"redesign": {"dcf_applicable": False, "business_model": "bank"}}),
        encoding="utf-8",
    )
    db = refresh_repo / "data" / "portfolio.db"
    res = refresh_dcf.refresh_one("TESTCO", refresh_repo, db, valuation_year=2026)
    assert res["status"] != "skipped"  # dispatched to the bank builder
    assert res["format"] == "bank"  # (status is 'failed' here — no TESTCO FMP fixture data)


def test_refresh_redesign_negative_fair_value_nulls_over_under(
    refresh_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A user forcing deeply negative margins yields a negative fair value;
    refresh_one persists it with over_under None (the #291 guard) rather than
    crashing on the over/under calc."""
    monkeypatch.setattr(refresh_dcf.live_price_mod, "read_live_price", _fake_read)
    db = refresh_repo / "data" / "portfolio.db"
    dest = refresh_repo / "dcf" / "TESTCO.xlsx"
    refresh_dcf.refresh_one("TESTCO", refresh_repo, db, valuation_year=2026)
    wb = openpyxl.load_workbook(str(dest))
    dsh = wb["Dashboard"]
    dsh.cell(row=29, column=2, value=-1.0)  # near op margin -100%
    dsh.cell(row=30, column=2, value=-1.0)  # terminal op margin -100%
    wb.save(str(dest))
    wb.close()

    res = refresh_dcf.refresh_one("TESTCO", refresh_repo, db, valuation_year=2026)
    assert res["status"] == "ok"
    fv = res["fair_value_per_share"]
    assert isinstance(fv, float) and fv < 0
    assert res["over_under_pct"] is None


def test_gsheets_reingest_carries_dashboard_edit_to_dcf_runs(
    refresh_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The Google-Sheets re-ingest path (dcf_sheets import --file -> refresh_one)
    carries a Dashboard edit on the pulled workbook through to the persisted
    dcf_runs value — the user edits in Sheets, pulls down, re-ingests."""
    monkeypatch.setattr(refresh_dcf.live_price_mod, "read_live_price", _fake_read)
    repo = refresh_repo
    # A redesign workbook standing in for the pulled Sheet; bump terminal margin.
    downloaded = repo / "downloaded_TESTCO.xlsx"
    _build(repo, "TESTCO", downloaded)
    base = redesign.read_and_value(downloaded)
    assert base is not None
    wb = openpyxl.load_workbook(str(downloaded))
    wb["Dashboard"].cell(row=30, column=2, value=0.40)  # terminal op margin up
    wb.save(str(downloaded))
    wb.close()
    edited = redesign.read_and_value(downloaded)
    assert edited is not None and edited.value_per_share_usd > base.value_per_share_usd

    rc = dcf_sheets.main(
        [
            "import",
            "--ticker",
            "TESTCO",
            "--file",
            str(downloaded),
            "--repo-root",
            str(repo),
            "--valuation-year",
            "2026",
        ]
    )
    assert rc == 0
    assert (repo / "dcf" / "TESTCO.xlsx").exists()  # placed at the canonical path

    conn = sqlite3.connect(str(repo / "data" / "portfolio.db"))
    row = conn.execute("SELECT npv_per_share FROM dcf_runs WHERE ticker='TESTCO'").fetchone()
    conn.close()
    assert row is not None and row[0] is not None
    # The persisted value reflects the edited (higher) terminal margin.
    assert float(row[0]) == pytest.approx(edited.value_per_share_usd, rel=0.05)
    assert float(row[0]) > base.value_per_share_usd
