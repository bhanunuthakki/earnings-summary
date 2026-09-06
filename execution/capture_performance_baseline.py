"""Capture a raw local performance timing receipt."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from quality.performance import (  # noqa: E402
    PerformanceExecutionError,
    PerformanceIdentityError,
    PerformanceInputError,
    PerformanceOutputError,
    capture_performance_baseline,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--command", required=True, help="Benchmark command")
    parser.add_argument("--repo-root", type=Path, default=PROJECT_ROOT)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--samples", type=int, default=7)
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--provenance", default="local-timing")
    parser.add_argument("--config", action="append", default=None, dest="config")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        receipt = capture_performance_baseline(
            args.repo_root,
            args.command,
            samples=args.samples,
            timeout_seconds=args.timeout,
            config_paths=args.config,
            provenance=args.provenance,
        )
    except PerformanceInputError:
        print("performance baseline input is invalid", file=sys.stderr)
        return 2
    except PerformanceIdentityError:
        print("performance baseline identity is unavailable", file=sys.stderr)
        return 2
    except PerformanceExecutionError:
        print("performance baseline execution failed", file=sys.stderr)
        return 1
    except PerformanceOutputError:
        print("performance baseline output is invalid", file=sys.stderr)
        return 1
    except Exception:
        print("performance baseline collection failed", file=sys.stderr)
        return 1
    try:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(receipt.model_dump_json(indent=2) + "\n", encoding="utf-8")
    except Exception:
        print("performance baseline output write failed", file=sys.stderr)
        return 1
    if receipt.status == "HOLD":
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
