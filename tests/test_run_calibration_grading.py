"""Tests for execution/run_calibration_grading.py — the calibration-grader orchestrator.

It runs a provider-free capture-retention sweep, then eleven grading
rungs (predictions -> decisions -> bear_cases -> seven eval-audit rungs ->
behavior-distill, LAST) as subprocesses. Its load-bearing contract mirrors
run_morning_pipeline:
attempt every non-skipped rung even when an earlier one fails or times out, and
report the count of failed rungs as the exit code.

``subprocess.run`` is monkeypatched throughout — no real processes are spawned.
Tests assert structural properties (call order, exit codes, summary statuses),
never log wording.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import cast

import pytest

from execution import run_calibration_grading

PREDICTIONS = "grade_predictions.py"
DECISIONS = "grade_decisions.py"
BEAR = "grade_bear_cases.py"
EVALS = "run_llm_evals.py"
CAPTURE_RETENTION = "prune_llm_capture.py"
BEHAVIOR_DISTILL = "run_behavior_distill.py"

# The full run order: outcome graders, then the seven eval-audit rungs, then
# the tenet-2 Phase 4 behavioral-rules distiller (always last).
ALL_SCRIPTS = [
    CAPTURE_RETENTION,
    PREDICTIONS,
    DECISIONS,
    BEAR,
    EVALS,
    EVALS,
    EVALS,
    EVALS,
    EVALS,
    EVALS,
    EVALS,
    BEHAVIOR_DISTILL,
]


class _FakeCompleted:
    def __init__(self, returncode: int, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class _RecordingRun:
    """Records subprocess.run calls; returns a per-script-configurable result."""

    def __init__(
        self,
        *,
        returncodes: dict[str, int] | None = None,
        timeout_scripts: set[str] | None = None,
    ) -> None:
        self.calls: list[list[str]] = []
        self._returncodes = returncodes or {}
        self._timeout_scripts = timeout_scripts or set()

    def __call__(self, argv: list[str], **kwargs: object) -> _FakeCompleted:
        self.calls.append(list(argv))
        script = _script_of(argv)
        if script in self._timeout_scripts:
            timeout = kwargs.get("timeout")
            raise subprocess.TimeoutExpired(
                cmd=argv, timeout=timeout if isinstance(timeout, float | int) else 0
            )
        return _FakeCompleted(returncode=self._returncodes.get(script or "", 0))

    @property
    def scripts(self) -> list[str]:
        return [s for c in self.calls if (s := _script_of(c)) is not None]


def _script_of(argv: list[str]) -> str | None:
    for tok in argv:
        if tok.endswith(".py"):
            return Path(tok).name
    return None


def _parse_summary(out: str) -> dict[str, object]:
    start = out.rfind("{")
    end = out.rfind("}")
    assert start != -1 and end != -1 and end > start, f"no summary JSON in:\n{out}"
    parsed = json.loads(out[start : end + 1])
    assert isinstance(parsed, dict)
    return cast("dict[str, object]", parsed)


def _install_fake(monkeypatch: pytest.MonkeyPatch, fake: _RecordingRun) -> None:
    monkeypatch.setattr("execution.run_calibration_grading.subprocess.run", fake)


def test_all_graders_run_in_order(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = _RecordingRun()
    _install_fake(monkeypatch, fake)

    rc = run_calibration_grading.main([])
    assert rc == 0
    assert fake.scripts == ALL_SCRIPTS

    summary = _parse_summary(capsys.readouterr().out)
    assert summary["predictions"] == "ok"
    assert summary["capture_retention"] == "ok"
    assert summary["decisions"] == "ok"
    assert summary["bear_cases"] == "ok"
    assert summary["behavior_distill"] == "ok"


def test_bear_grader_runs_over_whole_portfolio(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Regression: the bear_cases rung must pass --all-portfolio.

    grade_bear_cases.py has a required mutually-exclusive scope group
    (--ticker | --all-portfolio); with no scope it exits 2 (argparse) before
    grading anything, which silently failed this rung on every weekly run.
    """
    fake = _RecordingRun()
    _install_fake(monkeypatch, fake)

    rc = run_calibration_grading.main([])
    assert rc == 0

    bear_calls = [c for c in fake.calls if _script_of(c) == BEAR]
    assert len(bear_calls) == 1
    assert "--all-portfolio" in bear_calls[0]


def test_one_grader_failure_does_not_stop_the_rest(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A failed predictions grader must not stop decisions + bear_cases — exit
    code counts exactly the one failure."""
    fake = _RecordingRun(returncodes={PREDICTIONS: 1})
    _install_fake(monkeypatch, fake)

    rc = run_calibration_grading.main([])
    assert rc == 1
    assert fake.scripts == ALL_SCRIPTS

    summary = _parse_summary(capsys.readouterr().out)
    assert summary["predictions"] == "failed"
    assert summary["decisions"] == "ok"
    assert summary["bear_cases"] == "ok"


def test_timeout_is_caught_and_counted(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A hung bear grader is caught (not propagated), counted as failed, and the
    others still ran."""
    fake = _RecordingRun(timeout_scripts={BEAR})
    _install_fake(monkeypatch, fake)

    rc = run_calibration_grading.main([])
    assert rc == 1
    assert fake.scripts == ALL_SCRIPTS

    summary = _parse_summary(capsys.readouterr().out)
    assert summary["bear_cases"] == "failed"


def test_skip_omits_a_grader(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = _RecordingRun()
    _install_fake(monkeypatch, fake)

    rc = run_calibration_grading.main(["--skip", "bear_cases"])
    assert rc == 0
    assert fake.scripts == [
        CAPTURE_RETENTION,
        PREDICTIONS,
        DECISIONS,
        EVALS,
        EVALS,
        EVALS,
        EVALS,
        EVALS,
        EVALS,
        EVALS,
        BEHAVIOR_DISTILL,
    ]
    assert BEAR not in fake.scripts

    summary = _parse_summary(capsys.readouterr().out)
    assert summary["bear_cases"] == "skipped"


def test_all_fail_exit_code_counts_all(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = _RecordingRun(returncodes={PREDICTIONS: 1, DECISIONS: 2, BEAR: 1})
    _install_fake(monkeypatch, fake)

    rc = run_calibration_grading.main([])
    assert rc == 3


def test_eval_audit_rungs_scope_to_fresh_artifacts(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The seven eval-audit rungs (llm_evals_plan PR 3 + close_the_loops L3)
    call run_llm_evals.py one purpose each, scoped by --since-days so the weekly
    cron only judges the week's fresh artifacts; their statuses land in the
    summary JSON."""
    fake = _RecordingRun()
    _install_fake(monkeypatch, fake)

    rc = run_calibration_grading.main([])
    assert rc == 0
    eval_calls = [c for c in fake.calls if _script_of(c) == EVALS]
    purposes = []
    for argv in eval_calls:
        assert "--since-days" in argv
        purposes.append(argv[argv.index("--purpose") + 1])
    assert purposes == [
        "bear_case",
        "transcript_summary",
        "advisor_next_dollar",
        "ask_advisory_answer",
        "calibration_coach",
        "material_news_classification",
        "earnings_tone_diff",
    ]

    summary = _parse_summary(capsys.readouterr().out)
    assert summary["eval_bear_case"] == "ok"
    assert summary["eval_transcript_summary"] == "ok"
    assert summary["eval_advisor_next_dollar"] == "ok"
    assert summary["eval_ask_advisory_answer"] == "ok"
    assert summary["eval_calibration_coach"] == "ok"
    assert summary["eval_material_news_capture"] == "ok"
    assert summary["eval_earnings_tone_capture"] == "ok"


def test_behavior_distill_runs_last_and_a_hard_failure_is_counted(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The behavioral-rules distiller (tenet-2 Phase 4) must run AFTER every
    grading rung, and a hard failure (e.g. a propagated LLMBudgetExceeded ->
    non-zero exit) is caught and counted, never crashing the orchestrator —
    mirroring how bear_cases/predictions failures are handled."""
    fake = _RecordingRun(returncodes={BEHAVIOR_DISTILL: 1})
    _install_fake(monkeypatch, fake)

    rc = run_calibration_grading.main([])
    assert rc == 1
    assert fake.scripts == ALL_SCRIPTS
    assert fake.scripts[-1] == BEHAVIOR_DISTILL

    summary = _parse_summary(capsys.readouterr().out)
    assert summary["behavior_distill"] == "failed"


def test_skip_behavior_distill(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = _RecordingRun()
    _install_fake(monkeypatch, fake)

    rc = run_calibration_grading.main(["--skip", "behavior_distill"])
    assert rc == 0
    assert BEHAVIOR_DISTILL not in fake.scripts

    summary = _parse_summary(capsys.readouterr().out)
    assert summary["behavior_distill"] == "skipped"


def test_skip_an_eval_rung(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = _RecordingRun()
    _install_fake(monkeypatch, fake)

    rc = run_calibration_grading.main(["--skip", "eval_transcript_summary"])
    assert rc == 0
    eval_purposes = [c[c.index("--purpose") + 1] for c in fake.calls if _script_of(c) == EVALS]
    assert eval_purposes == [
        "bear_case",
        "advisor_next_dollar",
        "ask_advisory_answer",
        "calibration_coach",
        "material_news_classification",
        "earnings_tone_diff",
    ]
    summary = _parse_summary(capsys.readouterr().out)
    assert summary["eval_transcript_summary"] == "skipped"
