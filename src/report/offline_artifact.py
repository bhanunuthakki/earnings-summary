"""Sealed, deterministic output primitives for offline report rendering.

This module owns the narrow boundary shared by the offline CLI and its tests:
dependency classification, a WAL-aware private SQLite snapshot, runtime
capability denial, normalized deliverables, and a write-once receipt.  It does
not call the ordinary artifact builder or mutate canonical repository state.
"""

from __future__ import annotations

import builtins
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
from collections.abc import Callable, Generator, Mapping, Sequence
from contextlib import contextmanager
from datetime import date
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Literal, cast
from unittest.mock import patch

from pydantic import BaseModel, ConfigDict, Field, field_validator


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

    source = source.resolve()
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
        shutil.copyfile(source, private_db)
        source_wal = source.with_name(f"{source.name}-wal")
        if source_wal.is_file():
            shutil.copyfile(source_wal, private_db.with_name(f"{private_db.name}-wal"))
        source_conn = sqlite3.connect(f"{private_db.as_uri()}?mode=ro", uri=True)
        destination_conn = sqlite3.connect(destination)
        try:
            source_conn.backup(destination_conn)
        finally:
            destination_conn.close()
            source_conn.close()
    after = {
        path: dependency_record(
            path,
            logical_path=record.logical_path,
            dependency_class=record.dependency_class,
        )
        for path, record in before.items()
    }
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
    for path in sorted(root.rglob("*")):
        if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc":
            yield path


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
    ordered_dependencies = tuple(
        sorted(dependencies, key=lambda item: (item.dependency_class.value, item.logical_path))
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


def _verify_directory(output_dir: Path, expected: Mapping[str, bytes]) -> None:
    actual_names = {path.name for path in output_dir.iterdir() if path.is_file()}
    expected_names = set(expected)
    if actual_names != expected_names:
        raise OfflineBoundaryError("immutable offline artifact differs: file inventory mismatch")
    for name, body in expected.items():
        if (output_dir / name).read_bytes() != body:
            raise OfflineBoundaryError(f"immutable offline artifact differs: {name}")


def write_offline_artifact(
    *,
    output_dir: Path,
    ticker: str,
    as_of: date,
    payload: OfflineArtifactPayload,
    dependencies: Sequence[DependencyRecord],
) -> OfflineArtifactReceipt:
    """Write or verify one deterministic, immutable offline artifact bundle."""

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
        _verify_directory(output_dir, files)
        return receipt

    output_dir.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{output_dir.name}.", dir=output_dir.parent))
    try:
        for name, body in files.items():
            (staging / name).write_bytes(body)
        _verify_directory(staging, files)
        staging.replace(output_dir)
    finally:
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
    "snapshot_database",
    "stage_offline_repository",
    "write_offline_artifact",
]
