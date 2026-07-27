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
from collections.abc import Generator
from contextlib import AbstractContextManager, contextmanager, suppress
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import cast
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


def _process_start_identity(pid: int) -> str | None:
    """Best-effort process creation identity used to detect PID reuse."""
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        process_query_limited_information = 0x1000
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
        if not handle:
            return None
        created = wintypes.FILETIME()
        exited = wintypes.FILETIME()
        kernel = wintypes.FILETIME()
        user = wintypes.FILETIME()
        try:
            if not kernel32.GetProcessTimes(
                handle,
                ctypes.byref(created),
                ctypes.byref(exited),
                ctypes.byref(kernel),
                ctypes.byref(user),
            ):
                return None
            ticks = (int(created.dwHighDateTime) << 32) | int(created.dwLowDateTime)
            return f"win:{ticks}"
        finally:
            kernel32.CloseHandle(handle)
    try:
        fields = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").split()
    except OSError:
        return None
    return f"proc:{fields[21]}" if len(fields) > 21 else None


@dataclass(frozen=True, slots=True)
class _LockOwner:
    pid: int
    token: str
    process_start: str | None = None


def _read_lock_owner(path: Path) -> _LockOwner | None:
    """Read a validated owner identity, or ``None`` for unsafe/corrupt state."""
    try:
        raw: object = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return None
        payload = cast("dict[str, object]", raw)
        pid = payload.get("pid")
        token = payload.get("token")
        process_start = payload.get("process_start")
        if (
            not isinstance(pid, int)
            or isinstance(pid, bool)
            or pid <= 0
            or not isinstance(token, str)
            or not token
            or (process_start is not None and not isinstance(process_start, str))
        ):
            return None
        return _LockOwner(pid=pid, token=token, process_start=process_start)
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return None


def _owner_is_live(owner: _LockOwner) -> bool:
    if not _pid_is_alive(owner.pid):
        return False
    if owner.process_start is None:
        return True
    observed_start = _process_start_identity(owner.pid)
    return observed_start is None or observed_start == owner.process_start


def _stale_lock(path: Path) -> bool:
    """True only when a lock's recorded process identity no longer exists.

    A crashed process must not permanently disable every future scheduled run.
    Parse failures are deliberately treated as live locks: manual deletion is
    safer than guessing ownership from corrupt state.
    """
    owner = _read_lock_owner(path)
    return owner is not None and not _owner_is_live(owner)


@contextmanager
def _lock_transition_guard(path: Path) -> Generator[None, None, None]:
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
        self._repo_root = repo_root.resolve()
        self._job_name = job_name
        self._write_sets = sorted(set(write_sets))
        self._owned: list[tuple[Path, str]] = []

    def __enter__(self) -> JobLock:
        try:
            for write_set in self._write_sets:
                path = _write_set_lock_path(self._repo_root, write_set)
                path.parent.mkdir(parents=True, exist_ok=True)
                token = uuid4().hex
                with _lock_transition_guard(path):
                    try:
                        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                    except FileExistsError as exc:
                        observed_owner = _read_lock_owner(path)
                        if observed_owner is None or _owner_is_live(observed_owner):
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
                            {
                                "job": self._job_name,
                                "pid": os.getpid(),
                                "token": token,
                                "process_start": _process_start_identity(os.getpid()),
                            },
                            lock_file,
                        )
                    self._owned.append((path, token))
        except Exception:
            self.__exit__(None, None, None)
            raise
        return self

    def inheritance_proof(self) -> str:
        """Opaque child proof, validated against the still-live lock records."""
        return json.dumps(
            {
                write_set: {"path": str(path), "token": token, "pid": os.getpid()}
                for write_set, (path, token) in zip(self._write_sets, self._owned, strict=True)
            },
            separators=(",", ":"),
            sort_keys=True,
        )

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        for path, token in reversed(self._owned):
            with _lock_transition_guard(path):
                owner = _read_lock_owner(path)
                if owner is not None and owner.token == token:
                    with suppress(FileNotFoundError):
                        path.unlink()
        self._owned.clear()


def portfolio_db_path(repo_root: Path) -> Path:
    configured = os.environ.get("EARNINGS_SUMMARY_DB_PATH", "").strip()
    return (
        Path(configured).expanduser().resolve()
        if configured
        else (repo_root / "data" / "portfolio.db").resolve()
    )


def _write_set_lock_path(repo_root: Path, write_set: str) -> Path:
    if write_set == "portfolio-db":
        db_path = portfolio_db_path(repo_root)
        digest = hashlib.sha256(str(db_path).casefold().encode("utf-8")).hexdigest()[:20]
        return db_path.parent / ".job_locks" / f"portfolio-db-{digest}.lock"
    return repo_root / ".tmp" / "job_locks" / f"{_safe_name(write_set)}.lock"


def inherited_lock_is_valid(repo_root: Path, write_set: str) -> bool:
    """Validate that the parent process still owns the exact inherited lock."""
    raw = os.environ.get("EARNINGS_SUMMARY_JOB_LOCK_PROOF", "")
    try:
        proof = json.loads(raw)
        entry = proof[write_set]
        path = Path(entry["path"]).resolve()
        token = entry["token"]
        pid = entry["pid"]
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return False
    if (
        path != _write_set_lock_path(repo_root.resolve(), write_set)
        or not isinstance(token, str)
        or not isinstance(pid, int)
        or pid != os.getppid()
    ):
        return False
    owner = _read_lock_owner(path)
    return owner is not None and owner.pid == pid and owner.token == token and _owner_is_live(owner)


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
    from runtime.secrets import load_project_env

    load_project_env(repo_root)
    started = datetime.now(UTC)
    try:
        with JobLock(repo_root, job_name, write_sets) as lock:
            child_env = {
                **os.environ,
                "EARNINGS_SUMMARY_JOB_LOCK_PROOF": lock.inheritance_proof(),
            }
            completed = subprocess.run(command, cwd=repo_root, check=False, env=child_env)
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
