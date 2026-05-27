"""Tests for the calibration-score aggregation helpers in the graders.

P2-16 built the prompt_calibration_scores table + record/summarize API;
this PR's job (P4-A4) is to make the graders actually call record_score so
the table fills with production data. These tests pin the score math —
the wiring side is the one-line monkeypatch-style hook above.
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "execution"))

from grade_bear_cases import _aggregate_to_calibration_score  # noqa: E402
from grade_decisions import _decisions_calibration_score  # noqa: E402


def test_bear_case_perfect_run_scores_one_point_zero() -> None:
    score = _aggregate_to_calibration_score(
        {"met": 5, "missed": 0, "mixed": 0, "unfalsifiable": 0}
    )
    assert score == 1.0


def test_bear_case_zero_when_all_missed() -> None:
    score = _aggregate_to_calibration_score(
        {"met": 0, "missed": 5, "mixed": 0, "unfalsifiable": 0}
    )
    assert score == 0.0


def test_bear_case_half_credit_on_mixed() -> None:
    """met=2, missed=2, mixed=2 → 2 + 1 (half-credit) = 3 / 6 = 0.5"""
    score = _aggregate_to_calibration_score(
        {"met": 2, "missed": 2, "mixed": 2, "unfalsifiable": 0}
    )
    assert score == 0.5


def test_bear_case_unfalsifiable_dropped_from_denominator() -> None:
    """met=1, unfalsifiable=10 → 1/1 = 1.0 (the 10 unfalsifiable predictions
    aren't a signal in either direction so they don't enter the ratio)."""
    score = _aggregate_to_calibration_score(
        {"met": 1, "missed": 0, "mixed": 0, "unfalsifiable": 10}
    )
    assert score == 1.0


def test_bear_case_returns_none_when_zero_falsifiable() -> None:
    score = _aggregate_to_calibration_score(
        {"met": 0, "missed": 0, "mixed": 0, "unfalsifiable": 7}
    )
    assert score is None


def test_decision_score_uses_same_credit_scheme() -> None:
    """Decisions: correct = full, mixed = half, wrong = 0. correct=3 wrong=1 mixed=2
    → (3 + 1) / 6 = 0.667."""
    score = _decisions_calibration_score(
        {"correct": 3, "wrong": 1, "mixed": 2, "skipped_no_price": 4, "graded": 6}
    )
    assert score is not None
    assert abs(score - (4 / 6)) < 1e-9


def test_decision_score_none_when_only_skipped_no_price() -> None:
    score = _decisions_calibration_score(
        {"correct": 0, "wrong": 0, "mixed": 0, "skipped_no_price": 5, "graded": 0}
    )
    assert score is None
