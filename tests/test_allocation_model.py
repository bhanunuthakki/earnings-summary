"""Tests for src/allocation/model.py — factors, blend, softmax, degradation.

Fixtures fabricate a repo root with the three substrates the model reads:
dcf_runs rows (return factor), FMP price-chart JSONs (diversification), and
macro_series + macro_sensitivities rows (macro tilt).
"""

from __future__ import annotations

import json
import math
import sqlite3
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import numpy as np
import pytest

from allocation.model import (
    _dcf_upside,  # pyright: ignore[reportPrivateUsage]  # internal seam under test
    _zscore,  # pyright: ignore[reportPrivateUsage]  # internal seam under test
    build_next_dollar_model,
)

TICKERS = ["AAA", "BBB", "CCC"]


def _schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE dcf_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL,
            valuation_date TEXT,
            npv_per_share NUMERIC,
            live_price FLOAT,
            created_at DATETIME
        );
        CREATE TABLE macro_series (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            series_id VARCHAR(48) NOT NULL,
            rate_date DATE NOT NULL,
            value NUMERIC(20,8) NOT NULL,
            source VARCHAR(32) NOT NULL DEFAULT 'FMP',
            created_at DATETIME NOT NULL,
            UNIQUE(series_id, rate_date)
        );
        CREATE TABLE macro_sensitivities (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker VARCHAR(16) NOT NULL,
            series_id VARCHAR(48) NOT NULL,
            beta FLOAT NOT NULL,
            r_squared FLOAT,
            lookback_window_days INTEGER NOT NULL,
            computed_at DATETIME NOT NULL,
            UNIQUE(ticker, series_id, lookback_window_days)
        );
        """
    )
    conn.commit()


@pytest.fixture()
def repo_root(tmp_path: Path) -> Path:
    db = tmp_path / "data" / "portfolio.db"
    db.parent.mkdir(parents=True)
    conn = sqlite3.connect(db)
    try:
        _schema(conn)
    finally:
        conn.close()
    return tmp_path


def _db(repo_root: Path) -> Path:
    return repo_root / "data" / "portfolio.db"


def _seed_dcf(repo_root: Path, rows: list[tuple[str, float, float, str]]) -> None:
    conn = sqlite3.connect(_db(repo_root))
    try:
        conn.executemany(
            "INSERT INTO dcf_runs (ticker, valuation_date, npv_per_share, live_price, created_at)"
            " VALUES (?, ?, ?, ?, ?)",
            [(t, vd, fv, px, f"{vd} 00:00:00") for t, fv, px, vd in rows],
        )
        conn.commit()
    finally:
        conn.close()


def _seed_prices(repo_root: Path, n: int = 200) -> None:
    """Three synthetic names: BBB tracks AAA closely; CCC is independent."""
    days: list[date] = []
    d = date.today() - timedelta(days=2)
    while len(days) < n:
        if d.weekday() < 5:
            days.append(d)
        d -= timedelta(days=1)
    days.reverse()
    rng = np.random.default_rng(0)
    base = rng.normal(0.0005, 0.02, n)
    noise = rng.normal(0.0, 0.005, n)
    indep = rng.normal(0.0005, 0.02, n)
    series = {"AAA": base, "BBB": 0.9 * base + noise, "CCC": indep}
    fmp = repo_root / "data" / "historical" / "fmp"
    fmp.mkdir(parents=True, exist_ok=True)
    for ticker, rets in series.items():
        prices = 100.0 * np.exp(np.cumsum(rets))
        rows = [
            {"date": days[i].isoformat(), "adjClose": round(float(prices[i]), 6)} for i in range(n)
        ][::-1]  # newest first, like FMP
        (fmp / f"{ticker}_price_chart_10y_div_adj.json").write_text(
            json.dumps(rows), encoding="utf-8"
        )


def _seed_macro(
    repo_root: Path,
    betas: dict[str, float],
    *,
    latest_age_days: int = 1,
    rise: float = 1.10,
) -> None:
    """One series (usd_brl) stepping up by ``rise`` inside the momentum window."""
    conn = sqlite3.connect(_db(repo_root))
    now = datetime.now(UTC).isoformat()
    latest = date.today() - timedelta(days=latest_age_days)
    cutoff = latest - timedelta(days=90)
    try:
        for k in range(19):  # weekly grid covering ~126 days
            day = latest - timedelta(days=7 * k)
            value = 5.0 * rise if day > cutoff else 5.0
            conn.execute(
                "INSERT INTO macro_series (series_id, rate_date, value, source, created_at)"
                " VALUES (?, ?, ?, 'FMP', ?)",
                ("usd_brl", day.isoformat(), value, now),
            )
        for ticker, beta in betas.items():
            conn.execute(
                "INSERT INTO macro_sensitivities"
                " (ticker, series_id, beta, r_squared, lookback_window_days, computed_at)"
                " VALUES (?, 'usd_brl', ?, 0.4, 252, ?)",
                (ticker, beta, now),
            )
        conn.commit()
    finally:
        conn.close()


def test_full_model_three_factors(repo_root: Path) -> None:
    _seed_prices(repo_root)
    _seed_dcf(
        repo_root,
        [
            ("AAA", 999.0, 100.0, "2026-01-01"),  # stale run — must lose to the latest
            ("AAA", 150.0, 100.0, "2026-06-08"),
            ("BBB", 100.0, 100.0, "2026-06-08"),
            ("CCC", 120.0, 100.0, "2026-06-09"),
        ],
    )
    _seed_macro(repo_root, {"AAA": 2.0, "BBB": -1.0, "CCC": 0.5})
    live = {"AAA": 5000.0, "BBB": 3000.0, "CCC": 2000.0, "ZZZ": 99999.0}  # ZZZ ignored

    model = build_next_dollar_model(_db(repo_root), repo_root, TICKERS, live)
    assert model is not None
    assert model.weights_source == "tracker"
    assert model.hidden_factors == {}
    assert model.blend == [("ret", 0.5), ("div", 0.3), ("macro", 0.2)]
    assert model.excluded == {}
    assert model.cov_obs is not None and model.cov_obs >= 120
    assert model.shrinkage is not None and 0.0 <= model.shrinkage <= 1.0
    assert model.prices_through is not None
    assert model.portfolio_vol_ann is not None and model.portfolio_vol_ann > 0.0

    assert len(model.rows) == 3
    assert sum(r.allocation_pct for r in model.rows) == pytest.approx(100.0)
    assert all(r.allocation_pct > 0.0 for r in model.rows)
    by_ticker = {r.ticker: r for r in model.rows}
    # ZZZ excluded from the book: weights renormalize over the three names.
    assert by_ticker["AAA"].current_weight_pct == pytest.approx(50.0)

    for row in model.rows:
        assert row.missing == ()
        for key in ("ret", "div", "macro"):
            reading = row.reading(key)
            assert reading is not None
            assert reading.contribution == pytest.approx(reading.weight * reading.z)
        assert row.score == pytest.approx(
            sum(r.contribution for r in (row.ret, row.div, row.macro) if r is not None)
        )

    # Latest DCF run wins: +50% upside, not the stale 10x row.
    aaa_ret = by_ticker["AAA"].ret
    assert aaa_ret is not None
    assert aaa_ret.raw == pytest.approx(0.50)
    assert "fair $150.00 vs $100.00" in aaa_ret.detail

    # Macro: rising usd_brl is a tailwind for AAA (beta +2), headwind for BBB.
    aaa_macro, bbb_macro = by_ticker["AAA"].macro, by_ticker["BBB"].macro
    assert aaa_macro is not None and bbb_macro is not None
    assert aaa_macro.raw == pytest.approx(2.0 * math.log(1.10), rel=1e-6)
    assert bbb_macro.raw == pytest.approx(-math.log(1.10), rel=1e-6)
    assert "usd_brl" in aaa_macro.detail

    # CCC is the independent name — best diversifier of the three.
    div_raws = {t: r.raw for t in TICKERS if (r := by_ticker[t].div) is not None}
    assert div_raws["CCC"] == max(div_raws.values())

    # AAA dominates on return + macro; BBB trails on everything.
    assert model.rows[0].ticker == "AAA"
    assert model.rows[-1].ticker == "BBB"
    # Sorted descending by allocation.
    allocs = [r.allocation_pct for r in model.rows]
    assert allocs == sorted(allocs, reverse=True)


def test_macro_hidden_when_tables_empty(repo_root: Path) -> None:
    _seed_prices(repo_root)
    _seed_dcf(
        repo_root,
        [
            ("AAA", 150.0, 100.0, "2026-06-08"),
            ("BBB", 90.0, 100.0, "2026-06-08"),
            ("CCC", 120.0, 100.0, "2026-06-08"),
        ],
    )
    model = build_next_dollar_model(_db(repo_root), repo_root, TICKERS, None)
    assert model is not None
    assert "macro" in model.hidden_factors
    assert model.blend == [("ret", pytest.approx(0.625)), ("div", pytest.approx(0.375))]
    for row in model.rows:
        assert row.macro is None
        assert row.missing == ()  # macro is inactive, not per-holding-missing


def test_stale_macro_series_hides_factor(repo_root: Path) -> None:
    _seed_prices(repo_root)
    _seed_dcf(
        repo_root,
        [
            ("AAA", 150.0, 100.0, "2026-06-08"),
            ("BBB", 90.0, 100.0, "2026-06-08"),
            ("CCC", 120.0, 100.0, "2026-06-08"),
        ],
    )
    _seed_macro(repo_root, {"AAA": 2.0, "BBB": -1.0, "CCC": 0.5}, latest_age_days=60)
    model = build_next_dollar_model(_db(repo_root), repo_root, TICKERS, None)
    assert model is not None
    assert "macro" in model.hidden_factors


def test_per_holding_renormalization_when_dcf_missing(repo_root: Path) -> None:
    _seed_prices(repo_root)
    _seed_dcf(
        repo_root,
        [("AAA", 150.0, 100.0, "2026-06-08"), ("CCC", 120.0, 100.0, "2026-06-08")],
    )
    _seed_macro(repo_root, {"AAA": 2.0, "BBB": -1.0, "CCC": 0.5})
    model = build_next_dollar_model(_db(repo_root), repo_root, TICKERS, None)
    assert model is not None
    assert model.hidden_factors == {}  # ret still has two carriers
    bbb = next(r for r in model.rows if r.ticker == "BBB")
    assert bbb.ret is None
    assert bbb.missing == ("ret",)
    assert bbb.div is not None and bbb.macro is not None
    assert bbb.div.weight == pytest.approx(0.6)  # 0.3 / (0.3 + 0.2)
    assert bbb.macro.weight == pytest.approx(0.4)
    aaa = next(r for r in model.rows if r.ticker == "AAA")
    assert aaa.ret is not None and aaa.ret.weight == pytest.approx(0.5)


def test_equal_weight_fallback_when_tracker_absent(repo_root: Path) -> None:
    _seed_prices(repo_root)
    _seed_dcf(
        repo_root,
        [
            ("AAA", 150.0, 100.0, "2026-06-08"),
            ("BBB", 90.0, 100.0, "2026-06-08"),
            ("CCC", 120.0, 100.0, "2026-06-08"),
        ],
    )
    model = build_next_dollar_model(_db(repo_root), repo_root, TICKERS, None)
    assert model is not None
    assert model.weights_source == "equal"
    for row in model.rows:
        assert row.current_weight_pct == pytest.approx(100.0 / 3.0)


def test_returns_none_when_no_substrate(repo_root: Path) -> None:
    assert build_next_dollar_model(_db(repo_root), repo_root, TICKERS, None) is None
    assert build_next_dollar_model(_db(repo_root), repo_root, [], None) is None


def test_zscore_clamps_outliers_but_keeps_raw_visible(repo_root: Path) -> None:
    clamped = _zscore({"A": (4.0, ""), "B": (0.5, ""), "C": (-0.2, "")}, clamp=1.0)
    reference = _zscore({"A": (1.0, ""), "B": (0.5, ""), "C": (-0.2, "")})
    assert clamped == reference

    _seed_prices(repo_root)
    _seed_dcf(
        repo_root,
        [
            ("AAA", 500.0, 100.0, "2026-06-08"),
            ("BBB", 110.0, 100.0, "2026-06-08"),
            ("CCC", 80.0, 100.0, "2026-06-08"),
        ],
    )
    model = build_next_dollar_model(_db(repo_root), repo_root, TICKERS, None)
    assert model is not None
    aaa = next(r for r in model.rows if r.ticker == "AAA")
    assert aaa.ret is not None
    assert aaa.ret.raw == pytest.approx(4.0)  # raw stays unclamped in the waterfall


def test_zscore_degenerate_cross_section_scores_zero() -> None:
    out = _zscore({"A": (0.3, ""), "B": (0.3, ""), "C": (0.3, "")})
    assert out == {"A": 0.0, "B": 0.0, "C": 0.0}


def _seed_dcf_with_scenarios(db: Path) -> None:
    """A dcf_runs schema carrying assumption_snapshot_json (the production shape),
    so the ret factor reads the bull/base/bear range, not just the base point."""
    conn = sqlite3.connect(db)
    try:
        conn.executescript(
            """
            CREATE TABLE dcf_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker TEXT NOT NULL,
                valuation_date TEXT,
                npv_per_share NUMERIC,
                live_price FLOAT,
                assumption_snapshot_json TEXT,
                created_at DATETIME
            );
            """
        )
        snap = json.dumps(
            {
                "format": "redesign",
                "scenarios": {
                    "bull": {"fair_value_per_share_usd": 130.0},
                    "base": {"fair_value_per_share_usd": 110.0},
                    "bear": {"fair_value_per_share_usd": 40.0},
                },
            }
        )
        conn.execute(
            "INSERT INTO dcf_runs (ticker, valuation_date, npv_per_share, live_price,"
            " assumption_snapshot_json, created_at) VALUES"
            " ('SKEW', '2026-06-08', 110.0, 100.0, ?, '2026-06-08 00:00:00'),"
            " ('FLAT', '2026-06-08', 120.0, 100.0, NULL, '2026-06-08 00:00:00')",
            (snap,),
        )
        conn.commit()
    finally:
        conn.close()


def test_dcf_upside_uses_scenario_asymmetry(tmp_path: Path) -> None:
    db = tmp_path / "portfolio.db"
    _seed_dcf_with_scenarios(db)
    out = _dcf_upside(db, ["SKEW", "FLAT"])
    # SKEW: downside-skewed range pulls the reward below the +10% base point.
    skew_raw, skew_detail = out["SKEW"]
    assert skew_raw == pytest.approx(-0.025)  # 0.25*0.30 + 0.5*0.10 + 0.25*(-0.60)
    assert "exp -2%" in skew_detail and "(2026-06-08)" in skew_detail
    # FLAT: no scenarios → the legacy base point estimate, detail unchanged.
    flat_raw, flat_detail = out["FLAT"]
    assert flat_raw == pytest.approx(0.20)
    assert flat_detail == "fair $120.00 vs $100.00 (2026-06-08)"


def _seed_dcf_versioned(db: Path) -> None:
    """The versioned/sanity-gated schema (migration 0137+/0182): is_latest,
    segment_name, sanity_flag — exercising the branches the plain _seed_dcf
    schema (no such columns) never reaches."""
    conn = sqlite3.connect(db)
    try:
        conn.executescript(
            """
            CREATE TABLE dcf_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker TEXT NOT NULL,
                created_at DATETIME,
                is_latest INTEGER,
                segment_name TEXT,
                valuation_date TEXT,
                npv_per_share NUMERIC,
                live_price FLOAT,
                sanity_flag TEXT
            );
            """
        )
        conn.commit()
    finally:
        conn.close()


def _insert_versioned_dcf(
    db: Path,
    *,
    ticker: str,
    created_at: str,
    is_latest: int = 1,
    segment_name: str | None = None,
    valuation_date: str = "2026-06-08",
    npv_per_share: float | None,
    live_price: float | None,
    sanity_flag: str | None = None,
) -> None:
    conn = sqlite3.connect(db)
    try:
        conn.execute(
            "INSERT INTO dcf_runs (ticker, created_at, is_latest, segment_name, "
            "valuation_date, npv_per_share, live_price, sanity_flag) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                ticker,
                created_at,
                is_latest,
                segment_name,
                valuation_date,
                npv_per_share,
                live_price,
                sanity_flag,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def test_dcf_upside_ignores_segment_row_even_when_newer(tmp_path: Path) -> None:
    """PART A: a segment row (segment_name set) landed AFTER the consolidated
    row must not win the ret factor's reward leg — the exact bug the shared
    dcf.latest reader fixes."""
    db = tmp_path / "portfolio.db"
    _seed_dcf_versioned(db)
    _insert_versioned_dcf(
        db, ticker="AAA", created_at="2026-06-08T00:00:00", npv_per_share=120.0, live_price=100.0
    )
    _insert_versioned_dcf(
        db,
        ticker="AAA",
        created_at="2026-06-09T00:00:00",  # newer than the consolidated row
        segment_name="Consumer",
        npv_per_share=999.0,
        live_price=100.0,
    )
    out = _dcf_upside(db, ["AAA"])
    aaa_raw, _ = out["AAA"]
    assert aaa_raw == pytest.approx(0.20)  # from the consolidated 120/100 row, not the segment one


def test_dcf_upside_excludes_sanity_flagged_row(tmp_path: Path) -> None:
    """Caller policy (ratified): the ret factor is a ranking/valuation leg,
    so a sanity-flagged (outlier) run is excluded entirely — same rule
    allocation.eligibility already enforces for the decision-ready gate."""
    db = tmp_path / "portfolio.db"
    _seed_dcf_versioned(db)
    _insert_versioned_dcf(
        db,
        ticker="AAA",
        created_at="2026-06-08T00:00:00",
        npv_per_share=500.0,
        live_price=100.0,
        sanity_flag="outlier",
    )
    out = _dcf_upside(db, ["AAA"])
    assert "AAA" not in out


def test_low_r_squared_sensitivity_excluded_from_macro_tilt(repo_root: Path) -> None:
    """C4 pin (A3's read-side floor): a sensitivity with r_squared below
    MACRO_MIN_R_SQUARED never enters the tilt — statistically indistinguishable
    betas (the MELI/NU/NVO usd_cad case from the 2026-07 review) must not
    manufacture a macro preference between names."""
    from allocation.model import MACRO_MIN_R_SQUARED

    _seed_prices(repo_root)
    _seed_dcf(
        repo_root,
        [
            ("AAA", 150.0, 100.0, "2026-06-08"),
            ("BBB", 90.0, 100.0, "2026-06-08"),
            ("CCC", 120.0, 100.0, "2026-06-08"),
        ],
    )
    _seed_macro(repo_root, {"AAA": 2.0, "BBB": -1.0})
    # CCC's beta is huge but statistically meaningless — below the floor.
    conn = sqlite3.connect(str(_db(repo_root)))
    try:
        conn.execute(
            "INSERT INTO macro_sensitivities"
            " (ticker, series_id, beta, r_squared, lookback_window_days, computed_at)"
            " VALUES ('CCC', 'usd_brl', 50.0, ?, 252, '2026-06-09')",
            (MACRO_MIN_R_SQUARED - 0.02,),
        )
        conn.commit()
    finally:
        conn.close()

    model = build_next_dollar_model(_db(repo_root), repo_root, TICKERS, None)
    assert model is not None
    by_ticker = {r.ticker: r for r in model.rows}
    assert by_ticker["AAA"].macro is not None  # the floor passes real signal
    assert by_ticker["CCC"].macro is None  # below-floor beta never tilts
