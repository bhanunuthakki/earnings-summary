"""Reconcile provisional roadmap baselines against fresh typed receipts."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from quality.atomic_write import write_text_atomic  # noqa: E402
from quality.roadmap_reconciliation import (  # noqa: E402
    ROADMAP_CANDIDATES,
    SOURCE_PATHS,
    reconcile,
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--repo-root", "--root", dest="repo_root", type=Path, default=Path.cwd())
    p.add_argument("--output", type=Path, default=None)
    return p.parse_args(argv)


def _output_is_protected(output: Path, root: Path) -> bool:
    """Check whether a requested output aliases a protected input path."""
    try:
        resolved_output = output.resolve()
    except OSError:
        return True
    for rel in (*SOURCE_PATHS.values(), *ROADMAP_CANDIDATES):
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


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        root = args.repo_root.resolve()
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
