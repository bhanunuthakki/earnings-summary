from __future__ import annotations

import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest

from quality.changed_suppressions import (
    ChangedSuppressionError,
    changed_retained_python_files,
    suppression_findings,
)


def _runner(paths: Sequence[str], *, returncode: int = 0):
    def run(command: Sequence[str], root: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, returncode, "\0".join(paths) + "\0", "")

    return run


def test_changed_paths_exclude_non_retained_partitions_and_explicit_exceptions(
    tmp_path: Path,
) -> None:
    paths = [
        "src/app.py",
        "alembic/versions/0001.py",
        "alembic/versions_archived/0000.py",
        "scratch/retire.py",
        "generated.py",
    ]
    for path in paths:
        target = tmp_path / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("VALUE = 1\n", encoding="utf-8")

    assert changed_retained_python_files(
        tmp_path,
        "origin/main",
        exception_paths=["generated.py"],
        runner=_runner(paths),
    ) == ["src/app.py"]


def test_scanner_finds_supported_directives_but_not_strings(tmp_path: Path) -> None:
    path = tmp_path / "src/app.py"
    path.parent.mkdir(parents=True)
    path.write_text(
        'TEXT = "# type: ignore and # noqa"\n'
        "value = call()  # type: ignore[arg-type]\n"
        "# pyright: reportPrivateUsage=false\n"
        "other = call()  # noqa: F401\n",
        encoding="utf-8",
    )

    findings = suppression_findings(tmp_path, ["src/app.py"])

    assert [(item.line, item.directive) for item in findings] == [
        (2, "type-ignore"),
        (3, "pyright-directive"),
        (4, "ruff-noqa"),
    ]


def test_changed_path_discovery_fails_closed_on_git_error(tmp_path: Path) -> None:
    with pytest.raises(ChangedSuppressionError, match="git diff failed"):
        changed_retained_python_files(tmp_path, "origin/main", runner=_runner([], returncode=128))


def test_scanner_fails_closed_on_invalid_python(tmp_path: Path) -> None:
    path = tmp_path / "broken.py"
    path.write_text("'''unterminated\n", encoding="utf-8")

    with pytest.raises(ChangedSuppressionError, match="unable to scan"):
        suppression_findings(tmp_path, ["broken.py"])
