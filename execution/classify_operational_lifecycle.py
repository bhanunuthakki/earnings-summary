"""Generate or validate the evidence-bound operational lifecycle inventory."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from quality.lifecycle import (  # noqa: E402
    MAX_STDOUT_BYTES,
    LifecycleError,
    build_inventory,
    load_inventory,
    validate_inventory,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", "--root", dest="repo_root", type=Path, default=Path.cwd())
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--validate", type=Path, help="Validate an existing receipt against the worktree"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    root = args.repo_root.resolve()
    try:
        if args.validate:
            persisted = load_inventory(args.validate)
            drift = validate_inventory(root, persisted)
            sys.stdout.write(
                json.dumps({"status": "HOLD" if drift else "PASS", "violations": drift}, indent=2)
                + "\n"
            )
            return 2 if drift else 0
        report = build_inventory(root)
        payload = report.model_dump_json(indent=2) + "\n"
        output = args.output
        if output is None and len(payload.encode("utf-8")) > MAX_STDOUT_BYTES:
            output = root / ".tmp/quality/lifecycle-inventory.json"
        if output is not None:
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(payload, encoding="utf-8")
            sys.stdout.write(
                json.dumps(
                    {
                        "output": str(output),
                        "status": report.status,
                        "coverage": report.coverage,
                    },
                    sort_keys=True,
                )
                + "\n"
            )
        else:
            sys.stdout.write(payload)
        return 0 if report.status == "PASS" else 2
    except (LifecycleError, OSError, ValueError) as exc:
        sys.stderr.write(json.dumps({"error": type(exc).__name__, "message": str(exc)}) + "\n")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
