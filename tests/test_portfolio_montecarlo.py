"""Fat-tailed book Monte Carlo — the pure simulation math + the joint-LatAm
event-correlation stress, over synthetic fixtures (no randomness in the
fixtures themselves; the simulation is seeded for determinism)."""

from __future__ import annotations

import json
import math
import random
import sqlite3
from datetime import date, timedelta
from pathlib import Path

import pytest

from portfolio_montecarlo import (
    CASH_LIKE_TICKERS,
    DRAWDOWN_LABELS,
    EVENT_SCENARIOS,
    build_book_monte_carlo,
    build_event_stress,
    build_joint_latam_stress,
)

# ---------------------------------------------------------------------------
# Synthetic daily-return fixtures (deterministic RNG seed — reproducible).
# ---------------------------------------------------------------------------


def _trading_days(n: int, start: date = date(2023, 1, 3)) -> list[date]:
    out: list[date] = []
    d = start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def _write_price_chart(
    repo_root: Path,
    ticker: str,
    days: list[date],
    *,
    vol: float,
    drift: float = 0.0004,
    corr_with: list[float] | None = None,
    corr: float = 0.0,
    rng: random.Random | None = None,
) -> list[float]:
    rng = rng or random.Random(0)
    base = [rng.gauss(drift, vol) for _ in days]
    if corr_with is not None:
        base = [corr * c + math.sqrt(1 - corr**2) * b for b, c in zip(base, corr_with)]
    fmp = repo_root / "data" / "historical" / "fmp"
    fmp.mkdir(parents=True, exist_ok=True)
    level = 100.0
    rows: list[dict[str, object]] = []
    for d, r in zip(days, base):
        level *= math.exp(r)
        rows.append({"date": d.isoformat(), "adjClose": level})
    (fmp / f"{ticker}_price_chart_10y_div_adj.json").write_text(json.dumps(rows), encoding="utf-8")
    return base


def _seed_book(tmp_path: Path, n_days: int = 300) -> list[date]:
    days = _trading_days(n_days)
    rng = random.Random(42)
    meli = _write_price_chart(tmp_path, "MELI", days, vol=0.020, rng=rng)
    _write_price_chart(tmp_path, "NU", days, vol=0.022, corr_with=meli, corr=0.5, rng=rng)
    _write_price_chart(tmp_path, "NOW", days, vol=0.015, rng=rng)
    return days


# ---------------------------------------------------------------------------
# build_book_monte_carlo
# ---------------------------------------------------------------------------


def test_no_weights_returns_none(tmp_path: Path) -> None:
    assert build_book_monte_carlo(tmp_path, {}) is None
    assert build_book_monte_carlo(tmp_path, {"AAA": 0.0}) is None


def test_needs_two_risky_names(tmp_path: Path) -> None:
    _write_price_chart(tmp_path, "AAA", _trading_days(300), vol=0.02)
    # Only one risky name (SGOV is cash-like, excluded from the panel) —
    # matches build_holdings_correlation_from_disk's "nothing pairwise to
    # say" contract on the same substrate.
    assert build_book_monte_carlo(tmp_path, {"AAA": 0.5, "SGOV": 0.5}) is None


def test_cash_only_book_returns_none(tmp_path: Path) -> None:
    assert build_book_monte_carlo(tmp_path, {"SGOV": 0.6, "FDRXX": 0.4}) is None


def test_dropped_names_reported_with_reason(tmp_path: Path) -> None:
    _seed_book(tmp_path)
    mc = build_book_monte_carlo(
        tmp_path, {"MELI": 0.4, "NU": 0.3, "NOW": 0.2, "ZZZ": 0.1}, n_paths=5_000
    )
    assert mc is not None
    assert mc.dropped == {"ZZZ": "no daily price history on file"}
    assert mc.tickers == ["MELI", "NOW", "NU"]
    assert mc.unmodeled_weight_pct == pytest.approx(10.0)
    assert mc.modeled_weight_pct == pytest.approx(90.0)


def test_coverage_splits_risky_vs_cash_vs_unmodeled(tmp_path: Path) -> None:
    _seed_book(tmp_path)
    mc = build_book_monte_carlo(
        tmp_path,
        {"MELI": 0.35, "NU": 0.25, "NOW": 0.15, "SGOV": 0.15, "ZZZ": 0.10},
        n_paths=5_000,
    )
    assert mc is not None
    assert mc.risky_weight_pct == pytest.approx(75.0)
    assert mc.cash_like_weight_pct == pytest.approx(15.0)
    assert mc.modeled_weight_pct == pytest.approx(90.0)
    assert mc.unmodeled_weight_pct == pytest.approx(10.0)


def test_t_distribution_fatter_left_tail_than_normal(tmp_path: Path) -> None:
    """The core claim this whole module exists for: at the same seed/paths,
    the Student-t model's 1st percentile is materially below the normal
    model's, and its probability of a >=30% book drawdown is at least as
    high — the fat tail must actually show up, not just exist in name."""
    _seed_book(tmp_path)
    weights = {"MELI": 0.4, "NU": 0.3, "NOW": 0.2, "SGOV": 0.1}
    mc = build_book_monte_carlo(tmp_path, weights, n_paths=30_000, seed=11)
    assert mc is not None
    assert mc.student_t.pct_1st < mc.normal.pct_1st
    assert mc.student_t.pct_5th < mc.normal.pct_5th
    assert mc.student_t.prob_below["-30%"] >= mc.normal.prob_below["-30%"]
    assert set(mc.normal.prob_below) == set(DRAWDOWN_LABELS)
    assert set(mc.student_t.prob_below) == set(DRAWDOWN_LABELS)


def test_student_t_mean_and_vol_stay_bounded_even_with_pathological_draws(
    tmp_path: Path,
) -> None:
    """``exp(t-shock)`` has NO finite population moments (a log-t random
    variable's tail decays too slowly for E[exp(X)] to converge) — a rare
    near-zero shared chi-square draw among 50k paths can otherwise send the
    Student-t model's arithmetic mean/vol to absurd magnitudes (verified live
    against the real portfolio book during PR4 development: mean_pct hit
    ~3344%, vol_pct ~639,487% before the ``_MAX_SIMPLE_RETURN`` clip). This is
    a permanent invariant on the clipped output, not a one-off number."""
    _seed_book(tmp_path)
    mc = build_book_monte_carlo(
        tmp_path, {"MELI": 0.4, "NU": 0.3, "NOW": 0.3}, n_paths=50_000, seed=42
    )
    assert mc is not None
    assert abs(mc.student_t.mean_pct) < 10_000.0
    assert mc.student_t.vol_pct < 10_000.0
    # The clip must never touch the left-tail percentiles/probabilities —
    # they live on the opposite, naturally -100%-floored tail.
    assert mc.student_t.pct_1st < 0.0
    assert mc.student_t.prob_below["-20%"] > 0.0


def test_deterministic_for_fixed_seed(tmp_path: Path) -> None:
    _seed_book(tmp_path)
    weights = {"MELI": 0.4, "NU": 0.3, "NOW": 0.3}
    a = build_book_monte_carlo(tmp_path, weights, n_paths=10_000, seed=5)
    b = build_book_monte_carlo(tmp_path, weights, n_paths=10_000, seed=5)
    assert a is not None and b is not None
    assert a.normal.mean_pct == b.normal.mean_pct
    assert a.normal.vol_pct == b.normal.vol_pct
    assert a.student_t.pct_1st == b.student_t.pct_1st
    assert a.normal.prob_below == b.normal.prob_below


def test_different_seed_changes_the_read(tmp_path: Path) -> None:
    _seed_book(tmp_path)
    weights = {"MELI": 0.4, "NU": 0.3, "NOW": 0.3}
    a = build_book_monte_carlo(tmp_path, weights, n_paths=10_000, seed=5)
    b = build_book_monte_carlo(tmp_path, weights, n_paths=10_000, seed=6)
    assert a is not None and b is not None
    assert a.normal.pct_1st != b.normal.pct_1st


def test_analytic_vol_tracks_simulated_normal_vol(tmp_path: Path) -> None:
    """analytic_vol_pct (closed-form sqrt(w' Sigma w)) and the simulated
    normal model's vol derive from the SAME annual covariance — they should
    land in the same ballpark (the simulated figure also carries expm1()
    convexity + Monte Carlo noise, so this is a sanity band, not equality)."""
    _seed_book(tmp_path)
    mc = build_book_monte_carlo(
        tmp_path, {"MELI": 0.4, "NU": 0.3, "NOW": 0.3}, n_paths=40_000, seed=3
    )
    assert mc is not None
    assert mc.analytic_vol_pct > 0.0
    # A generous band, not a tight match: expm1()'s convexity on a ~20-30%
    # vol book (Jensen's inequality — Var[exp(X)] > Var[X] for normal X) is a
    # real, expected divergence between the log-covariance figure and the
    # simulated simple-return vol, not simulation noise to average away.
    ratio = mc.normal.vol_pct / mc.analytic_vol_pct
    assert 0.5 < ratio < 3.0


def test_drift_override_shifts_the_mean(tmp_path: Path) -> None:
    _seed_book(tmp_path)
    weights = {"MELI": 0.4, "NU": 0.3, "NOW": 0.3}
    base = build_book_monte_carlo(tmp_path, weights, n_paths=20_000, seed=9)
    boosted = build_book_monte_carlo(
        tmp_path,
        weights,
        n_paths=20_000,
        seed=9,
        drift_annual_override={"MELI": 5.0, "NU": 5.0, "NOW": 5.0},
    )
    assert base is not None and boosted is not None
    assert boosted.normal.mean_pct > base.normal.mean_pct
    assert "overridden" in boosted.drift_source


def test_cash_like_tickers_registry() -> None:
    assert frozenset({"SGOV", "FDRXX", "CUR:USD"}) == CASH_LIKE_TICKERS


# ---------------------------------------------------------------------------
# Event-correlation stress (joint_latam)
# ---------------------------------------------------------------------------


def _make_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "CREATE TABLE dcf_runs ("
        "id INTEGER PRIMARY KEY, ticker TEXT, created_at TEXT, valuation_date TEXT, "
        "npv_per_share REAL, live_price REAL, live_price_at TEXT, "
        "assumption_snapshot_json TEXT)"
    )
    conn.commit()
    conn.close()
    return db_path


def _insert_bear_run(db_path: Path, ticker: str, *, live_price: float, bear_fv: float) -> None:
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "INSERT INTO dcf_runs "
        "(ticker, created_at, valuation_date, npv_per_share, live_price, live_price_at, "
        "assumption_snapshot_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            ticker,
            "2026-06-01T00:00:00",
            "2026-06-15",
            live_price * 1.2,
            live_price,
            "2026-07-01",
            json.dumps({"scenarios": {"bear": {"fair_value_per_share_usd": bear_fv}}}),
        ),
    )
    conn.commit()
    conn.close()


def test_joint_latam_no_weights_returns_none(tmp_path: Path) -> None:
    db_path = _make_db(tmp_path)
    assert build_joint_latam_stress(db_path, {}) is None


def test_joint_latam_uses_persisted_bear_when_present(tmp_path: Path) -> None:
    db_path = _make_db(tmp_path)
    _insert_bear_run(db_path, "MELI", live_price=2000.0, bear_fv=1200.0)  # -40%
    _insert_bear_run(db_path, "NU", live_price=10.0, bear_fv=6.0)  # -40%
    weights = {"MELI": 0.3, "NU": 0.2, "NOW": 0.3, "SGOV": 0.2}
    result = build_joint_latam_stress(db_path, weights)
    assert result is not None
    by_ticker = {leg.ticker: leg for leg in result.legs}
    assert by_ticker["MELI"].return_pct == pytest.approx(-40.0)
    assert by_ticker["MELI"].label == "persisted bear-case DCF fair value"
    assert by_ticker["NU"].return_pct == pytest.approx(-40.0)
    assert by_ticker["NOW"].return_pct == pytest.approx(-15.0)
    assert by_ticker["NOW"].label == "other equities (generic macro drag)"
    assert by_ticker["SGOV"].return_pct == pytest.approx(0.0)
    assert by_ticker["SGOV"].label == "cash-like (flat)"
    assert result.notes == []
    expected = 0.3 * -40.0 + 0.2 * -40.0 + 0.3 * -15.0 + 0.2 * 0.0
    assert result.book_return_pct == pytest.approx(expected)
    assert result.modeled_weight_pct == pytest.approx(100.0)


def test_joint_latam_falls_back_when_no_bear_persisted(tmp_path: Path) -> None:
    db_path = _make_db(tmp_path)
    weights = {"MELI": 0.3, "NU": 0.2, "NOW": 0.3}
    result = build_joint_latam_stress(db_path, weights)
    assert result is not None
    by_ticker = {leg.ticker: leg for leg in result.legs}
    assert by_ticker["MELI"].return_pct == pytest.approx(-38.0)
    assert "fallback" in by_ticker["MELI"].label
    assert by_ticker["NU"].return_pct == pytest.approx(-52.0)
    assert "fallback" in by_ticker["NU"].label
    assert result.notes and "MELI" in result.notes[0] and "NU" in result.notes[0]
    expected = 0.3 * -38.0 + 0.2 * -52.0 + 0.3 * -15.0
    assert result.book_return_pct == pytest.approx(expected)


def test_event_scenario_registry_has_joint_latam() -> None:
    ids = [s.id for s in EVENT_SCENARIOS]
    assert "joint_latam" in ids
    scenario = next(s for s in EVENT_SCENARIOS if s.id == "joint_latam")
    assert set(scenario.named_tickers) == {"MELI", "NU"}


def test_build_event_stress_directly_with_scenario_def(tmp_path: Path) -> None:
    db_path = _make_db(tmp_path)
    scenario = EVENT_SCENARIOS[0]
    result = build_event_stress(scenario, db_path, {"MELI": 1.0})
    assert result is not None
    assert result.scenario_id == scenario.id


def test_event_scenario_registry_c5_additions() -> None:
    """C5 (2026-07-19 plan): the three event correlations the price matrix
    can't see — policy, multiple-regime, and ad-cycle clusters — with every
    named ticker carrying a labeled fallback (no silent substitution)."""
    by_id = {s.id: s for s in EVENT_SCENARIOS}
    assert set(by_id["glp1_pricing_shock"].named_tickers) == {"NVO"}
    assert set(by_id["saas_multiple_compression"].named_tickers) == {
        "NOW",
        "VEEV",
        "WIX",
        "RBRK",
    }
    assert set(by_id["ad_recession"].named_tickers) == {"META", "GOOGL"}
    for scenario in by_id.values():
        assert set(scenario.fallback_returns_pct) == set(scenario.named_tickers)
        assert all(v < 0 for v in scenario.fallback_returns_pct.values())
