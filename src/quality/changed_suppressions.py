"""Reject inline static-analysis suppressions in changed retained Python files."""

from __future__ import annotations

import argparse
import io
import re
import subprocess
import sys
import tokenize
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal

from quality.git_env import clean_local_git_env

CommandRunner = Callable[[Sequence[str], Path], subprocess.CompletedProcess[str]]
_RETIRED_PREFIXES = (
    "alembic/versions/",
    "alembic/versions_archived/",
    "scratch/",
)
_DIRECTIVES = (
    ("type-ignore", re.compile(r"#\s*type\s*:\s*ignore\b", re.IGNORECASE)),
    ("pyright-directive", re.compile(r"^\s*#\s*pyright\s*:", re.IGNORECASE)),
    ("ruff-noqa", re.compile(r"#\s*noqa\b", re.IGNORECASE)),
)


class ChangedSuppressionError(RuntimeError):
    """The gate could not produce complete, trustworthy evidence."""


@dataclass(frozen=True)
class SuppressionFinding:
    path: str
    line: int
    directive: str


def _run(args: Sequence[str], root: Path) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            args,
            cwd=root,
            text=True,
            capture_output=True,
            check=False,
            env=clean_local_git_env(),
        )
    except (OSError, UnicodeError) as exc:
        raise ChangedSuppressionError("unable to inspect changed Python files") from exc


def changed_retained_python_files(
    repo_root: Path,
    base: str,
    *,
    mode: Literal["committed", "worktree"] = "committed",
    exception_paths: Sequence[str] = (),
    runner: CommandRunner = _run,
) -> list[str]:
    """Return changed Python paths governed by the retained-file ratchet."""
    if not base.strip() or base.startswith("-"):
        raise ChangedSuppressionError("base revision is invalid")
    root = repo_root.resolve()
    if mode == "worktree":
        ancestor = runner(["git", "merge-base", base, "HEAD"], root)
        if ancestor.returncode or not re.fullmatch(r"[0-9a-f]{40,64}", ancestor.stdout.strip()):
            raise ChangedSuppressionError("git merge-base failed")
        comparison = ancestor.stdout.strip()
    else:
        # The push candidate is HEAD. A dirty imported module or test fixture can
        # hide committed failures just as a dirty selected file can.
        for command in (
            ["git", "diff", "--name-only", "-z", "HEAD", "--", "*.py"],
            ["git", "ls-files", "--others", "--exclude-standard", "-z", "--", "*.py"],
        ):
            current = runner(command, root)
            if current.returncode:
                raise ChangedSuppressionError("committed candidate isolation check failed")
            if current.stdout.strip("\0\n"):
                raise ChangedSuppressionError(
                    "committed checks require a clean Python worktree; commit or isolate local changes"
                )
        comparison = f"{base}...HEAD"
    result = runner(
        ["git", "diff", "--name-only", "--diff-filter=ACMR", "-z", comparison, "--", "*.py"], root
    )
    if result.returncode:
        raise ChangedSuppressionError(f"git diff failed ({result.returncode})")
    exceptions = {path.replace("\\", "/") for path in exception_paths}
    paths = {path for path in result.stdout.split("\0") if path}
    if mode == "worktree":
        untracked = runner(
            ["git", "ls-files", "--others", "--exclude-standard", "-z", "--", "*.py"], root
        )
        if untracked.returncode:
            raise ChangedSuppressionError("git untracked-file discovery failed")
        paths.update(path for path in untracked.stdout.split("\0") if path)
    retained: list[str] = []
    for path in sorted(paths):
        candidate = PurePosixPath(path)
        if candidate.is_absolute() or ".." in candidate.parts or re.match(r"^[A-Za-z]:/", path):
            raise ChangedSuppressionError("changed Python path escapes the repository")
        if path in exceptions or path.startswith(_RETIRED_PREFIXES):
            continue
        target = root.joinpath(*candidate.parts)
        try:
            resolved = target.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise ChangedSuppressionError(f"changed Python file is missing: {path}") from exc
        if not resolved.is_relative_to(root) or not resolved.is_file():
            raise ChangedSuppressionError(f"changed Python file escapes the repository: {path}")
        retained.append(path)
    return retained


def suppression_findings(repo_root: Path, paths: Sequence[str]) -> list[SuppressionFinding]:
    """Find lexical suppression directives, ignoring string contents."""
    root = repo_root.resolve()
    findings: list[SuppressionFinding] = []
    for path in paths:
        target = root.joinpath(*PurePosixPath(path).parts)
        try:
            content = target.read_text(encoding="utf-8")
            tokens = tokenize.generate_tokens(io.StringIO(content).readline)
            for token in tokens:
                if token.type != tokenize.COMMENT:
                    continue
                for directive, pattern in _DIRECTIVES:
                    if pattern.search(token.string):
                        findings.append(
                            SuppressionFinding(path=path, line=token.start[0], directive=directive)
                        )
        except (
            OSError,
            UnicodeDecodeError,
            IndentationError,
            SyntaxError,
            tokenize.TokenError,
        ) as exc:
            raise ChangedSuppressionError(f"unable to scan changed Python file: {path}") from exc
    return findings


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--base", default="origin/main")
    parser.add_argument("--mode", choices=("committed", "worktree"), default="committed")
    parser.add_argument("--exception-path", action="append", default=[])
    args = parser.parse_args(argv)
    try:
        paths = changed_retained_python_files(
            args.repo_root, args.base, mode=args.mode, exception_paths=args.exception_path
        )
        findings = suppression_findings(args.repo_root, paths)
    except ChangedSuppressionError as exc:
        print(f"changed-suppression gate failed closed: {exc}", file=sys.stderr)
        return 2
    if findings:
        for finding in findings:
            print(
                f"{finding.path}:{finding.line}: prohibited {finding.directive} suppression",
                file=sys.stderr,
            )
        print(
            f"changed-suppression gate: {len(findings)} suppression(s) in "
            f"{len({finding.path for finding in findings})} retained file(s)",
            file=sys.stderr,
        )
        return 1
    print(f"changed-suppression gate: {len(paths)} retained changed file(s) clean")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
