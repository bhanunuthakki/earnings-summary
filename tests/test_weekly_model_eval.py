# pyright: reportPrivateUsage=false
"""Tests for the weekly model-eval orchestrator's pure logic (rotation).

The harvest/sweep/apply steps are subprocess + DB orchestration (exercised by
their own modules' tests); here we lock in the deterministic rotation that keeps
weekly harvest cost bounded while covering the universe over time.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "execution"))

from run_weekly_model_eval import _persist_sweep_receipt, _rotating_sample, main  # noqa: E402

from llm.model_eval import CandidateVerdict  # noqa: E402


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


def _verdict(recommendation: str) -> CandidateVerdict:
    return CandidateVerdict(
        purpose="bear_case",
        incumbent="claude-sonnet-5",
        candidate="gemini-3-flash-preview",
        n=4,
        candidate_wins=0,
        incumbent_wins=4,
        ties=0,
        parity_rate=0.0,
        judge_agreement=1.0,
        recommendation=recommendation,
        reason="test",
    )


def test_weekly_sweep_receipt_fails_closed_when_nothing_was_graded(tmp_path: Path) -> None:
    started = datetime.now(UTC)
    receipt = _persist_sweep_receipt(
        tmp_path,
        [],
        run_id="weekly-empty",
        started_at=started,
    )

    assert receipt.status == "alert"
    assert receipt.alerts == ("no_graded_verdict",)
    assert (tmp_path / "data" / "model_eval_runs" / "weekly-empty.json").is_file()


def test_weekly_sweep_receipt_rejects_infrastructure_errors(tmp_path: Path) -> None:
    receipt = _persist_sweep_receipt(
        tmp_path,
        [_verdict("CANDIDATE_ERRORED")],
        run_id="weekly-error",
        started_at=datetime.now(UTC),
    )

    assert receipt.status == "alert"
    assert receipt.alerts == ("no_graded_verdict", "eval_errors_present")


def test_weekly_sweep_receipt_accepts_a_graded_verdict(tmp_path: Path) -> None:
    receipt = _persist_sweep_receipt(
        tmp_path,
        [_verdict("KEEP_INCUMBENT")],
        run_id="weekly-graded",
        started_at=datetime.now(UTC),
    )

    assert receipt.status == "passed"
    assert receipt.graded == 1


def test_weekly_main_exits_nonzero_when_capture_produces_no_verdicts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_weekly_model_eval.py",
            "--repo-root",
            str(tmp_path),
            "--skip-harvest",
            "--skip-nominate",
            "--skip-apply",
            "--prompt-cycles",
            "0",
        ],
    )

    assert main() == 2
    receipt = tmp_path / "data" / "model_eval_runs" / "latest.json"
    assert receipt.is_file()
    assert '"status": "alert"' in receipt.read_text(encoding="utf-8")
