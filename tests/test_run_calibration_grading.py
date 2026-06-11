"""Tests for execution/run_calibration_grading.py — the calibration-grader orchestrator.

It runs six rungs (predictions -> decisions -> bear_cases -> three eval-audit
rungs, llm_evals_plan PR 3) as subprocesses. Its load-bearing contract mirrors
run_morning_pipeline: attempt every non-skipped rung even when an earlier one
fails or times out, and report the count of failed rungs as the exit code.

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

# The full run order: outcome graders, then the three eval-audit rungs.
ALL_SCRIPTS = [PREDICTIONS, DECISIONS, BEAR, EVALS, EVALS, EVALS]


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
    assert summary["decisions"] == "ok"
    assert summary["bear_cases"] == "ok"


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
    assert fake.scripts == [PREDICTIONS, DECISIONS, EVALS, EVALS, EVALS]
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
    """The three eval-audit rungs (llm_evals_plan PR 3) call run_llm_evals.py
    one purpose each, scoped by --since-days so the weekly cron only judges
    the week's fresh artifacts; their statuses land in the summary JSON."""
    fake = _RecordingRun()
    _install_fake(monkeypatch, fake)

    rc = run_calibration_grading.main([])
    assert rc == 0
    eval_calls = [c for c in fake.calls if _script_of(c) == EVALS]
    purposes = []
    for argv in eval_calls:
        assert "--since-days" in argv
        purposes.append(argv[argv.index("--purpose") + 1])
    assert purposes == ["bear_case", "transcript_summary", "advisor_next_dollar"]

    summary = _parse_summary(capsys.readouterr().out)
    assert summary["eval_bear_case"] == "ok"
    assert summary["eval_transcript_summary"] == "ok"
    assert summary["eval_advisor_next_dollar"] == "ok"


def test_skip_an_eval_rung(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = _RecordingRun()
    _install_fake(monkeypatch, fake)

    rc = run_calibration_grading.main(["--skip", "eval_transcript_summary"])
    assert rc == 0
    eval_purposes = [c[c.index("--purpose") + 1] for c in fake.calls if _script_of(c) == EVALS]
    assert eval_purposes == ["bear_case", "advisor_next_dollar"]
    summary = _parse_summary(capsys.readouterr().out)
    assert summary["eval_transcript_summary"] == "skipped"
