"""Immutable revision-paired source-analysis benchmark."""

from __future__ import annotations

import json
import shlex
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Literal

from .performance_models import (
    CausalEvidence,
    CausalRunEnvelope,
    CompanionMeasures,
    FrozenPerformanceCohort,
    PerformanceEvidenceReceipt,
    PerformanceReceipt,
    SourceAnalysisSummary,
    TimingSample,
)
from .performance_support import (
    _archive_revision,
    _bootstrap_median,
    _environment,
    _git,
    _managed_command,
    _sha256_bytes,
    _sha256_file,
    _source_hash,
    _timing,
    _tree_hash,
)

# The source-analysis cohort must use this collector-pinned scanner.  A
# revision under test may contain a file with the same name, but it cannot
# replace the scanner process used to produce the receipt.
_TRUSTED_SCANNER_RELATIVE_PATH = Path("execution/analyze_code_duplicates.py")
_TRUSTED_SCANNER_IMPLEMENTATION_RELATIVE_PATH = Path("src/quality/duplicates.py")
_TRUSTED_SCANNER_WRAPPER_SHA256 = "a58155146ef2042cf01ef0073f7e7e4423306ff23accf6773cd3da43b323b7d9"
_TRUSTED_SCANNER_IMPLEMENTATION_SHA256 = (
    "00e368ea7988c87450670b6ed4c7463953e51bf38779292f344d46c8f4e4552e"
)


def paired_source_analysis(
    root: Path,
    cohort: FrozenPerformanceCohort,
    *,
    baseline_revision: str,
    current_revision: str,
    samples: int,
    timeout_seconds: float,
    config_paths: list[str] | None,
    provenance: Literal["mac_guidance", "approved_windows_production_shaped"],
) -> PerformanceEvidenceReceipt:
    """Run immutable revision snapshots with truthful parser-cache evidence.

    ``quality.duplicates`` parses every file on every invocation and exposes no
    cache counters. Repeated invocations are therefore cold, rather than being
    relabelled warm based on their ordinal position.
    """
    reasons: list[str] = []
    identity_reasons: list[str] = []
    revisions_are_distinct = baseline_revision != current_revision
    if not revisions_are_distinct:
        identity_reasons.append(
            "baseline and current revisions must be distinct immutable revisions"
        )
    trusted_root = Path(__file__).resolve().parents[2]
    trusted_wrapper = trusted_root / _TRUSTED_SCANNER_RELATIVE_PATH
    trusted_implementation = trusted_root / _TRUSTED_SCANNER_IMPLEMENTATION_RELATIVE_PATH
    trusted_wrapper_sha256 = _sha256_file(trusted_wrapper)
    trusted_implementation_sha256 = _sha256_file(trusted_implementation)
    if trusted_wrapper_sha256 != _TRUSTED_SCANNER_WRAPPER_SHA256:
        identity_reasons.append("trusted duplicate scanner wrapper identity mismatch")
    if trusted_implementation_sha256 != _TRUSTED_SCANNER_IMPLEMENTATION_SHA256:
        identity_reasons.append("trusted duplicate scanner implementation identity mismatch")
    expected_commits = {
        baseline_revision: _git(root, "rev-parse", baseline_revision),
        current_revision: _git(root, "rev-parse", current_revision),
    }
    if any(value is None for value in expected_commits.values()):
        identity_reasons.append("cannot resolve immutable baseline/current revisions")
    if samples < 7:
        reasons.append("at least 7 measured repeats are required")
    count = min(max(samples, 0), 21)
    runs: list[CausalRunEnvelope] = []
    revision_durations: dict[str, list[float]] = {
        baseline_revision: [],
        current_revision: [],
    }
    revision_warmups: dict[str, list[float]] = {
        baseline_revision: [],
        current_revision: [],
    }
    outputs: list[bytes] = []
    exit_codes: list[int] = []
    with tempfile.TemporaryDirectory(prefix="performance-paired-") as temp_name:
        temp_root = Path(temp_name)
        # Equality collapses the revision-keyed maps below and can otherwise
        # make one revision appear to satisfy both sides of the pair.  Reject
        # it before any benchmark execution or set comparison is attempted.
        rounds = range(22) if revisions_are_distinct else ()
        for round_number in rounds:
            warmup = round_number == 0
            completed_round = True
            stability_by_revision: dict[str, bool] = {}
            for revision in (baseline_revision, current_revision):
                revision_samples = revision_durations[revision]
                stable = False
                if len(revision_samples) >= 7:
                    revision_median = statistics.median(revision_samples)
                    revision_mad = statistics.median(
                        abs(value - revision_median) for value in revision_samples
                    )
                    stable = revision_mad <= revision_median * 0.05
                stability_by_revision[revision] = stable
            any_unstable = any(
                len(revision_durations[revision]) >= 7 and not stable
                for revision, stable in stability_by_revision.items()
            )
            for revision in (baseline_revision, current_revision):
                if (
                    not warmup
                    and round_number > count
                    and not any_unstable
                    and stability_by_revision[revision]
                ):
                    continue
                completed_round = False
                snapshot = temp_root / revision.replace("/", "_")
                if not snapshot.exists():
                    snapshot.mkdir()
                    try:
                        _archive_revision(root, revision, snapshot)
                    except (OSError, subprocess.SubprocessError, ValueError) as exc:
                        reasons.append(
                            f"cannot materialize revision {revision}: {type(exc).__name__}"
                        )
                        completed_round = True
                        continue
                output_path = snapshot / ".tmp" / "quality" / "performance-duplicates.json"
                # Run the trusted collector scanner against the immutable
                # revision selector. Never import or execute a script from
                # the revision-under-test (including ``snapshot``).
                argv = [
                    sys.executable,
                    str(trusted_wrapper),
                    "--repo-root",
                    str(root),
                    "--revision",
                    revision,
                    "--out",
                    str(output_path),
                ]
                argv = _managed_command(trusted_root, argv)
                started = time.perf_counter()
                try:
                    completed = subprocess.run(
                        argv,
                        cwd=snapshot,
                        capture_output=True,
                        timeout=timeout_seconds,
                        check=False,
                    )
                except (OSError, subprocess.SubprocessError) as exc:
                    reasons.append(f"paired benchmark failed: {type(exc).__name__}")
                    completed_round = True
                    continue
                elapsed = time.perf_counter() - started
                phase: Literal["warmup", "cold", "warm"] = "warmup" if warmup else "cold"
                if warmup:
                    revision_warmups[revision].append(elapsed)
                else:
                    revision_durations[revision].append(elapsed)
                exit_codes.append(completed.returncode)
                outputs.append(completed.stdout + completed.stderr)
                if completed.returncode not in (0, 2) or not output_path.is_file():
                    reasons.append("source analysis command produced no valid inventory")
                    completed_round = True
                    continue
                try:
                    inventory = json.loads(output_path.read_text(encoding="utf-8"))
                    rows = int(inventory["files_scanned"])
                    if (
                        inventory.get("scoped_revision") != revision
                        or inventory.get("commit_hash") != expected_commits[revision]
                    ):
                        identity_reasons.append(
                            f"inventory identity mismatch for revision {revision}"
                        )
                    if inventory.get("scanner_hash") != _TRUSTED_SCANNER_IMPLEMENTATION_SHA256:
                        identity_reasons.append(
                            f"inventory scanner identity mismatch for revision {revision}"
                        )
                    if rows <= 0:
                        reasons.append(f"source analysis scanned no files for revision {revision}")
                    if completed.returncode == 2:
                        reasons.append("source analysis reported parse errors")
                except (OSError, ValueError, KeyError, TypeError):
                    reasons.append("source analysis inventory is malformed")
                    completed_round = True
                    continue
                runs.append(
                    CausalRunEnvelope(
                        sql_statements=0,
                        rows=rows,
                        elapsed_seconds=elapsed,
                        peak_rss_bytes=0,
                        alembic_revision=None,
                        query_plan_sha256=None,
                        connection_role="none",
                        stage="source-analysis",
                        revision=revision,
                        phase=phase,
                        process_peak_rss_bytes=None,
                        cache_state="no-cache",
                        cache_hits=0,
                        cache_misses=rows,
                        parsed_once=True,
                        rss_semantics="unavailable",
                    )
                )
            if completed_round:
                break
    baseline_values = revision_durations[baseline_revision]
    current_values = revision_durations[current_revision]
    baseline_warmups = revision_warmups[baseline_revision]
    current_warmups = revision_warmups[current_revision]
    durations = baseline_values + current_values
    timing_samples = [TimingSample(label="cold", elapsed_seconds=value) for value in durations]
    median = statistics.median(durations) if durations else None
    mad = statistics.median(abs(value - median) for value in durations) if median else None
    if any(len(values) < count for values in revision_durations.values()):
        reasons.append("paired baseline/current samples are incomplete")
    for revision, values in revision_durations.items():
        if len(values) >= 7:
            revision_median = statistics.median(values)
            revision_mad = statistics.median(abs(value - revision_median) for value in values)
            if revision_mad > revision_median * 0.05:
                reasons.append(f"timing samples remain unstable for revision {revision}")
    deltas = [
        current - baseline
        for baseline, current in zip(baseline_values, current_values, strict=False)
    ]
    reasons.append("per-revision RSS unavailable: parent child high-water is not comparable")
    timing_regression = (
        bool(baseline_values)
        and bool(current_values)
        and statistics.median(current_values) > statistics.median(baseline_values) * 1.10
    )
    if timing_regression:
        reasons.append("current source-analysis timing exceeds baseline by more than 10%")
    reasons.append("source-analysis parser has no cache; warm evidence unavailable")
    if provenance == "approved_windows_production_shaped" and sys.platform != "win32":
        reasons.append("approved Windows production-shaped evidence requires Windows host")
    reasons.extend(identity_reasons)
    reasons = list(dict.fromkeys(reasons))
    receipt = PerformanceReceipt(
        benchmark_command=cohort.declared_command,
        command_argv=shlex.split(cohort.declared_command),
        revision=current_revision,
        source_sha256=_source_hash(root),
        config_sha256=_tree_hash(root, config_paths or ["pyproject.toml", "requirements.lock"]),
        timing=_timing(durations),
        environment=_environment(),
        status="HOLD" if reasons else "PASS",
        hold=bool(reasons),
        hold_reasons=reasons,
        exit_codes=exit_codes,
        output_sha256=_sha256_bytes(b"".join(outputs)) if outputs else None,
        output_bytes=sum(len(output) for output in outputs),
        output=b"".join(outputs)[:12000].decode("utf-8", errors="replace"),
        warmup_seconds=statistics.median(baseline_warmups + current_warmups)
        if baseline_warmups and current_warmups
        else None,
        timing_samples=timing_samples,
        median_seconds=median,
        mad_seconds=mad,
        bootstrap_ci_95=_bootstrap_median(durations),
        stability_verdict="stable"
        if mad is not None and median and mad <= median * 0.05
        else "unstable",
        adaptive_verdict="eligible" if not reasons else "hold",
        companion_measures=CompanionMeasures(
            sql_statements=0,
            rows=int(statistics.median([run.rows for run in runs])) if runs else None,
            elapsed_seconds=median,
            peak_rss_bytes=None,
        ),
        provenance=provenance,
        causal_runs=runs,
        source_analysis=SourceAnalysisSummary(
            baseline_cold_seconds=tuple(baseline_values),
            current_cold_seconds=tuple(current_values),
            paired_delta_seconds=tuple(deltas),
            paired_delta_bootstrap_ci_95=_bootstrap_median(deltas),
            baseline_peak_rss_bytes=(),
            current_peak_rss_bytes=(),
            cache_disposition="no-cache",
            cache_hits=0,
            cache_misses=sum(run.cache_misses or 0 for run in runs),
            parsed_once=all(run.parsed_once is True for run in runs),
            baseline_warmup_seconds=tuple(baseline_warmups),
            current_warmup_seconds=tuple(current_warmups),
            warmup_count=2,
            regression_over_10_percent=timing_regression,
            rss_disposition="unavailable",
            trusted_scanner_sha256=trusted_implementation_sha256,
            trusted_scanner_wrapper_sha256=trusted_wrapper_sha256,
        ),
    )
    aggregate = CausalEvidence(
        sql_statements=0,
        rows=receipt.companion_measures.rows,
        elapsed_seconds=median,
        peak_rss_bytes=None,
        alembic_revision=None,
        query_plan_sha256=None,
        connection_role="none",
        stage="source-analysis",
    )
    return PerformanceEvidenceReceipt(
        cohort=cohort,
        baseline=receipt,
        causal_evidence=aggregate,
        causal_runs=tuple(runs),
        baseline_revision=baseline_revision,
        current_revision=current_revision,
        paired_identity=not identity_reasons,
    )


__all__ = ["paired_source_analysis"]
