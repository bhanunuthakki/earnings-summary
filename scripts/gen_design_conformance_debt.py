"""Generate or check the exact legacy design-debt ledger.

The ledger is a shrink-only migration boundary, not an allowlist generator for
new work.  Normal CI uses ``--check``; ``--write`` is an explicit design-system
change that must be reviewed with the source diff that justified it.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import cast

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC = PROJECT_ROOT / "src"
sys.path.insert(0, str(SRC))

from ui.conformance_scan import (  # noqa: E402
    css_text,
    finding_debt_id,
    geometry_debt_fingerprints,
    scan_surface_evidence,
    unverifiable_debt_id,
)
from ui.design_registry import GOVERNED, REGISTRY_VERSION  # noqa: E402

BASELINE = PROJECT_ROOT / "tests" / "design_conformance_debt.json"
SCHEMA_VERSION = "1.0.0"


def _surface_path(project_root: Path, surface: str) -> Path:
    source_path = project_root / "src" / surface
    return source_path if source_path.exists() else project_root / surface


def build_payload(project_root: Path = PROJECT_ROOT) -> dict[str, object]:
    findings: list[str] = []
    geometry: list[str] = []
    unverifiable: list[str] = []
    for surface in sorted(GOVERNED):
        path = _surface_path(project_root, surface)
        text = css_text(path) if path.suffix == ".py" else path.read_text("utf-8")
        evidence = scan_surface_evidence(surface, text)
        for dimension, values in evidence.findings:
            findings.extend(finding_debt_id(surface, dimension, value) for value in values)
        geometry.extend(geometry_debt_fingerprints(surface, text))
        unverifiable.extend(
            unverifiable_debt_id(surface, value) for value in evidence.unverifiable_markup
        )
    return {
        "schema_version": SCHEMA_VERSION,
        "registry_version": REGISTRY_VERSION,
        "findings": sorted(findings),
        "geometry": sorted(geometry),
        "unverifiable": sorted(unverifiable),
    }


def canonical_json(payload: dict[str, object]) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def check(project_root: Path = PROJECT_ROOT, baseline: Path = BASELINE) -> bool:
    if not baseline.exists():
        return False
    return baseline.read_text("utf-8") == canonical_json(build_payload(project_root))


def growth_failures(candidate: dict[str, object], current: dict[str, object]) -> list[str]:
    """Reject every new debt identity; only exact preservation or shrink may write."""

    failures: list[str] = []
    for dimension in ("findings", "geometry", "unverifiable"):
        candidate_values = candidate.get(dimension)
        current_values = current.get(dimension)
        if not isinstance(candidate_values, list) or not isinstance(current_values, list):
            failures.append(f"invalid {dimension} debt ledger shape")
            continue
        candidate_set: set[str] = set()
        current_set: set[str] = set()
        invalid = False
        for raw, target in (
            (cast(list[object], candidate_values), candidate_set),
            (cast(list[object], current_values), current_set),
        ):
            for item in raw:
                if not isinstance(item, str):
                    invalid = True
                    break
                target.add(item)
        if invalid:
            failures.append(f"invalid {dimension} debt identity")
            continue
        growth = candidate_set - current_set
        if growth:
            failures.append(f"{dimension} debt grew by {len(growth)} exact identities")
    return failures


def _ledger_at_merge_base(project_root: Path, base_ref: str) -> dict[str, object] | None:
    merge_base = subprocess.run(
        ["git", "merge-base", "HEAD", base_ref],
        cwd=project_root,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    if not merge_base:
        raise ValueError(f"no merge base with {base_ref!r}")
    result = subprocess.run(
        ["git", "show", f"{merge_base}:tests/design_conformance_debt.json"],
        cwd=project_root,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        exists = subprocess.run(
            ["git", "cat-file", "-e", f"{merge_base}:tests/design_conformance_debt.json"],
            cwd=project_root,
            capture_output=True,
            check=False,
        )
        if exists.returncode != 0:
            return None
        raise ValueError("could not read merge-base design debt ledger")
    raw = result.stdout
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError("merge-base design debt ledger is not an object")
    return cast(dict[str, object], payload)


def merge_base_growth_failures(
    base_ref: str,
    *,
    project_root: Path = PROJECT_ROOT,
    candidate: dict[str, object] | None = None,
    allow_missing_base: bool = False,
) -> list[str]:
    """Reject debt added relative to the VCS merge base, including hand edits."""

    try:
        base = _ledger_at_merge_base(project_root, base_ref)
    except (json.JSONDecodeError, OSError, subprocess.CalledProcessError, ValueError) as exc:
        return [f"could not read merge-base debt ledger: {exc}"]
    head = build_payload(project_root) if candidate is None else candidate
    if base is None:
        if not allow_missing_base:
            return ["merge-base design debt ledger is missing"]
        empty: dict[str, object] = {
            "findings": [],
            "geometry": [],
            "unverifiable": [],
        }
        bootstrap_failures = growth_failures(head, empty)
        return [
            f"first-ledger bootstrap must be debt-free: {failure}" for failure in bootstrap_failures
        ]
    return growth_failures(head, base)


def write_shrinking(project_root: Path = PROJECT_ROOT, baseline: Path = BASELINE) -> list[str]:
    """Write the current snapshot only when no debt dimension grows."""

    if not baseline.exists():
        return ["refusing to initialize debt without a reviewed checked-in baseline"]
    current = json.loads(baseline.read_text("utf-8"))
    candidate = build_payload(project_root)
    if not isinstance(current, dict):
        return ["invalid checked-in debt ledger"]
    failures = growth_failures(candidate, cast(dict[str, object], current))
    if failures:
        return failures
    baseline.write_text(canonical_json(candidate), encoding="utf-8", newline="\n")
    return []


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--write",
        action="store_true",
        help="replace the checked-in ledger with the current exact debt snapshot",
    )
    parser.add_argument(
        "--base-ref",
        help="reject debt growth relative to the merge base with this Git ref",
    )
    parser.add_argument(
        "--allow-missing-base",
        action="store_true",
        help="allow a debt-free one-time bootstrap when the merge base has no ledger",
    )
    args = parser.parse_args()
    if args.allow_missing_base and not args.base_ref:
        print("REFUSED: --allow-missing-base requires --base-ref", file=sys.stderr)
        return 1
    if args.write:
        if args.base_ref:
            print("REFUSED: --write and --base-ref are mutually exclusive", file=sys.stderr)
            return 1
        failures = write_shrinking()
        if failures:
            for failure in failures:
                print(f"REFUSED: {failure}", file=sys.stderr)
            return 1
        print(f"wrote {BASELINE.relative_to(PROJECT_ROOT)}")
        return 0
    payload_object = build_payload()
    payload = canonical_json(payload_object)
    if not BASELINE.exists() or BASELINE.read_text("utf-8") != payload:
        print("design conformance debt ledger drifted", file=sys.stderr)
        return 1
    if args.base_ref:
        failures = merge_base_growth_failures(
            args.base_ref,
            candidate=payload_object,
            allow_missing_base=args.allow_missing_base,
        )
        if failures:
            for failure in failures:
                print(f"REFUSED: {failure}", file=sys.stderr)
            return 1
    print("design conformance debt ledger: exact")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
