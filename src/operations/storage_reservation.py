"""Cross-process peak reservations for optional temporary storage on one volume."""

from __future__ import annotations

import ctypes
import json
import os
import shutil
import stat
import sys
import tempfile
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

GIB = 1024**3


def _windows_common_appdata() -> Path:
    """Resolve shared Windows state from the OS, never account environment."""
    loader = getattr(ctypes, "WinDLL", None)
    if not callable(loader):
        raise OSError("Windows shared application-data API is unavailable")
    shell32 = loader("shell32", use_last_error=True)
    get_folder = getattr(shell32, "SHGetFolderPathW", None)
    if not callable(get_folder):
        raise OSError("Windows shared application-data API is unavailable")
    folder = ctypes.create_unicode_buffer(32_768)
    result = get_folder(None, 0x0023, None, 0, folder)
    if not isinstance(result, int) or result != 0 or not folder.value:
        raise OSError("Windows shared application-data identity is unavailable")
    path = Path(folder.value)
    if not path.is_absolute():
        raise ValueError("Windows shared application-data path must be absolute")
    return path


def _safe_path(path: Path) -> None:
    """Do not create or change ledger files through links or reparse points."""
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("reservation paths must be absolute without traversal")
    for component in reversed((path, *path.parents)):
        try:
            metadata = component.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(metadata.st_mode) or getattr(metadata, "st_file_attributes", 0) & 0x400:
            raise ValueError("reservation paths may not traverse links or reparse points")
        if stat.S_ISREG(metadata.st_mode) and metadata.st_nlink != 1:
            raise ValueError("reservation files must not be hard linked")


class _Lease(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    root: str
    expected_bytes: int = Field(ge=0, strict=True)
    pid: int


@contextmanager
def _locked(directory: Path) -> Generator[None]:
    _safe_path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    _safe_path(directory)
    lock = directory / "lock"
    _safe_path(lock)
    if lock.exists() and not stat.S_ISREG(lock.lstat().st_mode):
        raise ValueError("reservation lock must be a regular file")
    with lock.open("a+b") as handle:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            handle.write(b"0")
            handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl

            fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            if os.name == "nt":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)


def _store(volume: Path) -> Path:
    anchor = next(parent for parent in (volume, *volume.parents) if parent.exists())
    if sys.platform == "win32":
        return (
            _windows_common_appdata()
            / "BhanuOperations"
            / "storage-reservations"
            / str(anchor.stat().st_dev)
        )
    return (
        Path(tempfile.gettempdir()).resolve()
        / f"earnings-storage-reservations-{anchor.stat().st_dev}"
    )


def reserve_peak(
    root: Path,
    expected_bytes: int,
    *,
    ordinary_limit: int = 5 * GIB,
    host_limit: int = 20 * GIB,
    reserve_bytes: int = 50 * GIB,
    store: Path | None = None,
) -> str:
    """Reserve a declared peak; unresolved leases stay held until explicit release."""
    if expected_bytes < 0 or expected_bytes > ordinary_limit:
        raise ValueError("expected peak exceeds the ordinary run limit")
    directory = store or _store(root)
    existing = next(parent for parent in (root, *root.parents) if parent.exists())
    with _locked(directory):
        leases: list[_Lease] = []
        for path in directory.glob("*.json"):
            _safe_path(path)
            if not stat.S_ISREG(path.lstat().st_mode):
                raise ValueError("reservation must be a regular file")
            leases.append(_Lease.model_validate_json(path.read_bytes()))
        reserved = sum(lease.expected_bytes for lease in leases)
        if reserved + expected_bytes > host_limit:
            raise ValueError("concurrent disposable storage exceeds the host limit")
        if shutil.disk_usage(existing).free - reserved - expected_bytes < reserve_bytes:
            raise ValueError("projected allocation would consume operating headroom")
        token = uuid4().hex
        with (directory / f"{token}.json").open("x", encoding="utf-8") as handle:
            json.dump(
                {"root": str(root), "expected_bytes": expected_bytes, "pid": os.getpid()}, handle
            )
            handle.flush()
            os.fsync(handle.fileno())
        return token


def release_peak(root: Path, token: str, *, store: Path | None = None) -> None:
    """Release only the exact root-bound lease after producer closure."""
    if len(token) != 32 or any(c not in "0123456789abcdef" for c in token):
        raise ValueError("invalid reservation identity")
    directory = store or _store(root)
    with _locked(directory):
        path = directory / f"{token}.json"
        _safe_path(path)
        lease = _Lease.model_validate_json(path.read_bytes())
        if lease.root != str(root):
            raise ValueError("reservation belongs to another root")
        path.unlink()
