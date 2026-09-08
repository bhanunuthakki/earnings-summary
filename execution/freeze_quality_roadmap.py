"""Index exact-subject roadmap evidence without granting program admission."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from quality.evidence_bundle_io import install_handoff, staged_input_alias_paths  # noqa: E402
from quality.roadmap_freeze import build_freeze  # noqa: E402
from quality.roadmap_freeze_bundle import load_dependency_inputs  # noqa: E402
from quality.roadmap_freeze_inputs import assert_unchanged  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=ROOT)
    parser.add_argument("--input-manifest", type=Path, required=True)
    parser.add_argument("--plan", type=Path)
    parser.add_argument("--owner-snapshot", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    try:
        root = args.repo_root.resolve()
        manifest = args.input_manifest
        if not manifest.is_absolute():
            manifest = root / manifest
        requested_output: Path | None = args.output
        output = None
        if requested_output is not None:
            output = requested_output if requested_output.is_absolute() else root / requested_output
            resolved_output = output.resolve()
            owner = args.owner_snapshot or Path("config/quality_roadmap_owners.json")
            owner = owner if owner.is_absolute() else root / owner
            plan = args.plan
            if plan is not None and not plan.is_absolute():
                plan = root / plan
            protected = (
                manifest,
                owner,
                *([plan] if plan is not None else []),
                *staged_input_alias_paths(manifest),
            )
            if any(
                path.resolve() == resolved_output
                or (output.exists() and path.exists() and output.samefile(path))
                for path in protected
            ):
                raise ValueError("output aliases a protected input")
        paths, snapshots = load_dependency_inputs(root, manifest)
        receipt = build_freeze(root, paths, args.plan, args.owner_snapshot)
        for snapshot in snapshots:
            assert_unchanged(snapshot)
        payload = receipt.model_dump_json(indent=2) + "\n"
        if output is None:
            sys.stdout.write(payload)
        else:
            # Producer output is an ignored handoff; assembly owns retained JSON.
            relative = output.relative_to(root).as_posix()
            install_handoff(root, relative, payload.encode())
        return 0 if receipt.artifact_status == "PASS" else 2
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"freeze input rejected: {type(exc).__name__}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
