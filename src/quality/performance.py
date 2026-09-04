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

from pydantic import BaseModel, ConfigDict

ReceiptStatus = Literal["PASS", "HOLD", "FAIL"]


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
    if not companion_measures or not required_companions <= companion_measures.keys():
        reasons.append("all companion measures are required")
    if provenance is None:
        reasons.append("evidence provenance is required")
    durations: list[float] = []
    exit_codes: list[int] = []
    output_parts: list[bytes] = []
    failed = False
    warmup_seconds: float | None = None
    timing_samples: list[TimingSample] = []
    for run_number in range((max(samples, 0) + 1) if argv else 0):
        started = time.perf_counter()
        try:
            completed = subprocess.run(
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
    )
