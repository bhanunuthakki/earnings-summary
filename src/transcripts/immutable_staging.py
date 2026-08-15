"""Dormant, handle-verified staging for already-authorized transcript bytes.

The caller must independently supply the exact source digest and byte length.
No database, network, persistence, or acquisition entrypoint imports this module.
"""

from __future__ import annotations

import ctypes
import errno
import hashlib
import os
import re
import secrets
import stat
import sys
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Protocol, Self, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator


class _WindowsFunction(Protocol):
    argtypes: object
    restype: object

    def __call__(self, *args: object) -> int | None: ...


class _WindowsLibrary(Protocol):
    CreateFileW: _WindowsFunction
    CloseHandle: _WindowsFunction
    GetFileInformationByHandleEx: _WindowsFunction
    NtCreateFile: _WindowsFunction
    NtSetInformationFile: _WindowsFunction
    RtlNtStatusToDosError: _WindowsFunction
    SetFileInformationByHandle: _WindowsFunction


class _WindowsFFI(Protocol):
    def load_library(
        self,
        name: str,
        *,
        use_last_error: bool = False,
    ) -> _WindowsLibrary: ...

    def get_last_error(self) -> int: ...

    def open_osfhandle(self, handle: int, flags: int) -> int: ...

    def get_osfhandle(self, descriptor: int) -> int: ...


if sys.platform == "win32":
    import msvcrt as _native_msvcrt

    class _NativeWindowsFFI:
        @staticmethod
        def load_library(
            name: str,
            *,
            use_last_error: bool = False,
        ) -> _WindowsLibrary:
            return cast(
                _WindowsLibrary,
                ctypes.WinDLL(name, use_last_error=use_last_error),
            )

        @staticmethod
        def get_last_error() -> int:
            return int(ctypes.get_last_error())

        @staticmethod
        def open_osfhandle(handle: int, flags: int) -> int:
            return int(_native_msvcrt.open_osfhandle(handle, flags))

        @staticmethod
        def get_osfhandle(descriptor: int) -> int:
            return int(_native_msvcrt.get_osfhandle(descriptor))

    _WINDOWS_FFI: _WindowsFFI = _NativeWindowsFFI()
else:

    class _UnavailableWindowsFFI:
        @staticmethod
        def _unavailable() -> RuntimeError:
            return RuntimeError("Windows filesystem APIs are unavailable")

        def load_library(
            self,
            name: str,
            *,
            use_last_error: bool = False,
        ) -> _WindowsLibrary:
            del name, use_last_error
            raise self._unavailable()

        def get_last_error(self) -> int:
            raise self._unavailable()

        def open_osfhandle(self, handle: int, flags: int) -> int:
            del handle, flags
            raise self._unavailable()

        def get_osfhandle(self, descriptor: int) -> int:
            del descriptor
            raise self._unavailable()

    _WINDOWS_FFI = _UnavailableWindowsFFI()


def _windows_integer_result(result: int | None, *, api: str) -> int:
    if result is None:
        raise OSError(f"{api} returned no status")
    return result


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


@dataclass
class _OwnedTemporary:
    descriptor: int
    identity: tuple[int, int]
    name: str | None
    installed_name: str | None = None


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
    kernel32 = _WINDOWS_FFI.load_library("kernel32", use_last_error=True)
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
    sharing = (
        0x00000003 if directory else 0x00000007
    )  # Root denies DELETE; file readers share READ | WRITE | DELETE.
    raw_handle = create_file(str(path), 0x80000000, sharing, None, 3, flags, None)
    invalid_handle = ctypes.c_void_p(-1).value
    if raw_handle in (None, invalid_handle):
        error = _WINDOWS_FFI.get_last_error()
        raise OSError(error, os.strerror(error), str(path))
    handle_value = int(raw_handle)
    try:
        return _WINDOWS_FFI.open_osfhandle(
            handle_value,
            os.O_RDONLY | getattr(os, "O_BINARY", 0),
        )
    except Exception:
        kernel32.CloseHandle(ctypes.c_void_p(handle_value))
        raise


def _windows_create_owned_temporary(root: _OpenedRoot, *, digest: str) -> _OwnedTemporary:
    class _UnicodeString(ctypes.Structure):
        _fields_ = (
            ("Length", ctypes.c_ushort),
            ("MaximumLength", ctypes.c_ushort),
            ("Buffer", ctypes.c_wchar_p),
        )

    class _ObjectAttributes(ctypes.Structure):
        _fields_ = (
            ("Length", ctypes.c_uint32),
            ("RootDirectory", ctypes.c_void_p),
            ("ObjectName", ctypes.POINTER(_UnicodeString)),
            ("Attributes", ctypes.c_uint32),
            ("SecurityDescriptor", ctypes.c_void_p),
            ("SecurityQualityOfService", ctypes.c_void_p),
        )

    class _IoStatusBlock(ctypes.Structure):
        _fields_ = (
            ("Status", ctypes.c_void_p),
            ("Information", ctypes.c_size_t),
        )

    ntdll = _WINDOWS_FFI.load_library("ntdll")
    create_file = ntdll.NtCreateFile
    create_file.restype = ctypes.c_long
    for _attempt in range(32):
        name = f".{digest}.{secrets.token_hex(16)}.tmp"
        name_buffer = ctypes.create_unicode_buffer(name)
        unicode_name = _UnicodeString(
            Length=len(name) * ctypes.sizeof(ctypes.c_wchar),
            MaximumLength=(len(name) + 1) * ctypes.sizeof(ctypes.c_wchar),
            Buffer=ctypes.cast(name_buffer, ctypes.c_wchar_p),
        )
        attributes = _ObjectAttributes(
            Length=ctypes.sizeof(_ObjectAttributes),
            RootDirectory=_WINDOWS_FFI.get_osfhandle(root.descriptor),
            ObjectName=ctypes.pointer(unicode_name),
            Attributes=0x00000040,  # OBJ_CASE_INSENSITIVE
            SecurityDescriptor=None,
            SecurityQualityOfService=None,
        )
        raw_handle = ctypes.c_void_p()
        io_status = _IoStatusBlock()
        status = _windows_integer_result(
            create_file(
                ctypes.byref(raw_handle),
                0x0013019F,  # FILE_GENERIC_READ | FILE_GENERIC_WRITE | DELETE
                ctypes.byref(attributes),
                ctypes.byref(io_status),
                None,
                0x00000080,  # FILE_ATTRIBUTE_NORMAL
                0x00000001,  # FILE_SHARE_READ: deny replacement while owned
                2,  # FILE_CREATE: no overwrite
                0x00200060,  # OPEN_REPARSE_POINT | NON_DIRECTORY | SYNCHRONOUS_NONALERT
                None,
                0,
            ),
            api="NtCreateFile",
        )
        if status < 0:
            error = _windows_error_from_ntstatus(status)
            if error in (80, 183):
                continue
            raise OSError(error, os.strerror(error), name)
        if raw_handle.value is None:
            raise OSError("NtCreateFile returned an empty handle")
        handle_value = int(raw_handle.value)
        try:
            descriptor = _WINDOWS_FFI.open_osfhandle(
                handle_value,
                os.O_RDWR | getattr(os, "O_BINARY", 0),
            )
        except Exception:
            _WINDOWS_FFI.load_library("kernel32").CloseHandle(ctypes.c_void_p(handle_value))
            raise
        metadata = os.fstat(descriptor)
        _require_regular(metadata, label="owned temporary", read_only=False)
        return _OwnedTemporary(
            descriptor=descriptor,
            identity=_object_identity(metadata),
            name=name,
        )
    raise TranscriptStagingError("owned temporary name allocation was exhausted")


def _windows_error_from_ntstatus(status: int) -> int:
    ntdll = _WINDOWS_FFI.load_library("ntdll")
    convert = ntdll.RtlNtStatusToDosError
    convert.argtypes = (ctypes.c_long,)
    convert.restype = ctypes.c_uint32
    converted = convert(status)
    if converted is None:
        raise OSError("RtlNtStatusToDosError returned no status")
    return int(converted)


def _windows_set_read_only(descriptor: int) -> None:
    class _FileBasicInfo(ctypes.Structure):
        _fields_ = (
            ("CreationTime", ctypes.c_longlong),
            ("LastAccessTime", ctypes.c_longlong),
            ("LastWriteTime", ctypes.c_longlong),
            ("ChangeTime", ctypes.c_longlong),
            ("FileAttributes", ctypes.c_uint32),
        )

    kernel32 = _WINDOWS_FFI.load_library("kernel32", use_last_error=True)
    get_info = kernel32.GetFileInformationByHandleEx
    set_info = kernel32.SetFileInformationByHandle
    handle = ctypes.c_void_p(_WINDOWS_FFI.get_osfhandle(descriptor))
    basic = _FileBasicInfo()
    if not get_info(handle, 0, ctypes.byref(basic), ctypes.sizeof(basic)):
        error = _WINDOWS_FFI.get_last_error()
        raise OSError(error, os.strerror(error))
    basic.FileAttributes = (basic.FileAttributes & ~0x00000080) | 0x00000001
    if not set_info(handle, 0, ctypes.byref(basic), ctypes.sizeof(basic)):
        error = _WINDOWS_FFI.get_last_error()
        raise OSError(error, os.strerror(error))


def _windows_rename_owned(
    temporary: _OwnedTemporary,
    *,
    root: _OpenedRoot,
    target_name: str,
) -> None:
    class _FileRenameInfo(ctypes.Structure):
        _fields_ = (
            ("ReplaceIfExists", ctypes.c_ubyte),
            ("RootDirectory", ctypes.c_void_p),
            ("FileNameLength", ctypes.c_uint32),
            ("FileName", ctypes.c_wchar * (len(target_name) + 1)),
        )

    class _IoStatusBlock(ctypes.Structure):
        _fields_ = (
            ("Status", ctypes.c_void_p),
            ("Information", ctypes.c_size_t),
        )

    ntdll = _WINDOWS_FFI.load_library("ntdll")
    set_info = ntdll.NtSetInformationFile
    set_info.argtypes = (
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_int,
    )
    set_info.restype = ctypes.c_long
    info = _FileRenameInfo()
    info.ReplaceIfExists = 0
    info.RootDirectory = _WINDOWS_FFI.get_osfhandle(root.descriptor)
    info.FileNameLength = len(target_name) * ctypes.sizeof(ctypes.c_wchar)
    info.FileName = target_name
    handle = ctypes.c_void_p(_WINDOWS_FFI.get_osfhandle(temporary.descriptor))
    io_status = _IoStatusBlock()
    status = _windows_integer_result(
        set_info(
            handle,
            ctypes.byref(io_status),
            ctypes.byref(info),
            ctypes.sizeof(info),
            10,  # FileRenameInformation
        ),
        api="NtSetInformationFile",
    )
    if status >= 0:
        return
    error = _windows_error_from_ntstatus(status)
    if error in (80, 183):
        raise FileExistsError(error, os.strerror(error), target_name)
    raise OSError(error, os.strerror(error), target_name)


def _windows_mark_owned_for_deletion(descriptor: int) -> None:
    kernel32 = _WINDOWS_FFI.load_library("kernel32", use_last_error=True)
    set_info = kernel32.SetFileInformationByHandle
    handle = ctypes.c_void_p(_WINDOWS_FFI.get_osfhandle(descriptor))
    flags = ctypes.c_uint32(0x00000013)  # DELETE | POSIX_SEMANTICS | IGNORE_READONLY
    if set_info(handle, 21, ctypes.byref(flags), ctypes.sizeof(flags)):
        return
    first_error = _WINDOWS_FFI.get_last_error()
    disposition = ctypes.c_int(1)
    if set_info(handle, 4, ctypes.byref(disposition), ctypes.sizeof(disposition)):
        return
    error = _WINDOWS_FFI.get_last_error() or first_error
    raise OSError(error, os.strerror(error))


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


def _lstat_under_root(
    root: _OpenedRoot,
    name: str,
    *,
    label: str,
) -> os.stat_result:
    if os.name == "nt":
        return _lstat(root.path / name, label=label)
    try:
        metadata = os.stat(name, dir_fd=root.descriptor, follow_symlinks=False)
    except OSError as exc:
        raise TranscriptStagingError(f"{label} is unavailable") from exc
    if stat.S_ISLNK(metadata.st_mode) or _has_reparse_attribute(metadata):
        raise TranscriptStagingError(f"{label} must not be a symlink or reparse point")
    return metadata


def _open_under_root(root: _OpenedRoot, name: str) -> int:
    if os.name == "nt":
        return _open_no_follow(root.path / name)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        return os.open(name, flags, dir_fd=root.descriptor)
    except OSError as exc:
        raise TranscriptStagingError("path could not be opened without following links") from exc


def _read_verified_target(
    root: _OpenedRoot,
    name: str,
    *,
    digest: str,
    expected_size: int,
    label: str,
) -> bytes:
    before = _lstat_under_root(root, name, label=label)
    _require_regular(before, label=label, read_only=True)
    descriptor = _open_under_root(root, name)
    try:
        opened = os.fstat(descriptor)
        _require_regular(opened, label=label, read_only=True)
        if _object_identity(before) != _object_identity(opened):
            raise TranscriptStagingError(f"{label} identity changed while opening")
        payload = _read_exact_descriptor(descriptor, expected_size=expected_size)
        after = os.fstat(descriptor)
        _require_regular(after, label=label, read_only=True)
        if _stable_identity(opened) != _stable_identity(after):
            raise TranscriptStagingError(f"{label} identity changed during read")
        current = _lstat_under_root(root, name, label=label)
        if _object_identity(after) != _object_identity(current):
            raise TranscriptStagingError(f"{label} identity changed after read")
    finally:
        os.close(descriptor)
    if (
        int(after.st_size) != expected_size
        or len(payload) != expected_size
        or hashlib.sha256(payload).hexdigest() != digest
    ):
        mismatch_label = "staged" if label == "staged transcript" else label
        raise TranscriptStagingError(f"{mismatch_label} SHA-256 or byte length does not match")
    return payload


def _read_exact_descriptor(descriptor: int, *, expected_size: int) -> bytes:
    os.lseek(descriptor, 0, os.SEEK_SET)
    chunks: list[bytes] = []
    remaining = expected_size + 1
    while remaining > 0:
        chunk = os.read(descriptor, min(1024 * 1024, remaining))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _create_owned_temporary(root: _OpenedRoot, *, digest: str) -> _OwnedTemporary:
    if os.name == "nt":
        try:
            return _windows_create_owned_temporary(root, digest=digest)
        except OSError as exc:
            raise TranscriptStagingError("handle-owned temporary could not be created") from exc
    temporary_flag = int(getattr(os, "O_TMPFILE", 0))
    if not sys.platform.startswith("linux") or temporary_flag == 0:
        raise TranscriptStagingError("this POSIX platform lacks handle-owned anonymous staging")
    flags = os.O_RDWR | temporary_flag | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(".", flags, 0o600, dir_fd=root.descriptor)
    except OSError as exc:
        raise TranscriptStagingError(
            "staging root does not support handle-owned anonymous staging"
        ) from exc
    metadata = os.fstat(descriptor)
    if not stat.S_ISREG(metadata.st_mode) or int(metadata.st_nlink) != 0:
        os.close(descriptor)
        raise TranscriptStagingError("anonymous staging handle is not an unlinked regular file")
    return _OwnedTemporary(
        descriptor=descriptor,
        identity=_object_identity(metadata),
        name=None,
    )


def _seal_owned_temporary(temporary: _OwnedTemporary) -> None:
    if os.name == "nt":
        _windows_set_read_only(temporary.descriptor)
    else:
        os.fchmod(temporary.descriptor, stat.S_IREAD)


def _link_anonymous_posix(
    temporary: _OwnedTemporary,
    *,
    root: _OpenedRoot,
    target_name: str,
) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    link_at = libc.linkat
    link_at.argtypes = (
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
    )
    link_at.restype = ctypes.c_int
    result = link_at(
        temporary.descriptor,
        b"",
        root.descriptor,
        os.fsencode(target_name),
        0x1000,  # AT_EMPTY_PATH: link the exact open anonymous inode
    )
    if result == 0:
        return
    error = ctypes.get_errno()
    if error in (errno.ENOENT, errno.EPERM, errno.EINVAL):
        result = link_at(
            -100,  # AT_FDCWD for the procfs descriptor path
            os.fsencode(f"/proc/self/fd/{temporary.descriptor}"),
            root.descriptor,
            os.fsencode(target_name),
            0x400,  # AT_SYMLINK_FOLLOW: follow procfs to the exact open inode
        )
        if result == 0:
            return
        error = ctypes.get_errno()
    if error == errno.EEXIST:
        raise FileExistsError(error, os.strerror(error), target_name)
    raise OSError(error, os.strerror(error), target_name)


def _atomic_install_no_replace(
    temporary: _OwnedTemporary,
    *,
    root: _OpenedRoot,
    target_name: str,
) -> None:
    if os.name == "nt":
        _windows_rename_owned(temporary, root=root, target_name=target_name)
    else:
        _link_anonymous_posix(temporary, root=root, target_name=target_name)
    temporary.installed_name = target_name


def _validate_owned_temporary(
    temporary: _OwnedTemporary,
    *,
    payload: bytes,
    digest: str,
    installed: bool,
) -> None:
    before = os.fstat(temporary.descriptor)
    if not stat.S_ISREG(before.st_mode) or _has_reparse_attribute(before):
        raise TranscriptStagingError("owned temporary must remain a regular direct file")
    expected_links = 1 if installed or os.name == "nt" else 0
    if int(before.st_nlink) != expected_links:
        raise TranscriptStagingError("owned temporary link count changed")
    if before.st_mode & _WRITE_MODE_MASK:
        raise TranscriptStagingError("owned temporary must remain read-only")
    observed = _read_exact_descriptor(
        temporary.descriptor,
        expected_size=len(payload),
    )
    after = os.fstat(temporary.descriptor)
    if _stable_identity(before) != _stable_identity(after):
        raise TranscriptStagingError("owned temporary identity changed during verification")
    if (
        _object_identity(after) != temporary.identity
        or len(observed) != len(payload)
        or observed != payload
        or hashlib.sha256(observed).hexdigest() != digest
    ):
        raise TranscriptStagingError("owned temporary bytes do not match the authorized source")


def _delete_owned_temporary(
    temporary: _OwnedTemporary,
    *,
    root: _OpenedRoot,
    descriptor: int,
) -> None:
    if os.name == "nt":
        _windows_mark_owned_for_deletion(descriptor)
    elif temporary.installed_name is not None:
        raise OSError("POSIX installed residue retained rather than unlinking a mutable name")


def _close_owned_temporary(
    temporary: _OwnedTemporary,
    *,
    root: _OpenedRoot,
    preserve_installed: bool,
) -> None:
    descriptor = temporary.descriptor
    temporary.descriptor = -1
    cleanup_error: OSError | None = None
    try:
        if not preserve_installed:
            _delete_owned_temporary(
                temporary,
                root=root,
                descriptor=descriptor,
            )
    except OSError as exc:
        cleanup_error = exc
    try:
        os.close(descriptor)
    except OSError as exc:
        cleanup_error = cleanup_error or exc
    if cleanup_error is not None:
        residue = temporary.installed_name or temporary.name or "<anonymous handle>"
        raise TranscriptStagingError(
            f"owned temporary cleanup could not be completed safely; residue retained: {residue}"
        ) from cleanup_error


def _commit_snapshot(*, root: _OpenedRoot, target: Path, payload: bytes, digest: str) -> None:
    target_name = target.name
    try:
        _lstat_under_root(root, target_name, label="content-address collision")
    except TranscriptStagingError as exc:
        if not isinstance(exc.__cause__, FileNotFoundError):
            raise
    else:
        _read_verified_target(
            root,
            target_name,
            digest=digest,
            expected_size=len(payload),
            label="content-address collision",
        )
        return

    temporary = _create_owned_temporary(root, digest=digest)
    preserve_installed = False
    try:
        _write_all(temporary.descriptor, payload)
        _seal_owned_temporary(temporary)
        _validate_owned_temporary(
            temporary,
            payload=payload,
            digest=digest,
            installed=False,
        )
        try:
            _atomic_install_no_replace(
                temporary,
                root=root,
                target_name=target_name,
            )
        except FileExistsError:
            _close_owned_temporary(
                temporary,
                root=root,
                preserve_installed=False,
            )
            _read_verified_target(
                root,
                target_name,
                digest=digest,
                expected_size=len(payload),
                label="content-address collision",
            )
            return
        except OSError as exc:
            raise TranscriptStagingError(
                "content-addressed snapshot could not be committed"
            ) from exc
        _validate_owned_temporary(
            temporary,
            payload=payload,
            digest=digest,
            installed=True,
        )
        if os.name != "nt":
            installed_metadata = _lstat_under_root(
                root,
                target_name,
                label="installed target",
            )
            _require_regular(installed_metadata, label="installed target", read_only=True)
            if _object_identity(installed_metadata) != temporary.identity:
                raise TranscriptStagingError(
                    "installed target identity does not match owned handle"
                )
        preserve_installed = True
    except OSError as exc:
        raise TranscriptStagingError("owned snapshot operation failed") from exc
    finally:
        if temporary.descriptor >= 0:
            _close_owned_temporary(
                temporary,
                root=root,
                preserve_installed=preserve_installed,
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
            root,
            expected_target.name,
            digest=expected_sha256,
            expected_size=expected_size_bytes,
            label="staged transcript",
        )


def install_transcript_output(
    payload: bytes,
    output_root: Path,
    target_name: str,
    *,
    expected_sha256: str,
    expected_size_bytes: int,
) -> Path:
    """Install exact bytes under a trusted output root without replacing any target."""

    _validate_expected(
        sha256=expected_sha256,
        size_bytes=expected_size_bytes,
        max_bytes=_read_validation_max(expected_size_bytes),
    )
    if (
        len(payload) != expected_size_bytes
        or hashlib.sha256(payload).hexdigest() != expected_sha256
    ):
        raise TranscriptStagingError("output payload does not match expected identity")
    if not target_name or Path(target_name).name != target_name or target_name in {".", ".."}:
        raise TranscriptStagingError("output target must be one direct filename")
    root_path = _require_path(output_root, label="output root")
    with _open_root(root_path) as root:
        target = root.path / target_name
        _commit_snapshot(root=root, target=target, payload=payload, digest=expected_sha256)
        _read_verified_target(
            root,
            target_name,
            digest=expected_sha256,
            expected_size=expected_size_bytes,
            label="installed output",
        )
    return target


__all__ = [
    "StagedTranscriptArtifact",
    "TranscriptStagingError",
    "install_transcript_output",
    "read_staged_transcript",
    "stage_transcript_artifact",
]
