from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from quality.check_changed import run_checks


def test_checks_use_project_interpreter_and_preserve_filename_arguments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[str]] = []

    def run(command: list[str], *, cwd: Path, check: bool) -> subprocess.CompletedProcess[str]:
        assert cwd == tmp_path
        assert check is False
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

    def run(command: list[str], *, cwd: Path, check: bool) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        return subprocess.CompletedProcess(command, 2)

    monkeypatch.setattr(subprocess, "run", run)
    assert run_checks(tmp_path, ["src/broken.py"], ["format", "lint"]) == 2
    assert len(calls) == 1
