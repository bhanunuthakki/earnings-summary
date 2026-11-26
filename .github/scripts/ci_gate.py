#!/usr/bin/env python3
"""Classify changed paths and verify the single required CI gate."""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import cast
from xml.etree import ElementTree

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
    "AGENTS.md",
    "Makefile",
    "alembic.ini",
    "pyproject.toml",
    "requirements.lock",
    "requirements.txt",
    "requirements-browser.lock",
}
PYTHON_ROOT_FILES = {"pyproject.toml", "requirements.lock"}
DOCUMENTATION_SUFFIXES = {".md", ".rst"}
CONDITIONAL_JOBS = {
    "fast-signal": "code",
    "windows-native": "code",
    "windows-app": "windows_app",
    "tests": "tests",
    "browser-security": "code",
    "quality": "python",
    "typecheck": "python",
    "security": "code",
}
TERMINAL_SUCCESS_RESULTS = {"success", "skipped"}
# The application harness on a hosted Windows VM: every file must report cases,
# and none may be skipped, fail or error.
WINDOWS_APP_TEST_FILES = (
    "tests/test_owner_proposal_approval.py",
    "tests/test_storage_reservation.py",
    "tests/test_job_runtime.py",
    "tests/test_cockpit_fundamentals.py",
    "tests/test_sqlite_bootstrap.py",
    "tests/test_dcf_retained_availability.py",
    "tests/test_dcf_canonical_statements.py",
    "tests/test_redesign_input_evidence.py",
    "tests/test_redesign_workbook_evidence.py",
)
# Cases whose absence would void the native evidence, including every Windows
# parametrization. A deselected case leaves no testcase element, so module
# presence alone cannot prove it ran; each of these must pass exactly once.
WINDOWS_APP_REQUIRED_CASES = frozenset(
    f"{module}::{name}"
    for module, names in {
        "tests.test_dcf_retained_availability": (
            "test_native_statement_roles_require_review_and_keep_exact_input_lineage[currency]",
            "test_native_statement_roles_require_review_and_keep_exact_input_lineage[millions]",
        ),
        "tests.test_dcf_canonical_statements": (
            "test_actual_builder_preserves_zero_capex_and_emits_no_unavailable_gross_margin[True]",
            "test_actual_builder_preserves_zero_capex_and_emits_no_unavailable_gross_margin[False]",
        ),
        "tests.test_redesign_input_evidence": (
            "test_real_retained_source_recipe_and_changed_source_bytes[currency]",
            "test_real_retained_source_recipe_and_changed_source_bytes[millions]",
            "test_real_retained_source_recipe_and_changed_source_bytes[billions]",
        ),
        "tests.test_redesign_workbook_evidence": (
            "test_workbook_receipt_roundtrip_and_replay",
            "test_native_debt_operand_arithmetic_matches_workbook_display",
        ),
        "tests.test_owner_proposal_approval": (
            "test_register_and_approve_through_existing_routes[False]",
            "test_register_and_approve_through_existing_routes[True]",
            "test_sql_replace_cannot_erase_owner_authority[owner_proposal_authorizations]",
            "test_sql_replace_cannot_erase_owner_authority[research_proposals]",
            "test_authorization_and_link_are_immutable",
            "test_decision_revalidates_retained_authorization_evidence_digest",
            *(
                f"test_id_updates_cannot_move_or_replace_retained_owner_proposals[{operation}-{column}]"
                for operation in ("move_owner", "replace_owner", "replace_legacy")
                for column in ("id", "rowid", "_rowid_", "oid")
            ),
            *(
                f"test_authorization_replacement_cannot_target_hidden_row_identity[{column}]"
                for column in ("rowid", "_rowid_", "oid")
            ),
            "test_hash_mismatch_and_atomic_registration",
        ),
        "tests.test_cockpit_fundamentals": (
            "test_cockpit_never_serves_cache_built_before_canonical_facts_changed",
            "test_same_database_capture_invalidates_the_cache",
            "test_every_revision_token_table_rejects_update_and_delete",
            "test_materialize_refuses_an_open_caller_transaction",
        ),
        "tests.test_storage_reservation": (
            "test_windows_accounts_with_different_temp_roots_share_volume_admission",
            "test_concurrent_processes_cannot_both_admit_over_host_limit",
            "test_ledger_parent_link_is_refused_without_writing_target",
        ),
        "tests.test_sqlite_bootstrap": (
            "test_vendored_sqlite_dll_matches_pinned_binary_hash",
            "test_bootstrap_preloads_sqlite_3534_before_application_import",
            "test_windows_session_is_bound_to_the_vendored_hash_verified_dll",
        ),
        "tests.test_job_runtime": (
            *(
                f"test_windows_process_probe_checks_termination_and_closes_handle[{p}]"
                for p in ("0-False", "258-True", "4294967295-True", "128-True")
            ),
            *(
                f"test_windows_process_open_failure_requires_positive_absence_evidence[{p}]"
                for p in ("87-False", "5-True", "6-True")
            ),
            *(
                f"test_windows_creation_identity_uses_full_handle_and_closes_on_query_failure[{p}]"
                for p in ("True", "False")
            ),
            "test_windows_process_loader_declares_handle_and_filetime_pointer_abi",
            *(
                f"test_windows_owner_wait_state_controls_stale_lock_recovery[{p}]"
                for p in ("0", "258", "4294967295", "128")
            ),
            *(
                f"test_windows_scheduler_owner_wait_state_controls_child_termination[{p}]"
                for p in (
                    "0-win:same--15",
                    "258-win:same-17",
                    "4294967295-win:same-17",
                    "128-win:same-17",
                    "258-None-17",
                    "258-win:other--15",
                )
            ),
            "test_windows_mutex_is_cross_session",
        ),
    }.items()
    for name in names
)
DiagnosticFingerprint = tuple[str, str, str]
WINDOWS_STORAGE_LEASE_CASES = frozenset(
    {
        "test_concurrent_processes_cannot_both_admit_over_host_limit",
        "test_dead_holder_lease_is_reclaimed_at_admission",
        "test_live_holder_lease_is_never_reclaimed",
    }
)
WINDOWS_NATIVE_CASES = WINDOWS_STORAGE_LEASE_CASES | frozenset(
    {
        "test_windows_exited_process_with_held_handle_is_dead_and_lock_is_recoverable[0]",
        "test_windows_exited_process_with_held_handle_is_dead_and_lock_is_recoverable[259]",
        "test_windows_kill_on_close_job_terminates_process_and_descendant",
        "test_managed_child_is_suspended_assigned_resumed_and_kills_descendant",
        "test_managed_deadline_kills_live_worker_and_descendant",
    }
)

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
# A selected pull-request subset gets one shard per this many measured test
# seconds, so a small selection runs in a few jobs instead of thirteen.
SELECTED_SHARD_SECONDS = 60.0
# The browser canary has one owner, Browser Security; test shards never run it.
BROWSER_CANARY = "tests/test_browser_security_canary.py"


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
    return _pack(files, durations, durations.labels)


def _pack(
    files: Sequence[str], durations: TestDurations, labels: Sequence[str]
) -> dict[str, list[str]]:
    totals = {label: 0.0 for label in labels}
    bins: dict[str, list[str]] = {label: [] for label in labels}
    for path in sorted(files, key=lambda p: (-durations.duration_seconds(p), p)):
        label = min(labels, key=lambda cand: (totals[cand], list(labels).index(cand)))
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

    The returned files are ordered longest-first (path tie-break). pytest
    collects arguments in the given order and xdist `--dist=loadfile` hands out
    whole files in collection order, so this order lets the slowest files
    start first instead of finishing last on one worker.
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
    return sorted(selected, key=lambda path: (-durations.duration_seconds(path), path))


def junit_seconds_by_file(junit_paths: Sequence[Path], files: Sequence[str]) -> dict[str, float]:
    """Sum JUnit testcase seconds per test file (fail closed on unknown classes).

    pytest writes `classname="tests.test_x"` or `"tests.test_x.TestClass"`; the
    longest dotted prefix that names a file in `files` owns the case.
    """
    by_module = {path.removesuffix(".py").replace("/", "."): path for path in files}
    seconds: dict[str, float] = {}
    for junit_path in junit_paths:
        root = ElementTree.parse(junit_path).getroot()
        for case in root.iter("testcase"):
            # A module skipped at collection reports classname="" and its
            # dotted module path as the case name.
            classname = case.get("classname", "") or case.get("name", "")
            parts = classname.split(".")
            owner = next(
                (
                    by_module[".".join(parts[:end])]
                    for end in range(len(parts), 0, -1)
                    if ".".join(parts[:end]) in by_module
                ),
                None,
            )
            if owner is None:
                raise ValueError(f"{junit_path}: testcase class {classname!r} names no test file")
            seconds[owner] = seconds.get(owner, 0.0) + float(case.get("time", "0") or 0)
    return seconds


def regenerate_test_durations(
    junit_paths: Sequence[Path], files: Sequence[str], *, source: str, generated: str
) -> dict[str, object]:
    """Build the checked-in durations table from CI JUnit with canonical packing."""
    measured = junit_seconds_by_file(junit_paths, files)
    default_seconds = 1.6
    # Files whose cases all report 0.000 s still cost collection; keep the floor.
    seconds = {path: round(max(value, 0.001), 3) for path, value in sorted(measured.items())}
    durations = TestDurations(
        labels=SHARD_LABELS,
        default_seconds=default_seconds,
        seconds_by_file=seconds,
        shard_by_file={},
    )
    packed = pack_test_shards(list(seconds), durations)
    shard_by_file = {path: label for label, paths in packed.items() for path in paths}
    return {
        "schema": 1,
        "generated": generated,
        "source": source,
        "default_seconds": default_seconds,
        "labels": list(SHARD_LABELS),
        "shard_timings_seconds": {
            label: round(sum(seconds[path] for path in packed[label]), 3) for label in SHARD_LABELS
        },
        "files": {
            path: {"seconds": seconds[path], "shard": shard_by_file[path]} for path in seconds
        },
    }


# quality.test_selection.SCANS_REPOSITORY: a file chosen only for this reason
# reads the whole tree, not code the change alters.
SCANS_REPOSITORY = "scans the repository"


def selected_files(lines: Sequence[str]) -> dict[str, str]:
    """Each chosen test file of a ``SELECTED`` verdict and why it was chosen."""
    chosen: dict[str, str] = {}
    for line in lines[1:]:
        path, _, reason = line.partition("\t")
        if path.strip():
            chosen[path.strip()] = reason.strip()
    return chosen


def test_matrix(lines: Sequence[str], durations: TestDurations) -> tuple[bool, dict[str, object]]:
    """The tests job matrix for a test-selection verdict, and whether any shard runs.

    A ``SELECTED`` verdict packs its files over just enough of the canonical
    labels, about ``SELECTED_SHARD_SECONDS`` of measured time each; each entry
    carries its files, longest first. Any other verdict gets every label with
    no files, and each shard then runs its pinned share of the full suite.
    """
    full: dict[str, object] = {"include": [{"label": label, "files": ""} for label in SHARD_LABELS]}
    if not lines or not lines[0].startswith("SELECTED"):
        return True, full
    files = sorted(set(selected_files(lines)) - {BROWSER_CANARY})
    if not files:
        return False, full
    total = sum(durations.duration_seconds(path) for path in files)
    count = max(1, min(len(SHARD_LABELS), len(files), math.ceil(total / SELECTED_SHARD_SECONDS)))
    bins = _pack(files, durations, SHARD_LABELS[:count])
    include = [
        {
            "label": label,
            "files": "\n".join(
                sorted(bins[label], key=lambda path: (-durations.duration_seconds(path), path))
            ),
        }
        for label in SHARD_LABELS[:count]
    ]
    return True, {"include": include}


def windows_app_needed(lines: Sequence[str]) -> bool:
    """Whether the Windows application job runs for a test-selection verdict.

    Only a ``SELECTED`` verdict that chose none of ``WINDOWS_APP_TEST_FILES``
    for the change itself skips it. A file chosen only because it scans the
    repository does not count: two of these files do, so every pull request
    chooses them, and the Linux shards still run them. Every other verdict,
    including the merge queue's, runs the job.
    """
    if not lines or not lines[0].startswith("SELECTED"):
        return True
    return any(
        path in WINDOWS_APP_TEST_FILES and reason != SCANS_REPOSITORY
        for path, reason in selected_files(lines).items()
    )


def gate_failures(
    *,
    code: bool,
    python: bool,
    results: Mapping[str, str],
    tests: bool | None = None,
    windows_app: bool | None = None,
) -> list[str]:
    """Explain every terminal result that makes the aggregate gate unsafe.

    ``tests`` is False only when a pull request's selection chose no test file;
    the tests job may then be skipped. ``windows_app`` is False only when that
    selection chose none of the Windows application files. Both default to
    ``code``.
    """

    failures: list[str] = []
    for job_name in ("changes", "public-boundary"):
        result = results.get(job_name, "")
        if result != "success":
            failures.append(f"{job_name} must succeed; got {result or 'missing result'}")

    for job_name in CONDITIONAL_JOBS:
        result = results.get(job_name, "")
        if result not in TERMINAL_SUCCESS_RESULTS:
            failures.append(f"{job_name} finished with {result or 'missing result'}")

    required_groups = {
        "code": code,
        "python": python,
        "tests": code and tests is not False,
        "windows_app": code and windows_app is not False,
    }
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


def _test_matrix_command(args: argparse.Namespace) -> int:
    lines = [raw.rstrip("\n") for raw in sys.stdin]
    try:
        durations = load_test_durations(args.durations_file)
    except ValueError as exc:
        print(f"::error::{exc}", file=sys.stderr)
        return 1
    run, matrix = test_matrix(lines, durations)
    windows_app = windows_app_needed(lines)
    shards = cast(list[dict[str, str]], matrix["include"])
    with args.github_output.open("a", encoding="utf-8", newline="\n") as output:
        print(f"tests={str(run).lower()}", file=output)
        print(f"matrix={json.dumps(matrix, separators=(',', ':'))}", file=output)
        print(f"windows_app={str(windows_app).lower()}", file=output)
    verdict = lines[0].split("\t", 1)[0] if lines else "FULL"
    print(
        f"Test selection: {verdict}; {len(shards) if run else 0} test shard(s); "
        f"Windows application tests: {'yes' if windows_app else 'no'}"
    )
    return 0


def _durations_from_junit_command(args: argparse.Namespace) -> int:
    """Write a regenerated durations table from CI JUnit; test files on stdin."""
    files = [line for raw in sys.stdin for line in [raw.strip()] if line]
    try:
        payload = regenerate_test_durations(
            args.junit, files, source=args.source, generated=args.generated
        )
    except (ValueError, ElementTree.ParseError) as exc:
        print(f"::error::{exc}", file=sys.stderr)
        return 1
    args.output.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
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
        for name in ("code", "python"):
            print(f"{name}={str(groups[name]).lower()}", file=output)
    print(f"Changed paths: {len(paths)}; code={groups['code']}; python={groups['python']}")
    return 0


def _verify_command(args: argparse.Namespace) -> int:
    results = {
        "changes": args.changes_result,
        "public-boundary": args.public_boundary_result,
        "fast-signal": args.fast_signal_result,
        "windows-native": args.windows_native_result,
        "windows-app": args.windows_app_result,
        "tests": args.tests_result,
        "browser-security": args.browser_security_result,
        "quality": args.quality_result,
        "typecheck": args.typecheck_result,
        "security": args.security_result,
    }
    failures = gate_failures(
        code=args.code,
        python=args.python,
        results=results,
        tests=args.tests,
        windows_app=args.windows_app,
    )
    for failure in failures:
        print(f"::error::{failure}")
    if failures:
        return 1
    print("All applicable CI jobs completed successfully.")
    return 0


def _verify_windows_native(junit: Path) -> int:
    """Reject missing, duplicate, skipped or failed cases in pytest's own receipt."""
    try:
        cases = ElementTree.parse(junit).findall(".//testcase")
    except (OSError, ElementTree.ParseError) as error:
        print(f"::error::Native Windows receipt unavailable: {type(error).__name__}")
        return 1
    names = Counter(case.get("name", "") for case in cases)
    if names != Counter(WINDOWS_NATIVE_CASES) or any(
        child.tag in {"skipped", "failure", "error"} for case in cases for child in case
    ):
        print("::error::Each of the eight native Windows cases must pass exactly once.")
        return 1
    print("All eight native Windows process-tree and storage-lease cases passed.")
    return 0


def _verify_windows_app(junit: Path) -> int:
    """Reject a receipt that lacks any selected file or has any non-passing case."""
    try:
        cases = ElementTree.parse(junit).findall(".//testcase")
    except (OSError, ElementTree.ParseError) as error:
        print(f"::error::Native Windows application receipt unavailable: {type(error).__name__}")
        return 1
    modules = Counter(".".join(case.get("classname", "").split(".")[:2]) for case in cases)
    missing = [
        path
        for path in WINDOWS_APP_TEST_FILES
        if modules[path.removesuffix(".py").replace("/", ".")] == 0
    ]
    not_passed = [
        case.get("name", "")
        for case in cases
        if any(child.tag in {"skipped", "failure", "error"} for child in case)
    ]
    ran = Counter(
        f"{'.'.join(case.get('classname', '').split('.')[:2])}::{case.get('name', '')}"
        for case in cases
    )
    absent = sorted(case for case in WINDOWS_APP_REQUIRED_CASES if ran[case] != 1)
    if missing or not_passed or absent:
        for path in missing:
            print(f"::error::No native Windows cases ran from {path}.")
        for case in absent:
            print(f"::error::Required native Windows case did not run exactly once: {case}")
        for name in not_passed:
            print(f"::error::Native Windows case did not pass: {name}")
        return 1
    print(f"All {len(cases)} native Windows application cases passed.")
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    classify = subparsers.add_parser("classify")
    classify.add_argument("--github-output", type=Path, required=True)

    verify = subparsers.add_parser("verify")
    verify.add_argument("--code", type=_parse_bool, required=True)
    verify.add_argument("--python", type=_parse_bool, required=True)
    verify.add_argument("--tests", type=_parse_bool, default=None)
    verify.add_argument("--windows-app", type=_parse_bool, default=None)
    for job_name in ("changes", "public-boundary", *CONDITIONAL_JOBS):
        verify.add_argument(f"--{job_name}-result", required=True)
    native = subparsers.add_parser("verify-windows-native")
    native.add_argument("--junit", type=Path, required=True)
    native_app = subparsers.add_parser("verify-windows-app")
    native_app.add_argument("--junit", type=Path, required=True)
    subparsers.add_parser("pyright-count")
    pyright_diff = subparsers.add_parser("pyright-diff")
    pyright_diff.add_argument("--base-json", type=Path, required=True)
    pyright_diff.add_argument("--head-json", type=Path, required=True)
    pyright_diff.add_argument("--base-root", type=Path, required=True)
    pyright_diff.add_argument("--head-root", type=Path, required=True)
    matrix = subparsers.add_parser("test-matrix")
    matrix.add_argument("--durations-file", type=Path, default=Path(DURATIONS_FILE))
    matrix.add_argument("--github-output", type=Path, required=True)
    select_tests = subparsers.add_parser("select-tests")
    select_tests.add_argument("--shard-label", required=True)
    select_tests.add_argument("--durations-file", type=Path, default=Path(DURATIONS_FILE))
    from_junit = subparsers.add_parser("durations-from-junit")
    from_junit.add_argument("--junit", type=Path, action="append", required=True)
    from_junit.add_argument("--source", required=True)
    from_junit.add_argument("--generated", required=True)
    from_junit.add_argument("--output", type=Path, default=Path(DURATIONS_FILE))
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
    if args.command == "test-matrix":
        return _test_matrix_command(args)
    if args.command == "durations-from-junit":
        return _durations_from_junit_command(args)
    if args.command == "durations-report":
        return _durations_report_command(args)
    if args.command == "verify-windows-native":
        return _verify_windows_native(args.junit)
    if args.command == "verify-windows-app":
        return _verify_windows_app(args.junit)
    return _verify_command(args)


if __name__ == "__main__":
    raise SystemExit(main())
