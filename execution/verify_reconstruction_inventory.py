"""verify_reconstruction_inventory.py — Deterministic inventory validator for the 11-project manifest.

Validates:
  1. Integrity and schema of `reconstruction_manifest.json`.
  2. Existence of all declared subsystem root paths, entrypoints, and documentation references.
  3. Syntax correctness of all declared Python entrypoints via `ast.parse`.
  4. Emits a deterministic, typed JSON verification receipt for replacement-agent drills.

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

PROJECT_ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class SubsystemCheckResult:
    subsystem_id: str
    name: str
    path_exists: bool
    entrypoints_valid: bool
    docs_valid: bool
    python_syntax_pass: bool
    issues: list[str]


@dataclass(frozen=True)
class ManifestVerificationReceipt:
    timestamp_utc: str
    manifest_version: str
    workspace_name: str
    subsystem_count: int
    all_subsystems_pass: bool
    total_issues_count: int
    results: list[SubsystemCheckResult]


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
            results=[
                SubsystemCheckResult(
                    subsystem_id="manifest",
                    name="Manifest File",
                    path_exists=False,
                    entrypoints_valid=False,
                    docs_valid=False,
                    python_syntax_pass=False,
                    issues=[f"Manifest file not found: {manifest_path}"],
                )
            ],
        )

    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception as e:
        return ManifestVerificationReceipt(
            timestamp_utc=datetime.now(UTC).isoformat(),
            manifest_version="unparseable",
            workspace_name="unknown",
            subsystem_count=0,
            all_subsystems_pass=False,
            total_issues_count=1,
            results=[
                SubsystemCheckResult(
                    subsystem_id="manifest",
                    name="Manifest File",
                    path_exists=True,
                    entrypoints_valid=False,
                    docs_valid=False,
                    python_syntax_pass=False,
                    issues=[f"Manifest JSON parsing error: {e}"],
                )
            ],
        )

    manifest_version = str(data.get("manifest_version", "unknown"))
    workspace_name = str(data.get("workspace_name", "unknown"))
    subsystems = data.get("subsystems", [])

    if len(subsystems) != 11:
        issues_total.append(f"Expected exactly 11 subsystems, found {len(subsystems)}")

    for item in subsystems:
        sub_id = str(item.get("id", "unnamed"))
        name = str(item.get("name", "Unnamed Subsystem"))
        path_str = str(item.get("path", ""))
        entrypoints = item.get("entrypoints", [])
        docs = item.get("documentation", [])

        sub_issues: list[str] = []
        path_exists = False
        entrypoints_valid = True
        docs_valid = True
        python_syntax_pass = True

        # 1. Check subsystem base path
        base_path = repo_root / path_str
        if base_path.exists():
            path_exists = True
        else:
            sub_issues.append(f"Base path does not exist: {path_str}")
            path_exists = False

        # 2. Check entrypoints
        for ep in entrypoints:
            ep_path = repo_root / ep
            if not ep_path.exists():
                sub_issues.append(f"Entrypoint file missing: {ep}")
                entrypoints_valid = False
            elif ep.endswith(".py"):
                try:
                    ast.parse(ep_path.read_text(encoding="utf-8"), filename=str(ep_path))
                except Exception as e:
                    sub_issues.append(f"Python syntax error in {ep}: {e}")
                    python_syntax_pass = False

        # 3. Check documentation files
        for doc in docs:
            doc_path = repo_root / doc
            if not doc_path.exists():
                sub_issues.append(f"Documentation file missing: {doc}")
                docs_valid = False

        results.append(
            SubsystemCheckResult(
                subsystem_id=sub_id,
                name=name,
                path_exists=path_exists,
                entrypoints_valid=entrypoints_valid,
                docs_valid=docs_valid,
                python_syntax_pass=python_syntax_pass,
                issues=sub_issues,
            )
        )
        issues_total.extend(sub_issues)

    all_pass = (len(issues_total) == 0) and (len(subsystems) == 11)
    return ManifestVerificationReceipt(
        timestamp_utc=datetime.now(UTC).isoformat(),
        manifest_version=manifest_version,
        workspace_name=workspace_name,
        subsystem_count=len(subsystems),
        all_subsystems_pass=all_pass,
        total_issues_count=len(issues_total),
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
        print(f"Manifest Version: {receipt.manifest_version} | Subsystems: {receipt.subsystem_count}/11")
        print(f"Timestamp: {receipt.timestamp_utc}")
        print("\nSubsystems Checklist:")
        for r in receipt.results:
            state = " OK " if not r.issues else "FAIL"
            print(f"  [{state}] {r.subsystem_id:<28} | {r.name}")
            for iss in r.issues:
                print(f"        [!] {iss}")

        if receipt.all_subsystems_pass:
            print("\nAll 11 project subsystems verified deterministically.")

    return 0 if receipt.all_subsystems_pass else 1


if __name__ == "__main__":
    sys.exit(main())
