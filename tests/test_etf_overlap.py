"""Stored partial holdings overlap and shared portfolio-fit factor math.

Dedicated ETF candidate scoring and cache materialization are retired.
These tests retain pure overlap coverage and shared fit factors.
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime

import pytest

from allocation.candidate_fit import (
    BookContext,
    CandidateRisk,
    score_candidate_fit,
)
from etf_overlap import (
    EtfOverlap,
    compute_lookthrough_overlap,
    overlap_detail,
    sleeves_served,
)
from instrument_store import upsert_etf_holdings
from models.instruments import EtfHolding
from positioning.target import TargetContext

_DDL = """
CREATE TABLE etf_holdings (
    ticker TEXT NOT NULL,
    as_of_date TEXT NOT NULL,
    constituent_ticker TEXT,
    name TEXT, weight_pct REAL, shares_held REAL, market_value_usd REAL,
    sector TEXT, asset_class TEXT, rank_position INTEGER, country TEXT,
    source TEXT NOT NULL DEFAULT 'fmp',
    fetched_at TIMESTAMP NOT NULL,
    PRIMARY KEY (ticker, as_of_date, constituent_ticker)
);
"""


def _holding(
    etf: str,
    constituent: str | None,
    weight: float,
    *,
    country: str | None = None,
    sector: str | None = None,
) -> EtfHolding:
    return EtfHolding(
        ticker=etf,
        as_of_date=date(2026, 5, 31),
        constituent_ticker=constituent,
        name=constituent or "CASH",
        weight_pct=weight,
        sector=sector,
        country=country,
        source="nport",
        fetched_at=datetime(2026, 7, 10),
    )


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript(_DDL)
    return c


def test_overlap_math_and_rollups(conn: sqlite3.Connection) -> None:
    rows = [
        _holding("AVDV", "NU", 0.04, country="BR", sector="Financial Services"),
        _holding("AVDV", "MELI", 0.03, country="BR", sector="Consumer"),
        _holding("AVDV", "7203", 0.05, country="JP", sector="Industrials"),
        _holding("AVDV", "RIO", 0.02, country="GB", sector="Materials"),
        _holding("AVDV", None, 0.01, country=None),  # cash sleeve
    ]
    upsert_etf_holdings(conn, "AVDV", date(2026, 5, 31), rows)
    book = {"NU": 0.30, "MELI": 0.25, "NOW": 0.45}
    ov = compute_lookthrough_overlap(conn, "avdv", book)
    assert ov is not None
    assert ov.direct_overlap_pct == pytest.approx(0.07)  # NU + MELI
    assert [r.constituent for r in ov.top_overlaps] == ["NU", "MELI"]
    assert ov.top_overlaps[0].book_weight == pytest.approx(0.30)
    assert ov.holdings_count == 5
    assert ov.source == "nport" and ov.as_of == date(2026, 5, 31)
    assert ov.country_weights["JP"] == pytest.approx(0.05)
    assert ov.intl_weight == pytest.approx(0.14)  # every countried row is non-US
    assert ov.em_weight == pytest.approx(0.07)  # BR only
    assert ov.sector_weights["Financial Services"] == pytest.approx(0.04)
    detail = overlap_detail(ov)
    assert detail.startswith("7% of AVDV overlaps the book")
    assert "nport as of 2026-05-31" in detail


def test_country_rollup_falls_back_to_nport_snapshot(conn: sqlite3.Connection) -> None:
    """A fresher issuer snapshot with no per-constituent country (Vanguard's
    API doesn't publish it) must not shadow the spine's geography — the
    country rollup falls back to the newest N-PORT snapshot (VWO E2E)."""
    upsert_etf_holdings(
        conn,
        "VWO",
        date(2026, 4, 30),
        [
            _holding("VWO", "2330", 0.15, country="TW"),
            _holding("VWO", "700", 0.05, country="CN"),
        ],
    )
    fresher_issuer = [
        EtfHolding(
            ticker="VWO",
            as_of_date=date(2026, 5, 31),
            constituent_ticker="2330",
            name="TSMC",
            weight_pct=0.15,
            country=None,  # the issuer API publishes no country
            source="issuer:vanguard",
            fetched_at=datetime(2026, 7, 10),
        )
    ]
    upsert_etf_holdings(conn, "VWO", date(2026, 5, 31), fresher_issuer)
    ov = compute_lookthrough_overlap(conn, "VWO", {"NU": 1.0})
    assert ov is not None
    assert ov.source == "issuer:vanguard" and ov.as_of == date(2026, 5, 31)  # freshest wins
    assert ov.country_weights == {"TW": pytest.approx(0.15), "CN": pytest.approx(0.05)}
    assert ov.em_weight == pytest.approx(0.20)  # geography from the spine


def test_overlap_none_without_holdings(conn: sqlite3.Connection) -> None:
    assert compute_lookthrough_overlap(conn, "GHOST", {"NU": 1.0}) is None
    bare = sqlite3.connect(":memory:")
    try:
        assert compute_lookthrough_overlap(bare, "AVDV", {"NU": 1.0}) is None  # no table
    finally:
        bare.close()


def test_sleeves_served_reads() -> None:
    intl = EtfOverlap(
        ticker="AVDV",
        as_of=date(2026, 5, 31),
        source="nport",
        holdings_count=3,
        direct_overlap_pct=0.0,
        country_weights={"JP": 0.4, "GB": 0.3, "US": 0.1},
    )
    assert sleeves_served(intl) == ("intl",)
    assert sleeves_served(intl, size_beta=0.5) == ("intl", "small_cap")
    em = EtfOverlap(
        ticker="VWO",
        as_of=date(2026, 5, 31),
        source="nport",
        holdings_count=3,
        direct_overlap_pct=0.0,
        country_weights={"CN": 0.3, "IN": 0.2, "BR": 0.2, "TW": 0.2},
    )
    assert sleeves_served(em) == ("intl", "em")
    # No data can't say anything — omitted, never guessed.
    assert sleeves_served(None) == ()
    assert sleeves_served(None, size_beta=0.1) == ()


# ---------------------------------------------------------------------------
# Fit factors
# ---------------------------------------------------------------------------


def test_overlap_factor_bands_and_equity_omission() -> None:
    book = BookContext(weights={})

    def factors(overlap: float | None) -> dict[str, float]:
        risk = CandidateRisk(ticker="X", overlap_pct=overlap, overlap_detail="d")
        return {f.key: f.multiplier for f in score_candidate_fit(risk, book).factors}

    # Equities (no overlap read) keep EXACTLY the four factors — pinned.
    assert set(factors(None)) == {"sharpe", "divers", "factor", "sector"}
    assert factors(0.30)["overlap"] == pytest.approx(0.85)
    assert factors(0.15)["overlap"] == pytest.approx(0.92)
    assert factors(0.05)["overlap"] == pytest.approx(1.0)
    assert factors(0.02)["overlap"] == pytest.approx(1.08)
    # The why string carries the 5th factor for ETFs.
    fit = score_candidate_fit(
        CandidateRisk(ticker="AVDV", overlap_pct=0.02, overlap_detail="2% of AVDV overlaps"),
        book,
    )
    assert "overlap 1.08 (2% of AVDV overlaps (genuinely additive))" in fit.why


def test_sleeve_factor_intent_only() -> None:
    book = BookContext(weights={})
    risk = CandidateRisk(ticker="AVDV", sleeves_served=("intl", "small_cap"))
    intent = TargetContext(
        growth_tilt=None, growth_tilt_band=0.15, sleeves={"intl": 0.15}, source="intent"
    )
    fit = score_candidate_fit(risk, book, intent)
    sleeve = {f.key: f for f in fit.target_factors}["sleeve"]
    assert sleeve.multiplier == pytest.approx(1.10)
    assert "serves targeted sleeve intl" in sleeve.detail
    # Served sleeves but none targeted → scored neutral, not a lift.
    no_hit = TargetContext(
        growth_tilt=None, growth_tilt_band=0.15, sleeves={"em": 0.1}, source="intent"
    )
    sleeve2 = {f.key: f for f in score_candidate_fit(risk, book, no_hit).target_factors}["sleeve"]
    assert sleeve2.multiplier == pytest.approx(1.0)
    # Book-default target (no intent) → the factor is omitted entirely.
    default = TargetContext(growth_tilt=None, growth_tilt_band=0.15, source="book_default")
    assert "sleeve" not in {f.key for f in score_candidate_fit(risk, book, default).target_factors}
    # An equity (no sleeve read) never gets the factor, intent or not.
    eq = CandidateRisk(ticker="NU")
    assert "sleeve" not in {f.key for f in score_candidate_fit(eq, book, intent).target_factors}
