"""Deterministic identity and statistics helpers for raw timing capture."""

from __future__ import annotations

import hashlib
import math
import os
import platform
import random
import re
import shlex
import statistics
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path, PurePosixPath
from typing import Literal, cast

from .git_env import clean_local_git_env
from .performance_models import SCANNER_VERSION, SourceIdentity

BOOTSTRAP_REPLICATES = 1000
STABILITY_TOLERANCE = 0.05
_COMMIT_RE = re.compile(r"^[0-9a-fA-F]{40}$")
_WINDOWS_ABSOLUTE_RE = re.compile(r"^[A-Za-z]:[\\/]")


class PerformanceError(Exception):
    """Safe public base error for timing collection."""


class PerformanceInputError(PerformanceError):
    """Caller input cannot be interpreted safely."""


class PerformanceIdentityError(PerformanceError):
    """Source, configuration, or scanner identity is unavailable."""


class PerformanceExecutionError(PerformanceError):
    """The benchmark process did not complete successfully."""


class PerformanceOutputError(PerformanceError):
    """Benchmark output or elapsed measurements are malformed."""


def run_git_subprocess(
    args: Sequence[str], *, cwd: Path, env: dict[str, str]
) -> subprocess.CompletedProcess[bytes]:
    """Narrow patchable Git subprocess seam."""
    return subprocess.run(
        list(args),
        cwd=cwd,
        capture_output=True,
        check=False,
        env=env,
        timeout=30,
    )


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def parse_command(command: object) -> tuple[str, list[str]]:
    if not isinstance(command, str) or not command.strip():
        raise PerformanceInputError("benchmark command is empty")
    try:
        argv = shlex.split(command)
    except ValueError as exc:
        raise PerformanceInputError("benchmark command is malformed") from exc
    if not argv:
        raise PerformanceInputError("benchmark command is empty")
    return command, argv


def validate_samples(samples: object) -> int:
    if isinstance(samples, bool) or not isinstance(samples, int):
        raise PerformanceInputError("sample count is invalid")
    if samples < 1 or samples > 21:
        raise PerformanceInputError("sample count is invalid")
    return samples


def validate_timeout(timeout: object) -> float:
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
        raise PerformanceInputError("timeout is invalid")
    value = float(timeout)
    if not math.isfinite(value) or value <= 0:
        raise PerformanceInputError("timeout is invalid")
    return value


def _safe_relative_name(value: object, *, kind: str) -> str:
    if not isinstance(value, str) or not value or "\x00" in value or "\\" in value:
        raise PerformanceInputError(f"{kind} declaration is invalid")
    path = PurePosixPath(value)
    if value.startswith("/") or _WINDOWS_ABSOLUTE_RE.match(value) or ".." in path.parts:
        raise PerformanceInputError(f"{kind} path escapes repository")
    if str(path) != value or value == ".":
        raise PerformanceInputError(f"{kind} declaration is invalid")
    return value


def validate_config_paths(config_paths: object) -> list[str]:
    if not isinstance(config_paths, list) or not config_paths:
        raise PerformanceInputError("config declaration is invalid")
    raw_values = cast(list[object], config_paths)
    if not all(isinstance(value, str) for value in raw_values):
        raise PerformanceInputError("config declaration is invalid")
    string_values = cast(list[str], raw_values)
    names = [_safe_relative_name(value, kind="config") for value in string_values]
    if len(names) != len(set(names)):
        raise PerformanceInputError("duplicate config path")
    return names


def _git_bytes(root: Path, *args: str) -> bytes:
    try:
        completed = run_git_subprocess(
            ["git", "-C", str(root), *args],
            cwd=root,
            env=clean_local_git_env(),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise PerformanceIdentityError("git subprocess failed") from exc
    if completed.returncode != 0:
        raise PerformanceIdentityError("git subprocess failed")
    return completed.stdout


def _parse_nul_paths(raw: bytes) -> list[str]:
    if not raw:
        return []
    if not raw.endswith(b"\x00"):
        raise PerformanceIdentityError("tracked path listing is malformed")
    parts = raw[:-1].split(b"\x00")
    if any(not part for part in parts):
        raise PerformanceIdentityError("tracked path listing is malformed")
    try:
        names = [part.decode("utf-8") for part in parts]
    except UnicodeDecodeError as exc:
        raise PerformanceIdentityError("tracked path is invalid") from exc
    try:
        checked = [_safe_relative_name(name, kind="tracked") for name in names]
    except PerformanceInputError as exc:
        raise PerformanceIdentityError("tracked path is invalid") from exc
    if len(checked) != len(set(checked)):
        raise PerformanceIdentityError("duplicate tracked path")
    return checked


def source_revision(root: Path) -> str:
    raw = _git_bytes(root, "rev-parse", "--verify", "HEAD^{commit}")
    try:
        revision = raw.decode("ascii").strip()
    except UnicodeDecodeError as exc:
        raise PerformanceIdentityError("revision is unavailable") from exc
    if not _COMMIT_RE.fullmatch(revision):
        raise PerformanceIdentityError("revision is unavailable")
    return revision.lower()


def _resolved_root(root: Path) -> Path:
    try:
        return root.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise PerformanceIdentityError("repository root is unavailable") from exc


def _hash_entries(root: Path, names: Sequence[str], *, empty_error: str) -> str:
    resolved_root = _resolved_root(root)
    entries: list[bytes] = []
    for name in sorted(names):
        target = resolved_root / name
        try:
            resolved = target.resolve(strict=True)
            resolved.relative_to(resolved_root)
            content = resolved.read_bytes()
        except (OSError, RuntimeError, ValueError) as exc:
            raise PerformanceIdentityError("declared file is unavailable") from exc
        entries.append(name.encode("utf-8") + b"\x00" + sha256_bytes(content).encode() + b"\n")
    if not entries:
        raise PerformanceIdentityError(empty_error)
    return sha256_bytes(b"".join(entries))


def tracked_python_source_hash(root: Path) -> str:
    names = _parse_nul_paths(_git_bytes(root, "ls-files", "-z", "--", "src", "execution"))
    python_names = [name for name in names if name.endswith(".py")]
    return _hash_entries(root, python_names, empty_error="tracked source is unavailable")


def declared_config_hash(root: Path, config_paths: Sequence[str]) -> str:
    raw = _git_bytes(root, "ls-files", "-z", "--error-unmatch", "--", *config_paths)
    tracked = _parse_nul_paths(raw)
    if set(tracked) != set(config_paths) or len(tracked) != len(config_paths):
        raise PerformanceIdentityError("declared config is untracked")
    return _hash_entries(root, config_paths, empty_error="declared config is unavailable")


def source_identity(root: Path) -> SourceIdentity:
    raw = _git_bytes(
        root,
        "status",
        "--porcelain=v1",
        "-z",
        "--untracked-files=all",
    )
    return "working_tree" if raw else "clean_head"


def scanner_identity(root: Path) -> tuple[str, str]:
    names = [
        "src/log_redact.py",
        "src/quality/git_env.py",
        "src/quality/performance.py",
        "src/quality/performance_models.py",
        "src/quality/performance_support.py",
        "src/runtime/python_process.py",
        "execution/capture_performance_baseline.py",
    ]
    return (
        _hash_entries(root, names, empty_error="scanner is unavailable"),
        SCANNER_VERSION,
    )


def runtime_environment() -> dict[str, str]:
    return {
        "python": platform.python_version(),
        "python_implementation": platform.python_implementation(),
        "platform": platform.platform(aliased=True),
        "machine": platform.machine(),
        "processor": platform.processor() or "unknown",
        "executable": os.path.realpath(sys.executable),
        "runtime": sys.version.replace("\n", " "),
    }


def bootstrap_ci_95(samples: Sequence[float]) -> tuple[float, float] | None:
    if len(samples) < 2:
        return None
    rng = random.Random(0)
    estimates = [
        float(statistics.median(rng.choices(samples, k=len(samples))))
        for _ in range(BOOTSTRAP_REPLICATES)
    ]
    estimates.sort()
    return estimates[25], estimates[974]


def describe_samples(
    samples: Sequence[float],
) -> tuple[
    float | None,
    float | None,
    tuple[float, float] | None,
    Literal["stable", "unstable", "insufficient"],
]:
    if not samples:
        return None, None, None, "insufficient"
    median = float(statistics.median(samples))
    mad = float(statistics.median(abs(value - median) for value in samples))
    verdict: Literal["stable", "unstable", "insufficient"] = (
        "insufficient"
        if len(samples) < 7
        else "stable"
        if mad <= median * STABILITY_TOLERANCE
        else "unstable"
    )
    return median, mad, bootstrap_ci_95(samples), verdict


__all__ = [
    "PerformanceError",
    "PerformanceExecutionError",
    "PerformanceIdentityError",
    "PerformanceInputError",
    "PerformanceOutputError",
    "bootstrap_ci_95",
    "declared_config_hash",
    "describe_samples",
    "parse_command",
    "run_git_subprocess",
    "runtime_environment",
    "scanner_identity",
    "sha256_bytes",
    "source_identity",
    "source_revision",
    "tracked_python_source_hash",
    "validate_config_paths",
    "validate_samples",
    "validate_timeout",
]
