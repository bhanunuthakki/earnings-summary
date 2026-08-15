"""Dormant, content-addressed staging for authorized transcript bytes.

The primitive copies an already-authorized source exactly once into a caller-owned
private root. Consumers receive verified bytes from the staged snapshot and never
reopen the mutable source. This module has no database, network, or entrypoint
wiring; importing it does not activate transcript acquisition.
"""

from __future__ import annotations

import hashlib
import os
import re
import stat
import tempfile
import time
from pathlib import Path
from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_STRICT_FROZEN = ConfigDict(
    extra="forbid",
    frozen=True,
    strict=True,
    revalidate_instances="always",
)


class TranscriptStagingError(ValueError):
    """The transcript snapshot could not be staged or verified safely."""


class StagedTranscriptArtifact(BaseModel):
    """Immutable identity for bytes held in a private content-addressed root."""

    model_config = _STRICT_FROZEN

    source_path: Path
    staging_root: Path
    staged_path: Path
    sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    size_bytes: Annotated[int, Field(ge=0)]

    @model_validator(mode="after")
    def _validate_paths(self) -> Self:
        if not self.source_path.is_absolute():
            raise ValueError("source path must be absolute")
        if not self.staging_root.is_absolute():
            raise ValueError("staging root must be absolute")
        if not self.staged_path.is_absolute():
            raise ValueError("staged path must be absolute")
        expected = self.staging_root / f"{self.sha256}.transcript"
        if self.staged_path != expected:
            raise ValueError("canonical staged path does not match the content digest")
        return self


def _has_reparse_attribute(metadata: os.stat_result) -> bool:
    attributes = getattr(metadata, "st_file_attributes", 0)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return bool(reparse_flag and attributes & reparse_flag)


def _lstat(path: Path, *, label: str) -> os.stat_result:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise TranscriptStagingError(f"{label} is unavailable") from exc
    if stat.S_ISLNK(metadata.st_mode) or _has_reparse_attribute(metadata):
        raise TranscriptStagingError(f"{label} must not be a symlink or reparse point")
    return metadata


def _require_private_root(private_root: Path) -> Path:
    metadata = _lstat(private_root, label="staging root")
    if not stat.S_ISDIR(metadata.st_mode):
        raise TranscriptStagingError("staging root must be an existing directory")
    resolved = private_root.resolve(strict=True)
    if resolved != private_root.absolute():
        raise TranscriptStagingError("staging root must be a direct canonical path")
    return resolved


def _require_regular_file(path: Path, *, label: str) -> os.stat_result:
    metadata = _lstat(path, label=label)
    if not stat.S_ISREG(metadata.st_mode):
        raise TranscriptStagingError(f"{label} must be a regular file")
    if metadata.st_nlink != 1:
        raise TranscriptStagingError(f"{label} must not have a hard link")
    return metadata


def _require_read_only(metadata: os.stat_result, *, label: str) -> None:
    if metadata.st_mode & stat.S_IWUSR:
        raise TranscriptStagingError(f"{label} must remain read-only")


def _read_source_once(source_path: Path, *, max_bytes: int) -> tuple[Path, bytes]:
    if isinstance(max_bytes, bool) or max_bytes <= 0:
        raise TranscriptStagingError("max_bytes must be a positive integer")
    _require_regular_file(source_path, label="source transcript")
    resolved = source_path.resolve(strict=True)
    if resolved != source_path.absolute():
        raise TranscriptStagingError("source transcript must be a direct canonical path")
    try:
        with source_path.open("rb") as handle:
            payload = handle.read(max_bytes + 1)
    except OSError as exc:
        raise TranscriptStagingError("source transcript could not be read") from exc
    if len(payload) > max_bytes:
        raise TranscriptStagingError("source transcript exceeds the maximum size")
    return resolved, payload


def _write_all(file_descriptor: int, payload: bytes) -> None:
    remaining = memoryview(payload)
    while remaining:
        written = os.write(file_descriptor, remaining)
        if written <= 0:
            raise TranscriptStagingError("staged transcript write made no progress")
        remaining = remaining[written:]
    os.fsync(file_descriptor)


def _read_verified_target(path: Path, *, digest: str, payload: bytes) -> None:
    metadata = _require_regular_file(path, label="content-address collision")
    _require_read_only(metadata, label="content-address collision")
    try:
        with path.open("rb") as handle:
            existing = handle.read(len(payload) + 1)
    except OSError as exc:
        raise TranscriptStagingError("content-address collision could not be read") from exc
    if (
        metadata.st_size != len(payload)
        or existing != payload
        or hashlib.sha256(existing).hexdigest() != digest
    ):
        raise TranscriptStagingError("content-address collision contains different bytes")


def _acquire_digest_lock(lock_path: Path) -> None:
    deadline = time.monotonic() + 2.0
    while True:
        try:
            descriptor = os.open(lock_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            if time.monotonic() >= deadline:
                raise TranscriptStagingError(
                    "content-addressed snapshot lock is unavailable"
                ) from None
            time.sleep(0.01)
            continue
        except OSError as exc:
            raise TranscriptStagingError(
                "content-addressed snapshot lock could not be created"
            ) from exc
        os.close(descriptor)
        _require_regular_file(lock_path, label="content-addressed snapshot lock")
        return


def _commit_snapshot(*, private_root: Path, target: Path, payload: bytes, digest: str) -> None:
    lock_path = private_root / f".{digest}.lock"
    _acquire_digest_lock(lock_path)
    file_descriptor = -1
    temporary_path: Path | None = None
    target_created = False
    try:
        try:
            target.lstat()
        except FileNotFoundError:
            pass
        else:
            _read_verified_target(target, digest=digest, payload=payload)
            return
        file_descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{digest}.", suffix=".tmp", dir=private_root
        )
        temporary_path = Path(temporary_name)
        _write_all(file_descriptor, payload)
        os.close(file_descriptor)
        file_descriptor = -1
        _require_regular_file(temporary_path, label="temporary staged transcript")
        _require_private_root(private_root)
        try:
            os.link(temporary_path, target)
            target_created = True
        except FileExistsError:
            _read_verified_target(target, digest=digest, payload=payload)
            return
        except OSError as exc:
            raise TranscriptStagingError(
                "content-addressed snapshot could not be committed"
            ) from exc
        temporary_path.unlink()
        target.chmod(stat.S_IREAD)
        _require_private_root(private_root)
        metadata = _require_regular_file(target, label="staged transcript")
        _require_read_only(metadata, label="staged transcript")
        if metadata.st_size != len(payload):
            raise TranscriptStagingError("staged transcript size changed during commit")
    except Exception:
        if target_created:
            try:
                target.chmod(stat.S_IWRITE)
                target.unlink()
            except OSError:
                pass
        raise
    finally:
        if file_descriptor >= 0:
            os.close(file_descriptor)
        if temporary_path is not None and temporary_path.exists():
            try:
                temporary_path.chmod(stat.S_IWRITE)
                temporary_path.unlink()
            except OSError:
                pass
        try:
            lock_path.chmod(stat.S_IWRITE)
            lock_path.unlink()
        except OSError:
            pass


def stage_transcript_artifact(
    source_path: Path,
    private_root: Path,
    *,
    max_bytes: int,
    expected_sha256: str | None = None,
) -> StagedTranscriptArtifact:
    """Read a source once and seal its exact bytes inside ``private_root``."""

    if expected_sha256 is not None and not _SHA256_RE.fullmatch(expected_sha256):
        raise TranscriptStagingError("expected SHA-256 must be 64 lowercase hex characters")
    staging_root = _require_private_root(private_root)
    resolved_source, payload = _read_source_once(source_path, max_bytes=max_bytes)
    digest = hashlib.sha256(payload).hexdigest()
    if expected_sha256 is not None and digest != expected_sha256:
        raise TranscriptStagingError("source transcript does not match expected SHA-256")
    target = staging_root / f"{digest}.transcript"
    _commit_snapshot(
        private_root=staging_root,
        target=target,
        payload=payload,
        digest=digest,
    )
    return StagedTranscriptArtifact(
        source_path=resolved_source,
        staging_root=staging_root,
        staged_path=target,
        sha256=digest,
        size_bytes=len(payload),
    )


def read_staged_transcript(artifact: StagedTranscriptArtifact) -> bytes:
    """Return verified snapshot bytes without reopening the original source."""

    try:
        validated = StagedTranscriptArtifact.model_validate(artifact, strict=True)
    except ValidationError as exc:
        raise TranscriptStagingError(str(exc)) from None
    _require_private_root(validated.staging_root)
    expected_path = validated.staging_root / f"{validated.sha256}.transcript"
    if validated.staged_path != expected_path:
        raise TranscriptStagingError("canonical staged path does not match the content digest")
    metadata = _require_regular_file(validated.staged_path, label="staged transcript")
    _require_read_only(metadata, label="staged transcript")
    try:
        with validated.staged_path.open("rb") as handle:
            payload = handle.read(validated.size_bytes + 1)
    except OSError as exc:
        raise TranscriptStagingError("staged transcript could not be read") from exc
    if metadata.st_size != validated.size_bytes or len(payload) != validated.size_bytes:
        raise TranscriptStagingError("staged transcript size does not match its receipt")
    if hashlib.sha256(payload).hexdigest() != validated.sha256:
        raise TranscriptStagingError("staged SHA-256 does not match its receipt")
    return payload


__all__ = [
    "StagedTranscriptArtifact",
    "TranscriptStagingError",
    "read_staged_transcript",
    "stage_transcript_artifact",
]
