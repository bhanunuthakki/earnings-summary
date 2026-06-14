"""Tests for execution/run_calibration_scorecard.py — the monthly scorecard CLI.

The CLI orchestrates build -> (gate when coachable) -> save. All LLM work lives
in calibration_coach (tested separately); here the coach functions are
monkeypatched so the orchestration, the thin-ledger skip-the-gate path, and the
hard-stop exit code are exercised without spend.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import calibration_coach as cc
from calibration_coach import CalibrationScorecard, NamedBias
from execution import run_calibration_scorecard
from llm.cli import LLMBudgetExceeded


def _card(*, can_coach: bool, ok: bool | None) -> CalibrationScorecard:
    return CalibrationScorecard(
        period="2026-06", generated_at="2026-06-14T00:00:00", granularity="quarter",
        can_coach=can_coach, n_graded=12 if can_coach else 3, overall_hit_rate=0.5,
        improving=True, hit_rate_delta=0.1, latest_period="2026-Q2", latest_hit_rate=0.6,
        selection_usd=1.0, sizing_usd=-1.0, timing_usd=None,
        biases=[NamedBias("b", "p", ["e"], "t")] if can_coach else [],
        experiment=None, coach_quality_ok=ok, coach_quality_score=0.8 if ok else None,
    )  # fmt: skip


def test_cli_builds_gates_and_saves(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(cc, "build_scorecard", lambda *_a, **_k: _card(can_coach=True, ok=None))
    monkeypatch.setattr(cc, "gate_scorecard", lambda card, **_k: _card(can_coach=True, ok=True))
    rc = run_calibration_scorecard.main(
        ["--repo-root", str(tmp_path), "--code-root", str(tmp_path), "--period", "2026-06"]
    )
    assert rc == 0
    assert (tmp_path / "data" / "calibration_scorecard" / "2026-06.json").exists()


def test_cli_thin_skips_gate(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(cc, "build_scorecard", lambda *_a, **_k: _card(can_coach=False, ok=None))

    def _boom(*_a: object, **_k: object) -> object:
        raise AssertionError("the gate must not run on a thin scorecard")

    monkeypatch.setattr(cc, "gate_scorecard", _boom)
    rc = run_calibration_scorecard.main(["--repo-root", str(tmp_path), "--period", "2026-06"])
    assert rc == 0
    assert (tmp_path / "data" / "calibration_scorecard" / "2026-06.json").exists()


def test_cli_no_gate_flag(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(cc, "build_scorecard", lambda *_a, **_k: _card(can_coach=True, ok=None))

    def _boom(*_a: object, **_k: object) -> object:
        raise AssertionError("--no-gate must skip the eval gate")

    monkeypatch.setattr(cc, "gate_scorecard", _boom)
    rc = run_calibration_scorecard.main(
        ["--repo-root", str(tmp_path), "--period", "2026-06", "--no-gate"]
    )
    assert rc == 0


def test_cli_hard_stop_returns_2(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def _raise(*_a: object, **_k: object) -> object:
        raise LLMBudgetExceeded("cap")

    monkeypatch.setattr(cc, "build_scorecard", _raise)
    rc = run_calibration_scorecard.main(["--repo-root", str(tmp_path), "--period", "2026-06"])
    assert rc == 2
