"""Shared deterministic helpers for performance receipt capture."""

from __future__ import annotations

import hashlib
import io
import os
import platform
import random
import shlex
import statistics
import subprocess
import sys
import tarfile
from pathlib import Path
from typing import Literal

from runtime.python_process import ensure_managed_python_argv

from .performance_models import (
    CausalEvidence,
    CompanionMeasures,
    FrozenPerformanceCohort,
    PerformanceEvidenceReceipt,
    PerformanceReceipt,
    TimingStats,
)


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str | None:
    """Hash one local artifact; unreadable files cannot satisfy identity."""
    try:
        return _sha256_bytes(path.read_bytes())
    except OSError:
        return None


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


def _archive_revision(root: Path, revision: str, destination: Path) -> None:
    """Materialize a tracked revision without changing the caller's checkout."""
    completed = subprocess.run(
        ["git", "-C", str(root), "archive", "--format=tar", revision],
        capture_output=True,
        check=True,
    )
    with tarfile.open(fileobj=io.BytesIO(completed.stdout), mode="r:") as archive:
        for member in archive.getmembers():
            target = (destination / member.name).resolve()
            if destination.resolve() not in target.parents:
                raise ValueError("git archive contained an unsafe path")
            archive.extract(member, destination)


def _bootstrap_median(samples: list[float]) -> tuple[float, float] | None:
    if len(samples) < 2:
        return None
    rng = random.Random(0)
    estimates = [statistics.median(rng.choices(samples, k=len(samples))) for _ in range(1000)]
    estimates.sort()
    return estimates[25], estimates[974]


def _managed_command(repo_root: Path, argv: list[str]) -> list[str]:
    """Resolve portable Python command names through the managed runtime."""
    if argv and Path(argv[0]).name.lower() in {"python", "python3", "python.exe"}:
        argv[0] = sys.executable
    return ensure_managed_python_argv(repo_root, argv)


def held_evidence_receipt(
    cohort: FrozenPerformanceCohort,
    *,
    command: str,
    provenance: Literal["mac_guidance", "approved_windows_production_shaped"] | None,
    baseline_revision: str | None,
    current_revision: str | None,
    reason: str,
) -> PerformanceEvidenceReceipt:
    """Return a typed hold without launching a caller-selected workload."""
    try:
        command_argv = shlex.split(command)
    except ValueError:
        command_argv = []
    baseline = PerformanceReceipt(
        benchmark_command=command,
        command_argv=command_argv,
        revision=None,
        source_sha256=None,
        config_sha256=None,
        timing=_timing([]),
        environment=_environment(),
        status="HOLD",
        hold=True,
        hold_reasons=[reason],
        exit_codes=[],
        output_sha256=None,
        output_bytes=0,
        output="",
        warmup_seconds=None,
        timing_samples=[],
        median_seconds=None,
        mad_seconds=None,
        bootstrap_ci_95=None,
        stability_verdict="insufficient",
        adaptive_verdict="hold",
        companion_measures=CompanionMeasures(
            sql_statements=None,
            rows=None,
            elapsed_seconds=None,
            peak_rss_bytes=None,
        ),
        provenance=provenance or "mac_guidance",
        causal_runs=[],
    )
    return PerformanceEvidenceReceipt(
        cohort=cohort,
        baseline=baseline,
        causal_evidence=CausalEvidence(
            sql_statements=None,
            rows=None,
            elapsed_seconds=None,
            peak_rss_bytes=None,
            alembic_revision=None,
            query_plan_sha256=None,
            connection_role="none",
            stage=None,
        ),
        causal_runs=(),
        baseline_revision=baseline_revision,
        current_revision=current_revision,
        paired_identity=False,
    )


__all__ = [
    "_archive_revision",
    "_bootstrap_median",
    "_environment",
    "_git",
    "_managed_command",
    "_sha256_bytes",
    "_sha256_file",
    "_source_hash",
    "_timing",
    "_tree_hash",
    "held_evidence_receipt",
]
