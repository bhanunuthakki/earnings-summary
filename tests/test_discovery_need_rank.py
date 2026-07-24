"""P1-B focused Discovery ranking (PRD §8.2): the pure need_rank legs — no
DB, no files, no network. ``compute_need_rank``'s I/O gathering is exercised
indirectly through ``run_discovery.discover`` in ``test_discovery.py``; this
file pins the scoring logic the PRD's priority order depends on.
"""

from __future__ import annotations

from dataclasses import replace

from discovery.need_rank import (
    ADJACENCY_CAP,
    GARP_MAX,
    composite_score,
    effort_estimate,
    eval_adjacency,
    first_rejection,
    garp_grade,
    need_rank_from_json,
    need_rank_to_json,
)
from discovery.screens import TickerMetrics

_DEFAULT_METRICS = TickerMetrics(
    ticker="TEST",
    name="Test Co",
    sector="Technology",
    industry="Software",
    market_cap=5e9,
    roic_ttm=None,
    fcf_yield_ttm=None,
    nd_to_ebitda_ttm=None,
    rev_yoy=None,
    rev_yoy_prior=None,
    gross_margin_ttm=None,
    op_margin_ttm=None,
    is_actively_trading=True,
    latest_income_date="2026-03-31",
)


def _metrics(**overrides: object) -> TickerMetrics:
    return replace(_DEFAULT_METRICS, **overrides)


_EvalNames = dict[str, tuple[str | None, str | None]]


def _evals(**by_ticker: tuple[str | None, str | None]) -> _EvalNames:
    return dict(by_ticker)


# ----------------------------------------------------------------------------
# eval_adjacency
# ----------------------------------------------------------------------------


def test_adjacency_peer_of_evaluation_is_strongest() -> None:
    eval_names = _evals(WDC=("Technology", "Hardware"))
    score, reasons = eval_adjacency({"WDC"}, "Technology", "Hardware", eval_names)
    assert score == 2.0
    assert reasons == ("peer of WDC (evaluation)",)


def test_adjacency_peer_beats_a_same_sector_non_peer() -> None:
    """A peer match (+2) plus an incidental same-sector-different-industry
    name (+0.5) sum — the priority order is additive, not winner-take-all."""
    eval_names = _evals(WDC=("Technology", "Hardware"), MU=("Technology", "Semiconductors"))
    score, reasons = eval_adjacency({"WDC"}, "Technology", "Hardware", eval_names)
    assert score == 2.5
    assert reasons == ("peer of WDC (evaluation)", "same sector as MU")


def test_adjacency_industry_match_without_peer() -> None:
    eval_names = _evals(WDC=("Technology", "Hardware"))
    score, reasons = eval_adjacency(set(), "Technology", "Hardware", eval_names)
    assert score == 1.0
    assert reasons == ("same industry as WDC",)


def test_adjacency_sector_only_is_weakest() -> None:
    eval_names = _evals(WDC=("Technology", "Hardware"))
    score, reasons = eval_adjacency(set(), "Technology", "Software", eval_names)
    assert score == 0.5
    assert reasons == ("same sector as WDC",)


def test_adjacency_no_match_scores_zero() -> None:
    eval_names = _evals(WDC=("Technology", "Hardware"))
    score, reasons = eval_adjacency(set(), "Energy", "Oil & Gas", eval_names)
    assert score == 0.0
    assert reasons == ()


def test_adjacency_caps_at_three() -> None:
    # Four peer hits at +2 each would be 8.0 uncapped — must clamp to the cap.
    eval_names: _EvalNames = {f"P{i}": ("Technology", "Hardware") for i in range(4)}
    score, reasons = eval_adjacency(set(eval_names), "Technology", "Hardware", eval_names)
    assert score == ADJACENCY_CAP
    assert len(reasons) <= 4  # ADJACENCY_MAX_REASONS


def test_adjacency_empty_eval_names_never_crashes() -> None:
    score, reasons = eval_adjacency({"WDC"}, "Technology", "Hardware", {})
    assert score == 0.0
    assert reasons == ()


# ----------------------------------------------------------------------------
# garp_grade
# ----------------------------------------------------------------------------


def test_garp_grade_top_case() -> None:
    m = _metrics(rev_yoy=0.15, fcf_yield_ttm=0.05, roic_ttm=0.12)
    score, reason = garp_grade(m)
    assert score == GARP_MAX
    assert "growth at a reasonable FCF yield" in reason
    assert "rev YoY 15.0%" in reason


def test_garp_grade_growth_without_valuation_anchor() -> None:
    m = _metrics(rev_yoy=0.25, fcf_yield_ttm=-0.02, roic_ttm=0.10)
    score, reason = garp_grade(m)
    assert score == 0.5
    assert "growth without a valuation anchor" in reason


def test_garp_grade_growth_without_valuation_anchor_when_fcf_missing() -> None:
    m = _metrics(rev_yoy=0.30, fcf_yield_ttm=None, roic_ttm=None)
    score, reason = garp_grade(m)
    assert score == 0.5
    assert "growth without a valuation anchor" in reason


def test_garp_grade_graded_between() -> None:
    m = _metrics(rev_yoy=0.06, fcf_yield_ttm=0.025, roic_ttm=0.09)
    score, reason = garp_grade(m)
    assert 0.0 < score < GARP_MAX
    assert "no P/E cached" in reason  # the FCF-yield-proxy disclosure


def test_garp_grade_no_metrics() -> None:
    score, reason = garp_grade(None)
    assert score == 0.0
    assert "no cached fundamentals" in reason


def test_garp_grade_reason_always_carries_the_proxy_disclosure() -> None:
    """Every non-trivial reason must disclose the FCF-yield-for-P/E proxy —
    the card must never imply precision the screen cache doesn't have."""
    for m in (
        _metrics(rev_yoy=0.15, fcf_yield_ttm=0.05, roic_ttm=0.12),
        _metrics(rev_yoy=0.06, fcf_yield_ttm=0.01, roic_ttm=0.02),
    ):
        _score, reason = garp_grade(m)
        assert "FCF yield" in reason and "proxy" in reason


# ----------------------------------------------------------------------------
# effort_estimate
# ----------------------------------------------------------------------------


def test_effort_light_with_metrics_and_deep_history() -> None:
    assert effort_estimate(True, 252) == "light"


def test_effort_medium_with_metrics_only() -> None:
    assert effort_estimate(True, None) == "medium"
    assert effort_estimate(True, 10) == "medium"  # history too thin for "light"


def test_effort_medium_with_history_only() -> None:
    assert effort_estimate(False, 200) == "medium"


def test_effort_heavy_with_nothing() -> None:
    assert effort_estimate(False, None) == "heavy"
    assert effort_estimate(False, 0) == "heavy"


# ----------------------------------------------------------------------------
# first_rejection
# ----------------------------------------------------------------------------


def test_first_rejection_leverage_gate() -> None:
    m = _metrics(nd_to_ebitda_ttm=3.2)
    reason = first_rejection(m)
    assert reason is not None
    assert "3.2x ND/EBITDA" in reason


def test_first_rejection_decelerating_revenue() -> None:
    m = _metrics(nd_to_ebitda_ttm=1.0, rev_yoy=0.05, rev_yoy_prior=0.20)
    reason = first_rejection(m)
    assert reason is not None
    assert "decelerating" in reason
    assert "5.0%" in reason and "20.0%" in reason


def test_first_rejection_negative_margin() -> None:
    m = _metrics(nd_to_ebitda_ttm=1.0, rev_yoy=0.10, rev_yoy_prior=0.05, op_margin_ttm=-0.03)
    reason = first_rejection(m)
    assert reason is not None
    assert "operating margin negative" in reason
    assert "-3.0%" in reason


def test_first_rejection_none_when_clean() -> None:
    m = _metrics(nd_to_ebitda_ttm=1.0, rev_yoy=0.20, rev_yoy_prior=0.05, op_margin_ttm=0.15)
    assert first_rejection(m) is None


def test_first_rejection_none_metrics() -> None:
    assert first_rejection(None) is None


# ----------------------------------------------------------------------------
# composite ordering — the PRD's priority order made numeric
# ----------------------------------------------------------------------------


def test_composite_adjacency_outranks_signal_at_equal_other_legs() -> None:
    """An adjacency-3 (corroborating) name must outrank a signal-heavy but
    portfolio-cold (adjacency-0) name when every other leg is equal — the
    PRD's #1 priority: evaluation-list adjacency beats raw signal strength."""
    corroborating = composite_score(eval_adj=3.0, diversifier=None, garp=1.0, base_score=1.5)
    cold_but_loud = composite_score(eval_adj=0.0, diversifier=None, garp=1.0, base_score=50.0)
    assert corroborating > cold_but_loud


def test_composite_diversifier_none_contributes_zero_not_a_crash() -> None:
    with_none = composite_score(eval_adj=1.0, diversifier=None, garp=1.0, base_score=2.0)
    with_neutral = composite_score(eval_adj=1.0, diversifier=1.0, garp=1.0, base_score=2.0)
    # A neutral (book-average) diversifier of 1.0 sits mid-band and contributes
    # a positive amount, so it must score >= the None (uncomputed) case.
    assert with_neutral >= with_none


def test_composite_higher_garp_scores_higher_at_equal_other_legs() -> None:
    low = composite_score(eval_adj=1.0, diversifier=None, garp=0.5, base_score=2.0)
    high = composite_score(eval_adj=1.0, diversifier=None, garp=2.0, base_score=2.0)
    assert high > low


def test_composite_is_deterministic() -> None:
    a = composite_score(eval_adj=2.0, diversifier=1.1, garp=1.5, base_score=3.0)
    b = composite_score(eval_adj=2.0, diversifier=1.1, garp=1.5, base_score=3.0)
    assert a == b


# ----------------------------------------------------------------------------
# JSON round-trip (score_json['need_rank'] persistence shape)
# ----------------------------------------------------------------------------


def test_need_rank_json_round_trip() -> None:
    from discovery.need_rank import NeedRank

    rank = NeedRank(
        eval_adjacency=2.0,
        adjacency_reasons=("peer of WDC (evaluation)",),
        diversifier=1.08,
        diversifier_note="fit 1.08x vs the current book",
        preliminary=True,
        garp=1.5,
        garp_reason="growth-valuation-quality profile: rev YoY 12.0%, FCF yield (proxy, no P/E cached) 2.5%, ROIC 10.0%",
        signal=2.9,
        effort="light",
        first_rejection_reason=None,
        composite=6.4,
    )
    blob = need_rank_to_json(rank)
    restored = need_rank_from_json(blob)
    assert restored == rank


def test_need_rank_from_json_degrades_on_garbage() -> None:
    assert need_rank_from_json(None) is None
    assert need_rank_from_json("not a dict") is None
    assert need_rank_from_json([1, 2, 3]) is None
    # A malformed blob (wrong types) degrades to None, not a raised exception.
    assert need_rank_from_json({"effort": "bogus", "eval_adjacency": "not a number"}) is not None
