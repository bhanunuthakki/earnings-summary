"""Sealed, deterministic output primitives for offline report rendering.

This module owns the narrow boundary shared by the offline CLI and its tests:
dependency classification, a WAL-aware private SQLite snapshot, runtime
capability denial, normalized deliverables, and a write-once receipt.  It does
not call the ordinary artifact builder or mutate canonical repository state.
"""

from __future__ import annotations

import builtins
import ctypes
import hashlib
import io
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Generator, Mapping, Sequence
from contextlib import contextmanager
from ctypes import wintypes
from datetime import date
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Literal, cast
from unittest.mock import patch

from pydantic import BaseModel, ConfigDict, Field, field_validator

ArtifactTelemetry = Callable[[str, Literal["start", "complete"], int | None], None]


class OfflineBoundaryError(RuntimeError):
    """A sealed offline-render invariant was violated."""


class UnclassifiedDependencyError(OfflineBoundaryError):
    """A local input did not match the closed dependency vocabulary."""


class DependencyClass(StrEnum):
    CODE = "code"
    DATABASE = "database"
    DATABASE_WAL = "database_wal"
    DATABASE_SHM = "database_shm"
    DATABASE_SNAPSHOT = "database_snapshot"
    FILESYSTEM = "filesystem"
    CONFIG = "config"
    PRICE = "price"
    ESTIMATE = "estimate"
    DCF = "dcf"
    PORTFOLIO = "portfolio"
    POLICY = "policy"
    RUNTIME = "runtime"


class DependencyRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    logical_path: str
    dependency_class: DependencyClass
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(ge=0)

    @field_validator("logical_path")
    @classmethod
    def _logical_path_is_relative(cls, value: str) -> str:
        normalized = PurePosixPath(value.replace("\\", "/"))
        if normalized.is_absolute() or ".." in normalized.parts:
            raise ValueError("dependency logical paths must be relative")
        return normalized.as_posix()


class OfflineArtifactPayload(BaseModel):
    model_config = ConfigDict(frozen=True)

    html: str
    markdown: str
    sections: dict[str, object]
    status: dict[str, object]
    numeric_provenance: dict[str, object]


class OfflineArtifactReceipt(BaseModel):
    model_config = ConfigDict(frozen=True)

    schema_version: Literal["offline_report_artifact.v1"] = "offline_report_artifact.v1"
    ticker: str
    as_of: date
    dependency_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    code_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    dependencies: tuple[DependencyRecord, ...]
    output_sha256: dict[str, str]
    network_attempts: Literal[0] = 0
    llm_attempts: Literal[0] = 0
    canonical_mutations: tuple[str, ...] = ()

    @field_validator("ticker")
    @classmethod
    def _canonical_ticker(cls, value: str) -> str:
        normalized = value.strip().upper()
        if not normalized:
            raise ValueError("ticker is required")
        return normalized


class OfflineGuardMetrics(BaseModel):
    network_attempts: int = 0
    subprocess_attempts: int = 0
    llm_attempts: int = 0
    denied_writes: int = 0
    denied_write_paths: list[str] = Field(default_factory=list)


_FILESYSTEM_PREFIXES = (
    "data/bear_case/",
    "data/company_description/",
    "data/earnings_themes/",
    "data/exec_comp/",
    "data/filing_intelligence/",
    "data/key_metrics/",
    "data/peer_selection/",
    "data/platform_diagram/",
    "data/qa_topics/",
    "data/recent_developments/",
    "data/report_comments/",
    "data/saydo_filter/",
    "data/segment_definitions/",
    "data/ticker_specific/",
    "data/valuation_basis/",
    ".tmp/",
    "micro_thesis/",
    "transcripts/",
)


def classify_dependency(path: Path, *, ticker: str) -> DependencyClass:
    """Classify one repo-relative input, failing closed on unknown paths."""

    del ticker  # Path membership is closed; ticker scoping is enforced by staging.
    logical = PurePosixPath(path.as_posix().lstrip("./")).as_posix()
    lower = logical.lower()
    if any(
        part in {".env", "credentials.json", "token.json"} or part.endswith((".key", ".pem"))
        for part in PurePosixPath(lower).parts
    ):
        raise UnclassifiedDependencyError(f"sensitive path is not an offline dependency: {logical}")
    if (
        lower.startswith("src/")
        or lower.startswith("alembic/")
        or lower == "execution/build_offline_artifact.py"
    ):
        return DependencyClass.CODE
    if lower == "data/portfolio.db":
        return DependencyClass.DATABASE
    if lower == "data/portfolio.db-wal":
        return DependencyClass.DATABASE_WAL
    if lower == "data/portfolio.db-shm":
        return DependencyClass.DATABASE_SHM
    if lower.startswith("data/holdings/") or lower == "portfolio.db":
        return DependencyClass.PORTFOLIO
    if lower.startswith("data/historical/fmp/") and "price" in path.name.lower():
        return DependencyClass.PRICE
    if lower.startswith("data/estimates/") or (
        lower.startswith("data/historical/fmp/") and "estimate" in path.name.lower()
    ):
        return DependencyClass.ESTIMATE
    if lower.startswith("config/") or lower in {
        "alembic.ini",
        "pyproject.toml",
        "uv.lock",
    }:
        return DependencyClass.CONFIG
    if lower.startswith("policies/"):
        return DependencyClass.POLICY
    if lower.startswith("directives/"):
        return DependencyClass.POLICY
    if lower.startswith("dcf/") and path.suffix.lower() == ".xlsx":
        return DependencyClass.DCF
    if lower.startswith("data/historical/fmp/") or any(
        lower.startswith(prefix) for prefix in _FILESYSTEM_PREFIXES
    ):
        return DependencyClass.FILESYSTEM
    raise UnclassifiedDependencyError(f"unclassified offline dependency: {logical}")


def dependency_record(
    path: Path, *, logical_path: str, dependency_class: DependencyClass
) -> DependencyRecord:
    body = path.read_bytes()
    return DependencyRecord(
        logical_path=logical_path,
        dependency_class=dependency_class,
        sha256=hashlib.sha256(body).hexdigest(),
        size_bytes=len(body),
    )


def _dependency_bytes(
    body: bytes, *, logical_path: str, dependency_class: DependencyClass
) -> DependencyRecord:
    return DependencyRecord(
        logical_path=logical_path,
        dependency_class=dependency_class,
        sha256=hashlib.sha256(body).hexdigest(),
        size_bytes=len(body),
    )


def runtime_dependency_records(
    *, isolated_repo: Path, private_write_root: Path
) -> tuple[DependencyRecord, ...]:
    """Hash the effective imported runtime closure and normalized minimal env."""

    isolated_repo = isolated_repo.resolve()
    private_write_root = private_write_root.resolve()
    runtime_root = Path(sys.base_prefix).resolve()
    records = list(
        runtime_native_dependency_records(
            isolated_repo=isolated_repo,
            runtime_root=runtime_root,
        )
    )
    normalized_environment: dict[str, str] = {}
    substitutions = (
        (str(private_write_root), "$PRIVATE_WRITE"),
        (str(isolated_repo), "$SEALED_REPO"),
        (str(runtime_root), "$PYTHON_RUNTIME"),
    )
    private_homepath = str(private_write_root)[len(private_write_root.drive) :]
    for key, value in sorted(os.environ.items()):
        normalized = value
        for source, replacement in substitutions:
            normalized = normalized.replace(source, replacement)
        if (
            os.name == "nt"
            and key.upper() == "HOMEPATH"
            and value.casefold() == private_homepath.casefold()
        ):
            normalized = "$PRIVATE_WRITE"
        normalized_environment[key] = normalized
    environment_body = _canonical_json(normalized_environment)
    records.append(
        _dependency_bytes(
            environment_body,
            logical_path="runtime/environment.json",
            dependency_class=DependencyClass.RUNTIME,
        )
    )
    return tuple(sorted(records, key=lambda item: item.logical_path))


def runtime_tree_dependency_records(runtime_root: Path) -> tuple[DependencyRecord, ...]:
    """Hash every regular file admitted by the Python-runtime ACL."""

    runtime_root = Path(os.path.abspath(runtime_root))
    if not runtime_root.is_dir():
        raise OfflineBoundaryError(f"Python runtime root is unavailable: {runtime_root}")
    _require_no_reparse_points(runtime_root)
    records: list[DependencyRecord] = []
    pending = [runtime_root]
    while pending:
        current = pending.pop()
        with os.scandir(current) as entries:
            children = sorted(entries, key=lambda entry: entry.name.casefold())
        directories: list[Path] = []
        for entry in children:
            path = Path(entry.path)
            attributes = getattr(path.lstat(), "st_file_attributes", 0)
            if entry.is_symlink() or attributes & 0x400:
                raise OfflineBoundaryError(f"Python runtime contains a reparse point: {path}")
            if entry.is_dir(follow_symlinks=False):
                directories.append(path)
                continue
            if not entry.is_file(follow_symlinks=False):
                raise OfflineBoundaryError(f"Python runtime contains a special file: {path}")
            relative = path.relative_to(runtime_root).as_posix()
            records.append(
                dependency_record(
                    path,
                    logical_path=f"runtime/python/{relative}",
                    dependency_class=DependencyClass.RUNTIME,
                )
            )
        pending.extend(reversed(directories))
    return tuple(sorted(records, key=lambda item: item.logical_path))


def runtime_native_dependency_records(
    *, isolated_repo: Path, runtime_root: Path
) -> tuple[DependencyRecord, ...]:
    """Hash the current process' complete loaded native module closure."""

    if os.name != "nt":
        raise OfflineBoundaryError("native runtime attestation requires Windows")
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    kernel32.GetCurrentProcess.argtypes = []
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    psapi.EnumProcessModules.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.HMODULE),
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    psapi.EnumProcessModules.restype = wintypes.BOOL
    psapi.GetModuleFileNameExW.argtypes = [
        wintypes.HANDLE,
        wintypes.HMODULE,
        wintypes.LPWSTR,
        wintypes.DWORD,
    ]
    psapi.GetModuleFileNameExW.restype = wintypes.DWORD
    process = kernel32.GetCurrentProcess()
    needed = wintypes.DWORD()
    capacity = 256
    while True:
        modules = (wintypes.HMODULE * capacity)()
        if not psapi.EnumProcessModules(
            process,
            modules,
            ctypes.sizeof(modules),
            ctypes.byref(needed),
        ):
            raise OfflineBoundaryError(
                f"native runtime enumeration failed ({ctypes.get_last_error()})"
            )
        count = needed.value // ctypes.sizeof(wintypes.HMODULE)
        if count <= capacity:
            break
        capacity = count + 32

    isolated_repo = isolated_repo.resolve()
    runtime_root = runtime_root.resolve()
    system_root = Path(os.environ.get("SYSTEMROOT", r"C:\Windows")).resolve()
    defender_root = (
        Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData"))
        / "Microsoft"
        / "Windows Defender"
        / "Platform"
    ).resolve()
    observed: dict[str, Path] = {}
    for module in modules[:count]:
        buffer = ctypes.create_unicode_buffer(32_768)
        length = psapi.GetModuleFileNameExW(process, module, buffer, len(buffer))
        if length == 0 or length >= len(buffer):
            raise OfflineBoundaryError(
                f"native runtime module identity failed ({ctypes.get_last_error()})"
            )
        path = Path(buffer.value).resolve()
        if _is_within(path, runtime_root):
            logical = f"runtime/python/{path.relative_to(runtime_root).as_posix()}"
        elif _is_within(path, isolated_repo):
            logical = f"runtime/sealed-repo/{path.relative_to(isolated_repo).as_posix()}"
        elif _is_within(path, system_root):
            logical = f"runtime/os/{path.relative_to(system_root).as_posix()}"
        elif _is_within(path, defender_root):
            logical = f"runtime/os/defender/{path.relative_to(defender_root).as_posix()}"
        else:
            raise OfflineBoundaryError(f"loaded native module escapes trusted roots: {path}")
        observed[logical] = path
    return tuple(
        dependency_record(path, logical_path=logical, dependency_class=DependencyClass.RUNTIME)
        for logical, path in sorted(observed.items())
    )


def _require_no_reparse_points(path: Path, *, attested_root: Path | None = None) -> None:
    current = Path(os.path.abspath(path))
    boundary = Path(os.path.abspath(attested_root)) if attested_root is not None else None
    if boundary is not None and current != boundary and boundary not in current.parents:
        raise OfflineBoundaryError(f"offline dependency escapes its attested root: {path}")
    while True:
        try:
            attributes = getattr(current.lstat(), "st_file_attributes", 0)
        except OSError as exc:
            raise OfflineBoundaryError(
                f"offline dependency identity is unavailable: {path}"
            ) from exc
        if current.is_symlink() or attributes & 0x400:
            raise OfflineBoundaryError(f"offline dependency contains a reparse point: {path}")
        if boundary is not None and current == boundary:
            return
        if current.parent == current:
            return
        current = current.parent


def _require_single_link_file(path: Path) -> None:
    _require_no_reparse_points(path)
    try:
        observed = path.stat()
    except OSError as exc:
        raise OfflineBoundaryError(f"offline dependency is unavailable: {path}") from exc
    if not path.is_file():
        raise OfflineBoundaryError(f"offline dependency is not a regular file: {path}")
    if observed.st_nlink != 1:
        raise OfflineBoundaryError(f"offline dependency has a hardlink alias: {path}")


def snapshot_database(
    source: Path,
    destination: Path,
    *,
    source_logical_path: str = "data/portfolio.db",
    source_class: DependencyClass = DependencyClass.DATABASE,
    snapshot_logical_path: str = "isolated/data/portfolio.db",
) -> tuple[DependencyRecord, ...]:
    """Take a private SQLite backup while binding the source DB/WAL bundle.

    Source bytes are hashed before and after backup. Any concurrent source
    mutation fails the snapshot instead of producing an ambiguously based
    artifact. The destination is a single checkpointed database suitable for
    immutable read-only connections.
    """

    source = Path(os.path.abspath(source))
    _require_single_link_file(source)
    if not source.is_file():
        raise FileNotFoundError(source)
    sidecars = (
        (source, source_class, source_logical_path),
        (
            source.with_name(f"{source.name}-wal"),
            DependencyClass.DATABASE_WAL,
            "data/portfolio.db-wal",
        ),
        (
            source.with_name(f"{source.name}-shm"),
            DependencyClass.DATABASE_SHM,
            "data/portfolio.db-shm",
        ),
    )
    before = {
        path: dependency_record(path, logical_path=logical, dependency_class=kind)
        for path, kind, logical in sidecars
        if path.is_file()
    }
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Opening a live WAL database can legitimately update lock bytes in its
    # shared-memory sidecar even through a read-only connection. Copy the byte
    # bundle first, then let SQLite resolve WAL state only inside private space.
    with tempfile.TemporaryDirectory(prefix="offline-db-bundle-", dir=destination.parent) as name:
        private_db = Path(name) / source.name
        copied: dict[Path, DependencyRecord] = {}
        for path, kind, logical in sidecars:
            if path not in before:
                continue
            _require_single_link_file(path)
            target = private_db.with_name(private_db.name + path.name.removeprefix(source.name))
            shutil.copyfile(path, target)
            copied[path] = dependency_record(
                target,
                logical_path=logical,
                dependency_class=kind,
            )
        if copied != before:
            raise OfflineBoundaryError("source database bundle changed during offline snapshot")
        source_conn = sqlite3.connect(f"{private_db.as_uri()}?mode=ro", uri=True)
        destination_conn = sqlite3.connect(destination)
        try:
            source_conn.backup(destination_conn)
        finally:
            destination_conn.close()
            source_conn.close()
    after = {}
    for path, kind, logical in sidecars:
        if path.is_file():
            _require_single_link_file(path)
            after[path] = dependency_record(
                path,
                logical_path=logical,
                dependency_class=kind,
            )
    if before != after:
        destination.unlink(missing_ok=True)
        raise OfflineBoundaryError("source database bundle changed during offline snapshot")
    snapshot = dependency_record(
        destination,
        logical_path=snapshot_logical_path,
        dependency_class=DependencyClass.DATABASE_SNAPSHOT,
    )
    return (*before.values(), snapshot)


def _copy_dependency(
    source_repo: Path,
    isolated_repo: Path,
    relative_path: Path,
    *,
    ticker: str,
) -> DependencyRecord:
    source = source_repo / relative_path
    _require_single_link_file(source)
    destination = isolated_repo / relative_path
    dependency_class = classify_dependency(relative_path, ticker=ticker)
    before = dependency_record(
        source,
        logical_path=relative_path.as_posix(),
        dependency_class=dependency_class,
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination)
    copied = dependency_record(
        destination,
        logical_path=relative_path.as_posix(),
        dependency_class=dependency_class,
    )
    if copied != before:
        destination.unlink(missing_ok=True)
        raise OfflineBoundaryError(f"dependency changed while staging: {relative_path.as_posix()}")
    return before


def _iter_files(root: Path) -> Generator[Path]:
    if not root.is_dir():
        return
    _require_no_reparse_points(root)
    pending = [root]
    while pending:
        current = pending.pop()
        with os.scandir(current) as entries:
            children = sorted(entries, key=lambda entry: entry.name.casefold())
        directories: list[Path] = []
        for entry in children:
            path = Path(entry.path)
            _require_no_reparse_points(path)
            if entry.is_symlink():
                raise OfflineBoundaryError(f"offline dependency contains a symbolic link: {path}")
            if entry.is_dir(follow_symlinks=False):
                directories.append(path)
            elif not entry.is_file(follow_symlinks=False):
                raise OfflineBoundaryError(f"offline dependency contains a special file: {path}")
            elif "__pycache__" not in path.parts and path.suffix != ".pyc":
                _require_single_link_file(path)
                yield path
        pending.extend(reversed(directories))


def _ticker_file(path: Path, ticker: str) -> bool:
    upper = ticker.upper()
    return upper in path.name.upper() or upper in {part.upper() for part in path.parts}


def stage_offline_repository(
    *,
    source_repo: Path,
    isolated_repo: Path,
    database: Path,
    ticker: str,
    portfolio_database: Path | None = None,
) -> tuple[DependencyRecord, ...]:
    """Copy the closed report input set into an isolated repository tree."""

    source_repo = source_repo.resolve()
    isolated_repo = isolated_repo.resolve()
    if _is_within(isolated_repo, source_repo) or _is_within(source_repo, isolated_repo):
        raise OfflineBoundaryError("isolated repository must be outside the source repository")
    ticker = ticker.strip().upper()
    isolated_repo.mkdir(parents=True, exist_ok=False)
    selected: set[Path] = set()

    for path in _iter_files(source_repo / "src"):
        selected.add(path.relative_to(source_repo))
    for path in _iter_files(source_repo / "alembic"):
        selected.add(path.relative_to(source_repo))
    offline_cli = source_repo / "execution" / "build_offline_artifact.py"
    if offline_cli.is_file():
        selected.add(offline_cli.relative_to(source_repo))
    for name in ("alembic.ini", "pyproject.toml", "uv.lock"):
        if (source_repo / name).is_file():
            selected.add(Path(name))
    for directory in ("config", "policies", "directives"):
        for path in _iter_files(source_repo / directory):
            selected.add(path.relative_to(source_repo))

    # The report boundary reads every local FMP/estimate file when composing
    # comparable screens, while all other filesystem inputs are ticker-scoped.
    for directory in ("data/historical/fmp", "data/estimates"):
        for path in _iter_files(source_repo / directory):
            selected.add(path.relative_to(source_repo))
    for directory in (
        "data/bear_case",
        "data/company_description",
        "data/earnings_themes",
        "data/exec_comp",
        "data/filing_intelligence",
        "data/key_metrics",
        "data/peer_selection",
        "data/platform_diagram",
        "data/qa_topics",
        "data/recent_developments",
        "data/report_comments",
        "data/saydo_filter",
        "data/segment_definitions",
        "data/ticker_specific",
        "data/valuation_basis",
        ".tmp",
        "micro_thesis",
        "transcripts",
    ):
        for path in _iter_files(source_repo / directory):
            relative = path.relative_to(source_repo)
            if _ticker_file(relative, ticker):
                selected.add(relative)
    dcf = Path("dcf") / f"{ticker}.xlsx"
    if (source_repo / dcf).is_file():
        selected.add(dcf)

    records = [
        _copy_dependency(source_repo, isolated_repo, path, ticker=ticker)
        for path in sorted(selected)
    ]
    records.extend(snapshot_database(database, isolated_repo / "data" / "portfolio.db"))
    if portfolio_database is not None:
        records.extend(
            snapshot_database(
                portfolio_database,
                isolated_repo.parent / "portfolio-tracker" / "portfolio.db",
                source_logical_path="portfolio/portfolio.db",
                source_class=DependencyClass.PORTFOLIO,
                snapshot_logical_path="isolated/portfolio/portfolio.db",
            )
        )

    runtime_path = isolated_repo / "config" / "offline_runtime.json"
    runtime_path.parent.mkdir(parents=True, exist_ok=True)
    runtime_body = _canonical_json(
        {
            "implementation": sys.implementation.name,
            "python": sys.version.split()[0],
            "sqlite": sqlite3.sqlite_version,
        }
    )
    runtime_path.write_bytes(runtime_body)
    records.append(
        dependency_record(
            runtime_path,
            logical_path="config/offline_runtime.json",
            dependency_class=DependencyClass.RUNTIME,
        )
    )
    return tuple(sorted(records, key=lambda item: (item.dependency_class.value, item.logical_path)))


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


@contextmanager
def offline_runtime_guard(output_dir: Path) -> Generator[OfflineGuardMetrics]:
    """Deny network, subprocess/LLM execution, and writes outside output."""

    metrics = OfflineGuardMetrics()
    output_root = output_dir.resolve()
    real_builtin_open = builtins.open
    real_io_open = io.open
    real_mkdir = Path.mkdir
    builtin_open_call = cast("Callable[..., object]", real_builtin_open)
    io_open_call = cast("Callable[..., object]", real_io_open)
    mkdir_call = cast("Callable[..., object]", real_mkdir)

    def denied_connect(*_args: object, **_kwargs: object) -> None:
        metrics.network_attempts += 1
        raise OfflineBoundaryError("network denied by offline artifact boundary")

    def denied_connect_ex(*_args: object, **_kwargs: object) -> int:
        metrics.network_attempts += 1
        raise OfflineBoundaryError("network denied by offline artifact boundary")

    def denied_subprocess(*args: object, **_kwargs: object) -> None:
        metrics.subprocess_attempts += 1
        command = args[0] if args else None
        command_text = str(command).lower()
        if any(token in command_text for token in ("claude", "gemini", "codex", "llm")):
            metrics.llm_attempts += 1
        raise OfflineBoundaryError("subprocess denied by offline artifact boundary")

    def guarded_open(
        file: str | bytes | os.PathLike[str] | int,
        mode: str = "r",
        *args: object,
        **kwargs: object,
    ) -> object:
        if isinstance(file, int) or not any(flag in mode for flag in ("w", "a", "x", "+")):
            return builtin_open_call(file, mode, *args, **kwargs)
        target = Path(os.fsdecode(file))
        if not _is_within(target, output_root):
            metrics.denied_writes += 1
            metrics.denied_write_paths.append(str(target))
            raise OfflineBoundaryError(f"write outside offline output denied: {target}")
        return builtin_open_call(file, mode, *args, **kwargs)

    def guarded_io_open(
        file: str | bytes | os.PathLike[str] | int,
        mode: str = "r",
        *args: object,
        **kwargs: object,
    ) -> object:
        if isinstance(file, int) or not any(flag in mode for flag in ("w", "a", "x", "+")):
            return io_open_call(file, mode, *args, **kwargs)
        target = Path(os.fsdecode(file))
        if not _is_within(target, output_root):
            metrics.denied_writes += 1
            metrics.denied_write_paths.append(str(target))
            raise OfflineBoundaryError(f"write outside offline output denied: {target}")
        return io_open_call(file, mode, *args, **kwargs)

    def guarded_mkdir(path: Path, *args: object, **kwargs: object) -> None:
        if not _is_within(path, output_root):
            metrics.denied_writes += 1
            metrics.denied_write_paths.append(str(path))
            raise OfflineBoundaryError(f"write outside offline output denied: {path}")
        mkdir_call(path, *args, **kwargs)

    with (
        patch.object(socket.socket, "connect", denied_connect),
        patch.object(socket.socket, "connect_ex", denied_connect_ex),
        patch.object(socket, "create_connection", denied_connect),
        patch.object(subprocess, "run", denied_subprocess),
        patch.object(subprocess, "Popen", denied_subprocess),
        patch.object(builtins, "open", guarded_open),
        patch.object(io, "open", guarded_io_open),
        patch.object(Path, "mkdir", guarded_mkdir),
    ):
        yield metrics


def _normalized_text(value: str) -> str:
    return value.replace("\r\n", "\n").replace("\r", "\n").rstrip() + "\n"


def _canonical_json(value: object) -> bytes:
    return (
        json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False, separators=(",", ": "))
        + "\n"
    ).encode("utf-8")


def _manifest_body(dependencies: Sequence[DependencyRecord]) -> bytes:
    rows = [
        record.model_dump(mode="json")
        for record in sorted(
            dependencies,
            key=lambda item: (item.dependency_class.value, item.logical_path),
        )
    ]
    return _canonical_json(rows)


def _class_digest(
    dependencies: Sequence[DependencyRecord], dependency_class: DependencyClass
) -> str:
    selected = [
        record.model_dump(mode="json")
        for record in sorted(dependencies, key=lambda item: item.logical_path)
        if record.dependency_class is dependency_class
    ]
    return hashlib.sha256(_canonical_json(selected)).hexdigest()


def _artifact_files(
    *,
    ticker: str,
    as_of: date,
    payload: OfflineArtifactPayload,
    dependencies: Sequence[DependencyRecord],
) -> tuple[dict[str, bytes], OfflineArtifactReceipt]:
    deliverables = {
        "report.html": _normalized_text(payload.html).encode("utf-8"),
        "report.md": _normalized_text(payload.markdown).encode("utf-8"),
        "sections.json": _canonical_json(payload.sections),
        "status.json": _canonical_json(payload.status),
        "numeric_provenance.json": _canonical_json(payload.numeric_provenance),
    }
    output_sha256 = {
        name: hashlib.sha256(body).hexdigest() for name, body in sorted(deliverables.items())
    }
    unique_dependencies: dict[str, DependencyRecord] = {}
    for record in dependencies:
        prior = unique_dependencies.get(record.logical_path)
        if prior is not None and prior != record:
            raise OfflineBoundaryError(
                f"conflicting offline dependency identity: {record.logical_path}"
            )
        unique_dependencies[record.logical_path] = record
    ordered_dependencies = tuple(
        sorted(
            unique_dependencies.values(),
            key=lambda item: (item.dependency_class.value, item.logical_path),
        )
    )
    receipt = OfflineArtifactReceipt(
        ticker=ticker,
        as_of=as_of,
        dependency_manifest_sha256=hashlib.sha256(_manifest_body(ordered_dependencies)).hexdigest(),
        code_sha256=_class_digest(ordered_dependencies, DependencyClass.CODE),
        dependencies=ordered_dependencies,
        output_sha256=output_sha256,
    )
    deliverables["receipt.json"] = _canonical_json(receipt.model_dump(mode="json"))
    return deliverables, receipt


@contextmanager
def _timed_artifact_stage(
    stage: str, telemetry: ArtifactTelemetry | None
) -> Generator[None, None, None]:
    started_ns = time.perf_counter_ns()
    if telemetry is not None:
        telemetry(stage, "start", None)
    try:
        yield
    finally:
        if telemetry is not None:
            elapsed_ms = max(0, (time.perf_counter_ns() - started_ns) // 1_000_000)
            telemetry(stage, "complete", elapsed_ms)


def _verify_directory(
    output_dir: Path,
    expected: Mapping[str, bytes],
    *,
    attested_root: Path | None = None,
) -> None:
    _require_no_reparse_points(output_dir, attested_root=attested_root)
    children = tuple(output_dir.iterdir())
    if any(path.is_symlink() or not path.is_file() for path in children):
        raise OfflineBoundaryError("immutable offline artifact differs: inventory mismatch")
    actual_names = {path.name for path in children}
    expected_names = set(expected)
    if actual_names != expected_names:
        raise OfflineBoundaryError("immutable offline artifact differs: file inventory mismatch")
    for name, body in expected.items():
        if (output_dir / name).read_bytes() != body:
            raise OfflineBoundaryError(f"immutable offline artifact differs: {name}")


def verify_artifact_copy(source: Path, destination: Path) -> None:
    """Require two flat immutable artifact directories to be byte-identical."""

    _require_no_reparse_points(source)
    _require_no_reparse_points(destination)
    source_children = tuple(source.iterdir())
    destination_children = tuple(destination.iterdir())
    if any(
        path.is_symlink() or not path.is_file()
        for path in (*source_children, *destination_children)
    ):
        raise OfflineBoundaryError("immutable offline artifact differs: inventory mismatch")
    source_names = {path.name for path in source_children}
    if source_names != {path.name for path in destination_children}:
        raise OfflineBoundaryError("immutable offline artifact differs: inventory mismatch")
    for name in source_names:
        if (source / name).read_bytes() != (destination / name).read_bytes():
            raise OfflineBoundaryError(f"immutable offline artifact differs: {name}")


def write_offline_artifact(
    *,
    output_dir: Path,
    ticker: str,
    as_of: date,
    payload: OfflineArtifactPayload,
    dependencies: Sequence[DependencyRecord],
    telemetry: ArtifactTelemetry | None = None,
    attested_root: Path | None = None,
) -> OfflineArtifactReceipt:
    """Write or verify one deterministic, immutable offline artifact bundle."""

    with _timed_artifact_stage("serialization", telemetry):
        files, receipt = _artifact_files(
            ticker=ticker,
            as_of=as_of,
            payload=payload,
            dependencies=dependencies,
        )
    output_dir = output_dir.resolve()
    if output_dir.exists():
        if not output_dir.is_dir():
            raise OfflineBoundaryError("offline output path exists and is not a directory")
        with _timed_artifact_stage("verification", telemetry):
            _verify_directory(output_dir, files, attested_root=attested_root)
        return receipt

    with _timed_artifact_stage("staging_directory", telemetry):
        output_dir.parent.mkdir(parents=True, exist_ok=True)
        staging = output_dir.with_name(f".{output_dir.name}.{os.getpid():x}.staging")
        try:
            staging.mkdir()
        except FileExistsError:
            raise OfflineBoundaryError("offline artifact staging path already exists") from None
    try:
        with _timed_artifact_stage("writes", telemetry):
            for name, body in files.items():
                (staging / name).write_bytes(body)
        with _timed_artifact_stage("verification", telemetry):
            _verify_directory(staging, files, attested_root=attested_root)
        with _timed_artifact_stage("rename", telemetry):
            staging.replace(output_dir)
    finally:
        with _timed_artifact_stage("cleanup", telemetry):
            if staging.exists():
                shutil.rmtree(staging)
    return receipt


__all__ = [
    "DependencyClass",
    "DependencyRecord",
    "OfflineArtifactPayload",
    "OfflineArtifactReceipt",
    "OfflineBoundaryError",
    "OfflineGuardMetrics",
    "UnclassifiedDependencyError",
    "classify_dependency",
    "dependency_record",
    "offline_runtime_guard",
    "runtime_dependency_records",
    "runtime_native_dependency_records",
    "runtime_tree_dependency_records",
    "snapshot_database",
    "stage_offline_repository",
    "verify_artifact_copy",
    "write_offline_artifact",
]
