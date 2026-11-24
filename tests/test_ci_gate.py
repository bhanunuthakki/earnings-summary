from __future__ import annotations

import importlib.util
import io
import json
import math
import os
import re
import shutil
import subprocess
import sys
from collections import Counter
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
HELPER_PATH = REPO_ROOT / ".github" / "scripts" / "ci_gate.py"
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "ci.yml"


def _load_helper() -> ModuleType:
    spec = importlib.util.spec_from_file_location("ci_gate", HELPER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def helper() -> ModuleType:
    return _load_helper()


def test_documentation_only_change_skips_expensive_jobs(helper: ModuleType) -> None:
    assert helper.classify_paths(["README.md", "directives/roadmap_2026_08_consolidated.md"]) == {
        "code": False,
        "python": False,
    }


def test_agent_contract_changes_run_executable_guards(helper: ModuleType) -> None:
    assert helper.classify_paths(["AGENTS.md"]) == {"code": True, "python": False}


def test_unknown_non_documentation_path_fails_closed(helper: ModuleType) -> None:
    assert helper.classify_paths(["new-tool/config.toml"]) == {"code": True, "python": False}


@pytest.mark.parametrize(
    ("path", "python"),
    [
        ("src/report/builder.py", True),
        ("execution/run_morning_pipeline.py", True),
        ("tests/test_smoke.py", True),
        ("alembic/versions/0003_seed.py", True),
        ("cron/check_task_exit.ps1", False),
        ("scripts/check_ci.sh", False),
        (".githooks/pre-push", False),
        ("config/task_manifest.json", False),
        ("templates/company_brief.html", False),
        (".github/workflows/ci.yml", False),
        (".github/scripts/ci_gate.py", True),
        ("requirements.lock", True),
        ("requirements.txt", False),
        ("requirements-browser.lock", False),
        ("pyproject.toml", True),
        ("Makefile", False),
    ],
)
def test_code_change_classification(helper: ModuleType, path: str, python: bool) -> None:
    assert helper.classify_paths([path]) == {"code": True, "python": python}


def test_gate_requires_every_applicable_job_to_succeed(helper: ModuleType) -> None:
    assert helper.gate_failures(
        code=True,
        python=True,
        results={
            "changes": "success",
            "public-boundary": "success",
            "fast-signal": "success",
            "tests": "success",
            "windows-native": "success",
            "windows-app": "success",
            "browser-security": "success",
            "quality": "success",
            "typecheck": "skipped",
            "security": "success",
        },
    ) == ["typecheck must succeed for this change set; got skipped"]


def test_gate_accepts_skipped_expensive_jobs_for_docs_only(helper: ModuleType) -> None:
    assert (
        helper.gate_failures(
            code=False,
            python=False,
            results={
                "changes": "success",
                "public-boundary": "success",
                "fast-signal": "skipped",
                "tests": "skipped",
                "windows-native": "skipped",
                "windows-app": "skipped",
                "browser-security": "skipped",
                "quality": "skipped",
                "typecheck": "skipped",
                "security": "skipped",
            },
        )
        == []
    )


def test_gate_requires_fast_signal_for_code_changes(helper: ModuleType) -> None:
    assert helper.gate_failures(
        code=True,
        python=False,
        results={
            "changes": "success",
            "public-boundary": "success",
            "fast-signal": "skipped",
            "tests": "success",
            "windows-native": "success",
            "windows-app": "success",
            "browser-security": "success",
            "quality": "skipped",
            "typecheck": "skipped",
            "security": "success",
        },
    ) == ["fast-signal must succeed for this change set; got skipped"]


@pytest.mark.parametrize("result", ["skipped", "failure", "cancelled", ""])
def test_gate_requires_native_windows_evidence(helper: ModuleType, result: str) -> None:
    results: dict[str, str] = dict.fromkeys(
        (
            "changes",
            "public-boundary",
            "fast-signal",
            "windows-app",
            "tests",
            "browser-security",
            "quality",
            "typecheck",
            "security",
        ),
        "success",
    )
    results["windows-native"] = result
    failures = helper.gate_failures(code=True, python=True, results=results)
    assert len(failures) == 1
    assert failures[0].startswith("windows-native ")


@pytest.mark.parametrize(
    "disposition",
    ["passed", "skipped", "failure", "error", "missing", "duplicate", "malformed", "unavailable"],
)
def test_native_receipt_requires_each_exact_case_to_pass(
    helper: ModuleType, tmp_path: Path, disposition: str
) -> None:
    cases = [
        "test_windows_exited_process_with_held_handle_is_dead_and_lock_is_recoverable[0]",
        "test_windows_exited_process_with_held_handle_is_dead_and_lock_is_recoverable[259]",
        "test_windows_kill_on_close_job_terminates_process_and_descendant",
        "test_managed_child_is_suspended_assigned_resumed_and_kills_descendant",
        "test_managed_deadline_kills_live_worker_and_descendant",
        "test_concurrent_processes_cannot_both_admit_over_host_limit",
        "test_dead_holder_lease_is_reclaimed_at_admission",
        "test_live_holder_lease_is_never_reclaimed",
    ]
    if disposition == "missing":
        cases.pop()
    elif disposition == "duplicate":
        cases[-1] = cases[0]
    children = "" if disposition in {"passed", "missing", "duplicate"} else f"<{disposition}/>"
    receipt = tmp_path / "native.xml"
    receipt.write_text(
        "<testsuites><testsuite>"
        + "".join(
            f'<testcase name="{name}">{children if i == 0 else ""}</testcase>'
            for i, name in enumerate(cases)
        )
        + "</testsuite></testsuites>",
        encoding="utf-8",
    )
    if disposition == "malformed":
        receipt.write_text("not XML", encoding="utf-8")
    elif disposition == "unavailable":
        receipt.unlink()
    assert helper.main(["verify-windows-native", "--junit", str(receipt)]) == (
        0 if disposition == "passed" else 1
    )


def test_windows_native_job_is_isolated_and_required() -> None:
    workflow = WORKFLOW_PATH.read_text(encoding="utf-8")
    native_job = workflow.split("\n  windows-native:\n", maxsplit=1)[1].split(
        "\n  merge-gate:\n", maxsplit=1
    )[0]
    assert "runs-on: windows-latest" in native_job
    assert "--noconftest" in native_job
    assert "verify-windows-native --junit" in native_job
    assert "retention-days: 7" in native_job
    helper = _load_helper()
    for case in helper.WINDOWS_NATIVE_CASES:
        module = (
            "test_storage_reservation"
            if case in helper.WINDOWS_STORAGE_LEASE_CASES
            else "test_job_runtime_process_tree"
        )
        assert f"tests/{module}.py::{case}" in native_job
    assert "WINDOWS_NATIVE_RESULT: ${{ needs.windows-native.result }}" in workflow
    assert '--windows-native-result "$WINDOWS_NATIVE_RESULT"' in workflow


@pytest.mark.parametrize("result", ["skipped", "failure", "cancelled", ""])
def test_gate_requires_native_windows_application_evidence(helper: ModuleType, result: str) -> None:
    results: dict[str, str] = dict.fromkeys(
        (
            "changes",
            "public-boundary",
            "fast-signal",
            "windows-native",
            "tests",
            "browser-security",
            "quality",
            "typecheck",
            "security",
        ),
        "success",
    )
    results["windows-app"] = result
    failures = helper.gate_failures(code=True, python=True, results=results)
    assert len(failures) == 1
    assert failures[0].startswith("windows-app ")


@pytest.mark.parametrize(
    "disposition",
    [
        "passed",
        "skipped",
        "failure",
        "error",
        "missing_file",
        "missing_required",
        "duplicate_required",
        "malformed",
        "unavailable",
    ],
)
def test_native_app_receipt_requires_every_file_and_named_case(
    helper: ModuleType, tmp_path: Path, disposition: str
) -> None:
    required = sorted(helper.WINDOWS_APP_REQUIRED_CASES)
    files = list(helper.WINDOWS_APP_TEST_FILES)
    entries = [tuple(case.split("::", 1)) for case in required] + [
        (path.removesuffix(".py").replace("/", "."), "test_extra_case") for path in files
    ]
    if disposition == "missing_file":
        dropped = files[-1].removesuffix(".py").replace("/", ".")
        entries = [entry for entry in entries if entry[0] != dropped]
    elif disposition == "missing_required":
        entries.remove(tuple(required[0].split("::", 1)))
    elif disposition == "duplicate_required":
        entries.append(tuple(required[0].split("::", 1)))
    child = (
        ""
        if disposition in {"passed", "missing_file", "missing_required", "duplicate_required"}
        else f"<{disposition}/>"
    )
    cases = "".join(
        f'<testcase classname="{module}" name="{name}">{child if i == 0 else ""}</testcase>'
        for i, (module, name) in enumerate(entries)
    )
    receipt = tmp_path / "native-app.xml"
    receipt.write_text(f"<testsuites><testsuite>{cases}</testsuite></testsuites>", encoding="utf-8")
    if disposition == "malformed":
        receipt.write_text("not XML", encoding="utf-8")
    elif disposition == "unavailable":
        receipt.unlink()
    assert helper.main(["verify-windows-app", "--junit", str(receipt)]) == (
        0 if disposition == "passed" else 1
    )


def test_every_required_windows_case_belongs_to_a_selected_file() -> None:
    helper = _load_helper()
    modules = {path.removesuffix(".py").replace("/", ".") for path in helper.WINDOWS_APP_TEST_FILES}
    assert {case.split("::", 1)[0] for case in helper.WINDOWS_APP_REQUIRED_CASES} == modules


@pytest.mark.parametrize(
    "path",
    (
        "tests/test_dcf_retained_availability.py",
        "tests/test_dcf_canonical_statements.py",
        "tests/test_redesign_input_evidence.py",
        "tests/test_redesign_workbook_evidence.py",
    ),
)
def test_windows_app_requires_dcf_evidence_cases(helper: ModuleType, path: str) -> None:
    assert path in helper.WINDOWS_APP_TEST_FILES
    module = path.removesuffix(".py").replace("/", ".")
    assert any(case.startswith(module + "::") for case in helper.WINDOWS_APP_REQUIRED_CASES)


def test_windows_app_job_runs_the_harness_on_hosted_windows_and_is_required() -> None:
    workflow = WORKFLOW_PATH.read_text(encoding="utf-8")
    app_job = workflow.split("\n  windows-app:\n", maxsplit=1)[1].split(
        "\n  windows-native:\n", maxsplit=1
    )[0]
    assert "runs-on: windows-latest" in app_job
    assert "--noconftest" not in app_job
    run_step = app_job.split("      - name: Run Windows-relevant application tests", 1)[1]
    assert "TEMP: ${{ runner.temp }}" in run_step
    assert "TMP: ${{ runner.temp }}" in run_step
    recipe = (REPO_ROOT / ".github/scripts/install_windows_app.ps1").read_text()
    assert "--require-hashes -r requirements.lock" in recipe
    assert "run: ./.github/scripts/install_windows_app.ps1" in app_job
    assert "verify-windows-app --junit" in app_job
    assert "retention-days: 7" in app_job
    for path in _load_helper().WINDOWS_APP_TEST_FILES:
        assert path in app_job
    assert "WINDOWS_APP_RESULT: ${{ needs.windows-app.result }}" in workflow
    assert '--windows-app-result "$WINDOWS_APP_RESULT"' in workflow


def test_gate_never_hides_failed_or_cancelled_jobs(helper: ModuleType) -> None:
    assert helper.gate_failures(
        code=False,
        python=False,
        results={
            "changes": "failure",
            "public-boundary": "success",
            "fast-signal": "skipped",
            "tests": "skipped",
            "windows-native": "skipped",
            "windows-app": "skipped",
            "browser-security": "skipped",
            "quality": "skipped",
            "typecheck": "skipped",
            "security": "cancelled",
        },
    ) == [
        "changes must succeed; got failure",
        "security finished with cancelled",
    ]


def test_gate_rejects_skipped_change_classification(helper: ModuleType) -> None:
    assert helper.gate_failures(
        code=False,
        python=False,
        results={
            "changes": "skipped",
            "public-boundary": "success",
            "fast-signal": "skipped",
            "tests": "skipped",
            "windows-native": "skipped",
            "windows-app": "skipped",
            "browser-security": "skipped",
            "quality": "skipped",
            "typecheck": "skipped",
            "security": "skipped",
        },
    ) == ["changes must succeed; got skipped"]


def test_gate_always_requires_public_boundary(helper: ModuleType) -> None:
    gate_failures = helper.gate_failures(
        code=False,
        python=False,
        results={
            "changes": "success",
            "public-boundary": "skipped",
            "fast-signal": "skipped",
            "tests": "skipped",
            "windows-native": "skipped",
            "windows-app": "skipped",
            "browser-security": "skipped",
            "quality": "skipped",
            "typecheck": "skipped",
            "security": "skipped",
        },
    )
    assert gate_failures == ["public-boundary must succeed; got skipped"]


def test_verify_command_requires_browser_security_for_code(helper: ModuleType) -> None:
    def argv(*, browser_result: str) -> list[str]:
        return [
            "verify",
            "--code",
            "true",
            "--python",
            "true",
            "--changes-result",
            "success",
            "--public-boundary-result",
            "success",
            "--fast-signal-result",
            "success",
            "--tests-result",
            "success",
            "--windows-native-result",
            "success",
            "--windows-app-result",
            "success",
            "--browser-security-result",
            browser_result,
            "--quality-result",
            "success",
            "--typecheck-result",
            "success",
            "--security-result",
            "success",
        ]

    assert helper.main(argv(browser_result="success")) == 0
    assert helper.main(argv(browser_result="skipped")) == 1


def test_pyright_count_requires_valid_non_negative_integer(helper: ModuleType) -> None:
    assert helper.pyright_error_count({"summary": {"errorCount": 3070}}) == 3070
    payloads: tuple[object, ...] = (
        {},
        {"summary": {}},
        {"summary": {"errorCount": True}},
        {"summary": {"errorCount": "0"}},
        {"summary": {"errorCount": -1}},
    )
    for payload in payloads:
        with pytest.raises(ValueError):
            helper.pyright_error_count(payload)


def test_pyright_diff_ignores_worktree_root_and_source_location(helper: ModuleType) -> None:
    base = {
        "summary": {"errorCount": 1},
        "generalDiagnostics": [
            {
                "file": "/tmp/base/src/example.py",
                "severity": "error",
                "message": "Type of value is unknown",
                "rule": "reportUnknownVariableType",
                "range": {"start": {"line": 1, "character": 2}},
            }
        ],
    }
    head = {
        "summary": {"errorCount": 1},
        "generalDiagnostics": [
            {
                "file": "/home/runner/head/src/example.py",
                "severity": "error",
                "message": "Type of value is unknown",
                "rule": "reportUnknownVariableType",
                "range": {"start": {"line": 20, "character": 8}},
            }
        ],
    }

    assert (
        helper.pyright_new_errors(
            base,
            head,
            base_root=Path("/tmp/base"),
            head_root=Path("/home/runner/head"),
        )
        == []
    )


def test_pyright_diff_is_a_multiset_and_catches_new_errors(helper: ModuleType) -> None:
    diagnostic = {
        "file": "/repo/src/example.py",
        "severity": "error",
        "message": "Type of value is unknown",
        "rule": "reportUnknownVariableType",
    }
    base = {"summary": {"errorCount": 1}, "generalDiagnostics": [diagnostic]}
    head = {
        "summary": {"errorCount": 2},
        "generalDiagnostics": [diagnostic, diagnostic],
    }

    assert helper.pyright_new_errors(
        base,
        head,
        base_root=Path("/repo"),
        head_root=Path("/repo"),
    ) == [("src/example.py", "reportUnknownVariableType", "Type of value is unknown")]


def test_pyright_diff_rejects_incomplete_diagnostics(helper: ModuleType) -> None:
    invalid: object = {"summary": {"errorCount": 1}, "generalDiagnostics": []}
    valid: object = {"summary": {"errorCount": 0}, "generalDiagnostics": []}
    with pytest.raises(ValueError):
        helper.pyright_new_errors(
            invalid,
            valid,
            base_root=Path("/repo"),
            head_root=Path("/repo"),
        )


def test_cached_base_scan_yields_identical_pyright_diff_verdict(helper: ModuleType) -> None:
    """Workstream C2: the base scan is reusable across runs because the
    pyright-diff verdict is independent of the absolute location each scan
    ran in — provided the root prefix stored WITH the cached JSON is the one
    passed to --base-root."""
    head_root = Path("/home/runner/work/repo")

    def base_payload(root: Path) -> dict[str, object]:
        shared = {
            "file": str(root / "src/legacy.py"),
            "severity": "error",
            "rule": "reportUnknownParameterType",
            "message": f'cannot resolve "{root / "src/legacy.py"}"',
        }
        return {"summary": {"errorCount": 1}, "generalDiagnostics": [shared]}

    fresh_root = Path("/runner/_temp/base-pyright")  # today's base worktree
    cached_root = Path("/runner/_temp/base-pyright-20260908")  # the stored root
    head: object = {
        "summary": {"errorCount": 2},
        "generalDiagnostics": [
            {
                "file": str(head_root / "src/legacy.py"),
                "severity": "error",
                "rule": "reportUnknownParameterType",
                "message": f'cannot resolve "{head_root / "src/legacy.py"}"',
            },
            {
                "file": str(head_root / "src/new_code.py"),
                "severity": "error",
                "rule": "reportUnknownParameterType",
                "message": 'cannot resolve "new_code"',
            },
        ],
    }
    expected = [("src/new_code.py", "reportUnknownParameterType", 'cannot resolve "new_code"')]

    # Cache miss: a fresh base scan in today's worktree.
    fresh_verdict = helper.pyright_new_errors(
        base_payload(fresh_root), head, base_root=fresh_root, head_root=head_root
    )
    # Cache hit: the stored JSON bytes from an earlier run, paired with the
    # base-root string that was stored alongside them.
    cached_verdict = helper.pyright_new_errors(
        base_payload(cached_root), head, base_root=cached_root, head_root=head_root
    )
    assert fresh_verdict == cached_verdict == expected
    # A base-root that does not match the JSON it accompanies fails closed —
    # exactly why the workflow must pass the STORED root on a cache hit.
    with pytest.raises(ValueError, match="outside repository root"):
        helper.pyright_new_errors(
            base_payload(fresh_root), head, base_root=cached_root, head_root=head_root
        )


def _test_durations(
    helper: ModuleType,
    seconds: dict[str, float],
    *,
    default_seconds: float = 2.0,
    shard_by_file: dict[str, str] | None = None,
):
    """Build a TestDurations from per-file seconds, pinning via the generator.

    Mirrors the checked-in table's production flow: `pack_test_shards` derives
    the pinned `shard` field from the durations, exactly like the seed script.
    """
    if shard_by_file is None:
        base = helper.TestDurations(
            labels=helper.SHARD_LABELS,
            default_seconds=default_seconds,
            seconds_by_file=seconds,
            shard_by_file={},
        )
        packed = helper.pack_test_shards(list(seconds), base)
        shard_by_file = {path: label for label, paths in packed.items() for path in paths}
    return helper.TestDurations(
        labels=helper.SHARD_LABELS,
        default_seconds=default_seconds,
        seconds_by_file=seconds,
        shard_by_file=shard_by_file,
    )


def test_ci_test_partitions_are_exhaustive_disjoint_and_nonempty(helper: ModuleType) -> None:
    files = [f"tests/test_{index:04d}.py" for index in range(257)]
    seconds = {path: 3.0 for path in files}
    durations = _test_durations(helper, seconds)
    partitions: dict[str, list[str]] = {}
    for label in helper.SHARD_LABELS:
        partitions[label] = helper.select_test_files(files, shard_label=label, durations=durations)

    assert all(partitions[label] for label in helper.SHARD_LABELS)
    assert Counter(path for partition in partitions.values() for path in partition) == Counter(
        files
    )
    assert sum(len(v) for v in partitions.values()) == len(files)


def test_shard_assignment_is_deterministic_and_pins_known_files(helper: ModuleType) -> None:
    files = [f"tests/test_{index:04d}.py" for index in range(40)]
    seconds = {path: float((index % 7) + 1) for index, path in enumerate(files)}
    durations = _test_durations(helper, seconds)

    first = {
        label: helper.select_test_files(files, shard_label=label, durations=durations)
        for label in helper.SHARD_LABELS
    }
    second = {
        label: helper.select_test_files(files, shard_label=label, durations=durations)
        for label in helper.SHARD_LABELS
    }
    assert first == second

    # A known file whose cost did not change stays in its pinned shard even
    # when an unrelated new file is added.
    extended = [*files, "tests/test_new_file_smoke.py"]
    after = {
        label: helper.select_test_files(extended, shard_label=label, durations=durations)
        for label in helper.SHARD_LABELS
    }
    for path in files:
        assert path in after[durations.shard_by_file[path]]


def test_new_files_get_a_deterministic_default_shard(helper: ModuleType) -> None:
    files = [f"tests/test_{index:04d}.py" for index in range(40)]
    seconds = {path: float((index % 7) + 1) for index, path in enumerate(files)}
    durations = _test_durations(helper, seconds, default_seconds=1.5)
    extended = [*files, "tests/test_brand_new_smoke.py", "tests/test_other_new_smoke.py"]

    forward = {
        label: helper.select_test_files(extended, shard_label=label, durations=durations)
        for label in helper.SHARD_LABELS
    }
    backward = {
        label: helper.select_test_files(
            list(reversed(extended)), shard_label=label, durations=durations
        )
        for label in helper.SHARD_LABELS
    }
    # Independent of caller input order, and the two new files land somewhere
    # while every known file keeps its pin.
    assert {k: set(v) for k, v in forward.items()} == {k: set(v) for k, v in backward.items()}
    assert all(path in forward[durations.shard_by_file[path]] for path in files)


def test_pack_test_shards_is_duration_aware_and_deterministic(helper: ModuleType) -> None:
    files = [f"tests/test_{index:04d}.py" for index in range(20)]
    seconds = {path: float((index % 5) + 1) * 3 for index, path in enumerate(files)}
    durations = _test_durations(helper, seconds, shard_by_file={})

    forward = helper.pack_test_shards(files, durations)
    backward = helper.pack_test_shards(list(reversed(files)), durations)
    assert forward == backward
    total = sum(seconds.values())
    assert sum(sum(seconds[path] for path in paths) for paths in forward.values()) == total

    heavy = sorted(seconds.items(), key=lambda item: (-item[1], item[0]))[:2]
    shard_of = {path: label for label, paths in forward.items() for path in paths}
    assert shard_of[heavy[0][0]] != shard_of[heavy[1][0]]
    assert shard_of[heavy[0][0]] in helper.SHARD_LABELS


def test_selected_files_start_longest_first(helper: ModuleType) -> None:
    files = [f"tests/test_{index:04d}.py" for index in range(60)]
    seconds = {path: float((index * 7) % 23 + 1) for index, path in enumerate(files)}
    durations = _test_durations(helper, seconds)
    for label in helper.SHARD_LABELS:
        selected = helper.select_test_files(files, shard_label=label, durations=durations)
        assert selected == sorted(selected, key=lambda path: (-seconds[path], path))


def test_durations_regenerate_from_junit_with_canonical_packing(
    helper: ModuleType, tmp_path: Path
) -> None:
    junit = tmp_path / "shard.xml"
    junit.write_text(
        '<testsuites><testsuite name="pytest">'
        '<testcase classname="tests.test_alpha" name="a" time="2.5" />'
        '<testcase classname="tests.test_alpha.TestGroup" name="b" time="1.5" />'
        '<testcase classname="tests.test_beta" name="c" time="0.000" />'
        '<testcase classname="" name="tests.test_gamma" time="0.000">'
        "<skipped>collection skipped</skipped></testcase>"
        "</testsuite></testsuites>",
        encoding="utf-8",
    )
    files = ["tests/test_alpha.py", "tests/test_beta.py", "tests/test_gamma.py"]
    payload = helper.regenerate_test_durations(
        [junit], files, source="unit fixture", generated="2026-11-22"
    )
    assert payload["files"]["tests/test_alpha.py"]["seconds"] == 4.0
    assert payload["files"]["tests/test_beta.py"]["seconds"] == 0.001
    output = tmp_path / "durations.json"
    output.write_text(json.dumps(payload), encoding="utf-8")
    durations = helper.load_test_durations(output)
    repacked = helper.pack_test_shards(sorted(durations.seconds_by_file), durations)
    assert {
        path: label for label, paths in repacked.items() for path in paths
    } == durations.shard_by_file

    unknown = tmp_path / "unknown.xml"
    unknown.write_text(
        '<testsuites><testcase classname="tests.test_missing" name="x" time="1" /></testsuites>',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="names no test file"):
        helper.junit_seconds_by_file([unknown], files)


def test_pack_reproduces_the_checked_in_pinned_assignment(helper: ModuleType) -> None:
    """The checked-in table is the output of the generator: re-packing the
    table's own durations must reproduce every pinned file's shard."""
    durations = helper.load_test_durations(REPO_ROOT / ".github" / "test-durations.json")
    files = sorted(durations.seconds_by_file)
    repacked = helper.pack_test_shards(files, durations)
    repacked_label = {path: label for label, paths in repacked.items() for path in paths}
    assert {path: durations.shard_by_file[path] for path in files} == repacked_label


def test_checked_in_durations_file_is_valid_and_current(helper: ModuleType) -> None:
    durations_path = REPO_ROOT / ".github" / "test-durations.json"
    assert durations_path.is_file()
    durations = helper.load_test_durations(durations_path)
    assert durations.labels == helper.SHARD_LABELS
    assert durations.default_seconds > 0

    files = sorted("tests/" + path.name for path in (REPO_ROOT / "tests").glob("test_*.py"))
    assert files
    covered = sum(1 for path in files if path in durations.seconds_by_file)
    assert covered / len(files) >= 0.9

    partitions = [
        helper.select_test_files(files, shard_label=label, durations=durations)
        for label in helper.SHARD_LABELS
    ]
    assert all(partitions)
    assert Counter(path for partition in partitions for path in partition) == Counter(files)


def test_workflow_uses_native_classifier_and_fail_closed_aggregate() -> None:
    workflow = WORKFLOW_PATH.read_text(encoding="utf-8")
    assert "dorny/paths-filter" not in workflow
    assert 'git diff --name-only --no-renames -z "$base...$head"' in workflow
    assert 'git diff --name-only --no-renames -z "$PUSH_BEFORE_SHA" "$CURRENT_SHA"' in workflow
    # Static debt is enforced against exhaustive checked-in subsystem ceilings;
    # the retired base-vs-head scanner and its fail-open `|| true` wrapper must
    # not return.
    assert 'pyright --outputjson > "$head_json" 2>/dev/null || true' not in workflow
    assert 'pyright --outputjson ) > "$out" 2>"$log" || true' not in workflow
    assert "ci_gate.py pyright-diff" not in workflow
    assert "execution/enforce_static_quality.py" in workflow
    assert 'pip install "pyright==1.1.414"' in workflow
    assert "ci_gate.py select-tests" in workflow
    assert "errcount || echo 0" not in workflow
    assert "python .github/scripts/ci_gate.py classify" in workflow
    assert "python .github/scripts/ci_gate.py verify" in workflow
    assert "if: ${{ always() }}" in workflow
    assert "name: CI Gate" in workflow
    assert "name: Public Boundary" in workflow
    assert "python execution/verify_public_tree.py" in workflow
    assert (
        "needs: [changes, public-boundary, fast-signal, windows-native, windows-app, tests, browser-security, quality, typecheck, security]"
        in workflow
    )
    assert "PUBLIC_BOUNDARY_RESULT" in workflow


def test_browser_security_job_runs_for_code_without_a_schedule() -> None:
    workflow = WORKFLOW_PATH.read_text(encoding="utf-8")
    job = workflow.split("\n  browser-security:\n", maxsplit=1)[1].split(
        "\n  quality:\n", maxsplit=1
    )[0]
    assert "if: ${{ needs.changes.outputs.code == 'true' }}" in job
    assert "schedule:" not in workflow
    assert "BROWSER_SECURITY_RESULT: ${{ needs.browser-security.result }}" in workflow
    assert '--browser-security-result "$BROWSER_SECURITY_RESULT"' in workflow


def test_typecheck_enforces_changed_files_and_exact_population_ceilings() -> None:
    workflow = WORKFLOW_PATH.read_text(encoding="utf-8")
    typecheck_job = workflow.split("\n  typecheck:\n", maxsplit=1)[1].split(
        "\n  security:\n", maxsplit=1
    )[0]
    assert "actions/cache@" not in typecheck_job
    assert (
        'python -m quality.check_changed --base "$CHECK_BASE" --mode committed --check types'
        in typecheck_job
    )
    assert "execution/enforce_static_quality.py" in typecheck_job
    assert '"pyright==1.1.414"' in typecheck_job
    assert '"playwright>=1.48"' in typecheck_job
    assert "ci_gate.py pyright-diff" not in typecheck_job


def test_typecheck_receipt_is_exact_keyed_and_written_only_by_main() -> None:
    workflow = WORKFLOW_PATH.read_text(encoding="utf-8")
    job = workflow.split("\n  typecheck:\n", maxsplit=1)[1].split("\n  security:\n", maxsplit=1)[0]
    # Every main push leaves a receipt, even one with no Python change.
    assert "github.event_name == 'push'" in job.split("runs-on:", 1)[0]
    restore = job.split("Restore base commit Pyright receipt", 1)[1].split("- name:", 1)[0]
    assert "actions/cache/restore@" in restore
    assert "key: pyright-receipt-v1-${{ steps.base.outputs.sha }}" in restore
    assert "restore-keys" not in job
    assert 'echo "sha=$(git rev-parse HEAD^1)"' in job
    assert '--receipt-in "$RUNNER_TEMP/pyright-receipt.json" --receipt-commit "$BASE_SHA"' in job
    save = job.split("Save this commit's Pyright receipt", 1)[1]
    assert (
        "github.event_name == 'push' && github.ref == 'refs/heads/main' && !inputs.candidate_sha"
        in save
    )
    assert '[ "$EVENT_NAME" = "push" ] && [ -z "$CANDIDATE_SHA" ]' in job
    assert "key: pyright-receipt-v1-${{ github.sha }}" in save


def test_pull_requests_check_changed_files_and_the_queue_checks_every_file() -> None:
    workflow = WORKFLOW_PATH.read_text(encoding="utf-8")
    job = workflow.split("\n  typecheck:\n", maxsplit=1)[1].split("\n  security:\n", maxsplit=1)[0]
    # The changed-file check runs on every event, pull requests included.
    changed = job.split("changed retained files are wholly clean", 1)[1].split("- name:", 1)[0]
    assert "if:" not in changed
    # The population ceilings never run on a pull request: the merge queue
    # runs them on each candidate before it can reach main.
    ceilings = job.split("complete active population is exact", 1)[1].split("- name:", 1)[0]
    assert "if: ${{ github.event_name != 'pull_request' }}" in ceilings
    # Only a queue candidate restores main's receipt to re-check incrementally.
    restore = job.split("Restore base commit Pyright receipt", 1)[1].split("- name:", 1)[0]
    assert "if: ${{ inputs.candidate_sha }}" in restore
    assert '[ -n "$CANDIDATE_SHA" ] && [ "$RECEIPT_HIT" = "true" ]' in ceilings


def test_public_boundary_is_unconditional_and_pre_push_uses_same_guard() -> None:
    workflow = WORKFLOW_PATH.read_text(encoding="utf-8")
    all_refs_workflow = (REPO_ROOT / ".github" / "workflows" / "public-boundary.yml").read_text(
        encoding="utf-8"
    )
    public_job = workflow.split("  public-boundary:\n", maxsplit=1)[1].split(
        "\n  fast-signal:", maxsplit=1
    )[0]
    pre_commit = (REPO_ROOT / ".pre-commit-config.yaml").read_text(encoding="utf-8")
    pre_push = (REPO_ROOT / ".githooks" / "pre-push").read_text(encoding="utf-8")

    assert "needs:" not in public_job
    assert "if:" not in public_job
    assert "python execution/verify_public_tree.py" in public_job
    assert "id: public-tree-boundary" in pre_commit
    assert "entry: python execution/verify_public_tree.py" in pre_commit
    assert "always_run: true" in pre_commit
    assert "stages: [pre-push]" in pre_commit
    assert 'run "$python_bin" execution/verify_public_tree.py' in pre_push
    assert "  pull_request:\n" in all_refs_workflow
    assert "  push:\n" in all_refs_workflow
    assert "branches:" not in all_refs_workflow
    assert "python execution/verify_public_tree.py" in all_refs_workflow


def test_security_job_runs_every_scanner_before_failing_closed() -> None:
    workflow = WORKFLOW_PATH.read_text(encoding="utf-8")

    for step_id in ("pip_audit", "bandit", "detect_secrets", "sbom"):
        assert f"id: {step_id}" in workflow
    assert "Require every security scanner to pass" in workflow
    assert "PIP_AUDIT_OUTCOME" in workflow
    assert "setup-bun" not in workflow
    assert "bun audit" not in workflow
    assert "npm " not in workflow
    assert "BANDIT_OUTCOME" in workflow
    assert "DETECT_SECRETS_OUTCOME" in workflow
    assert "SBOM_OUTCOME" in workflow
    assert "pip-audit -r requirements-browser.lock" in workflow
    assert "cyclonedx-py requirements requirements-browser.lock" in workflow
    assert "always() && hashFiles('sbom-*.cdx.json')" in workflow

    exclude_pattern = _load_secret_scan().EXCLUDE_FILES
    receipt_pattern = (
        r"docs[\\/]quality[\\/]"
        r"(?:architecture-initial-09d35d1a|duplicates-initial-09d35d1a|"
        r"static-baseline|compatibility-baseline)\.json"
    )
    assert receipt_pattern in exclude_pattern
    for receipt in (
        "architecture-initial-09d35d1a.json",
        "duplicates-initial-09d35d1a.json",
        "static-baseline.json",
        "compatibility-baseline.json",
    ):
        assert re.fullmatch(exclude_pattern, f"docs/quality/{receipt}")
    assert not re.fullmatch(exclude_pattern, "docs/quality/policy-enforcement.json")
    assert r"docs[\\/]quality[\\/].*\.json" not in exclude_pattern


def test_test_job_labels_count_and_picker_are_stable() -> None:
    workflow = WORKFLOW_PATH.read_text(encoding="utf-8")
    # The job-name template, the 13 matrix labels, and the picker are the CI
    # surface the aggregate gate keys on; they must not drift from the helper's
    # canonical labels. The old modulo-8 source-shard/split interface is gone.
    assert "name: tests (shard ${{ matrix.label }}/8)" in workflow
    # The labels come from the helper's matrix (test_matrix), never a copy here.
    assert re.findall(r'- \{ label: "([^"]+)" \}', workflow) == []
    assert len(_load_helper().SHARD_LABELS) == 13
    assert "ci_gate.py select-tests" in workflow
    assert "SHARD_LABEL: ${{ matrix.label }}" in workflow
    assert '--shard-label "$SHARD_LABEL"' in workflow
    assert "--durations-file .github/test-durations.json" in workflow
    assert "--source-shard" not in workflow
    assert "--split-count" not in workflow
    assert (
        "needs: [changes, public-boundary, fast-signal, windows-native, windows-app, tests, browser-security, quality, typecheck, security]"
        in workflow
    )


def test_fast_signal_is_required_for_code_without_replacing_the_full_matrix() -> None:
    workflow = WORKFLOW_PATH.read_text(encoding="utf-8")
    fast_job = workflow.split("\n  fast-signal:\n", maxsplit=1)[1].split(
        "\n  tests:\n", maxsplit=1
    )[0]
    assert "name: Fast Signal" in fast_job
    assert "needs.changes.outputs.code == 'true'" in fast_job
    assert "timeout-minutes: 5" in fast_job
    assert "Preload verified SQLite writer runtime" in fast_job
    assert 'assert sqlite3.sqlite_version == "3.53.4"' in fast_job
    assert "PYTHONPATH=src pytest -q -n 0" in fast_job
    for path in (
        "tests/test_smoke.py",
        "tests/test_ci_gate.py",
        "tests/test_filing_xbrl_bridge.py",
        "tests/test_filing_xbrl_normalization_rejection.py",
        "tests/test_sec_filing_xbrl_ingest_guards.py",
        "tests/test_sec_filing_xbrl_manifest_binding.py",
    ):
        assert path in fast_job
    assert "name: tests (shard ${{ matrix.label }}/8)" in workflow
    assert "FAST_SIGNAL_RESULT: ${{ needs.fast-signal.result }}" in workflow
    assert '--fast-signal-result "$FAST_SIGNAL_RESULT"' in workflow


def test_env_caches_sqlite_by_version_os_and_recipe_hash() -> None:
    workflow = WORKFLOW_PATH.read_text(encoding="utf-8")
    cache = "actions/cache@0057852bfaa89a56745cba8c7296529d2fc39830 # v4"
    assert cache in workflow
    assert "id: sqlite_cache" in workflow
    assert "path: ${{ runner.temp }}/sqlite-3.53.4" in workflow
    assert (
        "key: ci-sqlite-${{ runner.os }}-${{ runner.arch }}-3.53.4-${{ hashFiles('.github/scripts/build_sqlite.sh') }}"
        in workflow
    )
    # Both the tests matrix and Design Sync gate the build on a miss and keep
    # the preload/verify step unconditional.
    assert "if: steps.sqlite_cache.outputs.cache-hit != 'true'" in workflow
    assert "Preload verified SQLite writer runtime" in workflow
    assert 'assert sqlite3.sqlite_version == "3.53.4"' in workflow
    _assert_only_verified_caches_use_prefix_restore(workflow)


def _assert_only_verified_caches_use_prefix_restore(workflow: str) -> None:
    # Environment caches restore by exact key only. Two caches restore by prefix
    # because each entry is verified before use: secret-scan entries are salted
    # and checked in leak_scan.py, and a test impact receipt names its commit,
    # which test_selection.py uses only when it is an ancestor of the base.
    restore_lines = [line.strip() for line in workflow.splitlines() if "restore-keys:" in line]
    assert sorted(restore_lines) == [
        "restore-keys: secret-scan-v1-",
        "restore-keys: test-impact-v1-",
    ]


def test_env_caches_virtualenv_by_locks_and_python_version() -> None:
    workflow = WORKFLOW_PATH.read_text(encoding="utf-8")
    assert "id: venv_cache" in workflow
    assert "path: ${{ runner.temp }}/ci-venv" in workflow
    assert "id: pyver" in workflow
    assert "steps.pyver.outputs.version" in workflow
    assert (
        "key: ci-venv-${{ runner.os }}-py${{ steps.pyver.outputs.version }}"
        "-lock${{ hashFiles('requirements.lock') }}-pyproject${{ hashFiles('pyproject.toml') }}"
        in workflow
    )
    assert (
        "key: ci-venv-${{ runner.os }}-py${{ steps.pyver.outputs.version }}"
        "-lock${{ hashFiles('requirements.lock') }}-browser${{ hashFiles('requirements-browser.lock') }}"
        "-pyproject${{ hashFiles('pyproject.toml') }}" in workflow
    )
    # Install is skipped on a hit; the venv bin dir is prepended to PATH so
    # every later step runs inside the cached environment.
    assert "if: steps.venv_cache.outputs.cache-hit != 'true'" in workflow
    assert 'python -m venv "$RUNNER_TEMP/ci-venv"' in workflow
    assert 'echo "$RUNNER_TEMP/ci-venv/bin" >> "$GITHUB_PATH"' in workflow
    # The Playwright browser is a separate artifact and must install on every
    # run (it is not venv-cached) — the canary matrix needs Chromium.
    assert "python -m playwright install --with-deps --only-shell chromium" in workflow
    _assert_only_verified_caches_use_prefix_restore(workflow)


def test_durations_report_is_available_on_the_real_table(
    helper: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import io
    import sys

    files = sorted("tests/" + path.name for path in (REPO_ROOT / "tests").glob("test_*.py"))
    monkeypatch.setattr(sys, "stdin", io.StringIO("\n".join(files) + "\n"))
    code = helper.main(
        [
            "durations-report",
            "--durations-file",
            os.fspath(REPO_ROOT / ".github" / "test-durations.json"),
        ]
    )
    assert code == 0


def test_durations_loader_fails_closed_on_malformed_input(
    helper: ModuleType, tmp_path: Path
) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text('{"schema": 1, "labels": ["nope"], "default_seconds": 1}', encoding="utf-8")
    with pytest.raises(ValueError, match="shard labels"):
        helper.load_test_durations(bad)

    bad.write_text("not json", encoding="utf-8")
    with pytest.raises(ValueError, match="invalid durations file"):
        helper.load_test_durations(bad)

    unknown_label = tmp_path / "unknown.json"
    payload = {
        "schema": 1,
        "labels": list(helper.SHARD_LABELS),
        "default_seconds": 2.0,
        "files": {"tests/test_x.py": {"seconds": 1.0, "shard": helper.SHARD_LABELS[0]}},
    }
    unknown_label.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="unknown shard label"):
        helper.select_test_files(
            ["tests/test_x.py", "tests/test_y.py"],
            shard_label="not-a-shard",
            durations=helper.load_test_durations(unknown_label),
        )


def test_env_cache_miss_falls_back_to_build_and_install() -> None:
    workflow = WORKFLOW_PATH.read_text(encoding="utf-8")
    recipe = (REPO_ROOT / ".github/scripts/build_sqlite.sh").read_text(encoding="utf-8")
    assert "https://www.sqlite.org/2026/sqlite-amalgamation-3530400.zip" in recipe
    assert (
        "628a44cfe82c66aed1ccbbe85a562d2e33ebe64b3288981ed76285612227934e"  # pragma: allowlist secret
        in recipe
    )
    assert "-DSQLITE_ENABLE_FTS5" in recipe
    assert "gcc -O2 -fPIC -shared -pthread" in recipe
    assert "--retry" not in recipe
    assert workflow.count("run: bash .github/scripts/build_sqlite.sh") == 3
    # pip install falls back to the full hash-pinned install into the venv.
    assert (
        '"$RUNNER_TEMP/ci-venv/bin/pip" install --require-hashes -r requirements.lock' in workflow
    )
    assert '"$RUNNER_TEMP/ci-venv/bin/pip" install -e .[dev]' in workflow
    assert (
        '"$RUNNER_TEMP/ci-venv/bin/pip" install --require-hashes -r requirements-browser.lock'
        in workflow
    )


def test_security_browser_canary_has_one_mandatory_ci_owner() -> None:
    workflow = WORKFLOW_PATH.read_text(encoding="utf-8")
    assert "grep -v '^tests/test_browser_security_canary.py$'" in workflow
    job = workflow.split("\n  browser-security:\n", 1)[1].split("\n  quality:\n", 1)[0]
    assert "python -m pytest -q -n 0 tests/test_browser_security_canary.py" in job
    assert "continue-on-error" not in job
    assert "Ensure data dir exists" not in workflow
    assert "one migrated template per worker" in workflow


def _load_secret_scan() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "secret_scan_under_test", REPO_ROOT / ".github" / "scripts" / "leak_scan.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _secret_scan_repo(tmp_path: Path, files: dict[str, str]) -> Path:
    root = tmp_path / "repo"
    (root / ".github" / "scripts").mkdir(parents=True)
    shutil.copyfile(
        REPO_ROOT / ".github" / "scripts" / "quality_hash_filter.py",
        root / ".github" / "scripts" / "quality_hash_filter.py",
    )
    for name, text in files.items():
        (root / name).write_text(text, encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    return root


def _install_recording_scanner(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Put a fake ``detect-secrets-hook`` first on PATH that records its files."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    scanner = bin_dir / "detect-secrets-hook"
    scanner.write_text(
        f"#!{sys.executable}\n"
        "import json, os, pathlib, sys\n"
        "paths = sys.argv[sys.argv.index('--') + 1:]\n"
        "pathlib.Path(os.environ['SCAN_OUTPUT'], str(os.getpid())).write_text(json.dumps(paths))\n"
        "raise SystemExit(1 if any('bad' in path for path in paths) else 0)\n",
        encoding="utf-8",
    )
    scanner.chmod(0o755)
    monkeypatch.setenv("PATH", os.pathsep.join((str(bin_dir), os.environ["PATH"])))


def _scanned(output: Path) -> Counter[str]:
    scanned = Counter(
        path for receipt in output.iterdir() for path in json.loads(receipt.read_text())
    )
    for receipt in output.iterdir():
        receipt.unlink()
    return scanned


def test_secret_scan_covers_every_file_then_skips_only_proven_clean_blobs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    helper = _load_secret_scan()
    files = {f"file {index}.py": f"VALUE = {index}\n" for index in range(70)}
    root = _secret_scan_repo(tmp_path, files)
    tracked = {path for path, _blob in helper.tracked_blobs(root)}
    output = tmp_path / "output"
    output.mkdir()
    monkeypatch.setenv("SCAN_OUTPUT", str(output))
    cache = tmp_path / "cache.json"
    _install_recording_scanner(tmp_path, monkeypatch)

    def run(*, write_cache: bool, version: str = "1.5.0") -> int:
        return helper.scan(
            root,
            cache,
            detector_version=version,
            jobs=2,
            write_cache=write_cache,
        )

    # Cold: every tracked file is scanned exactly once.
    assert run(write_cache=True) == 0
    assert _scanned(output) == Counter(tracked)

    # Warm: nothing is rescanned; an edited file is rescanned alone.
    assert run(write_cache=False) == 0
    assert _scanned(output) == Counter()
    (root / "file 3.py").write_text("VALUE = 'changed'\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    assert run(write_cache=False) == 0
    assert _scanned(output) == Counter({"file 3.py": 1})

    # A different detector version (or any salt input) discards the cache.
    assert run(write_cache=False, version="9.9.9") == 0
    assert _scanned(output) == Counter(tracked)


def test_secret_scan_failure_fails_closed_and_is_never_cached(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    helper = _load_secret_scan()
    root = _secret_scan_repo(tmp_path, {"good.py": "A = 1\n", "bad file.py": "B = 2\n"})
    output = tmp_path / "output"
    output.mkdir()
    monkeypatch.setenv("SCAN_OUTPUT", str(output))
    cache = tmp_path / "cache.json"
    _install_recording_scanner(tmp_path, monkeypatch)
    kwargs = {
        "detector_version": "1.5.0",
        "jobs": 2,
        "write_cache": True,
    }
    assert helper.scan(root, cache, **kwargs) == 1
    assert not cache.exists()
    _scanned(output)
    assert helper.scan(root, cache, **kwargs) == 1
    assert "bad file.py" in _scanned(output)


def test_secret_scan_real_detector_rejects_a_private_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    helper = _load_secret_scan()
    # The real hook is installed beside the interpreter running the tests.
    monkeypatch.setenv(
        "PATH", os.pathsep.join((str(Path(sys.executable).parent), os.environ["PATH"]))
    )
    header = "-----BEGIN " + "RSA PRIVATE KEY-----"
    root = _secret_scan_repo(tmp_path, {"key.txt": f"{header}\nMIIB\n"})
    cache = tmp_path / "cache.json"
    result = helper.scan(
        root,
        cache,
        detector_version="real",
        jobs=1,
        write_cache=True,
    )
    assert result == 1
    assert not cache.exists()


def test_workflow_saves_the_secret_scan_cache_only_from_main_pushes() -> None:
    workflow = WORKFLOW_PATH.read_text(encoding="utf-8")
    security = workflow.split("\n  security:\n", 1)[1].split("\n  windows-app:\n", 1)[0]
    assert "python .github/scripts/leak_scan.py" in security
    assert "detect-secrets-hook" not in security
    trusted = "github.event_name == 'push' && github.ref == 'refs/heads/main'"
    save = security.split("- name: Save detect-secrets clean-file cache", 1)[1]
    assert trusted in save.split("\n      - name:", 1)[0]
    assert f"TRUSTED_CACHE_WRITE: ${{{{ {trusted} }}}}" in security
    assert "actions/cache/restore@" in security


def test_sqlite_build_rejects_corrupt_archive_before_compiling(tmp_path: Path) -> None:
    binaries = tmp_path / "bin"
    binaries.mkdir()
    curl = binaries / "curl"
    curl.write_text(
        '#!/bin/sh\nfor argument; do output=$argument; done\nprintf corrupt > "$output"\n',
        encoding="utf-8",
    )
    curl.chmod(0o755)
    result = subprocess.run(
        ["bash", str(REPO_ROOT / ".github/scripts/build_sqlite.sh")],
        check=False,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "RUNNER_TEMP": str(tmp_path),
            "PATH": os.pathsep.join(
                (str(binaries), str(Path(sys.executable).parent), os.environ["PATH"])
            ),
        },
    )
    assert result.returncode != 0
    assert "SQLite archive SHA3-256 mismatch" in result.stderr
    assert not (tmp_path / "sqlite-3.53.4/libsqlite3.so.0").exists()


def test_every_ci_checkout_has_complete_blobless_history() -> None:
    for path in (WORKFLOW_PATH, WORKFLOW_PATH.with_name("public-boundary.yml")):
        workflow = path.read_text()
        checkouts = re.findall(
            r"      - uses: actions/checkout@[^\n]+\n(.*?)(?=      - |\Z)",
            workflow,
            re.DOTALL,
        )
        assert checkouts, path
        for checkout in checkouts:
            assert "filter: blob:none" in checkout, path
            assert "fetch-depth: 0" in checkout, path


def test_partial_clone_materializes_the_accepted_replay_blob_before_tests() -> None:
    workflow = (
        WORKFLOW_PATH.read_text().split("\n  tests:\n", 1)[1].split("\n  browser-security:\n", 1)[0]
    )
    fixture_source = (REPO_ROOT / "tests/test_evidence_bundle.py").read_text()
    blob = re.search(r'ACCEPTED_BASELINE_BLOB = "([0-9a-f]{40})"', fixture_source)
    assert blob is not None
    assert f"git cat-file blob {blob.group(1)} >/dev/null" in workflow
    assert workflow.index("git cat-file blob") < workflow.index("actions/setup-python@")


def test_quality_reuses_the_exact_main_seeded_core_environment_and_refreshes_ruff() -> None:
    workflow = WORKFLOW_PATH.read_text()
    quality = workflow.split("\n  quality:\n", 1)[1].split("\n  typecheck:\n", 1)[0]
    fast = workflow.split("\n  fast-signal:\n", 1)[1].split("\n  tests:\n", 1)[0]
    pattern = r"key: (ci-venv-[^\n]+)"
    assert re.findall(pattern, quality) == re.findall(pattern, fast)
    assert "restore-keys:" not in quality
    assert "path: ${{ runner.temp }}/ci-venv" in quality
    assert "uses: actions/cache/restore@" in quality
    assert "uses: actions/cache@" not in quality
    assert "cache: pip" not in quality
    assert "if: steps.venv_cache.outputs.cache-hit != 'true'" in quality
    refresh = quality.split("      - name: Refresh the quality scanner\n", 1)[1].split(
        "      - name:", 1
    )[0]
    assert "if:" not in refresh
    assert "run: python -m pip install --upgrade ruff" in refresh


def test_windows_environment_cache_is_exact_and_does_not_skip_native_tests() -> None:
    workflow = WORKFLOW_PATH.read_text()
    app = workflow.split("\n  windows-app:\n", 1)[1].split("\n  windows-native:\n", 1)[0]
    for identity in (
        "runner.os",
        "runner.arch",
        "steps.pyver.outputs.version",
        "hashFiles('requirements.lock')",
        "hashFiles('pyproject.toml')",
        "hashFiles('.github/scripts/install_windows_app.ps1')",
    ):
        assert identity in app
    assert "restore-keys:" not in app
    assert "cache: pip" not in app
    assert "if: steps.windows_venv_cache.outputs.cache-hit != 'true'" in app
    checks = app.split("      - name: Verify restored Windows test dependencies\n", 1)[1]
    assert "if: steps.windows_venv_cache" not in checks
    assert "-m pip check" in checks
    assert "Run Windows-relevant application tests" in checks
    assert "verify-windows-app --junit" in checks


def test_full_verdicts_get_every_shard_with_its_pinned_share(helper: ModuleType) -> None:
    durations = helper.load_test_durations(REPO_ROOT / ".github" / "test-durations.json")
    for lines in ([], ["FULL\tnot a pull request"], ["FULL\tci.yml is a shared test input"]):
        run, matrix = helper.test_matrix(lines, durations)
        assert run is True
        assert matrix["include"] == [{"label": label, "files": ""} for label in helper.SHARD_LABELS]


def test_a_selected_subset_runs_in_just_enough_shards(helper: ModuleType) -> None:
    durations = helper.load_test_durations(REPO_ROOT / ".github" / "test-durations.json")
    known = sorted(
        durations.seconds_by_file, key=lambda path: (durations.seconds_by_file[path], path)
    )
    small = known[:5]
    run, matrix = helper.test_matrix(
        ["SELECTED\t5 files", *small, helper.BROWSER_CANARY], durations
    )
    assert run is True
    assert [row["label"] for row in matrix["include"]] == ["1"]
    assert sorted(matrix["include"][0]["files"].split("\n")) == small
    # A large subset spreads over more shards; every file runs exactly once.
    large = known[-60:]
    total = sum(durations.duration_seconds(path) for path in large)
    run, matrix = helper.test_matrix(["SELECTED\tmany", *large], durations)
    expected = min(len(helper.SHARD_LABELS), math.ceil(total / helper.SELECTED_SHARD_SECONDS))
    assert [row["label"] for row in matrix["include"]] == list(helper.SHARD_LABELS[:expected])
    placed = [path for row in matrix["include"] for path in row["files"].split("\n")]
    assert sorted(placed) == sorted(large)
    for row in matrix["include"]:
        files = row["files"].split("\n")
        assert files == sorted(files, key=lambda path: (-durations.duration_seconds(path), path))


def test_an_empty_selection_runs_no_shard_and_the_gate_allows_it(helper: ModuleType) -> None:
    durations = helper.load_test_durations(REPO_ROOT / ".github" / "test-durations.json")
    run, _ = helper.test_matrix(["SELECTED\t0 files", helper.BROWSER_CANARY], durations)
    assert run is False
    results = dict.fromkeys(("changes", "public-boundary", *helper.CONDITIONAL_JOBS), "success") | {
        "tests": "skipped"
    }
    assert helper.gate_failures(code=True, python=True, results=results, tests=False) == []
    assert helper.gate_failures(code=True, python=True, results=results)
    assert helper.gate_failures(code=True, python=True, results=results, tests=True)


def test_the_windows_app_job_runs_only_when_selection_needs_it(helper: ModuleType) -> None:
    windows_file = helper.WINDOWS_APP_TEST_FILES[0]
    assert helper.windows_app_needed(["FULL\tnot a pull request"])
    assert helper.windows_app_needed([])
    assert helper.windows_app_needed(["SELECTED\t2 files", "tests/test_other.py", windows_file])
    assert not helper.windows_app_needed(["SELECTED\t1 file", "tests/test_other.py"])
    assert not helper.windows_app_needed(["SELECTED\t0 files"])
    # A Windows file chosen only because it scans the repository does not
    # run the job; chosen for the change itself, or with no reason, it does.
    scan = f"{windows_file}\t{helper.SCANS_REPOSITORY}"
    assert not helper.windows_app_needed(
        ["SELECTED\t2 files", "tests/test_other.py\tchanged", scan]
    )
    assert helper.windows_app_needed(["SELECTED\t1 file", f"{windows_file}\truns src/x.py:f"])
    assert helper.windows_app_needed(["SELECTED\t1 file", windows_file])
    results = dict.fromkeys(("changes", "public-boundary", *helper.CONDITIONAL_JOBS), "success") | {
        "windows-app": "skipped"
    }
    assert helper.gate_failures(code=True, python=True, results=results, windows_app=False) == []
    assert helper.gate_failures(code=True, python=True, results=results)
    assert helper.gate_failures(code=True, python=True, results=results, windows_app=True)
    failed = results | {"windows-app": "failure"}
    assert helper.gate_failures(code=True, python=True, results=failed, windows_app=False)
    workflow = WORKFLOW_PATH.read_text(encoding="utf-8")
    app_job = workflow.split("\n  windows-app:\n", 1)[1].split("\n  windows-native:\n", 1)[0]
    assert "needs.changes.outputs.windows_app != 'false'" in app_job
    assert "windows_app: ${{ steps.tests.outputs.windows_app }}" in workflow
    assert "WINDOWS_APP_NEEDED: ${{ needs.changes.outputs.windows_app || 'true' }}" in workflow
    assert '--windows-app "$WINDOWS_APP_NEEDED"' in workflow


def test_the_gate_and_selection_share_the_repository_scan_reason(helper: ModuleType) -> None:
    from quality import test_selection

    assert helper.SCANS_REPOSITORY == test_selection.SCANS_REPOSITORY
    durations = helper.load_test_durations(REPO_ROOT / ".github" / "test-durations.json")
    _, matrix = helper.test_matrix(
        ["SELECTED\t1 file", f"tests/test_ci_gate.py\t{helper.SCANS_REPOSITORY}"], durations
    )
    assert matrix == {
        "include": [{"label": helper.SHARD_LABELS[0], "files": "tests/test_ci_gate.py"}]
    }


def test_the_test_matrix_command_reports_the_windows_app_decision(
    helper: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "output"
    durations = str(REPO_ROOT / ".github" / "test-durations.json")
    for selection, expected in (
        ("FULL\tnot a pull request\n", "windows_app=true"),
        ("SELECTED\t1 file\ntests/test_ci_gate.py\n", "windows_app=false"),
        (f"SELECTED\t1 file\n{helper.WINDOWS_APP_TEST_FILES[0]}\n", "windows_app=true"),
    ):
        output.write_text("")
        monkeypatch.setattr("sys.stdin", io.StringIO(selection))
        assert (
            helper.main(
                ["test-matrix", "--github-output", str(output), "--durations-file", durations]
            )
            == 0
        )
        assert expected in output.read_text().splitlines()


def test_pull_requests_select_tests_from_main_recorded_impact() -> None:
    workflow = WORKFLOW_PATH.read_text(encoding="utf-8")
    changes_job = workflow[
        workflow.index("\n  changes:\n") : workflow.index("\n  public-boundary:\n")
    ]
    tests_job = workflow[workflow.index("\n  tests:\n") : workflow.index("\n  browser-security:\n")]
    # Check Changed Paths selects once, only for a pull request that changes
    # code, from the base commit's receipt or main's newest one.
    assert "python -m quality.test_selection --base HEAD^1" in changes_job
    assert "key: test-impact-v1-${{ steps.base.outputs.sha }}" in changes_job
    assert "restore-keys: test-impact-v1-\n" in changes_job
    assert '[ "$EVENT_NAME" = "pull_request" ] && [ "$CODE_CHANGED" = "true" ]' in changes_job
    assert "ci_gate.py test-matrix" in changes_job
    assert "test_matrix: ${{ steps.tests.outputs.matrix }}" in changes_job
    assert "quality.test_selection" not in tests_job
    # The shards follow that matrix; no selection means no shard at all.
    assert "matrix: ${{ fromJSON(needs.changes.outputs.test_matrix) }}" in tests_job
    assert "needs.changes.outputs.tests == 'true'" in tests_job
    assert "SHARD_FILES: ${{ matrix.files }}" in tests_job
    assert 'echo "::error::test shard $SHARD_LABEL selected no files"' in tests_job
    assert "TESTS_NEEDED: ${{ needs.changes.outputs.tests || 'true' }}" in workflow
    assert '--tests "$TESTS_NEEDED"' in workflow
    # Main records; the receipt is merged only after every shard passed.
    # A merge-queue candidate runs on main's ref but must never record.
    main_only = (
        "github.event_name == 'push' && github.ref == 'refs/heads/main' && !inputs.candidate_sha"
    )
    assert f"RECORD_IMPACT: ${{{{ {main_only} }}}}" in tests_job
    assert f"if: ${{{{ {main_only} }}}}" in tests_job
    assert "-p src.quality.test_impact_plugin" in tests_job
    receipt_job = workflow[workflow.index("\n  test-impact-receipt:\n") :]
    receipt_job = receipt_job[: receipt_job.index("\n\n  ") if "\n\n  " in receipt_job else None]
    assert "needs: [tests]" in receipt_job
    assert f"if: ${{{{ {main_only} }}}}" in receipt_job
    assert "key: test-impact-v1-${{ steps.merge.outputs.commit }}" in receipt_job
