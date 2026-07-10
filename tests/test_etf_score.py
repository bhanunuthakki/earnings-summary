"""ETF next-dollar score (pipeline/etf_score + etf_score_cache + cockpit wiring).

Band edges + why format pinned with the pure scorer (retuning must be
deliberate); gatherer against synthetic price/proxy files; cache round-trip +
last-good; cockpit: an evaluation ETF row scores from the cache (never the
equity factors) and wears the ETF pill; the score peek branches to the cached
ETF breakdown.
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

from etf_score_cache import (  # noqa: E402
    materialize_etf_scores,
    read_materialized_etf_loadings,
    read_materialized_etf_scores,
)
from instrument_store import upsert_etf_profile  # noqa: E402
from models.instruments import EtfProfile  # noqa: E402
from pipeline.etf_score import (  # noqa: E402
    EtfScoreInputs,
    StyleLoadingRead,
    gather_etf_score_inputs,
    score_etf,
)

# ---------------------------------------------------------------------------
# Pure scorer — band edges + why format
# ---------------------------------------------------------------------------


def _mult(bd, key: str) -> float:  # type: ignore[no-untyped-def]
    return {f.key: f.multiplier for f in bd.factors}[key]


def test_ret_band_edges() -> None:
    def m(delta: float) -> float:
        return _mult(score_etf(EtfScoreInputs(ticker="X", delta_sharpe_vs_bench=delta)), "ret")

    assert m(0.30) == pytest.approx(1.40)
    assert m(0.10) == pytest.approx(1.20)
    assert m(-0.10) == pytest.approx(1.00)
    assert m(-0.30) == pytest.approx(0.80)
    assert m(-0.31) == pytest.approx(0.65)


def test_er_band_edges() -> None:
    def m(er: float) -> float:
        return _mult(score_etf(EtfScoreInputs(ticker="X", expense_ratio=er)), "er")

    assert m(0.0010) == pytest.approx(1.15)  # 10 bps
    assert m(0.0025) == pytest.approx(1.05)
    assert m(0.0050) == pytest.approx(1.00)
    assert m(0.0075) == pytest.approx(0.90)
    assert m(0.0090) == pytest.approx(0.80)


def test_prem_band_edges() -> None:
    def m(d: float) -> float:
        return _mult(score_etf(EtfScoreInputs(ticker="X", distinctiveness=d)), "prem")

    assert m(1.00) == pytest.approx(1.20)
    assert m(0.50) == pytest.approx(1.10)
    assert m(0.20) == pytest.approx(1.00)
    assert m(0.10) == pytest.approx(0.95)  # closet benchmark


def test_val_lens_selection_and_bands() -> None:
    # Value-tilted funds are judged on P/B…
    pb = score_etf(EtfScoreInputs(ticker="X", value_tilted=True, pb_ratio=1.2, pe_ratio=50.0))
    assert _mult(pb, "val") == pytest.approx(1.15)
    assert "P/B 1.2" in pb.why
    # …everything else on P/E; P/B is the fallback when P/E is absent.
    pe = score_etf(EtfScoreInputs(ticker="X", pe_ratio=27.0))
    assert _mult(pe, "val") == pytest.approx(1.00)
    assert "P/E 27.0" in pe.why
    fallback = score_etf(EtfScoreInputs(ticker="X", pb_ratio=4.6))
    assert _mult(fallback, "val") == pytest.approx(0.80)


def test_missing_inputs_sink_at_085_like_equities() -> None:
    bd = score_etf(EtfScoreInputs(ticker="X"))
    assert bd.partial
    assert all(f.missing and f.multiplier == pytest.approx(0.85) for f in bd.factors)
    assert bd.score == pytest.approx(0.85**4)


def test_why_format_matches_equity_idiom() -> None:
    bd = score_etf(
        EtfScoreInputs(
            ticker="AVUV",
            delta_sharpe_vs_bench=0.14,
            ret_obs=504,
            expense_ratio=0.0025,
            distinctiveness=1.17,
            loadings=(
                StyleLoadingRead(key="value", beta=0.62, r_squared=0.4, n_obs=252),
                StyleLoadingRead(key="size", beta=0.55, r_squared=0.5, n_obs=252),
            ),
            value_tilted=True,
            pb_ratio=1.1,
        )
    )
    assert not bd.partial
    assert bd.why.startswith("ret 1.20 (dSR +0.14 vs SPY, n=504) x er 1.05 (25 bps/yr) x ")
    assert "prem 1.20 (D=1.17 (value +0.62 size +0.55))" in bd.why
    assert bd.why.endswith(f"= {bd.score:.2f}")
    assert bd.score == pytest.approx(1.20 * 1.05 * 1.20 * 1.15)


# ---------------------------------------------------------------------------
# Gatherer — synthetic substrates
# ---------------------------------------------------------------------------

_ETF_DDL = """
CREATE TABLE tracked_companies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT DEFAULT 'bhanu',
    ticker TEXT NOT NULL,
    name TEXT,
    list_type TEXT NOT NULL,
    instrument_type VARCHAR,
    added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_built_at TIMESTAMP,
    archived_at TIMESTAMP,
    sec_validated INTEGER DEFAULT 0,
    ir_url TEXT,
    filing_regime TEXT,
    fiscal_year_end TEXT,
    fmp_data_saved INTEGER DEFAULT 0,
    fmp_data_upto TEXT
);
CREATE TABLE fmp_endpoint_status (
    ticker TEXT, endpoint TEXT, period TEXT, status TEXT, last_pulled TIMESTAMP
);
CREATE TABLE transcripts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id INTEGER, ticker TEXT, period_end TEXT, has_qa_section INTEGER,
    call_date TEXT
);
CREATE TABLE thesis_evaluations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT, evaluated_at TEXT, overall_status TEXT, rule_evaluations_json TEXT
);
CREATE TABLE etf_profile (
    ticker TEXT PRIMARY KEY,
    name TEXT, issuer TEXT, expense_ratio REAL, aum_usd_m REAL,
    inception_date TEXT, asset_class TEXT, benchmark_index TEXT,
    domicile TEXT, listed_exchange TEXT, distribution_yield REAL,
    description TEXT, sector_label TEXT, nav REAL, price REAL,
    premium_discount_pct REAL,
    pe_ratio REAL, pb_ratio REAL, weighted_avg_mktcap_usd_m REAL,
    characteristics_as_of TEXT, characteristics_source TEXT,
    source TEXT NOT NULL DEFAULT 'fmp',
    profile_fetched_at TIMESTAMP NOT NULL
);
"""

_START = date(2024, 1, 2)


def _write_chart(repo: Path, ticker: str, returns: list[float]) -> None:
    fmp = repo / "data" / "historical" / "fmp"
    fmp.mkdir(parents=True, exist_ok=True)
    price = 100.0
    rows = [{"date": _START.isoformat(), "adjClose": price}]
    for i, r in enumerate(returns, start=1):
        price *= math.exp(r)
        rows.append({"date": (_START + timedelta(days=i)).isoformat(), "adjClose": price})
    (fmp / f"{ticker}_price_chart_10y_div_adj.json").write_text(json.dumps(rows), encoding="utf-8")


def _write_proxy(repo: Path, ticker: str, returns: list[float]) -> None:
    d = repo / "data" / "factor_proxies"
    d.mkdir(parents=True, exist_ok=True)
    price = 50.0
    rows = [[_START.isoformat(), price]]
    for i, r in enumerate(returns, start=1):
        price *= math.exp(r)
        rows.append([(_START + timedelta(days=i)).isoformat(), price])
    (d / f"{ticker}.json").write_text(
        json.dumps({"ticker": ticker, "fetched_at": "2026-07-10T00:00:00", "rows": rows}),
        encoding="utf-8",
    )


@pytest.fixture
def etf_env(tmp_path: Path) -> tuple[sqlite3.Connection, Path]:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(_ETF_DDL)
    conn.execute(
        "INSERT INTO tracked_companies (ticker, name, list_type, instrument_type) "
        "VALUES ('AVUV', 'Avantis US Small Cap Value ETF', 'evaluation', 'etf')"
    )
    upsert_etf_profile(
        conn,
        EtfProfile(
            ticker="AVUV",
            name="Avantis US Small Cap Value ETF",
            expense_ratio=0.0025,
            pb_ratio=1.3,
            source="issuer:test",
            profile_fetched_at=datetime(2026, 7, 10),
        ),
    )
    market = [0.010 * math.sin(i / 6.0) + 0.0004 for i in range(300)]
    _write_chart(tmp_path, "SPY", market)
    # The ETF outruns SPY (positive drift edge) and loads hard on the value
    # spread (beta ≈ 1.5 → the r² floor clears comfortably).
    value_spread = [0.006 * math.cos(i / 9.0) for i in range(300)]
    _write_chart(
        tmp_path, "AVUV", [m + 1.5 * v + 0.0006 for m, v in zip(market, value_spread, strict=True)]
    )
    _write_proxy(tmp_path, "VTV", [0.004 + 0.5 * v for v in value_spread])
    _write_proxy(tmp_path, "VUG", [0.004 - 0.5 * v for v in value_spread])
    _write_proxy(tmp_path, "SPY", market)
    _write_proxy(tmp_path, "IWM", market)  # size spread ≈ 0 → low r², dropped
    _write_proxy(tmp_path, "MTUM", market)  # momentum spread ≈ 0 → dropped
    yield conn, tmp_path
    conn.close()


def test_gather_inputs_from_substrates(etf_env: tuple[sqlite3.Connection, Path]) -> None:
    conn, repo = etf_env
    inputs = gather_etf_score_inputs(conn, repo, "AVUV")
    assert inputs.expense_ratio == pytest.approx(0.0025)
    assert inputs.pb_ratio == pytest.approx(1.3)
    assert inputs.value_tilted  # 'Value' in the fund name
    assert inputs.delta_sharpe_vs_bench is not None and inputs.delta_sharpe_vs_bench > 0
    assert inputs.ret_obs is not None and inputs.ret_obs >= 120
    # The value leg loads (the ETF was built on the VTV-VUG spread); the flat
    # size/momentum spreads can't clear the r² floor.
    keys = {ld.key for ld in inputs.loadings}
    assert "value" in keys
    assert inputs.distinctiveness is not None and inputs.distinctiveness > 0.2
    bd = score_etf(inputs)
    assert not bd.partial


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------


def test_materialize_and_read_round_trip(etf_env: tuple[sqlite3.Connection, Path]) -> None:
    conn, repo = etf_env
    n = materialize_etf_scores(conn, repo)
    assert n == 1
    scores = read_materialized_etf_scores(repo)
    assert set(scores) == {"AVUV"}
    bd = scores["AVUV"]
    assert [f.key for f in bd.factors] == ["ret", "er", "prem", "val"]
    assert bd.why.endswith(f"= {bd.score:.2f}")
    loadings = read_materialized_etf_loadings(repo)
    assert loadings["AVUV"] and loadings["AVUV"][0].key == "value"


def test_read_missing_or_malformed_cache_degrades(tmp_path: Path) -> None:
    assert read_materialized_etf_scores(tmp_path) == {}
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "etf_score.json").write_text("{oops", encoding="utf-8")
    assert read_materialized_etf_scores(tmp_path) == {}
    assert read_materialized_etf_loadings(tmp_path) == {}


# ---------------------------------------------------------------------------
# Cockpit + peek wiring
# ---------------------------------------------------------------------------


def test_cockpit_etf_row_uses_cached_score_and_pill(
    etf_env: tuple[sqlite3.Connection, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    from pipeline.research_cockpit import build_cockpit_rows, render_research_cockpit

    conn, repo = etf_env
    materialize_etf_scores(conn, repo)

    # The equity scorer must never be consulted for an ETF row.
    import pipeline.research_cockpit as rc

    def _boom(**_kw: object) -> tuple[float, str, bool]:
        raise AssertionError("eval_attractiveness called for an ETF row")

    monkeypatch.setattr(rc, "eval_attractiveness", _boom)
    rows = build_cockpit_rows(conn, repo)
    (avuv,) = rows["evaluation"]
    assert avuv.is_etf
    assert avuv.attractiveness is not None
    assert avuv.attractiveness_why is not None and avuv.attractiveness_why.startswith("ret ")
    html = render_research_cockpit(rows)
    assert "<span class='k-pill'>ETF</span>" in html
    assert "data-peek-url='/api/peek/score?ticker=AVUV'" in html


def test_score_peek_branches_to_cached_etf_breakdown(
    etf_env: tuple[sqlite3.Connection, Path],
) -> None:
    from pipeline.peeks import render_score_peek

    conn, repo = etf_env
    # Uncached ETF → 404-shaped None (never a live Sharpe/OLS on the render path).
    assert render_score_peek(conn, repo, "AVUV") is None
    materialize_etf_scores(conn, repo)
    html = render_score_peek(conn, repo, "AVUV")
    assert html is not None
    assert "ETF factors" in html
    for label in ("Risk-adj return", "Expense drag", "Factor premium", "Basket valuation"):
        assert label in html
