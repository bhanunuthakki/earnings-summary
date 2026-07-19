"""Tests for the stance scorecard (master build P2.5).

Layers:
* price-return lookup over the div-adjusted FMP cache (at-or-before dates,
  coverage gating — never grade a window the data doesn't cover),
* the verdict rules (directional deadband, the hold floor),
* score_memo per kind — socratic (tracker-SPY vs absolute basis), swap
  (realized pair margin), next_dollar (unscoreable), with deferral on
  immature horizons / cache gaps,
* run_scoring end-to-end over an alembic DB (idempotent over the pending
  set; score_status flips),
* the Memos panel's verdict pills + track-record strip.
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic.config import Config

from alembic import command

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "execution"))
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import advisor.scoring as scoring_mod  # noqa: E402
from advisor.scoring import (  # noqa: E402
    grade_directional,
    price_return_from_cache,
    run_scoring,
    score_memo,
)
from advisor.store import (  # noqa: E402
    AdvisorMemoRow,
    StanceScoreRow,
    get_memo,
    insert_memo,
    list_scores_for_memos,
)
from pipeline.advisor_memos_panel import compose_memos_page  # noqa: E402

_PRIOR_HEAD = "0059_kpi_facts_restatement"


def _build_db(tmp_path: Path) -> Path:
    db = tmp_path / "data" / "portfolio.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db}")
    command.stamp(cfg, _PRIOR_HEAD)
    command.upgrade(cfg, "head")
    return db


def _write_price_cache(repo_root: Path, ticker: str, rows: list[tuple[str, float]]) -> None:
    """rows = [(date, adjClose)] — written newest-first like the FMP cache."""
    out = repo_root / "data" / "historical" / "fmp"
    out.mkdir(parents=True, exist_ok=True)
    payload = [
        {"symbol": ticker, "date": d, "adjClose": c}
        for d, c in sorted(rows, key=lambda r: r[0], reverse=True)
    ]
    (out / f"{ticker}_price_chart_10y_div_adj.json").write_text(
        json.dumps(payload), encoding="utf-8"
    )


# --------------------------------------------------------------------------- #
# Price lookup + verdict rules
# --------------------------------------------------------------------------- #


def test_price_return_at_or_before_dates(tmp_path: Path) -> None:
    _write_price_cache(
        tmp_path,
        "NU",
        [("2026-01-02", 10.0), ("2026-01-05", 11.0), ("2026-04-01", 12.0), ("2026-04-03", 13.0)],
    )
    # Window endpoints fall on non-trading days -> nearest earlier close.
    ret = price_return_from_cache(tmp_path, "NU", "2026-01-04", "2026-04-02")
    assert ret == pytest.approx((12.0 / 10.0 - 1.0) * 100.0)


def test_price_return_defers_on_coverage_gaps(tmp_path: Path) -> None:
    _write_price_cache(tmp_path, "NU", [("2026-01-02", 10.0), ("2026-03-01", 12.0)])
    # Cache hasn't caught up to the window end -> None (defer).
    assert price_return_from_cache(tmp_path, "NU", "2026-01-02", "2026-04-01") is None
    # Window starts before the cache begins -> None.
    assert price_return_from_cache(tmp_path, "NU", "2025-12-01", "2026-03-01") is None
    # Missing cache entirely -> None.
    assert price_return_from_cache(tmp_path, "ZZZZ", "2026-01-02", "2026-03-01") is None


def test_grade_directional_rules() -> None:
    assert grade_directional("buy", 5.0) == "correct"
    assert grade_directional("add", 1.0) == "mixed"
    assert grade_directional("buy", -5.0) == "wrong"
    assert grade_directional("sell", -5.0) == "correct"
    assert grade_directional("trim", 5.0) == "wrong"
    assert grade_directional("hold", -3.0) == "correct"  # above the -5pp floor
    assert grade_directional("hold", -8.0) == "wrong"
    assert grade_directional("hold", 12.0) == "correct"  # a rally doesn't fail a hold


# --------------------------------------------------------------------------- #
# score_memo per kind
# --------------------------------------------------------------------------- #

_NOW = datetime(2026, 9, 20, tzinfo=UTC)  # well past a 90d horizon from June


def _spy_stub(value: float | None):
    """Typed monkeypatch target for spy_return_from_tracker."""

    def stub(*a: object, **k: object) -> float | None:
        return value

    return stub


def _memo(
    *,
    memo_id: int = 1,
    kind: str = "socratic",
    ticker: str | None = "NU",
    counter: str | None = None,
    stance: str | None = "add",
    horizon: int = 90,
    created: str = "2026-06-01T12:00:00+00:00",
    context: dict[str, object] | None = None,
) -> AdvisorMemoRow:
    return AdvisorMemoRow(
        id=memo_id,
        user_id="bhanu",
        kind=kind,
        ticker=ticker,
        counter_ticker=counter,
        title="t",
        body_md="b",
        context=context,
        stance=stance,
        horizon_days=horizon,
        score_status="pending",
        note_id=None,
        ledger_entry_id=None,
        created_at=datetime.fromisoformat(created),
    )


def test_socratic_scores_tracker_spy_basis(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = _build_db(tmp_path)
    # NU +20% over the horizon; SPY +5% -> excess +15pp -> 'add' correct.
    _write_price_cache(tmp_path, "NU", [("2026-06-01", 10.0), ("2026-08-30", 12.0)])
    monkeypatch.setattr(scoring_mod, "spy_return_from_tracker", _spy_stub(5.0))
    outcome = score_memo(tmp_path, _memo(), now=_NOW, db_path=db)
    assert outcome.scored and outcome.verdict == "correct"
    score = list_scores_for_memos([1], db_path=db)[1]
    assert score.benchmark_basis == "tracker_spy"
    assert score.excess_return_pct == pytest.approx(15.0)
    assert score.ticker_return_pct == pytest.approx(20.0)


def test_socratic_falls_back_to_absolute_when_tracker_down(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = _build_db(tmp_path)
    _write_price_cache(tmp_path, "NU", [("2026-06-01", 10.0), ("2026-08-30", 9.0)])  # -10%
    monkeypatch.setattr(scoring_mod, "spy_return_from_tracker", _spy_stub(None))
    outcome = score_memo(tmp_path, _memo(stance="trim"), now=_NOW, db_path=db)
    assert outcome.scored and outcome.verdict == "correct"  # trim before a -10% move
    score = list_scores_for_memos([1], db_path=db)[1]
    assert score.benchmark_basis == "absolute" and score.excess_return_pct is None


def test_socratic_defers_until_mature_and_covered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = _build_db(tmp_path)
    monkeypatch.setattr(scoring_mod, "spy_return_from_tracker", _spy_stub(None))
    # Immature horizon -> defer.
    early = datetime(2026, 7, 1, tzinfo=UTC)
    outcome = score_memo(tmp_path, _memo(), now=early, db_path=db)
    assert not outcome.scored and "matures" in (outcome.defer_reason or "")
    # Mature but the price cache doesn't cover the window -> defer.
    _write_price_cache(tmp_path, "NU", [("2026-06-01", 10.0), ("2026-07-15", 11.0)])
    outcome2 = score_memo(tmp_path, _memo(), now=_NOW, db_path=db)
    assert not outcome2.scored and "price cache" in (outcome2.defer_reason or "")
    assert list_scores_for_memos([1], db_path=db) == {}


def test_socratic_without_stance_is_unscoreable(tmp_path: Path) -> None:
    db = _build_db(tmp_path)
    _write_price_cache(tmp_path, "NU", [("2026-06-01", 10.0), ("2026-08-30", 12.0)])
    outcome = score_memo(tmp_path, _memo(stance=None), now=_NOW, db_path=db)
    assert outcome.scored and outcome.verdict == "unscoreable"


def test_swap_check_grades_realized_margin(tmp_path: Path) -> None:
    db = _build_db(tmp_path)
    _write_price_cache(tmp_path, "NU", [("2026-06-01", 10.0), ("2026-08-30", 11.0)])  # +10%
    _write_price_cache(tmp_path, "BKNG", [("2026-06-01", 100.0), ("2026-08-30", 125.0)])  # +25%
    outcome = score_memo(
        tmp_path,
        _memo(kind="swap_check", counter="BKNG", stance=None),
        now=_NOW,
        db_path=db,
    )
    assert outcome.scored and outcome.verdict == "screen_validated"
    score = list_scores_for_memos([1], db_path=db)[1]
    assert score.counter_return_pct == pytest.approx(25.0)
    assert score.detail is not None
    assert score.detail["realized_margin_pp"] == pytest.approx(15.0)
    assert score.benchmark_basis == "none"


def test_next_dollar_marks_unscoreable_immediately(tmp_path: Path) -> None:
    db = _build_db(tmp_path)
    outcome = score_memo(
        tmp_path,
        _memo(kind="next_dollar", ticker=None, stance=None),
        now=datetime(2026, 6, 11, tzinfo=UTC),  # even before the horizon
        db_path=db,
    )
    assert outcome.scored and outcome.verdict == "unscoreable"
    score = list_scores_for_memos([1], db_path=db)[1]
    assert score.detail is not None
    assert "legacy" in str(score.detail["reason"])


def test_next_dollar_with_model_rows_defers_until_mature(tmp_path: Path) -> None:
    """A next_dollar memo persisted WITH model_rows now waits for its horizon
    (like every other gradeable kind) instead of being marked unscoreable
    immediately — the whole point of persisting model_rows is that it CAN
    eventually be graded."""
    db = _build_db(tmp_path)
    memo = _memo(
        kind="next_dollar",
        ticker=None,
        stance=None,
        context={"model_rows": [{"ticker": "NU", "upside_pct": 20.0}]},
    )
    outcome = score_memo(tmp_path, memo, now=datetime(2026, 6, 11, tzinfo=UTC), db_path=db)
    assert not outcome.scored
    assert "matures" in (outcome.defer_reason or "")


def test_next_dollar_with_model_rows_grades_top_pick_mechanically(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = _build_db(tmp_path)
    # NU (the top-ranked pick) +20%; SPY +5% -> beats the basis -> validated.
    _write_price_cache(tmp_path, "NU", [("2026-06-01", 10.0), ("2026-08-30", 12.0)])
    monkeypatch.setattr(scoring_mod, "spy_return_from_tracker", _spy_stub(5.0))
    memo = _memo(
        kind="next_dollar",
        ticker=None,
        stance=None,
        context={
            "model_rows": [
                {"ticker": "NU", "upside_pct": 20.0},
                {"ticker": "WIX", "upside_pct": 10.0},
            ]
        },
    )
    outcome = score_memo(tmp_path, memo, now=_NOW, db_path=db)
    assert outcome.scored and outcome.verdict == "screen_validated"
    score = list_scores_for_memos([1], db_path=db)[1]
    assert score.ticker == "NU"
    assert score.benchmark_basis == "tracker_spy"
    assert score.detail is not None
    assert score.detail["top_ticker"] == "NU"


def test_next_dollar_top_pick_below_basis_is_refuted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = _build_db(tmp_path)
    _write_price_cache(tmp_path, "NU", [("2026-06-01", 10.0), ("2026-08-30", 10.2)])  # +2%
    monkeypatch.setattr(scoring_mod, "spy_return_from_tracker", _spy_stub(5.0))
    memo = _memo(
        kind="next_dollar",
        ticker=None,
        stance=None,
        context={"model_rows": [{"ticker": "NU", "upside_pct": 20.0}]},
    )
    outcome = score_memo(tmp_path, memo, now=_NOW, db_path=db)
    assert outcome.scored and outcome.verdict == "screen_refuted"


def test_next_dollar_top_row_missing_ticker_is_unscoreable(tmp_path: Path) -> None:
    db = _build_db(tmp_path)
    memo = _memo(
        kind="next_dollar", ticker=None, stance=None, context={"model_rows": [{"upside_pct": 20.0}]}
    )
    outcome = score_memo(tmp_path, memo, now=_NOW, db_path=db)
    assert outcome.scored and outcome.verdict == "unscoreable"
    score = list_scores_for_memos([1], db_path=db)[1]
    assert score.detail is not None
    assert "no ticker" in str(score.detail["reason"])


def test_guard_override_position_review_scored_as_hold_with_track_record_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§5.3b: a guard_override review is graded as an explicit hold at its
    horizon, with a distinguishing ``detail.guard_override`` marker so its
    track record is separately queryable (the decision journal / calibration
    scorecard partition on it) rather than folded, unmarked, into the generic
    Socratic path."""
    db = _build_db(tmp_path)
    _write_price_cache(tmp_path, "NU", [("2026-06-01", 10.0), ("2026-08-30", 9.0)])  # -10%
    monkeypatch.setattr(scoring_mod, "spy_return_from_tracker", _spy_stub(None))
    memo = _memo(
        kind="position_review",
        stance="hold",
        context={"verdict_source": "guard_override"},
    )
    outcome = score_memo(tmp_path, memo, now=_NOW, db_path=db)
    # -10% absolute is below HOLD_FLOOR_PP (-5.0) -> wrong.
    assert outcome.scored and outcome.verdict == "wrong"
    score = list_scores_for_memos([1], db_path=db)[1]
    assert score.detail is not None
    assert score.detail["guard_override"] is True
    assert score.detail["stance"] == "hold"


def test_guard_override_position_review_correct_when_hold_paid_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = _build_db(tmp_path)
    _write_price_cache(tmp_path, "NU", [("2026-06-01", 10.0), ("2026-08-30", 11.0)])  # +10%
    monkeypatch.setattr(scoring_mod, "spy_return_from_tracker", _spy_stub(None))
    memo = _memo(
        kind="position_review",
        stance="hold",
        context={"verdict_source": "guard_override"},
    )
    outcome = score_memo(tmp_path, memo, now=_NOW, db_path=db)
    assert outcome.scored and outcome.verdict == "correct"


def test_non_guard_position_review_unaffected_by_new_branch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A position_review memo with no verdict_source (or an 'llm' one) still
    falls through to the ordinary Socratic grading path — the new
    guard_override branch must not swallow genuine LLM-authored reviews."""
    db = _build_db(tmp_path)
    _write_price_cache(tmp_path, "NU", [("2026-06-01", 10.0), ("2026-08-30", 12.0)])  # +20%
    monkeypatch.setattr(scoring_mod, "spy_return_from_tracker", _spy_stub(5.0))
    memo = _memo(kind="position_review", stance="add", context={"verdict_source": "llm"})
    outcome = score_memo(tmp_path, memo, now=_NOW, db_path=db)
    assert outcome.scored and outcome.verdict == "correct"
    score = list_scores_for_memos([1], db_path=db)[1]
    assert score.detail is not None
    assert "guard_override" not in score.detail


# --------------------------------------------------------------------------- #
# run_scoring end-to-end (real memo rows, idempotent)
# --------------------------------------------------------------------------- #


def test_run_scoring_flips_status_and_is_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = _build_db(tmp_path)
    monkeypatch.setattr(scoring_mod, "spy_return_from_tracker", _spy_stub(0.0))
    memo = insert_memo(
        user_id="bhanu",
        kind="socratic",
        ticker="NU",
        title="Socratic think-through · NU",
        body_md="## Bull\n\nSTANCE: add",
        stance="add",
        horizon_days=30,
        db_path=db,
    )
    nd = insert_memo(
        user_id="bhanu",
        kind="next_dollar",
        ticker=None,
        title="Next-dollar memo",
        body_md="## body",
        db_path=db,
    )
    start = memo.created_at.date().isoformat()
    # Price cache covering [created, created+30d]: +10% absolute -> excess +10pp.
    from datetime import timedelta

    end = (memo.created_at + timedelta(days=31)).date().isoformat()
    _write_price_cache(tmp_path, "NU", [(start, 10.0), (end, 11.0)])
    far_future = memo.created_at + timedelta(days=60)

    result = run_scoring(tmp_path, user_id="bhanu", now=far_future)
    assert result.scored == 2 and result.deferred == 0
    scored_memo = get_memo(memo.id, db_path=db)
    assert scored_memo is not None and scored_memo.score_status == "scored"
    nd_memo = get_memo(nd.id, db_path=db)
    assert nd_memo is not None and nd_memo.score_status == "unscoreable"

    # Idempotent: nothing pending remains, so a second run grades nothing.
    again = run_scoring(tmp_path, user_id="bhanu", now=far_future)
    assert again.scored == 0 and again.deferred == 0
    assert len(list_scores_for_memos([memo.id], db_path=db)) == 1


# --------------------------------------------------------------------------- #
# Panel surfacing
# --------------------------------------------------------------------------- #


def test_panel_shows_verdict_pills_and_track_record() -> None:
    socratic = _memo(memo_id=1, stance="add")
    swap = _memo(memo_id=2, kind="swap_check", counter="BKNG", stance=None)
    scores = {
        1: StanceScoreRow(
            id=1,
            memo_id=1,
            user_id="bhanu",
            verdict="correct",
            benchmark_basis="tracker_spy",
            horizon_days=90,
            start_date="2026-06-01",
            end_date="2026-08-30",
            ticker="NU",
            counter_ticker=None,
            ticker_return_pct=20.0,
            benchmark_return_pct=5.0,
            excess_return_pct=15.0,
            counter_return_pct=None,
            detail=None,
            created_at=datetime(2026, 9, 1, tzinfo=UTC),
        ),
        2: StanceScoreRow(
            id=2,
            memo_id=2,
            user_id="bhanu",
            verdict="screen_refuted",
            benchmark_basis="none",
            horizon_days=90,
            start_date="2026-06-01",
            end_date="2026-08-30",
            ticker="NU",
            counter_ticker="BKNG",
            ticker_return_pct=10.0,
            benchmark_return_pct=None,
            excess_return_pct=None,
            counter_return_pct=2.0,
            detail={"realized_margin_pp": -8.0},
            created_at=datetime(2026, 9, 1, tzinfo=UTC),
        ),
    }
    html = compose_memos_page([], [socratic, swap], scores=scores)
    assert "correct +15.0pp vs SPY" in html
    assert "screen refuted -8.0pp realized" in html
    assert "Track record" in html
    assert "Stances: 1/1 correct" in html and "avg excess +15.0pp" in html
    assert "Swap screens: 0/1 validated" in html
    # Unscored memos still show the pending state, and no strip without grades.
    bare = compose_memos_page([], [socratic])
    assert "Track record" not in bare
    assert 'title="scoring pending"' in bare
