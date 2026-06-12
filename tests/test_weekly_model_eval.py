# pyright: reportPrivateUsage=false
"""Tests for the weekly model-eval orchestrator's pure logic (rotation).

The harvest/sweep/apply steps are subprocess + DB orchestration (exercised by
their own modules' tests); here we lock in the deterministic rotation that keeps
weekly harvest cost bounded while covering the universe over time.
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "execution"))

from run_weekly_model_eval import _rotating_sample  # noqa: E402


def test_rotation_covers_universe_over_weeks() -> None:
    tickers = ["A", "B", "C", "D", "E"]
    seen: set[str] = set()
    for week in range(5):
        seen.update(_rotating_sample(tickers, 2, week))
    assert seen == set(tickers)  # every ticker harvested within a rotation cycle


def test_rotation_is_deterministic() -> None:
    tickers = ["A", "B", "C", "D", "E"]
    assert _rotating_sample(tickers, 2, 3) == _rotating_sample(tickers, 2, 3)


def test_rotation_window_size() -> None:
    assert len(_rotating_sample(["A", "B", "C", "D"], 2, 0)) == 2


def test_rotation_size_ge_universe_returns_all() -> None:
    assert set(_rotating_sample(["A", "B"], 5, 7)) == {"A", "B"}


def test_rotation_empty_inputs() -> None:
    assert _rotating_sample([], 2, 0) == []
    assert _rotating_sample(["A", "B"], 0, 0) == []
