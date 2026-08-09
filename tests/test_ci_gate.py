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
    }


def test_unknown_non_documentation_path_fails_closed(helper: ModuleType) -> None:
    assert helper.classify_paths(["new-tool/config.toml"]) == {
        "code": True,
        "python": False,
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
    assert helper.classify_paths([path]) == {"code": True, "python": python}


def test_gate_requires_every_applicable_job_to_succeed(helper: ModuleType) -> None:
    assert helper.gate_failures(
        code=True,
        python=True,
        results={
            "changes": "success",
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
            results={
                "changes": "success",
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
        results={
            "changes": "failure",
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
        results={
            "changes": "skipped",
            "tests": "skipped",
            "design": "skipped",
            "quality": "skipped",
            "typecheck": "skipped",
            "security": "skipped",
        },
    ) == ["changes must succeed; got skipped"]


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
    assert 'pyright --outputjson > "$head_json" 2>/dev/null || true' in workflow
    assert "ci_gate.py pyright-diff" in workflow
    assert (
        'pip install "pyright>=1.1.380" "pytest>=8" "alembic>=1.13" "sqlalchemy>=2.0"' in workflow
    )
    assert "ci_gate.py select-tests" in workflow
    assert "errcount || echo 0" not in workflow
    assert "python .github/scripts/ci_gate.py classify" in workflow
    assert "python .github/scripts/ci_gate.py verify" in workflow
    assert "if: ${{ always() }}" in workflow
    assert "name: CI Gate" in workflow


def test_security_job_runs_every_scanner_before_failing_closed() -> None:
    workflow = WORKFLOW_PATH.read_text(encoding="utf-8")

    for step_id in ("pip_audit", "bandit", "detect_secrets", "sbom"):
        assert f"id: {step_id}" in workflow
    assert "Require every security scanner to pass" in workflow
    assert "PIP_AUDIT_OUTCOME" in workflow
    assert "BANDIT_OUTCOME" in workflow
    assert "DETECT_SECRETS_OUTCOME" in workflow
    assert "SBOM_OUTCOME" in workflow
    assert "always() && hashFiles('sbom.cdx.json')" in workflow
