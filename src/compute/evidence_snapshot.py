"""Handle-bound, no-follow snapshots for immutable local evidence files."""

from __future__ import annotations

import ctypes
import errno
import hashlib
import importlib
import os
import sys
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, cast


class _WinDllFactory(Protocol):
    def __call__(self, name: str, *, use_last_error: bool) -> object: ...


class _WinUIntFunction(Protocol):
    argtypes: list[object]
    restype: object

    def __call__(self, *args: object) -> int: ...


class _WinHandleFunction(Protocol):
    argtypes: list[object]
    restype: object

    def __call__(self, *args: object) -> int | None: ...


class _MsvcrtModule(Protocol):
    def get_osfhandle(self, fd: int) -> int: ...

    def open_osfhandle(self, handle: int, flags: int) -> int: ...


class _LastErrorFunction(Protocol):
    def __call__(self) -> int: ...


class UnsafeEvidencePathError(ValueError):
    """The opened evidence handle is outside its verified root or is a reparse point."""


class EvidenceSourceChangedError(OSError):
    """Evidence changed or became unreadable while it was captured."""


class EvidenceFenceUnavailableError(OSError):
    """A retained native evidence fence cannot be provided on this platform."""


class EvidenceBudgetExceededError(OSError):
    """A bounded evidence read exceeded its explicit positive byte budget."""


DEFAULT_EVIDENCE_MAX_BYTES = 8 * 1024 * 1024


@dataclass(frozen=True)
class EvidenceSnapshot:
    path: Path
    payload: bytes
    sha256: str


def _identity(stat_result: os.stat_result) -> tuple[int, int, int, int]:
    return (
        int(stat_result.st_dev),
        int(stat_result.st_ino),
        int(stat_result.st_size),
        int(stat_result.st_mtime_ns),
    )


def _windows_runtime() -> tuple[object, _MsvcrtModule, _LastErrorFunction]:
    """Load Win32-only APIs behind a typed boundary that also parses on POSIX."""
    if sys.platform != "win32":
        raise EvidenceSourceChangedError("Win32 evidence API is unavailable")
    win_dll_value = getattr(ctypes, "WinDLL", None)
    last_error_value = getattr(ctypes, "get_last_error", None)
    if not callable(win_dll_value) or not callable(last_error_value):
        raise EvidenceSourceChangedError("Win32 evidence API is unavailable")
    win_dll = cast(_WinDllFactory, win_dll_value)
    last_error = cast(_LastErrorFunction, last_error_value)
    msvcrt = cast(_MsvcrtModule, importlib.import_module("msvcrt"))
    return (win_dll("kernel32", use_last_error=True), msvcrt, last_error)


def _win_uint_function(library: object, name: str) -> _WinUIntFunction:
    return cast(_WinUIntFunction, getattr(library, name))


def _win_handle_function(library: object, name: str) -> _WinHandleFunction:
    return cast(_WinHandleFunction, getattr(library, name))


def _windows_final_path(fd: int) -> Path:
    kernel32, msvcrt, _last_error = _windows_runtime()
    get_final = _win_uint_function(kernel32, "GetFinalPathNameByHandleW")
    get_final.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32]
    get_final.restype = ctypes.c_uint32
    handle = msvcrt.get_osfhandle(fd)
    size = get_final(ctypes.c_void_p(handle), None, 0, 0)
    if size == 0:
        raise EvidenceSourceChangedError("unable to resolve opened evidence handle")
    buffer = ctypes.create_unicode_buffer(size + 1)
    copied = get_final(ctypes.c_void_p(handle), buffer, len(buffer), 0)
    if copied == 0 or copied >= len(buffer):
        raise EvidenceSourceChangedError("unable to resolve opened evidence handle")
    value = buffer.value
    if value.startswith("\\\\?\\UNC\\"):
        value = "\\\\" + value[8:]
    elif value.startswith("\\\\?\\"):
        value = value[4:]
    return Path(value)


def _windows_open_no_follow(path: Path, *, directory: bool, deny_delete: bool = False) -> int:
    kernel32, msvcrt, get_last_error = _windows_runtime()
    create_file = _win_handle_function(kernel32, "CreateFileW")
    create_file.argtypes = [
        ctypes.c_wchar_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
    ]
    create_file.restype = ctypes.c_void_p
    flags = 0x00200000 | (0x02000000 if directory else 0)
    share_mode = (0x3 if deny_delete else 0x7) if directory else 0x1
    close_handle = _win_uint_function(kernel32, "CloseHandle")
    close_handle.argtypes = [ctypes.c_void_p]
    close_handle.restype = ctypes.c_int
    handle = create_file(str(path), 0x80000000, share_mode, None, 3, flags, None)
    invalid = ctypes.c_void_p(-1).value
    if handle in (None, invalid):
        if get_last_error() in {2, 3}:
            raise FileNotFoundError("evidence path is missing")
        raise EvidenceSourceChangedError("unable to open evidence handle")
    transferred = False
    try:

        class FileAttributeTagInfo(ctypes.Structure):
            _fields_ = [("attributes", ctypes.c_uint32), ("reparse_tag", ctypes.c_uint32)]

        info = FileAttributeTagInfo()
        get_info = _win_uint_function(kernel32, "GetFileInformationByHandleEx")
        get_info.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
        get_info.restype = ctypes.c_int
        if not get_info(handle, 9, ctypes.byref(info), ctypes.sizeof(info)):
            raise EvidenceSourceChangedError("unable to inspect evidence handle")
        if info.attributes & 0x400:
            raise UnsafeEvidencePathError("evidence handle is a reparse point")
        descriptor = msvcrt.open_osfhandle(int(handle), os.O_RDONLY | getattr(os, "O_BINARY", 0))
        transferred = True
        return descriptor
    finally:
        if not transferred and not close_handle(handle):
            raise EvidenceSourceChangedError("unable to close refused evidence handle")


@contextmanager
def open_windows_evidence_handle(
    path: Path, *, directory: bool = False
) -> Generator[int, None, None]:
    """Own one existing Win32 no-reparse handle; close it on every exit."""
    descriptor = _windows_open_no_follow(path, directory=directory)
    try:
        yield descriptor
    finally:
        os.close(descriptor)


def windows_evidence_handle_path(descriptor: int) -> Path:
    """Return the actual Win32 opened path, without a lexical fallback."""
    return _windows_final_path(descriptor)


def _open_no_follow(path: Path, *, directory: bool = False, deny_delete: bool = False) -> int:
    if os.name == "nt":
        return _windows_open_no_follow(path, directory=directory, deny_delete=deny_delete)
    no_follow = getattr(os, "O_NOFOLLOW", None)
    if not isinstance(no_follow, int):
        raise UnsafeEvidencePathError("no-follow file opening is unavailable")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | no_follow
    if directory:
        flags |= getattr(os, "O_DIRECTORY", 0)
    try:
        return os.open(path, flags)
    except OSError as exc:
        if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
            raise UnsafeEvidencePathError("evidence path crosses an unsafe link") from exc
        raise


def _final_path(fd: int, fallback: Path) -> Path:
    if os.name == "nt":
        return _windows_final_path(fd)
    proc_path = Path(f"/proc/self/fd/{fd}")
    try:
        return proc_path.resolve(strict=True)
    except OSError:
        return fallback.resolve(strict=True)


def _require_within(child: Path, root: Path) -> None:
    try:
        child.relative_to(root)
    except ValueError as exc:
        raise UnsafeEvidencePathError("opened evidence handle escapes its verified root") from exc


def _read_verified_payload(
    fd: int, expected: tuple[int, int, int, int], *, max_bytes: int | None
) -> bytes:
    payload_parts: list[bytes] = []
    total = 0
    while chunk := os.read(fd, 1024 * 1024):
        total += len(chunk)
        if max_bytes is not None and total > max_bytes:
            raise EvidenceBudgetExceededError("evidence exceeds the permitted byte budget")
        payload_parts.append(chunk)
    payload = b"".join(payload_parts)
    if expected != _identity(os.fstat(fd)) or len(payload) != expected[2]:
        raise EvidenceSourceChangedError("evidence changed during capture")
    return payload


def _close_descriptors(descriptors: list[int]) -> OSError | None:
    """Attempt every close and return the first failure, if any."""
    error: OSError | None = None
    for descriptor in reversed(descriptors):
        try:
            os.close(descriptor)
        except OSError as exc:
            if error is None:
                error = exc
    return error


def capture_snapshot(path: Path, allowed_root: Path) -> EvidenceSnapshot:
    """Read bytes once from a verified handle and bind path, identity, and SHA."""
    root_fd = -1
    file_fd = -1
    try:
        root_fd = _open_no_follow(allowed_root, directory=True)
        file_fd = _open_no_follow(path)
        root_before = _identity(os.fstat(root_fd))
        file_before = _identity(os.fstat(file_fd))
        final_root = _final_path(root_fd, allowed_root)
        final_path = _final_path(file_fd, path)
        _require_within(final_path, final_root)
        payload = _read_verified_payload(file_fd, file_before, max_bytes=None)
        if root_before != _identity(os.fstat(root_fd)):
            raise EvidenceSourceChangedError("verified evidence root changed during capture")
        return EvidenceSnapshot(
            path=final_path,
            payload=payload,
            sha256=hashlib.sha256(payload).hexdigest(),
        )
    finally:
        error = _close_descriptors([fd for fd in (root_fd, file_fd) if fd >= 0])
        if error is not None:
            raise error


def _validate_fence_operands(path: Path, allowed_root: Path, max_bytes: int) -> Path:
    if max_bytes <= 0:
        raise ValueError("max_bytes must be a positive integer byte budget")
    if not path.is_absolute() or not allowed_root.is_absolute():
        raise UnsafeEvidencePathError("evidence path and allowed root must be absolute")
    if ".." in path.parts or ".." in allowed_root.parts:
        raise UnsafeEvidencePathError("evidence path and allowed root must not contain '..'")
    try:
        relative = path.relative_to(allowed_root)
    except ValueError as exc:
        raise UnsafeEvidencePathError(
            "evidence path is not under the approved allowed root"
        ) from exc
    if relative == Path(".") or not relative.name:
        raise UnsafeEvidencePathError("evidence path is not a plain file under the root")
    return relative


def _same_physical_path(actual: Path, expected: Path) -> bool:
    """Compare opened and approved paths with Windows case/prefix normalization."""
    return os.path.normcase(str(actual)) == os.path.normcase(str(expected))


def _directory_chain(approved_root: Path) -> list[Path]:
    """Return every lexical directory from the volume anchor down to the root."""
    chain = [approved_root, *approved_root.parents]
    chain.reverse()
    return chain


@contextmanager
def hold_windows_evidence_snapshot(
    path: Path,
    allowed_root: Path,
    *,
    max_bytes: int = DEFAULT_EVIDENCE_MAX_BYTES,
) -> Generator[EvidenceSnapshot, None, None]:
    """Hold a retained no-reparse fence over one current regular evidence file.

    Yields the bytes the producer exposes at acquisition time, read from the
    retained descriptor, and keeps the file plus every directory from the volume
    anchor down to its parent open until the caller exits. Each opened object is
    matched against its approved lexical physical path, so a redirected root or
    any reparse component in the chain is refused rather than silently adopted.
    Ancestor handles only deny delete/rename of the held objects; they do not
    prevent child creation or population additions under those directories. Only
    existing fixed regular-file dependencies are supported: mutable
    latest-directory selection, absent-file creation, and external API selection
    require separate owner fences and are not provided. This fence excludes
    competing *handles* via sharing; whether it also excludes a memory-mapped
    writer whose file handle has already closed requires native confirmation.
    """
    relative = _validate_fence_operands(path, allowed_root, max_bytes)
    if os.name != "nt":
        raise EvidenceFenceUnavailableError(
            "retained Windows evidence fences require the Win32 runtime"
        )
    descriptors: list[int] = []
    pending: BaseException | None = None
    try:
        expected_dirs = _directory_chain(allowed_root)
        current = allowed_root
        for part in relative.parts[:-1]:
            current = current / part
            expected_dirs.append(current)
        directory_fds: list[int] = []
        for expected in expected_dirs:
            directory_fd = _open_no_follow(expected, directory=True, deny_delete=True)
            descriptors.append(directory_fd)
            directory_fds.append(directory_fd)
        file_fd = _open_no_follow(path)
        descriptors.append(file_fd)

        directory_before = [(fd, _identity(os.fstat(fd))) for fd in directory_fds]
        file_before = _identity(os.fstat(file_fd))
        for expected, directory_fd in zip(expected_dirs, directory_fds, strict=True):
            if not _same_physical_path(_final_path(directory_fd, expected), expected):
                raise UnsafeEvidencePathError(
                    "opened evidence directory does not match the approved physical path"
                )
        final_path = _final_path(file_fd, path)
        if not _same_physical_path(final_path, path):
            raise UnsafeEvidencePathError(
                "opened evidence file does not match the approved physical path"
            )
        payload = _read_verified_payload(file_fd, file_before, max_bytes=max_bytes)
        for directory_fd, identity in directory_before:
            if identity != _identity(os.fstat(directory_fd)):
                raise EvidenceSourceChangedError(
                    "verified evidence ancestor changed during capture"
                )
        yield EvidenceSnapshot(
            path=final_path,
            payload=payload,
            sha256=hashlib.sha256(payload).hexdigest(),
        )
    except BaseException as exc:
        pending = exc
        raise
    finally:
        error = _close_descriptors(descriptors)
        if error is not None:
            if pending is not None:
                pending.add_note(f"evidence fence cleanup also failed: {error!r}")
            else:
                raise error


def recorded_evidence_location(repo_root: Path, recorded: str) -> tuple[Path, Path] | None:
    """Validate a lexical repo-relative evidence path and return path + intake root."""
    candidate = Path(recorded)
    if candidate.is_absolute() or ".." in candidate.parts:
        return None
    parts = candidate.parts
    if len(parts) >= 3 and parts[0] == "transcripts" and parts[1] in {"raw", "processed"}:
        root_parts = parts[:2]
    elif len(parts) >= 2 and parts[0] == "ir_documents":
        root_parts = parts[:1]
    else:
        return None
    root = repo_root.resolve()
    intake_root = root.joinpath(*root_parts)
    path = root / candidate
    return (path, intake_root)


def snapshot_recorded_evidence(repo_root: Path, recorded: str) -> EvidenceSnapshot | None:
    location = recorded_evidence_location(repo_root, recorded)
    if location is None:
        return None
    path, intake_root = location
    try:
        return capture_snapshot(path, intake_root)
    except (OSError, ValueError):
        return None
