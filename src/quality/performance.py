"""Raw local performance timing capture; never performance admission.

Benchmark commands are caller-trusted and execute locally without network
sandboxing. The child environment excludes credential-like variables, and raw
output is hashed but only a bounded, redacted preview may enter a receipt.
"""

from __future__ import annotations

import os
import subprocess
import time
from collections.abc import Mapping, Sequence
from pathlib import Path

from log_redact import sanitize_operational_text
from runtime.python_process import ensure_managed_python_argv

from .performance_models import (
    ADMISSION_HOLD_REASON,
    OUTPUT_PREVIEW_LIMIT,
    PerformanceReceipt,
    ReceiptStatus,
    TimingSample,
    TimingStats,
)
from .performance_support import (
    PerformanceExecutionError,
    PerformanceIdentityError,
    PerformanceInputError,
    PerformanceOutputError,
    declared_config_hash,
    describe_samples,
    parse_command,
    runtime_environment,
    scanner_identity,
    sha256_bytes,
    source_identity,
    source_revision,
    tracked_python_source_hash,
    validate_config_paths,
    validate_samples,
    validate_timeout,
)

__all__ = [
    "ADMISSION_HOLD_REASON",
    "PerformanceExecutionError",
    "PerformanceIdentityError",
    "PerformanceInputError",
    "PerformanceOutputError",
    "capture_performance_baseline",
]

_SENSITIVE_ENV_MARKERS = (
    "API_KEY",
    "TOKEN",
    "SECRET",
    "PASSWORD",
    "PASSPHRASE",
    "CREDENTIAL",
    "AUTHORIZATION",
    "COOKIE",
    "PRIVATE_KEY",
)
_FORBIDDEN_ENV_NAMES = {
    "AWS_ACCESS_KEY_ID",
    "AWS_PROFILE",
    "AWS_DEFAULT_PROFILE",
    "DYLD_INSERT_LIBRARIES",
    "LD_PRELOAD",
    "PYTHONHOME",
    "PYTHONPATH",
}


def benchmark_environment(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """Preserve ordinary runtime variables while removing credential-like ones."""
    source = os.environ if environ is None else environ
    return {
        key: value
        for key, value in source.items()
        if key.upper() not in _FORBIDDEN_ENV_NAMES
        and not key.upper().startswith("GIT_")
        and not any(marker in key.upper() for marker in _SENSITIVE_ENV_MARKERS)
    }


def run_benchmark_subprocess(
    argv: Sequence[str], *, cwd: Path, timeout: float, env: dict[str, str]
) -> subprocess.CompletedProcess[bytes]:
    """Narrow patchable benchmark subprocess seam."""
    return subprocess.run(
        list(argv),
        cwd=cwd,
        capture_output=True,
        check=False,
        timeout=timeout,
        env=env,
    )


def _managed_argv(root: Path, argv: Sequence[str]) -> list[str]:
    try:
        return ensure_managed_python_argv(root, argv)
    except (OSError, RuntimeError, ValueError) as exc:
        raise PerformanceIdentityError("managed Python command is unavailable") from exc


def _bounded_utf8(text: str, *, max_bytes: int) -> str:
    """Bound text without leaving a partial UTF-8 code point."""
    return text.encode("utf-8")[:max_bytes].decode("utf-8", errors="ignore")


def _run_once(
    root: Path, argv: Sequence[str], timeout: float, env: dict[str, str]
) -> tuple[subprocess.CompletedProcess[bytes], float]:
    started = time.perf_counter()
    try:
        completed = run_benchmark_subprocess(
            argv,
            cwd=root,
            timeout=timeout,
            env=env,
        )
    except subprocess.TimeoutExpired as exc:
        raise PerformanceExecutionError("benchmark timed out") from exc
    except (OSError, subprocess.SubprocessError) as exc:
        raise PerformanceExecutionError("benchmark execution failed") from exc
    elapsed = time.perf_counter() - started
    if completed.returncode != 0:
        raise PerformanceExecutionError("benchmark exited nonzero")
    if not (elapsed > 0 and elapsed < float("inf")):
        raise PerformanceOutputError("elapsed sample is malformed")
    return completed, elapsed


def capture_performance_baseline(
    repo_root: str | Path,
    command: object,
    *,
    samples: object = 7,
    timeout_seconds: object = 120.0,
    config_paths: object = None,
    provenance: object = "local-timing",
) -> PerformanceReceipt:
    """Collect raw timing; successful collection remains admission HOLD."""
    resolved_samples = validate_samples(samples)
    resolved_timeout = validate_timeout(timeout_seconds)
    benchmark_command, declared_argv = parse_command(command)
    declared_configs = validate_config_paths(
        ["pyproject.toml", "requirements.lock"] if config_paths is None else config_paths
    )
    if not isinstance(provenance, str) or not provenance.strip():
        raise PerformanceInputError("provenance is required")
    try:
        root = Path(repo_root).resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise PerformanceIdentityError("repository root is unavailable") from exc

    revision = source_revision(root)
    content_identity = source_identity(root)
    source_sha256 = tracked_python_source_hash(root)
    config_sha256 = declared_config_hash(root, declared_configs)
    scanner_sha256, scanner_version = scanner_identity(root)
    executed_argv = _managed_argv(root, declared_argv)
    child_env = benchmark_environment()

    output_parts: list[bytes] = []
    exit_codes: list[int] = []
    measured_seconds: list[float] = []

    warmup, warmup_seconds = _run_once(root, executed_argv, resolved_timeout, child_env)
    output_parts.extend((warmup.stdout, warmup.stderr))
    exit_codes.append(warmup.returncode)

    for _ in range(resolved_samples):
        completed, elapsed = _run_once(root, executed_argv, resolved_timeout, child_env)
        measured_seconds.append(elapsed)
        output_parts.extend((completed.stdout, completed.stderr))
        exit_codes.append(completed.returncode)

    combined_output = b"".join(output_parts)
    try:
        decoded_output = combined_output.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PerformanceOutputError("benchmark output is malformed") from exc
    redacted_preview = sanitize_operational_text(
        decoded_output[:OUTPUT_PREVIEW_LIMIT], mode="persisted"
    )
    output_preview = _bounded_utf8(redacted_preview, max_bytes=OUTPUT_PREVIEW_LIMIT)
    median, mad, confidence_interval, stability = describe_samples(measured_seconds)
    timing_samples = [
        TimingSample(label="measured", ordinal=index, elapsed_seconds=value)
        for index, value in enumerate(measured_seconds, start=1)
    ]
    reasons = [ADMISSION_HOLD_REASON]
    if resolved_samples < 7:
        reasons.append("at least 7 measured repeats are required for stability")
    if content_identity == "working_tree":
        reasons.append("working-tree content differs from the recorded revision")
    status: ReceiptStatus = "HOLD"
    return PerformanceReceipt(
        schema_version="performance-baseline/v1",
        benchmark_command=benchmark_command,
        command_argv=executed_argv,
        revision=revision,
        source_identity=content_identity,
        source_sha256=source_sha256,
        config_sha256=config_sha256,
        scanner_sha256=scanner_sha256,
        scanner_version=scanner_version,
        timing=TimingStats(
            samples=measured_seconds,
            count=len(measured_seconds),
            median_seconds=median,
            mad_seconds=mad,
            bootstrap_ci_95=confidence_interval,
            stability_verdict=stability,
        ),
        timing_samples=timing_samples,
        warmup_seconds=warmup_seconds,
        environment=runtime_environment(),
        collection_status="COMPLETE",
        admission_status="HOLD",
        status=status,
        hold=True,
        hold_reasons=reasons,
        exit_codes=exit_codes,
        output_sha256=sha256_bytes(combined_output),
        output_bytes=len(combined_output),
        output_preview=output_preview,
        provenance=provenance,
    )
