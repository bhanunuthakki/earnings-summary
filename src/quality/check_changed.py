"""Run the same retained-file checks locally, in hooks, and in CI."""

from __future__ import annotations

import argparse
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

from quality.changed_suppressions import (
    ChangedSuppressionError,
    changed_retained_python_files,
    suppression_findings,
)
from quality.git_env import clean_local_git_env


def run_checks(root: Path, paths: Sequence[str], checks: Sequence[str]) -> int:
    """Use this interpreter and argument lists, preserving spaces in filenames."""
    for check in checks:
        if check == "suppressions":
            findings = suppression_findings(root, paths)
            for finding in findings:
                print(
                    f"{finding.path}:{finding.line}: prohibited {finding.directive}",
                    file=sys.stderr,
                )
            if findings:
                return 1
            continue
        selected = list(paths)
        if check == "tests":
            selected = [
                path
                for path in paths
                if Path(path).name.startswith("test_")
                and Path(path).parts[0] in {"tests", "instruction_tests"}
            ]
            print("Changed-test check only; full suite remains required for delivery.", flush=True)
        if check == "full-tests":
            selected = []
        if not selected and check != "full-tests":
            print(f"{check}: no selected files", flush=True)
            continue
        commands = {
            "format": ["ruff", "format", "--check"],
            "lint": ["ruff", "check"],
            "types": ["pyright", "--pythonpath", sys.executable],
            "tests": ["pytest", "-q"],
            "full-tests": ["pytest", "-q"],
        }
        command = [sys.executable, "-m", *commands[check], *(f"./{p}" for p in selected)]
        population = (
            "complete test suite" if check == "full-tests" else f"{len(selected)} retained files"
        )
        print(f"{check}: {population}", flush=True)
        result = subprocess.run(command, cwd=root, check=False, env=clean_local_git_env())
        if result.returncode:
            return result.returncode
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--base", default="origin/main")
    parser.add_argument("--mode", choices=("committed", "worktree"), default="worktree")
    parser.add_argument(
        "--check",
        choices=("format", "lint", "types", "suppressions", "tests", "full-tests"),
        action="append",
        dest="checks",
    )
    args = parser.parse_args(argv)
    try:
        root = args.repo_root.resolve()
        paths = changed_retained_python_files(root, args.base, mode=args.mode)
        return run_checks(root, paths, args.checks or ["format", "lint", "types", "suppressions"])
    except (ChangedSuppressionError, OSError) as exc:
        print(f"changed-file gate failed closed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
