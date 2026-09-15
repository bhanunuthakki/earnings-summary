"""Run one bounded paired experiment over two immutable Git revisions.

This protocol can establish whether causal performance evidence is collectable.
It deliberately cannot grant performance admission.
"""

from __future__ import annotations

import hashlib
import io
import json
import math
import os
import platform
import random
import selectors
import signal
import statistics
import subprocess
import sys
import tarfile
import tempfile
import time
from collections.abc import Mapping, Sequence
from contextlib import suppress
from pathlib import Path, PurePosixPath
from typing import Any, Literal, cast

from pydantic import ValidationError

from quality.git_env import clean_local_git_env
from quality.performance import benchmark_environment
from quality.performance_experiment_models import (
    ArmSample,
    ArmStats,
    CompanionEnvelope,
    ExperimentDeclaration,
    IsolationProof,
    PairedStats,
    PerformanceExperimentReceipt,
    RunnerIdentity,
    SourceArmIdentity,
)

ADMISSION_HOLD_REASON = "performance admission remains deferred (BHA-122 HOLD)"
BOOTSTRAP_REPLICATES = 2_000
MAX_COMPANION_OUTPUT_BYTES = 64 * 1024
_SHA256_LENGTH = 64


class PerformanceExperimentError(Exception):
    """The paired protocol could not produce complete trustworthy evidence."""


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _safe_relative_path(value: str, *, kind: str) -> str:
    path = PurePosixPath(value)
    if not value or value.startswith("/") or "\\" in value or ".." in path.parts:
        raise PerformanceExperimentError(f"{kind} path is unsafe")
    if str(path) != value or value == ".":
        raise PerformanceExperimentError(f"{kind} path is invalid")
    return value


def run_git(
    args: Sequence[str], *, cwd: Path, env: Mapping[str, str]
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        list(args), cwd=cwd, env=dict(env), capture_output=True, check=False, timeout=30
    )


def run_experiment_subprocess(
    argv: Sequence[str], *, cwd: Path, env: Mapping[str, str], timeout: float
) -> subprocess.CompletedProcess[bytes]:
    """Run one POSIX process group with bounded output and deadline ownership."""
    if os.name != "posix":
        raise PerformanceExperimentError(
            "paired experiment process-tree isolation is unsupported on this platform"
        )
    process = subprocess.Popen(
        list(argv),
        cwd=cwd,
        env=dict(env),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    assert process.stdout is not None
    assert process.stderr is not None
    streams = selectors.DefaultSelector()
    streams.register(process.stdout, selectors.EVENT_READ, "stdout")
    streams.register(process.stderr, selectors.EVENT_READ, "stderr")
    chunks: dict[str, list[bytes]] = {"stdout": [], "stderr": []}
    captured_bytes = 0
    deadline = time.monotonic() + timeout

    def process_group_alive() -> bool:
        try:
            os.killpg(process.pid, 0)
        except ProcessLookupError:
            return False
        except OSError as exc:
            raise PerformanceExperimentError(
                "unable to verify workload process-group cleanup"
            ) from exc
        return True

    def terminate_group() -> None:
        with suppress(OSError):
            os.killpg(process.pid, signal.SIGTERM)
        termination_deadline = time.monotonic() + 0.5
        while time.monotonic() < termination_deadline:
            try:
                os.killpg(process.pid, 0)
            except OSError:
                break
            time.sleep(0.01)
        else:
            with suppress(OSError):
                os.killpg(process.pid, signal.SIGKILL)
        with suppress(subprocess.TimeoutExpired):
            process.wait(timeout=1)

    try:
        while streams.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                terminate_group()
                raise PerformanceExperimentError("workload timed out")
            for key, _events in streams.select(timeout=min(remaining, 0.05)):
                data = os.read(key.fd, 8192)
                if not data:
                    streams.unregister(key.fileobj)
                    continue
                captured_bytes += len(data)
                if captured_bytes > MAX_COMPANION_OUTPUT_BYTES:
                    terminate_group()
                    raise PerformanceExperimentError("workload output exceeded limit")
                chunks[str(key.data)].append(data)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            terminate_group()
            raise PerformanceExperimentError("workload timed out")
        returncode = process.wait(timeout=remaining)
        if process_group_alive():
            terminate_group()
            raise PerformanceExperimentError("workload left descendant processes")
    except BaseException:
        terminate_group()
        raise
    finally:
        streams.close()
        process.stdout.close()
        process.stderr.close()
    return subprocess.CompletedProcess(
        list(argv),
        returncode,
        stdout=b"".join(chunks["stdout"]),
        stderr=b"".join(chunks["stderr"]),
    )


def _git_bytes(root: Path, *args: str) -> bytes:
    try:
        result = run_git(["git", "-C", str(root), *args], cwd=root, env=clean_local_git_env())
    except (OSError, subprocess.SubprocessError) as exc:
        raise PerformanceExperimentError("Git identity lookup failed") from exc
    if result.returncode != 0:
        raise PerformanceExperimentError("Git identity lookup failed")
    return result.stdout


def _commit(root: Path, revision: str) -> str:
    if not revision or revision.startswith("-"):
        raise PerformanceExperimentError("revision is invalid")
    try:
        value = (
            _git_bytes(root, "rev-parse", "--verify", f"{revision}^{{commit}}")
            .decode("ascii")
            .strip()
        )
    except UnicodeDecodeError as exc:
        raise PerformanceExperimentError("revision identity is malformed") from exc
    if len(value) != 40 or any(char not in "0123456789abcdef" for char in value.lower()):
        raise PerformanceExperimentError("revision identity is malformed")
    return value.lower()


def _blob(root: Path, revision: str, relative: str) -> bytes:
    _safe_relative_path(relative, kind="declared")
    return _git_bytes(root, "show", f"{revision}:{relative}")


def _tree_hash(root: Path, revision: str) -> str:
    raw = _git_bytes(root, "ls-tree", "-r", "-z", revision)
    if not raw or not raw.endswith(b"\0"):
        raise PerformanceExperimentError("revision tree is unavailable")
    return _sha256(raw)


def _runner_identity(runner_id: str) -> RunnerIdentity:
    runner = Path(sys.executable)
    try:
        resolved = runner.resolve(strict=True)
        if not resolved.is_file() or not os.access(resolved, os.X_OK):
            raise OSError
        digest = _sha256(resolved.read_bytes())
    except OSError as exc:
        raise PerformanceExperimentError("declared runner is unavailable") from exc
    protocol_entries: list[bytes] = []
    for path in (Path(__file__), Path(__file__).with_name("performance_experiment_models.py")):
        protocol_entries.append(path.name.encode() + b"\0" + _sha256(path.read_bytes()).encode())
    return RunnerIdentity(
        runner_id=runner_id,
        executable=str(runner),
        resolved_executable=str(resolved),
        executable_sha256=digest,
        protocol_sha256=_sha256(b"\n".join(protocol_entries)),
        python_version=platform.python_version(),
        platform=platform.platform(aliased=True),
    )


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise PerformanceExperimentError("JSON object contains duplicate keys")
        value[key] = item
    return value


def _strict_json(raw: bytes, *, kind: str) -> object:
    try:
        return json.loads(
            raw,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=lambda _value: (_ for _ in ()).throw(
                PerformanceExperimentError(f"{kind} contains a non-finite number")
            ),
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PerformanceExperimentError(f"{kind} JSON is invalid") from exc


def _load_declaration(raw: bytes) -> ExperimentDeclaration:
    payload = _strict_json(raw, kind="declaration")
    if not isinstance(payload, dict):
        raise PerformanceExperimentError("experiment declaration is invalid")
    normalized = cast(dict[str, object], payload).copy()
    workload_argv = normalized.get("workload_argv")
    required_companions = normalized.get("required_companions")
    if not isinstance(workload_argv, list) or not isinstance(required_companions, list):
        raise PerformanceExperimentError("experiment declaration is invalid")
    raw_argv = cast(list[object], workload_argv)
    raw_companions = cast(list[object], required_companions)
    if not all(isinstance(value, str) for value in (*raw_argv, *raw_companions)):
        raise PerformanceExperimentError("experiment declaration is invalid")
    normalized["workload_argv"] = tuple(cast(list[str], raw_argv))
    normalized["required_companions"] = tuple(cast(list[str], raw_companions))
    try:
        value = ExperimentDeclaration.model_validate(normalized, strict=True)
    except ValidationError as exc:
        raise PerformanceExperimentError("experiment declaration is invalid") from exc
    if (
        not value.experiment_id.strip()
        or not value.workload_id.strip()
        or not value.runner_id.strip()
    ):
        raise PerformanceExperimentError("experiment declaration identity is empty")
    if not value.workload_argv or any(not part for part in value.workload_argv):
        raise PerformanceExperimentError("workload argv is empty")
    required = {
        "coverage_sha256",
        "fixture_sha256",
        "peak_rss_bytes",
        "result_sha256",
        "rows",
        "sql_statements",
        "workload_id",
    }
    if set(value.required_companions) != required or len(value.required_companions) != len(
        required
    ):
        raise PerformanceExperimentError("companion coverage declaration is incomplete")
    _safe_relative_path(value.fixture_path, kind="fixture")
    _safe_relative_path(value.workload_entrypoint, kind="workload")
    if value.workload_argv[:2] != ("{runner}", value.workload_entrypoint):
        raise PerformanceExperimentError(
            "workload argv must invoke the declared entrypoint with the sealed runner"
        )
    return value


def _extract_archive(root: Path, revision: str, destination: Path) -> None:
    if not callable(getattr(tarfile, "data_filter", None)):
        raise PerformanceExperimentError(
            "safe tar extraction requires Python with tarfile.data_filter support"
        )
    raw = _git_bytes(root, "archive", "--format=tar", revision)
    try:
        with tarfile.open(fileobj=io.BytesIO(raw), mode="r:") as archive:
            destination_root = destination.resolve()
            for member in archive.getmembers():
                target = (destination / member.name).resolve()
                if target != destination_root and destination_root not in target.parents:
                    raise PerformanceExperimentError("Git archive path escaped snapshot")
            archive.extractall(destination, filter="data")
    except (OSError, tarfile.TarError) as exc:
        raise PerformanceExperimentError("revision archive is invalid") from exc


def _snapshot_hash(root: Path) -> str:
    """Hash the extracted bytes and link targets before and after execution."""
    entries: list[bytes] = []
    try:
        for path in sorted(root.rglob("*")):
            relative = path.relative_to(root).as_posix().encode()
            if path.is_symlink():
                payload = b"link\0" + os.readlink(path).encode()
            elif path.is_file():
                payload = b"file\0" + path.read_bytes()
            elif path.is_dir():
                payload = b"directory\0"
            else:
                raise PerformanceExperimentError("snapshot contains unsupported file type")
            entries.append(relative + b"\0" + _sha256(payload).encode() + b"\n")
    except OSError as exc:
        raise PerformanceExperimentError("snapshot identity is unavailable") from exc
    return _sha256(b"".join(entries))


def _render_argv(
    declaration: ExperimentDeclaration, runner: RunnerIdentity, snapshot: Path
) -> list[str]:
    replacements = {"{runner}": runner.executable, "{snapshot}": str(snapshot)}
    return [replacements.get(part, part) for part in declaration.workload_argv]


def _validate_digest(value: str, *, kind: str) -> None:
    if len(value) != _SHA256_LENGTH or any(char not in "0123456789abcdef" for char in value):
        raise PerformanceExperimentError(f"{kind} digest is invalid")


def _run_sample(
    *,
    arm: Literal["control", "treatment"],
    ordinal: int,
    order: Literal[1, 2],
    snapshot: Path,
    output_dir: Path,
    declaration: ExperimentDeclaration,
    runner: RunnerIdentity,
    revision: str,
    fixture_sha256: str,
    environment: Mapping[str, str],
) -> ArmSample:
    output_dir.mkdir(parents=True, exist_ok=False)
    env = dict(environment)
    env["PERFORMANCE_EXPERIMENT_OUTPUT_DIR"] = str(output_dir)
    env["PERFORMANCE_EXPERIMENT_REVISION"] = revision
    env["PERFORMANCE_EXPERIMENT_FIXTURE_SHA256"] = fixture_sha256
    env["PERFORMANCE_EXPERIMENT_WORKLOAD_ID"] = declaration.workload_id
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    started = time.perf_counter()
    try:
        completed = run_experiment_subprocess(
            _render_argv(declaration, runner, snapshot),
            cwd=snapshot,
            env=env,
            timeout=declaration.timeout_seconds,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise PerformanceExperimentError("workload execution failed") from exc
    elapsed = time.perf_counter() - started
    if completed.returncode != 0:
        raise PerformanceExperimentError("workload exited nonzero")
    if completed.stderr:
        raise PerformanceExperimentError("workload emitted unexpected stderr")
    try:
        companion = CompanionEnvelope.model_validate(
            _strict_json(completed.stdout, kind="companion"), strict=True
        )
    except ValidationError as exc:
        raise PerformanceExperimentError("workload companion is invalid") from exc
    if companion.workload_id != declaration.workload_id:
        raise PerformanceExperimentError("workload companion identity mismatches declaration")
    if companion.revision != revision:
        raise PerformanceExperimentError("workload companion revision mismatches executed arm")
    if companion.fixture_sha256 != fixture_sha256:
        raise PerformanceExperimentError("workload companion fixture mismatches executed arm")
    for kind, digest in (
        ("coverage", companion.coverage_sha256),
        ("fixture", companion.fixture_sha256),
        ("result", companion.result_sha256),
    ):
        _validate_digest(digest, kind=kind)
    if not math.isfinite(elapsed) or not elapsed > 0:
        raise PerformanceExperimentError("elapsed timing is invalid")
    return ArmSample(
        arm=arm,
        pair_ordinal=ordinal,
        order=order,
        elapsed_seconds=elapsed,
        companion=companion,
        companion_trust="self_reported_unverified",
        variable_measurement_policy=("sql_statements_and_peak_rss_are_per_sample_unverified"),
    )


def _arm_stats(values: Sequence[float]) -> ArmStats:
    if not values:
        return ArmStats(count=0, median_seconds=None, mad_seconds=None)
    median = float(statistics.median(values))
    return ArmStats(
        count=len(values),
        median_seconds=median,
        mad_seconds=float(statistics.median(abs(value - median) for value in values)),
    )


def _paired_stats(control: Sequence[float], treatment: Sequence[float]) -> PairedStats:
    if len(control) != len(treatment) or len(control) < 7:
        return PairedStats(
            count=min(len(control), len(treatment)),
            median_delta_seconds=None,
            mad_delta_seconds=None,
            bootstrap_ci_95_delta_seconds=None,
        )
    deltas = [after - before for before, after in zip(control, treatment, strict=True)]
    median = float(statistics.median(deltas))
    mad = float(statistics.median(abs(value - median) for value in deltas))
    rng = random.Random(0)
    estimates = sorted(
        float(statistics.median(rng.choices(deltas, k=len(deltas))))
        for _ in range(BOOTSTRAP_REPLICATES)
    )
    return PairedStats(
        count=len(deltas),
        median_delta_seconds=median,
        mad_delta_seconds=mad,
        bootstrap_ci_95_delta_seconds=(estimates[50], estimates[1949]),
    )


def capture_performance_experiment(
    repo_root: str | Path,
    *,
    declaration_path: str,
    control_revision: str,
    treatment_revision: str,
) -> PerformanceExperimentReceipt:
    """Run a sealed paired protocol; a complete receipt still remains HOLD."""
    try:
        root = Path(repo_root).resolve(strict=True)
    except OSError as exc:
        raise PerformanceExperimentError("repository root is unavailable") from exc
    declaration_name = _safe_relative_path(declaration_path, kind="declaration")
    control_commit = _commit(root, control_revision)
    treatment_commit = _commit(root, treatment_revision)
    if control_commit == treatment_commit:
        raise PerformanceExperimentError("control and treatment revisions must be distinct")
    control_declaration = _blob(root, control_commit, declaration_name)
    treatment_declaration = _blob(root, treatment_commit, declaration_name)
    if control_declaration != treatment_declaration:
        raise PerformanceExperimentError("experiment declaration drifted between revisions")
    declaration = _load_declaration(control_declaration)
    runner_identity = _runner_identity(declaration.runner_id)
    declaration_sha256 = _sha256(control_declaration)
    control_fixture = _blob(root, control_commit, declaration.fixture_path)
    treatment_fixture = _blob(root, treatment_commit, declaration.fixture_path)
    if control_fixture != treatment_fixture:
        raise PerformanceExperimentError("fixture drifted between revisions")
    fixture_sha256 = _sha256(control_fixture)
    control_workload_sha256 = _sha256(_blob(root, control_commit, declaration.workload_entrypoint))
    treatment_workload_sha256 = _sha256(
        _blob(root, treatment_commit, declaration.workload_entrypoint)
    )
    if control_workload_sha256 != treatment_workload_sha256:
        raise PerformanceExperimentError("workload adapter drifted between revisions")
    environment = benchmark_environment(clean_local_git_env())
    credential_removed = all(
        marker not in key.upper()
        for key in environment
        for marker in ("API_KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL", "COOKIE")
    )
    git_removed = all(not key.upper().startswith("GIT_") for key in environment)

    with tempfile.TemporaryDirectory(prefix="paired-performance-") as temp_name:
        temp_root = Path(temp_name)
        snapshots = {
            "control": temp_root / "control" / "source",
            "treatment": temp_root / "treatment" / "source",
        }
        outputs = {
            "control": temp_root / "control" / "outputs",
            "treatment": temp_root / "treatment" / "outputs",
        }
        for snapshot in snapshots.values():
            snapshot.mkdir(parents=True)
        _extract_archive(root, control_commit, snapshots["control"])
        _extract_archive(root, treatment_commit, snapshots["treatment"])
        snapshot_hashes_before = {
            arm: _snapshot_hash(snapshot) for arm, snapshot in snapshots.items()
        }

        identities = {
            "control": SourceArmIdentity(
                arm="control",
                revision=control_commit,
                tree_sha256=_tree_hash(root, control_commit),
                declaration_sha256=declaration_sha256,
                fixture_sha256=fixture_sha256,
                workload_sha256=control_workload_sha256,
                snapshot_path_sha256=_sha256(str(snapshots["control"]).encode()),
            ),
            "treatment": SourceArmIdentity(
                arm="treatment",
                revision=treatment_commit,
                tree_sha256=_tree_hash(root, treatment_commit),
                declaration_sha256=declaration_sha256,
                fixture_sha256=fixture_sha256,
                workload_sha256=treatment_workload_sha256,
                snapshot_path_sha256=_sha256(str(snapshots["treatment"]).encode()),
            ),
        }

        def collect(
            arm: Literal["control", "treatment"],
            ordinal: int,
            order: Literal[1, 2],
            phase: str,
        ) -> ArmSample:
            identity = identities[arm]
            return _run_sample(
                arm=arm,
                ordinal=ordinal,
                order=order,
                snapshot=snapshots[arm],
                output_dir=outputs[arm] / f"{phase}-{ordinal}",
                declaration=declaration,
                runner=runner_identity,
                revision=identity.revision,
                fixture_sha256=identity.fixture_sha256,
                environment=environment,
            )

        warmups = (collect("control", 1, 1, "warmup"), collect("treatment", 1, 2, "warmup"))
        measured: list[ArmSample] = []
        for ordinal in range(1, declaration.repeats + 1):
            order: tuple[Literal["control", "treatment"], Literal["control", "treatment"]] = (
                ("control", "treatment") if ordinal % 2 else ("treatment", "control")
            )
            measured.append(collect(order[0], ordinal, 1, "measured"))
            measured.append(collect(order[1], ordinal, 2, "measured"))

        by_arm = {
            arm: sorted(
                (sample for sample in measured if sample.arm == arm),
                key=lambda sample: sample.pair_ordinal,
            )
            for arm in ("control", "treatment")
        }
        all_samples = (*warmups, *measured)
        expected = all_samples[0].companion
        for sample in all_samples[1:]:
            companion = sample.companion
            if (
                companion.workload_id != expected.workload_id
                or companion.fixture_sha256 != expected.fixture_sha256
                or companion.coverage_sha256 != expected.coverage_sha256
                or companion.result_sha256 != expected.result_sha256
                or companion.rows != expected.rows
            ):
                raise PerformanceExperimentError(
                    "fixed workload companion identity differs across the series"
                )
        control_values = [sample.elapsed_seconds for sample in by_arm["control"]]
        treatment_values = [sample.elapsed_seconds for sample in by_arm["treatment"]]
        isolation = IsolationProof(
            separate_snapshot_roots=snapshots["control"] != snapshots["treatment"],
            immutable_archives=True,
            separate_output_directories=outputs["control"] != outputs["treatment"],
            source_trees_unchanged=all(
                _snapshot_hash(snapshot) == snapshot_hashes_before[arm]
                for arm, snapshot in snapshots.items()
            ),
            credential_environment_removed=credential_removed,
            inherited_git_environment_removed=git_removed,
            process_isolation="unavailable",
            network_isolation="unavailable",
        )
        if not all(
            (
                isolation.separate_snapshot_roots,
                isolation.immutable_archives,
                isolation.separate_output_directories,
                isolation.source_trees_unchanged,
                isolation.credential_environment_removed,
                isolation.inherited_git_environment_removed,
            )
        ):
            raise PerformanceExperimentError("isolation proof is incomplete")
        return PerformanceExperimentReceipt(
            schema_version="performance-experiment-receipt/v1",
            declaration=declaration,
            declaration_sha256=declaration_sha256,
            runner=runner_identity,
            control=identities["control"],
            treatment=identities["treatment"],
            warmups=warmups,
            measured_samples=tuple(measured),
            control_stats=_arm_stats(control_values),
            treatment_stats=_arm_stats(treatment_values),
            paired_stats=_paired_stats(control_values, treatment_values),
            isolation=isolation,
            collection_status="COMPLETE",
            causal_feasibility_status="HOLD",
            admission_status="HOLD",
            hold=True,
            hold_reasons=(
                ADMISSION_HOLD_REASON,
                "PF1 establishes paired latency collection only",
                "companion measures are self-reported and unverified",
                "independent process and network isolation proof is unavailable",
            ),
        )


__all__ = [
    "ADMISSION_HOLD_REASON",
    "PerformanceExperimentError",
    "capture_performance_experiment",
    "run_experiment_subprocess",
    "run_git",
]
