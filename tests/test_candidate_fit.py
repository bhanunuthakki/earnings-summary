"""Tests for allocation/candidate_fit.py — portfolio-fit scoring of evaluation
names. The pure scorer (band edges, missing→neutral, tone, why format) is tested
directly; the gatherer is exercised against synthetic on-disk price charts in a
tmp repo so the price-history geometry (corr to book, candidate Sharpe, growth
tilt) runs without a network or the real FMP cache.
"""

from __future__ import annotations

import json
import math
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from allocation.candidate_fit import (  # noqa: E402
    BookContext,
    CandidateRisk,
    compute_candidate_fit,
    fit_tone,
    score_candidate_fit,
)

# --------------------------------------------------------------------------- #
# Pure scorer — band edges
# --------------------------------------------------------------------------- #


def _factors(fit) -> dict[str, float]:  # type: ignore[no-untyped-def]
    return {f.key: f.multiplier for f in fit.factors}


def test_marginal_sharpe_bands() -> None:
    """A name lifts the book Sharpe iff SR_cand > SR_book·ρ; the improvement
    margin bands best-first, below the last band falling to the floor."""
    book = BookContext(weights={}, sharpe=1.0)  # hurdle = 1.0 * corr

    def m(sr: float, corr: float = 0.0) -> float:
        risk = CandidateRisk(ticker="X", sharpe=sr, corr_to_book=corr)
        return _factors(score_candidate_fit(risk, book))["sharpe"]

    assert m(0.30) == pytest.approx(1.25)
    assert m(0.10) == pytest.approx(1.12)
    assert m(0.00) == pytest.approx(1.0)
    assert m(-0.10) == pytest.approx(1.0)
    assert m(-0.30) == pytest.approx(0.90)
    assert m(-0.31) == pytest.approx(0.80)
    # The hurdle scales with correlation: same SR, a more correlated name clears
    # a higher bar. SR 0.55 vs book 1.0 x corr 0.8 = hurdle 0.8 -> margin -0.25
    # -> 0.90, while SR 0.55 against an uncorrelated book clears the top band.
    assert m(0.55, corr=0.8) == pytest.approx(0.90)
    assert m(0.55, corr=0.0) == pytest.approx(1.25)


def test_diversification_bands() -> None:
    """Correlation to book, lower is better (value <= threshold wins)."""

    def d(corr: float) -> float:
        return _factors(
            score_candidate_fit(
                CandidateRisk(ticker="X", corr_to_book=corr), BookContext(weights={})
            )
        )["divers"]

    assert d(0.2) == pytest.approx(1.20)
    assert d(-0.5) == pytest.approx(1.20)  # a hedge clears the best band
    assert d(0.4) == pytest.approx(1.10)
    assert d(0.6) == pytest.approx(1.0)
    assert d(0.8) == pytest.approx(0.92)
    assert d(0.95) == pytest.approx(0.85)


def test_factor_fit_crowding_vs_balance() -> None:
    """Alignment = sign(book tilt) · candidate tilt: same-sign deepens the lean
    (drag), opposite-sign balances it (lift), small either way is neutral."""
    growth_book = BookContext(weights={}, growth_tilt=0.4)  # growth-leaning book
    assert _factors(score_candidate_fit(CandidateRisk(ticker="X", growth_tilt=0.5), growth_book))[
        "factor"
    ] == pytest.approx(0.88)
    assert _factors(score_candidate_fit(CandidateRisk(ticker="X", growth_tilt=-0.5), growth_book))[
        "factor"
    ] == pytest.approx(1.12)
    assert _factors(score_candidate_fit(CandidateRisk(ticker="X", growth_tilt=0.1), growth_book))[
        "factor"
    ] == pytest.approx(1.0)
    # A value-leaning book (negative tilt): a value name (negative tilt) now
    # CROWDS, a growth name balances — the sign flips with the book.
    value_book = BookContext(weights={}, growth_tilt=-0.4)
    assert _factors(score_candidate_fit(CandidateRisk(ticker="X", growth_tilt=-0.5), value_book))[
        "factor"
    ] == pytest.approx(0.88)
    assert _factors(score_candidate_fit(CandidateRisk(ticker="X", growth_tilt=0.5), value_book))[
        "factor"
    ] == pytest.approx(1.12)


def test_sector_fit_bands() -> None:
    def s(book_weight: float) -> float:
        book = BookContext(weights={}, sector_weights={"Technology": book_weight})
        return _factors(score_candidate_fit(CandidateRisk(ticker="X", sector="Technology"), book))[
            "sector"
        ]

    assert s(0.30) == pytest.approx(0.88)  # heavy
    assert s(0.20) == pytest.approx(0.95)  # warm
    assert s(0.12) == pytest.approx(1.0)  # balanced
    assert s(0.05) == pytest.approx(1.08)  # under-represented
    # A sector the book doesn't hold at all is under-represented (weight 0).
    book = BookContext(weights={}, sector_weights={"Energy": 0.5})
    assert _factors(score_candidate_fit(CandidateRisk(ticker="X", sector="Healthcare"), book))[
        "sector"
    ] == pytest.approx(1.08)


def test_missing_factors_are_neutral_not_dilutive() -> None:
    """A factor whose inputs are unavailable contributes a NEUTRAL 1.0 (unlike
    the score's x0.85 miss) and flags the read partial — an unknowable fit must
    never read as dilutive. An all-missing candidate scores exactly 1.0."""
    fit = score_candidate_fit(CandidateRisk(ticker="X"), BookContext(weights={}))
    assert fit.fit == pytest.approx(1.0)
    assert fit.partial
    assert all(f.missing and f.multiplier == 1.0 and f.detail == "n/a" for f in fit.factors)
    # One known factor + three missing: fit = that factor alone, still partial.
    only_divers = score_candidate_fit(
        CandidateRisk(ticker="X", corr_to_book=0.2), BookContext(weights={})
    )
    assert only_divers.fit == pytest.approx(1.20)
    assert only_divers.partial


def test_fit_tone_and_why_format() -> None:
    assert fit_tone(1.10) == "hi"
    assert fit_tone(0.90) == "lo"
    assert fit_tone(1.0) == ""
    risk = CandidateRisk(
        ticker="X", corr_to_book=0.2, sharpe=0.4, growth_tilt=-0.5, sector="Energy"
    )
    book = BookContext(weights={}, sharpe=1.0, growth_tilt=0.4, sector_weights={"Tech": 0.5})
    fit = score_candidate_fit(risk, book)
    # The why string mirrors the score chip: "key mult (detail) x ... = fit".
    assert fit.why.startswith("sharpe ")
    assert " x divers " in fit.why
    assert fit.why.endswith(f"= {fit.fit:.2f}")
    assert not fit.partial  # all four factors had inputs


# --------------------------------------------------------------------------- #
# Gatherer — synthetic price charts in a tmp repo
# --------------------------------------------------------------------------- #

_START = date(2024, 1, 1)


def _write_chart(repo: Path, ticker: str, returns: list[float], start_price: float = 100.0) -> None:
    """Write an FMP price-chart JSON whose adjClose path integrates ``returns``
    (one row per day, ascending). ``len(returns)+1`` rows → ``len(returns)``
    daily log returns."""
    fmp = repo / "data" / "historical" / "fmp"
    fmp.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, object]] = []
    price = start_price
    rows.append({"date": _START.isoformat(), "adjClose": price})
    for i, r in enumerate(returns, start=1):
        price *= math.exp(r)
        rows.append({"date": (_START + timedelta(days=i)).isoformat(), "adjClose": price})
    (fmp / f"{ticker.upper()}_price_chart_10y_div_adj.json").write_text(
        json.dumps(rows), encoding="utf-8"
    )


@pytest.fixture
def market() -> list[float]:
    # Deterministic, non-degenerate market series (200 daily returns > the
    # 120-day overlap floor).
    return [0.012 * math.sin(i / 6.0) for i in range(200)]


def _seed_book_and_benchmarks(repo: Path, market: list[float]) -> None:
    n = len(market)
    # Two holdings ≈ market + tiny idiosyncratic noise → book ≈ market.
    _write_chart(repo, "AAA", [m + 0.0008 * ((i % 3) - 1) for i, m in enumerate(market)])
    _write_chart(repo, "BBB", [m + 0.0008 * ((i % 5) - 2) for i, m in enumerate(market)])
    _write_chart(repo, "SPY", list(market))  # SPY = market
    _write_chart(repo, "QQQ", [1.5 * m for m in market])  # QQQ = 1.5x market
    del n


def test_compute_fit_correlated_vs_diversifier(tmp_path: Path, market: list[float]) -> None:
    """A candidate that tracks the book is a poor diversifier (corr→floor) and
    crowds the book's growth lean; an anti-correlated candidate diversifies and
    balances. Sectors flow through to the sector factor."""
    _seed_book_and_benchmarks(tmp_path, market)
    _write_chart(tmp_path, "CORR", list(market))  # tracks the market (≈ the book)
    _write_chart(tmp_path, "DIVR", [-m for m in market])  # mirror image of the book

    book = BookContext(
        weights={"AAA": 0.5, "BBB": 0.5},
        sharpe=1.0,
        risk_free_annual=0.02,
        growth_tilt=0.4,  # growth-leaning book
        sector_weights={"Technology": 0.40},
    )
    fits = compute_candidate_fit(
        tmp_path, ["corr", "divr"], book, sectors={"CORR": "Technology", "DIVR": "Energy"}
    )
    corr, divr = fits["CORR"], fits["DIVR"]
    fc = {f.key: f for f in corr.factors}
    fd = {f.key: f for f in divr.factors}

    # Diversification: CORR tracks the book (corr≈1 → floor); DIVR is a hedge.
    assert fc["divers"].multiplier == pytest.approx(0.85)
    assert fd["divers"].multiplier == pytest.approx(1.20)
    # Sector: CORR adds to a 40% Technology book; DIVR opens Energy (unheld).
    assert fc["sector"].multiplier == pytest.approx(0.88)
    assert fd["sector"].multiplier == pytest.approx(1.08)
    # The factor + Sharpe legs are computed from price history (not asserted on
    # an exact band here — the synthetic benchmark betas are scale artifacts; the
    # band math is pinned precisely by the pure-scorer tests above).
    assert not fc["factor"].missing and not fd["factor"].missing
    assert not fc["sharpe"].missing and not fd["sharpe"].missing
    assert not corr.partial and not divr.partial
    # The diversifier is the more accretive next-dollar fit overall (its corr +
    # sector legs dominate, and a hedge clears the marginal-Sharpe hurdle).
    assert divr.fit > corr.fit
    assert fit_tone(divr.fit) == "hi"


def test_compute_fit_thin_history_degrades_partial(tmp_path: Path, market: list[float]) -> None:
    """A candidate with too few overlapping days gets no price-derived factors —
    they degrade to neutral and the read is partial, never a fabricated number."""
    _seed_book_and_benchmarks(tmp_path, market)
    _write_chart(tmp_path, "IPO", [0.01, -0.02, 0.015, 0.0, -0.01])  # 5 returns « 120
    book = BookContext(
        weights={"AAA": 0.5, "BBB": 0.5},
        sharpe=1.0,
        risk_free_annual=0.02,
        sector_weights={"Technology": 0.40},
    )
    fits = compute_candidate_fit(tmp_path, ["IPO"], book, sectors={"IPO": "Technology"})
    ipo = fits["IPO"]
    by = {f.key: f for f in ipo.factors}
    assert by["sharpe"].missing and by["divers"].missing and by["factor"].missing
    assert ipo.partial
    # Sector still scores (it needs no price history) — IPO is in an unheld sector.
    assert not by["sector"].missing
    assert ipo.obs is None


def test_compute_fit_missing_benchmarks_drops_factor_leg(
    tmp_path: Path, market: list[float]
) -> None:
    """No SPY/QQQ cache → the candidate's growth tilt can't be computed, so only
    the factor leg goes missing; the book-relative legs still score."""
    # Seed holdings but NOT SPY/QQQ.
    _write_chart(tmp_path, "AAA", [m + 0.0008 * ((i % 3) - 1) for i, m in enumerate(market)])
    _write_chart(tmp_path, "BBB", [m + 0.0008 * ((i % 5) - 2) for i, m in enumerate(market)])
    _write_chart(tmp_path, "CORR", list(market))
    book = BookContext(weights={"AAA": 0.5, "BBB": 0.5}, sharpe=1.0, risk_free_annual=0.02)
    fit = compute_candidate_fit(tmp_path, ["CORR"], book)["CORR"]
    by = {f.key: f for f in fit.factors}
    assert by["factor"].missing  # no benchmark history → no growth tilt
    assert not by["divers"].missing  # book-relative legs survive
    assert by["sector"].missing  # no sectors passed
    assert fit.partial


def test_compute_fit_no_book_history_keeps_factor_and_sector(
    tmp_path: Path, market: list[float]
) -> None:
    """With fewer than two priced holdings the book return series is empty, so
    the book-relative legs (Sharpe, diversification) degrade — but the
    benchmark-relative factor leg and the sector leg still score."""
    _write_chart(tmp_path, "SPY", list(market))
    _write_chart(tmp_path, "QQQ", [1.5 * m for m in market])
    _write_chart(tmp_path, "CORR", list(market))
    book = BookContext(
        weights={"AAA": 1.0},  # AAA has no chart → no book series
        sharpe=1.0,
        risk_free_annual=0.02,
        growth_tilt=0.4,
        sector_weights={"Technology": 0.4},
    )
    fit = compute_candidate_fit(tmp_path, ["CORR"], book, sectors={"CORR": "Technology"})["CORR"]
    by = {f.key: f for f in fit.factors}
    assert by["sharpe"].missing and by["divers"].missing
    assert not by["factor"].missing  # SPY/QQQ present → growth tilt computable
    assert not by["sector"].missing
    assert fit.partial
