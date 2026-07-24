"""allocation/what_if.py — before→after book stats at a chosen weight.

Pure-math checks on synthetic price charts (no network): identity when the
candidate IS the book, a hand-computed blend, degraded paths, weight
clamping, and the module result cache.
"""

from __future__ import annotations

import json
import math
import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from allocation.what_if import (  # noqa: E402
    ALLOWED_WEIGHTS,
    FUNDING_MODES,
    WhatIfResult,
    clear_caches,
    compute_what_if,
    validate_weight,
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


def test_validate_weight_passes_through_with_rounding() -> None:
    assert validate_weight(0.03) == 0.03
    assert validate_weight(0.031234) == pytest.approx(0.0312)  # rounded to 4 decimals
    assert validate_weight(0.25) == 0.25  # top of the allowed range, inclusive
    assert validate_weight(0.001) == pytest.approx(0.001)  # no silent clamp to a preset
    assert all(validate_weight(w) == w for w in ALLOWED_WEIGHTS)


def test_validate_weight_menu_extends_through_zone_boundaries() -> None:
    # PRD §7.2, P0.2 extends the UI preset menu through the concentration-zone
    # boundaries (10/12/15/20/25%), on top of the original 1/2/3/5/8%.
    assert ALLOWED_WEIGHTS == (0.01, 0.02, 0.03, 0.05, 0.08, 0.10, 0.12, 0.15, 0.20, 0.25)


def test_validate_weight_rejects_zero() -> None:
    with pytest.raises(ValueError, match="1%"):
        validate_weight(0.0)


def test_validate_weight_rejects_above_25pct_and_lists_presets() -> None:
    with pytest.raises(ValueError) as exc_info:
        validate_weight(0.5)
    msg = str(exc_info.value)
    assert "25%" in msg
    # Every preset percentage is named in the error.
    for w in ALLOWED_WEIGHTS:
        assert f"{w * 100:g}%" in msg


def test_validate_weight_rejects_negative() -> None:
    with pytest.raises(ValueError):
        validate_weight(-0.01)


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


def test_result_cache_keys_on_funding_mode(tmp_path: Path, market: list[float]) -> None:
    """The two funding modes are mathematically identical (same book blend)
    but must never share a cache slot — each is its own result object,
    tagged with its own ``funding_mode`` (PRD §7.2, P0.2)."""
    _write_chart(tmp_path, "AAA", list(market))
    _write_chart(tmp_path, "CCC", [0.01 * math.cos(i / 5.0) for i in range(200)])
    weights = {"AAA": 1.0}
    r_cash = compute_what_if(
        tmp_path,
        "CCC",
        0.03,
        book_weights=weights,
        risk_free_annual=0.02,
        book_growth_tilt=None,
        funding_mode="new_cash",
    )
    r_pro_rata = compute_what_if(
        tmp_path,
        "CCC",
        0.03,
        book_weights=weights,
        risk_free_annual=0.02,
        book_growth_tilt=None,
        funding_mode="pro_rata_reallocation",
    )
    assert r_cash is not r_pro_rata
    assert r_cash.funding_mode == "new_cash"
    assert r_pro_rata.funding_mode == "pro_rata_reallocation"
    # Same math regardless of framing.
    assert r_cash.vol_after_ann == pytest.approx(r_pro_rata.vol_after_ann)
    assert r_cash.sharpe_delta_bps == pytest.approx(r_pro_rata.sharpe_delta_bps)


def test_funding_modes_contents() -> None:
    assert FUNDING_MODES == ("new_cash", "pro_rata_reallocation")


def test_compute_what_if_rejects_unknown_funding_mode(tmp_path: Path, market: list[float]) -> None:
    _write_chart(tmp_path, "AAA", list(market))
    _write_chart(tmp_path, "CCC", list(market))
    with pytest.raises(ValueError, match="funding_mode"):
        compute_what_if(
            tmp_path,
            "CCC",
            0.03,
            book_weights={"AAA": 1.0},
            risk_free_annual=0.02,
            book_growth_tilt=None,
            funding_mode="cash_out_a_kidney",
        )


# --------------------------------------------------------------------------- #
# resulting_weight_pct + zone (PRD §7.2, P0.2)
# --------------------------------------------------------------------------- #


def test_resulting_weight_pct_for_a_new_name_is_just_the_add(
    tmp_path: Path, market: list[float]
) -> None:
    """CCC isn't in the book at all: resulting weight is simply w*100 (0 blend)."""
    _write_chart(tmp_path, "AAA", list(market))
    _write_chart(tmp_path, "CCC", [0.01 * math.cos(i / 5.0) for i in range(200)])
    r = compute_what_if(
        tmp_path,
        "CCC",
        0.05,
        book_weights={"AAA": 1.0},
        risk_free_annual=0.02,
        book_growth_tilt=None,
    )
    assert r.resulting_weight_pct == pytest.approx(5.0)
    assert r.zone == "ordinary"  # 5% < 10%


def test_resulting_weight_pct_blends_an_existing_holdings_weight(
    tmp_path: Path, market: list[float]
) -> None:
    """AAA is already 20% of the book (w0=0.20); adding 5% more blends to
    w0*(1-w) + w = 0.20*0.95 + 0.05 = 0.24 -> 24%, the "exceptional" zone
    (>= 20%)."""
    _write_chart(tmp_path, "AAA", list(market))
    _write_chart(tmp_path, "BBB", [0.008 * math.sin(i / 4.0) for i in range(200)])
    r = compute_what_if(
        tmp_path,
        "AAA",
        0.05,
        book_weights={"AAA": 0.20, "BBB": 0.80},
        risk_free_annual=0.02,
        book_growth_tilt=None,
    )
    expected_pct = (0.20 * (1.0 - 0.05) + 0.05) * 100.0
    assert r.resulting_weight_pct == pytest.approx(expected_pct)
    assert expected_pct == pytest.approx(24.0)
    assert r.zone == "exceptional"  # 24% >= 20%


# --------------------------------------------------------------------------- #
# C7 — factor_vector_before/after (src.risk_factors, C3 business-factor
# exposures, pro-rata blended the same way sector_mix_after already is)
# --------------------------------------------------------------------------- #

_FACTOR_DDL = """
CREATE TABLE business_factor_exposures (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    factor TEXT NOT NULL,
    loading REAL NOT NULL,
    rationale TEXT,
    provenance TEXT NOT NULL,
    input_sha TEXT,
    owner_edited INTEGER NOT NULL DEFAULT 0,
    is_latest INTEGER NOT NULL DEFAULT 1,
    superseded_by INTEGER,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


def _write_factor_exposures(db_path: Path, rows: list[tuple[str, str, float, bool]]) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.executescript(_FACTOR_DDL)
    now = "2026-07-24T00:00:00"
    for ticker, factor, loading, is_latest in rows:
        conn.execute(
            "INSERT INTO business_factor_exposures "
            "(ticker, factor, loading, provenance, is_latest, created_at, updated_at) "
            "VALUES (?, ?, ?, 'segment_derived', ?, ?, ?)",
            (ticker, factor, loading, int(is_latest), now, now),
        )
    conn.commit()
    conn.close()


def _write_weights_cache(repo_root: Path, weights: dict[str, float]) -> None:
    cache = repo_root / "data" / "portfolio_weights.json"
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(
        json.dumps({"computed_at": "2026-07-24T00:00:00", "weights": weights}), encoding="utf-8"
    )


def test_factor_vector_before_after_pro_rata_blend(tmp_path: Path, market: list[float]) -> None:
    _write_chart(tmp_path, "AAA", list(market))
    _write_chart(tmp_path, "CCC", [0.01 * math.cos(i / 5.0) for i in range(200)])
    _write_weights_cache(tmp_path, {"AAA": 1.0})
    db_path = tmp_path / "data" / "portfolio.db"
    _write_factor_exposures(
        db_path,
        [
            ("AAA", "Brazil consumer credit", 0.8, True),
            ("CCC", "Brazil consumer credit", 0.6, True),
            ("CCC", "digital ad spend", 0.4, True),
            ("CCC", "STALE_FACTOR", 0.99, False),  # superseded — must be excluded
        ],
    )
    w = 0.10
    r = compute_what_if(
        tmp_path,
        "CCC",
        w,
        book_weights={"AAA": 1.0},
        risk_free_annual=0.02,
        book_growth_tilt=None,
        db_path=db_path,
    )
    assert r.factor_vector_before == pytest.approx({"Brazil consumer credit": 0.8})
    expected_after = {
        "Brazil consumer credit": (1.0 - w) * 0.8 + w * 0.6,
        "digital ad spend": (1.0 - w) * 0.0 + w * 0.4,
    }
    assert r.factor_vector_after == pytest.approx(expected_after)


def test_factor_vector_absent_without_db_path(tmp_path: Path, market: list[float]) -> None:
    """db_path defaults to None (today's only caller, pipeline.peeks, doesn't
    pass one) -> the factor legs are absent, never raising, never computed."""
    _write_chart(tmp_path, "AAA", list(market))
    _write_chart(tmp_path, "CCC", list(market))
    r = compute_what_if(
        tmp_path,
        "CCC",
        0.05,
        book_weights={"AAA": 1.0},
        risk_free_annual=0.02,
        book_growth_tilt=None,
    )
    assert r.factor_vector_before is None
    assert r.factor_vector_after is None


def test_factor_vector_absent_table_degrades_cleanly(tmp_path: Path, market: list[float]) -> None:
    """A db_path pointing at a real DB that predates the C3 migration (no
    business_factor_exposures table) must degrade to an absent section, never
    raise and never appear in ``degraded`` (a missing table is a clean
    absence, not a failure — book_factor_vector already treats it that way)."""
    _write_chart(tmp_path, "AAA", list(market))
    _write_chart(tmp_path, "CCC", list(market))
    _write_weights_cache(tmp_path, {"AAA": 1.0})
    db_path = tmp_path / "data" / "portfolio.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    sqlite3.connect(str(db_path)).close()  # a real, empty DB file — no tables

    r = compute_what_if(
        tmp_path,
        "CCC",
        0.05,
        book_weights={"AAA": 1.0},
        risk_free_annual=0.02,
        book_growth_tilt=None,
        db_path=db_path,
    )
    assert r.factor_vector_before is None
    assert r.factor_vector_after is None
    assert not any("business-factor" in d for d in r.degraded)


def test_factor_vector_result_cache_distinguishes_db_path(
    tmp_path: Path, market: list[float]
) -> None:
    """A call WITH db_path must never be served from a cache entry populated
    by an earlier call WITHOUT one (or vice versa) — the result cache key
    must include db_path."""
    _write_chart(tmp_path, "AAA", list(market))
    _write_chart(tmp_path, "CCC", [0.01 * math.cos(i / 5.0) for i in range(200)])
    _write_weights_cache(tmp_path, {"AAA": 1.0})
    db_path = tmp_path / "data" / "portfolio.db"
    _write_factor_exposures(
        db_path,
        [("AAA", "digital ad spend", 0.3, True), ("CCC", "digital ad spend", 0.5, True)],
    )

    weights = {"AAA": 1.0}
    r_without = compute_what_if(
        tmp_path, "CCC", 0.05, book_weights=weights, risk_free_annual=0.02, book_growth_tilt=None
    )
    r_with = compute_what_if(
        tmp_path,
        "CCC",
        0.05,
        book_weights=weights,
        risk_free_annual=0.02,
        book_growth_tilt=None,
        db_path=db_path,
    )
    assert r_without.factor_vector_after is None
    assert r_with.factor_vector_after is not None
