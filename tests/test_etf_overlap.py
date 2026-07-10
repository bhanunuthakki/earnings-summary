"""ETF fit extension: look-through overlap, sleeve factor, what-if precompute.

- etf_overlap.compute_lookthrough_overlap: direct-overlap math, top table,
  sector/country rollups (+ intl/em reads), None on no holdings.
- candidate_fit: the ETF-only 5th 'overlap' factor (equities keep exactly 4 —
  pinned), the intent-only sleeve target factor.
- candidate_fit_cache: ETF candidates get overlap/sleeve reads and lose the
  meaningless profile sector.
- etf_score_cache: the 1/3/5% what-if block rides the Stage 0f payload.
"""

from __future__ import annotations

import json
import math
import sqlite3
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from allocation.candidate_fit import (  # noqa: E402
    BookContext,
    CandidateRisk,
    score_candidate_fit,
)
from etf_overlap import (  # noqa: E402
    EtfOverlap,
    compute_lookthrough_overlap,
    overlap_detail,
    sleeves_served,
)
from instrument_store import upsert_etf_holdings  # noqa: E402
from models.instruments import EtfHolding  # noqa: E402
from positioning.target import TargetContext  # noqa: E402

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


# ---------------------------------------------------------------------------
# Materializer integration (candidate_fit_cache)
# ---------------------------------------------------------------------------


def test_materializer_gives_etfs_overlap_and_drops_sector(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import candidate_fit_cache as cfc

    db_path = tmp_path / "portfolio.db"
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.executescript(
        _DDL
        + """
        CREATE TABLE tracked_companies (
            ticker TEXT, list_type TEXT, instrument_type TEXT, archived_at TEXT
        );
        """
    )
    conn.executemany(
        "INSERT INTO tracked_companies VALUES (?, 'evaluation', ?, NULL)",
        [("AVDV", "etf"), ("DLO", "equity")],
    )
    upsert_etf_holdings(
        conn,
        "AVDV",
        date(2026, 5, 31),
        [_holding("AVDV", "NU", 0.06, country="BR"), _holding("AVDV", "7203", 0.5, country="JP")],
    )
    conn.commit()

    # A profile-sector cache for BOTH names — the ETF's must be dropped.
    fmp = tmp_path / "data" / "historical" / "fmp"
    fmp.mkdir(parents=True)
    for t in ("AVDV", "DLO"):
        (fmp / f"{t}_profile.json").write_text(
            json.dumps([{"sector": "Technology"}]), encoding="utf-8"
        )

    monkeypatch.setattr(
        cfc,
        "assemble_book_context",
        lambda *a, **k: BookContext(weights={"NU": 1.0}),
        raising=True,
    )
    captured: dict[str, object] = {}

    def _fake_compute(repo_root, candidates, book, **kw):  # type: ignore[no-untyped-def]
        captured.update(kw)
        captured["candidates"] = sorted(candidates)
        from allocation.candidate_fit import CandidateFit

        return {
            t: CandidateFit(ticker=t, factors=[], fit=1.0, why="", partial=False)
            for t in candidates
        }

    monkeypatch.setattr(cfc, "compute_candidate_fit", _fake_compute, raising=True)
    conn2 = sqlite3.connect(str(db_path))
    conn2.row_factory = sqlite3.Row
    try:
        cfc.materialize_candidate_fit(conn2, tmp_path, db_path=db_path)
    finally:
        conn2.close()
    conn.close()

    sectors = captured["sectors"]
    assert isinstance(sectors, dict)
    assert "DLO" in sectors and "AVDV" not in sectors  # ETF profile sector dropped
    overlaps = captured["overlaps"]
    assert isinstance(overlaps, dict) and "AVDV" in overlaps
    pct, detail = overlaps["AVDV"]
    assert pct == pytest.approx(0.06)  # NU is held; 7203 isn't
    assert "AVDV overlaps the book" in detail
    sleeves = captured["sleeves"]
    assert isinstance(sleeves, dict)
    assert sleeves.get("AVDV") == ("intl",)  # JP-heavy rollup


# ---------------------------------------------------------------------------
# What-if precompute (etf_score_cache payload)
# ---------------------------------------------------------------------------


def test_etf_whatif_block_materializes(tmp_path: Path) -> None:
    from allocation.what_if import clear_caches
    from etf_score_cache import materialize_etf_scores, read_materialized_etf_whatif

    clear_caches()
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE tracked_companies (
            ticker TEXT, list_type TEXT, instrument_type TEXT, archived_at TEXT
        );
        """
    )
    conn.execute("INSERT INTO tracked_companies VALUES ('AVDV', 'evaluation', 'etf', NULL)")

    start = date(2025, 1, 2)
    fmp = tmp_path / "data" / "historical" / "fmp"
    fmp.mkdir(parents=True)

    def _chart(ticker: str, gen: list[float]) -> None:
        price, rows = 100.0, [{"date": start.isoformat(), "adjClose": 100.0}]
        for i, r in enumerate(gen, start=1):
            price *= math.exp(r)
            rows.append({"date": (start + timedelta(days=i)).isoformat(), "adjClose": price})
        (fmp / f"{ticker}_price_chart_10y_div_adj.json").write_text(
            json.dumps(rows), encoding="utf-8"
        )

    market = [0.01 * math.sin(i / 5.0) + 0.0004 for i in range(200)]
    _chart("AAA", market)
    _chart("AVDV", [0.008 * math.cos(i / 7.0) for i in range(200)])
    (tmp_path / "data" / "portfolio_weights.json").write_text(
        json.dumps({"computed_at": "2026-07-10T04:00:00", "weights": {"AAA": 1.0}}),
        encoding="utf-8",
    )
    (tmp_path / "data" / "candidate_fit.json").write_text(
        json.dumps(
            {
                "version": 2,
                "book": {"risk_free_annual": 0.04, "growth_tilt": 0.2},
                "fits": {},
            }
        ),
        encoding="utf-8",
    )

    n = materialize_etf_scores(conn, tmp_path)
    conn.close()
    assert n == 1
    whatif = read_materialized_etf_whatif(tmp_path)
    assert set(whatif) == {"AVDV"}
    assert set(whatif["AVDV"]) == {"0.01", "0.03", "0.05"}
    three = whatif["AVDV"]["0.03"]
    assert isinstance(three["sharpe_delta_bps"], float)
    assert three["obs"] and three["prices_through"]
