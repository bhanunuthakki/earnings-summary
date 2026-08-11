"""Contract tests for the single-owner weekly synthesis orchestrator."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from execution import run_weekly_synthesis as weekly


class _Completed:
    def __init__(self, returncode: int) -> None:
        self.returncode = returncode
        self.stdout = ""
        self.stderr = ""


def _install_roster(monkeypatch: pytest.MonkeyPatch, tickers: list[str]) -> None:
    def resolve(root: Path) -> Path:
        return (root / "data" / "portfolio.db").resolve()

    def roster(_root: Path, _db_path: Path) -> list[str]:
        return tickers

    monkeypatch.setattr(weekly, "_resolve_db_identity", resolve)
    monkeypatch.setattr(weekly, "_active_portfolio_tickers", roster)


def test_weekly_synthesis_uses_dynamic_roster_and_fails_fast(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _install_roster(monkeypatch, ["META", "NU"])
    calls: list[list[str]] = []

    def fake_run(argv: list[str], **_kwargs: object) -> _Completed:
        calls.append(argv)
        return _Completed(23 if "run_lens.py" in " ".join(argv) else 0)

    monkeypatch.setattr(weekly.subprocess, "run", fake_run)

    assert weekly.run(tmp_path) == 23
    assert len(calls) == 2
    assert calls[1][-3:] == ["--ticker", "META", "--all"]
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["status"] == "failed"
    assert receipt["failed_stage"] == "ticker_lenses:META"
    assert receipt["portfolio_tickers"] == ["META", "NU"]


def test_weekly_synthesis_resumes_checkpoint_then_clears_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _install_roster(monkeypatch, ["META"])
    attempt = 0

    def first_attempt(argv: list[str], **_kwargs: object) -> _Completed:
        nonlocal attempt
        attempt += 1
        return _Completed(0 if attempt == 1 else 7)

    monkeypatch.setattr(weekly.subprocess, "run", first_attempt)
    assert weekly.run(tmp_path) == 7
    capsys.readouterr()
    checkpoint = tmp_path / ".tmp" / "weekly_synthesis" / "state.json"
    assert checkpoint.exists()
    assert json.loads(checkpoint.read_text(encoding="utf-8"))["completed_stages"] == [
        "refresh_dirty_artifacts"
    ]

    resumed_calls: list[list[str]] = []

    def resumed(argv: list[str], **_kwargs: object) -> _Completed:
        resumed_calls.append(argv)
        return _Completed(0)

    monkeypatch.setattr(weekly.subprocess, "run", resumed)
    assert weekly.run(tmp_path) == 0
    assert len(resumed_calls) == 3
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["status"] == "ok"
    assert receipt["resumed_stages"] == ["refresh_dirty_artifacts"]
    assert not checkpoint.exists()


def test_roster_change_invalidates_completed_ticker_stage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _install_roster(monkeypatch, ["META"])
    returns = iter([0, 0, 9])

    def staged(*_args: object, **_kwargs: object) -> _Completed:
        return _Completed(next(returns))

    monkeypatch.setattr(weekly.subprocess, "run", staged)
    assert weekly.run(tmp_path) == 9
    capsys.readouterr()

    _install_roster(monkeypatch, ["META", "NU"])
    calls: list[list[str]] = []

    def record(argv: list[str], **_kwargs: object) -> _Completed:
        calls.append(argv)
        return _Completed(0)

    monkeypatch.setattr(weekly.subprocess, "run", record)
    assert weekly.run(tmp_path) == 0
    assert len(calls) == 5


def test_empty_portfolio_fails_before_spawning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _install_roster(monkeypatch, [])

    def must_not_spawn(*_args: object, **_kwargs: object) -> _Completed:
        pytest.fail("must not spawn with an empty roster")

    monkeypatch.setattr(weekly.subprocess, "run", must_not_spawn)

    assert weekly.run(tmp_path) == 2
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["status"] == "failed"
    assert receipt["detail"] == "active portfolio roster is empty"


def test_spawn_error_is_terminal_and_checkpoint_is_preserved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _install_roster(monkeypatch, ["META"])

    def fail_spawn(*_args: object, **_kwargs: object) -> _Completed:
        raise OSError("spawn failed")

    monkeypatch.setattr(weekly.subprocess, "run", fail_spawn)

    assert weekly.run(tmp_path) == 1
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["status"] == "failed"
    assert receipt["failed_stage"] == "refresh_dirty_artifacts"
    assert "spawn failed" in receipt["detail"]


def test_ticker_lens_stage_never_duplicates_portfolio_synthesis(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _install_roster(monkeypatch, ["META", "NU"])
    calls: list[list[str]] = []

    def record(argv: list[str], **_kwargs: object) -> _Completed:
        calls.append(argv)
        return _Completed(0)

    monkeypatch.setattr(weekly.subprocess, "run", record)

    assert weekly.run(tmp_path) == 0
    capsys.readouterr()
    lens_calls = [argv for argv in calls if "run_lens.py" in " ".join(argv)]
    ticker_calls = [argv for argv in lens_calls if "--ticker" in argv]
    cross_calls = [argv for argv in lens_calls if "cross_portfolio_synthesis" in argv]
    assert [argv[argv.index("--ticker") + 1] for argv in ticker_calls] == ["META", "NU"]
    assert len(cross_calls) == 1
    assert all("--tickers" not in argv for argv in lens_calls)
    expected_root = str(tmp_path.resolve())
    assert all(argv[argv.index("--repo-root") + 1] == expected_root for argv in calls)


def test_db_override_cannot_launch_repo_root_stages_against_another_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    database_a = (tmp_path / "external" / "portfolio.db").resolve()
    database_b = (tmp_path / "data" / "portfolio.db").resolve()
    monkeypatch.setenv("EARNINGS_SUMMARY_DB_PATH", str(database_a))

    def must_not_load_roster(*_args: object, **_kwargs: object) -> list[str]:
        pytest.fail("split-brain DB identity must fail before roster loading")

    def must_not_spawn(*_args: object, **_kwargs: object) -> _Completed:
        pytest.fail("split-brain DB identity must fail before stage launch")

    monkeypatch.setattr(weekly, "_active_portfolio_tickers", must_not_load_roster)
    monkeypatch.setattr(weekly.subprocess, "run", must_not_spawn)

    assert weekly.run(tmp_path) == 2
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["status"] == "failed"
    assert receipt["db_path"] == str(database_a)
    assert str(database_b) in receipt["detail"]
    assert "cannot safely thread EARNINGS_SUMMARY_DB_PATH" in receipt["detail"]
