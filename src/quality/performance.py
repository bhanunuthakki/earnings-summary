"""Deterministic, local-only performance baseline receipts."""

from __future__ import annotations

import hashlib
import os
import platform
import random
import shlex
import statistics
import subprocess
import sys
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, ValidationError

ReceiptStatus = Literal["PASS", "HOLD", "FAIL"]
CohortName = Literal["integrity", "migrations", "route_cold_warm", "dcf", "source_analysis", "ci"]


class TimingStats(BaseModel):
    model_config = ConfigDict(extra="forbid")
    samples: list[float]
    count: int
    minimum_seconds: float | None
    median_seconds: float | None
    mean_seconds: float | None
    maximum_seconds: float | None
    stdev_seconds: float | None


class TimingSample(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label: Literal["cold", "warm"]
    elapsed_seconds: float


class CompanionMeasures(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sql_statements: int | None
    rows: int | None
    elapsed_seconds: float | None
    peak_rss_bytes: int | None


class CausalRunEnvelope(BaseModel):
    """Required per-invocation envelope emitted by a benchmark command."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    sql_statements: int
    rows: int
    elapsed_seconds: float
    peak_rss_bytes: int
    alembic_revision: str | None
    query_plan_sha256: str | None
    connection_role: Literal["read", "write", "request_scoped_read", "none"]
    stage: str
    revision: str


class PerformanceReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: str = "performance-baseline/v1"
    benchmark_command: str
    command_argv: list[str]
    revision: str | None
    source_sha256: str | None
    config_sha256: str | None
    timing: TimingStats
    environment: dict[str, str]
    status: ReceiptStatus
    hold: bool
    hold_reasons: list[str]
    exit_codes: list[int]
    output_sha256: str | None
    output_bytes: int
    output: str
    warmup_seconds: float | None
    timing_samples: list[TimingSample]
    median_seconds: float | None
    mad_seconds: float | None
    bootstrap_ci_95: tuple[float, float] | None
    stability_verdict: Literal["stable", "unstable", "insufficient"]
    adaptive_verdict: Literal["eligible", "hold", "failed"]
    companion_measures: CompanionMeasures
    provenance: Literal["mac_guidance", "approved_windows_production_shaped"]
    causal_runs: list[CausalRunEnvelope] = []


class CausalEvidence(BaseModel):
    """Typed companions that explain what a timing sample exercised."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    sql_statements: int | None
    rows: int | None
    elapsed_seconds: float | None
    peak_rss_bytes: int | None
    alembic_revision: str | None
    query_plan_sha256: str | None
    connection_role: Literal["read", "write", "request_scoped_read", "none"]
    stage: str | None


class FrozenPerformanceCohort(BaseModel):
    """Versioned declaration of a Train-0 benchmark cohort."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    cohort: CohortName
    config_version: str = "train0/v1"
    declared_command: str
    route_count: int = 0
    route_names: tuple[str, ...] = ()


_ROUTE_NAMES: tuple[str, ...] = (
    "/healthz",
    "/api/capture/text",
    "/api/onmymind/<int:note_id>/reply",
    "/api/onmymind/<int:note_id>/answer",
    "/api/research/task/<int:task_id>/run",
    "/api/research/task/<int:task_id>/status",
    "/api/research/task/<int:task_id>/reject",
    "/api/research/proposal/<int:proposal_id>/<verb>",
    "/api/research/proposals/<int:proposal_id>",
    "/api/reconcile/<kind>/<int:item_id>/<verdict>",
    "/api/reconcile/falsifier/<int:decision_id>",
    "/api/onmymind/<int:note_id>/<verb>",
    "/api/tenets",
    "/api/tenets/<int:tenet_id>/<action>",
    "/api/profile/fact/<int:fact_id>/affirm",
    "/api/profile/fact/<int:fact_id>/reject",
    "/api/profile/fact/<int:fact_id>/reaffirm",
    "/api/profile/fact/<int:fact_id>/retire",
    "/api/profile/fact/<int:fact_id>/update",
    "/api/tenets/distill",
)


_COHORT_COMMANDS: dict[CohortName, str] = {
    "integrity": f"{sys.executable} execution/verify_reconstruction_inventory.py --json",
    "migrations": f"{sys.executable} execution/validate_directive_manifest.py",
    "route_cold_warm": f"{sys.executable} execution/comments_server.py --help",
    "dcf": f"{sys.executable} execution/build_redesigned_dcf.py --help",
    "source_analysis": f"{sys.executable} execution/analyze_code_duplicates.py --help",
    "ci": f"{sys.executable} execution/format_changed.py --help",
}


COHORT_REGISTRY: dict[CohortName, FrozenPerformanceCohort] = {
    name: FrozenPerformanceCohort(
        cohort=name,
        declared_command=_COHORT_COMMANDS[name],
        route_count=20 if name == "route_cold_warm" else 0,
        route_names=_ROUTE_NAMES if name == "route_cold_warm" else (),
    )
    for name in ("integrity", "migrations", "route_cold_warm", "dcf", "source_analysis", "ci")
}


class PerformanceEvidenceReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: str = "performance-evidence/v1"
    cohort: FrozenPerformanceCohort
    baseline: PerformanceReceipt
    causal_evidence: CausalEvidence
    causal_runs: tuple[CausalRunEnvelope, ...]
    baseline_revision: str | None
    current_revision: str | None
    paired_identity: bool


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _git(root: Path, *args: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True,
            text=True,
            check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() or None


def _tree_hash(root: Path, paths: list[str]) -> str | None:
    entries: list[bytes] = []
    for name in sorted(paths):
        path = root / name
        if not path.is_file():
            continue
        try:
            content = path.read_bytes()
        except OSError:
            continue
        entries.append(name.encode() + b"\0" + _sha256_bytes(content).encode() + b"\n")
    return _sha256_bytes(b"".join(entries)) if entries else None


def _source_hash(root: Path) -> str | None:
    listing = _git(root, "ls-files", "-z", "--", "src", "execution")
    if listing is None:
        return None
    names = listing.split("\0")
    return _tree_hash(root, [name for name in names if name.endswith(".py")])


def _environment() -> dict[str, str]:
    return {
        "python": platform.python_version(),
        "python_implementation": platform.python_implementation(),
        "platform": platform.platform(aliased=True),
        "machine": platform.machine(),
        "processor": platform.processor() or "unknown",
        "executable": os.path.realpath(sys.executable),
    }


def _timing(samples: list[float]) -> TimingStats:
    return TimingStats(
        samples=samples,
        count=len(samples),
        minimum_seconds=min(samples) if samples else None,
        median_seconds=statistics.median(samples) if samples else None,
        mean_seconds=statistics.fmean(samples) if samples else None,
        maximum_seconds=max(samples) if samples else None,
        stdev_seconds=statistics.stdev(samples) if len(samples) > 1 else None,
    )


def _int_measure(measures: Mapping[str, float | int | None] | None, key: str) -> int | None:
    value = measures.get(key) if measures else None
    if isinstance(value, (int, float)):
        return int(value)
    return None


def _bootstrap_median(samples: list[float]) -> tuple[float, float] | None:
    if len(samples) < 2:
        return None
    rng = random.Random(0)
    estimates = [statistics.median(rng.choices(samples, k=len(samples))) for _ in range(1000)]
    estimates.sort()
    return estimates[25], estimates[974]


def capture_performance_baseline(
    repo_root: str | Path,
    command: str,
    *,
    samples: int = 3,
    timeout_seconds: float = 120.0,
    config_paths: list[str] | None = None,
    companion_measures: Mapping[str, float | int | None] | None = None,
    provenance: Literal["mac_guidance", "approved_windows_production_shaped"] | None = None,
    require_causal_envelope: bool = False,
    require_companion_measures: bool = True,
) -> PerformanceReceipt:
    """Run a declared local command and return evidence, never contacting a network."""
    root = Path(repo_root).resolve()
    reasons: list[str] = []
    revision = _git(root, "rev-parse", "HEAD")
    if revision is None:
        reasons.append("revision unavailable")
    source_hash = _source_hash(root)
    if source_hash is None:
        reasons.append("tracked source hash unavailable")
    config_hash = _tree_hash(root, config_paths or ["pyproject.toml", "requirements.lock"])
    if config_hash is None:
        reasons.append("declared config files unavailable")
    try:
        argv = shlex.split(command)
    except ValueError as exc:
        argv = []
        reasons.append(f"invalid command: {exc}")
    if not argv:
        reasons.append("benchmark command is empty")
    if samples < 1:
        reasons.append("sample count must be positive")
    if samples < 7:
        reasons.append("at least 7 measured repeats are required")
    required_companions = {"sql_statements", "rows", "elapsed_seconds", "peak_rss_bytes"}
    if require_companion_measures and (
        not companion_measures or not required_companions <= companion_measures.keys()
    ):
        reasons.append("all companion measures are required")
    if provenance is None:
        reasons.append("evidence provenance is required")
    durations: list[float] = []
    exit_codes: list[int] = []
    output_parts: list[bytes] = []
    failed = False
    warmup_seconds: float | None = None
    timing_samples: list[TimingSample] = []
    causal_runs: list[CausalRunEnvelope] = []
    requested_runs = min(max(samples, 0), 21)
    for run_number in range((requested_runs + 1) if argv else 0):
        started = time.perf_counter()
        try:
            completed = subprocess.run(  # reachability: external-process
                argv,
                cwd=root,
                capture_output=True,
                timeout=timeout_seconds,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            reasons.append(f"benchmark execution failed: {type(exc).__name__}")
            failed = True
            break
        durations.append(time.perf_counter() - started)
        elapsed = durations[-1]
        if run_number == 0:
            warmup_seconds = elapsed
        else:
            timing_samples.append(
                TimingSample(label="cold" if run_number == 1 else "warm", elapsed_seconds=elapsed)
            )
        exit_codes.append(completed.returncode)
        output_parts.append(completed.stdout + completed.stderr)
        if require_causal_envelope:
            try:
                envelope = CausalRunEnvelope.model_validate_json(completed.stdout.strip())
                if envelope.elapsed_seconds <= 0 or envelope.peak_rss_bytes < 0:
                    raise ValueError("causal envelope contains non-positive elapsed evidence")
                causal_runs.append(envelope)
            except (ValidationError, UnicodeDecodeError, ValueError):
                reasons.append("missing or invalid per-run causal evidence envelope")
                break
        if completed.returncode != 0:
            failed = True
            reasons.append(f"benchmark exited {completed.returncode}")
            break
    # A noisy first batch gets bounded adaptive retries.  This only improves
    # precision; it never turns an under-sampled or failed run into PASS.
    while not failed and len(timing_samples) < 21:
        measured_now = [sample.elapsed_seconds for sample in timing_samples]
        if len(measured_now) < 7:
            break
        median_now = statistics.median(measured_now)
        mad_now = statistics.median(abs(value - median_now) for value in measured_now)
        if mad_now <= median_now * 0.05:
            break
        run_number = len(timing_samples) + 1
        started = time.perf_counter()
        try:
            completed = subprocess.run(  # reachability: external-process
                argv,
                cwd=root,
                capture_output=True,
                timeout=timeout_seconds,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            reasons.append(f"adaptive benchmark execution failed: {type(exc).__name__}")
            failed = True
            break
        elapsed = time.perf_counter() - started
        timing_samples.append(TimingSample(label="warm", elapsed_seconds=elapsed))
        exit_codes.append(completed.returncode)
        output_parts.append(completed.stdout + completed.stderr)
        if require_causal_envelope:
            try:
                envelope = CausalRunEnvelope.model_validate_json(completed.stdout.strip())
                if envelope.elapsed_seconds <= 0 or envelope.peak_rss_bytes < 0:
                    raise ValueError("causal envelope contains non-positive elapsed evidence")
                causal_runs.append(envelope)
            except (ValidationError, UnicodeDecodeError, ValueError):
                reasons.append("missing or invalid per-run causal evidence envelope")
                break
        if completed.returncode != 0:
            failed = True
            reasons.append(f"benchmark exited {completed.returncode}")
            break
    combined = b"".join(output_parts)
    measured = [sample.elapsed_seconds for sample in timing_samples]
    median = statistics.median(measured) if measured else None
    mad = (
        statistics.median([abs(value - median) for value in measured])
        if measured and median is not None
        else None
    )
    stability: Literal["stable", "unstable", "insufficient"] = "insufficient"
    if median is not None and mad is not None and len(measured) >= 7:
        stability = "stable" if mad <= median * 0.05 else "unstable"
    if stability == "unstable":
        reasons.append("timing samples are unstable; collect additional repeats")
    status: ReceiptStatus = "FAIL" if failed else ("HOLD" if reasons else "PASS")
    return PerformanceReceipt(
        benchmark_command=command,
        command_argv=argv,
        revision=revision,
        source_sha256=source_hash,
        config_sha256=config_hash,
        timing=_timing(measured),
        environment=_environment(),
        status=status,
        hold=bool(reasons),
        hold_reasons=reasons,
        exit_codes=exit_codes,
        output_sha256=_sha256_bytes(combined) if combined else None,
        output_bytes=len(combined),
        output=combined[:12000].decode("utf-8", errors="replace"),
        warmup_seconds=warmup_seconds,
        timing_samples=timing_samples,
        median_seconds=median,
        mad_seconds=mad,
        bootstrap_ci_95=_bootstrap_median(measured),
        stability_verdict=stability,
        adaptive_verdict="failed" if failed else ("hold" if reasons else "eligible"),
        companion_measures=CompanionMeasures(
            sql_statements=_int_measure(companion_measures, "sql_statements"),
            rows=_int_measure(companion_measures, "rows"),
            elapsed_seconds=companion_measures.get("elapsed_seconds")
            if companion_measures
            else None,
            peak_rss_bytes=_int_measure(companion_measures, "peak_rss_bytes"),
        ),
        provenance=provenance or "mac_guidance",
        causal_runs=causal_runs,
    )


def capture_performance_evidence(
    repo_root: str | Path,
    cohort: FrozenPerformanceCohort,
    *,
    evidence: CausalEvidence | None = None,
    samples: int = 7,
    timeout_seconds: float = 120.0,
    config_paths: list[str] | None = None,
    provenance: Literal["mac_guidance", "approved_windows_production_shaped"] | None = None,
    baseline_revision: str | None = None,
    current_revision: str | None = None,
) -> PerformanceEvidenceReceipt:
    """Capture one declared cohort; missing causal companions fail closed."""
    reasons: list[str] = []
    # This adapter can execute one checkout only.  Revision labels emitted by a
    # child process are claims, not proof that the same cohort ran at both
    # revisions.  Keep the receipt fail-closed until a revision-aware runner
    # (Windows harness or an equivalent immutable worktree runner) supplies
    # paired execution provenance.
    reasons.append("paired execution provenance requires a revision-aware harness")
    required: dict[CohortName, set[str]] = {
        "integrity": {"stage", "sql_statements", "rows"},
        "migrations": {"stage", "alembic_revision"},
        "route_cold_warm": {"stage", "sql_statements", "rows", "connection_role"},
        "dcf": {"stage", "elapsed_seconds", "peak_rss_bytes"},
        "source_analysis": {"stage", "elapsed_seconds", "peak_rss_bytes"},
        "ci": {"stage", "elapsed_seconds"},
    }
    if cohort.cohort == "route_cold_warm" and (
        cohort.route_count != 20 or len(cohort.route_names) != 20
    ):
        reasons.append("route_cold_warm requires exactly 20 declared routes")
    baseline = capture_performance_baseline(
        repo_root,
        cohort.declared_command,
        samples=samples,
        timeout_seconds=timeout_seconds,
        config_paths=config_paths,
        companion_measures=None,
        provenance=provenance,
        require_causal_envelope=True,
        require_companion_measures=False,
    )
    runs = tuple(baseline.causal_runs)
    requirement_values: dict[str, list[object]] = {
        "stage": [run.stage for run in runs],
        "sql_statements": [run.sql_statements for run in runs],
        "rows": [run.rows for run in runs],
        "elapsed_seconds": [run.elapsed_seconds for run in runs],
        "peak_rss_bytes": [run.peak_rss_bytes for run in runs],
        "alembic_revision": [run.alembic_revision for run in runs],
        "connection_role": [run.connection_role for run in runs],
    }
    for field in required[cohort.cohort]:
        if not runs or any(value in (None, "") for value in requirement_values[field]):
            reasons.append(f"{cohort.cohort} requires causal evidence: {field}")
    sql_values = [run.sql_statements for run in runs]
    row_values = [run.rows for run in runs]
    elapsed_values = [run.elapsed_seconds for run in runs]
    rss_values = [run.peak_rss_bytes for run in runs]
    aggregate = CausalEvidence(
        sql_statements=int(statistics.median(sql_values)) if runs else None,
        rows=int(statistics.median(row_values)) if runs else None,
        elapsed_seconds=statistics.median(elapsed_values) if runs else None,
        peak_rss_bytes=int(statistics.median(rss_values)) if runs else None,
        alembic_revision=runs[0].alembic_revision
        if runs and len({r.alembic_revision for r in runs}) == 1
        else None,
        query_plan_sha256=runs[0].query_plan_sha256
        if runs and len({r.query_plan_sha256 for r in runs}) == 1
        else None,
        connection_role=runs[0].connection_role
        if runs and len({r.connection_role for r in runs}) == 1
        else "none",
        stage=runs[0].stage if runs and len({r.stage for r in runs}) == 1 else None,
    )
    revisions = {run.revision for run in runs}
    if (
        baseline_revision is None
        or current_revision is None
        or revisions != {baseline_revision, current_revision}
    ):
        reasons.append("paired baseline/current revision identity is required")
    if reasons:
        baseline.hold_reasons.extend(reasons)
        baseline.hold = True
        if baseline.status == "PASS":
            baseline.status = "HOLD"
        baseline.adaptive_verdict = "hold"
    if provenance == "approved_windows_production_shaped" and sys.platform != "win32":
        baseline.hold_reasons.append(
            "approved Windows production-shaped evidence requires Windows host"
        )
        baseline.hold = True
        if baseline.status == "PASS":
            baseline.status = "HOLD"
        baseline.adaptive_verdict = "hold"
    return PerformanceEvidenceReceipt(
        cohort=cohort,
        baseline=baseline,
        causal_evidence=aggregate,
        causal_runs=runs,
        baseline_revision=baseline_revision,
        current_revision=current_revision,
        paired_identity=bool(
            baseline_revision
            and current_revision
            and revisions == {baseline_revision, current_revision}
        ),
    )
