"""Reconcile provisional roadmap baselines against fresh typed receipts."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from quality.atomic_write import write_text_atomic  # noqa: E402
from quality.evidence_bundle_io import staged_input_alias_paths  # noqa: E402
from quality.roadmap_reconciliation import (  # noqa: E402
    ROADMAP_CANDIDATES,
    SOURCE_PATHS,
    StagedManifestError,
    reconcile,
    reconcile_staged_subject,
)
from quality.roadmap_source import ROADMAP_CLAIM_MAP_PATH  # noqa: E402


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--repo-root", "--root", dest="repo_root", type=Path, default=None)
    p.add_argument("--subject-root", type=Path, default=None)
    p.add_argument("--staged-manifest", type=Path, default=None)
    p.add_argument("--output", type=Path, default=None)
    return p.parse_args(argv)


def _output_is_protected(output: Path, root: Path) -> bool:
    """Check whether a requested output aliases a protected input path."""
    try:
        resolved_output = output.resolve()
    except OSError:
        return True
    for rel in (*SOURCE_PATHS.values(), *ROADMAP_CANDIDATES, ROADMAP_CLAIM_MAP_PATH):
        protected = root / rel
        try:
            if protected.resolve() == resolved_output:
                return True
        except OSError:
            pass
        try:
            if protected.samefile(output):
                return True
        except OSError:
            continue
    return False


def _output_is_protected_staged(output: Path, subject: Path, manifest: Path) -> bool:
    try:
        resolved_output = output.resolve()
    except OSError:
        return True
    try:
        if manifest.resolve() == resolved_output:
            return True
    except OSError:
        pass
    try:
        if manifest.samefile(output):
            return True
    except OSError:
        pass
    for staged in staged_input_alias_paths(manifest):
        if staged == resolved_output:
            return True
        try:
            if staged.samefile(output):
                return True
        except OSError:
            continue
    return _output_is_protected(output, subject)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        staged_mode = args.staged_manifest is not None or args.subject_root is not None
        if staged_mode:
            if args.staged_manifest is None or args.subject_root is None:
                sys.stderr.write(
                    json.dumps({"error": "staged_mode_requires_subject_and_manifest"}) + "\n"
                )
                return 1
            if args.repo_root is not None:
                sys.stderr.write(json.dumps({"error": "mutually_exclusive_roots"}) + "\n")
                return 1
            try:
                subject = args.subject_root.resolve()
            except OSError as exc:
                sys.stderr.write(json.dumps({"error": type(exc).__name__}) + "\n")
                return 1
            if not subject.is_dir():
                sys.stderr.write(json.dumps({"error": "repo_root_missing"}) + "\n")
                return 1
            manifest: Path = args.staged_manifest
            if args.output is not None and _output_is_protected_staged(
                args.output, subject, manifest
            ):
                sys.stderr.write(json.dumps({"error": "output_aliases_protected_input"}) + "\n")
                return 1
            try:
                result = reconcile_staged_subject(subject, manifest)
            except StagedManifestError:
                sys.stderr.write(json.dumps({"error": "staged_manifest_invalid"}) + "\n")
                return 1
            payload = result.model_dump_json(indent=2) + "\n"
            if args.output is not None:
                try:
                    write_text_atomic(args.output, payload)
                except OSError as exc:
                    sys.stderr.write(json.dumps({"error": type(exc).__name__}) + "\n")
                    return 1
            else:
                sys.stdout.write(payload)
            return 0 if result.status == "PASS" else 2
        root_arg: Path | None = args.repo_root
        root = (root_arg if root_arg is not None else Path.cwd()).resolve()
        if not root.is_dir():
            sys.stderr.write(json.dumps({"error": "repo_root_missing"}) + "\n")
            return 1
        if args.output is not None and _output_is_protected(args.output, root):
            sys.stderr.write(json.dumps({"error": "output_aliases_protected_input"}) + "\n")
            return 1
        result = reconcile(root)
        payload = result.model_dump_json(indent=2) + "\n"
        if args.output is not None:
            try:
                write_text_atomic(args.output, payload)
            except OSError as exc:
                sys.stderr.write(json.dumps({"error": type(exc).__name__}) + "\n")
                return 1
        else:
            sys.stdout.write(payload)
        return 0 if result.status == "PASS" else 2
    except (OSError, ValueError, RuntimeError) as exc:
        sys.stderr.write(json.dumps({"error": type(exc).__name__}) + "\n")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
