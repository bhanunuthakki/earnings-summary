"""verify_reconstruction_inventory.py — Deterministic inventory validator for the 11-project manifest.

Validates:
  1. Integrity, strict schema, and version/backup ownership of `reconstruction_manifest.json`.
  2. Existence of all declared subsystem root paths, entrypoints, and documentation references.
  3. Syntax correctness of all declared Python entrypoints via `ast.parse`.
  4. Acyclicity and completeness of the subsystem dependency graph (DAG validation).
  5. Acyclicity, reachability, and single-head completeness of active Alembic migrations.
  6. Correctness of reconstruction tiers, invariants, and exit-ready boundary declarations.
  7. Emits a deterministic, typed JSON verification receipt for replacement-agent drills.

Usage:
  python execution/verify_reconstruction_inventory.py [--manifest PATH] [--receipt PATH] [--json]
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import shlex
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
    "ownership_paths",
    "state_classification",
    "reconstruction_tier",
    "invariants",
    "exit_ready_boundary",
)
OWNERSHIP_FIELDS = frozenset({"version_ownership", "backup_ownership"})
OWNERSHIP_KINDS = frozenset({"file", "directory", "non_path"})
NON_PATH_EVIDENCE = re.compile(r"^(git|schema|runtime|external):[a-z0-9][a-z0-9_.-]*$")
SUPPORTED_PYTEST_FLAGS = frozenset({"-q"})


@dataclass(frozen=True)
class SubsystemCheckResult:
    subsystem_id: str
    name: str
    path_exists: bool
    entrypoints_valid: bool
    test_commands_valid: bool
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


def check_alembic_graph(repo_root: Path) -> tuple[bool, list[str]]:
    """Verify that active Alembic migrations form one complete DAG.

    The check intentionally derives the head from the active directory rather
    than naming a historical revision, so archived migrations and future
    migrations cannot silently make the manifest stale. Every node must be
    reachable from a base and lead to the sole active head; this also catches
    disconnected cycles that a head-count check alone would miss.
    """
    versions_dir = repo_root / "alembic" / "versions"
    if not versions_dir.is_dir():
        return False, ["Active Alembic versions directory does not exist: alembic/versions"]

    revisions: dict[str, Path] = {}
    parents_by_revision: dict[str, tuple[str, ...]] = {}
    issues: list[str] = []
    migration_files = sorted(
        path for path in versions_dir.glob("*.py") if path.name != "__init__.py"
    )
    if not migration_files:
        return False, ["Active Alembic versions directory contains no migration files"]
    for path in migration_files:
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (OSError, SyntaxError) as exc:
            issues.append(
                f"Alembic migration cannot be parsed: {path.relative_to(repo_root)} ({exc})"
            )
            continue
        values: dict[str, object] = {}
        for node in tree.body:
            if isinstance(node, ast.Assign):
                targets = node.targets
                value = node.value
            elif isinstance(node, ast.AnnAssign):
                targets = [node.target]
                value = node.value
            else:
                continue
            for target in targets:
                if isinstance(target, ast.Name) and target.id in {"revision", "down_revision"}:
                    if value is None:
                        issues.append(
                            f"Alembic migration has a non-literal {target.id}: {path.relative_to(repo_root)}"
                        )
                        continue
                    try:
                        values[target.id] = ast.literal_eval(value)
                    except (ValueError, SyntaxError):
                        issues.append(
                            f"Alembic migration has a non-literal {target.id}: {path.relative_to(repo_root)}"
                        )
        revision = values.get("revision")
        if not isinstance(revision, str) or not revision.strip():
            issues.append(
                f"Alembic migration has no non-empty revision: {path.relative_to(repo_root)}"
            )
            continue
        if revision in revisions:
            issues.append(f"Duplicate Alembic revision '{revision}' in active graph")
            continue
        revisions[revision] = path
        down_revision = values.get("down_revision")
        migration_path = str(path.relative_to(repo_root))
        if down_revision is None:
            parents_by_revision[revision] = ()
        elif isinstance(down_revision, str) and down_revision.strip():
            parents_by_revision[revision] = (down_revision,)
        elif isinstance(down_revision, (tuple, list)):
            candidate_parents = cast(tuple[object, ...] | list[object], down_revision)
            if all(isinstance(item, str) and item.strip() for item in candidate_parents):
                parents_by_revision[revision] = tuple(cast(str, item) for item in candidate_parents)
            else:
                issues.append(f"Alembic migration has an invalid down_revision: {migration_path}")
                parents_by_revision[revision] = ()
        else:
            issues.append(f"Alembic migration has an invalid down_revision: {migration_path}")
            parents_by_revision[revision] = ()

    revision_ids = set(revisions)
    parents = {
        parent for revision_parents in parents_by_revision.values() for parent in revision_parents
    }
    unknown_parents = sorted(parents - revision_ids)
    if unknown_parents:
        issues.append(
            "Alembic graph references missing parent revisions: " + ", ".join(unknown_parents)
        )
    children_by_revision: dict[str, set[str]] = {revision: set() for revision in revisions}
    for revision, revision_parents in parents_by_revision.items():
        for parent in revision_parents:
            if parent in children_by_revision:
                children_by_revision[parent].add(revision)

    heads = sorted(revision for revision, children in children_by_revision.items() if not children)
    if len(heads) != 1:
        issues.append(
            f"Alembic migration graph must have exactly one active head; found {len(heads)}: {', '.join(heads) or 'none'}"
        )

    # Three-colour DFS across every node detects cycles, including components
    # disconnected from the otherwise valid primary migration chain.
    visit_state: dict[str, int] = {revision: 0 for revision in revisions}
    cycle_paths: list[str] = []

    def visit(revision: str, path: list[str]) -> None:
        visit_state[revision] = 1
        path.append(revision)
        for child in sorted(children_by_revision[revision]):
            if visit_state[child] == 0:
                visit(child, path)
            elif visit_state[child] == 1:
                cycle_start = path.index(child)
                cycle_paths.append(" -> ".join([*path[cycle_start:], child]))
        path.pop()
        visit_state[revision] = 2

    for revision in sorted(revisions):
        if visit_state[revision] == 0:
            visit(revision, [])
    for cycle in cycle_paths:
        issues.append(f"Alembic migration cycle detected: {cycle}")

    bases = sorted(
        revision
        for revision, revision_parents in parents_by_revision.items()
        if not revision_parents
    )
    reachable_from_base: set[str] = set()
    pending = list(bases)
    while pending:
        revision = pending.pop()
        if revision in reachable_from_base:
            continue
        reachable_from_base.add(revision)
        pending.extend(sorted(children_by_revision[revision] - reachable_from_base))
    unreachable_from_base = sorted(revision_ids - reachable_from_base)
    if unreachable_from_base:
        issues.append(
            "Alembic migration nodes are not reachable from a base: "
            + ", ".join(unreachable_from_base)
        )

    if len(heads) == 1:
        reaches_head: set[str] = set()
        pending = [heads[0]]
        while pending:
            revision = pending.pop()
            if revision in reaches_head:
                continue
            reaches_head.add(revision)
            pending.extend(
                parent for parent in parents_by_revision[revision] if parent in revision_ids
            )
        cannot_reach_head = sorted(revision_ids - reaches_head)
        if cannot_reach_head:
            issues.append(
                f"Alembic migration nodes cannot reach sole active head '{heads[0]}': "
                + ", ".join(cannot_reach_head)
            )
    return not issues, issues


def _validate_ownership_paths(
    repo_root: Path,
    raw_paths: object,
) -> tuple[dict[str, bool], list[str]]:
    """Validate structured ownership paths without requiring runtime directories.

    ``required_in_checkout`` distinguishes version-controlled authorities from
    runtime-created or externally populated directories. Every path is still
    required to be workspace-relative, even when its presence is optional.
    """
    validity = {field: True for field in OWNERSHIP_FIELDS}
    issues: list[str] = []
    if not isinstance(raw_paths, list):
        return (
            {field: False for field in OWNERSHIP_FIELDS},
            ["'ownership_paths' must be a list of structured path entries"],
        )

    workspace_root = repo_root.resolve()
    for index, raw_entry in enumerate(cast(list[object], raw_paths)):
        prefix = f"ownership_paths[{index}]"
        if not isinstance(raw_entry, dict):
            issues.append(f"{prefix} must be an object")
            for field in validity:
                validity[field] = False
            continue

        entry = cast(dict[str, object], raw_entry)
        missing = [key for key in ("field", "kind", "required_in_checkout") if key not in entry]
        if missing:
            issues.append(f"{prefix} missing required keys: {', '.join(missing)}")
            for field in validity:
                validity[field] = False
            continue

        field = entry["field"]
        kind = entry["kind"]
        required = entry["required_in_checkout"]
        entry_field = field if isinstance(field, str) else None
        if entry_field not in OWNERSHIP_FIELDS:
            issues.append(f"{prefix}.field must be one of {sorted(OWNERSHIP_FIELDS)}")
            for valid_field in validity:
                validity[valid_field] = False
            entry_field = None
        if not isinstance(kind, str) or kind not in OWNERSHIP_KINDS:
            issues.append(f"{prefix}.kind must be one of {sorted(OWNERSHIP_KINDS)}")
            if entry_field is not None:
                validity[entry_field] = False
        if not isinstance(required, bool):
            issues.append(f"{prefix}.required_in_checkout must be a boolean")
            if entry_field is not None:
                validity[entry_field] = False
            continue

        if kind == "non_path":
            evidence = entry.get("evidence")
            if not isinstance(evidence, str) or not NON_PATH_EVIDENCE.fullmatch(evidence):
                issues.append(
                    f"{prefix}.evidence must use a supported typed prefix for non_path ownership"
                )
                if entry_field is not None:
                    validity[entry_field] = False
            continue

        path_value = entry.get("path")
        if not isinstance(path_value, str) or not path_value.strip():
            issues.append(f"{prefix}.path must be a non-empty string")
            if entry_field is not None:
                validity[entry_field] = False
            continue

        ownership_path = path_value.strip()
        normalized = ownership_path.replace("\\", "/")
        path_parts = tuple(part for part in normalized.split("/") if part)
        is_drive_absolute = len(normalized) >= 3 and normalized[1:3] == ":/"
        if normalized.startswith("/") or normalized.startswith("//") or is_drive_absolute:
            issues.append(f"{prefix}.path escapes workspace root: {ownership_path}")
            if entry_field is not None:
                validity[entry_field] = False
            continue
        if ".." in path_parts:
            issues.append(f"{prefix}.path escapes workspace root: {ownership_path}")
            if entry_field is not None:
                validity[entry_field] = False
            continue

        candidate = (repo_root / Path(*path_parts)).resolve()
        try:
            candidate.relative_to(workspace_root)
        except ValueError:
            issues.append(f"{prefix}.path escapes workspace root: {ownership_path}")
            if entry_field is not None:
                validity[entry_field] = False
            continue

        # Runtime/external paths are intentionally allowed to be absent. If an
        # optional path is present, its declared file-vs-directory type remains
        # part of the manifest contract; checkout-owned paths also fail closed
        # when missing.
        if required or candidate.exists():
            if kind == "file" and not candidate.is_file():
                issues.append(f"{prefix}.path must be an existing file: {ownership_path}")
                if entry_field is not None:
                    validity[entry_field] = False
            elif kind == "directory" and not candidate.is_dir():
                issues.append(f"{prefix}.path must be an existing directory: {ownership_path}")
                if entry_field is not None:
                    validity[entry_field] = False

    return validity, issues


def _validate_test_commands(repo_root: Path, raw_commands: object) -> tuple[bool, list[str]]:
    """Validate pytest commands and every declared test target they name."""
    if not isinstance(raw_commands, list):
        return False, ["'test_commands' must be a list"]
    if not raw_commands:
        return False, ["'test_commands' must contain at least one command"]
    issues: list[str] = []
    for index, raw_command in enumerate(cast(list[object], raw_commands)):
        prefix = f"test_commands[{index}]"
        if not isinstance(raw_command, str) or not raw_command.strip():
            issues.append(f"{prefix} must be a non-empty string")
            continue
        try:
            tokens = shlex.split(raw_command)
        except ValueError as exc:
            issues.append(f"{prefix} is not shell-parseable: {exc}")
            continue
        if not tokens or not (
            tokens[0] == "pytest"
            or tokens[:3] == [sys.executable, "-m", "pytest"]
            or (tokens[:2] == ["python", "-m"] and len(tokens) > 2 and tokens[2] == "pytest")
        ):
            issues.append(f"{prefix} must invoke pytest directly")
            continue
        unsupported_flags = {
            token
            for token in tokens
            if token.startswith("-") and token not in SUPPORTED_PYTEST_FLAGS
        }
        if unsupported_flags:
            issues.append(
                f"{prefix} contains unsupported pytest flags: {sorted(unsupported_flags)}"
            )
            continue
        targets = [token for token in tokens[1:] if not token.startswith("-") and token != "pytest"]
        if (
            tokens[0] in {"python", sys.executable}
            and len(tokens) >= 3
            and tokens[1:3] == ["-m", "pytest"]
        ):
            targets = [token for token in tokens[3:] if not token.startswith("-")]
        if not targets:
            issues.append(f"{prefix} must declare at least one test target")
            continue
        for target in targets:
            normalized = target.replace("\\", "/")
            parts = tuple(part for part in normalized.split("/") if part)
            if (
                normalized.startswith(("/", "//"))
                or (len(normalized) >= 3 and normalized[1:3] == ":/")
                or ".." in parts
                or not normalized.startswith("tests/")
            ):
                issues.append(f"{prefix} target must be a workspace-relative tests path: {target}")
                continue
            matches = list(repo_root.glob(normalized))
            if not matches or not all(path.is_file() for path in matches):
                issues.append(f"{prefix} target does not match an existing test file: {target}")
    return not issues, issues


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
                    test_commands_valid=False,
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
                    test_commands_valid=False,
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
    if len(subsystem_ids) != len(subsystems):
        issues_total.append("Subsystem IDs must be unique")

    # Verify dependency DAG
    dag_acyclic, dag_issues = check_dependency_dag(subsystems, subsystem_ids)
    issues_total.extend(dag_issues)
    alembic_graph_valid, alembic_graph_issues = check_alembic_graph(repo_root)

    for item in subsystems:
        sub_id = str(item.get("id", "unnamed"))
        name = str(item.get("name", "Unnamed Subsystem"))
        path_str = str(item.get("path", ""))
        entrypoints_raw = item.get("entrypoints", [])
        docs_raw = item.get("documentation", [])
        dependencies_raw = item.get("dependencies", [])
        ownership_paths_raw = item.get("ownership_paths")
        version_ownership = str(item.get("version_ownership", "")).strip()
        backup_ownership = str(item.get("backup_ownership", "")).strip()
        state_classification = str(item.get("state_classification", "")).strip()
        reconstruction_tier = str(item.get("reconstruction_tier", "")).strip()
        invariants_raw = item.get("invariants", [])
        exit_ready_boundary = str(item.get("exit_ready_boundary", "")).strip()

        sub_issues: list[str] = []
        path_exists = False
        entrypoints_valid = True
        test_commands_valid = True
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

        ownership_validity, ownership_issues = _validate_ownership_paths(
            repo_root, ownership_paths_raw
        )
        sub_issues.extend(ownership_issues)
        version_valid = version_valid and ownership_validity["version_ownership"]
        backup_valid = backup_valid and ownership_validity["backup_ownership"]
        ownership_fields_bound: set[str] = set()
        if isinstance(ownership_paths_raw, list):
            for raw_entry in cast(list[object], ownership_paths_raw):
                if isinstance(raw_entry, dict):
                    field_value = cast(dict[str, object], raw_entry).get("field")
                    if isinstance(field_value, str):
                        ownership_fields_bound.add(field_value)
        for ownership_field in OWNERSHIP_FIELDS - ownership_fields_bound:
            sub_issues.append(f"{ownership_field} lacks bound typed ownership evidence")
            if ownership_field == "version_ownership":
                version_valid = False
            else:
                backup_valid = False

        if sub_id == "core_data_layer" and not alembic_graph_valid:
            sub_issues.extend(alembic_graph_issues)
            version_valid = False

        if reconstruction_tier not in VALID_RECONSTRUCTION_TIERS:
            sub_issues.append(
                f"Invalid reconstruction_tier: '{reconstruction_tier}'. Expected one of {sorted(VALID_RECONSTRUCTION_TIERS)}"
            )

        if not isinstance(invariants_raw, list) or len(cast(list[object], invariants_raw)) == 0:
            sub_issues.append("Invariants must be a non-empty list of invariant statements")

        test_commands_valid, test_command_issues = _validate_test_commands(
            repo_root, item.get("test_commands")
        )
        sub_issues.extend(test_command_issues)

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
                test_commands_valid=test_commands_valid,
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
