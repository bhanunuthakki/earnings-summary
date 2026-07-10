"""allocation/what_if.py — before→after book stats at a chosen weight.

Pure-math checks on synthetic price charts (no network): identity when the
candidate IS the book, a hand-computed blend, degraded paths, weight
clamping, and the module result cache.
"""

from __future__ import annotations

import json
import math
import sys
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from allocation.what_if import (  # noqa: E402
    ALLOWED_WEIGHTS,
    WhatIfResult,
    clamp_weight,
    clear_caches,
    compute_what_if,
)

_START = date(2024, 1, 1)


def _write_chart(repo: Path, ticker: str, returns: list[float], start_price: float = 100.0) -> None:
    fmp = repo / "data" / "historical" / "fmp"
    fmp.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, object]] = [{"date": _START.isoformat(), "adjClose": start_price}]
    price = start_price
    for i, r in enumerate(returns, start=1):
        price *= math.exp(r)
        rows.append({"date": (_START + timedelta(days=i)).isoformat(), "adjClose": price})
    (fmp / f"{ticker.upper()}_price_chart_10y_div_adj.json").write_text(
        json.dumps(rows), encoding="utf-8"
    )


@pytest.fixture(autouse=True)
def _fresh_caches() -> None:
    clear_caches()


@pytest.fixture
def market() -> list[float]:
    return [0.012 * math.sin(i / 6.0) + 0.0005 for i in range(200)]


def test_clamp_weight_snaps_to_menu() -> None:
    assert clamp_weight(0.03) == 0.03
    assert clamp_weight(0.031) == 0.03
    assert clamp_weight(0.0) == 0.01
    assert clamp_weight(0.5) == 0.08
    assert all(clamp_weight(w) == w for w in ALLOWED_WEIGHTS)


def test_candidate_identical_to_book_is_a_no_op(tmp_path: Path, market: list[float]) -> None:
    """Blending in a name that IS the book changes nothing: vol and Sharpe
    before == after, ΔSR ≈ 0."""
    _write_chart(tmp_path, "AAA", list(market))
    _write_chart(tmp_path, "BBB", list(market))
    _write_chart(tmp_path, "SAME", list(market))
    r = compute_what_if(
        tmp_path,
        "SAME",
        0.05,
        book_weights={"AAA": 0.5, "BBB": 0.5},
        risk_free_annual=0.02,
        book_growth_tilt=0.3,
    )
    assert r.vol_before_ann is not None and r.vol_after_ann is not None
    assert r.vol_after_ann == pytest.approx(r.vol_before_ann, rel=1e-9)
    assert r.sharpe_delta_bps == pytest.approx(0.0, abs=1e-6)
    assert r.obs is not None and r.obs >= 120
    assert r.prices_through is not None
    assert r.degraded == () or all("tilt" in d for d in r.degraded)


def test_two_asset_blend_matches_hand_math(tmp_path: Path, market: list[float]) -> None:
    """Book = one holding, candidate = an independent series: the after-vol and
    Sharpe must equal the numpy-computed stats of (1-w)·book + w·cand."""
    book_rets = list(market)
    cand_rets = [0.010 * math.cos(i / 5.0) for i in range(200)]
    _write_chart(tmp_path, "AAA", book_rets)
    _write_chart(tmp_path, "CCC", cand_rets)
    w, rf = 0.05, 0.02
    r = compute_what_if(
        tmp_path,
        "CCC",
        w,
        book_weights={"AAA": 1.0},
        risk_free_annual=rf,
        book_growth_tilt=None,
    )
    assert r.obs is not None
    # Reproduce: the aligned window keeps the latest obs; both series share
    # the calendar so the full 200 returns align.
    a = np.array(book_rets[-r.obs :])
    c = np.array(cand_rets[-r.obs :])
    blend = (1 - w) * a + w * c

    def _stats(x: np.ndarray) -> tuple[float, float]:
        sd = float(np.std(x))  # population, matching _mean_var
        vol = sd * math.sqrt(252.0)
        sharpe = (float(np.mean(x)) * 252.0 - rf) / vol
        return vol, sharpe

    vol_b, sr_b = _stats(a)
    vol_a, sr_a = _stats(blend)
    assert r.vol_before_ann == pytest.approx(vol_b, rel=1e-9)
    assert r.vol_after_ann == pytest.approx(vol_a, rel=1e-9)
    assert r.sharpe_before == pytest.approx(sr_b, rel=1e-9)
    assert r.sharpe_after == pytest.approx(sr_a, rel=1e-9)
    assert r.sharpe_delta_bps == pytest.approx((sr_a - sr_b) * 1e4, rel=1e-9)
    # Top correlations: exactly the one holding.
    assert len(r.top_correlations) == 1 and r.top_correlations[0][0] == "AAA"
    # Book tilt unknown → tilt leg degraded, not fabricated.
    assert r.growth_tilt_after is None
    assert any("tilt" in d for d in r.degraded)


def test_sector_mix_after_renormalizes(tmp_path: Path, market: list[float]) -> None:
    _write_chart(tmp_path, "AAA", list(market))
    _write_chart(tmp_path, "CCC", [0.01 * math.cos(i / 5.0) for i in range(200)])
    r = compute_what_if(
        tmp_path,
        "CCC",
        0.08,
        book_weights={"AAA": 1.0},
        risk_free_annual=0.02,
        book_growth_tilt=None,
        sector="Energy",
        book_sector_weights={"Technology": 0.6, "Healthcare": 0.4},
    )
    assert r.sector_mix_after is not None
    assert r.sector_mix_after["Technology"] == pytest.approx(0.6 * 0.92)
    assert r.sector_mix_after["Healthcare"] == pytest.approx(0.4 * 0.92)
    assert r.sector_mix_after["Energy"] == pytest.approx(0.08)
    assert sum(r.sector_mix_after.values()) == pytest.approx(1.0)


def test_missing_candidate_history_degrades(tmp_path: Path, market: list[float]) -> None:
    _write_chart(tmp_path, "AAA", list(market))
    _write_chart(tmp_path, "BBB", list(market))
    r = compute_what_if(
        tmp_path,
        "GHOST",
        0.03,
        book_weights={"AAA": 0.5, "BBB": 0.5},
        risk_free_annual=0.02,
        book_growth_tilt=0.3,
    )
    assert r.vol_after_ann is None and r.sharpe_delta_bps is None
    assert any("GHOST" in d for d in r.degraded)


def test_missing_rf_degrades_sharpe_but_not_vol(tmp_path: Path, market: list[float]) -> None:
    _write_chart(tmp_path, "AAA", list(market))
    _write_chart(tmp_path, "CCC", [0.01 * math.cos(i / 5.0) for i in range(200)])
    r = compute_what_if(
        tmp_path,
        "CCC",
        0.03,
        book_weights={"AAA": 1.0},
        risk_free_annual=None,
        book_growth_tilt=None,
    )
    assert r.vol_before_ann is not None and r.vol_after_ann is not None
    assert r.sharpe_before is None and r.sharpe_delta_bps is None
    assert any("risk-free" in d for d in r.degraded)


def test_result_cache_hit_returns_same_object(tmp_path: Path, market: list[float]) -> None:
    _write_chart(tmp_path, "AAA", list(market))
    _write_chart(tmp_path, "CCC", [0.01 * math.cos(i / 5.0) for i in range(200)])
    weights = {"AAA": 1.0}
    r1 = compute_what_if(
        tmp_path, "CCC", 0.03, book_weights=weights, risk_free_annual=0.02, book_growth_tilt=None
    )
    r2 = compute_what_if(
        tmp_path, "CCC", 0.03, book_weights=weights, risk_free_annual=0.02, book_growth_tilt=None
    )
    assert isinstance(r1, WhatIfResult)
    assert r2 is r1  # served from the module result cache
    r3 = compute_what_if(
        tmp_path, "CCC", 0.05, book_weights=weights, risk_free_annual=0.02, book_growth_tilt=None
    )
    assert r3 is not r1 and r3.weight == 0.05
