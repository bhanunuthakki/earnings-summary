"""Safe shared runtime for a job that mutates one or more named write sets.

Both Task Scheduler wrappers and an interactive caller use this module.  The
lock is intentionally keyed by *what is written*, not only a job name: two
different commands that touch ``portfolio-db`` cannot overlap.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager, suppress
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4


class JobAlreadyRunningError(RuntimeError):
    """A mutable write set is already owned by another live process."""


def _pid_is_alive(pid: int) -> bool:
    if os.name == "nt":
        import ctypes

        process_query_limited_information = 0x1000
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
        if handle:
            kernel32.CloseHandle(handle)
            return True
        # Access denied also proves that a process owns the PID.
        return ctypes.get_last_error() == 5
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


@dataclass(frozen=True, slots=True)
class _LockOwner:
    pid: int
    token: str


def _read_lock_owner(path: Path) -> _LockOwner | None:
    """Read a validated owner identity, or ``None`` for unsafe/corrupt state."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            return None
        pid = payload.get("pid")
        token = payload.get("token")
        if (
            not isinstance(pid, int)
            or isinstance(pid, bool)
            or pid <= 0
            or not isinstance(token, str)
            or not token
        ):
            return None
        return _LockOwner(pid=pid, token=token)
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return None


def _stale_lock(path: Path) -> bool:
    """True only when a lock's recorded PID no longer exists.

    A crashed process must not permanently disable every future scheduled run.
    Parse failures are deliberately treated as live locks: manual deletion is
    safer than guessing ownership from corrupt state.
    """
    owner = _read_lock_owner(path)
    return owner is not None and not _pid_is_alive(owner.pid)


@contextmanager
def _lock_transition_guard(path: Path) -> Iterator[None]:
    """Serialize ownership changes using an OS lock on a persistent guard file.

    The guard file is intentionally never deleted.  Deleting the synchronization
    primitive would reintroduce the same check/delete race this guard prevents.
    Kernel locks are released automatically if a contender crashes.
    """
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
        kernel32.CreateMutexW.restype = wintypes.HANDLE
        kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel32.WaitForSingleObject.restype = wintypes.DWORD
        kernel32.ReleaseMutex.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel32.CreateMutexW(None, False, _windows_mutex_name(path))
        if not handle:
            raise OSError(ctypes.get_last_error(), "CreateMutexW failed")
        try:
            wait_result = kernel32.WaitForSingleObject(handle, 0xFFFFFFFF)
            if wait_result not in (0x00000000, 0x00000080):
                raise OSError(ctypes.get_last_error(), "WaitForSingleObject failed")
            try:
                yield
            finally:
                kernel32.ReleaseMutex(handle)
        finally:
            kernel32.CloseHandle(handle)
        return

    import fcntl

    guard_path = path.with_name(f"{path.name}.guard")
    fd = os.open(guard_path, os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(fd, "r+b") as guard:
        fcntl.flock(guard.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(guard.fileno(), fcntl.LOCK_UN)


def _safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip("-") or "job"


def _windows_mutex_name(path: Path) -> str:
    """Cross-session mutex name protected by the creator token's default ACL."""
    digest = hashlib.sha256(str(path.resolve()).casefold().encode("utf-8")).hexdigest()
    return f"Global\\earnings-summary-{digest}"


@dataclass(frozen=True, slots=True)
class HealthRecord:
    job: str
    write_sets: list[str]
    started_at: str
    ended_at: str
    status: str
    exit_code: int
    detail: str | None = None


class JobLock(AbstractContextManager["JobLock"]):
    """Atomic file locks for named mutable write sets, portable on Windows."""

    def __init__(self, repo_root: Path, job_name: str, write_sets: list[str]) -> None:
        self._dir = repo_root / ".tmp" / "job_locks"
        self._job_name = job_name
        self._write_sets = sorted(set(write_sets))
        self._owned: list[tuple[Path, str]] = []

    def __enter__(self) -> JobLock:
        self._dir.mkdir(parents=True, exist_ok=True)
        try:
            for write_set in self._write_sets:
                path = self._dir / f"{_safe_name(write_set)}.lock"
                token = uuid4().hex
                with _lock_transition_guard(path):
                    try:
                        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                    except FileExistsError as exc:
                        observed_owner = _read_lock_owner(path)
                        if observed_owner is None or _pid_is_alive(observed_owner.pid):
                            raise JobAlreadyRunningError(f"write set busy: {write_set}") from exc
                        # Compare the ownership token again immediately before
                        # deletion.  The OS guard serializes cooperating
                        # contenders; the token also prevents a stale observer
                        # from deleting a successor written by another actor.
                        if _read_lock_owner(path) != observed_owner:
                            raise JobAlreadyRunningError(
                                f"write set ownership changed: {write_set}"
                            ) from exc
                        path.unlink()
                        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                    with os.fdopen(fd, "w", encoding="utf-8") as lock_file:
                        json.dump(
                            {"job": self._job_name, "pid": os.getpid(), "token": token},
                            lock_file,
                        )
                    self._owned.append((path, token))
        except Exception:
            self.__exit__(None, None, None)
            raise
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        for path, token in reversed(self._owned):
            with _lock_transition_guard(path):
                owner = _read_lock_owner(path)
                if owner is not None and owner.token == token:
                    with suppress(FileNotFoundError):
                        path.unlink()
        self._owned.clear()


def _write_health(repo_root: Path, record: HealthRecord) -> Path:
    directory = repo_root / ".tmp" / "job_health" / _safe_name(record.job)
    directory.mkdir(parents=True, exist_ok=True)
    stamp = record.ended_at.replace(":", "-").replace("+", "_")
    path = directory / f"{stamp}.json"
    path.write_text(json.dumps(asdict(record), sort_keys=True) + "\n", encoding="utf-8")
    return path


def run_job(
    *,
    repo_root: Path,
    job_name: str,
    write_sets: list[str],
    command: list[str],
) -> int:
    """Run command under locks and write a durable JSON health record."""
    if not command:
        raise ValueError("job command is required")
    started = datetime.now(UTC)
    try:
        with JobLock(repo_root, job_name, write_sets):
            completed = subprocess.run(command, cwd=repo_root, check=False)
        exit_code = completed.returncode
        status = "ok" if exit_code == 0 else "failed"
        detail = None
    except JobAlreadyRunningError as exc:
        exit_code = 75  # EX_TEMPFAIL: safe, retryable scheduler contention.
        status = "skipped_locked"
        detail = str(exc)
    except OSError as exc:
        exit_code = 1
        status = "failed"
        detail = str(exc)
    ended = datetime.now(UTC)
    _write_health(
        repo_root,
        HealthRecord(
            job=job_name,
            write_sets=sorted(set(write_sets)),
            started_at=started.isoformat(),
            ended_at=ended.isoformat(),
            status=status,
            exit_code=exit_code,
            detail=detail,
        ),
    )
    return exit_code


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--job")
    parser.add_argument("--write-set", action="append", default=[])
    parser.add_argument("--repo-root", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--scheduler-wrapper", action="store_true")
    parser.add_argument("--python-executable")
    parser.add_argument("--python-arg", action="append", default=[])
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if args.scheduler_wrapper:
        if args.job is not None or args.write_set:
            parser.error("--scheduler-wrapper cannot be combined with --job/--write-set")
        if args.python_executable is None:
            parser.error("--scheduler-wrapper requires --python-executable")
        if len(command) < 3:
            parser.error("--scheduler-wrapper requires JOB WRITE_SET SCRIPT [SCRIPT_ARGS ...]")
        job_name, write_set, *script_command = command
        command = [args.python_executable, *args.python_arg, *script_command]
        write_sets = [write_set]
    else:
        if args.job is None:
            parser.error("--job is required")
        job_name = args.job
        write_sets = args.write_set or ["portfolio-db"]
    return run_job(
        repo_root=args.repo_root.resolve(),
        job_name=job_name,
        write_sets=write_sets,
        command=command,
    )


if __name__ == "__main__":
    raise SystemExit(main())
