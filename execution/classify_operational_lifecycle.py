"""Generate or validate the operational lifecycle inventory."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))
from quality.atomic_write import write_text_atomic  # noqa: E402
from quality.lifecycle import (  # noqa: E402
    LifecycleError,
    build_inventory,
    is_protected_lifecycle_output,
    load_inventory,
    validate_inventory,
)

MAX_STDOUT_BYTES = 100_000


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--repo-root", "--root", dest="repo_root", type=Path, default=Path.cwd())
    p.add_argument("--output", type=Path, default=None)
    p.add_argument("--validate", type=Path, default=None)
    return p


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    root = args.repo_root.resolve()
    try:
        if args.validate is not None:
            persisted = load_inventory(Path(args.validate))
            drift = validate_inventory(root, persisted)
            sys.stdout.write(
                json.dumps(
                    {"status": "HOLD" if drift else "PASS", "violations": list(drift)}, indent=2
                )
                + "\n"
            )
            return 2 if drift else 0
        report = build_inventory(root)
        payload = report.model_dump_json(indent=2) + "\n"
        output = args.output
        if output is None and len(payload.encode()) > MAX_STDOUT_BYTES:
            output = root / ".tmp/quality/lifecycle-inventory.json"
        if output is not None:
            if is_protected_lifecycle_output(root, output, report):
                sys.stderr.write(
                    json.dumps({"error": "LifecycleError", "message": "protected lifecycle output"})
                    + "\n"
                )
                return 1
            write_text_atomic(output, payload)
            sys.stdout.write(
                json.dumps(
                    {"output": str(output), "status": report.status, "coverage": report.coverage},
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
