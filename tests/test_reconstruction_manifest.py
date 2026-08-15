"""Unit tests for the 11-project reconstruction manifest and verification drill (BHA-53, BHA-54, BHA-55)."""

from __future__ import annotations

import json
from pathlib import Path

from execution.verify_reconstruction_inventory import (
    VALID_RECONSTRUCTION_TIERS,
    ManifestVerificationReceipt,
    verify_manifest,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_canonical_manifest_exists_and_passes_deterministic_inventory() -> None:
    manifest_path = PROJECT_ROOT / "reconstruction_manifest.json"
    assert manifest_path.exists(), "reconstruction_manifest.json must exist at repo root"

    receipt = verify_manifest(manifest_path, PROJECT_ROOT)

    assert isinstance(receipt, ManifestVerificationReceipt)
    assert receipt.manifest_version == "2026-08-15.2"
    assert receipt.workspace_name == "earnings-summary"
    assert receipt.subsystem_count == 11
    assert receipt.all_subsystems_pass is True
    assert receipt.total_issues_count == 0
    assert receipt.dependency_graph_acyclic is True

    subsystem_ids = {r.subsystem_id for r in receipt.results}
    expected_ids = {
        "core_data_layer",
        "pipeline_execution",
        "source_adapters",
        "financial_compute_engine",
        "synthesis_research_lenses",
        "llm_router_eval_harness",
        "operations_governance_hub",
        "ui_cockpit_server",
        "user_state_journal_memory",
        "cron_automation_scheduler",
        "test_and_verification_suite",
    }
    assert subsystem_ids == expected_ids

    # Invariant assertions across every subsystem
    for r in receipt.results:
        assert r.path_exists is True, f"Path for {r.subsystem_id} must exist"
        assert r.entrypoints_valid is True, f"Entrypoints for {r.subsystem_id} must be valid"
        assert r.docs_valid is True, f"Docs for {r.subsystem_id} must be valid"
        assert r.python_syntax_pass is True, f"Python syntax for {r.subsystem_id} must pass"
        assert r.version_ownership_valid is True, (
            f"Version ownership for {r.subsystem_id} must be valid"
        )
        assert r.backup_ownership_valid is True, (
            f"Backup ownership for {r.subsystem_id} must be valid"
        )
        assert r.dependencies_valid is True, f"Dependencies for {r.subsystem_id} must be valid"
        assert r.invariants_count > 0, f"Invariants count for {r.subsystem_id} must be > 0"
        assert r.reconstruction_tier in VALID_RECONSTRUCTION_TIERS


def test_manifest_validator_catches_missing_and_corrupt_files(tmp_path: Path) -> None:
    fake_manifest = tmp_path / "fake_manifest.json"
    fake_manifest.write_text(
        json.dumps(
            {
                "manifest_version": "test.1",
                "workspace_name": "test-ws",
                "subsystems": [
                    {
                        "id": "missing_subsystem",
                        "name": "Missing Component",
                        "path": "does_not_exist_dir",
                        "language": "python",
                        "entrypoints": ["missing_script.py"],
                        "dependencies": [],
                        "test_commands": [],
                        "documentation": ["missing_doc.md"],
                        "version_ownership": "test",
                        "backup_ownership": "test",
                        "state_classification": "test",
                        "reconstruction_tier": "tier_0_data_backbone",
                        "invariants": ["test invariant"],
                        "exit_ready_boundary": "test boundary",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    receipt = verify_manifest(fake_manifest, tmp_path)
    assert receipt.all_subsystems_pass is False
    assert receipt.subsystem_count == 1
    assert any("Base path does not exist" in iss for iss in receipt.results[0].issues)
    assert any("Entrypoint file missing" in iss for iss in receipt.results[0].issues)
    assert any("Documentation file missing" in iss for iss in receipt.results[0].issues)


def test_manifest_validator_catches_missing_ownership_and_invalid_tier(tmp_path: Path) -> None:
    base_dir = tmp_path / "valid_dir"
    base_dir.mkdir()
    entrypoint = tmp_path / "valid_dir" / "script.py"
    entrypoint.write_text("print('hello')\n", encoding="utf-8")
    doc = tmp_path / "valid_dir" / "README.md"
    doc.write_text("# Readme\n", encoding="utf-8")

    fake_manifest = tmp_path / "fake_manifest.json"
    fake_manifest.write_text(
        json.dumps(
            {
                "manifest_version": "test.1",
                "workspace_name": "test-ws",
                "subsystems": [
                    {
                        "id": "incomplete_subsystem",
                        "name": "Incomplete Component",
                        "path": "valid_dir",
                        "language": "python",
                        "entrypoints": ["valid_dir/script.py"],
                        "dependencies": [],
                        "test_commands": [],
                        "documentation": ["valid_dir/README.md"],
                        "version_ownership": "",
                        "backup_ownership": "",
                        "state_classification": "",
                        "reconstruction_tier": "invalid_tier_name",
                        "invariants": [],
                        "exit_ready_boundary": "",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    receipt = verify_manifest(fake_manifest, tmp_path)
    assert receipt.all_subsystems_pass is False
    issues = receipt.results[0].issues
    assert any("Empty or missing 'version_ownership'" in iss for iss in issues)
    assert any("Empty or missing 'backup_ownership'" in iss for iss in issues)
    assert any("Empty or missing 'state_classification'" in iss for iss in issues)
    assert any("Empty or missing 'exit_ready_boundary'" in iss for iss in issues)
    assert any("Invalid reconstruction_tier" in iss for iss in issues)
    assert any("Invariants must be a non-empty list" in iss for iss in issues)


def test_manifest_validator_detects_dependency_cycle(tmp_path: Path) -> None:
    for name in ("dir_a", "dir_b"):
        d = tmp_path / name
        d.mkdir()
        (d / "entry.py").write_text("x = 1\n", encoding="utf-8")
        (d / "doc.md").write_text("# Doc\n", encoding="utf-8")

    fake_manifest = tmp_path / "cyclic_manifest.json"
    fake_manifest.write_text(
        json.dumps(
            {
                "manifest_version": "test.1",
                "workspace_name": "test-ws",
                "subsystems": [
                    {
                        "id": "sub_a",
                        "name": "Sub A",
                        "path": "dir_a",
                        "language": "python",
                        "entrypoints": ["dir_a/entry.py"],
                        "dependencies": ["sub_b"],
                        "test_commands": [],
                        "documentation": ["dir_a/doc.md"],
                        "version_ownership": "v1",
                        "backup_ownership": "b1",
                        "state_classification": "sc1",
                        "reconstruction_tier": "tier_0_data_backbone",
                        "invariants": ["inv1"],
                        "exit_ready_boundary": "bound1",
                    },
                    {
                        "id": "sub_b",
                        "name": "Sub B",
                        "path": "dir_b",
                        "language": "python",
                        "entrypoints": ["dir_b/entry.py"],
                        "dependencies": ["sub_a"],
                        "test_commands": [],
                        "documentation": ["dir_b/doc.md"],
                        "version_ownership": "v2",
                        "backup_ownership": "b2",
                        "state_classification": "sc2",
                        "reconstruction_tier": "tier_1_pipeline_execution",
                        "invariants": ["inv2"],
                        "exit_ready_boundary": "bound2",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )

    receipt = verify_manifest(fake_manifest, tmp_path)
    assert receipt.all_subsystems_pass is False
    assert receipt.dependency_graph_acyclic is False
    assert receipt.total_issues_count > 0


def test_manifest_receipt_generation_to_file(tmp_path: Path) -> None:
    receipt_file = tmp_path / "drill_receipt.json"
    manifest_path = PROJECT_ROOT / "reconstruction_manifest.json"

    receipt = verify_manifest(manifest_path, PROJECT_ROOT)
    receipt_file.write_text(json.dumps(receipt.__dict__, default=str, indent=2), encoding="utf-8")

    assert receipt_file.exists()
    payload = json.loads(receipt_file.read_text(encoding="utf-8"))
    assert payload["manifest_version"] == "2026-08-15.2"
    assert payload["all_subsystems_pass"] is True
    assert payload["dependency_graph_acyclic"] is True
