"""Unit tests for the 11-project reconstruction manifest and verification drill (BHA-54 & BHA-55)."""

from __future__ import annotations

import json
from pathlib import Path

from execution.verify_reconstruction_inventory import (
    ManifestVerificationReceipt,
    verify_manifest,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_canonical_manifest_exists_and_passes_deterministic_inventory() -> None:
    manifest_path = PROJECT_ROOT / "reconstruction_manifest.json"
    assert manifest_path.exists(), "reconstruction_manifest.json must exist at repo root"

    receipt = verify_manifest(manifest_path, PROJECT_ROOT)

    assert isinstance(receipt, ManifestVerificationReceipt)
    assert receipt.manifest_version == "2026-08-15.1"
    assert receipt.workspace_name == "earnings-summary"
    assert receipt.subsystem_count == 11
    assert receipt.all_subsystems_pass is True
    assert receipt.total_issues_count == 0

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


def test_manifest_validator_catches_missing_and_corrupt_files(tmp_path: Path) -> None:
    fake_manifest = tmp_path / "fake_manifest.json"
    fake_manifest.write_text(
        json.dumps({
            "manifest_version": "test.1",
            "workspace_name": "test-ws",
            "subsystems": [
                {
                    "id": "missing_subsystem",
                    "name": "Missing Component",
                    "path": "does_not_exist_dir",
                    "entrypoints": ["missing_script.py"],
                    "documentation": ["missing_doc.md"],
                }
            ],
        }),
        encoding="utf-8",
    )

    receipt = verify_manifest(fake_manifest, tmp_path)
    assert receipt.all_subsystems_pass is False
    assert receipt.subsystem_count == 1
    assert any("Base path does not exist" in iss for iss in receipt.results[0].issues)
    assert any("Entrypoint file missing" in iss for iss in receipt.results[0].issues)
    assert any("Documentation file missing" in iss for iss in receipt.results[0].issues)


def test_manifest_receipt_generation_to_file(tmp_path: Path) -> None:
    receipt_file = tmp_path / "drill_receipt.json"
    manifest_path = PROJECT_ROOT / "reconstruction_manifest.json"

    receipt = verify_manifest(manifest_path, PROJECT_ROOT)
    receipt_file.write_text(json.dumps(receipt.__dict__, default=str, indent=2), encoding="utf-8")

    assert receipt_file.exists()
    payload = json.loads(receipt_file.read_text(encoding="utf-8"))
    assert payload["manifest_version"] == "2026-08-15.1"
    assert payload["all_subsystems_pass"] is True
