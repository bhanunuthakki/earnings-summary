"""Shared minimum-n guard (src/calibration_guard.py, Close-the-Loops L1):
the one place L1/L8/L10 decide when a rate is trustworthy and how a sparse
denominator is framed. Pure functions — no I/O, no LLM."""

from __future__ import annotations

import pytest

from calibration_guard import (
    MIN_CONFIDENT_N,
    confidence_note,
    is_confident,
    rate_phrase,
)


def test_is_confident_brackets_the_default_floor() -> None:
    assert not is_confident(MIN_CONFIDENT_N - 1)
    assert is_confident(MIN_CONFIDENT_N)
    assert is_confident(MIN_CONFIDENT_N + 5)
    # The directive's worked example: n=12 confident, n=3 not.
    assert is_confident(12)
    assert not is_confident(3)


def test_is_confident_honors_a_caller_supplied_floor() -> None:
    # L10 may want a stricter bar before replacing a hand-set constant.
    assert is_confident(8, min_n=8)
    assert not is_confident(8, min_n=20)


def test_confidence_note_labels_the_denominator_and_its_trust() -> None:
    assert confidence_note(12) == "n=12"
    assert confidence_note(3) == "n=3, low confidence"
    assert confidence_note(0) == "n=0, no graded calls"
    assert confidence_note(-1) == "n=0, no graded calls"


def test_rate_phrase_asserts_only_above_the_floor() -> None:
    # Confident: the percentage stands plainly.
    assert (
        rate_phrase("high-conviction calls", 0.4, 12) == "high-conviction calls 40% correct (n=12)"
    )
    # Sparse: the figure is shown but explicitly hedged, never suppressed.
    sparse = rate_phrase("high-conviction calls", 0.4, 3)
    assert "40% correct" in sparse
    assert "only n=3" in sparse
    assert "low confidence" in sparse


def test_rate_phrase_handles_the_empty_denominator() -> None:
    assert (
        rate_phrase("low-conviction calls", None, 0)
        == "low-conviction calls: no graded calls yet (n=0)"
    )
    # A None rate with a positive n can't happen from build_calibration, but
    # the guard must not divide by it regardless.
    assert "no graded calls yet" in rate_phrase("x", None, 5)


def test_rate_phrase_rounds_to_a_whole_percent() -> None:
    assert rate_phrase("calls", 2 / 3, 30) == "calls 67% correct (n=30)"


@pytest.mark.parametrize("n", [0, 1, 9, 10, 100])
def test_rate_phrase_never_raises(n: int) -> None:
    # Whatever the denominator, the phrase is a non-empty string.
    assert rate_phrase("x", 0.5 if n else None, n).strip()
