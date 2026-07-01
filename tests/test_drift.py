"""Phase-2 tests: conviction-drift narration over the timeseries primitive."""

from __future__ import annotations

from datetime import datetime

from research.drift import narrate_drift
from timeseries.primitives import Observation


def _series(values: list[float]) -> list[Observation]:
    return [Observation(period_end=datetime(2026, 1 + i, 1), value=v) for i, v in enumerate(values)]


def test_insufficient_data_reads_as_insufficient() -> None:
    out = narrate_drift(_series([1.0, 2.0, 3.0]))  # < 4 points
    assert out["drift"] == "insufficient"


def test_a_clear_rising_conviction_series_is_firming() -> None:
    out = narrate_drift(_series([1.0, 2.0, 3.0, 4.0, 5.0, 6.0]))
    assert out["drift"] == "firming"
    assert float(out["slope"]) > 0


def test_a_clear_falling_conviction_series_is_softening() -> None:
    out = narrate_drift(_series([6.0, 5.0, 4.0, 3.0, 2.0, 1.0]))
    assert out["drift"] == "softening"
    assert float(out["slope"]) < 0


def test_a_flat_series_is_steady() -> None:
    out = narrate_drift(_series([3.0, 3.0, 3.0, 3.0, 3.0]))
    assert out["drift"] == "steady"


def test_summary_is_a_short_human_string() -> None:
    out = narrate_drift(_series([1.0, 2.0, 3.0, 4.0, 5.0]))
    assert isinstance(out["summary"], str)
    assert "Conviction is" in str(out["summary"])
