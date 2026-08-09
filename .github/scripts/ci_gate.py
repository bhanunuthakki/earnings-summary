#!/usr/bin/env python3
"""Classify changed paths and verify the single required CI gate."""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
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
    "design-system/",
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
    "design": "code",
    "quality": "python",
    "typecheck": "python",
    "security": "code",
}
TERMINAL_SUCCESS_RESULTS = {"success", "skipped"}
DiagnosticFingerprint = tuple[str, str, str]


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


def _relative_pyright_path(file_path: str, root: Path) -> str:
    normalized_file = _normalize(file_path)
    normalized_root = _normalize(str(root)).rstrip("/")
    if not normalized_root:
        raise ValueError("pyright repository root must not be empty")
    prefix = f"{normalized_root}/"
    if not normalized_file.startswith(prefix):
        raise ValueError(f"pyright diagnostic is outside repository root: {file_path}")
    return normalized_file[len(prefix) :]


def _pyright_error_fingerprints(payload: object, *, root: Path) -> list[DiagnosticFingerprint]:
    expected_count = pyright_error_count(payload)
    payload_map = cast(Mapping[object, object], payload)
    diagnostics = payload_map.get("generalDiagnostics")
    if not isinstance(diagnostics, Sequence) or isinstance(diagnostics, (str, bytes)):
        raise ValueError("pyright output is missing generalDiagnostics")
    diagnostic_sequence = cast(Sequence[object], diagnostics)

    fingerprints: list[DiagnosticFingerprint] = []
    for raw_diagnostic in diagnostic_sequence:
        if not isinstance(raw_diagnostic, Mapping):
            raise ValueError("pyright diagnostic must be a JSON object")
        diagnostic = cast(Mapping[object, object], raw_diagnostic)
        severity = diagnostic.get("severity")
        if not isinstance(severity, str):
            raise ValueError("pyright diagnostic severity must be a string")
        if severity != "error":
            continue
        file_path = diagnostic.get("file")
        message = diagnostic.get("message")
        rule = diagnostic.get("rule")
        if not isinstance(file_path, str) or not isinstance(message, str):
            raise ValueError("pyright error must include string file and message fields")
        if rule is not None and not isinstance(rule, str):
            raise ValueError("pyright diagnostic rule must be a string or null")
        relative_path = _relative_pyright_path(file_path, root)
        normalized_message = message.replace(str(root), "<repo>").replace(
            _normalize(str(root)), "<repo>"
        )
        fingerprints.append((relative_path, rule or "", normalized_message))

    if len(fingerprints) != expected_count:
        raise ValueError(
            "pyright summary errorCount does not match error diagnostics "
            f"({expected_count} != {len(fingerprints)})"
        )
    return fingerprints


def pyright_new_errors(
    base_payload: object,
    head_payload: object,
    *,
    base_root: Path,
    head_root: Path,
) -> list[DiagnosticFingerprint]:
    """Return new strict errors as a multiset, independent of line movement."""

    base_errors = Counter(_pyright_error_fingerprints(base_payload, root=base_root))
    head_errors = Counter(_pyright_error_fingerprints(head_payload, root=head_root))
    return sorted((head_errors - base_errors).elements())


def _pyright_count_command() -> int:
    try:
        payload = json.load(sys.stdin)
        count = pyright_error_count(payload)
    except (json.JSONDecodeError, ValueError) as exc:
        print(f"::error::invalid pyright JSON: {exc}", file=sys.stderr)
        return 1
    print(count)
    return 0


def _pyright_diff_command(args: argparse.Namespace) -> int:
    try:
        with args.base_json.open(encoding="utf-8") as base_file:
            base_payload = json.load(base_file)
        with args.head_json.open(encoding="utf-8") as head_file:
            head_payload = json.load(head_file)
        new_errors = pyright_new_errors(
            base_payload,
            head_payload,
            base_root=args.base_root,
            head_root=args.head_root,
        )
        base_count = pyright_error_count(base_payload)
        head_count = pyright_error_count(head_payload)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        print(f"::error::invalid pyright comparison: {exc}", file=sys.stderr)
        return 1

    print(f"pyright strict errors - base={base_count} head={head_count}")
    errors_by_file = Counter(path for path, _rule, _message in new_errors)
    for path, count in errors_by_file.most_common():
        print(f"new pyright errors - {path}: {count}")
    for path, rule, message in new_errors[:100]:
        rule_prefix = f"{rule}: " if rule else ""
        print(f"::error file={path}::{rule_prefix}{message}")
    if len(new_errors) > 100:
        print(f"::error::{len(new_errors) - 100} additional new pyright errors omitted")
    if new_errors:
        print(f"::error::pyright introduced {len(new_errors)} new strict error(s)")
        return 1
    print("No new pyright strict diagnostics (legacy baseline tolerated).")
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
        "design": args.design_result,
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
    pyright_diff = subparsers.add_parser("pyright-diff")
    pyright_diff.add_argument("--base-json", type=Path, required=True)
    pyright_diff.add_argument("--head-json", type=Path, required=True)
    pyright_diff.add_argument("--base-root", type=Path, required=True)
    pyright_diff.add_argument("--head-root", type=Path, required=True)
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
    if args.command == "pyright-diff":
        return _pyright_diff_command(args)
    if args.command == "select-tests":
        return _select_tests_command(args)
    return _verify_command(args)


if __name__ == "__main__":
    raise SystemExit(main())
