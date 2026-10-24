"""Controller-owned recovery hold and cooperative lifetime for a source launcher."""

from __future__ import annotations

import ctypes
import json
import os
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from contextlib import ExitStack
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import BinaryIO, Literal
from uuid import uuid4

from runtime.job_runtime import WindowsKillOnCloseJob, resume_suspended_process
from runtime.windows_appcontainer import NativeApi

_RETAINED_FENCES: dict[Path, ExitStack] = {}
_RETAINED_OWNERS: dict[Path, SourceLauncherSupervisor] = {}


class SandboxRecoveryRequiredError(RuntimeError):
    """An owned sandbox requires explicit recovery before another invocation."""


CleanupStage = Literal[
    "job-stop",
    "suspended-root-kill",
    "suspended-root-wait",
    "job-close",
    "thread-drain",
    "stdio-close",
    "control-close",
]


@dataclass(frozen=True)
class SupervisorCleanupError:
    stage: CleanupStage
    error_type: str


def retain_fences(root: Path, stack: ExitStack) -> None:
    """Keep this process's single held write fence alive after unknown stop proof."""
    if root in _RETAINED_FENCES:
        raise SandboxRecoveryRequiredError("sandbox write fences are already retained")
    _RETAINED_FENCES[root] = stack.pop_all()


def _identity(path: Path) -> tuple[int, int]:
    for part in (path, *path.parents):
        stat = part.lstat()
        if part.is_symlink() or getattr(stat, "st_file_attributes", 0) & 0x400:
            raise SandboxRecoveryRequiredError("sandbox state contains a reparse point")
    stat = path.stat()
    if path.is_file() and stat.st_nlink != 1:
        raise SandboxRecoveryRequiredError("sandbox state contains a hardlink")
    return stat.st_dev, stat.st_ino


def _native() -> NativeApi:
    # Keep import side effects outside non-Windows inspection and unit tests.
    return NativeApi()


def _acl_census(
    roots: Sequence[Path], *, deadline: float, maximum_members: int
) -> dict[str, object]:
    native = _native()
    from ctypes import wintypes

    convert = native.a.ConvertSecurityDescriptorToStringSecurityDescriptorW
    convert.argtypes = [
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.LPWSTR),
        ctypes.POINTER(wintypes.DWORD),
    ]
    convert.restype = wintypes.BOOL
    rows: dict[str, object] = {}
    total_bytes = 0
    for root in roots:
        from itertools import chain

        for path in chain((root,), root.rglob("*")):
            if len(rows) >= maximum_members or time.monotonic() >= deadline:
                raise SandboxRecoveryRequiredError("sandbox ACL census bound exceeded")
            identity = _identity(path)
            owner, group, dacl, sacl, descriptor = (ctypes.c_void_p() for _ in range(5))
            result = native.a.GetNamedSecurityInfoW(
                str(path),
                1,
                7,
                ctypes.byref(owner),
                ctypes.byref(group),
                ctypes.byref(dacl),
                ctypes.byref(sacl),
                ctypes.byref(descriptor),
            )
            if result or not descriptor.value:
                raise SandboxRecoveryRequiredError("sandbox ACL census failed")
            text = wintypes.LPWSTR()
            try:
                native.check(convert(descriptor, 1, 7, ctypes.byref(text), None), "ACL census")
                total_bytes += len(str(path).encode()) + len((text.value or "").encode())
                if total_bytes > 64 * 1024 * 1024:
                    raise SandboxRecoveryRequiredError("sandbox ACL census byte cap exceeded")
                rows[str(path)] = [*identity, text.value]
            finally:
                if text:
                    native.k.LocalFree(text)
                native.k.LocalFree(descriptor)
    return rows


def _require_state_root(root: Path, protected: Sequence[Path]) -> None:
    if (
        not root.is_absolute()
        or ".." in root.parts
        or str(root).startswith(("//", "\\\\"))
        or not root.is_dir()
    ):
        raise SandboxRecoveryRequiredError("explicit local sandbox state root is required")
    _identity(root)
    if any(
        root == path or root.is_relative_to(path) or path.is_relative_to(root) for path in protected
    ):
        raise SandboxRecoveryRequiredError("sandbox state overlaps protected inputs")
    native = _native()
    trusted = {
        "S-1-5-18",
        "S-1-5-32-544",
        "S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464",
    }
    for ancestor in root.parents:
        ancestor_owner, _, ancestor_entries, _ = native.security(str(ancestor), 1, named=True)
        if ancestor_owner not in trusted or any(
            ace.kind not in (0, 1)
            or (
                ace.kind == 0
                and not ace.flags & 8
                and ace.sid not in trusted
                and ace.mask & 0x120D0040
            )
            for ace in ancestor_entries
        ):
            raise SandboxRecoveryRequiredError("sandbox state ancestor permits replacement")
    owner, guarded, entries, _ = native.security(str(root), 1, named=True)
    if owner not in ("S-1-5-18", "S-1-5-32-544") or not guarded or len(entries) != 2:
        raise SandboxRecoveryRequiredError("sandbox state authority is not protected")
    if {entry.sid for entry in entries} != {"S-1-5-18", "S-1-5-32-544"} or any(
        entry.kind != 0 or entry.flags not in (0, 3) or entry.mask != 0x1F01FF for entry in entries
    ):
        raise SandboxRecoveryRequiredError("sandbox state write authority changed")


class _Control:
    native: NativeApi
    cancel: int
    write: int
    reader: BinaryIO

    def __init__(self) -> None:
        if sys.platform != "win32":
            raise SandboxRecoveryRequiredError("Windows control handles unavailable")
        import msvcrt
        from ctypes import wintypes

        native = _native()
        self.native = native
        # The native API sets exact ABI types; only the event signatures are additional.
        native.k.CreateEventW.argtypes = [
            ctypes.c_void_p,
            wintypes.BOOL,
            wintypes.BOOL,
            wintypes.LPCWSTR,
        ]
        native.k.CreateEventW.restype = wintypes.HANDLE
        native.k.SetEvent.argtypes = [wintypes.HANDLE]
        native.k.SetEvent.restype = wintypes.BOOL
        self.cancel = int(
            native.check(native.k.CreateEventW(None, True, False, None), "cancel event")
        )
        self.write = 0
        read, write = wintypes.HANDLE(), wintypes.HANDLE()
        try:
            native.check(native.k.SetHandleInformation(self.cancel, 1, 1), "cancel inheritance")
            native.check(
                native.k.CreatePipe(ctypes.byref(read), ctypes.byref(write), None, 0),
                "cleanup pipe",
            )
            native.check(native.k.SetHandleInformation(write, 1, 1), "cleanup inheritance")
            assert write.value is not None and read.value is not None
            self.write = int(write.value)
            self.reader = os.fdopen(
                msvcrt.open_osfhandle(int(read.value), os.O_RDONLY), "rb", buffering=0
            )
        except BaseException:
            if read.value:
                native.k.CloseHandle(read)
            if write.value:
                native.k.CloseHandle(write)
            native.k.CloseHandle(self.cancel)
            raise

    def signal(self) -> None:
        self.native.check(self.native.k.SetEvent(self.cancel), "cancel signal")

    def close_write(self) -> None:
        if self.write:
            self.native.check(self.native.k.CloseHandle(self.write), "cleanup writer close")
            self.write = 0

    def close(self) -> None:
        self.close_write()
        self.reader.close()
        self.native.check(self.native.k.CloseHandle(self.cancel), "cancel event close")


def _acknowledged(body: bytes, nonce: str, installation: str) -> bool:
    try:
        value = json.loads(body)
    except (ValueError, UnicodeError):
        return False
    return (
        isinstance(value, dict)
        and value.get("cleanup_proved") is True
        and value
        == {
            "invocation_id": nonce,
            "installation_id": installation,
            "cleanup_proved": True,
        }
    )


class SourceLauncherSupervisor:
    def __init__(
        self,
        *,
        sandbox_state_root: Path,
        runtime_root: Path,
        input_root: Path,
        installation_id: str,
        timeout_seconds: int = 600,
        maximum_members: int = 200_000,
    ) -> None:
        self.deadline = time.monotonic() + timeout_seconds
        self.maximum_members = maximum_members
        self.retained_resources: tuple[object, ...] = ()
        self.cleanup_errors: list[SupervisorCleanupError] = []
        self.marker_identity: tuple[int, int] | None = None
        self.root = sandbox_state_root
        self.roots = (runtime_root, input_root)
        self.installation = installation_id
        self.nonce = uuid4().hex
        self.path = self.root / "active.json"
        self.stop_proved = True
        self.clean = False
        self.baseline: dict[str, object] = {}
        self.record = b""
        self.state_identity: tuple[int, int] | None = None

    def __enter__(self) -> SourceLauncherSupervisor:
        _require_state_root(self.root, self.roots)
        if self.path.exists() or self.root in _RETAINED_OWNERS or self.root in _RETAINED_FENCES:
            raise SandboxRecoveryRequiredError("sandbox recovery hold blocks execution")
        self.state_identity = _identity(self.root)
        self.baseline = _acl_census(
            self.roots, deadline=self.deadline, maximum_members=self.maximum_members
        )
        self.record = json.dumps(
            {
                "schema_version": "filing-xbrl-recovery/v1",
                "profile": "EarningsSummaryFilingXbrlV2",
                "invocation_id": self.nonce,
                "installation_id": self.installation,
                "root_identity": list(self.state_identity),
                "controller_pid": os.getpid(),
                "protected_roots": [str(path) for path in self.roots],
                "acl_baseline": self.baseline,
            },
            sort_keys=True,
        ).encode()
        if len(self.record) > 96 * 1024 * 1024:
            raise SandboxRecoveryRequiredError("sandbox recovery record byte cap exceeded")
        if _identity(self.root) != self.state_identity:
            raise SandboxRecoveryRequiredError("sandbox state root changed before hold creation")
        with self.path.open("xb") as stream:
            stream.write(self.record)
            stream.flush()
            os.fsync(stream.fileno())
            info = os.fstat(stream.fileno())
            self.marker_identity = (info.st_dev, info.st_ino)
        if (
            _identity(self.root) != self.state_identity
            or _identity(self.path) != self.marker_identity
        ):
            raise SandboxRecoveryRequiredError("sandbox hold identity changed during creation")
        return self

    def __exit__(self, *_exc: object) -> None:
        # A marker is never removed merely because a Python scope unwound.
        return None

    def complete(self) -> None:
        """Caller invokes only after its final runtime/input byte checks pass."""
        if (
            not self.clean
            or not self.stop_proved
            or _acl_census(
                self.roots, deadline=time.monotonic() + 30, maximum_members=self.maximum_members
            )
            != self.baseline
        ):
            raise SandboxRecoveryRequiredError("sandbox cleanup or ACL parity is unproved")
        if (
            _identity(self.root) != self.state_identity
            or self.path.stat().st_size != len(self.record)
            or self.path.read_bytes() != self.record
            or _identity(self.path) != self.marker_identity
        ):
            raise SandboxRecoveryRequiredError("sandbox recovery record ownership changed")
        _identity(self.path)
        self.path.unlink()

    def run(
        self,
        command: Sequence[str],
        payload: bytes,
        *,
        environment: Mapping[str, str],
        timeout_seconds: int,
        maximum_stdout_bytes: int,
        maximum_stderr_bytes: int,
    ) -> tuple[int, bytes, bytes]:
        if sys.platform != "win32":
            raise SandboxRecoveryRequiredError("Windows source supervision unavailable")
        deadline = self.deadline
        if time.monotonic() >= deadline:
            raise SandboxRecoveryRequiredError("sandbox preparation exhausted its deadline")
        control = _Control()
        process = None
        job = None
        threads: list[threading.Thread] = []
        started_threads: list[threading.Thread] = []
        overflow = threading.Event()
        chunks: dict[str, list[bytes]] = {"stdout": [], "stderr": [], "ack": []}
        forced = False
        cancelled = False
        self.stop_proved = False

        def drain(name: str, stream: BinaryIO, limit: int) -> None:
            total = 0
            try:
                while data := stream.read(65536):
                    available = max(0, limit - total)
                    if available:
                        chunks[name].append(data[:available])
                    total += len(data)
                    if total > limit:
                        overflow.set()
            except OSError:
                overflow.set()

        def write(stream: BinaryIO) -> None:
            try:
                stream.write(payload)
                stream.close()
            except OSError:
                overflow.set()

        try:
            separator = command.index("--")
            augmented = [
                *command[:separator],
                "--timeout-seconds",
                str(timeout_seconds),
                "--cancel-handle",
                str(control.cancel),
                "--cleanup-handle",
                str(control.write),
                "--invocation-id",
                self.nonce,
                "--installation-id",
                self.installation,
                *command[separator:],
            ]
            startup = subprocess.STARTUPINFO()
            startup.lpAttributeList = {"handle_list": [control.cancel, control.write]}
            process = subprocess.Popen(
                augmented,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=dict(environment),
                creationflags=0x4,
                close_fds=True,
                startupinfo=startup,
            )
            job = WindowsKillOnCloseJob.create_for_process(process.pid)
            control.close_write()
            resume_suspended_process(process.pid)
            assert process.stdin and process.stdout and process.stderr
            for name, stream, limit in (
                ("stdout", process.stdout, maximum_stdout_bytes),
                ("stderr", process.stderr, maximum_stderr_bytes),
                ("ack", control.reader, 1024),
            ):
                thread = threading.Thread(target=drain, args=(name, stream, limit), daemon=True)
                threads.append(thread)
                try:
                    thread.start()
                finally:
                    if thread.ident is not None:
                        started_threads.append(thread)
            thread = threading.Thread(target=write, args=(process.stdin,), daemon=True)
            threads.append(thread)
            try:
                thread.start()
            finally:
                if thread.ident is not None:
                    started_threads.append(thread)
            while process.poll() is None:
                if overflow.is_set() or time.monotonic() >= deadline:
                    cancelled = True
                    control.signal()
                    break
                time.sleep(0.02)
            if cancelled:
                try:
                    process.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    forced = True
            if forced:
                job.stop_and_wait(terminate=True)
            else:
                try:
                    job.stop_and_wait()
                except OSError:
                    forced = True
                    job.stop_and_wait(terminate=True)
            self.stop_proved = True
            process.wait(timeout=5)
            for thread in started_threads:
                thread.join(timeout=2)
            if any(thread.is_alive() for thread in threads):
                raise SandboxRecoveryRequiredError("sandbox stdio writers did not stop")
            self.clean = not forced and _acknowledged(
                b"".join(chunks["ack"]), self.nonce, self.installation
            )
            if not self.clean:
                raise SandboxRecoveryRequiredError("sandbox cleanup acknowledgement is missing")
            if cancelled or overflow.is_set():
                raise SandboxRecoveryRequiredError("sandbox cancelled; recovery hold retained")
            return process.returncode, b"".join(chunks["stdout"]), b"".join(chunks["stderr"])
        finally:
            cleanup_finished = False

            def attempt(stage: CleanupStage, action: Callable[[], object]) -> bool:
                try:
                    action()
                except BaseException as error:
                    self.cleanup_errors.append(SupervisorCleanupError(stage, type(error).__name__))
                    return False
                return True

            try:
                if job is not None and not self.stop_proved:
                    if attempt("job-stop", lambda: job.stop_and_wait(terminate=True)):
                        self.stop_proved = True
                elif process is not None and job is None:
                    # No launcher instructions ran: assignment failed while suspended.
                    attempt("suspended-root-kill", process.kill)
                    if attempt("suspended-root-wait", lambda: process.wait(timeout=5)):
                        self.stop_proved = True
                if job is not None and self.stop_proved:
                    attempt("job-close", job.close)
                if self.stop_proved:
                    for thread in started_threads:
                        attempt("thread-drain", partial(thread.join, timeout=2))
                    if any(thread.is_alive() for thread in started_threads):
                        self.cleanup_errors.append(
                            SupervisorCleanupError("thread-drain", "ThreadStillRunning")
                        )
                    elif process is not None:
                        for stream in (process.stdin, process.stdout, process.stderr):
                            if stream is not None:
                                attempt("stdio-close", stream.close)
                        attempt("control-close", control.close)
                cleanup_finished = True
            finally:
                # This registration must survive every stop/wait/close/join failure.
                if (
                    not cleanup_finished
                    or self.cleanup_errors
                    or not self.stop_proved
                    or not self.clean
                ):
                    self.clean = False
                    self.retained_resources = (job, control, process, *threads)
                    if self.root not in _RETAINED_OWNERS:
                        _RETAINED_OWNERS[self.root] = self
            if self.cleanup_errors:
                raise SandboxRecoveryRequiredError(
                    "sandbox cleanup failed; owner resources retained"
                )
