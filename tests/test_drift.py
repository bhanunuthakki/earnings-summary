"""Phase-2 tests: conviction-drift narration over the timeseries primitive."""

from __future__ import annotations

from datetime import datetime

import pytest

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


# --- the opt-in drift_narrate phrasing layer (narrate_fn) -----------------------


def test_no_narrator_fires_no_llm_and_keeps_the_deterministic_baseline() -> None:
    # default (no narrate_fn) must never touch an LLM; the computed summary stands.
    out = narrate_drift(_series([1.0, 2.0, 3.0, 4.0, 5.0]))
    assert str(out["summary"]).startswith("Conviction is")


def test_injected_narrator_replaces_only_the_summary() -> None:
    out = narrate_drift(
        _series([1.0, 2.0, 3.0, 4.0, 5.0, 6.0]), narrate_fn=lambda _s: "Getting surer here"
    )
    assert out["drift"] == "firming"  # classification is authoritative, unchanged
    assert out["summary"] == "Getting surer here"


def test_narrator_receives_the_computed_signal_incl_baseline() -> None:
    seen: dict[str, object] = {}
    narrate_drift(
        _series([6.0, 5.0, 4.0, 3.0, 2.0, 1.0]), narrate_fn=lambda s: seen.update(s) or "x"
    )
    assert seen["drift"] == "softening"
    assert "baseline_summary" in seen  # the narrator is handed the deterministic baseline


def test_narrator_failure_keeps_the_baseline() -> None:
    def boom(_s: dict[str, object]) -> str:
        raise RuntimeError("llm down")

    out = narrate_drift(_series([1.0, 2.0, 3.0, 4.0, 5.0]), narrate_fn=boom)
    assert str(out["summary"]).startswith("Conviction is")


def test_narrator_empty_output_keeps_baseline() -> None:
    out = narrate_drift(_series([1.0, 2.0, 3.0, 4.0, 5.0]), narrate_fn=lambda _s: "   ")
    assert str(out["summary"]).startswith("Conviction is")


def test_insufficient_series_never_calls_the_narrator() -> None:
    calls = {"n": 0}

    def spy(_s: dict[str, object]) -> str:
        calls["n"] += 1
        return "x"

    narrate_drift(_series([1.0, 2.0, 3.0]), narrate_fn=spy)  # < 4 points -> early return
    assert calls["n"] == 0


def test_llm_narrate_default_uses_call_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    import research.drift as drift_mod
    from llm import cli as llm_cli

    def fake_call(*_a: object, **_k: object) -> str:
        return "Firming up nicely"

    monkeypatch.setattr(llm_cli, "call_llm", fake_call)
    out = narrate_drift(_series([1.0, 2.0, 3.0, 4.0, 5.0]), narrate_fn=drift_mod.llm_narrate)
    assert out["summary"] == "Firming up nicely"


def test_llm_narrate_failure_degrades_to_baseline(monkeypatch: pytest.MonkeyPatch) -> None:
    import research.drift as drift_mod
    from llm import cli as llm_cli

    def boom(*_a: object, **_k: object) -> str:
        raise RuntimeError("cli missing")

    monkeypatch.setattr(llm_cli, "call_llm", boom)
    out = narrate_drift(_series([6.0, 5.0, 4.0, 3.0, 2.0, 1.0]), narrate_fn=drift_mod.llm_narrate)
    assert str(out["summary"]).startswith("Conviction is")  # _phrase swallowed the failure
