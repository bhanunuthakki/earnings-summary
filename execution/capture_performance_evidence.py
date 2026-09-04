"""Capture a frozen Train-0 performance cohort evidence receipt."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from quality.performance import (  # noqa: E402
    COHORT_REGISTRY,
    capture_performance_evidence,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--cohort",
        required=True,
        choices=sorted(COHORT_REGISTRY),
    )
    parser.add_argument("--repo-root", type=Path, default=PROJECT_ROOT)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--samples", type=int, default=7)
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument(
        "--provenance",
        choices=["mac_guidance", "approved_windows_production_shaped"],
        required=True,
    )
    parser.add_argument("--baseline-revision")
    parser.add_argument("--current-revision")
    args = parser.parse_args(argv)
    cohort = COHORT_REGISTRY[args.cohort]
    receipt = capture_performance_evidence(
        args.repo_root,
        cohort,
        samples=args.samples,
        timeout_seconds=args.timeout,
        provenance=args.provenance,
        baseline_revision=args.baseline_revision,
        current_revision=args.current_revision,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(receipt.model_dump_json(indent=2) + "\n", encoding="utf-8")
    return 0 if receipt.baseline.status == "PASS" else 2 if receipt.baseline.status == "HOLD" else 1


if __name__ == "__main__":
    raise SystemExit(main())
