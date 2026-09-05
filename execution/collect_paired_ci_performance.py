"""Prepare/resume a bounded revision-aware CI performance collection."""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import subprocess
import sys
import tempfile
from collections.abc import Generator
from pathlib import Path
from typing import cast

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.quality.ci_collection import (  # noqa: E402
    CollectionManifest,
    CollectionState,
    capture_python_sha256,
)
from src.quality.ci_collection import (  # noqa: E402
    file_sha256 as _file_sha256,
)
from src.quality.ci_collection import (  # noqa: E402
    manifest_digest as _digest,
)
from src.quality.ci_collection import (  # noqa: E402
    population as _population,
)
from src.quality.ci_collection import (  # noqa: E402
    trusted_paths as _trusted_paths,
)


def _revision(root: Path, value: str) -> None:
    observed = subprocess.check_output(
        ["git", "-C", str(root), "rev-parse", value], text=True
    ).strip()
    if observed != value:
        raise ValueError("revision identity does not resolve exactly")


def capture_command(
    mode: str,
    worktree: Path,
    destination: Path,
    manifest: CollectionManifest,
    repeat: int,
    fragments_dir: Path,
    python: str,
    sqlite_preload: str | None = None,
    sqlite_preload_sha256: str | None = None,
) -> list[str]:
    # The production harness is pinned to this collector checkout.  Only the
    # test/config/source inputs and pytest invocation root come from the
    # immutable revision worktree; a tested revision cannot replace the
    # evidence writer or its shard selector.
    script = (
        Path(__file__).with_name("capture_test_ci_performance.py").resolve()
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
    if sqlite_preload is not None and mode == "production":
        if sqlite_preload_sha256 is None:
            raise ValueError("sqlite_preload_sha256 is required with sqlite_preload")
        command.extend(
            ["--sqlite-preload", sqlite_preload, "--sqlite-preload-sha256", sqlite_preload_sha256]
        )
    return command


# Preserve the collector's existing private helper name for callers that pin it.
_capture_command = capture_command


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
    state: CollectionState,
    receipt_dir: Path,
    manifest: CollectionManifest,
    *,
    population_sha256: str | None = None,
    expected_test_files: tuple[str, ...] = (),
    expected_node_ids: tuple[str, ...] = (),
) -> None:
    from src.quality.test_ci_performance import TestCIPerformanceReceipt, cohort_identity

    expected_units = {
        f"{side}-{repeat}"
        for side in ("baseline", "current")
        for repeat in range(manifest.warmups + manifest.max_measured_repeats)
    }
    if state.population_sha256 != population_sha256:
        raise SystemExit("state expected-population identity mismatch")
    if manifest.capture_mode == "production":
        harness, selector, plugin = _trusted_paths()
        if state.trusted_harness_sha256 != _file_sha256(harness):
            raise SystemExit("state trusted capture harness identity mismatch")
        if state.trusted_selector_sha256 != _file_sha256(selector):
            raise SystemExit("state trusted selector identity mismatch")
        if state.trusted_plugin_sha256 != _file_sha256(plugin):
            raise SystemExit("state trusted pytest plugin identity mismatch")
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
        if manifest.capture_mode == "production":
            harness, selector, plugin = _trusted_paths()
            trusted = {item.path: item.sha256 for item in receipt.configuration}
            if trusted.get("__trusted__/capture_test_ci_performance.py") != _file_sha256(harness):
                raise SystemExit("resumed receipt trusted harness identity is invalid")
            if trusted.get("__trusted__/.github/scripts/ci_gate.py") != _file_sha256(selector):
                raise SystemExit("resumed receipt trusted selector identity is invalid")
            if trusted.get("__trusted__/src/quality/pytest_performance_plugin.py") != _file_sha256(
                plugin
            ):
                raise SystemExit("resumed receipt trusted pytest plugin identity is invalid")
        if receipt.cohort.kind != manifest.cohort:
            raise SystemExit("resumed receipt cohort kind does not match the manifest")
        if expected_test_files and receipt.cohort.test_files != expected_test_files:
            raise SystemExit("resumed receipt test-file population does not match the manifest")
        if expected_node_ids:
            actual_nodes = tuple(
                sorted(node for worker in receipt.workers for node in worker.node_ids)
            )
            if actual_nodes != expected_node_ids:
                raise SystemExit("resumed receipt node population does not match the manifest")
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


@contextlib.contextmanager
def collection_state_lock(path: Path) -> Generator[None, None, None]:
    """Hold an exclusive lock for the full collection lifetime."""
    lock_path = path.with_name(f"{path.name}.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    acquired = False
    try:
        try:
            if os.name == "nt":
                import msvcrt

                os.lseek(descriptor, 0, os.SEEK_SET)
                msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except (BlockingIOError, OSError) as exc:
            raise SystemExit("collection state is already locked by another writer") from exc
        yield
    finally:
        try:
            if acquired:
                if os.name == "nt":
                    import msvcrt

                    os.lseek(descriptor, 0, os.SEEK_SET)
                    msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def _main_unlocked(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--interrupt-after", type=int, default=None)
    parser.add_argument("--capture-python", default=sys.executable)
    parser.add_argument(
        "--sqlite-preload",
        help="Optional SQLite shared library passed to the production capture harness.",
    )
    parser.add_argument(
        "--sqlite-preload-sha256",
        help="Expected SHA-256 for the SQLite preload library.",
    )
    args = parser.parse_args(argv)
    args.manifest = args.manifest.resolve()
    args.state = args.state.resolve()
    args.repo_root = args.repo_root.resolve()
    manifest = CollectionManifest.model_validate_json(args.manifest.read_text(encoding="utf-8"))
    if (args.sqlite_preload is None) != (args.sqlite_preload_sha256 is None):
        raise SystemExit("SQLite preload path and SHA-256 must be supplied together")
    if args.sqlite_preload is not None and manifest.capture_mode != "production":
        raise SystemExit("SQLite preload is only supported for production capture")
    root = args.repo_root
    harness, selector, plugin = _trusted_paths()
    harness_sha256 = _file_sha256(harness)
    selector_sha256 = _file_sha256(selector)
    if manifest.capture_mode == "production":
        if manifest.trusted_harness_sha256 != harness_sha256:
            raise SystemExit("manifest trusted capture harness identity does not match collector")
        if manifest.trusted_selector_sha256 != selector_sha256:
            raise SystemExit("manifest trusted selector identity does not match collector")
        plugin_sha256 = _file_sha256(plugin)
        if manifest.trusted_plugin_sha256 != plugin_sha256:
            raise SystemExit("manifest trusted pytest plugin identity does not match collector")
    else:
        plugin_sha256 = None
    capture_python_digest = capture_python_sha256(args.capture_python)
    population, population_sha256 = _population(root, manifest, selector)
    population_path = args.state.parent / "expected-population.json"
    encoded_population = json.dumps(population, sort_keys=True, separators=(",", ":")) + "\n"
    if (
        population_path.exists()
        and population_path.read_text(encoding="utf-8") != encoded_population
    ):
        raise SystemExit("expected-population.json does not match the immutable manifest cohort")
    population_path.parent.mkdir(parents=True, exist_ok=True)
    if not population_path.exists():
        population_path.write_text(encoded_population, encoding="utf-8")
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
        state = CollectionState(
            manifest_sha256=digest,
            population_sha256=population_sha256,
            trusted_harness_sha256=harness_sha256
            if manifest.capture_mode == "production"
            else None,
            trusted_selector_sha256=selector_sha256
            if manifest.capture_mode == "production"
            else None,
            trusted_plugin_sha256=plugin_sha256,
            capture_python_sha256=capture_python_digest,
        )
    if state.capture_python_sha256 != capture_python_digest:
        raise SystemExit("state capture Python identity mismatch")
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
    raw_expected_files_obj = population.get("test_files")
    raw_expected_nodes_obj = population.get("node_ids")
    raw_expected_files = (
        cast(list[object], raw_expected_files_obj)
        if isinstance(raw_expected_files_obj, list)
        else None
    )
    raw_expected_nodes = (
        cast(list[object], raw_expected_nodes_obj)
        if isinstance(raw_expected_nodes_obj, list)
        else None
    )
    if raw_expected_files is None or not all(
        isinstance(value, str) for value in raw_expected_files
    ):
        raise SystemExit("trusted expected test-file population is invalid")
    if raw_expected_nodes is None or not all(
        isinstance(value, str) for value in raw_expected_nodes
    ):
        raise SystemExit("trusted expected node population is invalid")
    expected_files: tuple[str, ...] = tuple(cast(list[str], raw_expected_files))
    expected_nodes: tuple[str, ...] = tuple(cast(list[str], raw_expected_nodes))
    _validate_resume(
        state,
        receipt_dir,
        manifest,
        population_sha256=population_sha256,
        expected_test_files=expected_files,
        expected_node_ids=expected_nodes,
    )
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
                    command = capture_command(
                        manifest.capture_mode,
                        worktree,
                        destination,
                        manifest,
                        repeat,
                        receipt_dir / "fragments" / key,
                        args.capture_python,
                        args.sqlite_preload,
                        args.sqlite_preload_sha256,
                    )
                    if manifest.capture_mode == "production":
                        if plugin_sha256 is None:
                            raise SystemExit("trusted pytest plugin identity is unavailable")
                        command.extend(
                            [
                                "--expected-population",
                                str(population_path),
                                "--expected-population-sha256",
                                population_sha256,
                                "--trusted-harness-sha256",
                                harness_sha256,
                                "--trusted-selector-sha256",
                                selector_sha256,
                                "--trusted-plugin-sha256",
                                plugin_sha256,
                                "--capture-python-sha256",
                                capture_python_digest,
                            ]
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


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if "--help" in arguments or "-h" in arguments:
        # Let argparse render help before the state-lock bootstrap enforces
        # the normal execution-time --state requirement.
        _main_unlocked(arguments)
        return 0
    bootstrap = argparse.ArgumentParser(add_help=False)
    bootstrap.add_argument("--state", type=Path)
    args, _ = bootstrap.parse_known_args(arguments)
    if args.state is None:
        bootstrap.error("the following arguments are required: --state")
    with collection_state_lock(args.state):
        return _main_unlocked(arguments)


if __name__ == "__main__":
    raise SystemExit(main())
