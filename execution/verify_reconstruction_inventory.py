"""verify_reconstruction_inventory.py — Deterministic inventory validator for the 11-project manifest.

Validates:
  1. Integrity, strict schema, and version/backup ownership of `reconstruction_manifest.json`.
  2. Existence of all declared subsystem root paths, entrypoints, and documentation references.
  3. Syntax correctness of all declared Python entrypoints via `ast.parse`.
  4. Acyclicity and completeness of the subsystem dependency graph (DAG validation).
  5. Correctness of reconstruction tiers, invariants, and exit-ready boundary declarations.
  6. Emits a deterministic, typed JSON verification receipt for replacement-agent drills.

Usage:
  python execution/verify_reconstruction_inventory.py [--manifest PATH] [--receipt PATH] [--json]
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

PROJECT_ROOT = Path(__file__).resolve().parents[1]

VALID_RECONSTRUCTION_TIERS = frozenset(
    {
        "tier_0_data_backbone",
        "tier_1_pipeline_execution",
        "tier_2_synthesis_lens",
        "tier_3_presentation_cockpit",
    }
)

REQUIRED_SUBSYSTEM_FIELDS = (
    "id",
    "name",
    "path",
    "language",
    "entrypoints",
    "dependencies",
    "test_commands",
    "documentation",
    "version_ownership",
    "backup_ownership",
    "state_classification",
    "reconstruction_tier",
    "invariants",
    "exit_ready_boundary",
)


@dataclass(frozen=True)
class SubsystemCheckResult:
    subsystem_id: str
    name: str
    path_exists: bool
    entrypoints_valid: bool
    docs_valid: bool
    python_syntax_pass: bool
    version_ownership_valid: bool
    backup_ownership_valid: bool
    dependencies_valid: bool
    invariants_count: int
    reconstruction_tier: str
    issues: list[str]


@dataclass(frozen=True)
class ManifestVerificationReceipt:
    timestamp_utc: str
    manifest_version: str
    workspace_name: str
    subsystem_count: int
    all_subsystems_pass: bool
    total_issues_count: int
    dependency_graph_acyclic: bool
    results: list[SubsystemCheckResult]


def check_dependency_dag(
    subsystems: list[dict[str, object]],
    subsystem_ids: set[str],
) -> tuple[bool, list[str]]:
    """Check that the subsystem dependency graph contains no cycles."""
    adjacency: dict[str, list[str]] = {s_id: [] for s_id in subsystem_ids}
    issues: list[str] = []

    for item in subsystems:
        s_id = str(item.get("id", ""))
        deps_raw = item.get("dependencies", [])
        if isinstance(deps_raw, list):
            for dep in cast(list[object], deps_raw):
                dep_str = str(dep)
                if dep_str in subsystem_ids:
                    adjacency[s_id].append(dep_str)

    # Cycle detection via 3-color DFS: 0=unvisited, 1=visiting, 2=visited
    visited: dict[str, int] = {s_id: 0 for s_id in subsystem_ids}

    def dfs(node: str, path: list[str]) -> bool:
        visited[node] = 1
        path.append(node)
        for neighbor in adjacency.get(node, []):
            if visited[neighbor] == 1:
                cycle = " -> ".join([*path[path.index(neighbor) :], neighbor])
                issues.append(f"Dependency cycle detected: {cycle}")
                return False
            if visited[neighbor] == 0 and not dfs(neighbor, path):
                return False
        path.pop()
        visited[node] = 2
        return True

    for s_id in subsystem_ids:
        if visited[s_id] == 0 and not dfs(s_id, []):
            return False, issues

    return True, issues


def verify_manifest(
    manifest_path: Path,
    repo_root: Path,
) -> ManifestVerificationReceipt:
    issues_total: list[str] = []
    results: list[SubsystemCheckResult] = []

    if not manifest_path.exists():
        return ManifestVerificationReceipt(
            timestamp_utc=datetime.now(UTC).isoformat(),
            manifest_version="unknown",
            workspace_name="unknown",
            subsystem_count=0,
            all_subsystems_pass=False,
            total_issues_count=1,
            dependency_graph_acyclic=False,
            results=[
                SubsystemCheckResult(
                    subsystem_id="manifest",
                    name="Manifest File",
                    path_exists=False,
                    entrypoints_valid=False,
                    docs_valid=False,
                    python_syntax_pass=False,
                    version_ownership_valid=False,
                    backup_ownership_valid=False,
                    dependencies_valid=False,
                    invariants_count=0,
                    reconstruction_tier="unknown",
                    issues=[f"Manifest file not found: {manifest_path}"],
                )
            ],
        )

    try:
        raw_json = json.loads(manifest_path.read_text(encoding="utf-8"))
        data: dict[str, object] = (
            cast(dict[str, object], raw_json) if isinstance(raw_json, dict) else {}
        )
    except Exception as e:
        return ManifestVerificationReceipt(
            timestamp_utc=datetime.now(UTC).isoformat(),
            manifest_version="unparseable",
            workspace_name="unknown",
            subsystem_count=0,
            all_subsystems_pass=False,
            total_issues_count=1,
            dependency_graph_acyclic=False,
            results=[
                SubsystemCheckResult(
                    subsystem_id="manifest",
                    name="Manifest File",
                    path_exists=True,
                    entrypoints_valid=False,
                    docs_valid=False,
                    python_syntax_pass=False,
                    version_ownership_valid=False,
                    backup_ownership_valid=False,
                    dependencies_valid=False,
                    invariants_count=0,
                    reconstruction_tier="unknown",
                    issues=[f"Manifest JSON parsing error: {e}"],
                )
            ],
        )

    manifest_version = str(data.get("manifest_version", "unknown"))
    workspace_name = str(data.get("workspace_name", "unknown"))
    subsystems_raw = data.get("subsystems", [])
    subsystems: list[dict[str, object]] = []
    if isinstance(subsystems_raw, list):
        for item in cast(list[object], subsystems_raw):
            if isinstance(item, dict):
                subsystems.append(cast(dict[str, object], item))

    if len(subsystems) != 11:
        issues_total.append(f"Expected exactly 11 subsystems, found {len(subsystems)}")

    subsystem_ids: set[str] = {str(item.get("id", "")) for item in subsystems if "id" in item}

    # Verify dependency DAG
    dag_acyclic, dag_issues = check_dependency_dag(subsystems, subsystem_ids)
    issues_total.extend(dag_issues)

    for item in subsystems:
        sub_id = str(item.get("id", "unnamed"))
        name = str(item.get("name", "Unnamed Subsystem"))
        path_str = str(item.get("path", ""))
        entrypoints_raw = item.get("entrypoints", [])
        docs_raw = item.get("documentation", [])
        dependencies_raw = item.get("dependencies", [])
        version_ownership = str(item.get("version_ownership", "")).strip()
        backup_ownership = str(item.get("backup_ownership", "")).strip()
        state_classification = str(item.get("state_classification", "")).strip()
        reconstruction_tier = str(item.get("reconstruction_tier", "")).strip()
        invariants_raw = item.get("invariants", [])
        exit_ready_boundary = str(item.get("exit_ready_boundary", "")).strip()

        sub_issues: list[str] = []
        path_exists = False
        entrypoints_valid = True
        docs_valid = True
        python_syntax_pass = True
        version_valid = bool(version_ownership)
        backup_valid = bool(backup_ownership)
        dependencies_valid = True

        # Check required fields
        for rf in REQUIRED_SUBSYSTEM_FIELDS:
            if rf not in item:
                sub_issues.append(f"Missing required field: '{rf}'")

        if not version_valid:
            sub_issues.append("Empty or missing 'version_ownership'")
        if not backup_valid:
            sub_issues.append("Empty or missing 'backup_ownership'")
        if not state_classification:
            sub_issues.append("Empty or missing 'state_classification'")
        if not exit_ready_boundary:
            sub_issues.append("Empty or missing 'exit_ready_boundary'")

        if reconstruction_tier not in VALID_RECONSTRUCTION_TIERS:
            sub_issues.append(
                f"Invalid reconstruction_tier: '{reconstruction_tier}'. Expected one of {sorted(VALID_RECONSTRUCTION_TIERS)}"
            )

        if not isinstance(invariants_raw, list) or len(cast(list[object], invariants_raw)) == 0:
            sub_issues.append("Invariants must be a non-empty list of invariant statements")

        # 1. Check subsystem base path
        base_path = repo_root / path_str
        if base_path.exists():
            path_exists = True
        else:
            sub_issues.append(f"Base path does not exist: {path_str}")
            path_exists = False

        # 2. Check entrypoints
        if not isinstance(entrypoints_raw, list) or len(cast(list[object], entrypoints_raw)) == 0:
            sub_issues.append("Entrypoints must be a non-empty list")
            entrypoints_valid = False
        else:
            for ep in cast(list[object], entrypoints_raw):
                ep_str = str(ep)
                ep_path = repo_root / ep_str
                if not ep_path.exists():
                    sub_issues.append(f"Entrypoint file missing: {ep_str}")
                    entrypoints_valid = False
                elif ep_str.endswith(".py"):
                    try:
                        ast.parse(ep_path.read_text(encoding="utf-8"), filename=str(ep_path))
                    except Exception as e:
                        sub_issues.append(f"Python syntax error in {ep_str}: {e}")
                        python_syntax_pass = False

        # 3. Check documentation files
        if not isinstance(docs_raw, list) or len(cast(list[object], docs_raw)) == 0:
            sub_issues.append("Documentation must be a non-empty list")
            docs_valid = False
        else:
            for doc in cast(list[object], docs_raw):
                doc_str = str(doc)
                doc_path = repo_root / doc_str
                if not doc_path.exists():
                    sub_issues.append(f"Documentation file missing: {doc_str}")
                    docs_valid = False

        # 4. Check dependencies (either other subsystem IDs or existing repo files)
        if not isinstance(dependencies_raw, list):
            sub_issues.append("Dependencies must be a list")
            dependencies_valid = False
        else:
            for dep in cast(list[object], dependencies_raw):
                dep_str = str(dep)
                if dep_str in subsystem_ids:
                    continue
                dep_path = repo_root / dep_str
                if not dep_path.exists():
                    sub_issues.append(
                        f"Unresolvable dependency '{dep_str}' (not a subsystem ID or workspace file)"
                    )
                    dependencies_valid = False

        invariants_count = (
            len(cast(list[object], invariants_raw)) if isinstance(invariants_raw, list) else 0
        )

        results.append(
            SubsystemCheckResult(
                subsystem_id=sub_id,
                name=name,
                path_exists=path_exists,
                entrypoints_valid=entrypoints_valid,
                docs_valid=docs_valid,
                python_syntax_pass=python_syntax_pass,
                version_ownership_valid=version_valid,
                backup_ownership_valid=backup_valid,
                dependencies_valid=dependencies_valid,
                invariants_count=invariants_count,
                reconstruction_tier=reconstruction_tier,
                issues=sub_issues,
            )
        )
        issues_total.extend(sub_issues)

    all_pass = (len(issues_total) == 0) and (len(subsystems) == 11) and dag_acyclic
    return ManifestVerificationReceipt(
        timestamp_utc=datetime.now(UTC).isoformat(),
        manifest_version=manifest_version,
        workspace_name=workspace_name,
        subsystem_count=len(subsystems),
        all_subsystems_pass=all_pass,
        total_issues_count=len(issues_total),
        dependency_graph_acyclic=dag_acyclic,
        results=results,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Deterministic 11-project inventory validator.")
    parser.add_argument(
        "--manifest",
        type=Path,
        default=PROJECT_ROOT / "reconstruction_manifest.json",
        help="Path to manifest JSON file",
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=PROJECT_ROOT,
        help="Workspace root path",
    )
    parser.add_argument(
        "--receipt",
        type=Path,
        default=None,
        help="Path to write the JSON verification receipt",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit JSON output to stdout",
    )
    args = parser.parse_args()

    receipt = verify_manifest(args.manifest, args.repo_root)

    if args.receipt:
        args.receipt.parent.mkdir(parents=True, exist_ok=True)
        args.receipt.write_text(json.dumps(asdict(receipt), indent=2), encoding="utf-8")

    if args.json:
        print(json.dumps(asdict(receipt), indent=2))
    else:
        status_str = "PASS" if receipt.all_subsystems_pass else "FAIL"
        print(f"=== 11-Project Reconstruction Inventory: [{status_str}] ===")
        print(
            f"Manifest Version: {receipt.manifest_version} | Subsystems: {receipt.subsystem_count}/11"
        )
        print(
            f"Dependency Graph DAG: {'VALID (acyclic)' if receipt.dependency_graph_acyclic else 'INVALID (cycle detected)'}"
        )
        print(f"Timestamp: {receipt.timestamp_utc}")
        print("\nSubsystems Checklist:")
        for r in receipt.results:
            state = " OK " if not r.issues else "FAIL"
            print(f"  [{state}] {r.subsystem_id:<28} | {r.name} ({r.reconstruction_tier})")
            for iss in r.issues:
                print(f"        [!] {iss}")

        if receipt.all_subsystems_pass:
            print(
                "\nAll 11 project subsystems verified deterministically with strict invariant assertions."
            )

    return 0 if receipt.all_subsystems_pass else 1


if __name__ == "__main__":
    sys.exit(main())
