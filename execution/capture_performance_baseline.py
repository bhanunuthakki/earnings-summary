"""Capture a local performance baseline receipt."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from quality.performance import CompanionMeasures, capture_performance_baseline  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--command", required=True, help="Benchmark command; parsed without a shell"
    )
    parser.add_argument("--repo-root", type=Path, default=PROJECT_ROOT)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--samples", type=int, default=7)
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument(
        "--companion-json", type=Path, help="JSON object with declared companion measures"
    )
    parser.add_argument(
        "--provenance", choices=["mac_guidance", "approved_windows_production_shaped"]
    )
    args = parser.parse_args(argv)
    companion = None
    if args.companion_json:
        companion = CompanionMeasures.model_validate_json(
            args.companion_json.read_text(encoding="utf-8")
        ).model_dump()
    receipt = capture_performance_baseline(
        args.repo_root,
        args.command,
        samples=args.samples,
        timeout_seconds=args.timeout,
        companion_measures=companion,
        provenance=args.provenance,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(receipt.model_dump_json(indent=2) + "\n", encoding="utf-8")
    return 0 if receipt.status == "PASS" else 2 if receipt.status == "HOLD" else 1


if __name__ == "__main__":
    raise SystemExit(main())
