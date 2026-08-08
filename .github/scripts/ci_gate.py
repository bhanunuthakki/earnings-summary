#!/usr/bin/env python3
"""Classify changed paths and verify the single required CI gate."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import cast

CODE_PREFIXES = (
    "src/",
    "execution/",
    "tests/",
    "alembic/",
    "cron/",
    "scripts/",
    ".githooks/",
    "config/",
    "templates/",
    ".github/workflows/",
    ".github/scripts/",
)
CODE_ROOT_FILES = {
    ".bandit-baseline.json",
    ".pre-commit-config.yaml",
    "Makefile",
    "alembic.ini",
    "pyproject.toml",
    "requirements.lock",
    "requirements.txt",
}
PYTHON_ROOT_FILES = {"pyproject.toml", "requirements.lock"}
DOCUMENTATION_SUFFIXES = {".md", ".rst"}
CONDITIONAL_JOBS = {
    "tests": "code",
    "quality": "python",
    "typecheck": "python",
    "security": "code",
}
TERMINAL_SUCCESS_RESULTS = {"success", "skipped"}


def _normalize(path: str) -> str:
    return path.replace("\\", "/").removeprefix("./")


def classify_paths(paths: Iterable[str]) -> dict[str, bool]:
    """Return the expensive CI groups required by *paths*."""

    code = False
    python = False
    for raw_path in paths:
        path = _normalize(raw_path)
        if not path:
            continue
        known_code_path = path in CODE_ROOT_FILES or path.startswith(CODE_PREFIXES)
        is_code = known_code_path or Path(path).suffix.lower() not in DOCUMENTATION_SUFFIXES
        is_python = path in PYTHON_ROOT_FILES or path.endswith(".py")
        code = code or is_code
        python = python or is_python
    return {"code": code, "python": python}


def select_test_files(
    files: Sequence[str],
    *,
    source_shard: int,
    source_shards: int,
    split_count: int,
    split_part: int,
) -> list[str]:
    """Select one stable, disjoint CI partition from sorted test files."""
    if source_shards < 1 or not 1 <= source_shard <= source_shards:
        raise ValueError("source shard is outside configured shard count")
    if split_count < 1 or not 0 <= split_part < split_count:
        raise ValueError("split part is outside configured split count")
    selected: list[str] = []
    source_position = 0
    for index, path in enumerate(files):
        if index % source_shards != source_shard - 1:
            continue
        if source_position % split_count == split_part:
            selected.append(path)
        source_position += 1
    return selected


def gate_failures(*, code: bool, python: bool, results: Mapping[str, str]) -> list[str]:
    """Explain every terminal result that makes the aggregate gate unsafe."""

    failures: list[str] = []
    changes_result = results.get("changes", "")
    if changes_result != "success":
        failures.append(f"changes must succeed; got {changes_result or 'missing result'}")

    for job_name in CONDITIONAL_JOBS:
        result = results.get(job_name, "")
        if result not in TERMINAL_SUCCESS_RESULTS:
            failures.append(f"{job_name} finished with {result or 'missing result'}")

    required_groups = {"code": code, "python": python}
    for job_name, group in CONDITIONAL_JOBS.items():
        result = results.get(job_name, "")
        if required_groups[group] and result in TERMINAL_SUCCESS_RESULTS and result != "success":
            failures.append(
                f"{job_name} must succeed for this change set; got {result or 'missing result'}"
            )
    return failures


def _parse_bool(value: str) -> bool:
    if value == "true":
        return True
    if value == "false":
        return False
    raise argparse.ArgumentTypeError("expected 'true' or 'false'")


def pyright_error_count(payload: object) -> int:
    """Extract one trustworthy non-negative error count or fail closed."""
    if not isinstance(payload, Mapping):
        raise ValueError("pyright output must be a JSON object")
    payload_map = cast(Mapping[object, object], payload)
    summary = payload_map.get("summary")
    if not isinstance(summary, Mapping):
        raise ValueError("pyright output is missing summary")
    summary_map = cast(Mapping[object, object], summary)
    count = summary_map.get("errorCount")
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        raise ValueError("pyright errorCount must be a non-negative integer")
    return count


def _pyright_count_command() -> int:
    try:
        payload = json.load(sys.stdin)
        count = pyright_error_count(payload)
    except (json.JSONDecodeError, ValueError) as exc:
        print(f"::error::invalid pyright JSON: {exc}", file=sys.stderr)
        return 1
    print(count)
    return 0


def _select_tests_command(args: argparse.Namespace) -> int:
    files = [line for raw in sys.stdin for line in [raw.strip()] if line]
    try:
        selected = select_test_files(
            files,
            source_shard=args.source_shard,
            source_shards=args.source_shards,
            split_count=args.split_count,
            split_part=args.split_part,
        )
    except ValueError as exc:
        print(f"::error::{exc}", file=sys.stderr)
        return 1
    for path in selected:
        print(path)
    return 0


def _classify_command(github_output: Path) -> int:
    raw_paths = sys.stdin.buffer.read().split(b"\0")
    paths = [path.decode("utf-8", errors="surrogateescape") for path in raw_paths if path]
    groups = classify_paths(paths)
    with github_output.open("a", encoding="utf-8", newline="\n") as output:
        for name in ("code", "python"):
            print(f"{name}={str(groups[name]).lower()}", file=output)
    print(f"Changed paths: {len(paths)}; code={groups['code']}; python={groups['python']}")
    return 0


def _verify_command(args: argparse.Namespace) -> int:
    results = {
        "changes": args.changes_result,
        "tests": args.tests_result,
        "quality": args.quality_result,
        "typecheck": args.typecheck_result,
        "security": args.security_result,
    }
    failures = gate_failures(code=args.code, python=args.python, results=results)
    for failure in failures:
        print(f"::error::{failure}")
    if failures:
        return 1
    print("All applicable CI jobs completed successfully.")
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    classify = subparsers.add_parser("classify")
    classify.add_argument("--github-output", type=Path, required=True)

    verify = subparsers.add_parser("verify")
    verify.add_argument("--code", type=_parse_bool, required=True)
    verify.add_argument("--python", type=_parse_bool, required=True)
    for job_name in ("changes", *CONDITIONAL_JOBS):
        verify.add_argument(f"--{job_name}-result", required=True)
    subparsers.add_parser("pyright-count")
    select_tests = subparsers.add_parser("select-tests")
    select_tests.add_argument("--source-shard", type=int, required=True)
    select_tests.add_argument("--source-shards", type=int, default=8)
    select_tests.add_argument("--split-count", type=int, required=True)
    select_tests.add_argument("--split-part", type=int, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.command == "classify":
        return _classify_command(args.github_output)
    if args.command == "pyright-count":
        return _pyright_count_command()
    if args.command == "select-tests":
        return _select_tests_command(args)
    return _verify_command(args)


if __name__ == "__main__":
    raise SystemExit(main())
