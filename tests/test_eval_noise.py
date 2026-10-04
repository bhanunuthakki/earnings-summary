"""Eval noise and headroom reporting: confidence intervals, the saturation
flag, and ``--repeats`` noise measurement (evals.harness + the runner).

No LLM calls — summaries are built directly and the runner's per-purpose
dispatch is replaced with a scripted fake.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

import pytest

from evals.harness import (
    JUDGE_INFRA_STAGE,
    CaseResult,
    EvalRunSummary,
    combine_repeats,
)
from execution import run_llm_evals


def _summary(outcomes: Sequence[bool | None], *, run_id: str = "run") -> EvalRunSummary:
    """``None`` marks a judge-infra case (not measured)."""
    cases = [
        CaseResult(
            case_id=f"c{i}",
            question="q",
            passed=bool(ok),
            score=None if ok is None else (1.0 if ok else 0.0),
            failure_stage=JUDGE_INFRA_STAGE if ok is None else None,
        )
        for i, ok in enumerate(outcomes)
    ]
    return EvalRunSummary(
        run_id=run_id,
        purpose="intake_classifier",
        mode="golden",
        prompt_version="v1",
        model="m",
        judge_model=None,
        golden_set_sha="sha",
        started_at=datetime(2026, 10, 1, tzinfo=UTC).replace(tzinfo=None),
        cases=cases,
    )


def test_interval_excludes_infra_cases_and_is_honestly_wide() -> None:
    s = _summary([True, True, True, None])
    assert s.n_measured == 3
    assert s.pass_rate == 1.0
    ci = s.pass_rate_ci95
    assert ci is not None
    # 3/3 is not "100% ± 0": the lower bound sits far below 1.
    assert ci[0] < 0.5 and ci[1] == 1.0


def test_small_set_cannot_be_called_saturated_without_measurement() -> None:
    assert _summary([None, None]).pass_rate is None
    assert _summary([None, None]).saturated is False
    assert _summary([True] * 19 + [False]).saturated is True  # 95%
    assert _summary([True] * 9 + [False]).saturated is False  # 90%


def test_score_interval_needs_two_scored_cases() -> None:
    assert _summary([True]).avg_score_ci95 is None
    ci = _summary([True, False, True, False]).avg_score_ci95
    assert ci is not None and ci[0] == 0.0 and 0.5 < ci[1] <= 1.0


def test_json_report_carries_intervals_and_saturation() -> None:
    report = _summary([True, False]).to_json_dict()
    assert report["pass_rate"] == 0.5
    assert isinstance(report["pass_rate_ci95"], list)
    assert report["saturated"] is False


def test_combine_repeats_reports_flips_and_spread() -> None:
    runs = [
        _summary([True, True, False], run_id="a"),
        _summary([True, False, False], run_id="b"),
        _summary([True, True, None], run_id="c"),
    ]
    combined, noise = combine_repeats(runs)
    assert combined.run_id == "a"
    assert combined.n_cases == 9
    assert {c.case_id for c in combined.cases} >= {"c0@r1", "c0@r2", "c0@r3"}
    # c1 passed then failed; c2 failed twice and was unmeasured once (no flip).
    assert noise["flip_cases"] == ["c1"]
    assert noise["repeats"] == 3
    spread = noise["avg_score_spread"]
    assert isinstance(spread, float) and spread == pytest.approx(1.0 - 1 / 3)
    assert combined.notes is not None and "flips=1" in combined.notes


def test_combine_repeats_rejects_empty() -> None:
    with pytest.raises(ValueError):
        combine_repeats([])


def _no_sync(_root: Path) -> None:
    return None


def _run_main(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    argv: list[str],
    runs: list[EvalRunSummary],
) -> int:
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "portfolio.db").write_bytes(b"")
    queue = list(runs)

    def next_run(*_a: object, **_k: object) -> EvalRunSummary:
        return queue.pop(0)

    monkeypatch.setattr(run_llm_evals, "_sync_db_to_repo", _no_sync)
    monkeypatch.setattr(run_llm_evals, "_run_purpose", next_run)
    monkeypatch.setattr(
        "sys.argv",
        ["run_llm_evals.py", "--repo-root", str(tmp_path), "--no-persist", *argv],
    )
    return run_llm_evals.main()


def test_runner_repeats_prints_noise(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = _run_main(
        monkeypatch,
        tmp_path,
        ["--purpose", "intake_classifier", "--repeats", "2"],
        [_summary([True, True]), _summary([True, False])],
    )
    assert rc == 0
    report = json.loads(capsys.readouterr().out)
    assert report["n_cases"] == 4
    assert report["noise"]["flip_cases"] == ["c1"]


def test_runner_warns_on_saturated_set(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = _run_main(
        monkeypatch, tmp_path, ["--purpose", "intake_classifier"], [_summary([True] * 20)]
    )
    assert rc == 0
    captured = capsys.readouterr()
    assert "noise" not in json.loads(captured.out)
    assert "SATURATED" in captured.err


def test_runner_rejects_zero_repeats(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = _run_main(monkeypatch, tmp_path, ["--purpose", "intake_classifier", "--repeats", "0"], [])
    assert rc == 1
    assert "--repeats" in capsys.readouterr().err


def test_runner_usage_errors_still_exit_1(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Moving dispatch into a function must not change the CLI contract."""
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "portfolio.db").write_bytes(b"")
    monkeypatch.setattr(run_llm_evals, "_sync_db_to_repo", _no_sync)
    monkeypatch.setattr(
        "sys.argv",
        [
            "run_llm_evals.py",
            "--repo-root",
            str(tmp_path),
            "--purpose",
            "bear_case",
            "--no-judge",
        ],
    )
    assert run_llm_evals.main() == 1
    assert "mode-A flag" in capsys.readouterr().err
