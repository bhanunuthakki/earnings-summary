"""Capture one paired immutable performance experiment receipt."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from quality.atomic_write import write_text_atomic  # noqa: E402
from quality.performance_experiment import (  # noqa: E402
    PerformanceExperimentError,
    capture_performance_experiment,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=PROJECT_ROOT)
    parser.add_argument("--declaration", required=True)
    parser.add_argument("--control-revision", required=True)
    parser.add_argument("--treatment-revision", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        receipt = capture_performance_experiment(
            args.repo_root,
            declaration_path=args.declaration,
            control_revision=args.control_revision,
            treatment_revision=args.treatment_revision,
        )
        write_text_atomic(args.output, receipt.model_dump_json(indent=2) + "\n")
    except (OSError, PerformanceExperimentError):
        print('{"event":"performance_experiment_failed"}', file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
