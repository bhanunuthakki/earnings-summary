"""Windows AppContainer launcher for the sealed offline report worker.

The child starts with an AppContainer token and no capabilities, so network
and filesystem access are denied by the kernel rather than Python shims.  Only
the qualified Python runtime and staged repository receive read access; one
private result tree receives modify access.
"""

from __future__ import annotations

import ctypes
import json
import os
import subprocess
import sys
import uuid
from collections.abc import Generator, Mapping, Sequence
from contextlib import contextmanager
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path

from log_redact import redact
from report.offline_artifact import OfflineBoundaryError
from runtime.windows_appcontainer import (
    Ace,
    AppContainerError,
    JobExtendedLimitInformation,
    NativeApi,
    ProcessInformation,
    SecurityCapabilities,
    StartupInfoEx,
)

if sys.platform == "win32":
    import msvcrt
else:
    msvcrt = None

_EXTENDED_STARTUPINFO_PRESENT = 0x00080000
_CREATE_NO_WINDOW = 0x08000000
_CREATE_SUSPENDED = 0x00000004
_CREATE_UNICODE_ENVIRONMENT = 0x00000400
_STARTF_USESTDHANDLES = 0x00000100
_PROC_THREAD_ATTRIBUTE_HANDLE_LIST = 0x00020002
_PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES = 0x00020009
_TOKEN_QUERY = 0x0008
_TOKEN_IS_APP_CONTAINER = 29
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_JOB_OBJECT_LIMIT_ACTIVE_PROCESS = 0x00000008
_WAIT_OBJECT_0 = 0
_WAIT_ABANDONED = 0x00000080
_WAIT_TIMEOUT = 0x00000102


@dataclass(frozen=True)
class _EphemeralProfile:
    name: str
    sid_pointer: int
    sid_text: str
    storage_root: Path


class _ContainmentCleanupUncertainError(OfflineBoundaryError):
    """Keep recovery state alive when containment cleanup is not proved."""

    def __init__(self, message: str, *, retained_resources: Sequence[object] = ()) -> None:
        self.retained_resources = tuple(retained_resources)
        super().__init__(message)


class _ReportSecurityAttributes(ctypes.Structure):
    _fields_ = [
        ("length", wintypes.DWORD),
        ("descriptor", ctypes.c_void_p),
        ("inherit", wintypes.BOOL),
    ]


class _ReportPrivateDesktop:
    """Own a hidden, non-inheritable desktop for the offline report worker.

    Security policy mirrors the frozen runtime owner at SHA-256
    f9f9d8f14fd8bc331cf2876776fa69e251ade5c6a115dc88dca8718a20ab42b7.
    Keep owner, protected DACL, low label, inheritance, restoration, and
    closure checks in parity with that source.
    """

    def __init__(
        self, native: NativeApi, package: str, owned: list[_ReportPrivateDesktop] | None = None
    ) -> None:
        self.n = native
        self.name = "EarningsSummaryOffline_" + uuid.uuid4().hex
        self.path = self.name + r"\Processor"
        self.station: object | None = None
        self.desktop: object | None = None
        self.context_restored = True
        if owned is not None:
            owned.append(self)
        owner = native.owner_sid()
        station = native.check(native.u.GetProcessWindowStation(), "original station")
        desktop = native.check(
            native.u.GetThreadDesktop(native.k.GetCurrentThreadId()), "original desktop"
        )
        prefix = f"O:{owner}G:{owner}D:P(A;;GA;;;{owner})(A;;"
        suffix = f";;;{package})S:(ML;;NW;;;LW)"
        try:
            with native.descriptor(prefix + "0x00020023" + suffix) as descriptor:
                attributes = _ReportSecurityAttributes(
                    ctypes.sizeof(_ReportSecurityAttributes), descriptor, False
                )
                self.station = native.check(
                    native.u.CreateWindowStationW(self.name, 1, 0x2000B, ctypes.byref(attributes)),
                    "private station creation",
                )
            self._verify(self.station, owner, package, 0x20023, station=True)
            self.context_restored = False
            native.check(
                native.u.SetProcessWindowStation(self.station), "private station selection"
            )
            with native.descriptor(prefix + "0x00020087" + suffix) as descriptor:
                attributes = _ReportSecurityAttributes(
                    ctypes.sizeof(_ReportSecurityAttributes), descriptor, False
                )
                self.desktop = native.check(
                    native.u.CreateDesktopW(
                        "Processor", None, None, 0, 0x20083, ctypes.byref(attributes)
                    ),
                    "private desktop creation",
                )
            self._verify(self.desktop, owner, package, 0x20087, station=False)
            self._restore(station, desktop)
        except BaseException:
            self._restore(station, desktop)
            self.close()
            raise

    def _restore(self, station: object, desktop: object) -> None:
        self.context_restored = False
        n = self.n
        n.check(n.u.SetProcessWindowStation(station), "creator station restoration")
        n.check(n.u.SetThreadDesktop(desktop), "creator desktop restoration")
        if (
            n.u.GetProcessWindowStation() != station
            or n.u.GetThreadDesktop(n.k.GetCurrentThreadId()) != desktop
        ):
            raise AppContainerError("creator_ui_context_restoration_unproved")
        self.context_restored = True

    def _verify(
        self, handle: object, owner: str, package: str, mask: int, *, station: bool
    ) -> None:
        actual_owner, protected, entries, labels = self.n.security(handle, 7, labels=True)
        if not protected:
            raise AppContainerError("private_ui_dacl_is_unprotected")
        _require_report_ui_security(actual_owner, owner, package, mask, entries, labels)
        flags = (wintypes.DWORD * 3)()
        needed = wintypes.DWORD()
        self.n.check(
            self.n.u.GetUserObjectInformationW(
                handle, 1, flags, ctypes.sizeof(flags), ctypes.byref(needed)
            ),
            "private UI flags",
        )
        if needed.value != ctypes.sizeof(flags) or flags[0] or (station and flags[2]):
            raise AppContainerError("private_ui_is_visible_or_inheritable")

    def close(self) -> None:
        if sys.platform != "win32":
            raise AppContainerError("private_ui_requires_windows")
        if not self.context_restored:
            raise AppContainerError("creator_ui_context_restoration_unproved")
        n = self.n
        if self.desktop:
            n.check(n.u.CloseDesktop(self.desktop), "private desktop close")
            self.desktop = None
        if self.station:
            n.check(n.u.CloseWindowStation(self.station), "private station close")
            self.station = None
            ctypes.set_last_error(0)
            unexpected = n.u.OpenWindowStationW(self.name, False, 0x20000)
            error = int(ctypes.get_last_error())
            if unexpected:
                n.check(n.u.CloseWindowStation(unexpected), "unexpected private station close")
                raise AppContainerError("private_station_remains")
            if error != 2:
                raise AppContainerError("private_station_absence_unproved")


def _require_report_low_label(entries: Sequence[Ace]) -> None:
    if len(entries) != 1:
        raise AppContainerError("private_ui_label_count_changed")
    ace = entries[0]
    if (ace.kind, ace.flags, ace.mask, ace.sid, len(ace.raw)) != (
        0x11,
        0,
        1,
        "S-1-16-4096",
        20,
    ):
        raise AppContainerError("private_ui_low_mandatory_label_changed")


def _require_report_ui_security(
    owner: str,
    expected_owner: str,
    package: str,
    package_mask: int,
    entries: Sequence[Ace],
    labels: Sequence[Ace],
) -> None:
    if owner != expected_owner or len(entries) != 2:
        raise AppContainerError("private_ui_ownership_changed")
    owner_mask = 0xF037F if package_mask == 0x20023 else 0xF01FF
    if {ace.sid for ace in entries} != {owner, package} or any(
        ace.kind != 0
        or ace.flags != 0
        or (ace.sid == owner and ace.mask != owner_mask)
        or (ace.sid == package and ace.mask != package_mask)
        for ace in entries
    ):
        raise AppContainerError("private_ui_grants_changed")
    _require_report_low_label(labels)


def _win_error(stage: str) -> OfflineBoundaryError:
    if sys.platform != "win32":
        return OfflineBoundaryError(f"AppContainer {stage} is unavailable outside Windows")
    return OfflineBoundaryError(f"AppContainer {stage} failed ({ctypes.get_last_error()})")


def _system_binary(name: str) -> Path:
    if sys.platform != "win32":
        raise OfflineBoundaryError("AppContainer system binaries require Windows")
    buffer = ctypes.create_unicode_buffer(32_768)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    length = kernel32.GetSystemDirectoryW(buffer, len(buffer))
    if length == 0 or length >= len(buffer):
        raise _win_error("system-directory lookup")
    return Path(buffer.value) / name


def _create_ephemeral_profile() -> _EphemeralProfile:
    if sys.platform != "win32":
        raise OfflineBoundaryError("AppContainer profiles require Windows")
    userenv = ctypes.WinDLL("userenv", use_last_error=True)
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    sid = ctypes.c_void_p()
    create = userenv.CreateAppContainerProfile
    create.argtypes = [
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p),
    ]
    create.restype = ctypes.c_long
    profile_name = f"EarningsSummaryOffline.{uuid.uuid4().hex}"
    result = (
        int(
            create(
                profile_name,
                "Earnings Summary Offline Report",
                "Network-denied deterministic report renderer",
                None,
                0,
                ctypes.byref(sid),
            )
        )
        & 0xFFFFFFFF
    )
    if result != 0 or not sid.value:
        raise OfflineBoundaryError(f"AppContainer profile is unavailable ({result})")
    sid_text = wintypes.LPWSTR()
    convert = advapi32.ConvertSidToStringSidW
    convert.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR)]
    convert.restype = wintypes.BOOL
    if not convert(sid, ctypes.byref(sid_text)):
        advapi32.FreeSid(sid)
        raise _win_error("SID conversion")
    try:
        rendered = sid_text.value
    finally:
        kernel32.LocalFree(sid_text)
    if rendered is None:
        advapi32.FreeSid(sid)
        raise OfflineBoundaryError("AppContainer SID conversion returned no text")
    storage = wintypes.LPWSTR()
    get_storage = userenv.GetAppContainerFolderPath
    get_storage.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(wintypes.LPWSTR)]
    get_storage.restype = ctypes.c_long
    storage_result = int(get_storage(rendered, ctypes.byref(storage))) & 0xFFFFFFFF
    if storage_result != 0 or not storage.value:
        advapi32.FreeSid(sid)
        raise OfflineBoundaryError(
            f"AppContainer storage identity is unavailable ({storage_result})"
        )
    try:
        storage_root = Path(storage.value).resolve()
    finally:
        ctypes.windll.ole32.CoTaskMemFree(storage)
    return _EphemeralProfile(
        name=profile_name,
        sid_pointer=int(sid.value),
        sid_text=rendered,
        storage_root=storage_root,
    )


def _delete_ephemeral_profile(profile: _EphemeralProfile) -> None:
    if sys.platform != "win32":
        raise OfflineBoundaryError("AppContainer profiles require Windows")
    userenv = ctypes.WinDLL("userenv", use_last_error=True)
    delete = userenv.DeleteAppContainerProfile
    delete.argtypes = [wintypes.LPCWSTR]
    delete.restype = ctypes.c_long
    errors: list[int] = []
    for _attempt in range(2):
        result = int(delete(profile.name)) & 0xFFFFFFFF
        if result == 0 and not profile.storage_root.exists():
            return
        errors.append(result)
    storage_state = "present" if profile.storage_root.exists() else "absent"
    raise OfflineBoundaryError(
        "AppContainer profile cleanup failed; recovery required: "
        f"profile={profile.name}; sid={profile.sid_text}; storage={profile.storage_root}; "
        f"storage_state={storage_state}; delete_results={errors}"
    )


def _acl(sid: str, path: Path, rights: str | None) -> None:
    icacls = _system_binary("icacls.exe")
    command = [str(icacls), str(path)]
    if rights is None:
        command.extend(["/remove:g", f"*{sid}", "/Q"])
    else:
        command.extend(["/grant", f"*{sid}:(OI)(CI)({rights})", "/Q"])
    completed = subprocess.run(command, capture_output=True, text=True, check=False)
    if completed.returncode != 0:
        action = "grant" if rights is not None else "revoke"
        detail = redact((completed.stderr or completed.stdout or "unknown failure").strip())
        raise OfflineBoundaryError(
            f"AppContainer ACL {action} failed for {path} and SID {sid}: {detail[-1_000:]}"
        )
    inspected = subprocess.run(
        [str(icacls), str(path)], capture_output=True, text=True, check=False
    )
    if inspected.returncode != 0:
        raise OfflineBoundaryError(f"AppContainer ACL inspection failed for {path}")
    sid_present = sid.casefold() in inspected.stdout.casefold()
    if (rights is not None) != sid_present:
        expected = "present" if rights is not None else "absent"
        raise OfflineBoundaryError(
            f"AppContainer ACL postcondition failed for {path}: SID {sid} must be {expected}"
        )


@contextmanager
def _ephemeral_profile() -> Generator[_EphemeralProfile, None, None]:
    if sys.platform != "win32":
        raise OfflineBoundaryError("AppContainer profiles require Windows")
    profile = _create_ephemeral_profile()
    cleanup_unproven = False
    try:
        yield profile
    except _ContainmentCleanupUncertainError as exc:
        cleanup_unproven = True
        raise _ContainmentCleanupUncertainError(
            "AppContainer containment cleanup is unproved; profile identity and storage are "
            "retained for recovery: "
            f"profile={profile.name}; sid={profile.sid_text}; storage={profile.storage_root}; "
            f"cause={exc}",
            retained_resources=(*exc.retained_resources, profile),
        ) from exc
    finally:
        if not cleanup_unproven:
            _delete_ephemeral_profile(profile)
            ctypes.WinDLL("advapi32", use_last_error=True).FreeSid(
                ctypes.c_void_p(profile.sid_pointer)
            )


def _remove_and_verify_loopback_exemption(sid: str) -> None:
    binary = _system_binary("CheckNetIsolation.exe")
    removed = subprocess.run(
        [str(binary), "LoopbackExempt", "-d", f"-p={sid}"],
        capture_output=True,
        text=True,
        check=False,
    )
    if removed.returncode != 0:
        detail = redact((removed.stderr or removed.stdout or "unknown failure").strip())
        raise OfflineBoundaryError(f"AppContainer loopback exemption removal failed: {detail}")
    listed = subprocess.run(
        [str(binary), "LoopbackExempt", "-s"],
        capture_output=True,
        text=True,
        check=False,
    )
    if listed.returncode != 0 or sid.casefold() in listed.stdout.casefold():
        raise OfflineBoundaryError(f"AppContainer loopback exemption persists for SID {sid}")


def _environment_block(environment: Mapping[str, str]) -> ctypes.Array[ctypes.c_wchar]:
    items = [
        f"{key}={value}" for key, value in sorted(environment.items(), key=lambda x: x[0].upper())
    ]
    return ctypes.create_unicode_buffer("\0".join(items) + "\0\0")


def is_current_process_appcontainer() -> bool:
    if sys.platform != "win32":
        return False
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32.GetCurrentProcess.argtypes = []
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    advapi32.OpenProcessToken.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.HANDLE),
    ]
    advapi32.OpenProcessToken.restype = wintypes.BOOL
    advapi32.GetTokenInformation.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    advapi32.GetTokenInformation.restype = wintypes.BOOL
    token = wintypes.HANDLE()
    if not advapi32.OpenProcessToken(
        kernel32.GetCurrentProcess(), _TOKEN_QUERY, ctypes.byref(token)
    ):
        raise _win_error("token inspection")
    try:
        value = wintypes.DWORD()
        returned = wintypes.DWORD()
        if not advapi32.GetTokenInformation(
            token,
            _TOKEN_IS_APP_CONTAINER,
            ctypes.byref(value),
            ctypes.sizeof(value),
            ctypes.byref(returned),
        ):
            raise _win_error("token verification")
        return value.value == 1
    finally:
        kernel32.CloseHandle(token)


def _run_appcontainer_worker_with_acl(
    command: Sequence[str],
    *,
    profile: _EphemeralProfile,
    cwd: Path,
    read_roots: Sequence[Path],
    write_root: Path,
    environment: Mapping[str, str],
    timeout_seconds: int = 300,
) -> tuple[int, str, str]:
    if sys.platform != "win32":
        raise OfflineBoundaryError("sealed offline rendering requires Windows AppContainer")
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32.InitializeProcThreadAttributeList.argtypes = [
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.c_size_t),
    ]
    kernel32.InitializeProcThreadAttributeList.restype = wintypes.BOOL
    kernel32.UpdateProcThreadAttribute.argtypes = [
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.c_size_t,
        ctypes.c_void_p,
        ctypes.c_size_t,
        ctypes.c_void_p,
        ctypes.c_void_p,
    ]
    kernel32.UpdateProcThreadAttribute.restype = wintypes.BOOL
    kernel32.DeleteProcThreadAttributeList.argtypes = [ctypes.c_void_p]
    kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.SetInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    kernel32.SetInformationJobObject.restype = wintypes.BOOL
    kernel32.CreateProcessW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.LPWSTR,
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.BOOL,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.LPCWSTR,
        ctypes.POINTER(StartupInfoEx),
        ctypes.POINTER(ProcessInformation),
    ]
    kernel32.CreateProcessW.restype = wintypes.BOOL
    kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel32.ResumeThread.argtypes = [wintypes.HANDLE]
    kernel32.ResumeThread.restype = wintypes.DWORD
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel32.TerminateProcess.restype = wintypes.BOOL
    kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    advapi32.OpenProcessToken.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.HANDLE),
    ]
    advapi32.OpenProcessToken.restype = wintypes.BOOL
    advapi32.GetTokenInformation.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    advapi32.GetTokenInformation.restype = wintypes.BOOL
    sid_pointer = profile.sid_pointer
    sid_text = profile.sid_text
    roots = tuple(dict.fromkeys(Path(os.path.abspath(path)) for path in read_roots))
    write_root = Path(os.path.abspath(write_root))
    for root in (*roots, write_root):
        if not root.is_dir():
            raise OfflineBoundaryError(f"AppContainer root is unavailable: {root}")
    granted: list[Path] = []
    process = ProcessInformation()
    attribute_list = ctypes.c_void_p()
    job = wintypes.HANDLE()
    assigned_to_job = False
    owned_private_uis: list[_ReportPrivateDesktop] = []
    handles: list[int] = []
    stdout_path = write_root / "worker.stdout"
    stderr_path = write_root / "worker.stderr"
    try:
        for root in roots:
            _acl(sid_text, root, "RX")
            granted.append(root)

        private_ui = _ReportPrivateDesktop(NativeApi(), sid_text, owned_private_uis)

        stdin_fd = os.open(os.devnull, os.O_RDONLY)
        stdout_fd = os.open(stdout_path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC)
        stderr_fd = os.open(stderr_path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC)
        handles = [msvcrt.get_osfhandle(fd) for fd in (stdin_fd, stdout_fd, stderr_fd)]
        for handle in handles:
            os.set_handle_inheritable(handle, True)

        size = ctypes.c_size_t()
        kernel32.InitializeProcThreadAttributeList(None, 2, 0, ctypes.byref(size))
        attribute_buffer = ctypes.create_string_buffer(size.value)
        attribute_list = ctypes.cast(attribute_buffer, ctypes.c_void_p)
        if not kernel32.InitializeProcThreadAttributeList(attribute_list, 2, 0, ctypes.byref(size)):
            raise _win_error("attribute-list initialization")
        capabilities = SecurityCapabilities(sid_pointer, None, 0, 0)
        if not kernel32.UpdateProcThreadAttribute(
            attribute_list,
            0,
            _PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES,
            ctypes.byref(capabilities),
            ctypes.sizeof(capabilities),
            None,
            None,
        ):
            raise _win_error("security-capability assignment")
        handle_array = (wintypes.HANDLE * len(handles))(*handles)
        if not kernel32.UpdateProcThreadAttribute(
            attribute_list,
            0,
            _PROC_THREAD_ATTRIBUTE_HANDLE_LIST,
            handle_array,
            ctypes.sizeof(handle_array),
            None,
            None,
        ):
            raise _win_error("handle allowlist assignment")

        limits = JobExtendedLimitInformation()
        limits.BasicLimitInformation.LimitFlags = (
            _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE | _JOB_OBJECT_LIMIT_ACTIVE_PROCESS
        )
        limits.BasicLimitInformation.ActiveProcessLimit = 1
        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            raise _win_error("job creation")
        if not kernel32.SetInformationJobObject(
            job, _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION, ctypes.byref(limits), ctypes.sizeof(limits)
        ):
            raise _win_error("job configuration")

        startup = StartupInfoEx()
        startup.StartupInfo.cb = ctypes.sizeof(startup)
        startup.StartupInfo.dwFlags = _STARTF_USESTDHANDLES
        startup.StartupInfo.lpDesktop = private_ui.path
        startup.StartupInfo.hStdInput = handles[0]
        startup.StartupInfo.hStdOutput = handles[1]
        startup.StartupInfo.hStdError = handles[2]
        startup.lpAttributeList = attribute_list
        command_line = ctypes.create_unicode_buffer(subprocess.list2cmdline(list(command)))
        environment_block = _environment_block(environment)
        created = kernel32.CreateProcessW(
            str(Path(command[0]).resolve()),
            command_line,
            None,
            None,
            True,
            _EXTENDED_STARTUPINFO_PRESENT
            | _CREATE_NO_WINDOW
            | _CREATE_SUSPENDED
            | _CREATE_UNICODE_ENVIRONMENT,
            environment_block,
            str(cwd),
            ctypes.byref(startup),
            ctypes.byref(process),
        )
        if not created:
            raise _win_error("process creation")
        if not kernel32.AssignProcessToJobObject(job, process.hProcess):
            raise _win_error("job assignment")
        assigned_to_job = True
        token = wintypes.HANDLE()
        if not advapi32.OpenProcessToken(process.hProcess, _TOKEN_QUERY, ctypes.byref(token)):
            raise _win_error("child-token inspection")
        try:
            value = wintypes.DWORD()
            returned = wintypes.DWORD()
            if (
                not advapi32.GetTokenInformation(
                    token,
                    _TOKEN_IS_APP_CONTAINER,
                    ctypes.byref(value),
                    ctypes.sizeof(value),
                    ctypes.byref(returned),
                )
                or value.value != 1
            ):
                raise OfflineBoundaryError("child token is not an AppContainer")
        finally:
            kernel32.CloseHandle(token)
        if kernel32.ResumeThread(process.hThread) == 0xFFFFFFFF:
            raise _win_error("child resume")
        kernel32.CloseHandle(process.hThread)
        process.hThread = None
        wait = kernel32.WaitForSingleObject(process.hProcess, timeout_seconds * 1000)
        if wait != _WAIT_OBJECT_0:
            kernel32.TerminateProcess(process.hProcess, 124)
            if kernel32.WaitForSingleObject(process.hProcess, 5_000) != _WAIT_OBJECT_0:
                raise OfflineBoundaryError("AppContainer worker stop was not proved after timeout")
            detail = redact(stderr_path.read_text(encoding="utf-8", errors="replace").strip())
            raise OfflineBoundaryError(
                f"AppContainer worker timed out{': ' + detail[-8_000:] if detail else ''}"
            )
        exit_code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(process.hProcess, ctypes.byref(exit_code)):
            raise _win_error("exit-status read")
    finally:
        for fd in (locals().get("stdin_fd"), locals().get("stdout_fd"), locals().get("stderr_fd")):
            if isinstance(fd, int):
                os.close(fd)
        if attribute_list:
            kernel32.DeleteProcThreadAttributeList(attribute_list)
        cleanup_errors: list[str] = []
        child_stopped = not bool(process.hProcess)
        if process.hProcess and not assigned_to_job:
            kernel32.TerminateProcess(process.hProcess, 127)
        if job:
            if not kernel32.CloseHandle(job):
                cleanup_errors.append("owned Job close failed")
            else:
                job = wintypes.HANDLE()
        if process.hProcess:
            wait = kernel32.WaitForSingleObject(process.hProcess, 5_000)
            if wait != _WAIT_OBJECT_0:
                kernel32.TerminateProcess(process.hProcess, 127)
                wait = kernel32.WaitForSingleObject(process.hProcess, 5_000)
            if wait != _WAIT_OBJECT_0:
                cleanup_errors.append("owned child stop was not proved")
            else:
                child_stopped = True
        if not child_stopped:
            # Keep the private UI and root read grants alive while an un-stopped
            # process could still depend on them. Preserve the profile and native
            # owners so the caller can recover without deleting their identity.
            raise _ContainmentCleanupUncertainError(
                "AppContainer child cleanup failed; containment remains active: "
                + " | ".join(cleanup_errors),
                retained_resources=(profile, process, job, owned_private_uis),
            )
        if process.hThread:
            if not kernel32.CloseHandle(process.hThread):
                cleanup_errors.append("child thread handle close failed")
            else:
                process.hThread = None
        if process.hProcess:
            if not kernel32.CloseHandle(process.hProcess):
                cleanup_errors.append("child process handle close failed")
            else:
                process.hProcess = None
        private_ui_errors: list[str] = []
        for owned_private_ui in reversed(owned_private_uis):
            try:
                owned_private_ui.close()
            except AppContainerError as exc:
                private_ui_errors.append(f"private UI cleanup failed: {exc}")
        if private_ui_errors:
            raise _ContainmentCleanupUncertainError(
                "AppContainer private UI cleanup failed; read grants remain active: "
                + " | ".join([*cleanup_errors, *private_ui_errors]),
                retained_resources=(profile, process, job, owned_private_uis),
            )
        for root in reversed(granted):
            try:
                _acl(sid_text, root, None)
            except OfflineBoundaryError as exc:
                cleanup_errors.append(str(exc))
        if cleanup_errors:
            raise _ContainmentCleanupUncertainError(
                "AppContainer ACL or handle cleanup failed; recovery required: "
                + " | ".join(cleanup_errors),
                retained_resources=(profile, process, job, owned_private_uis),
            )
    stdout = (
        stdout_path.read_text(encoding="utf-8", errors="replace") if stdout_path.exists() else ""
    )
    stderr = (
        stderr_path.read_text(encoding="utf-8", errors="replace") if stderr_path.exists() else ""
    )
    return int(exit_code.value), stdout, stderr


def _substitute_private_write(value: str, source: Path, target: Path) -> str:
    substituted = value.replace(str(source), str(target))
    if os.name != "nt":
        return substituted
    source_drive_less = str(source)[len(source.drive) :]
    target_drive_less = str(target)[len(target.drive) :]
    return substituted.replace(source_drive_less, target_drive_less)


def run_appcontainer_worker(
    command: Sequence[str],
    *,
    cwd: Path,
    read_roots: Sequence[Path],
    write_root: Path,
    environment: Mapping[str, str],
    timeout_seconds: int = 300,
    export_names: Sequence[str] = (),
) -> tuple[int, str, str]:
    """Run one child with ephemeral identity and package-private writes."""

    export_root = Path(os.path.abspath(write_root))
    export_root.mkdir(parents=True, exist_ok=True)
    with _ephemeral_profile() as profile:
        storage_root = profile.storage_root
        if not storage_root.is_dir():
            raise OfflineBoundaryError(
                f"AppContainer private storage is unavailable: {storage_root}"
            )
        _remove_and_verify_loopback_exemption(profile.sid_text)
        sealed_command = tuple(
            _substitute_private_write(value, export_root, storage_root) for value in command
        )
        sealed_environment = {
            key: _substitute_private_write(value, export_root, storage_root)
            for key, value in environment.items()
        }
        returncode, stdout, stderr = _run_appcontainer_worker_with_acl(
            sealed_command,
            profile=profile,
            cwd=cwd,
            read_roots=read_roots,
            write_root=storage_root,
            environment=sealed_environment,
            timeout_seconds=timeout_seconds,
        )
        if returncode == 0:
            for name in export_names:
                relative = Path(name)
                if relative.is_absolute() or ".." in relative.parts or len(relative.parts) != 1:
                    raise OfflineBoundaryError(f"invalid AppContainer export name: {name}")
                source = storage_root / relative
                destination = export_root / relative
                if not source.exists() or destination.exists():
                    raise OfflineBoundaryError(f"AppContainer export is unavailable: {name}")
                source.replace(destination)
        profile_name = profile.name
        profile_sid = profile.sid_text
        storage_path = str(profile.storage_root)
    cleanup_receipt = json.dumps(
        {
            "acl_roots": [str(Path(os.path.abspath(path))) for path in read_roots],
            "event": "appcontainer_profile_cleanup",
            "profile": profile_name,
            "sid": profile_sid,
            "status": "deleted",
            "storage": storage_path,
            "storage_removed": True,
        },
        sort_keys=True,
    )
    return returncode, stdout, stderr + cleanup_receipt + "\n"


def minimal_worker_environment(*, isolated_repo: Path, write_root: Path) -> dict[str, str]:
    """Return the complete, secret-free environment admitted to the worker."""

    system_root = os.environ.get("SYSTEMROOT", r"C:\Windows")
    system_drive = Path(system_root).drive or "C:"
    return {
        "APPDATA": str(write_root),
        "COMSPEC": str(Path(system_root) / "System32" / "cmd.exe"),
        "EARNINGS_OFFLINE_RENDER": "1",
        "HOMEDRIVE": Path(write_root).drive or system_drive,
        "HOMEPATH": str(write_root)[len(Path(write_root).drive) :],
        "LOCALAPPDATA": str(write_root),
        "LLM_FALLBACK_DISABLED": "1",
        "PATH": str(Path(sys.executable).parent),
        "PATHEXT": ".COM;.EXE;.BAT;.CMD",
        "PROGRAMDATA": os.environ.get(
            "PROGRAMDATA", str(Path(system_drive + "\\") / "ProgramData")
        ),
        "PROCESSOR_ARCHITECTURE": os.environ.get("PROCESSOR_ARCHITECTURE", "AMD64"),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1",
        "PYTHONPATH": str(isolated_repo / "src"),
        "SYSTEMROOT": system_root,
        "TEMP": str(write_root),
        "TMP": str(write_root),
        "USERPROFILE": str(write_root),
        "WINDIR": system_root,
    }


__all__ = [
    "is_current_process_appcontainer",
    "minimal_worker_environment",
    "run_appcontainer_worker",
]
