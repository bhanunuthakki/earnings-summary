"""Small synthetic ledgers exercise shared admission and exact release."""

from __future__ import annotations

import ctypes
import json
import os
import subprocess
import sys
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from ctypes import wintypes
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from src.operations import storage_reservation as storage


def test_windows_accounts_with_different_temp_roots_share_volume_admission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shared = tmp_path / "shared-appdata"
    monkeypatch.setattr(storage.sys, "platform", "win32")
    monkeypatch.setattr(storage, "_windows_common_appdata", lambda: shared)
    monkeypatch.setenv("PROGRAMDATA", str(tmp_path / "untrusted-programdata"))
    first, second = tmp_path / "first", tmp_path / "second"
    monkeypatch.setattr(storage.tempfile, "gettempdir", lambda: str(tmp_path / "account-one"))
    token = storage.reserve_peak(first, 80, host_limit=100, reserve_bytes=0)
    ledger = shared / "BhanuOperations" / "storage-reservations" / str(tmp_path.stat().st_dev)
    assert (ledger / f"{token}.json").exists()
    monkeypatch.setattr(storage.tempfile, "gettempdir", lambda: str(tmp_path / "account-two"))
    with pytest.raises(ValueError, match="host limit"):
        storage.reserve_peak(second, 80, host_limit=100, reserve_bytes=0)
    assert len(list(ledger.glob("*.json"))) == 1
    storage.release_peak(first, token)
    assert not (tmp_path / "untrusted-programdata").exists()


def test_concurrent_processes_cannot_both_admit_over_host_limit(tmp_path: Path) -> None:
    ledger = tmp_path / "ledger"
    script = """
import json
import sys
from pathlib import Path
from src.operations.storage_reservation import reserve_peak
try:
    token = reserve_peak(Path(sys.argv[1]), 80, host_limit=100, reserve_bytes=0,
                         store=Path(sys.argv[2]))
except ValueError as error:
    print(json.dumps({"rejected": str(error)}), flush=True)
else:
    print(json.dumps({"token": token}), flush=True)
# A live holder keeps its lease until the parent has seen both admissions.
sys.stdin.read()
"""

    def admit(name: str) -> tuple[subprocess.Popen[str], dict[str, str]]:
        child = subprocess.Popen(
            [sys.executable, "-c", script, str(tmp_path / name), str(ledger)],
            cwd=Path(__file__).resolve().parents[1],
            env={
                **os.environ,
                "PYTHONPATH": os.pathsep.join(
                    (
                        str(Path(__file__).resolve().parents[1]),
                        str(Path(__file__).resolve().parents[1] / "src"),
                    )
                ),
            },
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        assert child.stdout is not None
        line = child.stdout.readline()
        assert line, child.stderr.read() if child.stderr else ""
        return child, json.loads(line)

    with ThreadPoolExecutor(max_workers=2) as workers:
        admitted = list(workers.map(admit, ("first", "second")))
    for child, _ in admitted:
        child.communicate(timeout=20)
        assert child.returncode == 0
    outcomes = [outcome for _, outcome in admitted]
    assert sum("token" in outcome for outcome in outcomes) == 1
    assert [outcome["rejected"] for outcome in outcomes if "rejected" in outcome] == [
        "concurrent disposable storage exceeds the host limit"
    ]
    assert len(list(ledger.glob("*.json"))) == 1


def test_release_requires_exact_root_and_retains_unresolved_lease(tmp_path: Path) -> None:
    ledger, root = tmp_path / "ledger", tmp_path / "run"
    token = storage.reserve_peak(root, 80, host_limit=100, reserve_bytes=0, store=ledger)
    lease = ledger / f"{token}.json"
    original = lease.read_bytes()
    with pytest.raises(ValueError, match="another root"):
        storage.release_peak(tmp_path / "other", token, store=ledger)
    assert lease.read_bytes() == original
    with pytest.raises(ValueError, match="host limit"):
        storage.reserve_peak(root, 80, host_limit=100, reserve_bytes=0, store=ledger)
    storage.release_peak(root, token, store=ledger)
    assert not lease.exists()
    replacement = storage.reserve_peak(root, 80, host_limit=100, reserve_bytes=0, store=ledger)
    storage.release_peak(root, replacement, store=ledger)


def _write_lease(ledger: Path, root: Path, pid: int) -> Path:
    ledger.mkdir(exist_ok=True)
    lease = ledger / f"{uuid4().hex}.json"
    lease.write_text(json.dumps({"root": str(root), "expected_bytes": 80, "pid": pid}))
    return lease


def test_dead_holder_lease_is_reclaimed_at_admission(tmp_path: Path) -> None:
    ledger = tmp_path / "ledger"
    exited = subprocess.run(
        [sys.executable, "-c", "import os; print(os.getpid())"],
        capture_output=True,
        text=True,
        check=True,
        timeout=20,
    )
    stale = _write_lease(ledger, tmp_path / "crashed-run", int(exited.stdout))
    token = storage.reserve_peak(
        tmp_path / "run", 80, host_limit=100, reserve_bytes=0, store=ledger
    )
    assert not stale.exists()
    assert [path.name for path in ledger.glob("*.json")] == [f"{token}.json"]
    storage.release_peak(tmp_path / "run", token, store=ledger)


def test_live_holder_lease_is_never_reclaimed(tmp_path: Path) -> None:
    ledger = tmp_path / "ledger"
    holder = subprocess.Popen(
        [sys.executable, "-c", "import sys; sys.stdin.read()"], stdin=subprocess.PIPE
    )
    try:
        lease = _write_lease(ledger, tmp_path / "concurrent-run", holder.pid)
        original = lease.read_bytes()
        with pytest.raises(ValueError, match="host limit"):
            storage.reserve_peak(
                tmp_path / "run", 80, host_limit=100, reserve_bytes=0, store=ledger
            )
        assert lease.read_bytes() == original
    finally:
        holder.communicate(timeout=20)


class _Export:
    """A synthetic kernel32 export whose ctypes signature can be declared."""

    def __init__(self, answer: Callable[..., int | None]) -> None:
        self.answer = answer
        self.argtypes: object = None
        self.restype: object = None
        self.calls = 0

    def __call__(self, *args: Any) -> int | None:
        self.calls += 1
        return self.answer(*args)


class _FakeKernel32:
    """Synthetic Windows process API answers; no real handle is opened."""

    def __init__(self, handle: int | None, exit_code: int, queried: bool, closed: bool) -> None:
        def write_code(_handle: int, code: Any) -> int:
            ctypes.cast(code, ctypes.POINTER(ctypes.c_ulong)).contents.value = exit_code
            return int(queried)

        self.OpenProcess = _Export(lambda *_args: handle)
        self.GetExitCodeProcess = _Export(write_code)
        self.CloseHandle = _Export(lambda *_args: int(closed))


@pytest.mark.parametrize(
    ("handle", "last_error", "exit_code", "queried", "closed", "alive"),
    [
        (None, 87, 0, True, True, False),  # no such process
        (None, 5, 0, True, True, True),  # another account's live process
        (2**40, 0, 259, True, True, True),  # still active; handle wider than C int
        (2**40, 0, 1, True, True, False),  # exited
        (2**40, 0, 1, False, True, True),  # failed exit-code query
        (2**40, 0, 1, True, False, False),  # failed close keeps the answer
    ],
)
def test_windows_holder_probe_releases_only_definite_absence(
    monkeypatch: pytest.MonkeyPatch,
    handle: int | None,
    last_error: int,
    exit_code: int,
    queried: bool,
    closed: bool,
    alive: bool,
) -> None:
    kernel32 = _FakeKernel32(handle, exit_code, queried, closed)

    def load(*_args: object, **_kwargs: object) -> _FakeKernel32:
        return kernel32

    monkeypatch.setattr(ctypes, "WinDLL", load, raising=False)
    monkeypatch.setattr(ctypes, "get_last_error", lambda: last_error, raising=False)
    with monkeypatch.context() as patch:
        patch.setattr(storage.os, "name", "nt")
        observed = storage.holder_alive(os.getpid() + 1)
    assert observed is alive
    assert kernel32.CloseHandle.calls == (handle is not None)
    assert kernel32.OpenProcess.restype is wintypes.HANDLE
    assert kernel32.GetExitCodeProcess.argtypes == (wintypes.HANDLE, wintypes.LPDWORD)
    assert kernel32.CloseHandle.argtypes == (wintypes.HANDLE,)


def test_ledger_parent_link_is_refused_without_writing_target(tmp_path: Path) -> None:
    target, alias = tmp_path / "target", tmp_path / "alias"
    target.mkdir()
    if os.name == "nt":
        subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(alias), str(target)],
            check=True,
            capture_output=True,
            timeout=5,
        )
    else:
        alias.symlink_to(target, target_is_directory=True)
    try:
        with pytest.raises(ValueError, match="links or reparse"):
            storage.reserve_peak(tmp_path / "run", 1, reserve_bytes=0, store=alias / "ledger")
        assert not list(target.iterdir())
    finally:
        if os.name == "nt":
            alias.rmdir()
        else:
            alias.unlink()


def test_hard_linked_lock_is_refused_without_changing_other_file(tmp_path: Path) -> None:
    ledger = tmp_path / "ledger"
    ledger.mkdir()
    other = tmp_path / "other-file"
    other.write_bytes(b"preserve")
    os.link(other, ledger / "lock")
    with pytest.raises(ValueError, match="hard linked"):
        storage.reserve_peak(tmp_path / "run", 1, reserve_bytes=0, store=ledger)
    assert other.read_bytes() == b"preserve"


def test_permission_failure_does_not_fall_back_to_account_temp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shared = tmp_path / "shared-appdata"
    monkeypatch.setattr(storage.sys, "platform", "win32")
    monkeypatch.setattr(storage, "_windows_common_appdata", lambda: shared)
    mkdir = Path.mkdir

    def deny_ledger(
        path: Path, mode: int = 0o777, parents: bool = False, exist_ok: bool = False
    ) -> None:
        if shared in path.parents:
            raise PermissionError("synthetic shared-ledger denial")
        mkdir(path, mode=mode, parents=parents, exist_ok=exist_ok)

    monkeypatch.setattr(Path, "mkdir", deny_ledger)
    with pytest.raises(PermissionError, match="shared-ledger denial"):
        storage.reserve_peak(tmp_path / "run", 1, reserve_bytes=0)
    assert not shared.exists()
