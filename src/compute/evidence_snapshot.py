"""Handle-bound, no-follow snapshots for immutable local evidence files."""

from __future__ import annotations

import ctypes
import errno
import hashlib
import importlib
import os
import sys
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


def _windows_open_no_follow(path: Path, *, directory: bool) -> int:
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
    share_mode = 0x7 if directory else 0x1
    handle = create_file(str(path), 0x80000000, share_mode, None, 3, flags, None)
    invalid = ctypes.c_void_p(-1).value
    if handle in (None, invalid):
        if get_last_error() in {2, 3}:
            raise FileNotFoundError("evidence path is missing")
        raise EvidenceSourceChangedError("unable to open evidence handle")

    class FileAttributeTagInfo(ctypes.Structure):
        _fields_ = [("attributes", ctypes.c_uint32), ("reparse_tag", ctypes.c_uint32)]

    info = FileAttributeTagInfo()
    get_info = _win_uint_function(kernel32, "GetFileInformationByHandleEx")
    get_info.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
    get_info.restype = ctypes.c_int
    if not get_info(handle, 9, ctypes.byref(info), ctypes.sizeof(info)):
        close_handle = _win_uint_function(kernel32, "CloseHandle")
        close_handle(handle)
        raise EvidenceSourceChangedError("unable to inspect evidence handle")
    if info.attributes & 0x400:
        close_handle = _win_uint_function(kernel32, "CloseHandle")
        close_handle(handle)
        raise UnsafeEvidencePathError("evidence handle is a reparse point")
    return msvcrt.open_osfhandle(int(handle), os.O_RDONLY | getattr(os, "O_BINARY", 0))


def _open_no_follow(path: Path, *, directory: bool = False) -> int:
    if os.name == "nt":
        return _windows_open_no_follow(path, directory=directory)
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
        try:
            final_path.relative_to(final_root)
        except ValueError as exc:
            raise UnsafeEvidencePathError(
                "opened evidence handle escapes its verified root"
            ) from exc
        payload_parts: list[bytes] = []
        while chunk := os.read(file_fd, 1024 * 1024):
            payload_parts.append(chunk)
        payload = b"".join(payload_parts)
        if root_before != _identity(os.fstat(root_fd)):
            raise EvidenceSourceChangedError("verified evidence root changed during capture")
        if file_before != _identity(os.fstat(file_fd)) or len(payload) != file_before[2]:
            raise EvidenceSourceChangedError("evidence changed during capture")
        return EvidenceSnapshot(
            path=final_path,
            payload=payload,
            sha256=hashlib.sha256(payload).hexdigest(),
        )
    finally:
        if file_fd >= 0:
            os.close(file_fd)
        if root_fd >= 0:
            os.close(root_fd)


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
