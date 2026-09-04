"""Prepare/resume a bounded revision-aware CI performance collection."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class CollectionManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal["ci-paired-collection/v1"] = "ci-paired-collection/v1"
    cohort: Literal["full-suite", "ci-shard"]
    source_shard: int | None = None
    source_shards: int = Field(default=8, ge=1)
    split_count: int = Field(default=1, ge=1)
    split_part: int = Field(default=0, ge=0)
    baseline_revision: str = Field(pattern=r"^[0-9a-f]{40}$")
    current_revision: str = Field(pattern=r"^[0-9a-f]{40}$")
    measured_repeats: int = Field(default=7, ge=7, le=21)
    max_measured_repeats: int = Field(default=21, ge=7, le=21)
    warmups: int = Field(default=1, ge=1, le=1)
    cache_proof_method: Literal["runner-isolated", "unavailable"] = "unavailable"
    network_proof_method: Literal["runner-isolated", "unavailable"] = "unavailable"
    # Only the repository-owned hermetic fixture is permitted for tests.  The
    # production default is deliberately the real capture entrypoint.
    capture_mode: Literal["production", "hermetic"] = "production"
    timing_profile: Literal["stable", "noisy", "max-unstable"] = "stable"

    @model_validator(mode="after")
    def proof_methods_are_honest(self) -> CollectionManifest:
        if self.cache_proof_method != "unavailable" or self.network_proof_method != "unavailable":
            raise ValueError("runner proof acquisition is not implemented by this collector")
        if self.cohort == "full-suite" and self.source_shard is not None:
            raise ValueError("full-suite manifest cannot carry shard coordinates")
        if self.cohort == "ci-shard" and self.source_shard is None:
            raise ValueError("ci-shard manifest requires source_shard")
        if (
            self.cohort == "ci-shard"
            and self.source_shard is not None
            and not 1 <= self.source_shard <= self.source_shards
        ):
            raise ValueError("source_shard is outside source_shards")
        if not 0 <= self.split_part < self.split_count:
            raise ValueError("split_part is outside split_count")
        return self


class CollectionState(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal["ci-paired-state/v1"] = "ci-paired-state/v1"
    manifest_sha256: str
    baseline_completed: int = 0
    current_completed: int = 0
    baseline_warmups_completed: int = 0
    current_warmups_completed: int = 0
    completed_units: tuple[str, ...] = ()
    status: Literal["pending", "interrupted", "complete"] = "pending"
    pairing_status: Literal["pending", "HOLD", "INVALID", "PASS"] = "pending"
    measured_pairs: int = 0
    stop_reason: Literal["pending", "stable", "max-exhausted"] = "pending"
    receipt_paths: tuple[str, ...] = ()


def _digest(manifest: CollectionManifest) -> str:
    import hashlib

    return hashlib.sha256(manifest.model_dump_json().encode()).hexdigest()


def _revision(root: Path, value: str) -> None:
    observed = subprocess.check_output(
        ["git", "-C", str(root), "rev-parse", value], text=True
    ).strip()
    if observed != value:
        raise ValueError("revision identity does not resolve exactly")


def _capture_command(
    mode: str,
    worktree: Path,
    destination: Path,
    manifest: CollectionManifest,
    repeat: int,
    fragments_dir: Path,
    python: str,
) -> list[str]:
    # Production code must be resolved from the immutable revision.  The
    # hermetic fixture is deliberately a test-only boundary and is resolved
    # from this collector's checkout because it is not part of old revisions.
    script = (
        (worktree / "execution" / "capture_test_ci_performance.py")
        if mode == "production"
        else Path(__file__).with_name("capture_test_ci_performance_fixture.py")
    )
    command = [
        python,
        str(script),
        "--repo-root",
        str(worktree),
        "--cohort",
        manifest.cohort,
        "--cache-state",
        "cold" if repeat == 0 else "warm",
        "--receipt",
        str(destination),
        "--fragments-dir",
        str(fragments_dir),
    ]
    if manifest.cohort == "ci-shard":
        if manifest.source_shard is None:
            raise ValueError("ci-shard manifest requires source_shard")
        command.extend(
            [
                "--source-shard",
                str(manifest.source_shard),
                "--source-shards",
                str(manifest.source_shards),
                "--split-count",
                str(manifest.split_count),
                "--split-part",
                str(manifest.split_part),
            ]
        )
    if manifest.capture_mode == "hermetic":
        command.extend(["--timing-profile", manifest.timing_profile])
    return command


def _unit_sort_key(value: str) -> tuple[str, int]:
    side, repeat = value.rsplit("-", 1)
    return side, int(repeat)


def _stable(paths: list[Path]) -> bool:
    if len(paths) < 7:
        return False
    import statistics

    values = [
        json.loads(path.read_text(encoding="utf-8")).get("process_wall_seconds") for path in paths
    ][-7:]
    if any(not isinstance(value, (int, float)) for value in values):
        return False
    median = statistics.median(values)
    return (
        median == 0 or statistics.median(abs(value - median) for value in values) <= median * 0.05
    )


def _receipt_revision(path: Path, revision: str) -> None:
    from src.quality.test_ci_performance import TestCIPerformanceReceipt

    receipt = TestCIPerformanceReceipt.model_validate_json(path.read_text(encoding="utf-8"))
    if receipt.revision != revision:
        raise ValueError("receipt revision does not match its immutable worktree")


def _validate_resume(
    state: CollectionState, receipt_dir: Path, manifest: CollectionManifest
) -> None:
    from src.quality.test_ci_performance import TestCIPerformanceReceipt, cohort_identity

    expected_units = {
        f"{side}-{repeat}"
        for side in ("baseline", "current")
        for repeat in range(manifest.warmups + manifest.max_measured_repeats)
    }
    if len(state.completed_units) != len(set(state.completed_units)):
        raise SystemExit("state contains duplicate collection units")
    if not set(state.completed_units).issubset(expected_units):
        raise SystemExit("state contains an out-of-bounds collection unit")
    if state.status == "complete":
        if not manifest.measured_repeats <= state.measured_pairs <= manifest.max_measured_repeats:
            raise SystemExit("complete state has an invalid measured pair count")
        expected_units = {
            f"{side}-{repeat}"
            for side in ("baseline", "current")
            for repeat in range(manifest.warmups + state.measured_pairs)
        }
        if set(state.completed_units) != expected_units:
            raise SystemExit("complete state does not contain the recorded contiguous units")
    attempts: set[str] = set()
    cohorts: list[object] = []
    for unit, raw_path in zip(state.completed_units, state.receipt_paths, strict=True):
        if unit.rsplit("-", 1)[0] not in {"baseline", "current"}:
            raise SystemExit("state contains an unknown collection unit")
        expected = receipt_dir / f"{unit}.json"
        path = Path(raw_path).resolve()
        if path != expected.resolve() or not path.is_file():
            raise SystemExit("state receipt path is missing or outside the receipt root")
        receipt = TestCIPerformanceReceipt.model_validate_json(path.read_text(encoding="utf-8"))
        revision = (
            manifest.baseline_revision
            if unit.startswith("baseline-")
            else manifest.current_revision
        )
        if receipt.revision != revision or receipt.cohort_sha256 != cohort_identity(receipt.cohort):
            raise SystemExit("resumed receipt identity does not match the manifest")
        if receipt.cohort.kind != manifest.cohort:
            raise SystemExit("resumed receipt cohort kind does not match the manifest")
        if (
            receipt.cohort.source_shard != manifest.source_shard
            or receipt.cohort.source_shards
            != (manifest.source_shards if manifest.cohort == "ci-shard" else None)
            or receipt.cohort.split_count
            != (manifest.split_count if manifest.cohort == "ci-shard" else None)
            or receipt.cohort.split_part
            != (manifest.split_part if manifest.cohort == "ci-shard" else None)
        ):
            raise SystemExit("resumed receipt shard coordinates do not match the manifest")
        cohorts.append(receipt.cohort)
        expected_cache = "cold" if unit.endswith("-0") else "warm"
        if receipt.cache_state != expected_cache:
            raise SystemExit("resumed receipt cache phase does not match its unit")
        if receipt.attempt_id in attempts:
            raise SystemExit("resumed receipts contain duplicate attempts")
        attempts.add(receipt.attempt_id)
    if cohorts and any(cohort != cohorts[0] for cohort in cohorts[1:]):
        raise SystemExit("resumed receipts do not share an exact frozen cohort")
    expected_baseline = sum(
        unit.startswith("baseline-") and int(unit.rsplit("-", 1)[1]) >= manifest.warmups
        for unit in state.completed_units
    )
    expected_current = sum(
        unit.startswith("current-") and int(unit.rsplit("-", 1)[1]) >= manifest.warmups
        for unit in state.completed_units
    )
    expected_baseline_warmups = sum(unit == "baseline-0" for unit in state.completed_units)
    expected_current_warmups = sum(unit == "current-0" for unit in state.completed_units)
    if (
        state.baseline_completed != expected_baseline
        or state.current_completed != expected_current
        or state.baseline_warmups_completed != expected_baseline_warmups
        or state.current_warmups_completed != expected_current_warmups
    ):
        raise SystemExit("state completion counters do not match validated units")
    if state.status == "complete":
        baseline_paths = [
            receipt_dir / f"baseline-{i}.json" for i in range(1, state.measured_pairs + 1)
        ]
        current_paths = [
            receipt_dir / f"current-{i}.json" for i in range(1, state.measured_pairs + 1)
        ]
        stable = _stable(baseline_paths) and _stable(current_paths)
        if state.stop_reason == "stable" and not stable:
            raise SystemExit("stable stop is not supported by the recorded timings")
        if state.stop_reason == "max-exhausted" and (
            state.measured_pairs != manifest.max_measured_repeats or stable
        ):
            raise SystemExit("max-exhausted stop is inconsistent with recorded timings")
    if state.status == "complete" and not (receipt_dir.parent / "pairing.json").is_file():
        raise SystemExit("complete state is missing its pairing receipt")


def _write_state(path: Path, state: CollectionState) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(state.model_dump_json(indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--interrupt-after", type=int, default=None)
    parser.add_argument("--capture-python", default=sys.executable)
    args = parser.parse_args(argv)
    manifest = CollectionManifest.model_validate_json(args.manifest.read_text(encoding="utf-8"))
    root = args.repo_root.resolve()
    _revision(root, manifest.baseline_revision)
    _revision(root, manifest.current_revision)
    if manifest.baseline_revision == manifest.current_revision:
        raise SystemExit("baseline and current revisions must differ")
    digest = _digest(manifest)
    if args.state.exists():
        state = CollectionState.model_validate_json(args.state.read_text(encoding="utf-8"))
        if state.manifest_sha256 != digest:
            raise SystemExit("state manifest identity mismatch")
    else:
        state = CollectionState(manifest_sha256=digest)
    if args.interrupt_after is not None and (
        args.interrupt_after < 0
        or args.interrupt_after > 2 * (manifest.warmups + manifest.max_measured_repeats)
    ):
        raise SystemExit("interrupt point is outside bounded repeat count")
    receipt_dir = args.state.parent / "receipts"
    completed = set(state.completed_units)
    launched = 0
    if len(state.receipt_paths) != len(completed):
        raise SystemExit("state receipt/unit cardinality mismatch")
    _validate_resume(state, receipt_dir, manifest)
    with tempfile.TemporaryDirectory(prefix="bha115-worktrees-") as temp_name:
        worktrees: dict[str, Path] = {}
        try:
            for side, revision in (
                ("baseline", manifest.baseline_revision),
                ("current", manifest.current_revision),
            ):
                worktree = Path(temp_name) / side
                worktrees[side] = worktree
                subprocess.run(
                    [
                        "git",
                        "-C",
                        str(root),
                        "worktree",
                        "add",
                        "--detach",
                        str(worktree),
                        revision,
                    ],
                    check=True,
                    capture_output=True,
                )
                _revision(worktree, revision)
                if subprocess.check_output(
                    ["git", "-C", str(worktree), "status", "--porcelain"], text=True
                ).strip():
                    raise ValueError("benchmark worktree is dirty")
            for repeat in range(manifest.warmups + manifest.max_measured_repeats):
                for side, revision in (
                    ("baseline", manifest.baseline_revision),
                    ("current", manifest.current_revision),
                ):
                    worktree = worktrees[side]
                    key = f"{side}-{repeat}"
                    if key in completed:
                        continue
                    receipt_dir.mkdir(parents=True, exist_ok=True)
                    destination = receipt_dir / f"{key}.json"
                    command = _capture_command(
                        manifest.capture_mode,
                        worktree,
                        destination,
                        manifest,
                        repeat,
                        receipt_dir / "fragments" / key,
                        args.capture_python,
                    )
                    completed_process = subprocess.run(command, cwd=worktree, check=False)
                    if completed_process.returncode not in (0, 2):
                        raise SystemExit(
                            f"capture failed for {key}: exit {completed_process.returncode}"
                        )
                    _receipt_revision(destination, revision)
                    with (args.state.parent / "launch-order.log").open(
                        "a", encoding="utf-8"
                    ) as log:
                        log.write(f"{key}\n")
                    completed.add(key)
                    launched += 1
                    state = state.model_copy(
                        update={
                            "receipt_paths": tuple(
                                str(receipt_dir / f"{item}.json")
                                for item in sorted(completed, key=_unit_sort_key)
                            ),
                            "completed_units": tuple(sorted(completed, key=_unit_sort_key)),
                            "baseline_completed": sum(
                                k.startswith("baseline-")
                                and int(k.rsplit("-", 1)[1]) >= manifest.warmups
                                for k in completed
                            ),
                            "current_completed": sum(
                                k.startswith("current-")
                                and int(k.rsplit("-", 1)[1]) >= manifest.warmups
                                for k in completed
                            ),
                            "baseline_warmups_completed": sum(k == "baseline-0" for k in completed),
                            "current_warmups_completed": sum(k == "current-0" for k in completed),
                        }
                    )
                    _write_state(args.state, state)
                    if args.interrupt_after is not None and launched >= args.interrupt_after:
                        state = state.model_copy(update={"status": "interrupted"})
                        _write_state(args.state, state)
                        return 130
                measured = repeat + 1 - manifest.warmups
                if measured >= manifest.measured_repeats:
                    baseline_paths = [
                        receipt_dir / f"baseline-{i}.json" for i in range(1, repeat + 1)
                    ]
                    current_paths = [
                        receipt_dir / f"current-{i}.json" for i in range(1, repeat + 1)
                    ]
                    if _stable(baseline_paths) and _stable(current_paths):
                        state = state.model_copy(
                            update={"measured_pairs": measured, "stop_reason": "stable"}
                        )
                        _write_state(args.state, state)
                        break
            else:
                state = state.model_copy(
                    update={
                        "measured_pairs": manifest.max_measured_repeats,
                        "stop_reason": "max-exhausted",
                    }
                )
                _write_state(args.state, state)
        finally:
            for worktree in worktrees.values():
                removed = subprocess.run(
                    ["git", "-C", str(root), "worktree", "remove", "--force", str(worktree)],
                    check=False,
                    capture_output=True,
                )
                if removed.returncode != 0:
                    raise RuntimeError("benchmark worktree cleanup failed")
    if (
        state.baseline_completed < manifest.measured_repeats
        or state.current_completed < manifest.measured_repeats
    ):
        raise SystemExit("collection ended without all warmup and measured receipts")
    state.status = "complete"
    _write_state(args.state, state)
    from src.quality.test_ci_pairing import aggregate_test_ci_pairs

    payloads = [
        (Path(path), json.loads(Path(path).read_text(encoding="utf-8")))
        for path in state.receipt_paths
    ]
    baseline = [
        payload
        for _, payload in sorted(
            ((path, payload) for path, payload in payloads if path.name.startswith("baseline-")),
            key=lambda pair: int(pair[0].stem.split("-")[1]),
        )
    ]
    current = [
        payload
        for _, payload in sorted(
            ((path, payload) for path, payload in payloads if path.name.startswith("current-")),
            key=lambda pair: int(pair[0].stem.split("-")[1]),
        )
    ]
    pairing = aggregate_test_ci_pairs(baseline, current)
    state.pairing_status = pairing.status
    _write_state(args.state, state)
    args.state.with_name("pairing.json").write_text(
        pairing.model_dump_json(indent=2) + "\n", encoding="utf-8"
    )
    print(state.model_dump_json())
    return 0 if pairing.status == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
