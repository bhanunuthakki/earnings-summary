from __future__ import annotations

import importlib.util
import json
import os
import re
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
        "design": False,
    }


@pytest.mark.parametrize(
    ("path", "design"),
    [("AGENTS.md", False), ("directives/design_language.md", True)],
)
def test_agent_and_design_contract_changes_run_executable_guards(
    helper: ModuleType, path: str, design: bool
) -> None:
    assert helper.classify_paths([path]) == {"code": True, "python": False, "design": design}


def test_unknown_non_documentation_path_fails_closed(helper: ModuleType) -> None:
    assert helper.classify_paths(["new-tool/config.toml"]) == {
        "code": True,
        "python": False,
        "design": False,
    }


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
        ("pyproject.toml", True),
        ("Makefile", False),
    ],
)
def test_code_change_classification(helper: ModuleType, path: str, python: bool) -> None:
    assert helper.classify_paths([path]) == {"code": True, "python": python, "design": False}


@pytest.mark.parametrize(
    "path",
    [
        # Prefix-owned design surfaces.
        "design-system/package.json",
        "src/ui/tokens.py",
        "src/report/renderers/workspace_styles.py",
        "mockups/work_os_shell.html",
        "tests/golden/workspace/some_golden.html",
        # Exact design contract sources, tooling, and guard inputs.
        "src/pipeline/work_os_shell.py",
        "src/pipeline/work_os_runtime.js",
        "src/pipeline/work_os_styles.py",
        "scripts/check_design_sync.py",
        "execution/verify_design_conformance.py",
        "execution/design_route_canaries.py",
        "directives/design_language.md",
        "tests/design_conformance_debt.json",
        "tests/design_geometry_baseline.json",
        "requirements-design.lock",
        # Browser-boundary changes require the Chromium-equipped job.
        "execution/comments_server.py",
        "src/server_runtime/access.py",
        "tests/test_browser_security_canary.py",
        # Design canary/golden/shell tests.
        "tests/test_design_computed_canary.py",
        "tests/test_design_conformance_canonical.py",
        "tests/test_design_registry.py",
        "tests/test_design_sync.py",
        "tests/test_workspace_golden.py",
        "tests/test_work_os_shell.py",
        "tests/test_extracted_runtime_design.py",
        "tests/test_work_os_style_master.py",
        # Design generator scripts.
        "scripts/gen_design_tokens.py",
        "scripts/gen_design_controls.py",
        "scripts/gen_design_conformance_debt.py",
    ],
)
def test_design_impacting_paths_require_the_design_job(helper: ModuleType, path: str) -> None:
    groups = helper.classify_paths([path])
    assert groups["code"] is True
    assert groups["design"] is True


def test_design_content_documentation_requires_design_job_without_code_jobs(
    helper: ModuleType,
) -> None:
    """A design-content prose change is design work even when it is not "code"."""
    assert helper.classify_paths(["mockups/README.md"]) == {
        "code": False,
        "python": False,
        "design": True,
    }


@pytest.mark.parametrize(
    "path",
    [
        "src/pipeline/work_os_portfolio.py",
        "src/pipeline/operations_panel.py",
        "src/timeseries/kpi.py",
        "cron/backup_db.py",
        "alembic/versions/0033_next.py",
        "tests/test_backup_restore.py",
        "tests/test_comments_server_dashboard.py",
        "src/report/builder.py",
    ],
)
def test_backend_only_paths_skip_design_sync(helper: ModuleType, path: str) -> None:
    assert helper.classify_paths([path])["design"] is False


def test_gate_requires_every_applicable_job_to_succeed(helper: ModuleType) -> None:
    assert helper.gate_failures(
        code=True,
        python=True,
        design=True,
        results={
            "changes": "success",
            "public-boundary": "success",
            "fast-signal": "success",
            "tests": "success",
            "design": "success",
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
            design=False,
            results={
                "changes": "success",
                "public-boundary": "success",
                "fast-signal": "skipped",
                "tests": "skipped",
                "design": "skipped",
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
        design=False,
        results={
            "changes": "success",
            "public-boundary": "success",
            "fast-signal": "skipped",
            "tests": "success",
            "design": "skipped",
            "quality": "skipped",
            "typecheck": "skipped",
            "security": "success",
        },
    ) == ["fast-signal must succeed for this change set; got skipped"]


def test_gate_never_hides_failed_or_cancelled_jobs(helper: ModuleType) -> None:
    assert helper.gate_failures(
        code=False,
        python=False,
        design=False,
        results={
            "changes": "failure",
            "public-boundary": "success",
            "fast-signal": "skipped",
            "tests": "skipped",
            "design": "skipped",
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
        design=False,
        results={
            "changes": "skipped",
            "public-boundary": "success",
            "fast-signal": "skipped",
            "tests": "skipped",
            "design": "skipped",
            "quality": "skipped",
            "typecheck": "skipped",
            "security": "skipped",
        },
    ) == ["changes must succeed; got skipped"]


def test_gate_always_requires_public_boundary(helper: ModuleType) -> None:
    gate_failures = helper.gate_failures(
        code=False,
        python=False,
        design=False,
        results={
            "changes": "success",
            "public-boundary": "skipped",
            "fast-signal": "skipped",
            "tests": "skipped",
            "design": "skipped",
            "quality": "skipped",
            "typecheck": "skipped",
            "security": "skipped",
        },
    )
    assert gate_failures == ["public-boundary must succeed; got skipped"]


def test_gate_fails_when_design_paths_changed_but_design_job_skipped(
    helper: ModuleType,
) -> None:
    """The gate contract for workstream C1: design-impacting path set + a
    skipped design job must FAIL the aggregate gate."""
    assert helper.gate_failures(
        code=True,
        python=True,
        design=True,
        results={
            "changes": "success",
            "public-boundary": "success",
            "fast-signal": "success",
            "tests": "success",
            "design": "skipped",
            "quality": "success",
            "typecheck": "success",
            "security": "success",
        },
    ) == ["design must succeed for this change set; got skipped"]


def test_gate_accepts_skipped_design_job_for_backend_only_change(helper: ModuleType) -> None:
    """A PR with no design path skips Design Sync and the aggregate gate stays
    green — that skip is exactly what the classifier licensed."""
    assert (
        helper.gate_failures(
            code=True,
            python=True,
            design=False,
            results={
                "changes": "success",
                "public-boundary": "success",
                "fast-signal": "success",
                "tests": "success",
                "design": "skipped",
                "quality": "success",
                "typecheck": "success",
                "security": "success",
            },
        )
        == []
    )


def test_verify_command_consumes_design_classification(helper: ModuleType) -> None:
    """The aggregate `verify` CLI wiring: --design drives the same contract."""

    def argv(*, design_result: str) -> list[str]:
        return [
            "verify",
            "--code",
            "true",
            "--python",
            "true",
            "--design",
            "true",
            "--changes-result",
            "success",
            "--public-boundary-result",
            "success",
            "--fast-signal-result",
            "success",
            "--tests-result",
            "success",
            "--design-result",
            design_result,
            "--quality-result",
            "success",
            "--typecheck-result",
            "success",
            "--security-result",
            "success",
        ]

    assert helper.main(argv(design_result="success")) == 0
    assert helper.main(argv(design_result="skipped")) == 1


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

    files = sorted(
        "tests/" + path.name
        for path in (REPO_ROOT / "tests").glob("test_*.py")
        if path.name != "test_design_computed_canary.py"
    )
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
        "needs: [changes, public-boundary, fast-signal, tests, design, quality, typecheck, security]"
        in workflow
    )
    assert "PUBLIC_BOUNDARY_RESULT" in workflow


def test_design_job_gating_consumes_classifier_design_output() -> None:
    workflow = WORKFLOW_PATH.read_text(encoding="utf-8")
    design_job = workflow.split("\n  design:\n", maxsplit=1)[1].split("\n  quality:\n", maxsplit=1)[
        0
    ]
    # The classifier publishes a design group consumed by the design job's if,
    # the aggregate verify step, and nothing else gates it on `code`.
    assert "design: ${{ steps.classify.outputs.design }}" in workflow
    assert "needs.changes.outputs.design == 'true'" in design_job
    assert "github.ref == 'refs/heads/main'" in design_job
    assert "github.event_name == 'schedule'" in design_job
    assert "needs.changes.outputs.code" not in design_job
    # The nightly schedule trigger (declared new scope) plus its cheap-fanout
    # contract: only the design job runs on schedule.
    assert 'cron: "17 6 * * *"' in workflow
    assert 'echo "design=false" >> "$GITHUB_OUTPUT"' in workflow
    # gen_design_conformance_debt.py refuses an empty --base-ref, which a
    # schedule event would otherwise produce.
    assert "github.event.before || 'HEAD~1'" in workflow
    # The aggregate gate consumes the design classification.
    assert "DESIGN_CHANGED: ${{ needs.changes.outputs.design || 'false' }}" in workflow
    assert '--design "$DESIGN_CHANGED"' in workflow


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

    for step_id in ("pip_audit", "npm_audit", "bandit", "detect_secrets", "sbom"):
        assert f"id: {step_id}" in workflow
    assert "Require every security scanner to pass" in workflow
    assert "PIP_AUDIT_OUTCOME" in workflow
    assert "NPM_AUDIT_OUTCOME" in workflow
    assert (
        "npm audit --package-lock-only --ignore-scripts --registry=https://registry.npmjs.org"
        in workflow
    )
    assert "BANDIT_OUTCOME" in workflow
    assert "DETECT_SECRETS_OUTCOME" in workflow
    assert "SBOM_OUTCOME" in workflow
    assert "pip-audit -r requirements-design.lock" in workflow
    assert "cyclonedx-py requirements requirements-design.lock" in workflow
    assert "always() && hashFiles('sbom-*.cdx.json')" in workflow

    exclude_match = re.search(r"--exclude-files\s+'([^']+)'", workflow)
    assert exclude_match is not None
    exclude_pattern = exclude_match.group(1)
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
    labels_in_workflow = re.findall(r'- \{ label: "([^"]+)" \}', workflow)
    assert len(labels_in_workflow) == 13
    assert labels_in_workflow == list(_load_helper().SHARD_LABELS)
    assert "ci_gate.py select-tests" in workflow
    assert "--shard-label '${{ matrix.label }}'" in workflow
    assert "--durations-file .github/test-durations.json" in workflow
    assert "--source-shard" not in workflow
    assert "--split-count" not in workflow
    assert (
        "needs: [changes, public-boundary, fast-signal, tests, design, quality, typecheck, security]"
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
        "key: ci-sqlite-${{ runner.os }}-3.53.4-${{ hashFiles('.github/workflows/ci.yml') }}"
        in workflow
    )
    # Both the tests matrix and Design Sync gate the build on a miss and keep
    # the preload/verify step unconditional.
    assert "if: steps.sqlite_cache.outputs.cache-hit != 'true'" in workflow
    assert "Preload verified SQLite writer runtime" in workflow
    assert 'assert sqlite3.sqlite_version == "3.53.4"' in workflow
    assert "restore-keys:" not in workflow


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
        "-lock${{ hashFiles('requirements.lock') }}-design${{ hashFiles('requirements-design.lock') }}"
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
    assert "restore-keys:" not in workflow


def test_durations_report_is_available_on_the_real_table(
    helper: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import io
    import sys

    files = sorted(
        "tests/" + path.name
        for path in (REPO_ROOT / "tests").glob("test_*.py")
        if path.name != "test_design_computed_canary.py"
    )
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
    # SQLite build remains in the workflow on the miss path: archive URL,
    # hash verification, FTS5 compile, hash check, and version assert.
    assert "https://www.sqlite.org/2026/sqlite-amalgamation-3530400.zip" in workflow
    assert (
        "628a44cfe82c66aed1ccbbe85a562d2e33ebe64b3288981ed76285612227934e"  # pragma: allowlist secret
        in workflow
    )
    assert "-DSQLITE_ENABLE_FTS5" in workflow
    assert "gcc -O2 -fPIC -shared -pthread" in workflow
    # pip install falls back to the full hash-pinned install into the venv.
    assert (
        '"$RUNNER_TEMP/ci-venv/bin/pip" install --require-hashes -r requirements.lock' in workflow
    )
    assert '"$RUNNER_TEMP/ci-venv/bin/pip" install -e .[dev]' in workflow
    assert (
        '"$RUNNER_TEMP/ci-venv/bin/pip" install --require-hashes -r requirements-design.lock'
        in workflow
    )


def test_security_browser_canary_has_one_mandatory_ci_owner() -> None:
    workflow = WORKFLOW_PATH.read_text(encoding="utf-8")
    assert "grep -v '^tests/test_browser_security_canary.py$'" in workflow
    design = workflow.split("\n  design:\n", 1)[1].split("\n  quality:\n", 1)[0]
    assert "python -m pytest -q -n 0 tests/test_browser_security_canary.py" in design
    assert "continue-on-error" not in design
    assert "Ensure data dir exists" not in workflow
    assert "one migrated template per worker" in workflow
