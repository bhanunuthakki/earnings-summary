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
        isolation = "--diff-filter=ACMR" not in command
        output = "" if isolation else "\0".join(paths) + "\0"
        return subprocess.CompletedProcess(command, returncode, output, "")

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
    with pytest.raises(ChangedSuppressionError, match="isolation check failed"):
        changed_retained_python_files(tmp_path, "origin/main", runner=_runner([], returncode=128))


def test_scanner_fails_closed_on_invalid_python(tmp_path: Path) -> None:
    path = tmp_path / "broken.py"
    path.write_text("'''unterminated\n", encoding="utf-8")

    with pytest.raises(ChangedSuppressionError, match="unable to scan"):
        suppression_findings(tmp_path, ["broken.py"])


def test_worktree_selection_includes_local_work_and_omits_deleted_paths(tmp_path: Path) -> None:
    def git(*args: str) -> str:
        result = subprocess.run(
            ["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid", *args],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout.strip()

    git("init")
    for name in ("committed.py", "staged.py", "unstaged.py", "deleted.py", "renamed.py"):
        (tmp_path / name).write_text("VALUE = 1\n")
    git("add", ".")
    git("commit", "-m", "baseline")
    base = git("rev-parse", "HEAD")
    (tmp_path / "committed.py").write_text("VALUE = 2\n")
    git("add", "committed.py")
    git("commit", "-m", "committed change")
    (tmp_path / "staged.py").write_text("VALUE = 3\n")
    git("add", "staged.py")
    (tmp_path / "unstaged.py").write_text("VALUE = 4\n")
    (tmp_path / "deleted.py").unlink()
    git("mv", "renamed.py", "renamed with space.py")
    (tmp_path / "untracked.py").write_text("VALUE = 5\n")
    (tmp_path / "scratch").mkdir()
    (tmp_path / "scratch" / "ignored.py").write_text("VALUE = 6\n")

    assert changed_retained_python_files(tmp_path, base, mode="worktree") == [
        "committed.py",
        "renamed with space.py",
        "staged.py",
        "unstaged.py",
        "untracked.py",
    ]
    with pytest.raises(ChangedSuppressionError, match=r"dirty|worktree|uncommitted"):
        changed_retained_python_files(tmp_path, base, mode="committed")


@pytest.mark.parametrize("contamination", ["selected", "dependency", "untracked", "staged"])
def test_committed_selection_rejects_dirty_python_before_checks(
    tmp_path: Path,
    contamination: str,
) -> None:
    """A working-tree repair must never hide a defect in the pushed HEAD."""

    def git(*args: str) -> str:
        result = subprocess.run(
            ["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid", *args],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout.strip()

    git("init")
    (tmp_path / "candidate.py").write_text("VALUE = 1\n")
    (tmp_path / "dependency.py").write_text("VALUE = 1\n")
    git("add", ".")
    git("commit", "-m", "baseline")
    base = git("rev-parse", "HEAD")
    (tmp_path / "candidate.py").write_text("VALUE = 2\n")
    git("add", "candidate.py")
    git("commit", "-m", "candidate")
    assert changed_retained_python_files(tmp_path, base, mode="committed") == ["candidate.py"]

    names = {
        "selected": "candidate.py",
        "dependency": "dependency.py",
        "untracked": "new.py",
        "staged": "candidate.py",
    }
    changed = names[contamination]
    (tmp_path / changed).write_text("VALUE = 3\n")
    if contamination == "staged":
        git("add", changed)
    with pytest.raises(ChangedSuppressionError, match=r"dirty|worktree|uncommitted"):
        changed_retained_python_files(tmp_path, base, mode="committed")
