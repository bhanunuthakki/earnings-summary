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
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from sqlite_runtime import SQLiteConnectionRole, connect_sqlite

_SCHEMA_VERSION = "sqlite-reader-snapshot/v1"
_DEFAULT_CODE_CONFIG_VERSION = "sqlite-reader-snapshot/v1"
_BACKUP_PAGE_COUNT = 128
_MAX_PROGRESS_EVENTS = 12

SnapshotLogger = Callable[[str, dict[str, object]], None]


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

    The comparison uses a fresh SQLite backup, so committed WAL pages participate
    even when the main database file's size and mtime have not changed. Neither the
    source nor the published snapshot is mutated.
    """

    if not request.source_path.is_file():
        raise FileNotFoundError(f"source database does not exist: {request.source_path}")
    source_observation = _file_observation(request.source_path)
    wal_before = _optional_file_state(_wal_path(request.source_path))
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
    if wal_before != wal_after or not _same_file_state(source_observation, source_after):
        raise RuntimeError("source WAL changed during snapshot verification; retry")
    return result


def _existing_replay(
    request: SnapshotRequest,
    source: SourceSnapshotObservation,
    source_conn: sqlite3.Connection,
    logger: SnapshotLogger | None,
    *,
    manifest_path: Path | None = None,
) -> SnapshotResult | None:
    destination_path = request.destination_path
    manifest_path = (
        manifest_path.expanduser().resolve()
        if manifest_path is not None
        else _manifest_path(destination_path)
    )
    destination_exists = destination_path.exists()
    manifest_exists = manifest_path.exists()
    if not destination_exists and not manifest_exists:
        return None
    if not destination_exists or not manifest_exists:
        raise SnapshotConflictError(
            f"snapshot destination already exists without a complete manifest: {destination_path}"
        )
    try:
        manifest = SnapshotManifest.model_validate_json(manifest_path.read_text(encoding="utf-8"))
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
    destination_verification = _verify(destination_path)
    _require_clean_verification(destination_verification)
    if manifest.verification != destination_verification:
        raise SnapshotConflictError(
            f"snapshot destination already exists with a different verification record: {destination_path}"
        )
    if manifest.snapshot.byte_size != destination_path.stat().st_size:
        raise SnapshotConflictError(
            f"snapshot destination already exists with a different byte size: {destination_path}"
        )
    if manifest.snapshot.sha256 != _sha256(destination_path):
        raise SnapshotConflictError(
            f"snapshot destination already exists with a different checksum: {destination_path}"
        )
    candidate_path = _temporary_path(destination_path)
    try:
        _backup(source_conn, candidate_path, logger)
        if not _same_file_state(_file_observation(request.source_path), source):
            raise RuntimeError(
                "source database changed during replay validation; retry from a new observation"
            )
        _require_clean_verification(_verify(candidate_path))
        if (
            manifest.snapshot.byte_size != candidate_path.stat().st_size
            or manifest.snapshot.sha256 != _sha256(candidate_path)
            or not _same_source(manifest.source, source)
        ):
            raise SnapshotConflictError(
                f"snapshot destination already exists for different source content: {destination_path}"
            )
    finally:
        if candidate_path.exists():
            candidate_path.unlink()
    return SnapshotResult(
        snapshot_path=str(destination_path),
        manifest_path=manifest_path,
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
        source.backup(destination, pages=_BACKUP_PAGE_COUNT, progress=progress)
    finally:
        destination.close()


def _verify(path: Path) -> SnapshotVerification:
    conn = connect_sqlite(path, role=SQLiteConnectionRole.READ_ONLY)
    try:
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


def _same_source(first: SourceSnapshotObservation, second: SourceSnapshotObservation) -> bool:
    """Match a replay to the same source state, not a later wall-clock read."""
    return _same_file_state(first, second) and first.alembic_revision == second.alembic_revision


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


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
