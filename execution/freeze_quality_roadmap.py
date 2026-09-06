"""Generate or validate the deterministic BHA-122 roadmap freeze."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from quality.roadmap_freeze import build_freeze, validate_freeze  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--output", type=Path)
    parser.add_argument("--validate", type=Path)
    args = parser.parse_args(argv)
    root = args.repo_root.resolve()
    try:
        if args.validate:
            freeze = validate_freeze(root, args.validate)
            print(json.dumps({"status": freeze.status, "hold_reasons": freeze.hold_reasons}))
            return 0
        freeze = build_freeze(root)
        payload = freeze.model_dump_json(indent=2) + "\n"
        output = args.output or root / "docs/quality/roadmap-freeze.json"
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(payload, encoding="utf-8")
        print(json.dumps({"output": str(output), "status": freeze.status}))
        return 0
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        sys.stderr.write(json.dumps({"error": type(exc).__name__, "message": str(exc)}) + "\n")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
