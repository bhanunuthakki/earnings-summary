"""The calibration_finding governor class (B1, program review 2026-07-19).

The 33%-at-high-conviction finding sat computed-but-undelivered for a month.
This class is the zero-LLM delivery leg: ≤1 moment per period when a
high/medium-conviction cohort is graded below the bar with a real denominator.
Hermetic — build_calibration is monkeypatched with synthetic stats.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

import research.calibration_moments as cm
from decision_calibration import CalibrationStats, ConvictionBucket
from research.governor import Moment

_NOW = datetime(2026, 7, 19, 12, 0, 0)


def _bucket(
    conviction: str, *, graded: int, correct: int, wrong: int, mixed: int = 0
) -> ConvictionBucket:
    rate = correct / graded if graded else None
    return ConvictionBucket(
        conviction=conviction,
        graded=graded,
        correct=correct,
        wrong=wrong,
        mixed=mixed,
        ungraded=0,
        hit_rate=rate,
        wilson_low=0.15 if rate is not None else None,
        wilson_high=0.60 if rate is not None else None,
    )


def _stats(buckets: list[ConvictionBucket]) -> CalibrationStats:
    return CalibrationStats(
        total=sum(b.graded + b.ungraded for b in buckets),
        graded=sum(b.graded for b in buckets),
        overall_hit_rate=None,
        by_conviction=buckets,
        action_mix={},
        reversals=[],
        reversals_vindicated=0,
        reversals_cost=0,
        time_to_outcome=[],
    )


def _patch(monkeypatch: pytest.MonkeyPatch, buckets: list[ConvictionBucket]) -> None:
    monkeypatch.setattr(cm, "build_calibration", lambda **_k: _stats(buckets))


def test_fires_on_a_miscalibrated_high_conviction_cohort(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The prod shape: high conviction 5 correct / 8 wrong / 2 mixed = 33%.
    _patch(monkeypatch, [_bucket("high", graded=15, correct=5, wrong=8, mixed=2)])
    out = cm.collect_calibration_finding_moments(Path("x.db"), now=_NOW)
    assert len(out) == 1
    m = out[0]
    assert isinstance(m, Moment)
    assert m.class_ == "calibration_finding"
    assert m.key == "calibration_finding:2026-07:high"
    assert "5 correct / 8 wrong / 2 mixed" in m.body
    assert "33%" in m.body
    assert "75%" in m.body  # what the label implies — the receipt


def test_quiet_below_the_denominator_floor(monkeypatch: pytest.MonkeyPatch) -> None:
    # Same terrible rate but n=6 — a thin cohort can't carry an accusation.
    _patch(monkeypatch, [_bucket("high", graded=6, correct=2, wrong=4)])
    assert cm.collect_calibration_finding_moments(Path("x.db"), now=_NOW) == []


def test_quiet_when_the_cohort_clears_the_bar(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch(monkeypatch, [_bucket("high", graded=15, correct=9, wrong=6)])
    assert cm.collect_calibration_finding_moments(Path("x.db"), now=_NOW) == []


def test_low_and_unstated_cohorts_never_confront(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch(
        monkeypatch,
        [
            _bucket("low", graded=20, correct=4, wrong=16),
            _bucket("unstated", graded=20, correct=4, wrong=16),
        ],
    )
    assert cm.collect_calibration_finding_moments(Path("x.db"), now=_NOW) == []


def test_at_most_one_moment_the_worst_cohort(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch(
        monkeypatch,
        [
            _bucket("medium", graded=12, correct=5, wrong=7),  # 42%
            _bucket("high", graded=15, correct=5, wrong=10),  # 33% — worse
        ],
    )
    out = cm.collect_calibration_finding_moments(Path("x.db"), now=_NOW)
    assert len(out) == 1
    assert out[0].key.endswith(":high")


def test_degrades_to_empty_when_substrate_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cm, "build_calibration", lambda **_k: None)
    assert cm.collect_calibration_finding_moments(Path("x.db"), now=_NOW) == []

    def _boom(**_k: object) -> None:
        raise RuntimeError("no decisions table")

    monkeypatch.setattr(cm, "build_calibration", _boom)
    assert cm.collect_calibration_finding_moments(Path("x.db"), now=_NOW) == []
