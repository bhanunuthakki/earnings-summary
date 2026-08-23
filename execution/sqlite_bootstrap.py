"""Run a Python target with the repository's verified SQLite runtime loaded.

This is intentionally an explicit managed-launcher seam, not a global Python
startup hook. On Windows it verifies and loads the pinned official SQLite DLL
before importing ``sqlite3``. On Unix, the launcher verifies that the process
was started with the CI-built, hash-verified SQLite shared library preloaded.
Hashing and library loading happen once per process, never per connection.
"""

from __future__ import annotations

import ctypes
import hashlib
import os
import runpy
import sys
from pathlib import Path

EXPECTED_SQLITE_VERSION = "3.53.4"
EXPECTED_DLL_SHA256 = (
    "ab57d0437795ecc757cb693f32ea224173fa9856594d95cfa6b5033e645cd1ec"  # pragma: allowlist secret
)
PROJECT_ROOT = Path(__file__).resolve().parents[1]
DLL_PATH = PROJECT_ROOT / "vendor" / "sqlite" / "windows-x64" / "sqlite3.dll"

_DLL_DIRECTORY_HANDLE: object | None = None
_DLL_HANDLE: object | None = None
_LOADED_VERSION: str | None = None


def preload_sqlite() -> str:
    """Load and verify SQLite once, returning the effective library version."""
    global _DLL_DIRECTORY_HANDLE, _DLL_HANDLE, _LOADED_VERSION

    if _LOADED_VERSION is not None:
        return _LOADED_VERSION
    if "sqlite3" in sys.modules or "_sqlite3" in sys.modules:
        raise RuntimeError("sqlite3 was imported before the managed runtime bootstrap")

    if sys.platform == "win32":
        if not DLL_PATH.is_file():
            raise RuntimeError(f"verified SQLite DLL is missing: {DLL_PATH}")
        with DLL_PATH.open("rb") as dll_file:
            observed_hash = hashlib.file_digest(dll_file, "sha256").hexdigest()
        if observed_hash != EXPECTED_DLL_SHA256:
            raise RuntimeError(
                f"SQLite DLL SHA-256 mismatch: expected {EXPECTED_DLL_SHA256}, got {observed_hash}"
            )
        _DLL_DIRECTORY_HANDLE = os.add_dll_directory(os.fspath(DLL_PATH.parent))
        _DLL_HANDLE = ctypes.WinDLL(os.fspath(DLL_PATH))

    import sqlite3

    if sqlite3.sqlite_version != EXPECTED_SQLITE_VERSION:
        raise RuntimeError(
            "Python did not bind to the verified SQLite runtime: "
            f"expected {EXPECTED_SQLITE_VERSION}, got {sqlite3.sqlite_version}"
        )
    _LOADED_VERSION = sqlite3.sqlite_version
    return _LOADED_VERSION


def require_managed_sqlite_runtime() -> str:
    """Return the verified runtime version or reject an unmanaged invocation."""
    if _LOADED_VERSION != EXPECTED_SQLITE_VERSION:
        raise RuntimeError("managed SQLite runtime bootstrap was not completed")
    return _LOADED_VERSION


def _run_target(arguments: list[str]) -> int:
    while arguments[:1] == ["-u"]:
        arguments.pop(0)
    if not arguments:
        raise ValueError("a Python script or -m module target is required")
    if arguments[0] == "-m":
        if len(arguments) < 2:
            raise ValueError("-m requires a module name")
        module_name = arguments[1]
        sys.argv = [module_name, *arguments[2:]]
        runpy.run_module(module_name, run_name="__main__", alter_sys=True)
        return 0

    target = Path(arguments[0])
    if not target.is_absolute():
        target = (Path.cwd() / target).resolve()
    if not target.is_file():
        raise FileNotFoundError(target)
    sys.argv = [os.fspath(target), *arguments[1:]]
    sys.path.insert(0, os.fspath(target.parent))
    runpy.run_path(os.fspath(target), run_name="__main__")
    return 0


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    try:
        loaded_version = preload_sqlite()
    except (ImportError, OSError, RuntimeError, ValueError) as exc:
        print(f"FATAL: managed SQLite startup failed: {exc}", file=sys.stderr, flush=True)
        return 78
    if arguments == ["--check"]:
        # Exercise the idempotent path too: this must return the cached result
        # without hashing or loading the DLL a second time.
        assert preload_sqlite() == loaded_version
        print(loaded_version)
        return 0
    return _run_target(arguments)


if __name__ == "__main__":
    # ``runpy`` targets (notably pytest) may import this file by its package
    # name after startup. Reuse this exact verified module object so a second
    # instance cannot mistake the already-loaded SQLite library for an unsafe
    # pre-import. No marker or environment bypass is involved.
    sys.modules.setdefault("execution.sqlite_bootstrap", sys.modules[__name__])
    sys.modules.setdefault("sqlite_bootstrap", sys.modules[__name__])
    raise SystemExit(main())
