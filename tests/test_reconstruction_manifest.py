"""Unit tests for the 11-project reconstruction manifest and verification drill (BHA-53, BHA-54, BHA-55)."""

from __future__ import annotations

import json
from pathlib import Path

from execution.verify_reconstruction_inventory import (
    VALID_RECONSTRUCTION_TIERS,
    ManifestVerificationReceipt,
    check_alembic_graph,
    verify_manifest,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_canonical_manifest_exists_and_passes_deterministic_inventory() -> None:
    manifest_path = PROJECT_ROOT / "reconstruction_manifest.json"
    assert manifest_path.exists(), "reconstruction_manifest.json must exist at repo root"

    receipt = verify_manifest(manifest_path, PROJECT_ROOT)

    assert isinstance(receipt, ManifestVerificationReceipt)
    assert receipt.manifest_version == "2026-08-28.1"
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
        assert r.test_commands_valid is True, f"Test commands for {r.subsystem_id} must be valid"
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


def test_manifest_validator_rejects_unbound_authority_and_missing_test_target(
    tmp_path: Path,
) -> None:
    payload = json.loads(
        (PROJECT_ROOT / "reconstruction_manifest.json").read_text(encoding="utf-8")
    )
    subsystem = payload["subsystems"][0]
    subsystem["test_commands"] = ["pytest tests/does_not_exist.py -q"]
    subsystem["ownership_paths"][0] = {
        "field": "version_ownership",
        "path": "authority/does_not_exist.py",
        "kind": "file",
        "required_in_checkout": True,
    }
    manifest = tmp_path / "invalid.json"
    manifest.write_text(json.dumps(payload), encoding="utf-8")

    receipt = verify_manifest(manifest, PROJECT_ROOT)
    result = next(row for row in receipt.results if row.subsystem_id == "core_data_layer")
    assert receipt.all_subsystems_pass is False
    assert result.test_commands_valid is False
    assert any("does_not_exist.py" in issue for issue in result.issues)
    assert result.version_ownership_valid is False
    assert any("must be an existing file" in issue for issue in result.issues)


def test_manifest_validator_rejects_unsupported_test_flag_and_duplicate_ids(tmp_path: Path) -> None:
    payload = json.loads(
        (PROJECT_ROOT / "reconstruction_manifest.json").read_text(encoding="utf-8")
    )
    payload["subsystems"][1]["id"] = payload["subsystems"][0]["id"]
    manifest = tmp_path / "invalid.json"
    manifest.write_text(json.dumps(payload), encoding="utf-8")

    receipt = verify_manifest(manifest, PROJECT_ROOT)
    assert receipt.all_subsystems_pass is False
    assert receipt.total_issues_count > 0
    payload["subsystems"][1]["id"] = "pipeline_execution"
    payload["subsystems"][0]["test_commands"] = ["pytest tests/test_sqlite_runtime.py --bogus"]
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    receipt = verify_manifest(manifest, PROJECT_ROOT)
    assert any("unsupported pytest flags" in issue for issue in receipt.results[0].issues)


def test_manifest_validator_rejects_unconstrained_non_path_evidence(tmp_path: Path) -> None:
    payload = json.loads(
        (PROJECT_ROOT / "reconstruction_manifest.json").read_text(encoding="utf-8")
    )
    payload["subsystems"][1]["ownership_paths"][0]["evidence"] = "git:arbitrary prose"
    manifest = tmp_path / "invalid.json"
    manifest.write_text(json.dumps(payload), encoding="utf-8")

    receipt = verify_manifest(manifest, PROJECT_ROOT)
    pipeline = next(row for row in receipt.results if row.subsystem_id == "pipeline_execution")
    assert pipeline.version_ownership_valid is False
    assert any("supported typed prefix" in issue for issue in pipeline.issues)


def test_readme_has_distinct_fresh_and_existing_upgrade_branches() -> None:
    readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
    fresh = readme.split("#### Fresh install (new database)", 1)[1].split(
        "#### Existing database upgrade", 1
    )[0]
    existing = readme.split("#### Existing database upgrade", 1)[1].split("On a fresh install", 1)[
        0
    ]
    assert "--phase0-backup-restore-receipt" not in fresh
    assert "--phase0-backup-restore-receipt $EarningsSummaryPhase0ReceiptPath" in existing
    assert "$EarningsSummaryAttemptId" in fresh and "$EarningsSummaryAttemptId" in existing
    assert fresh.count("[guid]::NewGuid().ToString('N')") == 1
    assert existing.count("[guid]::NewGuid().ToString('N')") == 1
    assert "Get-Date -Format 'yyyyMMdd_HHmmss_fff'" not in readme
    assert "create_sqlite_snapshot.py" in existing
    assert "backup_restore_readiness_receipt.py" in existing


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
    assert payload["manifest_version"] == "2026-08-28.1"
    assert payload["all_subsystems_pass"] is True
    assert payload["dependency_graph_acyclic"] is True


def test_manifest_uses_live_recovery_authorities_and_dynamic_migration_head() -> None:
    manifest = json.loads(
        (PROJECT_ROOT / "reconstruction_manifest.json").read_text(encoding="utf-8")
    )
    core = next(item for item in manifest["subsystems"] if item["id"] == "core_data_layer")
    assert "cron/backup_db.py" in core["backup_ownership"]
    assert "execution/restore_drill.py" in core["backup_ownership"]
    assert "repo-maintenance/backup_scratch.ps1" not in core["backup_ownership"]
    assert "0270+" not in core["version_ownership"]
    valid, issues = check_alembic_graph(PROJECT_ROOT)
    assert valid is True, issues


def test_manifest_validator_rejects_missing_explicit_ownership_path(tmp_path: Path) -> None:
    base_dir = tmp_path / "valid_dir"
    base_dir.mkdir()
    (base_dir / "script.py").write_text("x = 1\n", encoding="utf-8")
    (base_dir / "README.md").write_text("# Readme\n", encoding="utf-8")
    fake_manifest = tmp_path / "fake_manifest.json"
    fake_manifest.write_text(
        json.dumps(
            {
                "manifest_version": "test.1",
                "workspace_name": "test-ws",
                "subsystems": [
                    {
                        "id": "subsystem",
                        "name": "Component",
                        "path": "valid_dir",
                        "language": "python",
                        "entrypoints": ["valid_dir/script.py"],
                        "dependencies": [],
                        "test_commands": [],
                        "documentation": ["valid_dir/README.md"],
                        "version_ownership": "version authority",
                        "backup_ownership": "valid_dir/README.md",
                        "ownership_paths": [
                            {
                                "field": "version_ownership",
                                "path": "missing/authority.py",
                                "kind": "file",
                                "required_in_checkout": True,
                            },
                            {
                                "field": "backup_ownership",
                                "path": "valid_dir/README.md",
                                "kind": "file",
                                "required_in_checkout": True,
                            },
                        ],
                        "state_classification": "state",
                        "reconstruction_tier": "tier_0_data_backbone",
                        "invariants": ["invariant"],
                        "exit_ready_boundary": "boundary",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    receipt = verify_manifest(fake_manifest, tmp_path)
    assert receipt.all_subsystems_pass is False
    assert any(
        "ownership_paths[0].path must be an existing file" in issue
        for issue in receipt.results[0].issues
    )


def test_manifest_validator_checks_structured_ownership_paths_and_containment(
    tmp_path: Path,
) -> None:
    base_dir = tmp_path / "valid_dir"
    base_dir.mkdir()
    (base_dir / "script.py").write_text("x = 1\n", encoding="utf-8")
    (base_dir / "README.md").write_text("# Readme\n", encoding="utf-8")
    fake_manifest = tmp_path / "fake_manifest.json"

    def write_manifest(ownership_paths: list[dict[str, object]]) -> None:
        fake_manifest.write_text(
            json.dumps(
                {
                    "manifest_version": "test.1",
                    "workspace_name": "test-ws",
                    "subsystems": [
                        {
                            "id": "subsystem",
                            "name": "Component",
                            "path": "valid_dir",
                            "language": "python",
                            "entrypoints": ["valid_dir/script.py"],
                            "dependencies": [],
                            "test_commands": [],
                            "documentation": ["valid_dir/README.md"],
                            "version_ownership": "version authority",
                            "backup_ownership": "valid_dir/README.md",
                            "ownership_paths": ownership_paths,
                            "state_classification": "state",
                            "reconstruction_tier": "tier_0_data_backbone",
                            "invariants": ["invariant"],
                            "exit_ready_boundary": "boundary",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

    write_manifest(
        [
            {
                "field": "version_ownership",
                "path": "valid_dir/script.py",
                "kind": "file",
                "required_in_checkout": True,
            },
            {
                "field": "backup_ownership",
                "path": "valid_dir/README.md",
                "kind": "file",
                "required_in_checkout": True,
            },
            {
                "field": "backup_ownership",
                "path": "optional-runtime-dir",
                "kind": "directory",
                "required_in_checkout": False,
            },
        ]
    )
    receipt = verify_manifest(fake_manifest, tmp_path)
    assert receipt.results[0].version_ownership_valid is True
    assert receipt.results[0].backup_ownership_valid is True

    optional_runtime_path = tmp_path / "optional-runtime-dir"
    optional_runtime_path.write_text("not a directory\n", encoding="utf-8")
    receipt = verify_manifest(fake_manifest, tmp_path)
    assert receipt.results[0].backup_ownership_valid is False
    assert any(
        "ownership_paths[2].path must be an existing directory" in issue
        for issue in receipt.results[0].issues
    )

    write_manifest(
        [
            {
                "field": "version_ownership",
                "path": "../outside",
                "kind": "directory",
                "required_in_checkout": False,
            }
        ]
    )
    receipt = verify_manifest(fake_manifest, tmp_path)
    assert receipt.results[0].version_ownership_valid is False
    assert any(
        "ownership_paths[0].path escapes workspace root" in issue
        for issue in receipt.results[0].issues
    )

    outside = tmp_path.parent / "outside-ownership-root"
    outside.mkdir(exist_ok=True)
    write_manifest(
        [
            {
                "field": "version_ownership",
                "path": str(outside),
                "kind": "directory",
                "required_in_checkout": False,
            }
        ]
    )
    receipt = verify_manifest(fake_manifest, tmp_path)
    assert receipt.results[0].version_ownership_valid is False
    assert any(
        "ownership_paths[0].path escapes workspace root" in issue
        for issue in receipt.results[0].issues
    )


def test_manifest_validator_requires_exact_type_for_checkout_paths(tmp_path: Path) -> None:
    base_dir = tmp_path / "valid_dir"
    base_dir.mkdir()
    (base_dir / "script.py").write_text("x = 1\n", encoding="utf-8")
    (base_dir / "README.md").write_text("# Readme\n", encoding="utf-8")
    fake_manifest = tmp_path / "fake_manifest.json"
    fake_manifest.write_text(
        json.dumps(
            {
                "manifest_version": "test.1",
                "workspace_name": "test-ws",
                "subsystems": [
                    {
                        "id": "subsystem",
                        "name": "Component",
                        "path": "valid_dir",
                        "language": "python",
                        "entrypoints": ["valid_dir/script.py"],
                        "dependencies": [],
                        "test_commands": [],
                        "documentation": ["valid_dir/README.md"],
                        "version_ownership": "version authority",
                        "backup_ownership": "backup authority",
                        "ownership_paths": [
                            {
                                "field": "version_ownership",
                                "path": "valid_dir/README.md",
                                "kind": "directory",
                                "required_in_checkout": True,
                            }
                        ],
                        "state_classification": "state",
                        "reconstruction_tier": "tier_0_data_backbone",
                        "invariants": ["invariant"],
                        "exit_ready_boundary": "boundary",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    receipt = verify_manifest(fake_manifest, tmp_path)
    assert receipt.results[0].version_ownership_valid is False
    assert any("must be an existing directory" in issue for issue in receipt.results[0].issues)


def test_alembic_graph_rejects_multiple_active_heads(tmp_path: Path) -> None:
    versions = tmp_path / "alembic" / "versions"
    versions.mkdir(parents=True)
    (versions / "0001_base.py").write_text(
        "revision = 'base'\ndown_revision = None\n", encoding="utf-8"
    )
    (versions / "0002_a.py").write_text(
        "revision = 'a'\ndown_revision = 'base'\n", encoding="utf-8"
    )
    (versions / "0003_b.py").write_text(
        "revision = 'b'\ndown_revision = 'base'\n", encoding="utf-8"
    )
    valid, issues = check_alembic_graph(tmp_path)
    assert valid is False
    assert any("exactly one active head" in issue for issue in issues)


def test_alembic_graph_rejects_disconnected_cycle(tmp_path: Path) -> None:
    versions = tmp_path / "alembic" / "versions"
    versions.mkdir(parents=True)
    (versions / "0001_base.py").write_text(
        "revision = 'base'\ndown_revision = None\n", encoding="utf-8"
    )
    (versions / "0002_head.py").write_text(
        "revision = 'head'\ndown_revision = 'base'\n", encoding="utf-8"
    )
    (versions / "0003_a.py").write_text("revision = 'a'\ndown_revision = 'b'\n", encoding="utf-8")
    (versions / "0004_b.py").write_text("revision = 'b'\ndown_revision = 'a'\n", encoding="utf-8")

    valid, issues = check_alembic_graph(tmp_path)
    assert valid is False
    assert any("migration cycle detected" in issue for issue in issues)
    assert any("not reachable from a base" in issue for issue in issues)


def test_alembic_graph_accepts_annotated_revision_assignments(tmp_path: Path) -> None:
    versions = tmp_path / "alembic" / "versions"
    versions.mkdir(parents=True)
    (versions / "0001_base.py").write_text(
        "revision: str = 'base'\ndown_revision: str | None = None\n", encoding="utf-8"
    )
    (versions / "0002_child.py").write_text(
        "revision: str = 'child'\ndown_revision: str | None = 'base'\n", encoding="utf-8"
    )

    valid, issues = check_alembic_graph(tmp_path)
    assert valid is True, issues


def test_readme_separates_windows_runtime_from_mac_disposable_database() -> None:
    readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
    assert "### Canonical Windows runtime / product use" in readme
    assert "### Mac development" in readme
    mac_section = readme.split("### Mac development", 1)[1].split("## How it works", 1)[0]
    windows_section = readme.split("### Mac development", 1)[0]
    assert (
        "$EarningsSummaryCodeRoot = 'C:\\Users\\Bhanu\\.gemini\\antigravity\\runtime\\earnings-summary'"
        in windows_section
    )
    assert (
        "$EarningsSummaryDbRoot = 'C:\\Users\\Bhanu\\.gemini\\antigravity\\scratch\\earnings-summary'"
        in windows_section
    )
    assert "Join-Path $EarningsSummaryDbRoot 'data\\portfolio.db'" in windows_section
    assert "$env:EARNINGS_SUMMARY_DB_PATH = $EarningsSummaryDbPath" in windows_section
    assert (
        "upgrade_database.py --db-path $EarningsSummaryDbPath --repo-root $EarningsSummaryCodeRoot --runtime-root $EarningsSummaryCodeRoot"
        in windows_section
    )
    assert "create_sqlite_snapshot.py --source-path $EarningsSummaryDbPath" in windows_section
    assert (
        "backup_restore_readiness_receipt.py --source-db $EarningsSummaryDbPath" in windows_section
    )
    assert "--snapshot-db $EarningsSummarySnapshotPath" in windows_section
    assert '"$EarningsSummarySnapshotPath.manifest.json"' in windows_section
    assert "--phase0-backup-restore-receipt $EarningsSummaryPhase0ReceiptPath" in windows_section
    assert "--db-path data/portfolio.db" not in windows_section
    assert "--repo-root . --runtime-root ." not in windows_section
    assert "sync_thesis_state.py --apply --db $EarningsSummaryDbPath" in windows_section
    assert "EARNINGS_SUMMARY_DB_PATH" in mac_section
    assert "mktemp -d" in mac_section
    assert "--db-path data/portfolio.db" not in mac_section
    assert '--db-path "$EARNINGS_SUMMARY_DB_PATH"' in mac_section
    assert "--runtime-root . --allow-isolated-db" in mac_section
    assert "execution/sqlite_bootstrap.py execution/upgrade_database.py" in mac_section
    assert "execution/sqlite_bootstrap.py execution/comments_server.py" in mac_section
    assert "tailscale serve status" in mac_section
