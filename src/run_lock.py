"""Cross-process advisory run lock for a SQLite database's write set.

AGENTS.md concurrency rule: one process owns each mutable pipeline state /
database write set at a time, and scheduled + interactive runs must acquire or
honor the SAME run lock before mutation. This module is that lock for
``data/portfolio.db``.

Motivating incident (2026-07-31/08-01): a manual ``db_gc.py --apply`` run held
the portfolio.db write lock for 10+ hours, starving ``alembic upgrade head``
(whose connection has no busy timeout) and putting the 04:00 morning pipeline's
write legs at risk. SQLite's own locking cannot arbitrate this — WAL keeps
reads flowing while every writer queues blind — so mutating batch entrypoints
must coordinate ABOVE the database.

Design: a lock FILE next to the database (``<db>.write.lock``), created with
O_CREAT|O_EXCL (atomic on NTFS and POSIX), holding a JSON payload identifying
the holder (pid, owner label, acquired_at). Keying on the database file — not
the repo checkout — matters on this machine: the MAIN and runtime checkouts
can point at the same portfolio.db and must contend on one lock.

Stale-holder recovery: if the recorded pid is no longer alive (holder crashed
or was killed without cleanup), the lock is broken and re-acquired. Liveness
uses OpenProcess on Windows (``os.kill(pid, 0)`` is NOT a probe there — it
terminates the process) and signal-0 elsewhere.

The lock is advisory between registered participants (db_gc, the morning
pipeline orchestrator). Short-transaction writers (the dashboard, pollers)
deliberately do not take it — their writes ride on busy_timeout=30000.
"""

from __future__ import annotations

import json
import os
import sys
import time
from collections.abc import Generator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from clock import now_naive_utc

LOCK_SUFFIX = ".write.lock"

_STILL_ACTIVE = 259
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_ERROR_ACCESS_DENIED = 5


class RunLockHeldError(RuntimeError):
    """The run lock is held by another live process."""

    def __init__(self, lock_path: Path, holder: dict[str, object] | None) -> None:
        self.lock_path = lock_path
        self.holder = holder or {}
        who = self.holder.get("owner", "unknown")
        pid = self.holder.get("pid", "?")
        since = self.holder.get("acquired_at", "?")
        super().__init__(f"run lock {lock_path} held by owner={who!r} pid={pid} since {since}")


def lock_path_for(db_path: str | os.PathLike[str]) -> Path:
    """The lock file guarding ``db_path``'s write set (sits next to the DB)."""
    p = Path(db_path)
    return p.with_name(p.name + LOCK_SUFFIX)


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            # Access denied means the process exists but is not ours — alive.
            return kernel32.GetLastError() == _ERROR_ACCESS_DENIED
        try:
            code = ctypes.c_ulong()
            ok = kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
            return bool(ok) and code.value == _STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _read_holder(lock_path: Path) -> dict[str, object] | None:
    try:
        raw = lock_path.read_text(encoding="utf-8")
        parsed = json.loads(raw)
    except (OSError, ValueError):
        return None
    # cast at the validated JSON boundary (repo convention).
    return cast(dict[str, object], parsed) if isinstance(parsed, dict) else None


@dataclass(slots=True)
class RunLock:
    """A held run lock. Release exactly once; prefer ``hold_run_lock``."""

    lock_path: Path
    pid: int

    def release(self) -> None:
        holder = _read_holder(self.lock_path)
        # Only remove a lock file this process still owns — never clobber a
        # successor that legitimately broke our lock while we were suspended.
        if holder is not None and holder.get("pid") != self.pid:
            return
        with suppress(OSError):
            self.lock_path.unlink()


def acquire_run_lock(
    db_path: str | os.PathLike[str],
    *,
    owner: str,
    timeout_s: float = 30.0,
    poll_s: float = 2.0,
) -> RunLock:
    """Acquire the write-set lock for ``db_path`` or raise ``RunLockHeldError``.

    A lock file whose recorded pid is dead is treated as stale and broken.
    ``timeout_s`` bounds the total wait (polling every ``poll_s`` seconds);
    0 means one attempt only.
    """
    lock_path = lock_path_for(db_path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(
        {
            "pid": os.getpid(),
            "owner": owner,
            "acquired_at": now_naive_utc().isoformat(),
            "argv": sys.argv,
        }
    )
    deadline = time.monotonic() + max(0.0, timeout_s)
    holder: dict[str, object] | None = None
    while True:
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            holder = _read_holder(lock_path)
            holder_pid = holder.get("pid") if holder else None
            if not (isinstance(holder_pid, int) and _pid_alive(holder_pid)):
                # Stale (dead holder or unreadable payload): break and retry.
                with suppress(OSError):
                    lock_path.unlink()
                continue
            if time.monotonic() >= deadline:
                raise RunLockHeldError(lock_path, holder) from None
            time.sleep(poll_s)
            continue
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
        return RunLock(lock_path=lock_path, pid=os.getpid())


@contextmanager
def hold_run_lock(
    db_path: str | os.PathLike[str],
    *,
    owner: str,
    timeout_s: float = 30.0,
    poll_s: float = 2.0,
) -> Generator[RunLock]:
    """Context-manager form of ``acquire_run_lock`` with guaranteed release."""
    lock = acquire_run_lock(db_path, owner=owner, timeout_s=timeout_s, poll_s=poll_s)
    try:
        yield lock
    finally:
        lock.release()
