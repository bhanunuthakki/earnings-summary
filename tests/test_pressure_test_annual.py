"""pressure_test_thesis._load_annual reads financial_facts directly.

Regression for the last consumer of the metrics/ratios SQL views: the views
window-scan all of financial_facts per query (~12s) and key identity off the
fiscal-period LABEL, which cross-labels quarters for off-calendar reporters
(#452 took the cockpit and §3 financials off the views for the same reason).
The loader now pivots facts keyed on the period_end DATE and computes the four
ratios (operating/net/FCF margin, ROE) in Python with the same NULLIF
semantics the view had.

Also covers the signed-series CAGR fix: an all-negative series (capex is
stored as negative spend) gets a magnitude CAGR in both the §3 growth helper
(`compute_growth`) and the YoY heatmap matrix, instead of a permanent "—".
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.path.insert(0, str(PROJECT_ROOT / "execution"))

import pressure_test_thesis as ptt  # noqa: E402

from report.renderers.charts_v2 import MatrixRow, yoy_heatmap_table  # noqa: E402
from report.sections._common import compute_growth  # noqa: E402

# ---------------------------------------------------------------------------
# _load_annual — direct financial_facts read
# ---------------------------------------------------------------------------


def _facts_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE financial_facts (
            ticker TEXT, period_end TEXT, fiscal_period_type TEXT,
            line_item TEXT, value REAL, source_doc_id INTEGER
        )
        """
    )
    return conn


def _seed_year(
    conn: sqlite3.Connection,
    ticker: str,
    period_end: str,
    *,
    revenue: float,
    operating_income: float,
    net_income: float,
    free_cash_flow: float | None,
    equity: float | None,
    fiscal_period_type: str = "FY",
    source_doc_id: int = 100,
) -> None:
    items: list[tuple[str, float | None]] = [
        ("revenue", revenue),
        ("operating_income", operating_income),
        ("net_income", net_income),
        ("free_cash_flow", free_cash_flow),
        ("total_stockholders_equity", equity),
    ]
    for line_item, value in items:
        if value is None:
            continue
        conn.execute(
            "INSERT INTO financial_facts VALUES (?, ?, ?, ?, ?, ?)",
            (ticker, period_end, fiscal_period_type, line_item, value, source_doc_id),
        )


def test_load_annual_computes_view_equivalent_ratios() -> None:
    conn = _facts_conn()
    _seed_year(
        conn,
        "TST",
        "2024-12-31",
        revenue=1_000.0,
        operating_income=200.0,
        net_income=100.0,
        free_cash_flow=150.0,
        equity=500.0,
    )
    _seed_year(
        conn,
        "TST",
        "2025-12-31",
        revenue=1_200.0,
        operating_income=300.0,
        net_income=120.0,
        free_cash_flow=240.0,
        equity=600.0,
    )
    rows = ptt._load_annual(conn, "TST", n=5)  # pyright: ignore[reportPrivateUsage]

    assert [r["fiscal_year"] for r in rows] == [2024, 2025]  # oldest first
    latest = rows[-1]
    assert latest["revenue"] == 1_200.0
    assert latest["operating_margin"] == 0.25
    assert latest["net_margin"] == 0.10
    assert latest["fcf_margin"] == 0.20
    assert latest["roe"] == 0.20


def test_load_annual_dedups_on_source_doc_id_and_honors_limit() -> None:
    conn = _facts_conn()
    # Same FY twice: a stale extraction (doc 10) and a restated one (doc 99).
    _seed_year(
        conn,
        "TST",
        "2025-12-31",
        revenue=900.0,
        operating_income=90.0,
        net_income=45.0,
        free_cash_flow=None,
        equity=None,
        source_doc_id=10,
    )
    _seed_year(
        conn,
        "TST",
        "2025-12-28",  # day-level wobble within the year must not split the FY
        revenue=1_000.0,
        operating_income=250.0,
        net_income=100.0,
        free_cash_flow=None,
        equity=None,
        source_doc_id=99,
    )
    for year in (2020, 2021, 2022, 2023, 2024):
        _seed_year(
            conn,
            "TST",
            f"{year}-12-31",
            revenue=100.0,
            operating_income=10.0,
            net_income=5.0,
            free_cash_flow=None,
            equity=None,
        )
    rows = ptt._load_annual(conn, "TST", n=5)  # pyright: ignore[reportPrivateUsage]

    assert len(rows) == 5  # LIMIT respected
    latest = rows[-1]
    assert latest["fiscal_year"] == 2025
    assert latest["revenue"] == 1_000.0  # doc 99 wins, not the stale doc 10
    assert latest["operating_margin"] == 0.25
    # Missing line items → None ratios, never a crash or a zero-division.
    assert latest["fcf_margin"] is None
    assert latest["roe"] is None


def test_load_annual_accepts_annual_period_type_rows() -> None:
    conn = _facts_conn()
    _seed_year(
        conn,
        "TST",
        "2025-12-31",
        revenue=500.0,
        operating_income=50.0,
        net_income=25.0,
        free_cash_flow=None,
        equity=None,
        fiscal_period_type="annual",
    )
    rows = ptt._load_annual(conn, "TST", n=5)  # pyright: ignore[reportPrivateUsage]
    assert [r["fiscal_year"] for r in rows] == [2025]
    assert rows[0]["net_margin"] == 0.05


# ---------------------------------------------------------------------------
# Signed-series CAGR (capex stored as negative spend)
# ---------------------------------------------------------------------------


def test_compute_growth_all_negative_series_gets_magnitude_cagr() -> None:
    # 8 quarters of capex: TTM(now) = -120, TTM(1y ago) = -100 → +20% spend CAGR.
    values: list[float | None] = [*[-25.0] * 4, *[-30.0] * 4]
    g = compute_growth(values)
    assert g.cagr_1y_ttm is not None
    assert abs(g.cagr_1y_ttm - 0.20) < 1e-9
    # YoY of the raw quarters: -30 / -25 - 1 = +20% (sign-through, unchanged).
    assert g.yoy is not None
    assert abs(g.yoy - 0.20) < 1e-9


def test_compute_growth_sign_flip_still_returns_none() -> None:
    # TTM windows straddle zero → CAGR stays undefined.
    values: list[float | None] = [*[-25.0] * 4, *[25.0] * 4]
    g = compute_growth(values)
    assert g.cagr_1y_ttm is None


def test_yoy_matrix_capex_row_shows_magnitude_cagr() -> None:
    # 12 quarters, magnitude growing 100 → 120 → 180. The 2y CAGR
    # (sqrt(180/100) - 1 = +34.2%) appears nowhere among the YoY cells
    # (+20% / +50%), so asserting it proves the trailing CAGR cell rendered.
    levels: list[float | None] = [*[-100.0] * 4, *[-120.0] * 4, *[-180.0] * 4]
    periods = [f"{y} Q{q}" for y in (2023, 2024, 2025) for q in (1, 2, 3, 4)]
    out = yoy_heatmap_table(
        [MatrixRow(name="Capex", levels=levels, unit="USD millions")], periods, title=""
    )
    assert "+34.2%" in out
