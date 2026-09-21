#!/usr/bin/env python3
"""Classify changed paths and verify the single required CI gate."""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from fnmatch import fnmatchcase
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
    "AGENTS.md",
    "Makefile",
    "alembic.ini",
    "pyproject.toml",
    "requirements.lock",
    "requirements.txt",
    "directives/design_language.md",
}
PYTHON_ROOT_FILES = {"pyproject.toml", "requirements.lock"}
DOCUMENTATION_SUFFIXES = {".md", ".rst"}
# Design-impacting paths (workstream C1). Anything that owns rendered-surface
# truth must run the Design Sync job, so a doubtful path is classified as
# design (fail toward running the gate). A PR that touches no design path
# skips Design Sync; pushes to main and the nightly schedule trigger run it
# regardless of this classification (see the design job's `if:` in ci.yml).
DESIGN_PREFIXES = (
    "design-system/",
    "mockups/",
    # conformance_scan.discover_emitters scans both complete Python roots.
    # New and currently unregistered emitters must reach that gate too; the
    # registry remains the owner of the surface inventory.
    "src/",
    "execution/",
    "tests/golden/",
)
DESIGN_FILES = {
    # Work OS shell/style contract sources.
    "src/pipeline/work_os_shell.py",
    "src/pipeline/work_os_runtime.js",
    "src/pipeline/work_os_styles.py",
    # Browser security shares the Chromium-equipped Design job.
    "execution/comments_server.py",
    "src/server_runtime/access.py",
    "tests/test_browser_security_canary.py",
    # Design guard tooling.
    "scripts/check_design_sync.py",
    "execution/verify_design_conformance.py",
    "execution/design_route_canaries.py",
    # Contract document and machine-readable design baselines.
    "directives/design_language.md",
    "tests/design_conformance_debt.json",
    "tests/design_geometry_baseline.json",
    # Design guard inputs.
    "requirements-design.lock",
    # Design golden/shell tests. The canary and the other design-tool tests
    # are covered by DESIGN_FILE_PATTERNS below.
    "tests/test_workspace_golden.py",
    "tests/test_work_os_shell.py",
    "tests/test_extracted_runtime_design.py",
    "tests/test_work_os_style_master.py",
}
DESIGN_FILE_PATTERNS = ("scripts/gen_design_*.py", "tests/test_design_*.py")
CONDITIONAL_JOBS = {
    "fast-signal": "code",
    "tests": "code",
    "design": "design",
    "quality": "python",
    "typecheck": "python",
    "security": "code",
}
TERMINAL_SUCCESS_RESULTS = {"success", "skipped"}
DiagnosticFingerprint = tuple[str, str, str]

# Canonical test-shard label order. The `tests` matrix `include` list in
# ci.yml and `.github/test-durations.json`'s `labels` must both match this
# exactly (enforced by tests), so the aggregate gate's `needs.tests` contract
# and the `tests (shard <label>/8)` job names never drift.
SHARD_LABELS = (
    "1",
    "1 overflow",
    "2",
    "2 overflow",
    "3",
    "3 overflow",
    "4",
    "5",
    "6",
    "6 overflow",
    "7",
    "8",
    "8 overflow",
)
DURATIONS_FILE = ".github/test-durations.json"


class TestDurations:
    """Validated checked-in per-file durations plus the pinned shard assignment.

    `shard_by_file` pins every known test file to its canonical shard label so a
    file whose measured cost did not change never moves shards (the stable
    assignment property). `seconds_by_file` feeds the deterministic
    duration-aware packing used to regenerate the table and to place new files.
    """

    __slots__ = ("default_seconds", "labels", "seconds_by_file", "shard_by_file")

    def __init__(
        self,
        *,
        labels: tuple[str, ...],
        default_seconds: float,
        seconds_by_file: dict[str, float],
        shard_by_file: dict[str, str],
    ) -> None:
        self.labels = labels
        self.default_seconds = default_seconds
        self.seconds_by_file = seconds_by_file
        self.shard_by_file = shard_by_file

    def duration_seconds(self, path: str) -> float:
        return self.seconds_by_file.get(path, self.default_seconds)


def _normalize(path: str) -> str:
    return path.replace("\\", "/").removeprefix("./")


def _is_design_path(path: str) -> bool:
    """Design classification fails toward running the Design Sync job."""
    if path in DESIGN_FILES or path.startswith(DESIGN_PREFIXES):
        return True
    return any(fnmatchcase(path, pattern) for pattern in DESIGN_FILE_PATTERNS)


def classify_paths(paths: Iterable[str]) -> dict[str, bool]:
    """Return the expensive CI groups required by *paths*."""

    code = False
    python = False
    design = False
    for raw_path in paths:
        path = _normalize(raw_path)
        if not path:
            continue
        known_code_path = path in CODE_ROOT_FILES or path.startswith(CODE_PREFIXES)
        is_code = known_code_path or Path(path).suffix.lower() not in DOCUMENTATION_SUFFIXES
        is_python = path in PYTHON_ROOT_FILES or path.endswith(".py")
        code = code or is_code
        python = python or is_python
        design = design or _is_design_path(path)
    return {"code": code, "python": python, "design": design}


def _positive_float(value: object, *, what: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise ValueError(f"{what} must be a positive number")
    return float(value)


def load_test_durations(path: Path) -> TestDurations:
    """Load and validate the checked-in durations table (fail closed)."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid durations file {path}: {exc}") from exc
    if not isinstance(payload, Mapping):
        raise ValueError("durations payload must be a JSON object")
    payload_map = cast(Mapping[object, object], payload)

    schema = payload_map.get("schema")
    if isinstance(schema, bool) or schema != 1:
        raise ValueError("durations file schema must be 1")

    labels_raw = payload_map.get("labels")
    if not isinstance(labels_raw, Sequence) or isinstance(labels_raw, (str, bytes)):
        raise ValueError("durations labels must be a JSON array")
    labels_seq = cast(Sequence[object], labels_raw)
    labels: list[str] = []
    for raw_label in labels_seq:
        if not isinstance(raw_label, str) or not raw_label:
            raise ValueError("durations labels must be non-empty strings")
        labels.append(raw_label)
    if labels != list(SHARD_LABELS):
        raise ValueError("durations labels must match the CI shard labels exactly")
    label_tuple = tuple(labels)

    if "default_seconds" not in payload_map:
        raise ValueError("durations file is missing default_seconds")
    default_seconds = _positive_float(payload_map.get("default_seconds"), what="default_seconds")

    files_raw = payload_map.get("files")
    if not isinstance(files_raw, Mapping):
        raise ValueError("durations files must be a JSON object")
    files_map = cast(Mapping[object, object], files_raw)
    seconds_by_file: dict[str, float] = {}
    shard_by_file: dict[str, str] = {}
    for raw_path, raw_record in files_map.items():
        if not isinstance(raw_path, str) or not raw_path:
            raise ValueError("durations file keys must be non-empty strings")
        if not isinstance(raw_record, Mapping):
            raise ValueError(f"durations record for {raw_path} must be a JSON object")
        record = cast(Mapping[object, object], raw_record)
        seconds = _positive_float(record.get("seconds"), what=f"seconds for {raw_path}")
        shard = record.get("shard")
        if not isinstance(shard, str) or shard not in label_tuple:
            raise ValueError(f"shard for {raw_path} must be one of the shard labels")
        seconds_by_file[raw_path] = seconds
        shard_by_file[raw_path] = shard
    return TestDurations(
        labels=label_tuple,
        default_seconds=default_seconds,
        seconds_by_file=seconds_by_file,
        shard_by_file=shard_by_file,
    )


def pack_test_shards(files: Sequence[str], durations: TestDurations) -> dict[str, list[str]]:
    """Deterministic duration-aware bin packing over the canonical labels.

    Longest-processing-time packing: files are processed highest-seconds-first
    (path tie-break) and each goes to the least-loaded bin (earliest-label
    tie-break), so the result is a pure function of (files, durations) — never
    of run order or machine. This is the *generator* the checked-in table's
    `shard` fields are produced with; runtime selection in `select_test_files`
    does not re-pack known files, which is exactly what keeps an unchanged file
    in its shard when unrelated files change.
    """
    labels = durations.labels
    totals = {label: 0.0 for label in labels}
    bins: dict[str, list[str]] = {label: [] for label in labels}
    for path in sorted(files, key=lambda p: (-durations.duration_seconds(p), p)):
        label = min(labels, key=lambda cand: (totals[cand], labels.index(cand)))
        totals[label] += durations.duration_seconds(path)
        bins[label].append(path)
    return bins


def select_test_files(
    files: Sequence[str], *, shard_label: str, durations: TestDurations
) -> list[str]:
    """Return one job's test files from the stable, duration-aware assignment.

    Files recorded in the durations table keep their pinned shard (unchanged
    cost -> unchanged shard). Files not in the table get a deterministic default
    shard: the least-loaded label at their position in the sorted list, with the
    earliest label breaking ties, at `default_seconds` each. The file list is
    sorted internally so the assignment never depends on caller input order.
    """
    if shard_label not in durations.labels:
        raise ValueError(
            f"unknown shard label {shard_label!r}; expected one of {list(durations.labels)}"
        )
    totals = {label: 0.0 for label in durations.labels}
    selected: list[str] = []
    for path in sorted(files):
        known_shard = durations.shard_by_file.get(path)
        if known_shard is None:
            seconds = durations.default_seconds
            known_shard = min(
                durations.labels,
                key=lambda cand: (totals[cand], durations.labels.index(cand)),
            )
        else:
            seconds = durations.duration_seconds(path)
        totals[known_shard] += seconds
        if known_shard == shard_label:
            selected.append(path)
    return selected


def gate_failures(
    *, code: bool, python: bool, design: bool, results: Mapping[str, str]
) -> list[str]:
    """Explain every terminal result that makes the aggregate gate unsafe."""

    failures: list[str] = []
    for job_name in ("changes", "public-boundary"):
        result = results.get(job_name, "")
        if result != "success":
            failures.append(f"{job_name} must succeed; got {result or 'missing result'}")

    for job_name in CONDITIONAL_JOBS:
        result = results.get(job_name, "")
        if result not in TERMINAL_SUCCESS_RESULTS:
            failures.append(f"{job_name} finished with {result or 'missing result'}")

    required_groups = {"code": code, "python": python, "design": design}
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
        durations = load_test_durations(args.durations_file)
        selected = select_test_files(files, shard_label=args.shard_label, durations=durations)
    except ValueError as exc:
        print(f"::error::{exc}", file=sys.stderr)
        return 1
    for path in selected:
        print(path)
    return 0


def _durations_report_command(args: argparse.Namespace) -> int:
    """Print the predicted per-label file counts and serial seconds."""
    files = [line for raw in sys.stdin for line in [raw.strip()] if line]
    try:
        durations = load_test_durations(args.durations_file)
    except ValueError as exc:
        print(f"::error::{exc}", file=sys.stderr)
        return 1
    totals = {label: 0.0 for label in durations.labels}
    counts = {label: 0 for label in durations.labels}
    for path in sorted(files):
        known_shard = durations.shard_by_file.get(path)
        if known_shard is None:
            seconds = durations.default_seconds
            known_shard = min(
                durations.labels,
                key=lambda cand: (totals[cand], durations.labels.index(cand)),
            )
        else:
            seconds = durations.duration_seconds(path)
        totals[known_shard] += seconds
        counts[known_shard] += 1
    for label in durations.labels:
        print(f"{label}\t{counts[label]}\t{totals[label]:.1f}")
    print(f"files={len(files)} default_seconds={durations.default_seconds}")
    return 0


def _classify_command(github_output: Path) -> int:
    raw_paths = sys.stdin.buffer.read().split(b"\0")
    paths = [path.decode("utf-8", errors="surrogateescape") for path in raw_paths if path]
    groups = classify_paths(paths)
    with github_output.open("a", encoding="utf-8", newline="\n") as output:
        for name in ("code", "python", "design"):
            print(f"{name}={str(groups[name]).lower()}", file=output)
    print(
        f"Changed paths: {len(paths)}; code={groups['code']}; "
        f"python={groups['python']}; design={groups['design']}"
    )
    return 0


def _verify_command(args: argparse.Namespace) -> int:
    results = {
        "changes": args.changes_result,
        "public-boundary": args.public_boundary_result,
        "fast-signal": args.fast_signal_result,
        "tests": args.tests_result,
        "design": args.design_result,
        "quality": args.quality_result,
        "typecheck": args.typecheck_result,
        "security": args.security_result,
    }
    failures = gate_failures(
        code=args.code, python=args.python, design=args.design, results=results
    )
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
    verify.add_argument("--design", type=_parse_bool, required=True)
    for job_name in ("changes", "public-boundary", *CONDITIONAL_JOBS):
        verify.add_argument(f"--{job_name}-result", required=True)
    subparsers.add_parser("pyright-count")
    pyright_diff = subparsers.add_parser("pyright-diff")
    pyright_diff.add_argument("--base-json", type=Path, required=True)
    pyright_diff.add_argument("--head-json", type=Path, required=True)
    pyright_diff.add_argument("--base-root", type=Path, required=True)
    pyright_diff.add_argument("--head-root", type=Path, required=True)
    select_tests = subparsers.add_parser("select-tests")
    select_tests.add_argument("--shard-label", required=True)
    select_tests.add_argument("--durations-file", type=Path, default=Path(DURATIONS_FILE))
    durations_report = subparsers.add_parser("durations-report")
    durations_report.add_argument("--durations-file", type=Path, default=Path(DURATIONS_FILE))
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
    if args.command == "durations-report":
        return _durations_report_command(args)
    return _verify_command(args)


if __name__ == "__main__":
    raise SystemExit(main())
