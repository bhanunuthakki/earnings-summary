"""Create a verified, transactionally consistent SQLite reader snapshot.

The module deliberately uses SQLite's backup API instead of copying database,
WAL, or shared-memory files.  It therefore makes a stable reader artifact from
a live writer without changing the source database's connection policy.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
from collections.abc import Callable, Generator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from runtime.deterministic import sha256_file as _sha256
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite

_SCHEMA_VERSION = "sqlite-reader-snapshot/v1"
_DEFAULT_CODE_CONFIG_VERSION = "sqlite-reader-snapshot/v1"
_BACKUP_PAGE_COUNT = 4096
_BACKUP_DEST_CACHE_SIZE_KIB = -65536
_VERIFY_CACHE_SIZE_KIB = -262144
_VERIFY_MMAP_SIZE_BYTES = 536870912
_STREAM_CHUNK_BYTES = 4 * 1024 * 1024
_SQLITE_MAGIC = b"SQLite format 3\x00"
_SQLITE_HEADER_BYTES = 100
_MAX_PROGRESS_EVENTS = 12

SnapshotLogger = Callable[[str, dict[str, object]], None]


@dataclass(frozen=True, slots=True)
class _TrustedDestinationVerification:
    path: Path
    verification: SnapshotVerification
    expected_sha256: str | None = None
    file_stat: tuple[int, int] | None = None


_TRUSTED_DESTINATION_VERIFICATION: ContextVar[_TrustedDestinationVerification | None] = ContextVar(
    "sqlite_snapshot_trusted_destination_verification",
    default=None,
)


def apply_verification_pragmas(conn: sqlite3.Connection) -> None:
    """Apply connection-local read tuning for integrity and foreign-key scans."""
    conn.execute(f"PRAGMA cache_size = {_VERIFY_CACHE_SIZE_KIB}")
    conn.execute(f"PRAGMA mmap_size = {_VERIFY_MMAP_SIZE_BYTES}")


@contextmanager
def trusted_destination_verification(
    destination_path: Path,
    *,
    verification: SnapshotVerification,
    expected_sha256: str | None = None,
    file_stat: tuple[int, int] | None = None,
) -> Generator[None]:
    """Scope an already-proved clean destination verification to one call stack.

    The verifier still hashes the snapshot artifact and checks its manifest
    commitment; this context only avoids re-running redundant multi-gigabyte
    ``PRAGMA integrity_check`` and ``PRAGMA foreign_key_check`` scans on the
    exact same immutable snapshot bytes.
    """
    token = _TRUSTED_DESTINATION_VERIFICATION.set(
        _TrustedDestinationVerification(
            path=destination_path.expanduser().resolve(),
            verification=verification,
            expected_sha256=expected_sha256,
            file_stat=file_stat,
        )
    )
    try:
        yield
    finally:
        _TRUSTED_DESTINATION_VERIFICATION.reset(token)


class SnapshotConflictError(ValueError):
    """The requested artifact path is occupied by a different snapshot run."""


class FileObservation(BaseModel):
    """A bounded filesystem observation recorded before the read transaction."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    path: str
    byte_size: int = Field(ge=0)
    mtime_ns: int = Field(ge=0)
    observed_at: datetime


class SourceSnapshotObservation(FileObservation):
    """The source database identity tied to the snapshot transaction."""

    alembic_revision: str = Field(min_length=1)


class SnapshotArtifact(BaseModel):
    """The cryptographic identity of the published reader artifact."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    path: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    byte_size: int = Field(ge=1)


class SnapshotVerification(BaseModel):
    """SQLite integrity outcomes that gate publication."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    integrity_check: tuple[str, ...]
    foreign_key_check: tuple[tuple[str | int | float | None, ...], ...]


class SnapshotManifest(BaseModel):
    """Immutable, strict sidecar contract for one reader snapshot."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = _SCHEMA_VERSION
    created_at: datetime
    code_config_version: str = Field(min_length=1)
    source: SourceSnapshotObservation
    snapshot: SnapshotArtifact
    verification: SnapshotVerification


class SnapshotRequest(BaseModel):
    """Explicit snapshot target and deterministic implementation configuration."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source_path: Path
    destination_path: Path
    code_config_version: str = Field(default=_DEFAULT_CODE_CONFIG_VERSION, min_length=1)

    @field_validator("source_path", "destination_path")
    @classmethod
    def _absolute_path(cls, value: Path) -> Path:
        return value.expanduser().resolve()

    @model_validator(mode="after")
    def _distinct_artifact(self) -> SnapshotRequest:
        if self.source_path == self.destination_path:
            raise ValueError("destination_path must be outside the source database path")
        return self


class SnapshotResult(BaseModel):
    """Closed output contract for the deterministic execution entrypoint."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    snapshot_path: str
    manifest_path: Path
    snapshot_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    snapshot_byte_size: int = Field(ge=1)
    replayed: bool


def create_snapshot(
    request: SnapshotRequest, *, logger: SnapshotLogger | None = None
) -> SnapshotResult:
    """Build or verify-replay one reader snapshot without mutating its source.

    A destination can only be reused when its strict manifest still describes
    this exact source observation and the artifact re-verifies.  Every other
    occupied destination is a conflict rather than an implicit overwrite.
    """
    source_path = request.source_path
    destination_path = request.destination_path
    if not source_path.is_file():
        raise FileNotFoundError(f"source database does not exist: {source_path}")
    destination_path.parent.mkdir(parents=True, exist_ok=True)

    _emit(
        logger,
        "sqlite_snapshot_started",
        source_path=source_path,
        destination_path=destination_path,
    )
    source_observation = _file_observation(source_path)
    source_conn = connect_sqlite(source_path, role=SQLiteConnectionRole.READ_ONLY)
    try:
        source_conn.execute("BEGIN")
        revision = _alembic_revision(source_conn)
        source = SourceSnapshotObservation(
            **source_observation.model_dump(), alembic_revision=revision
        )
        existing = _existing_replay(request, source, source_conn, logger)
        if existing is not None:
            _emit(logger, "sqlite_snapshot_replayed", snapshot_path=destination_path)
            return existing

        temporary_path = _temporary_path(destination_path)
        try:
            _backup(source_conn, temporary_path, logger)
            if not _same_file_state(_file_observation(source_path), source_observation):
                raise RuntimeError(
                    "source database changed during snapshot; retry from a new observation"
                )
            verification = _verify(temporary_path)
            _require_clean_verification(verification)
            snapshot = SnapshotArtifact(
                path=str(destination_path),
                sha256=_sha256(temporary_path),
                byte_size=temporary_path.stat().st_size,
            )
            manifest = SnapshotManifest(
                created_at=datetime.now(UTC),
                code_config_version=request.code_config_version,
                source=source,
                snapshot=snapshot,
                verification=verification,
            )
            _publish_snapshot(temporary_path, destination_path)
            _write_manifest_atomically(_manifest_path(destination_path), manifest)
        finally:
            if temporary_path.exists():
                temporary_path.unlink()
    finally:
        if source_conn.in_transaction:
            source_conn.rollback()
        source_conn.close()

    _emit(
        logger, "sqlite_snapshot_finished", snapshot_path=destination_path, sha256=snapshot.sha256
    )
    return SnapshotResult(
        snapshot_path=str(destination_path),
        manifest_path=_manifest_path(destination_path),
        snapshot_sha256=snapshot.sha256,
        snapshot_byte_size=snapshot.byte_size,
        replayed=False,
    )


def verify_snapshot_matches_source(
    request: SnapshotRequest,
    *,
    manifest_path: Path | None = None,
) -> SnapshotResult:
    """Cryptographically re-prove an existing snapshot against current source content.

    When the source has no non-empty WAL or rollback journal sidecar, the
    verifier proves byte-for-byte identity against the published snapshot in a
    single lockstep streaming pass (ignoring only the 12 SQLite header bytes
    rewritten by ``sqlite3_backup_step()`` on destination commit). When active
    WAL or journal frames are present, it falls back to a fresh SQLite backup
    so committed WAL pages participate. Neither the source nor the published
    snapshot is mutated.
    """

    if not request.source_path.is_file():
        raise FileNotFoundError(f"source database does not exist: {request.source_path}")
    wal_before = _optional_file_state(_wal_path(request.source_path))
    journal_before = _optional_file_state(_journal_path(request.source_path))
    if wal_before is None and journal_before is None:
        return _verify_quiesced_snapshot_matches_source(
            request,
            manifest_path=manifest_path,
            wal_before=wal_before,
            journal_before=journal_before,
        )

    source_before_hash = _file_observation(request.source_path)
    source_sha256_before = _sha256(request.source_path)
    source_observation = _file_observation(request.source_path)
    wal_after_initial_hash = _optional_file_state(_wal_path(request.source_path))
    if wal_before != wal_after_initial_hash or not _same_file_state(
        source_before_hash, source_observation
    ):
        raise RuntimeError("source changed while establishing verification identity; retry")
    source_conn = connect_sqlite(request.source_path, role=SQLiteConnectionRole.READ_ONLY)
    try:
        source_conn.execute("BEGIN")
        source = SourceSnapshotObservation(
            **source_observation.model_dump(),
            alembic_revision=_alembic_revision(source_conn),
        )
        result = _existing_replay(
            request,
            source,
            source_conn,
            None,
            manifest_path=manifest_path,
            allow_source_mtime_drift=True,
            force_backup_comparison=True,
        )
        if result is None:
            raise SnapshotConflictError(
                f"snapshot destination does not exist: {request.destination_path}"
            )
    finally:
        if source_conn.in_transaction:
            source_conn.rollback()
        source_conn.close()
    # A read-only SQLite connection may create its own transient, zero-byte WAL
    # sidecar when it opens a WAL-mode database whose sidecars were absent.  Do
    # not classify that verifier-owned file as a concurrent writer.  Observe the
    # WAL only after the verifier connection has closed, then re-observe the main
    # file.  The WAL observation is the proof's linearization point: a committed
    # WAL change before it remains visible, while a checkpoint completed before
    # it advances the subsequent main-file observation.
    wal_after = _optional_file_state(_wal_path(request.source_path))
    source_after = _file_observation(request.source_path)
    source_sha256_after = _sha256(request.source_path)
    source_final = _file_observation(request.source_path)
    wal_final = _optional_file_state(_wal_path(request.source_path))
    if (
        wal_after_initial_hash != wal_after
        or wal_after != wal_final
        or not _same_file_state(source_after, source_final)
        or not _same_file_content_identity(source_observation, source_after)
        or source_sha256_before != source_sha256_after
    ):
        raise RuntimeError("source content or WAL changed during snapshot verification; retry")
    return result


def _verify_quiesced_snapshot_matches_source(
    request: SnapshotRequest,
    *,
    manifest_path: Path | None,
    wal_before: tuple[int, int] | None,
    journal_before: tuple[int, int] | None,
) -> SnapshotResult:
    source_before = _file_observation(request.source_path)
    source_observation = _file_observation(request.source_path)
    wal_after_initial_hash = _optional_file_state(_wal_path(request.source_path))
    journal_after_initial = _optional_file_state(_journal_path(request.source_path))
    if (
        wal_before != wal_after_initial_hash
        or journal_before != journal_after_initial
        or not _same_file_state(source_before, source_observation)
    ):
        raise RuntimeError("source changed while establishing verification identity; retry")

    source_conn = connect_sqlite(request.source_path, role=SQLiteConnectionRole.READ_ONLY)
    try:
        source_conn.execute("BEGIN")
        source = SourceSnapshotObservation(
            **source_observation.model_dump(),
            alembic_revision=_alembic_revision(source_conn),
        )
    finally:
        if source_conn.in_transaction:
            source_conn.rollback()
        source_conn.close()

    loaded = _load_manifest_for_replay(request, manifest_path=manifest_path)
    if loaded is None:
        raise SnapshotConflictError(
            f"snapshot destination does not exist: {request.destination_path}"
        )
    resolved_manifest_path, manifest = loaded
    destination_path = request.destination_path
    _verify_or_trust_destination(destination_path, manifest=manifest)
    dest_before = _file_observation(destination_path)
    if manifest.snapshot.byte_size != dest_before.byte_size:
        raise SnapshotConflictError(
            f"snapshot destination already exists with a different byte size: {destination_path}"
        )
    if not _same_source_content_identity(manifest.source, source):
        raise SnapshotConflictError(
            f"snapshot destination already exists for different source content: {destination_path}"
        )

    wal_after = _optional_file_state(_wal_path(request.source_path))
    journal_after = _optional_file_state(_journal_path(request.source_path))
    source_after = _file_observation(request.source_path)
    if (
        wal_after_initial_hash != wal_after
        or journal_after is not None
        or not _same_file_content_identity(source_observation, source_after)
    ):
        raise RuntimeError("source content or WAL changed during snapshot verification; retry")

    bytes_match, destination_sha256 = _compare_quiesced_source_and_hash_destination(
        request.source_path,
        destination_path,
    )
    if manifest.snapshot.sha256 != destination_sha256:
        raise SnapshotConflictError(
            f"snapshot destination already exists with a different checksum: {destination_path}"
        )
    if not bytes_match:
        raise RuntimeError("source content or WAL changed during snapshot verification; retry")

    dest_final = _file_observation(destination_path)
    source_final = _file_observation(request.source_path)
    wal_final = _optional_file_state(_wal_path(request.source_path))
    journal_final = _optional_file_state(_journal_path(request.source_path))
    if (
        wal_after != wal_final
        or journal_final is not None
        or not _same_file_state(source_after, source_final)
        or not _same_file_state(dest_before, dest_final)
    ):
        raise RuntimeError("source content or WAL changed during snapshot verification; retry")
    return SnapshotResult(
        snapshot_path=str(destination_path),
        manifest_path=resolved_manifest_path,
        snapshot_sha256=manifest.snapshot.sha256,
        snapshot_byte_size=manifest.snapshot.byte_size,
        replayed=True,
    )


def _load_manifest_for_replay(
    request: SnapshotRequest,
    *,
    manifest_path: Path | None = None,
) -> tuple[Path, SnapshotManifest] | None:
    destination_path = request.destination_path
    resolved_manifest = (
        manifest_path.expanduser().resolve()
        if manifest_path is not None
        else _manifest_path(destination_path)
    )
    destination_exists = destination_path.exists()
    manifest_exists = resolved_manifest.exists()
    if not destination_exists and not manifest_exists:
        return None
    if not destination_exists or not manifest_exists:
        raise SnapshotConflictError(
            f"snapshot destination already exists without a complete manifest: {destination_path}"
        )
    try:
        manifest = SnapshotManifest.model_validate_json(
            resolved_manifest.read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        raise SnapshotConflictError(
            f"snapshot destination already exists with an invalid manifest: {destination_path}"
        ) from exc
    if (
        manifest.schema_version != _SCHEMA_VERSION
        or manifest.code_config_version != request.code_config_version
        or manifest.snapshot.path != str(destination_path)
    ):
        raise SnapshotConflictError(
            f"snapshot destination already exists for a different input: {destination_path}"
        )
    return resolved_manifest, manifest


def _trusted_verification_for(
    destination_path: Path,
    *,
    manifest: SnapshotManifest,
) -> SnapshotVerification | None:
    trusted = _TRUSTED_DESTINATION_VERIFICATION.get()
    if trusted is None or trusted.path != destination_path:
        return None
    if trusted.verification.integrity_check != ("ok",) or trusted.verification.foreign_key_check:
        return None
    if trusted.verification != manifest.verification:
        return None
    if trusted.expected_sha256 is not None and trusted.expected_sha256 != manifest.snapshot.sha256:
        return None
    if trusted.file_stat is not None:
        try:
            stat = destination_path.stat()
        except OSError:
            return None
        if (stat.st_size, stat.st_mtime_ns) != trusted.file_stat:
            return None
    return trusted.verification


def _verify_or_trust_destination(
    destination_path: Path,
    *,
    manifest: SnapshotManifest,
) -> None:
    destination_verification = _trusted_verification_for(destination_path, manifest=manifest)
    if destination_verification is None:
        destination_verification = _verify(destination_path)
    _require_clean_verification(destination_verification)
    if manifest.verification != destination_verification:
        raise SnapshotConflictError(
            f"snapshot destination already exists with a different verification record: {destination_path}"
        )


def _existing_replay(
    request: SnapshotRequest,
    source: SourceSnapshotObservation,
    source_conn: sqlite3.Connection,
    logger: SnapshotLogger | None,
    *,
    manifest_path: Path | None = None,
    allow_source_mtime_drift: bool = False,
    force_backup_comparison: bool = False,
) -> SnapshotResult | None:
    loaded = _load_manifest_for_replay(request, manifest_path=manifest_path)
    if loaded is None:
        return None
    resolved_manifest_path, manifest = loaded
    destination_path = request.destination_path
    _verify_or_trust_destination(destination_path, manifest=manifest)
    if manifest.snapshot.byte_size != destination_path.stat().st_size:
        raise SnapshotConflictError(
            f"snapshot destination already exists with a different byte size: {destination_path}"
        )
    source_matches = _same_source(manifest.source, source) or (
        allow_source_mtime_drift and _same_source_content_identity(manifest.source, source)
    )
    wal_before = _optional_file_state(_wal_path(request.source_path))
    journal_before = _optional_file_state(_journal_path(request.source_path))
    if not force_backup_comparison and wal_before is None and journal_before is None:
        dest_before = _file_observation(destination_path)
        bytes_match, destination_sha256 = _compare_quiesced_source_and_hash_destination(
            request.source_path,
            destination_path,
        )
        if manifest.snapshot.sha256 != destination_sha256:
            raise SnapshotConflictError(
                f"snapshot destination already exists with a different checksum: {destination_path}"
            )
        source_after_compare = _file_observation(request.source_path)
        wal_after = _optional_file_state(_wal_path(request.source_path))
        journal_after = _optional_file_state(_journal_path(request.source_path))
        dest_after = _file_observation(destination_path)
        if (
            wal_after is not None
            or journal_after is not None
            or not _same_file_state(dest_before, dest_after)
            or (
                not _same_file_state(source_after_compare, source)
                and not (
                    allow_source_mtime_drift
                    and _same_file_content_identity(source_after_compare, source)
                )
            )
        ):
            raise RuntimeError(
                "source database changed during replay validation; retry from a new observation"
            )
        if not bytes_match or not source_matches:
            raise SnapshotConflictError(
                f"snapshot destination already exists for different source content: {destination_path}"
            )
        return SnapshotResult(
            snapshot_path=str(destination_path),
            manifest_path=resolved_manifest_path,
            snapshot_sha256=manifest.snapshot.sha256,
            snapshot_byte_size=manifest.snapshot.byte_size,
            replayed=True,
        )

    if manifest.snapshot.sha256 != _sha256(destination_path):
        raise SnapshotConflictError(
            f"snapshot destination already exists with a different checksum: {destination_path}"
        )
    candidate_path = _temporary_path(destination_path)
    try:
        _backup(source_conn, candidate_path, logger)
        source_after_backup = _file_observation(request.source_path)
        if not _same_file_state(source_after_backup, source) and not (
            allow_source_mtime_drift and _same_file_content_identity(source_after_backup, source)
        ):
            raise RuntimeError(
                "source database changed during replay validation; retry from a new observation"
            )
        _require_clean_verification(_verify(candidate_path))
        if (
            manifest.snapshot.byte_size != candidate_path.stat().st_size
            or manifest.snapshot.sha256 != _sha256(candidate_path)
            or not source_matches
        ):
            raise SnapshotConflictError(
                f"snapshot destination already exists for different source content: {destination_path}"
            )
    finally:
        if candidate_path.exists():
            candidate_path.unlink()
    return SnapshotResult(
        snapshot_path=str(destination_path),
        manifest_path=resolved_manifest_path,
        snapshot_sha256=manifest.snapshot.sha256,
        snapshot_byte_size=manifest.snapshot.byte_size,
        replayed=True,
    )


def _alembic_revision(conn: sqlite3.Connection) -> str:
    rows = conn.execute("SELECT version_num FROM alembic_version ORDER BY version_num").fetchall()
    if len(rows) != 1 or not isinstance(rows[0][0], str) or not rows[0][0]:
        raise ValueError("source database must contain exactly one non-empty alembic revision")
    return rows[0][0]


def _backup(
    source: sqlite3.Connection, temporary_path: Path, logger: SnapshotLogger | None
) -> None:
    destination = connect_sqlite(temporary_path, role=SQLiteConnectionRole.SNAPSHOT_DESTINATION)
    event_count = 0

    def progress(status: int, remaining: int, total: int) -> None:
        nonlocal event_count
        if event_count >= _MAX_PROGRESS_EVENTS:
            return
        if remaining == 0 or event_count == 0 or event_count % 4 == 0:
            _emit(
                logger,
                "sqlite_snapshot_backup_progress",
                status=status,
                remaining_pages=remaining,
                total_pages=total,
            )
        event_count += 1

    try:
        destination.execute(f"PRAGMA cache_size = {_BACKUP_DEST_CACHE_SIZE_KIB}")
        source.backup(destination, pages=_BACKUP_PAGE_COUNT, progress=progress)
    finally:
        destination.close()


def _verify(path: Path) -> SnapshotVerification:
    conn = connect_sqlite(path, role=SQLiteConnectionRole.READ_ONLY)
    try:
        apply_verification_pragmas(conn)
        integrity_rows = conn.execute("PRAGMA integrity_check").fetchall()
        foreign_key_rows = conn.execute("PRAGMA foreign_key_check").fetchall()
    finally:
        conn.close()
    return SnapshotVerification(
        integrity_check=tuple(str(row[0]) for row in integrity_rows),
        foreign_key_check=tuple(tuple(value for value in row) for row in foreign_key_rows),
    )


def _require_clean_verification(verification: SnapshotVerification) -> None:
    if verification.integrity_check != ("ok",):
        raise ValueError(f"snapshot integrity_check failed: {verification.integrity_check}")
    if verification.foreign_key_check:
        raise ValueError(f"snapshot foreign_key_check failed: {verification.foreign_key_check}")


def _file_observation(path: Path) -> FileObservation:
    stat = path.stat()
    return FileObservation(
        path=str(path),
        byte_size=stat.st_size,
        mtime_ns=stat.st_mtime_ns,
        observed_at=datetime.now(UTC),
    )


def _wal_path(source_path: Path) -> Path:
    return source_path.with_name(source_path.name + "-wal")


def _journal_path(source_path: Path) -> Path:
    return source_path.with_name(source_path.name + "-journal")


def _headers_match_ignoring_backup_counters(
    source_header: bytes, destination_header: bytes
) -> bool:
    """Compare 100-byte SQLite headers excluding sqlite3_backup_step() counters.

    SQLite's backup API copies every database page verbatim, then updates only
    the destination's file change counter (bytes 24..28), schema cookie
    (bytes 40..44, incremented on destination commit via
    ``sqlite3BtreeUpdateMeta`` so cached statements invalidate),
    version-valid-for number (bytes 92..96), and SQLite version number
    (bytes 96..100). Every other header field — including magic, page size,
    format versions, page count, freelist, schema format, encoding,
    ``user_version`` (60..64), and ``application_id`` (68..72) — plus the
    entire ``sqlite_schema`` B-tree and all data pages (bytes 100..EOF) must
    match bit-for-bit.
    """
    if len(source_header) < _SQLITE_HEADER_BYTES or len(destination_header) < _SQLITE_HEADER_BYTES:
        return False
    if source_header[:16] != _SQLITE_MAGIC or destination_header[:16] != _SQLITE_MAGIC:
        return False
    return (
        source_header[:24] == destination_header[:24]
        and source_header[28:40] == destination_header[28:40]
        and source_header[44:92] == destination_header[44:92]
    )


def _read_exact(handle: BinaryIO, size: int) -> bytes:
    chunk = handle.read(size)
    if not chunk or len(chunk) == size:
        return chunk
    parts = [chunk]
    remaining = size - len(chunk)
    while remaining > 0:
        more = handle.read(remaining)
        if not more:
            break
        parts.append(more)
        remaining -= len(more)
    return b"".join(parts)


def _compare_quiesced_source_and_hash_destination(
    source_path: Path, destination_path: Path
) -> tuple[bool, str]:
    """Stream source and snapshot in lockstep while computing snapshot SHA-256."""
    digest = hashlib.sha256()
    matches = True
    first_chunk = True
    with source_path.open("rb") as source_handle, destination_path.open("rb") as dest_handle:
        while True:
            dest_chunk = _read_exact(dest_handle, _STREAM_CHUNK_BYTES)
            if not dest_chunk:
                if matches and source_handle.read(1):
                    matches = False
                break
            digest.update(dest_chunk)
            if not matches:
                continue
            source_chunk = _read_exact(source_handle, len(dest_chunk))
            if len(source_chunk) != len(dest_chunk):
                matches = False
                continue
            if first_chunk:
                first_chunk = False
                if (
                    len(dest_chunk) < _SQLITE_HEADER_BYTES
                    or not _headers_match_ignoring_backup_counters(
                        source_chunk[:_SQLITE_HEADER_BYTES],
                        dest_chunk[:_SQLITE_HEADER_BYTES],
                    )
                    or source_chunk[_SQLITE_HEADER_BYTES:] != dest_chunk[_SQLITE_HEADER_BYTES:]
                ):
                    matches = False
            elif source_chunk != dest_chunk:
                matches = False
    if first_chunk:
        matches = False
    return matches, digest.hexdigest()


def _optional_file_state(path: Path) -> tuple[int, int] | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    # A zero-byte WAL contains no committed frames.  The managed Windows
    # SQLite reader may leave that empty sidecar behind after a read-only
    # connection closes, so its creation timestamp is not source-content
    # identity.  Non-empty WALs remain exact: an append before this observation
    # is visible here, while a checkpoint/truncate before it must advance the
    # main-file observation taken immediately afterwards.
    if stat.st_size == 0:
        return None
    return stat.st_size, stat.st_mtime_ns


def _same_file_state(first: FileObservation, second: FileObservation) -> bool:
    """Compare stable filesystem identity without conflating observation time."""
    return (
        first.path == second.path
        and first.byte_size == second.byte_size
        and first.mtime_ns == second.mtime_ns
    )


def _same_file_content_identity(first: FileObservation, second: FileObservation) -> bool:
    """Compare the stable path and size while a separate digest proves bytes."""
    return first.path == second.path and first.byte_size == second.byte_size


def _same_source(first: SourceSnapshotObservation, second: SourceSnapshotObservation) -> bool:
    """Match a replay to the same source state, not a later wall-clock read."""
    return _same_file_state(first, second) and first.alembic_revision == second.alembic_revision


def _same_source_content_identity(
    first: SourceSnapshotObservation, second: SourceSnapshotObservation
) -> bool:
    """Match source identity while allowing verifier-owned mtime-only drift."""
    return (
        _same_file_content_identity(first, second)
        and first.alembic_revision == second.alembic_revision
    )


def _temporary_path(destination_path: Path) -> Path:
    descriptor, raw_path = tempfile.mkstemp(
        prefix=f".{destination_path.name}.", suffix=".tmp", dir=destination_path.parent
    )
    os.close(descriptor)
    path = Path(raw_path)
    path.unlink()
    return path


def _publish_snapshot(temporary_path: Path, destination_path: Path) -> None:
    try:
        os.link(temporary_path, destination_path)
    except FileExistsError as exc:
        raise SnapshotConflictError(
            f"snapshot destination already exists: {destination_path}"
        ) from exc
    temporary_path.unlink()


def _manifest_path(destination_path: Path) -> Path:
    return destination_path.with_suffix(destination_path.suffix + ".manifest.json")


def _write_manifest_atomically(path: Path, manifest: SnapshotManifest) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(
                json.dumps(manifest.model_dump(mode="json"), allow_nan=False, sort_keys=True)
            )
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()


def _emit(logger: SnapshotLogger | None, event: str, **fields: object) -> None:
    if logger is not None:
        logger(event, fields)
