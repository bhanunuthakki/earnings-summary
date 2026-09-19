from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from quality.check_changed import run_checks
from quality.git_env import clean_local_git_env


def test_checks_use_project_interpreter_and_preserve_filename_arguments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[str]] = []

    def run(
        command: list[str], *, cwd: Path, check: bool, env: dict[str, str]
    ) -> subprocess.CompletedProcess[str]:
        assert cwd == tmp_path
        assert check is False
        assert not any(key.startswith("GIT_") for key in env)
        calls.append(command)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(subprocess, "run", run)
    assert (
        run_checks(
            tmp_path, ["src/a file.py", "tests/test_a file.py"], ["format", "types", "tests"]
        )
        == 0
    )
    assert calls == [
        [
            sys.executable,
            "-m",
            "ruff",
            "format",
            "--check",
            "./src/a file.py",
            "./tests/test_a file.py",
        ],
        [
            sys.executable,
            "-m",
            "pyright",
            "--pythonpath",
            sys.executable,
            "./src/a file.py",
            "./tests/test_a file.py",
        ],
        [sys.executable, "-m", "pytest", "-q", "./tests/test_a file.py"],
    ]


def test_tool_failure_stops_later_checks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    def run(
        command: list[str], *, cwd: Path, check: bool, env: dict[str, str]
    ) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        return subprocess.CompletedProcess(command, 2)

    monkeypatch.setattr(subprocess, "run", run)
    assert run_checks(tmp_path, ["src/broken.py"], ["format", "lint"]) == 2
    assert len(calls) == 1


def test_full_suite_runs_with_no_changed_python_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[str]] = []

    def run(
        command: list[str], *, cwd: Path, check: bool, env: dict[str, str]
    ) -> subprocess.CompletedProcess[str]:
        assert not any(key.startswith("GIT_") for key in env)
        calls.append(command)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setenv("GIT_INDEX_FILE", "outer-index")
    monkeypatch.setattr(subprocess, "run", run)
    assert run_checks(tmp_path, [], ["full-tests"]) == 0
    assert calls == [[sys.executable, "-m", "pytest", "-q"]]


def test_hook_context_cannot_redirect_fixture_git_commands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    outer = tmp_path / "outer"
    subprocess.run(["git", "init", str(outer)], check=True, env=clean_local_git_env())
    candidate = tmp_path / "candidate"
    tests = candidate / "tests"
    tests.mkdir(parents=True)
    (tests / "test_nested_git.py").write_text(
        "import subprocess\n"
        "def test_nested_repository():\n"
        "    subprocess.run(['git', 'init', '--bare', 'remote'], check=True)\n"
    )
    monkeypatch.setenv("GIT_DIR", str(outer / ".git"))
    monkeypatch.setenv("GIT_COMMON_DIR", str(outer / ".git"))
    assert run_checks(candidate, ["tests/test_nested_git.py"], ["tests"]) == 0
    result = subprocess.run(
        ["git", "config", "--get", "core.bare"],
        cwd=outer,
        env=clean_local_git_env(),
        text=True,
        capture_output=True,
        check=True,
    )
    assert result.stdout.strip() == "false"
