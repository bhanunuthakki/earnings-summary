"""Single-purpose CLI for the BHA-147 exact-subject evidence bundle."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from quality.evidence_bundle import (  # noqa: E402
    assemble_bundle,
    collect_evidence,
    default_artifact_specs,
    load_collection_manifest,
    record_score_evidence,
    validate_bundle_diff,
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--mode", choices=["collect", "assemble", "validate", "record"], required=True
    )
    parser.add_argument("--repo-root", type=Path, default=ROOT)
    parser.add_argument(
        "--staging-dir", type=Path, default=ROOT / ".tmp" / "quality" / "evidence-bundle"
    )
    parser.add_argument("--output-dir", type=Path, default=ROOT / "docs" / "quality")
    parser.add_argument("--manifest", type=Path, default=None)
    parser.add_argument("--subject", default=None)
    parser.add_argument("--bundle", default=None)
    parser.add_argument("--output", type=Path, default=None)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    repo_root = args.repo_root.resolve()
    staging = (
        args.staging_dir.resolve()
        if args.staging_dir.is_absolute()
        else (repo_root / args.staging_dir).resolve()
    )
    manifest_path = (
        args.manifest.resolve()
        if args.manifest is not None
        else (staging / "manifest.json").resolve()
    )
    try:
        if args.mode == "collect":
            manifest = collect_evidence(repo_root, staging, default_artifact_specs())
            print(manifest.model_dump_json(indent=2))
            return 0 if manifest.status == "COMPLETE" else 2
        if args.mode == "assemble":
            manifest = load_collection_manifest(manifest_path)
            output_dir = (
                args.output_dir.resolve()
                if args.output_dir.is_absolute()
                else (repo_root / args.output_dir).resolve()
            )
            result = assemble_bundle(repo_root, manifest, staging, output_dir)
            print(result.model_dump_json(indent=2))
            return 0 if result.status == "COMPLETE" else 2
        if args.mode == "validate":
            if args.subject is None or args.bundle is None:
                print("validate requires --subject and --bundle", file=sys.stderr)
                return 2
            manifest = load_collection_manifest(manifest_path)
            violations = validate_bundle_diff(repo_root, args.subject, args.bundle, manifest)
            if violations:
                print("\n".join(violations), file=sys.stderr)
                return 1
            print("bundle diff valid")
            return 0
        if args.mode == "record":
            if args.bundle is None:
                print("record requires --bundle", file=sys.stderr)
                return 2
            out = (
                args.output.resolve()
                if args.output is not None
                else (repo_root / ".tmp" / "quality" / "score-evidence.json").resolve()
            )
            manifest = load_collection_manifest(manifest_path)
            evidence = record_score_evidence(repo_root, args.bundle, manifest, staging, out)
            print(evidence.model_dump_json(indent=2))
            return 0
        return 2
    except (OSError, RuntimeError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
