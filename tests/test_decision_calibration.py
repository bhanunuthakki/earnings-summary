"""S15 PR2(a) — decision-calibration analytics + the panel section.

Deterministic SQL only (no LLM, no network): hit rate by conviction,
action mix + reversal vindication, time-to-outcome. The renderer rides
``compose_decisions_page`` with an added optional param — the existing
no-calibration call shape stays valid (pinned here).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from attribution import decompose_alpha
from decision_calibration import build_calibration, realized_magnitudes
from integrations.portfolio_tracker_client import (
    ExitQuality,
    ExitQualityRow,
    LivePortfolio,
    PositionAlpha,
    PositionAlphaRow,
)
from pipeline.allocation_decisions_panel import compose_decisions_page

_SCHEMA = """
CREATE TABLE decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker VARCHAR(16) NOT NULL,
    recommendation_kind VARCHAR(32) NOT NULL,
    recommendation_value FLOAT,
    conviction VARCHAR(16),
    source_artifact_id INTEGER,
    made_at DATETIME NOT NULL,
    user_acted_at DATETIME,
    user_action_kind VARCHAR(32),
    outcome_at DATETIME,
    outcome_label VARCHAR(16),
    outcome_pct FLOAT,
    process_quality VARCHAR(16),
    created_at DATETIME NOT NULL
);
"""


@pytest.fixture
def db(tmp_path: Path) -> Path:
    path = tmp_path / "cal.db"
    conn = sqlite3.connect(str(path))
    conn.executescript(_SCHEMA)
    conn.commit()
    conn.close()
    return path


def _insert(
    db: Path,
    *,
    ticker: str = "NU",
    kind: str = "trim",
    conviction: str | None = "high",
    action: str | None = None,
    outcome_label: str | None = None,
    made_at: str = "2026-01-10T09:00:00",
    outcome_at: str | None = None,
) -> None:
    conn = sqlite3.connect(str(db))
    conn.execute(
        "INSERT INTO decisions (ticker, recommendation_kind, conviction, user_action_kind, "
        "made_at, outcome_at, outcome_label, created_at) VALUES (?,?,?,?,?,?,?,?)",
        (ticker, kind, conviction, action, made_at, outcome_at, outcome_label, made_at),
    )
    conn.commit()
    conn.close()


def test_substrate_absent_returns_none(tmp_path: Path) -> None:
    assert build_calibration(db_path=tmp_path / "missing.db") is None
    empty = tmp_path / "no_table.db"
    sqlite3.connect(str(empty)).close()
    assert build_calibration(db_path=empty) is None


def test_empty_ledger_returns_zeroed_stats(db: Path) -> None:
    stats = build_calibration(db_path=db)
    assert stats is not None
    assert stats.total == 0 and stats.graded == 0
    assert stats.overall_hit_rate is None
    assert stats.by_conviction == []


def test_hit_rate_by_conviction_and_denominator(db: Path) -> None:
    # high: 2 correct, 1 wrong, 1 pending → hit 2/3 (pending never scored)
    _insert(db, conviction="high", outcome_label="correct", outcome_at="2026-02-01T00:00:00")
    _insert(db, conviction="high", outcome_label="correct", outcome_at="2026-02-01T00:00:00")
    _insert(db, conviction="high", outcome_label="wrong", outcome_at="2026-02-01T00:00:00")
    _insert(db, conviction="high", outcome_label="pending")
    # unstated conviction: 1 mixed, 1 unfalsifiable → hit 0/1
    _insert(db, conviction=None, outcome_label="mixed", outcome_at="2026-02-01T00:00:00")
    _insert(db, conviction=None, outcome_label="unfalsifiable", outcome_at="2026-02-01T00:00:00")

    stats = build_calibration(db_path=db)
    assert stats is not None
    assert stats.total == 6
    assert stats.graded == 4  # 2 correct + 1 wrong + 1 mixed
    assert stats.overall_hit_rate == pytest.approx(0.5)

    by = {b.conviction: b for b in stats.by_conviction}
    assert set(by) == {"high", "unstated"}
    high = by["high"]
    assert (high.graded, high.correct, high.wrong, high.ungraded) == (3, 2, 1, 1)
    assert high.hit_rate == pytest.approx(2 / 3)
    unstated = by["unstated"]
    assert unstated.graded == 1 and unstated.mixed == 1
    assert unstated.ungraded == 1  # unfalsifiable counts ungraded
    assert unstated.hit_rate == pytest.approx(0.0)
    # Ordered high → unstated per CONVICTION_ORDER
    assert [b.conviction for b in stats.by_conviction] == ["high", "unstated"]


def test_reversals_and_action_mix(db: Path) -> None:
    _insert(db, action="followed", outcome_label="correct", outcome_at="2026-02-01T00:00:00")
    _insert(db, action="reversed", outcome_label="wrong", outcome_at="2026-02-01T00:00:00",
            made_at="2026-01-20T09:00:00")  # fmt: skip
    _insert(db, action="reversed", outcome_label="correct", outcome_at="2026-02-01T00:00:00",
            made_at="2026-01-15T09:00:00")  # fmt: skip
    _insert(db, action="reversed", outcome_label="pending", made_at="2026-01-25T09:00:00")
    _insert(db, action=None, outcome_label="pending")

    stats = build_calibration(db_path=db)
    assert stats is not None
    assert stats.action_mix["followed"] == 1
    assert stats.action_mix["reversed"] == 3
    assert stats.action_mix["unacted"] == 1
    assert len(stats.reversals) == 3
    # newest-first by made_at
    assert [r.made_at for r in stats.reversals] == ["2026-01-25", "2026-01-20", "2026-01-15"]
    assert stats.reversals[0].vindicated is None  # pending → unresolved
    assert stats.reversals[0].outcome_label is None
    assert stats.reversals[1].vindicated is True  # call graded wrong → override right
    assert stats.reversals[2].vindicated is False  # call graded correct → override cost
    assert stats.reversals_vindicated == 1
    assert stats.reversals_cost == 1


def test_time_to_outcome(db: Path) -> None:
    _insert(db, kind="trim", outcome_label="correct",
            made_at="2026-01-01T00:00:00", outcome_at="2026-01-11T00:00:00")  # fmt: skip
    _insert(db, kind="trim", outcome_label="wrong",
            made_at="2026-01-01T00:00:00", outcome_at="2026-01-31T00:00:00")  # fmt: skip
    _insert(db, kind="add", outcome_label="mixed",
            made_at="2026-01-01T00:00:00", outcome_at="2026-01-06T00:00:00")  # fmt: skip
    _insert(db, kind="add", outcome_label="pending")  # ungraded — excluded

    stats = build_calibration(db_path=db)
    assert stats is not None
    timing = {t.kind: t for t in stats.time_to_outcome}
    assert timing["trim"].n == 2
    assert timing["trim"].avg_days == pytest.approx(20.0)
    assert timing["trim"].median_days == pytest.approx(20.0)
    assert timing["add"].n == 1
    assert timing["add"].avg_days == pytest.approx(5.0)
    # descending n
    assert [t.kind for t in stats.time_to_outcome] == ["trim", "add"]


# ---------------------------------------------------------------------------
# Period-over-period cohorts (L8)
# ---------------------------------------------------------------------------


def test_cohorts_quarter_trend_and_delta(db: Path) -> None:
    # Q1 2026: 1 correct, 1 wrong → 50%. Q2 2026: 2 correct → 100%.
    _insert(db, outcome_label="correct", made_at="2026-02-01T00:00:00",
            outcome_at="2026-03-01T00:00:00")  # fmt: skip
    _insert(db, outcome_label="wrong", made_at="2026-02-15T00:00:00",
            outcome_at="2026-03-01T00:00:00")  # fmt: skip
    _insert(db, outcome_label="correct", made_at="2026-05-01T00:00:00",
            outcome_at="2026-06-01T00:00:00")  # fmt: skip
    _insert(db, outcome_label="correct", made_at="2026-05-20T00:00:00",
            outcome_at="2026-06-01T00:00:00")  # fmt: skip
    # A pending Q2 call counts in `total` but never in graded/hit_rate.
    _insert(db, outcome_label="pending", made_at="2026-06-10T00:00:00")

    stats = build_calibration(db_path=db)
    assert stats is not None
    assert stats.cohort_granularity == "quarter"
    assert [c.period for c in stats.cohorts] == ["2026-Q1", "2026-Q2"]
    q1, q2 = stats.cohorts
    assert (q1.graded, q1.correct, q1.total) == (2, 1, 2)
    assert q1.hit_rate == pytest.approx(0.5)
    assert (q2.graded, q2.correct, q2.total) == (2, 2, 3)  # 2 graded + 1 pending
    assert q2.hit_rate == pytest.approx(1.0)
    assert stats.hit_rate_delta == pytest.approx(0.5)
    assert stats.improving is True


def test_cohort_conviction_gap(db: Path) -> None:
    # One quarter: a high-conviction correct + a low-conviction wrong.
    _insert(db, conviction="high", outcome_label="correct", made_at="2026-02-01T00:00:00")
    _insert(db, conviction="low", outcome_label="wrong", made_at="2026-02-10T00:00:00")
    stats = build_calibration(db_path=db)
    assert stats is not None
    (cohort,) = stats.cohorts
    assert cohort.high_hit_rate == pytest.approx(1.0)
    assert cohort.rest_hit_rate == pytest.approx(0.0)  # low conviction is "rest"
    assert cohort.conviction_gap == pytest.approx(1.0)  # high earned its label


def test_cohort_gap_none_when_one_side_empty(db: Path) -> None:
    # Only high-conviction graded calls → no "rest" to compare → gap None.
    _insert(db, conviction="high", outcome_label="correct", made_at="2026-02-01T00:00:00")
    stats = build_calibration(db_path=db)
    assert stats is not None
    (cohort,) = stats.cohorts
    assert cohort.high_hit_rate == pytest.approx(1.0)
    assert cohort.rest_hit_rate is None
    assert cohort.conviction_gap is None


def test_cohort_single_graded_period_no_trend(db: Path) -> None:
    _insert(db, outcome_label="correct", made_at="2026-02-01T00:00:00")
    stats = build_calibration(db_path=db)
    assert stats is not None
    assert len(stats.cohorts) == 1
    assert stats.hit_rate_delta is None  # need two graded periods for a direction
    assert stats.improving is None


def test_cohort_year_granularity_and_bad_value_fallback(db: Path) -> None:
    _insert(db, outcome_label="correct", made_at="2025-06-01T00:00:00")
    _insert(db, outcome_label="wrong", made_at="2026-06-01T00:00:00")
    yearly = build_calibration(db_path=db, cohort_granularity="year")
    assert yearly is not None
    assert [c.period for c in yearly.cohorts] == ["2025", "2026"]
    # An unrecognised granularity falls back to quarter rather than raising.
    bad = build_calibration(db_path=db, cohort_granularity="weekly")
    assert bad is not None
    assert bad.cohort_granularity == "quarter"


def test_cohort_unparseable_made_at_excluded_from_curve(db: Path) -> None:
    _insert(db, outcome_label="correct", made_at="not-a-date")
    _insert(db, outcome_label="correct", made_at="2026-02-01T00:00:00")
    stats = build_calibration(db_path=db)
    assert stats is not None
    # The garbage stamp drops out of the curve but stays in the flat totals.
    assert [c.period for c in stats.cohorts] == ["2026-Q1"]
    assert stats.total == 2 and stats.graded == 2


# ---------------------------------------------------------------------------
# Renderer
# ---------------------------------------------------------------------------


def _offline() -> LivePortfolio:
    return LivePortfolio(available=False, api_url="http://x", error="down")


def test_panel_renders_calibration_section(db: Path) -> None:
    _insert(db, conviction="high", action="reversed", outcome_label="wrong",
            outcome_at="2026-02-01T00:00:00")  # fmt: skip
    stats = build_calibration(db_path=db)
    assert stats is not None
    html = compose_decisions_page([], [], _offline(), None, calibration=stats)
    assert "<h2>Decision calibration</h2>" in html
    assert "hit rate" in html
    assert "reversal vindicated" in html
    assert "Reversals" in html
    assert 'class="adc-kpis"' in html


def test_panel_renders_trend_curve(db: Path) -> None:
    # Two graded quarters → the "am I getting better?" sparkline + table appear.
    _insert(db, outcome_label="wrong", made_at="2026-02-01T00:00:00")
    _insert(db, outcome_label="correct", made_at="2026-05-01T00:00:00")
    stats = build_calibration(db_path=db)
    assert stats is not None
    html = compose_decisions_page([], [], _offline(), None, calibration=stats)
    assert "Am I getting better?" in html
    assert '<svg class="adc-spark"' in html  # the rendered sparkline (not just the CSS rule)
    assert "improving" in html  # 0% → 100% across the two quarters
    assert "<polyline" in html


def test_panel_no_trend_with_single_period(db: Path) -> None:
    _insert(db, outcome_label="correct", made_at="2026-02-01T00:00:00")
    stats = build_calibration(db_path=db)
    assert stats is not None
    html = compose_decisions_page([], [], _offline(), None, calibration=stats)
    assert "Am I getting better?" not in html  # need ≥2 graded periods
    assert '<svg class="adc-spark"' not in html


def test_panel_empty_state_and_backward_call_shape(db: Path) -> None:
    stats = build_calibration(db_path=db)
    assert stats is not None
    html = compose_decisions_page([], [], _offline(), None, calibration=stats)
    assert "No decisions recorded yet" in html
    # Pre-S15 call shape (no calibration arg) still renders, without the section.
    legacy = compose_decisions_page([], [], _offline(), None)
    assert "<h2>Decision calibration</h2>" not in legacy
    # And without the new attribution arg → no skill section (byte-shape preserved).
    assert "<h2>Skill decomposition</h2>" not in legacy


def _two_name_alpha() -> PositionAlpha:
    def row(t: str, value: float, a: float) -> PositionAlphaRow:
        return PositionAlphaRow(
            ticker=t, name=None, value_at_start=value, bought_in_window=0.0,
            sold_in_window=0.0, value_at_end=value, actual_pl=None,
            spy_counterfactual_pl=None, alpha=a, alpha_vs_qqq=None,
            alpha_vs_policy=None, incomplete=False,
        )  # fmt: skip

    return PositionAlpha(
        start_date="2026-01-01", end_date="2026-03-31", has_policy=False,
        total_actual_pl=None, total_spy_pl=None, total_alpha=None,
        total_alpha_vs_qqq=None, total_alpha_vs_policy=None,
        rows=[row("A", 100, 20), row("B", 300, 15)],
    )  # fmt: skip


def test_panel_renders_skill_decomposition(db: Path) -> None:
    stats = build_calibration(db_path=db)
    attribution = decompose_alpha(_two_name_alpha(), conviction_by_ticker={"A": 5.0})
    assert attribution is not None
    html = compose_decisions_page(
        [], [], _offline(), None, calibration=stats, attribution=attribution
    )
    assert "<h2>Skill decomposition</h2>" in html
    assert "selection" in html and "sizing" in html and "timing" in html
    assert "Conviction &rarr; outcome" in html
    # Thin book (n=2) → the read is hedged as directional, not a verdict.
    assert "Directional only (thin book)" in html


def test_panel_renders_jensen_alpha_beside_decomposition(db: Path) -> None:
    from integrations.portfolio_tracker_client import BetaStats

    stats = build_calibration(db_path=db)
    attribution = decompose_alpha(_two_name_alpha(), conviction_by_ticker={"A": 5.0})
    beta = BetaStats(
        benchmark="SPY", start_date=None, end_date=None, sample_size=250, risk_free_annual=0.04,
        beta=1.1, alpha_annualized_pct=2.5, alpha_t_stat=2.3,
        alpha_std_error_annualized_pct=1.1, alpha_significant=True, r_squared=0.8,
        correlation=0.9, sharpe=None, sortino=None, information_ratio=None,
        portfolio_volatility_annualized=None, benchmark_volatility_annualized=None,
        tracking_error_annualized=None,
    )  # fmt: skip
    html = compose_decisions_page(
        [], [], _offline(), None, calibration=stats, attribution=attribution, beta=beta
    )
    assert "Jensen &alpha;" in html
    assert "+2.5%" in html
    assert "distinguishable from zero" in html  # skill-vs-luck verdict from the trio


def test_panel_jensen_absent_without_beta(db: Path) -> None:
    stats = build_calibration(db_path=db)
    attribution = decompose_alpha(_two_name_alpha())
    html = compose_decisions_page(
        [], [], _offline(), None, calibration=stats, attribution=attribution
    )
    assert "Jensen &alpha;" not in html


# ----- L-seam 3: Wilson CI on the conviction buckets -----


def test_wilson_ci_widens_a_thin_perfect_record(db: Path) -> None:
    # 3/3 high-conviction correct → the band must be honestly wide, NOT a false
    # "100% ± 0" that a point estimate on n=3 would imply.
    for _ in range(3):
        _insert(db, conviction="high", outcome_label="correct", outcome_at="2026-02-01T00:00:00")
    stats = build_calibration(db_path=db)
    assert stats is not None
    hi = next(b for b in stats.by_conviction if b.conviction == "high")
    assert hi.hit_rate == 1.0
    assert hi.wilson_low is not None and hi.wilson_high is not None
    assert hi.wilson_low < 0.5  # Wilson lower bound on 3/3 is ~0.44
    assert hi.wilson_high == pytest.approx(1.0)


def test_wilson_ci_none_when_nothing_graded(db: Path) -> None:
    _insert(db, conviction="high", outcome_label="pending")
    stats = build_calibration(db_path=db)
    assert stats is not None
    hi = next(b for b in stats.by_conviction if b.conviction == "high")
    assert hi.graded == 0
    assert hi.wilson_low is None and hi.wilson_high is None


# ----- L-seam 2: proper-scoring Brier on the owner's own conviction -----


def test_conviction_brier_and_reliability_curve(db: Path) -> None:
    # high: 4 correct, 1 wrong → observed 0.8 vs implied 0.75 (well calibrated);
    # low: 1 correct, 3 wrong → observed 0.25 vs implied 0.40 (overconfident).
    for _ in range(4):
        _insert(db, conviction="high", outcome_label="correct", outcome_at="2026-02-01T00:00:00")
    _insert(db, conviction="high", outcome_label="wrong", outcome_at="2026-02-01T00:00:00")
    _insert(db, conviction="low", outcome_label="correct", outcome_at="2026-02-01T00:00:00")
    for _ in range(3):
        _insert(db, conviction="low", outcome_label="wrong", outcome_at="2026-02-01T00:00:00")
    stats = build_calibration(db_path=db)
    assert stats is not None
    cc = stats.conviction_calibration
    assert cc is not None
    assert cc.n == 9  # only correct/wrong with a stated conviction
    assert cc.brier is not None and 0.0 <= cc.brier <= 1.0
    assert cc.baseline_brier is not None
    rows = {r.conviction: r for r in cc.rows}
    assert rows["high"].observed == pytest.approx(0.8)
    assert rows["high"].predicted == pytest.approx(0.75)
    assert rows["low"].observed == pytest.approx(0.25)
    assert rows["low"].gap == pytest.approx(0.40 - 0.25)  # overconfident on lows


def test_conviction_brier_excludes_mixed_and_unstated(db: Path) -> None:
    # mixed isn't binary-scored; unstated has no implied probability.
    _insert(db, conviction="high", outcome_label="mixed", outcome_at="2026-02-01T00:00:00")
    _insert(db, conviction=None, outcome_label="correct", outcome_at="2026-02-01T00:00:00")
    stats = build_calibration(db_path=db)
    assert stats is not None
    assert stats.conviction_calibration is None


# ----- L-seam 1: batting-vs-slugging expectancy from tracker magnitudes -----


def test_expectancy_from_realized_magnitudes(db: Path) -> None:
    _insert(db, ticker="NU", conviction="high", outcome_label="correct",
            outcome_at="2026-02-01T00:00:00")  # fmt: skip
    _insert(db, ticker="AAPL", conviction="high", outcome_label="wrong",
            outcome_at="2026-02-01T00:00:00")  # fmt: skip
    mags = {"NU": 5000.0, "AAPL": -2000.0}
    stats = build_calibration(db_path=db, magnitudes_by_ticker=mags)
    assert stats is not None
    exp = stats.expectancy
    assert exp is not None
    assert (exp.n, exp.wins, exp.losses) == (2, 1, 1)
    assert exp.avg_win == pytest.approx(5000.0)
    assert exp.avg_loss == pytest.approx(2000.0)
    assert exp.slugging == pytest.approx(2.5)
    assert exp.expectancy == pytest.approx(1500.0)  # (5000 - 2000) / 2
    hi = next(b for b in stats.by_conviction if b.conviction == "high")
    assert hi.expectancy is not None and hi.expectancy.n == 2


def test_expectancy_none_without_magnitudes(db: Path) -> None:
    _insert(db, conviction="high", outcome_label="correct", outcome_at="2026-02-01T00:00:00")
    stats = build_calibration(db_path=db)
    assert stats is not None
    assert stats.expectancy is None
    hi = next(b for b in stats.by_conviction if b.conviction == "high")
    assert hi.expectancy is None


# ----- L-seam 8: process-quality x outcome matrix -----


def _set_process_quality(db: Path, ticker: str, quality: str) -> None:
    conn = sqlite3.connect(str(db))
    conn.execute("UPDATE decisions SET process_quality = ? WHERE ticker = ?", (quality, ticker))
    conn.commit()
    conn.close()


def test_process_by_outcome_matrix(db: Path) -> None:
    # AAA: correct but lucky → right for the wrong reasons.
    # BBB: wrong but sound → wrong for the right reasons.
    # CCC: correct and sound → the clean case.
    _insert(db, ticker="AAA", outcome_label="correct", outcome_at="2026-02-01T00:00:00")
    _insert(db, ticker="BBB", outcome_label="wrong", outcome_at="2026-02-01T00:00:00")
    _insert(db, ticker="CCC", outcome_label="correct", outcome_at="2026-02-01T00:00:00")
    _set_process_quality(db, "AAA", "lucky")
    _set_process_quality(db, "BBB", "sound")
    _set_process_quality(db, "CCC", "sound")
    stats = build_calibration(db_path=db)
    assert stats is not None
    m = stats.process_outcome
    assert m is not None
    assert m.total_scored == 3
    assert m.right_for_wrong_reasons == 1  # AAA
    assert m.wrong_for_right_reasons == 1  # BBB
    assert m.sound_and_correct == 1  # CCC
    assert m.cells[("lucky", "correct")] == 1
    assert m.cells[("sound", "wrong")] == 1


def test_process_matrix_none_when_unscored(db: Path) -> None:
    _insert(db, ticker="AAA", outcome_label="correct", outcome_at="2026-02-01T00:00:00")
    stats = build_calibration(db_path=db)
    assert stats is not None
    assert stats.process_outcome is None


def test_realized_magnitudes_precedence_and_fallback() -> None:
    pa = PositionAlpha(
        start_date=None, end_date=None, has_policy=False, total_actual_pl=None,
        total_spy_pl=None, total_alpha=None, total_alpha_vs_qqq=None, total_alpha_vs_policy=None,
        rows=[PositionAlphaRow(
            ticker="NU", name=None, value_at_start=None, bought_in_window=None,
            sold_in_window=None, value_at_end=None, actual_pl=None, spy_counterfactual_pl=None,
            alpha=3000.0, alpha_vs_qqq=None, alpha_vs_policy=None, incomplete=False)],
    )  # fmt: skip
    eq = ExitQuality(
        start_date=None, end_date=None, total_sold_proceeds=None, total_value_if_held=None,
        total_regret_vs_hold=None, total_spy_value_if_reinvested=None, total_exit_alpha_vs_spy=None,
        rows=[
            ExitQualityRow(ticker="NU", name=None, sold_shares=None, sold_proceeds=None,
                avg_sell_price=None, price_now=None, value_if_held=None, regret_vs_hold=None,
                spy_value_if_reinvested=None, exit_alpha_vs_spy=999.0, still_held=False),
            ExitQualityRow(ticker="TSLA", name=None, sold_shares=None, sold_proceeds=None,
                avg_sell_price=None, price_now=None, value_if_held=None, regret_vs_hold=None,
                spy_value_if_reinvested=None, exit_alpha_vs_spy=-400.0, still_held=False),
        ],
    )  # fmt: skip
    mags = realized_magnitudes(pa, eq)
    assert mags["NU"] == pytest.approx(3000.0)  # position-alpha takes precedence
    assert mags["TSLA"] == pytest.approx(-400.0)  # exit-quality fills closed names
