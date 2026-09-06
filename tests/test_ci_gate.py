from __future__ import annotations

import importlib.util
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
        "quality": False,
    }


@pytest.mark.parametrize("path", ["AGENTS.md", "directives/design_language.md"])
def test_agent_and_design_contract_changes_run_executable_guards(
    helper: ModuleType, path: str
) -> None:
    assert helper.classify_paths([path]) == {"code": True, "python": False, "quality": False}


def test_unknown_non_documentation_path_fails_closed(helper: ModuleType) -> None:
    assert helper.classify_paths(["new-tool/config.toml"]) == {
        "code": True,
        "python": False,
        "quality": False,
    }


@pytest.mark.parametrize(
    "path",
    [
        "docs/quality/architecture-ratchet.json",
        "docs/quality/README.md",
        "Makefile",
        ".github/workflows/ci.yml",
        ".github/scripts/ci_gate.py",
        "src/quality/architecture.py",
    ],
)
def test_quality_control_changes_run_quality_ratchets(helper: ModuleType, path: str) -> None:
    assert helper.classify_paths([path])["quality"] is True


def test_unrelated_documentation_change_does_not_run_quality_ratchets(helper: ModuleType) -> None:
    assert helper.classify_paths(["docs/architecture.md"])["quality"] is False


def test_pyright_baseline_establishment_is_source_config_and_version_bound(
    helper: ModuleType,
) -> None:
    head: dict[str, object] = {
        "version": "1.1.411",
        "generalDiagnostics": [
            {
                "severity": "error",
                "file": "/repo/src/example.py",
                "message": "example",
                "rule": "reportArgumentType",
            }
        ],
        "summary": {"errorCount": 1},
    }
    pyright_diagnostic: dict[str, object] = {
        "tool": "pyright",
        "count": 1,
        "version": "pyright 1.1.411",
        "version_hash": "40c7560256cc8f524e955d3d620dae6e10b672651c9918bf032a9069babf086f",  # pragma: allowlist secret -- artifact digest
        "diagnostics_by_directory": {"src": 1},
        "diagnostics_by_rule": {"reportArgumentType": 1},
    }
    pyright_exclusions: dict[str, object] = {
        "tool.pyright.include": [
            ".github/scripts",
            "alembic",
            "cron",
            "evals",
            "execution",
            "instruction_tests",
            "scripts",
            "src",
            "tests",
        ],
        "tool.pyright.exclude": [".cache", ".tmp", "scratch"],
    }
    baseline: dict[str, object] = {
        "schema_version": "bha-120.v2",
        "status": "PASS",
        "violations": [],
        "source_hash": "source",
        "config_hash": "config",
        "current_exclusions": pyright_exclusions,
        "diagnostics": [pyright_diagnostic],
    }

    assert (
        helper.pyright_baseline_errors(
            head,
            baseline,
            head_root=Path("/repo"),
            source_hash="source",
            config_hash="config",
        )
        == []
    )
    assert "source hash is stale" in " ".join(
        helper.pyright_baseline_errors(
            head,
            baseline,
            head_root=Path("/repo"),
            source_hash="changed",
            config_hash="config",
        )
    )
    assert "error count differs" in " ".join(
        helper.pyright_baseline_errors(
            head,
            {
                **baseline,
                "diagnostics": [{**pyright_diagnostic, "count": 2}],
            },
            head_root=Path("/repo"),
            source_hash="source",
            config_hash="config",
        )
    )
    assert "configuration hash is stale" in " ".join(
        helper.pyright_baseline_errors(
            head,
            baseline,
            head_root=Path("/repo"),
            source_hash="source",
            config_hash="changed",
        )
    )
    cases: list[tuple[dict[str, object], str]] = [
        ({**baseline, "status": "HOLD"}, "not an accepted"),
        ({**baseline, "violations": ["stale"]}, "contract violations"),
        (
            {
                **baseline,
                "diagnostics": [{**pyright_diagnostic, "version": "pyright 0.0.0"}],
            },
            "version differs",
        ),
        (
            {
                **baseline,
                "current_exclusions": {
                    **pyright_exclusions,
                    "tool.pyright.include": ["src"],
                },
            },
            "does not cover every active Python root",
        ),
        (
            {
                **baseline,
                "current_exclusions": {
                    **pyright_exclusions,
                    "tool.pyright.exclude": ["src/example.py"],
                },
            },
            "source-file Pyright exclusion",
        ),
        (
            {
                **baseline,
                "diagnostics": [
                    {
                        **pyright_diagnostic,
                        "diagnostics_by_directory": {"wrong": 1},
                    }
                ],
            },
            "directory counts differ",
        ),
        (
            {
                **baseline,
                "diagnostics": [
                    {
                        **pyright_diagnostic,
                        "diagnostics_by_rule": {"wrong": 1},
                    }
                ],
            },
            "rule counts differ",
        ),
    ]
    for changed, expected in cases:
        assert expected in " ".join(
            helper.pyright_baseline_errors(
                head,
                changed,
                head_root=Path("/repo"),
                source_hash="source",
                config_hash="config",
            )
        )
    assert "must be JSON objects" in " ".join(
        helper.pyright_baseline_errors(
            [],
            baseline,
            head_root=Path("/repo"),
            source_hash="source",
            config_hash="config",
        )
    )


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
    assert helper.classify_paths([path]) == {
        "code": True,
        "python": python,
        "quality": python
        or path in {"Makefile"}
        or path.startswith((".github/workflows/", ".github/scripts/")),
    }


def test_gate_requires_every_applicable_job_to_succeed(helper: ModuleType) -> None:
    assert helper.gate_failures(
        code=True,
        python=True,
        quality=True,
        results={
            "changes": "success",
            "public-boundary": "success",
            "tests": "success",
            "design": "success",
            "quality": "success",
            "typecheck": "skipped",
            "security": "success",
        },
    ) == ["typecheck must succeed for this change set; got skipped"]


def test_gate_requires_quality_for_quality_control_changes(helper: ModuleType) -> None:
    assert helper.gate_failures(
        code=True,
        python=False,
        quality=True,
        results={
            "changes": "success",
            "public-boundary": "success",
            "tests": "success",
            "design": "success",
            "quality": "skipped",
            "typecheck": "skipped",
            "security": "success",
        },
    ) == ["quality must succeed for this change set; got skipped"]


def test_gate_keeps_python_changes_quality_required_even_if_groups_disagree(
    helper: ModuleType,
) -> None:
    assert helper.gate_failures(
        code=True,
        python=True,
        quality=False,
        results={
            "changes": "success",
            "public-boundary": "success",
            "tests": "success",
            "design": "success",
            "quality": "skipped",
            "typecheck": "success",
            "security": "success",
        },
    ) == ["quality must succeed for this change set; got skipped"]


def test_gate_accepts_skipped_expensive_jobs_for_docs_only(helper: ModuleType) -> None:
    assert (
        helper.gate_failures(
            code=False,
            python=False,
            quality=False,
            results={
                "changes": "success",
                "public-boundary": "success",
                "tests": "skipped",
                "design": "skipped",
                "quality": "skipped",
                "typecheck": "skipped",
                "security": "skipped",
            },
        )
        == []
    )


def test_gate_never_hides_failed_or_cancelled_jobs(helper: ModuleType) -> None:
    assert helper.gate_failures(
        code=False,
        python=False,
        quality=False,
        results={
            "changes": "failure",
            "public-boundary": "success",
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
        quality=False,
        results={
            "changes": "skipped",
            "public-boundary": "success",
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
        quality=False,
        results={
            "changes": "success",
            "public-boundary": "skipped",
            "tests": "skipped",
            "design": "skipped",
            "quality": "skipped",
            "typecheck": "skipped",
            "security": "skipped",
        },
    )
    assert gate_failures == ["public-boundary must succeed; got skipped"]


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


def test_ci_test_partitions_are_exhaustive_disjoint_and_nonempty(helper: ModuleType) -> None:
    files = [f"tests/test_{index:04d}.py" for index in range(257)]
    partitions = (
        (1, 2, 0),
        (1, 2, 1),
        (2, 2, 0),
        (2, 2, 1),
        (3, 1, 0),
        (4, 1, 0),
        (5, 1, 0),
        (6, 2, 0),
        (6, 2, 1),
        (7, 1, 0),
        (8, 1, 0),
    )
    selected = [
        helper.select_test_files(
            files,
            source_shard=source_shard,
            source_shards=8,
            split_count=split_count,
            split_part=split_part,
        )
        for source_shard, split_count, split_part in partitions
    ]

    assert all(selected)
    assert Counter(path for partition in selected for path in partition) == Counter(files)


def test_workflow_uses_native_classifier_and_fail_closed_aggregate() -> None:
    workflow = WORKFLOW_PATH.read_text(encoding="utf-8")
    assert "dorny/paths-filter" not in workflow
    assert 'git diff --name-only --no-renames -z "$base...$head"' in workflow
    assert 'git diff --name-only --no-renames -z "$PUSH_BEFORE_SHA" "$CURRENT_SHA"' in workflow
    assert (
        'pyright --pythonpath "$(command -v python)" --outputjson > "$head_json" 2>/dev/null || true'
        in workflow
    )
    assert 'cp pyproject.toml "$wt/pyproject.toml"' not in workflow
    assert "ci_gate.py pyright-diff" in workflow
    assert "ci_gate.py pyright-baseline" in workflow
    assert 'git cat-file -e "$base:docs/quality/static-baseline.json"' in workflow
    assert "the established static-quality baseline was deleted" in workflow
    assert "base and HEAD both lack the required static-quality baseline" in workflow
    assert (
        'pip install "pyright>=1.1.380" "pytest>=8" "alembic>=1.13" "sqlalchemy>=2.0"' in workflow
    )
    assert "ci_gate.py select-tests" in workflow
    assert "Prepare trusted canonical test population" in workflow
    assert "--expected-population-sha256" in workflow
    assert "--trusted-harness-sha256" in workflow
    assert "--trusted-selector-sha256" in workflow
    assert "--trusted-plugin-sha256" in workflow
    assert "--capture-python-sha256" in workflow
    assert "--sqlite-preload" in workflow
    assert "--sqlite-preload-sha256" in workflow
    assert "sqlite_preload_sha256" in workflow
    trusted_index = workflow.index("- name: Pin trusted CI identities")
    build_index = workflow.index("- name: Build verified SQLite writer runtime")
    install_index = workflow.index("- name: Install dependencies")
    population_index = workflow.index("- name: Prepare trusted canonical test population")
    capture_index = workflow.index("- name: Run test-suite shard with production harness")
    assert build_index < trusted_index < install_index < population_index < capture_index
    population_step = workflow[population_index:capture_index]
    assert "harness_sha256=$(sha256sum" not in population_step
    assert "selector_sha256=$(sha256sum" not in population_step
    assert "POPULATION_SELECTOR_SHA256=" in population_step
    assert '--trusted-harness-sha256 "${{ steps.trusted.outputs.harness_sha256 }}"' in workflow
    assert '--trusted-selector-sha256 "${{ steps.trusted.outputs.selector_sha256 }}"' in workflow
    assert '--trusted-plugin-sha256 "${{ steps.trusted.outputs.plugin_sha256 }}"' in workflow
    assert (
        '--capture-python-sha256 "${{ steps.trusted.outputs.capture_python_sha256 }}"' in workflow
    )
    assert (
        '--sqlite-preload-sha256 "${{ steps.trusted.outputs.sqlite_preload_sha256 }}"' in workflow
    )
    assert "Run quality ratchets" in workflow
    assert "make quality-ratchets" in workflow
    assert "docs[\\\\/]quality[\\\\/].*\\.json" in workflow
    assert "trusted_loader" in workflow
    assert "python -m pytest --collect-only -q --disable-warnings" in population_step
    assert "-n 2" not in population_step
    assert "--dist=loadfile" not in population_step
    assert "len(node_payload) != len(set(node_payload))" in workflow
    assert "nodes = sorted(set(node_payload))" not in workflow
    assert '--fragments-dir "$RUNNER_TEMP/test-ci-performance/' in workflow
    assert 'echo "LD_PRELOAD=' not in workflow.split("  tests:", 1)[1].split("\n  design:", 1)[0]
    assert 'pytest -q -n 2 --dist=loadfile --durations=25 "${selected[@]}"' not in workflow
    assert "errcount || echo 0" not in workflow
    assert "python .github/scripts/ci_gate.py classify" in workflow
    assert "python .github/scripts/ci_gate.py verify" in workflow
    assert "quality: ${{ steps.classify.outputs.quality }}" in workflow
    assert "needs.changes.outputs.quality == 'true'" in workflow
    assert "QUALITY_CHANGED: ${{ needs.changes.outputs.quality || 'false' }}" in workflow
    assert '--quality "$QUALITY_CHANGED"' in workflow
    assert "if: ${{ always() }}" in workflow
    assert "name: CI Gate" in workflow
    assert "name: Public Boundary" in workflow
    assert "python execution/verify_public_tree.py" in workflow
    assert "- name: Exercise pre-push hook" in workflow
    assert "run: sh .githooks/test_pre_push.sh" in workflow
    assert workflow.index("- name: Exercise pre-push hook") < workflow.index(
        "- name: LLM eval coverage - no new registered gaps"
    )
    assert (
        "needs: [changes, public-boundary, tests, design, quality, typecheck, security]" in workflow
    )
    assert "PUBLIC_BOUNDARY_RESULT" in workflow


def test_public_boundary_is_unconditional_and_pre_push_uses_same_guard() -> None:
    workflow = WORKFLOW_PATH.read_text(encoding="utf-8")
    all_refs_workflow = (REPO_ROOT / ".github" / "workflows" / "public-boundary.yml").read_text(
        encoding="utf-8"
    )
    public_job = workflow.split("  public-boundary:\n", maxsplit=1)[1].split(
        "\n  tests:", maxsplit=1
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
    assert "BANDIT_OUTCOME" in workflow
    assert "DETECT_SECRETS_OUTCOME" in workflow
    assert "SBOM_OUTCOME" in workflow
    assert "pip-audit -r requirements-design.lock" in workflow
    assert "cyclonedx-py requirements requirements-design.lock" in workflow
    assert "always() && hashFiles('sbom-*.cdx.json')" in workflow


def test_ci_performance_receipts_are_uploaded_without_becoming_a_gate() -> None:
    workflow = WORKFLOW_PATH.read_text(encoding="utf-8")
    assert "Upload raw test CI performance evidence" in workflow
    assert "name: test-ci-performance-${{ matrix.label }}" in workflow
    assert "if: ${{ always() }}" in workflow
    upload_step = workflow.split(
        "      - name: Upload raw test CI performance evidence\n", maxsplit=1
    )[1].split("\n  design:", maxsplit=1)[0]
    assert ".tmp/quality/test-ci-performance/${{ matrix.label }}/receipt.json" in upload_step
    runner_temp_root = "${{ runner.temp }}/test-ci-performance/${{ matrix.label }}/**/"
    assert f"{runner_temp_root}worker-*.json" in upload_step
    assert f"{runner_temp_root}pytest.stdout" in upload_step
    assert f"{runner_temp_root}pytest.stderr" in upload_step
    assert "if-no-files-found: error" in upload_step
    assert "if-no-files-found: ignore" not in upload_step
    assert "evidence_status" not in workflow
