from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from quality.compatibility import CompatibilityEvidenceError, capture_compatibility_evidence

sys.path.insert(0, str(Path(__file__).parents[1] / "execution"))


def _git(root: Path, *args: str) -> str:
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    return subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    ).stdout.strip()


def _repo(tmp_path: Path) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _git(tmp_path, "config", "user.name", "Test")
    (tmp_path / "src").mkdir()
    (tmp_path / "execution").mkdir()
    (tmp_path / "evals/golden").mkdir(parents=True)
    (tmp_path / "src/app.py").write_text("VALUE = 1\n", encoding="utf-8")
    (tmp_path / "execution/run.py").write_text(
        "def main() -> None:\n    return None\n", encoding="utf-8"
    )
    (tmp_path / "evals/golden/route.json").write_text(
        '{"cases": [{"input": 1}]}\n', encoding="utf-8"
    )
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-qm", "baseline")
    return tmp_path


def test_temp_repo_and_scanner_ignore_outer_git_repository_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sentinel = _repo(tmp_path / "sentinel")
    sentinel_config = sentinel / ".git/config"
    before = sentinel_config.read_bytes()
    monkeypatch.setenv("GIT_DIR", str(sentinel / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(sentinel))
    monkeypatch.setenv("GIT_INDEX_FILE", str(sentinel / ".git/index"))

    requested = _repo(tmp_path / "requested")
    baseline = _git(requested, "rev-parse", "HEAD")
    receipt = capture_compatibility_evidence(requested, baseline)

    assert receipt.current_revision == baseline
    assert _git(requested, "rev-parse", "--show-toplevel") == str(requested)
    assert sentinel_config.read_bytes() == before


def test_receipt_is_typed_deterministic_and_reports_parity(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    baseline = _git(root, "rev-parse", "HEAD")
    first = capture_compatibility_evidence(root, baseline)
    second = capture_compatibility_evidence(root, baseline)
    assert first.model_dump() == second.model_dump()
    assert first.golden_count == 1
    assert {item.status for item in first.entrypoint_parity} == {"unchanged"}
    assert first.source_sha256 and first.baseline_sha256
    assert {item.name for item in first.categories} == {
        "flask_url_method_endpoint_map",
        "integrity_serialized_ordering",
        "public_import_surfaces",
        "dcf_formula_cell_receipts",
        "population_dry_run_apply_receipts",
        "report_dashboard_goldens",
    }
    assert first.hold is True
    assert first.verification_status == "DEFERRED"
    assert "behavioral verification and admission deferred" in first.hold_reasons
    for category in first.categories:
        assert category.artifact_sha256
        assert category.scope == "collection_only"
        assert set(category.implementation_artifacts + category.verification_artifacts) == set(
            category.artifacts
        )


def test_changed_entrypoint_is_explicitly_detected(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    baseline = _git(root, "rev-parse", "HEAD")
    (root / "execution/run.py").write_text("def main() -> None:\n    return 1\n", encoding="utf-8")
    receipt = capture_compatibility_evidence(root, baseline)
    changed = next(item for item in receipt.entrypoint_parity if item.path == "execution/run.py")
    assert changed.status == "changed"
    assert receipt.hold is True
    assert "entrypoint changed: execution/run.py" in receipt.hold_reasons


def test_recursive_git_pathspec_includes_nested_entrypoints(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    nested = root / "src/package/nested.py"
    nested.parent.mkdir()
    nested.write_text("VALUE = 2\n", encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "add nested entrypoint")
    baseline = _git(root, "rev-parse", "HEAD")

    receipt = capture_compatibility_evidence(root, baseline)

    nested_parity = next(
        item for item in receipt.entrypoint_parity if item.path == "src/package/nested.py"
    )
    assert nested_parity.status == "unchanged"


def test_malformed_route_contract_forces_category_hold(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    baseline = _git(root, "rev-parse", "HEAD")
    server = root / "execution/comments_server.py"
    server.write_text("def broken(:\n", encoding="utf-8")
    route_test = root / "tests/test_comments_server_routes.py"
    route_test.parent.mkdir()
    route_test.write_text("def test_routes(): pass\n", encoding="utf-8")
    _git(root, "add", ".")
    receipt = capture_compatibility_evidence(root, baseline)
    flask = next(
        item for item in receipt.categories if item.name == "flask_url_method_endpoint_map"
    )
    assert flask.collection_status == "INCOMPLETE"
    assert flask.extracted == []


def test_population_receipt_names_missing_mode(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    baseline = _git(root, "rev-parse", "HEAD")
    script = root / "execution/evaluate_population_cutover.py"
    script.write_text("parser.add_argument('--apply')\nmode = 'apply'\n", encoding="utf-8")
    test = root / "tests/test_population_cutover.py"
    test.parent.mkdir()
    test.write_text("def test_population(): pass\n", encoding="utf-8")
    _git(root, "add", ".")
    receipt = capture_compatibility_evidence(root, baseline)
    population = next(
        item for item in receipt.categories if item.name == "population_dry_run_apply_receipts"
    )
    assert population.collection_status == "INCOMPLETE"
    assert "population dry_run mode" in population.collection_reason


def test_missing_baseline_or_goldens_fails_closed(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    with pytest.raises(CompatibilityEvidenceError):
        capture_compatibility_evidence(root, "")
    (root / "evals/golden/route.json").unlink()
    with pytest.raises(CompatibilityEvidenceError):
        capture_compatibility_evidence(root, _git(root, "rev-parse", "HEAD"))


@pytest.mark.parametrize(
    "payload",
    [
        "{}\n",
        '{"metadata": {"version": 1}}\n',
        '{"cases": []}\n',
        '{"cases": "not-a-list"}\n',
        '{"cases": [{"id": 1}], "goldens": [{"id": 2}]}\n',
        "[]\n",
    ],
)
def test_golden_without_recognized_case_collection_fails_closed(
    tmp_path: Path, payload: str
) -> None:
    root = _repo(tmp_path)
    baseline = _git(root, "rev-parse", "HEAD")
    (root / "evals/golden/route.json").write_text(payload, encoding="utf-8")
    _git(root, "add", ".")

    with pytest.raises(CompatibilityEvidenceError, match="has no cases"):
        capture_compatibility_evidence(root, baseline)


@pytest.mark.parametrize(
    "payload",
    [
        [{"id": 1}],
        {"cases": [{"id": 1}]},
        {"goldens": [{"id": 1}]},
        {"examples": [{"id": 1}]},
        {"items": [{"id": 1}]},
    ],
)
def test_supported_golden_case_collections_are_counted(tmp_path: Path, payload: object) -> None:
    root = _repo(tmp_path)
    baseline = _git(root, "rev-parse", "HEAD")
    (root / "evals/golden/route.json").write_text(json.dumps(payload), encoding="utf-8")
    _git(root, "add", ".")

    receipt = capture_compatibility_evidence(root, baseline)

    assert receipt.legacy_route_golden[0].cases == 1


def test_tracked_complete_collection_still_holds_for_deferred_verification(
    tmp_path: Path,
) -> None:
    root = Path(__file__).parents[1]
    baseline = _git(root, "rev-parse", "HEAD")
    receipt = capture_compatibility_evidence(root, baseline)
    receipts = receipt.legacy_route_golden

    assert len(receipts) == 24
    assert sum(receipt.cases for receipt in receipts) == 288
    assert (
        next(
            receipt.cases
            for receipt in receipts
            if receipt.path == "evals/golden/extract_8k_overrides.json"
        )
        == 1
    )
    assert all(category.collection_status == "COMPLETE" for category in receipt.categories)
    assert receipt.verification_status == "DEFERRED"
    assert receipt.hold is True
    assert "behavioral verification and admission deferred" in receipt.hold_reasons

    from capture_compatibility_evidence import main

    output = tmp_path / "compatibility.json"
    assert main(["--repo-root", str(root), "--baseline", baseline, "--out", str(output)]) == 2
    assert json.loads(output.read_text(encoding="utf-8"))["hold"] is True


def test_baseline_option_injection_fails_closed(tmp_path: Path) -> None:
    root = _repo(tmp_path)

    with pytest.raises(CompatibilityEvidenceError, match="git evidence command failed"):
        capture_compatibility_evidence(root, "--help")


def test_tracked_symlink_escape_fails_closed(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    baseline = _git(root, "rev-parse", "HEAD")
    outside = tmp_path.parent / "outside-compat.py"
    outside.write_text("VALUE = 2\n", encoding="utf-8")
    (root / "src/external.py").symlink_to(outside)
    _git(root, "add", ".")

    with pytest.raises(CompatibilityEvidenceError, match="escapes"):
        capture_compatibility_evidence(root, baseline)


def test_tracked_symlink_loop_fails_closed(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    baseline = _git(root, "rev-parse", "HEAD")
    (root / "src/loop.py").symlink_to("loop.py")
    _git(root, "add", ".")

    with pytest.raises(CompatibilityEvidenceError, match="unable to read"):
        capture_compatibility_evidence(root, baseline)


def test_untracked_matching_artifacts_are_ignored(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    baseline = _git(root, "rev-parse", "HEAD")
    (root / "evals/golden/untracked.json").write_bytes(b"\xff")
    (root / "execution/evaluate_population_cutover.py").write_text(
        "--apply --dry-run\n", encoding="utf-8"
    )

    receipt = capture_compatibility_evidence(root, baseline)
    population = next(
        item for item in receipt.categories if item.name == "population_dry_run_apply_receipts"
    )

    assert receipt.golden_count == 1
    assert population.artifacts == []
    assert population.collection_status == "INCOMPLETE"


def test_undecodable_tracked_artifact_fails_closed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _repo(tmp_path)
    baseline = _git(root, "rev-parse", "HEAD")
    script = root / "execution/evaluate_population_cutover.py"
    script.write_bytes(b"mode = '\xff\xfe'\n")
    (root / "tests").mkdir()
    (root / "tests/test_population_cutover.py").write_text(
        "def test_population(): pass\n", encoding="utf-8"
    )
    _git(root, "add", ".")

    with pytest.raises(CompatibilityEvidenceError, match=r"evaluate_population_cutover\.py"):
        capture_compatibility_evidence(root, baseline)

    from capture_compatibility_evidence import main

    assert main(["--repo-root", str(root), "--baseline", baseline]) == 2
    error = json.loads(capsys.readouterr().err)
    assert error["event"] == "compatibility_evidence_failed"
    assert "evaluate_population_cutover.py" in error["error"]


def test_nul_paths_preserve_whitespace_and_newlines(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    relative = "src/line\nbreak .py"
    (root / relative).write_text("VALUE = 2\n", encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "add unusual entrypoint")
    baseline = _git(root, "rev-parse", "HEAD")

    receipt = capture_compatibility_evidence(root, baseline)

    assert relative in {item.path for item in receipt.entrypoint_parity}


def test_cli_receipt_is_json_and_parse_errors_are_nonzero(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _repo(tmp_path)
    baseline = _git(root, "rev-parse", "HEAD")
    from capture_compatibility_evidence import main

    assert main(["--repo-root", str(root), "--baseline", baseline]) == 2
    assert json.loads(capsys.readouterr().out)["schema_version"] == "quality-compatibility/v1"
