"""Capture a declared full-suite or CI-shard pytest evidence receipt."""

from __future__ import annotations

import argparse
import hashlib
import os
import platform
import resource
import subprocess
import sys
import time
import uuid
from pathlib import Path

from pydantic import ValidationError

from src.quality.test_ci_performance import (
    FrozenTestCohort,
    WorkerEvidence,
    receipt_from_fragments,
    write_receipt,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", default=".")
    parser.add_argument("--cohort", choices=("full-suite", "ci-shard"), required=True)
    parser.add_argument("--source-shard", type=int)
    parser.add_argument("--source-shards", type=int, default=8)
    parser.add_argument("--split-count", type=int, default=1)
    parser.add_argument("--split-part", type=int, default=0)
    parser.add_argument("--cache-state", choices=("cold", "warm", "unknown"), required=True)
    parser.add_argument("--receipt", required=True)
    parser.add_argument("--fragments-dir", default=".tmp/quality/test-ci-performance")
    return parser


def _process_peak_rss_bytes() -> int:
    value = int(resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss)
    return value * 1024 if platform.system() != "Darwin" else value


def _selected_files(root: Path, args: argparse.Namespace) -> tuple[str, ...]:
    files = tuple(
        str(path.relative_to(root))
        for path in sorted((root / "tests").rglob("test_*.py"))
        if path.is_file()
        and path.relative_to(root).as_posix() != "tests/test_design_computed_canary.py"
    )
    if args.cohort != "ci-shard":
        return files
    selected = subprocess.run(
        [
            sys.executable,
            ".github/scripts/ci_gate.py",
            "select-tests",
            "--source-shard",
            str(args.source_shard),
            "--source-shards",
            str(args.source_shards),
            "--split-count",
            str(args.split_count),
            "--split-part",
            str(args.split_part),
        ],
        cwd=root,
        input=("\n".join(files) + "\n").encode(),
        capture_output=True,
        check=False,
    )
    if selected.returncode != 0:
        raise SystemExit("canonical CI shard selection failed")
    return tuple(line for line in selected.stdout.decode().splitlines() if line)


def main() -> int:
    args = _parser().parse_args()
    root = Path(args.repo_root).resolve()
    if args.cohort == "ci-shard" and args.source_shard is None:
        raise SystemExit("--source-shard is required for ci-shard")
    if args.cohort == "full-suite" and args.source_shard is not None:
        raise SystemExit("--source-shard is only valid for ci-shard")
    files = _selected_files(root, args)
    cohort = FrozenTestCohort(
        kind=args.cohort,
        source_shard=args.source_shard,
        source_shards=args.source_shards if args.cohort == "ci-shard" else None,
        split_count=args.split_count if args.cohort == "ci-shard" else None,
        split_part=args.split_part if args.cohort == "ci-shard" else None,
        test_files=files,
    )
    attempt_id = uuid.uuid4().hex
    fragment_root = Path(args.fragments_dir).resolve()
    attempt_dir = fragment_root / attempt_id
    attempt_dir.mkdir(parents=True, exist_ok=False)
    env = os.environ.copy()
    env.update(
        {
            "TEST_CI_PERFORMANCE_FRAGMENT_DIR": str(attempt_dir),
            "TEST_CI_PERFORMANCE_CACHE_STATE": args.cache_state,
            "EARNINGS_SUMMARY_DB_PATH": str(attempt_dir / "disposable.db"),
            "NO_NETWORK": "1",
        }
    )
    for name in ("FMP_API_KEY", "SEC_USER_AGENT", "OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
        env.pop(name, None)
    command = [
        sys.executable,
        "-m",
        "pytest",
        "-q",
        "-n",
        "2",
        "--dist=loadfile",
        "--durations=25",
        "-p",
        "src.quality.pytest_performance_plugin",
        *files,
    ]
    started = time.perf_counter()
    initial_receipt = receipt_from_fragments(
        root,
        cohort,
        [],
        attempt_id=attempt_id,
        execution_outcome="not_run",
        cache_state=args.cache_state,
    )
    write_receipt(initial_receipt, args.receipt)
    completed = subprocess.run(command, cwd=root, env=env, capture_output=True, check=False)
    wall = time.perf_counter() - started
    sys.stdout.buffer.write(completed.stdout)
    sys.stderr.buffer.write(completed.stderr)
    (attempt_dir / "pytest.stdout").write_bytes(completed.stdout)
    (attempt_dir / "pytest.stderr").write_bytes(completed.stderr)
    fragments: list[WorkerEvidence] = []
    fragment_errors: list[str] = []
    for path in sorted(attempt_dir.glob("worker-*.json")):
        try:
            fragments.append(WorkerEvidence.model_validate_json(path.read_text()))
        except (OSError, ValidationError, ValueError) as exc:
            fragment_errors.append(f"invalid worker fragment {path.name}: {type(exc).__name__}")
    receipt = receipt_from_fragments(
        root,
        cohort,
        fragments,
        attempt_id=attempt_id,
        execution_outcome="passed" if completed.returncode == 0 else "failed",
        cache_state=args.cache_state,
        fragment_errors=tuple(fragment_errors),
    )
    receipt = receipt.model_copy(
        update={
            "command_sha256": hashlib.sha256("\0".join(command).encode()).hexdigest(),
            "output_sha256": hashlib.sha256(completed.stdout + completed.stderr).hexdigest(),
            "process_wall_seconds": wall,
            "process_peak_rss_bytes": _process_peak_rss_bytes(),
        }
    )
    write_receipt(receipt, args.receipt)
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
