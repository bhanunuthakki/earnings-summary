"""Verify Windows Task Scheduler manifest, XML files, and wrapper scripts for drift.

Usage:
    python execution/verify_scheduler_wrappers.py
    python execution/verify_scheduler_wrappers.py --json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC = PROJECT_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from scheduler_manifest import (  # noqa: E402
    load_manifest,
    validate_source_tree,
)

CRON_DIR = PROJECT_ROOT / "cron"
MANIFEST_PATH = CRON_DIR / "task_manifest.json"
TMP_DIR = PROJECT_ROOT / ".tmp"


def verify_scheduler_fleet(cron_dir: Path, manifest_path: Path) -> tuple[bool, list[str], int]:
    """Verify manifest consistency and wrapper script invariants."""
    if not manifest_path.exists():
        return False, [f"Manifest missing: {manifest_path}"], 0

    try:
        manifest = load_manifest(manifest_path)
    except Exception as e:
        return False, [f"Manifest unparseable: {e}"], 0

    errors = validate_source_tree(manifest, cron_dir=cron_dir)
    return len(errors) == 0, errors, len(manifest.tasks)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cron-dir", type=Path, default=CRON_DIR, help="Directory containing cron files")
    parser.add_argument("--manifest", type=Path, default=MANIFEST_PATH, help="Path to task manifest")
    parser.add_argument("--json", action="store_true", help="Emit JSON output")
    args = parser.parse_args(argv)

    passed, errors, task_count = verify_scheduler_fleet(args.cron_dir, args.manifest)

    receipt = {
        "status": "PASS" if passed else "FAILED",
        "task_count": task_count,
        "errors": errors,
    }

    TMP_DIR.mkdir(parents=True, exist_ok=True)
    (TMP_DIR / "scheduler_wrappers_receipt.json").write_text(json.dumps(receipt, indent=2), encoding="utf-8")

    if not passed:
        sys.stderr.write(f"=== Scheduler Fleet Verification FAILED ({len(errors)} errors) ===\n")
        for err in errors:
            sys.stderr.write(f"  x {err}\n")
        return 1

    if args.json:
        print(json.dumps(receipt, indent=2))
    else:
        print("=== Scheduler Fleet Verification: [PASS] ===")
        print(f"  Validated Tasks: {task_count}")
        print("  All XML definitions, wrapper scripts, and exit tails match task_manifest.json exactly.")
        print(f"  Receipt Emitted: {TMP_DIR / 'scheduler_wrappers_receipt.json'}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
