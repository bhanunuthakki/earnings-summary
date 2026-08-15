"""Dormant, handle-verified staging for already-authorized transcript bytes.

The caller must independently supply the exact source digest and byte length.
No database, network, persistence, or acquisition entrypoint imports this module.
"""

from __future__ import annotations

import ctypes
import hashlib
import os
import re
import stat
import tempfile
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

if os.name == "nt":
    import msvcrt

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_WRITE_MODE_MASK = stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH
_STRICT_FROZEN = ConfigDict(
    extra="forbid",
    frozen=True,
    strict=True,
    revalidate_instances="always",
)


class TranscriptStagingError(ValueError):
    """The transcript snapshot could not be staged or verified safely."""


class StagedTranscriptArtifact(BaseModel):
    """Sealed path, content, source, and root identity for one staged snapshot."""

    model_config = _STRICT_FROZEN

    source_path: Path
    source_device: Annotated[int, Field(ge=0)]
    source_inode: Annotated[int, Field(ge=0)]
    staging_root: Path
    staging_root_device: Annotated[int, Field(ge=0)]
    staging_root_inode: Annotated[int, Field(ge=0)]
    staged_path: Path
    sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    size_bytes: Annotated[int, Field(ge=0)]

    @model_validator(mode="after")
    def _validate_paths(self) -> Self:
        for label, path in (
            ("source", self.source_path),
            ("staging root", self.staging_root),
            ("staged", self.staged_path),
        ):
            if not path.is_absolute():
                raise ValueError(f"{label} path must be absolute")
        expected = self.staging_root / f"{self.sha256}.transcript"
        if self.staged_path != expected:
            raise ValueError("canonical staged path does not match the content digest")
        return self


@dataclass(frozen=True)
class _OpenedRoot:
    path: Path
    descriptor: int
    metadata: os.stat_result


def _has_reparse_attribute(metadata: os.stat_result) -> bool:
    attributes = int(getattr(metadata, "st_file_attributes", 0))
    reparse_flag = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    return bool(attributes & reparse_flag)


def _lstat(path: Path, *, label: str) -> os.stat_result:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise TranscriptStagingError(f"{label} is unavailable") from exc
    if stat.S_ISLNK(metadata.st_mode) or _has_reparse_attribute(metadata):
        raise TranscriptStagingError(f"{label} must not be a symlink or reparse point")
    return metadata


def _object_identity(metadata: os.stat_result) -> tuple[int, int]:
    return int(metadata.st_dev), int(metadata.st_ino)


def _stable_identity(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        int(metadata.st_dev),
        int(metadata.st_ino),
        int(metadata.st_mode),
        int(metadata.st_nlink),
        int(metadata.st_size),
        int(metadata.st_mtime_ns),
        int(metadata.st_ctime_ns),
        int(getattr(metadata, "st_file_attributes", 0)),
    )


def _canonical_direct(path: Path, *, label: str) -> Path:
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise TranscriptStagingError(f"{label} is unavailable") from exc
    if resolved != path.absolute():
        raise TranscriptStagingError(f"{label} must be a direct canonical path")
    return resolved


def _windows_open_no_follow(path: Path, *, directory: bool) -> int:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = (
        ctypes.c_wchar_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
    )
    create_file.restype = ctypes.c_void_p
    flags = 0x00200000 | (0x02000000 if directory else 0)  # OPEN_REPARSE_POINT | BACKUP
    sharing = 0x00000001 | (0x00000002 if directory else 0)  # READ | directory WRITE
    raw_handle = create_file(str(path), 0x80000000, sharing, None, 3, flags, None)
    invalid_handle = ctypes.c_void_p(-1).value
    if raw_handle in (None, invalid_handle):
        error = ctypes.get_last_error()
        raise OSError(error, os.strerror(error), str(path))
    handle_value = int(raw_handle)
    try:
        return msvcrt.open_osfhandle(handle_value, os.O_RDONLY | getattr(os, "O_BINARY", 0))
    except Exception:
        kernel32.CloseHandle(ctypes.c_void_p(handle_value))
        raise


def _open_no_follow(path: Path, *, directory: bool = False) -> int:
    try:
        if os.name == "nt":
            return _windows_open_no_follow(path, directory=directory)
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        if directory:
            flags |= getattr(os, "O_DIRECTORY", 0)
        return os.open(path, flags)
    except OSError as exc:
        raise TranscriptStagingError("path could not be opened without following links") from exc


def _require_regular(metadata: os.stat_result, *, label: str, read_only: bool) -> None:
    if not stat.S_ISREG(metadata.st_mode):
        raise TranscriptStagingError(f"{label} must be a regular file")
    if _has_reparse_attribute(metadata):
        raise TranscriptStagingError(f"{label} must not be a symlink or reparse point")
    if int(metadata.st_nlink) != 1:
        raise TranscriptStagingError(f"{label} must not have a hard link")
    if read_only and metadata.st_mode & _WRITE_MODE_MASK:
        raise TranscriptStagingError(f"{label} must remain read-only")


@contextmanager
def _open_root(path: Path) -> Generator[_OpenedRoot]:
    before = _lstat(path, label="staging root")
    if not stat.S_ISDIR(before.st_mode):
        raise TranscriptStagingError("staging root must be an existing directory")
    canonical = _canonical_direct(path, label="staging root")
    descriptor = _open_no_follow(canonical, directory=True)
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISDIR(opened.st_mode) or _has_reparse_attribute(opened):
            raise TranscriptStagingError("staging root handle is not a direct directory")
        if _object_identity(before) != _object_identity(opened):
            raise TranscriptStagingError("staging root identity changed while opening")
        yield _OpenedRoot(canonical, descriptor, opened)
        after = os.fstat(descriptor)
        current = _lstat(canonical, label="staging root")
        if (
            _object_identity(opened) != _object_identity(after)
            or _object_identity(after) != _object_identity(current)
            or not stat.S_ISDIR(after.st_mode)
            or not stat.S_ISDIR(current.st_mode)
            or _has_reparse_attribute(after)
            or _has_reparse_attribute(current)
        ):
            raise TranscriptStagingError("staging root identity changed while in use")
    finally:
        os.close(descriptor)


def _read_handle_bound(
    path: Path,
    *,
    label: str,
    expected_size: int,
    max_bytes: int,
    read_only: bool,
) -> tuple[bytes, os.stat_result]:
    before = _lstat(path, label=label)
    _require_regular(before, label=label, read_only=read_only)
    descriptor = _open_no_follow(path)
    try:
        opened = os.fstat(descriptor)
        _require_regular(opened, label=label, read_only=read_only)
        if _object_identity(before) != _object_identity(opened):
            raise TranscriptStagingError(f"{label} identity changed while opening")
        chunks: list[bytes] = []
        remaining = min(max_bytes, expected_size) + 1
        while remaining > 0:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        after = os.fstat(descriptor)
        _require_regular(after, label=label, read_only=read_only)
        if _stable_identity(opened) != _stable_identity(after):
            raise TranscriptStagingError(f"{label} identity changed during read")
        current = _lstat(path, label=label)
        if _object_identity(after) != _object_identity(current):
            raise TranscriptStagingError(f"{label} identity changed after read")
        return payload, after
    finally:
        os.close(descriptor)


def _write_all(descriptor: int, payload: bytes) -> None:
    remaining = memoryview(payload)
    while remaining:
        written = os.write(descriptor, remaining)
        if written <= 0:
            raise TranscriptStagingError("staged transcript write made no progress")
        remaining = remaining[written:]
    os.fsync(descriptor)


def _atomic_install_no_replace(temporary: Path, target: Path) -> None:
    if os.name == "nt":
        os.rename(temporary, target)
        return
    os.link(temporary, target)
    temporary.unlink()


def _cleanup_owned_temporary(*, root: _OpenedRoot, path: Path, identity: tuple[int, int]) -> None:
    if path.parent != root.path or not path.name.endswith(".tmp"):
        return
    try:
        observed = _lstat(path, label="owned temporary")
        _require_regular(observed, label="owned temporary", read_only=False)
        if _object_identity(observed) != identity:
            return
        path.chmod(stat.S_IWRITE)
        descriptor = _open_no_follow(path)
        try:
            opened = os.fstat(descriptor)
            _require_regular(opened, label="owned temporary", read_only=False)
            if _object_identity(opened) != identity:
                return
            current = _lstat(path, label="owned temporary")
            _require_regular(current, label="owned temporary", read_only=False)
            if _object_identity(opened) != _object_identity(current):
                return
        finally:
            os.close(descriptor)
        if _object_identity(_lstat(path, label="owned temporary")) != identity:
            return
        path.unlink()
    except (OSError, TranscriptStagingError):
        return


def _read_verified_target(
    path: Path,
    *,
    digest: str,
    expected_size: int,
    label: str,
) -> bytes:
    payload, metadata = _read_handle_bound(
        path,
        label=label,
        expected_size=expected_size,
        max_bytes=expected_size,
        read_only=True,
    )
    if (
        int(metadata.st_size) != expected_size
        or len(payload) != expected_size
        or hashlib.sha256(payload).hexdigest() != digest
    ):
        mismatch_label = "staged" if label == "staged transcript" else label
        raise TranscriptStagingError(f"{mismatch_label} SHA-256 or byte length does not match")
    return payload


def _commit_snapshot(*, root: _OpenedRoot, target: Path, payload: bytes, digest: str) -> None:
    try:
        target.lstat()
    except FileNotFoundError:
        pass
    else:
        _read_verified_target(
            target,
            digest=digest,
            expected_size=len(payload),
            label="content-address collision",
        )
        return
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{digest}.", suffix=".tmp", dir=root.path
    )
    temporary = Path(temporary_name)
    temporary_identity: tuple[int, int] | None = None
    try:
        _write_all(descriptor, payload)
        temporary_identity = _object_identity(os.fstat(descriptor))
        os.close(descriptor)
        descriptor = -1
        temporary.chmod(stat.S_IREAD)
        try:
            _atomic_install_no_replace(temporary, target)
        except FileExistsError:
            _read_verified_target(
                target,
                digest=digest,
                expected_size=len(payload),
                label="content-address collision",
            )
            return
        except OSError as exc:
            raise TranscriptStagingError(
                "content-addressed snapshot could not be committed"
            ) from exc
        _read_verified_target(
            target,
            digest=digest,
            expected_size=len(payload),
            label="content-address collision",
        )
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary_identity is not None:
            _cleanup_owned_temporary(
                root=root,
                path=temporary,
                identity=temporary_identity,
            )


def _validate_expected(*, sha256: object, size_bytes: object, max_bytes: object) -> None:
    if not isinstance(sha256, str) or not _SHA256_RE.fullmatch(sha256):
        raise TranscriptStagingError("expected SHA-256 must be 64 lowercase hex characters")
    if isinstance(size_bytes, bool) or not isinstance(size_bytes, int) or size_bytes < 0:
        raise TranscriptStagingError("expected byte length must be a non-negative integer")
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes <= 0:
        raise TranscriptStagingError("max_bytes must be a positive integer")
    if size_bytes > max_bytes:
        raise TranscriptStagingError("source transcript exceeds the maximum size")


def _validate_expected_identity(*, device: object, inode: object, label: str) -> None:
    if (
        isinstance(device, bool)
        or not isinstance(device, int)
        or device < 0
        or isinstance(inode, bool)
        or not isinstance(inode, int)
        or inode < 0
    ):
        raise TranscriptStagingError(f"{label} must be a non-negative integer identity")


def _require_path(value: object, *, label: str) -> Path:
    if not isinstance(value, Path):
        raise TranscriptStagingError(f"{label} must be a pathlib Path")
    return value


def _read_validation_max(value: object) -> int:
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    return 1


def _revalidate_artifact(value: object) -> StagedTranscriptArtifact:
    if not isinstance(value, StagedTranscriptArtifact):
        raise TranscriptStagingError("artifact must be a StagedTranscriptArtifact")
    try:
        return StagedTranscriptArtifact.model_validate(value, strict=True)
    except ValidationError as exc:
        raise TranscriptStagingError(str(exc)) from None


def stage_transcript_artifact(
    source_path: Path,
    private_root: Path,
    *,
    expected_sha256: str,
    expected_size_bytes: int,
    max_bytes: int,
) -> StagedTranscriptArtifact:
    """Stage exactly the caller-committed source bytes under a trusted root."""

    _validate_expected(
        sha256=expected_sha256,
        size_bytes=expected_size_bytes,
        max_bytes=max_bytes,
    )
    source = _canonical_direct(
        _require_path(source_path, label="source transcript"),
        label="source transcript",
    )
    root_path = _require_path(private_root, label="staging root")
    with _open_root(root_path) as root:
        payload, source_metadata = _read_handle_bound(
            source,
            label="source transcript",
            expected_size=expected_size_bytes,
            max_bytes=max_bytes,
            read_only=False,
        )
        if len(payload) != expected_size_bytes:
            raise TranscriptStagingError("source transcript does not match expected byte length")
        digest = hashlib.sha256(payload).hexdigest()
        if digest != expected_sha256:
            raise TranscriptStagingError("source transcript does not match expected SHA-256")
        target = root.path / f"{digest}.transcript"
        _commit_snapshot(root=root, target=target, payload=payload, digest=digest)
        return StagedTranscriptArtifact(
            source_path=source,
            source_device=int(source_metadata.st_dev),
            source_inode=int(source_metadata.st_ino),
            staging_root=root.path,
            staging_root_device=int(root.metadata.st_dev),
            staging_root_inode=int(root.metadata.st_ino),
            staged_path=target,
            sha256=digest,
            size_bytes=len(payload),
        )


def read_staged_transcript(
    artifact: StagedTranscriptArtifact,
    *,
    trusted_staging_root: Path,
    trusted_staging_root_device: int,
    trusted_staging_root_inode: int,
    expected_source_path: Path,
    expected_source_device: int,
    expected_source_inode: int,
    expected_sha256: str,
    expected_size_bytes: int,
) -> bytes:
    """Read only a snapshot bound to independent caller expectations."""

    _validate_expected(
        sha256=expected_sha256,
        size_bytes=expected_size_bytes,
        max_bytes=_read_validation_max(expected_size_bytes),
    )
    _validate_expected_identity(
        device=trusted_staging_root_device,
        inode=trusted_staging_root_inode,
        label="trusted staging root identity",
    )
    _validate_expected_identity(
        device=expected_source_device,
        inode=expected_source_inode,
        label="expected source identity",
    )
    validated = _revalidate_artifact(artifact)
    trusted_root = _canonical_direct(
        _require_path(trusted_staging_root, label="trusted staging root"),
        label="trusted staging root",
    )
    expected_source = _require_path(expected_source_path, label="expected source path").absolute()
    if validated.staging_root != trusted_root:
        raise TranscriptStagingError("artifact does not match trusted staging root")
    trusted_root_identity = (trusted_staging_root_device, trusted_staging_root_inode)
    if (validated.staging_root_device, validated.staging_root_inode) != (trusted_root_identity):
        raise TranscriptStagingError("artifact does not match trusted staging root identity")
    if validated.source_path != expected_source:
        raise TranscriptStagingError("artifact does not match expected source identity")
    if (validated.source_device, validated.source_inode) != (
        expected_source_device,
        expected_source_inode,
    ):
        raise TranscriptStagingError("artifact does not match expected source identity")
    if validated.sha256 != expected_sha256:
        raise TranscriptStagingError("artifact does not match expected SHA-256")
    if validated.size_bytes != expected_size_bytes:
        raise TranscriptStagingError("artifact does not match expected byte length")
    with _open_root(trusted_root) as root:
        if trusted_root_identity != _object_identity(root.metadata):
            raise TranscriptStagingError("trusted staging root identity changed")
        expected_target = root.path / f"{expected_sha256}.transcript"
        if validated.staged_path != expected_target:
            raise TranscriptStagingError("canonical staged path does not match the content digest")
        return _read_verified_target(
            expected_target,
            digest=expected_sha256,
            expected_size=expected_size_bytes,
            label="staged transcript",
        )


__all__ = [
    "StagedTranscriptArtifact",
    "TranscriptStagingError",
    "read_staged_transcript",
    "stage_transcript_artifact",
]
