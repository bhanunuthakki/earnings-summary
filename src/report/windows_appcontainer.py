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
_INFINITE = 0xFFFFFFFF


@dataclass(frozen=True)
class _EphemeralProfile:
    name: str
    sid_pointer: int
    sid_text: str
    storage_root: Path


class _STARTUPINFO(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("lpReserved", wintypes.LPWSTR),
        ("lpDesktop", wintypes.LPWSTR),
        ("lpTitle", wintypes.LPWSTR),
        ("dwX", wintypes.DWORD),
        ("dwY", wintypes.DWORD),
        ("dwXSize", wintypes.DWORD),
        ("dwYSize", wintypes.DWORD),
        ("dwXCountChars", wintypes.DWORD),
        ("dwYCountChars", wintypes.DWORD),
        ("dwFillAttribute", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("wShowWindow", wintypes.WORD),
        ("cbReserved2", wintypes.WORD),
        ("lpReserved2", ctypes.POINTER(ctypes.c_byte)),
        ("hStdInput", wintypes.HANDLE),
        ("hStdOutput", wintypes.HANDLE),
        ("hStdError", wintypes.HANDLE),
    ]


class _STARTUPINFOEX(ctypes.Structure):
    _fields_ = [("StartupInfo", _STARTUPINFO), ("lpAttributeList", ctypes.c_void_p)]


class _PROCESS_INFORMATION(ctypes.Structure):  # noqa: N801 - Win32 ABI name
    _fields_ = [
        ("hProcess", wintypes.HANDLE),
        ("hThread", wintypes.HANDLE),
        ("dwProcessId", wintypes.DWORD),
        ("dwThreadId", wintypes.DWORD),
    ]


class _SECURITY_CAPABILITIES(ctypes.Structure):  # noqa: N801 - Win32 ABI name
    _fields_ = [
        ("AppContainerSid", ctypes.c_void_p),
        ("Capabilities", ctypes.c_void_p),
        ("CapabilityCount", wintypes.DWORD),
        ("Reserved", wintypes.DWORD),
    ]


class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):  # noqa: N801 - Win32 ABI name
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class _IO_COUNTERS(ctypes.Structure):  # noqa: N801 - Win32 ABI name
    _fields_ = [
        (name, ctypes.c_uint64)
        for name in (
            "ReadOperationCount",
            "WriteOperationCount",
            "OtherOperationCount",
            "ReadTransferCount",
            "WriteTransferCount",
            "OtherTransferCount",
        )
    ]


class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):  # noqa: N801 - Win32 ABI name
    _fields_ = [
        ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ("IoInfo", _IO_COUNTERS),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


def _win_error(stage: str) -> OfflineBoundaryError:
    return OfflineBoundaryError(f"AppContainer {stage} failed ({ctypes.get_last_error()})")


def _system_binary(name: str) -> Path:
    buffer = ctypes.create_unicode_buffer(32_768)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    length = kernel32.GetSystemDirectoryW(buffer, len(buffer))
    if length == 0 or length >= len(buffer):
        raise _win_error("system-directory lookup")
    return Path(buffer.value) / name


def _create_ephemeral_profile() -> _EphemeralProfile:
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
    profile = _create_ephemeral_profile()
    try:
        yield profile
    finally:
        try:
            _delete_ephemeral_profile(profile)
        finally:
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
    if os.name != "nt":
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
    if os.name != "nt":
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
        ctypes.POINTER(_STARTUPINFOEX),
        ctypes.POINTER(_PROCESS_INFORMATION),
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
    process = _PROCESS_INFORMATION()
    attribute_list = ctypes.c_void_p()
    job = wintypes.HANDLE()
    handles: list[int] = []
    stdout_path = write_root / "worker.stdout"
    stderr_path = write_root / "worker.stderr"
    try:
        for root in roots:
            _acl(sid_text, root, "RX")
            granted.append(root)

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
        capabilities = _SECURITY_CAPABILITIES(sid_pointer, None, 0, 0)
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

        limits = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
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

        startup = _STARTUPINFOEX()
        startup.StartupInfo.cb = ctypes.sizeof(startup)
        startup.StartupInfo.dwFlags = _STARTF_USESTDHANDLES
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
            kernel32.WaitForSingleObject(process.hProcess, _INFINITE)
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
        if process.hThread:
            kernel32.CloseHandle(process.hThread)
        if process.hProcess:
            kernel32.CloseHandle(process.hProcess)
        if attribute_list:
            kernel32.DeleteProcThreadAttributeList(attribute_list)
        if job:
            kernel32.CloseHandle(job)
        cleanup_errors: list[str] = []
        for root in reversed(granted):
            try:
                _acl(sid_text, root, None)
            except OfflineBoundaryError as exc:
                cleanup_errors.append(str(exc))
        if cleanup_errors:
            raise OfflineBoundaryError(
                "AppContainer ACL cleanup failed; recovery required: " + " | ".join(cleanup_errors)
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
