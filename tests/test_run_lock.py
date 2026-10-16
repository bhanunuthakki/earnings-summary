"""Tests for src/run_lock.py — the portfolio write-set run lock.

The lock is the cross-process arbiter between db_gc and the morning pipeline
orchestrator (AGENTS.md concurrency rule): file-based, keyed to the database
file, with stale-holder (dead pid) breaking.
"""

from __future__ import annotations

import ctypes
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import run_lock


@pytest.fixture()
def db(tmp_path: Path) -> Path:
    p = tmp_path / "portfolio.db"
    p.touch()
    return p


def _dead_pid() -> int:
    """A pid guaranteed to have exited (freshly spawned, already reaped)."""
    proc = subprocess.run(
        [sys.executable, "-c", "import os; print(os.getpid())"],
        capture_output=True,
        text=True,
        check=True,
    )
    return int(proc.stdout.strip())


def test_lock_path_sits_next_to_db(db: Path) -> None:
    assert run_lock.lock_path_for(db) == db.parent / ("portfolio.db" + run_lock.LOCK_SUFFIX)


def test_acquire_writes_holder_and_release_removes(db: Path) -> None:
    lock = run_lock.acquire_run_lock(db, owner="test-owner", timeout_s=0)
    lock_path = run_lock.lock_path_for(db)
    assert lock_path.exists()
    holder = json.loads(lock_path.read_text(encoding="utf-8"))
    assert holder["pid"] == os.getpid()
    assert holder["owner"] == "test-owner"
    lock.release()
    assert not lock_path.exists()


def test_live_holder_contention_raises(db: Path) -> None:
    lock = run_lock.acquire_run_lock(db, owner="first", timeout_s=0)
    try:
        with pytest.raises(run_lock.RunLockHeldError) as excinfo:
            run_lock.acquire_run_lock(db, owner="second", timeout_s=0)
        assert excinfo.value.holder.get("owner") == "first"
        assert excinfo.value.lock_path == run_lock.lock_path_for(db)
    finally:
        lock.release()
    # Released — a fresh acquire now succeeds.
    run_lock.acquire_run_lock(db, owner="second", timeout_s=0).release()


def test_stale_dead_pid_lock_is_broken(db: Path) -> None:
    lock_path = run_lock.lock_path_for(db)
    lock_path.write_text(
        json.dumps({"pid": _dead_pid(), "owner": "ghost", "acquired_at": "2026-01-01T00:00:00"}),
        encoding="utf-8",
    )
    lock = run_lock.acquire_run_lock(db, owner="successor", timeout_s=0)
    holder = json.loads(lock_path.read_text(encoding="utf-8"))
    assert holder["owner"] == "successor"
    lock.release()


def test_corrupt_lock_payload_is_treated_as_stale(db: Path) -> None:
    lock_path = run_lock.lock_path_for(db)
    lock_path.write_text("not json at all", encoding="utf-8")
    lock = run_lock.acquire_run_lock(db, owner="successor", timeout_s=0)
    assert json.loads(lock_path.read_text(encoding="utf-8"))["owner"] == "successor"
    lock.release()


def test_release_never_clobbers_a_successor(db: Path) -> None:
    lock = run_lock.acquire_run_lock(db, owner="original", timeout_s=0)
    lock_path = run_lock.lock_path_for(db)
    # Simulate a successor that legitimately broke our lock (e.g. this process
    # was presumed dead) and now owns the file with a different pid.
    lock_path.write_text(
        json.dumps({"pid": os.getpid() + 1, "owner": "successor", "acquired_at": "x"}),
        encoding="utf-8",
    )
    lock.release()
    assert lock_path.exists()
    assert json.loads(lock_path.read_text(encoding="utf-8"))["owner"] == "successor"
    lock_path.unlink()


def test_hold_run_lock_context_manager_releases(db: Path) -> None:
    lock_path = run_lock.lock_path_for(db)
    with run_lock.hold_run_lock(db, owner="ctx", timeout_s=0):
        assert lock_path.exists()
    assert not lock_path.exists()


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (ProcessLookupError("absent"), False),
        (PermissionError("denied"), True),
        (OSError("unknown"), True),
    ],
)
def test_process_probe_retains_unknown_posix_owners(
    monkeypatch: pytest.MonkeyPatch, error: OSError, expected: bool
) -> None:
    def kill(_pid: int, _signal: int) -> None:
        raise error

    monkeypatch.setattr(run_lock.sys, "platform", "linux")
    monkeypatch.setattr(run_lock.os, "kill", kill)
    assert run_lock.pid_is_alive(12345) is expected


@pytest.mark.parametrize("error", [PermissionError("denied"), OSError("unknown read failure")])
def test_unknown_owner_read_cannot_remove_run_lock(
    db: Path, monkeypatch: pytest.MonkeyPatch, error: OSError
) -> None:
    lock = run_lock.acquire_run_lock(db, owner="live-owner", timeout_s=0)
    path = run_lock.lock_path_for(db)
    original = path.read_bytes()
    real_read = Path.read_text

    def read_text(target: Path, encoding: str | None = None, errors: str | None = None) -> str:
        if target == path:
            raise error
        return real_read(target, encoding=encoding, errors=errors)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "read_text", read_text)
        with pytest.raises(OSError):
            run_lock.acquire_run_lock(db, owner="contender", timeout_s=0)
        with pytest.raises(OSError):
            lock.release()
    assert path.read_bytes() == original
    lock.release()
    assert not path.exists()


class _NativeProbeCall:
    def __init__(self, result: int, error: Exception | None = None) -> None:
        self.result, self.error = result, error
        self.calls: list[tuple[object, ...]] = []
        self.argtypes: list[object] | None = None
        self.restype: object | None = None

    def __call__(self, *args: object) -> int:
        self.calls.append(args)
        if self.error is not None:
            raise self.error
        return self.result


def _windows_probe(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    kernel = SimpleNamespace(
        OpenProcess=_NativeProbeCall(0x123456789ABC),
        WaitForSingleObject=_NativeProbeCall(0x102),
        CloseHandle=_NativeProbeCall(1),
    )

    def load(_name: str, *, use_last_error: bool) -> SimpleNamespace:
        assert use_last_error
        return kernel

    monkeypatch.setattr(run_lock.sys, "platform", "win32")
    monkeypatch.setattr(ctypes, "WinDLL", load, raising=False)
    monkeypatch.setattr(ctypes, "get_last_error", lambda: 5, raising=False)
    return kernel


@pytest.mark.parametrize(
    ("wait_result", "alive"), [(0, False), (0x102, True), (0xFFFFFFFF, True), (0x80, True)]
)
def test_windows_probe_uses_full_handle_and_only_signaled_exit_proves_death(
    monkeypatch: pytest.MonkeyPatch, wait_result: int, alive: bool
) -> None:
    from ctypes import wintypes

    kernel = _windows_probe(monkeypatch)
    kernel.WaitForSingleObject.result = wait_result
    assert run_lock.pid_is_alive(12345) is alive
    assert kernel.OpenProcess.calls == [(0x100000, False, 12345)]
    assert kernel.OpenProcess.restype is wintypes.HANDLE
    assert kernel.OpenProcess.argtypes == [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    assert kernel.WaitForSingleObject.calls == [(0x123456789ABC, 0)]
    assert kernel.WaitForSingleObject.argtypes == [wintypes.HANDLE, wintypes.DWORD]
    assert kernel.CloseHandle.calls == [(0x123456789ABC,)]
    assert kernel.CloseHandle.argtypes == [wintypes.HANDLE]


@pytest.mark.parametrize(("error_code", "alive"), [(87, False), (5, True), (6, True), (0, True)])
def test_windows_open_denial_and_unknown_error_cannot_prove_death(
    monkeypatch: pytest.MonkeyPatch, error_code: int, alive: bool
) -> None:
    kernel = _windows_probe(monkeypatch)
    kernel.OpenProcess.result = 0
    monkeypatch.setattr(ctypes, "get_last_error", lambda: error_code, raising=False)
    assert run_lock.pid_is_alive(12345) is alive
    assert not kernel.WaitForSingleObject.calls
    assert not kernel.CloseHandle.calls


@pytest.mark.parametrize("failure", ["loader", "open", "wait", "close", "close_false"])
def test_windows_probe_api_failure_retains_owner_then_recovers(
    monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    kernel = _windows_probe(monkeypatch)
    kernel.WaitForSingleObject.result = 0
    error = OSError("controlled native probe failure")
    with monkeypatch.context() as patch:
        if failure == "loader":

            def unavailable(_name: str, **_kwargs: object) -> None:
                raise error

            patch.setattr(ctypes, "WinDLL", unavailable, raising=False)
        elif failure == "open":
            patch.setattr(kernel.OpenProcess, "error", error)
        elif failure == "wait":
            patch.setattr(kernel.WaitForSingleObject, "error", error)
        elif failure == "close":
            patch.setattr(kernel.CloseHandle, "error", error)
        else:
            patch.setattr(kernel.CloseHandle, "result", 0)
        assert run_lock.pid_is_alive(12345) is True
    assert run_lock.pid_is_alive(12345) is False
