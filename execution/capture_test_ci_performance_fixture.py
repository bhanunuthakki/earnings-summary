"""Small repository-owned capture fixture used only by collector unit tests.

It implements the production capture CLI's receipt boundary without launching
pytest.  The collector never selects an arbitrary command; this entrypoint is
an explicit hermetic test mode.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.quality.test_ci_performance import (
    FrozenTestCohort,
    PhaseTimings,
    TestCounts,
    WorkerEvidence,
    node_identity,
    receipt_from_fragments,
    write_receipt,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--cohort", choices=("full-suite", "ci-shard"), required=True)
    parser.add_argument("--cache-state", choices=("cold", "warm", "unknown"), required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--fragments-dir", type=Path)
    parser.add_argument("--source-shard", type=int)
    parser.add_argument("--source-shards", type=int)
    parser.add_argument("--split-count", type=int)
    parser.add_argument("--split-part", type=int)
    parser.add_argument(
        "--timing-profile", choices=("stable", "noisy", "max-unstable"), default="stable"
    )
    args = parser.parse_args()
    fixture_root = Path(__file__).resolve().parents[1]
    files = tuple(
        str(path.relative_to(fixture_root))
        for path in sorted((fixture_root / "tests").rglob("test_*.py"))
        if path.is_file() and path.name != "test_design_computed_canary.py"
    )
    cohort = FrozenTestCohort(
        kind=args.cohort,
        source_shard=args.source_shard if args.cohort == "ci-shard" else None,
        source_shards=args.source_shards if args.cohort == "ci-shard" else None,
        split_count=args.split_count if args.cohort == "ci-shard" else None,
        split_part=args.split_part if args.cohort == "ci-shard" else None,
        test_files=files,
    )
    nodes = tuple(f"{name}::fixture" for name in files)
    midpoint = max(1, len(nodes) // 2)
    workers: list[WorkerEvidence] = []
    for index, owned in enumerate((nodes[:midpoint], nodes[midpoint:])):
        if not owned:
            continue
        workers.append(
            WorkerEvidence(
                worker_id=f"fixture-{index}",
                node_ids=owned,
                node_id_sha256=node_identity(owned),
                counts=TestCounts(
                    passed=len(owned), failed=0, errors=0, skipped=0, xfailed=0, xpassed=0
                ),
                timings=PhaseTimings(
                    collection_seconds=0.001,
                    setup_seconds=0.001,
                    call_seconds=0.001,
                    teardown_seconds=0.001,
                ),
                elapsed_seconds=0.004,
                peak_rss_bytes=1,
                cache_state=args.cache_state,
            )
        )
    receipt = receipt_from_fragments(
        args.repo_root,
        cohort,
        workers,
        attempt_id=args.receipt.stem,
        execution_outcome="passed",
        cache_state=args.cache_state,
        worker_count=len(workers),
    )
    index = int(args.receipt.stem.rsplit("-", 1)[1])
    wall = (
        1.0
        if args.timing_profile == "stable"
        else (
            1.0 + (index % 3) * 0.3
            if args.timing_profile == "noisy" and index < 8
            else (1.0 + (index % 3) * 0.5 if args.timing_profile == "max-unstable" else 1.0)
        )
    )
    receipt = receipt.model_copy(update={"process_wall_seconds": wall})
    write_receipt(receipt, args.receipt)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
