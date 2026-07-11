"""Risk-budget allocator (L7): the risk × reward × conviction join.

Three layers, each tested in isolation:
  * build_gap_rows — the pure join + scoring (the parity gap, the conviction-vs-
    risk and conviction-vs-DCF flags, low-confidence suppression, ranking);
  * _dcf_reward_legs — asymmetry-aware reward + the freshness-derived confidence;
  * build_risk_reward_gap — end-to-end over a seeded price cache + DB.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from allocation.book_risk import BookRisk  # noqa: E402
from risk_reward import (  # noqa: E402
    _dcf_reward_legs,  # pyright: ignore[reportPrivateUsage]  # internal seam under test
    _Reward,  # pyright: ignore[reportPrivateUsage]  # internal seam under test
    build_gap_rows,
    build_risk_reward_gap,
)


def _book(risk_share: dict[str, float], weights: dict[str, float]) -> BookRisk:
    """A BookRisk with chosen risk shares + weights (the rest is filler the join
    doesn't read for scoring)."""
    tickers = list(risk_share)
    return BookRisk(
        tickers=tickers,
        weights=weights,
        marginal_vol_ann={t: 0.3 for t in tickers},
        risk_contribution_ann={t: risk_share[t] * 0.2 for t in tickers},
        risk_share=risk_share,
        corr_to_book=dict.fromkeys(tickers, 0.5),
        portfolio_vol_ann=0.2,
        prices_through=date(2026, 6, 1),
        cov_obs=252,
        shrinkage=0.1,
    )


def _reward(er: float | None, *, low_conf: bool = False, reason: str | None = None) -> _Reward:
    return _Reward(
        expected_return=er,
        has_scenarios=er is not None,
        low_confidence=low_conf,
        confidence_reason=reason,
        detail=None,
    )


# --------------------------------------------------------------------------- #
# build_gap_rows — the pure join + scoring
# --------------------------------------------------------------------------- #


def test_over_risked_name_is_flagged_and_ranked_first() -> None:
    book = _book(
        risk_share={"NU": 0.50, "META": 0.40, "WIX": 0.10},
        weights={"NU": 0.3, "META": 0.5, "WIX": 0.2},
    )
    rewards = {"NU": _reward(0.05), "META": _reward(0.20), "WIX": _reward(0.10)}
    rows, valued = build_gap_rows(book, rewards, convictions={})
    assert valued == 3
    by = {r.ticker: r for r in rows}
    nu = by["NU"]
    # contrib: NU .3*.05=.015, META .5*.20=.10, WIX .2*.10=.02 → gross .135.
    assert nu.reward_share_pct == pytest.approx(0.015 / 0.135 * 100.0, abs=0.1)
    assert nu.risk_share_pct == pytest.approx(50.0)
    assert nu.gap_pct == pytest.approx(50.0 - 0.015 / 0.135 * 100.0, abs=0.1)
    assert nu.mismatch_score > 0
    assert any("of book risk vs" in c and "of expected reward" in c for c in nu.mismatch_reasons)
    # The over-risked name ranks first; the well-rewarded META is "aligned".
    assert rows[0].ticker == "NU"
    assert by["META"].mismatch_score == 0.0


def test_low_conviction_high_risk_flags_even_without_dcf() -> None:
    book = _book(risk_share={"AAA": 0.55, "BBB": 0.45}, weights={"AAA": 0.5, "BBB": 0.5})
    # No DCF rewards at all → reward leg low-confidence; B must still fire on AAA.
    rows, _ = build_gap_rows(book, rewards={}, convictions={"AAA": 2.0})
    aaa = next(r for r in rows if r.ticker == "AAA")
    assert aaa.low_confidence is True
    assert aaa.reward_share_pct is None
    assert aaa.mismatch_score > 0
    assert any("conviction 2/5 but" in c and "book risk" in c for c in aaa.mismatch_reasons)


def test_conviction_outruns_the_dcf() -> None:
    book = _book(risk_share={"FOO": 0.20, "BAR": 0.80}, weights={"FOO": 0.5, "BAR": 0.5})
    # FOO rated 5/5 but the DCF expects only +2% → conviction-vs-DCF flag.
    rows, _ = build_gap_rows(
        book, rewards={"FOO": _reward(0.02), "BAR": _reward(0.30)}, convictions={"FOO": 5.0}
    )
    foo = next(r for r in rows if r.ticker == "FOO")
    assert any("rated 5/5 but the DCF expects only" in c for c in foo.mismatch_reasons)


def test_low_confidence_reward_is_not_confidently_scored() -> None:
    book = _book(risk_share={"STALE": 0.60, "OK": 0.40}, weights={"STALE": 0.5, "OK": 0.5})
    rewards = {
        "STALE": _reward(0.01, low_conf=True, reason="fair value 90d stale"),
        "OK": _reward(0.25),
    }
    rows, _ = build_gap_rows(book, rewards, convictions={})
    stale = next(r for r in rows if r.ticker == "STALE")
    # A big parity gap exists, but the stale reward leg must NOT score it.
    assert stale.gap_pct is not None and stale.gap_pct > 5.0
    assert not any("of expected reward" in c for c in stale.mismatch_reasons)
    assert any("low-confidence" in c and "not scored" in c for c in stale.mismatch_reasons)
    assert stale.mismatch_score == 0.0


def test_no_positive_reward_leaves_shares_undefined() -> None:
    book = _book(risk_share={"A": 0.5, "B": 0.5}, weights={"A": 0.5, "B": 0.5})
    rows, _ = build_gap_rows(book, rewards={"A": _reward(-0.1), "B": _reward(-0.2)}, convictions={})
    # gross upside <= 0 → no reward shares to distribute; nothing confidently scored.
    for r in rows:
        assert r.reward_share_pct is None
        assert r.gap_pct is None
        assert not any("of expected reward" in c for c in r.mismatch_reasons)


# --------------------------------------------------------------------------- #
# _dcf_reward_legs — asymmetry + freshness confidence
# --------------------------------------------------------------------------- #


def _dcf_db(tmp_path: Path) -> Path:
    db = tmp_path / "portfolio.db"
    conn = sqlite3.connect(str(db))
    try:
        conn.executescript(
            """
            CREATE TABLE dcf_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker TEXT NOT NULL, valuation_date TEXT,
                npv_per_share NUMERIC, live_price FLOAT, live_price_at TEXT,
                assumption_snapshot_json TEXT, created_at DATETIME
            );
            """
        )
        conn.commit()
    finally:
        conn.close()
    return db


def _snap(bull: float, bear: float) -> str:
    return json.dumps(
        {
            "scenarios": {
                "bull": {"fair_value_per_share_usd": bull},
                "bear": {"fair_value_per_share_usd": bear},
            }
        }
    )


def test_reward_legs_freshness_and_asymmetry(tmp_path: Path) -> None:
    db = _dcf_db(tmp_path)
    today = date(2026, 6, 14)
    fresh = today.isoformat()
    stale_val = (today - timedelta(days=60)).isoformat()
    stale_price = (today - timedelta(days=20)).isoformat()
    conn = sqlite3.connect(str(db))
    try:
        conn.executemany(
            "INSERT INTO dcf_runs (ticker, valuation_date, npv_per_share, live_price,"
            " live_price_at, assumption_snapshot_json, created_at) VALUES (?,?,?,?,?,?,?)",
            [
                # FRESH: both legs current, downside-skewed range.
                ("FRESH", fresh, 110.0, 100.0, fresh, _snap(130.0, 40.0), f"{fresh} 00:00:00"),
                # OLDVAL: fair value 60d stale → low-confidence.
                ("OLDVAL", stale_val, 110.0, 100.0, fresh, None, f"{fresh} 00:00:00"),
                # OLDPX: price 20d stale → low-confidence.
                ("OLDPX", fresh, 110.0, 100.0, stale_price, None, f"{fresh} 00:00:00"),
            ],
        )
        conn.commit()
    finally:
        conn.close()
    legs = _dcf_reward_legs(db, ["FRESH", "OLDVAL", "OLDPX", "MISSING"], today)

    assert "MISSING" not in legs  # no row → caller treats as no DCF on file
    fresh_leg = legs["FRESH"]
    assert fresh_leg.low_confidence is False
    assert fresh_leg.has_scenarios is True
    # downside skew: 0.25*0.30 + 0.5*0.10 + 0.25*(-0.60) = -0.025.
    assert fresh_leg.expected_return == pytest.approx(-0.025)
    assert legs["OLDVAL"].low_confidence is True
    assert "fair value" in (legs["OLDVAL"].confidence_reason or "")
    assert legs["OLDPX"].low_confidence is True
    assert "price" in (legs["OLDPX"].confidence_reason or "")


# --------------------------------------------------------------------------- #
# build_risk_reward_gap — end to end
# --------------------------------------------------------------------------- #


def _seed_prices(repo_root: Path, n: int = 200) -> None:
    days: list[date] = []
    d = date.today() - timedelta(days=2)
    while len(days) < n:
        if d.weekday() < 5:
            days.append(d)
        d -= timedelta(days=1)
    days.reverse()
    rng = np.random.default_rng(1)
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
        ][::-1]
        (fmp / f"{ticker}_price_chart_10y_div_adj.json").write_text(
            json.dumps(rows), encoding="utf-8"
        )


def test_gap_section_renders_with_control_kit() -> None:
    from risk_reward import RiskRewardGap

    book = _book(
        risk_share={"NU": 0.50, "META": 0.40, "WIX": 0.10},
        weights={"NU": 0.3, "META": 0.5, "WIX": 0.2},
    )
    rows, valued = build_gap_rows(
        book,
        rewards={"NU": _reward(0.05), "META": _reward(0.20), "WIX": _reward(0.10)},
        convictions={"NU": 3.0},
    )
    gap = RiskRewardGap(
        rows=rows,
        portfolio_vol_ann=0.2,
        weights_source="tracker",
        prices_through=date(2026, 6, 1),
        cov_obs=252,
        shrinkage=0.1,
        valued_names=valued,
    )
    from pipeline.portfolio_panel import (
        _risk_reward_gap_section,  # pyright: ignore[reportPrivateUsage]
    )

    html = _risk_reward_gap_section(gap)
    assert "Risk vs reward vs conviction" in html
    assert 'class="p-table rrg-table"' in html  # S1 control kit table
    assert "k-pill" in html and "k-chip" in html  # kit pill + chips, no raw hex
    assert "of book risk vs" in html and "of expected reward" in html  # the parity-gap chip
    assert "NU" in html
    assert "OF REWARD UNMODELED" not in html  # 3/3 valued -> full coverage, no warning


def test_gap_section_low_reward_coverage_leads_with_warning() -> None:
    """Monthly Red Team Phase 1 guard 1: the reward leg is a book-level
    scenario-reward rollup too — a majority-unscored reward share must lead
    with the same UNMODELED warning tail stress does, not a quiet footnote."""
    from risk_reward import RiskRewardGap

    book = _book(
        risk_share={"NU": 0.50, "META": 0.40, "WIX": 0.10},
        weights={"NU": 0.3, "META": 0.5, "WIX": 0.2},
    )
    # Only 1 of 3 names has a usable reward -> 33% valued, well under COVERAGE_BAD_PCT.
    rows, valued = build_gap_rows(book, rewards={"NU": _reward(0.05)}, convictions={"NU": 3.0})
    gap = RiskRewardGap(
        rows=rows,
        portfolio_vol_ann=0.2,
        weights_source="tracker",
        prices_through=date(2026, 6, 1),
        cov_obs=252,
        shrinkage=0.1,
        valued_names=valued,
    )
    from pipeline.portfolio_panel import (
        _risk_reward_gap_section,  # pyright: ignore[reportPrivateUsage]
    )

    html = _risk_reward_gap_section(gap)
    assert "OF REWARD UNMODELED" in html
    assert "k-pill-bad" in html


def test_gap_section_hidden_reason_renders_note() -> None:
    from pipeline.portfolio_panel import (
        _risk_reward_gap_section,  # pyright: ignore[reportPrivateUsage]
    )
    from risk_reward import RiskRewardGap

    gap = RiskRewardGap(
        rows=[],
        portfolio_vol_ann=None,
        weights_source="tracker",
        prices_through=None,
        cov_obs=None,
        shrinkage=None,
        valued_names=0,
        hidden_reason="fewer than two holdings with price history on file",
    )
    html = _risk_reward_gap_section(gap)
    assert "Risk-parity gap unavailable" in html
    assert "fewer than two holdings" in html


def test_build_risk_reward_gap_end_to_end(tmp_path: Path) -> None:
    repo_root = tmp_path
    db = repo_root / "data" / "portfolio.db"
    _seed_prices(repo_root)
    today = date.today()
    conn = sqlite3.connect(str(db))
    try:
        conn.executescript(
            """
            CREATE TABLE dcf_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker TEXT NOT NULL, valuation_date TEXT,
                npv_per_share NUMERIC, live_price FLOAT, live_price_at TEXT,
                assumption_snapshot_json TEXT, created_at DATETIME
            );
            CREATE TABLE position_sizing_intent (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL DEFAULT 'bhanu',
                ticker TEXT NOT NULL, intent_kind TEXT NOT NULL,
                intent_value REAL, narrative TEXT,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            """
        )
        ts = today.isoformat()
        conn.executemany(
            "INSERT INTO dcf_runs (ticker, valuation_date, npv_per_share, live_price,"
            " live_price_at, assumption_snapshot_json, created_at) VALUES (?,?,?,?,?,?,?)",
            [
                ("AAA", ts, 90.0, 100.0, ts, None, f"{ts} 00:00:00"),  # below FV → negative reward
                ("BBB", ts, 150.0, 100.0, ts, None, f"{ts} 00:00:00"),
                ("CCC", ts, 130.0, 100.0, ts, None, f"{ts} 00:00:00"),
            ],
        )
        conn.execute(
            "INSERT INTO position_sizing_intent (user_id, ticker, intent_kind, intent_value,"
            " created_at, updated_at) VALUES ('bhanu','AAA','conviction',2,?,?)",
            (f"{ts} 00:00:00", f"{ts} 00:00:00"),
        )
        conn.commit()
    finally:
        conn.close()

    weights = {"AAA": 0.5, "BBB": 0.3, "CCC": 0.2}
    gap = build_risk_reward_gap(db, repo_root, weights, weights_source="tracker", today=today)
    assert gap.hidden_reason is None
    assert {r.ticker for r in gap.rows} == {"AAA", "BBB", "CCC"}
    assert gap.valued_names == 3
    assert gap.weights_source == "tracker"
    aaa = next(r for r in gap.rows if r.ticker == "AAA")
    assert aaa.conviction == 2.0
    assert aaa.expected_return_pct == pytest.approx(-10.0)  # 90/100 - 1
    # AAA: negative expected reward + low conviction at the book's biggest weight
    # → it carries the mismatch and ranks first.
    assert aaa.mismatch_score > 0
    assert gap.rows[0].ticker == "AAA"
