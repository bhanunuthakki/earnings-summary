from __future__ import annotations

import importlib.util
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
        "src/pipeline/work_os_styles.py",
        "scripts/check_design_sync.py",
        "execution/verify_design_conformance.py",
        "execution/design_route_canaries.py",
        "directives/design_language.md",
        "tests/design_conformance_debt.json",
        "tests/design_geometry_baseline.json",
        "requirements-design.lock",
        # Design canary/golden/shell tests.
        "tests/test_design_computed_canary.py",
        "tests/test_design_conformance_canonical.py",
        "tests/test_design_registry.py",
        "tests/test_design_sync.py",
        "tests/test_workspace_golden.py",
        "tests/test_work_os_shell.py",
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
        "execution/comments_server.py",
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
        design=False,
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
        design=False,
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
        design=False,
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
    # Both ratchet sides run through one helper. pyright's non-zero exit from the
    # tolerated baseline must stay non-fatal, but an unparseable payload has to
    # block on its own terms rather than reach the comparison as a parse error.
    assert 'pyright --outputjson > "$head_json" 2>/dev/null || true' not in workflow
    assert '( cd "$dir" && pyright --outputjson ) > "$out" 2>"$log" || true' in workflow
    assert 'run_pyright "$head_json" head "$GITHUB_WORKSPACE"' in workflow
    assert 'run_pyright "$base_json" base "$wt"' in workflow
    assert "json.load(open(sys.argv[1]))" in workflow
    assert 'tail -n 40 "$log"' in workflow
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
    assert "name: Public Boundary" in workflow
    assert "python execution/verify_public_tree.py" in workflow
    assert (
        "needs: [changes, public-boundary, tests, design, quality, typecheck, security]" in workflow
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


def test_typecheck_caches_base_pyright_scan() -> None:
    workflow = WORKFLOW_PATH.read_text(encoding="utf-8")
    typecheck_job = workflow.split("\n  typecheck:\n", maxsplit=1)[1].split(
        "\n  security:\n", maxsplit=1
    )[0]
    # Exact-key cache over (resolved base tip sha, pyright version, python
    # version, installed lock hash) — deliberately no restore-keys, so a fuzzy
    # restore can never compare HEAD against a stale ratchet.
    assert "actions/cache@" in typecheck_job
    assert "key: pyright-base-${{ steps.base.outputs.sha }}" in typecheck_job
    assert "pw${{ steps.tools.outputs.pyright }}" in typecheck_job
    assert "py${{ steps.tools.outputs.python }}" in typecheck_job
    assert "hashFiles('requirements.lock')" in typecheck_job
    assert "restore-keys:" not in typecheck_job
    # The stored base-root prefix is passed to --base-root on a hit (the gate
    # reads only the two JSONs and strips the root from every diagnostic).
    assert 'base_root="$(cat "$cache_dir/base-root.txt")"' in typecheck_job
    assert '--base-root "$base_root"' in typecheck_job
    assert 'echo "$wt" > "$cache_dir/base-root.txt"' in typecheck_job
    # A miss runs the same fail-closed worktree scan as before; head and base
    # scans both go through the hardened run_pyright wrapper (blocks on
    # unparseable JSON instead of misreading a pyright crash), and the
    # pyright-diff invocation is unchanged.
    assert "could not check out base" in typecheck_job
    assert 'run_pyright "$head_json" head "$GITHUB_WORKSPACE"' in typecheck_job
    assert 'run_pyright "$base_json" base "$wt"' in typecheck_job
    assert "ci_gate.py pyright-diff" in typecheck_job


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
