"""Deterministic, local-only performance baseline receipt facade."""

from __future__ import annotations

import shlex
import statistics
import subprocess
import sys
import time
from pathlib import Path
from typing import Literal

from pydantic import ValidationError

from .performance_models import (
    EXTERNAL_TRAP_BOUNDARIES,
    EXTERNAL_TRAP_PROOF_VERSION,
    CausalEvidence,
    CausalRunEnvelope,
    CohortName,
    CompanionMeasures,
    FrozenPerformanceCohort,
    PerformanceEvidenceReceipt,
    PerformanceReceipt,
    ReceiptStatus,
    RouteCausalCompanion,
    SourceAnalysisSummary,
    TimingSample,
    TimingStats,
    external_trap_proof_sha256,
)
from .performance_source_analysis import paired_source_analysis
from .performance_support import (
    _bootstrap_median,
    _environment,
    _git,
    _managed_command,
    _sha256_bytes,
    _source_hash,
    _timing,
    _tree_hash,
    held_evidence_receipt,
)

__all__ = [
    "COHORT_REGISTRY",
    "EXTERNAL_TRAP_BOUNDARIES",
    "EXTERNAL_TRAP_PROOF_VERSION",
    "CausalEvidence",
    "CausalRunEnvelope",
    "CohortName",
    "CompanionMeasures",
    "FrozenPerformanceCohort",
    "PerformanceEvidenceReceipt",
    "PerformanceReceipt",
    "ReceiptStatus",
    "RouteCausalCompanion",
    "SourceAnalysisSummary",
    "TimingSample",
    "TimingStats",
    "capture_performance_baseline",
    "capture_performance_evidence",
    "external_trap_proof_sha256",
]

# Keep these facade aliases stable: callers and tests use them as seams for
# the public baseline adapter and source-analysis subprocess boundary.
_paired_source_analysis = paired_source_analysis
_held_evidence_receipt = held_evidence_receipt

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
    "integrity": "python execution/benchmark_performance_workload.py --workload integrity",
    "migrations": "python execution/benchmark_performance_workload.py --workload migrations",
    "route_cold_warm": "python execution/benchmark_performance_workload.py --workload routes",
    "dcf": "python execution/benchmark_dcf_workload.py",
    "source_analysis": (
        "python execution/analyze_code_duplicates.py "
        "--repo-root {repo_root} --revision WORKTREE --out {output}"
    ),
    "ci": "python execution/collect_paired_ci_performance.py --manifest <immutable-ci-manifest> --state .tmp/<attempt>/state.json --repo-root <revision-repository>",
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


def capture_performance_baseline(
    repo_root: str | Path,
    command: str,
    *,
    samples: int = 3,
    timeout_seconds: float = 120.0,
    config_paths: list[str] | None = None,
    provenance: Literal["mac_guidance", "approved_windows_production_shaped"] | None = None,
) -> PerformanceReceipt:
    """Capture raw timing only; caller-supplied metrics cannot make it pass."""
    return _capture_performance_baseline(
        repo_root,
        command,
        samples=samples,
        timeout_seconds=timeout_seconds,
        config_paths=config_paths,
        provenance=provenance,
        require_causal_envelope=False,
    )


def _capture_performance_baseline(
    repo_root: str | Path,
    command: str,
    *,
    samples: int = 3,
    timeout_seconds: float = 120.0,
    config_paths: list[str] | None = None,
    provenance: Literal["mac_guidance", "approved_windows_production_shaped"] | None = None,
    require_causal_envelope: bool = False,
) -> PerformanceReceipt:
    """Run a declared workload, optionally requiring its validated envelope."""
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
    argv = _managed_command(root, argv)
    if samples < 1:
        reasons.append("sample count must be positive")
    if samples < 7:
        reasons.append("at least 7 measured repeats are required")
    if not require_causal_envelope:
        reasons.append("validated causal evidence envelope is required for PASS")
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
            completed = subprocess.run(
                argv, cwd=root, capture_output=True, timeout=timeout_seconds, check=False
            )
        except (OSError, subprocess.SubprocessError) as exc:
            reasons.append(f"benchmark execution failed: {type(exc).__name__}")
            failed = True
            break
        durations.append(time.perf_counter() - started)
        if run_number == 0:
            warmup_seconds = durations[-1]
        else:
            timing_samples.append(
                TimingSample(
                    label="cold" if run_number == 1 else "warm",
                    elapsed_seconds=durations[-1],
                )
            )
        exit_codes.append(completed.returncode)
        output_parts.append(completed.stdout + completed.stderr)
        if require_causal_envelope:
            try:
                envelope = CausalRunEnvelope.model_validate_json(completed.stdout.strip())
                causal_runs.append(envelope)
            except (ValidationError, UnicodeDecodeError, ValueError):
                reasons.append("missing or invalid per-run causal evidence envelope")
                break
        if completed.returncode != 0:
            failed = True
            reasons.append(f"benchmark exited {completed.returncode}")
            break
    while not failed and len(timing_samples) < 21:
        measured_now = [sample.elapsed_seconds for sample in timing_samples]
        if len(measured_now) < 7:
            break
        median_now = statistics.median(measured_now)
        mad_now = statistics.median(abs(value - median_now) for value in measured_now)
        if mad_now <= median_now * 0.05:
            break
        started = time.perf_counter()
        try:
            completed = subprocess.run(
                argv, cwd=root, capture_output=True, timeout=timeout_seconds, check=False
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
                causal_runs.append(CausalRunEnvelope.model_validate_json(completed.stdout.strip()))
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
        statistics.median(abs(value - median) for value in measured)
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
            sql_statements=None,
            rows=None,
            elapsed_seconds=None,
            peak_rss_bytes=None,
        ),
        provenance=provenance or "mac_guidance",
        causal_runs=causal_runs,
    )


def capture_performance_evidence(
    repo_root: str | Path,
    cohort: FrozenPerformanceCohort,
    *,
    samples: int = 7,
    timeout_seconds: float = 120.0,
    config_paths: list[str] | None = None,
    provenance: Literal["mac_guidance", "approved_windows_production_shaped"] | None = None,
    baseline_revision: str | None = None,
    current_revision: str | None = None,
) -> PerformanceEvidenceReceipt:
    """Capture one declared cohort; missing causal companions fail closed."""
    canonical_cohort = COHORT_REGISTRY[cohort.cohort]
    if cohort.cohort == "source_analysis" and cohort != canonical_cohort:
        return _held_evidence_receipt(
            cohort,
            command=cohort.declared_command,
            provenance=provenance,
            baseline_revision=baseline_revision,
            current_revision=current_revision,
            reason="cohort declaration does not match the frozen registry; no workload executed",
        )
    if (
        cohort.cohort == "source_analysis"
        and baseline_revision is not None
        and current_revision is not None
        and provenance is not None
    ):
        return _paired_source_analysis(
            Path(repo_root).resolve(),
            cohort,
            baseline_revision=baseline_revision,
            current_revision=current_revision,
            samples=samples,
            timeout_seconds=timeout_seconds,
            config_paths=config_paths,
            provenance=provenance,
        )
    if cohort.cohort == "ci" and cohort == canonical_cohort:
        return _held_evidence_receipt(
            cohort,
            command=cohort.declared_command,
            provenance=provenance,
            baseline_revision=baseline_revision,
            current_revision=current_revision,
            reason=(
                "CI evidence requires collect_paired_ci_performance.py; "
                "protocol template was not executed"
            ),
        )
    reasons: list[str] = ["paired execution provenance requires a revision-aware harness"]
    required: dict[CohortName, set[str]] = {
        "integrity": {"stage", "sql_statements", "rows"},
        "migrations": {"stage", "alembic_revision"},
        "route_cold_warm": {
            "stage",
            "sql_statements",
            "rows",
            "connection_role",
            "route_companions",
        },
        "dcf": {"stage", "elapsed_seconds", "peak_rss_bytes"},
        "source_analysis": {"stage", "elapsed_seconds", "peak_rss_bytes"},
        "ci": {"stage", "elapsed_seconds"},
    }
    if cohort.cohort == "route_cold_warm" and (
        cohort.route_count != 20 or len(cohort.route_names) != 20
    ):
        reasons.append("route_cold_warm requires exactly 20 declared routes")
    baseline = _capture_performance_baseline(
        repo_root,
        cohort.declared_command,
        samples=samples,
        timeout_seconds=timeout_seconds,
        config_paths=config_paths,
        provenance=provenance,
        require_causal_envelope=True,
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
        "route_companions": [run.route_companions for run in runs],
    }
    for field in required[cohort.cohort]:
        if not runs or any(value in (None, "") for value in requirement_values[field]):
            reasons.append(f"{cohort.cohort} requires causal evidence: {field}")
    if cohort.cohort == "route_cold_warm":
        for run in runs:
            if len(run.route_companions) != 40:
                reasons.append("route_cold_warm requires 20 routes measured cold and warm")
                break
            names = {companion.route_name for companion in run.route_companions}
            phases = {companion.phase for companion in run.route_companions}
            if names != set(cohort.route_names) or phases != {"cold", "warm"}:
                reasons.append("route_cold_warm route companion identity is incomplete")
                break
            expected_keys = {
                (name, phase) for name in cohort.route_names for phase in ("cold", "warm")
            }
            actual_keys = {
                (companion.route_name, companion.phase) for companion in run.route_companions
            }
            if actual_keys != expected_keys:
                reasons.append(
                    "route_cold_warm requires one unique cold and warm companion per route"
                )
                break
            invalid = [
                companion
                for companion in run.route_companions
                if companion.status_code not in companion.allowed_success_statuses
                or companion.elapsed_seconds <= 0
                or companion.network_disabled is not True
                or companion.external_attempt_count != 0
                or companion.external_call_hold_seconds < 0
                or not companion.external_trap_sha256
                or not companion.state_sha256
            ]
            if invalid:
                reasons.append(
                    "route_cold_warm requires valid status, SQL/connection, state, and external-boundary proof"
                )
                break
            if not any(companion.sql_statements > 0 for companion in run.route_companions):
                reasons.append("route_cold_warm requires nontrivial SQL measurement")
                break
            if not any(companion.connection_count > 0 for companion in run.route_companions):
                reasons.append("route_cold_warm requires nontrivial connection measurement")
                break
            by_route: dict[str, dict[str, RouteCausalCompanion]] = {}
            for companion in run.route_companions:
                by_route.setdefault(companion.route_name, {})[companion.phase] = companion
            if any(
                (
                    by_route[name]["cold"].status_code,
                    by_route[name]["cold"].allowed_success_statuses,
                    by_route[name]["cold"].response_sha256,
                    by_route[name]["cold"].fixture_sha256,
                    by_route[name]["cold"].state_sha256,
                )
                != (
                    by_route[name]["warm"].status_code,
                    by_route[name]["warm"].allowed_success_statuses,
                    by_route[name]["warm"].response_sha256,
                    by_route[name]["warm"].fixture_sha256,
                    by_route[name]["warm"].state_sha256,
                )
                for name in cohort.route_names
            ):
                reasons.append("route_cold_warm cold/warm response and state identity is required")
                break
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
