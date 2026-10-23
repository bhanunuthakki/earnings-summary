"""Stdlib-only Windows containment for the isolated filing processor.

This file can run beside the sealed launcher without importing the application.
Native operations fail closed. No profile deletion or host-policy changes occur.
"""

from __future__ import annotations

import ctypes
import os
import re
import struct
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Sequence
from contextlib import contextmanager
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path
from typing import Any

PROFILE_NAME = "EarningsSummaryFilingXbrlV2"
MUTEX_NAME = r"Global\EarningsSummaryFilingXbrlLauncherV2"
_SYSTEM = "S-1-5-18"
_ADMINISTRATORS = "S-1-5-32-544"
_READ_EXECUTE = 0x1200A9


class AppContainerError(RuntimeError):
    """A native containment invariant was not established."""

    def __init__(self, stage: str, *, winerror: int = 0, cleanup_proved: bool = False) -> None:
        if re.fullmatch(r"[a-z][a-z0-9_]*", stage) is None:
            raise ValueError("invalid containment stage")
        self.stage = stage
        self.winerror = int(winerror)
        self.cleanup_proved = cleanup_proved
        super().__init__(stage)


@dataclass(frozen=True)
class Ace:
    kind: int
    flags: int
    mask: int
    sid: str
    raw: bytes


def _sid_text(raw: bytes) -> str:
    if len(raw) < 8 or raw[0] != 1 or len(raw) != 8 + 4 * raw[1]:
        raise AppContainerError("invalid_sid_encoding")
    authority = int.from_bytes(raw[2:8], "big")
    parts = struct.unpack_from("<" + "I" * raw[1], raw, 8)
    return "S-1-" + str(authority) + "".join(f"-{part}" for part in parts)


def _parse_acl(raw: bytes) -> tuple[Ace, ...]:
    if len(raw) < 8:
        raise AppContainerError("acl_header_is_missing")
    revision, _, size, count, _ = struct.unpack_from("<BBHHH", raw)
    if revision not in (2, 4) or size != len(raw):
        raise AppContainerError("acl_header_is_invalid")
    offset = 8
    entries: list[Ace] = []
    for _ in range(count):
        if offset + 8 > size:
            raise AppContainerError("ace_header_exceeds_acl")
        kind, flags, length, mask = struct.unpack_from("<BBHI", raw, offset)
        if length < 8 or length % 4 or offset + length > size:
            raise AppContainerError("ace_length_is_invalid")
        data = raw[offset : offset + length]
        # Preserve unfamiliar ACEs byte-for-byte; they cannot satisfy our own grants.
        sid = _sid_text(data[8:]) if kind in (0, 1, 0x11) else ""
        entries.append(Ace(kind, flags, mask, sid, data))
        offset += length
    if any(raw[offset:]):
        raise AppContainerError("acl_trailing_bytes_are_nonzero")
    return tuple(entries)


def _require_mutex_security(owner: str, protected: bool, entries: Sequence[Ace]) -> None:
    if owner not in (_SYSTEM, _ADMINISTRATORS) or not protected or len(entries) != 2:
        raise AppContainerError("global_mutex_ownership_or_protection_changed")
    if {entry.sid for entry in entries} != {_SYSTEM, _ADMINISTRATORS} or any(
        entry.kind != 0 or entry.flags != 0 or entry.mask != 0x1F0001 for entry in entries
    ):
        raise AppContainerError("global_mutex_grants_changed")


def _require_low_label(entries: Sequence[Ace]) -> None:
    if len(entries) != 1:
        raise AppContainerError("private_ui_label_count_changed")
    ace = entries[0]
    if (ace.kind, ace.flags, ace.mask, ace.sid, len(ace.raw)) != (0x11, 0, 1, "S-1-16-4096", 20):
        raise AppContainerError("private_ui_low_mandatory_label_changed")


def _require_ui_security(
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
    if {a.sid for a in entries} != {owner, package} or any(
        a.kind != 0
        or a.flags != 0
        or (a.sid == owner and a.mask != owner_mask)
        or (a.sid == package and a.mask != package_mask)
        for a in entries
    ):
        raise AppContainerError("private_ui_grants_changed")
    _require_low_label(labels)


class StartupInfo(ctypes.Structure):
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


class StartupInfoEx(ctypes.Structure):
    _fields_ = [("StartupInfo", StartupInfo), ("lpAttributeList", ctypes.c_void_p)]


class ProcessInformation(ctypes.Structure):
    _fields_ = [
        ("hProcess", wintypes.HANDLE),
        ("hThread", wintypes.HANDLE),
        ("dwProcessId", wintypes.DWORD),
        ("dwThreadId", wintypes.DWORD),
    ]


class SecurityCapabilities(ctypes.Structure):
    _fields_ = [
        ("AppContainerSid", ctypes.c_void_p),
        ("Capabilities", ctypes.c_void_p),
        ("CapabilityCount", wintypes.DWORD),
        ("Reserved", wintypes.DWORD),
    ]


class JobBasicLimitInformation(ctypes.Structure):
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


class IoCounters(ctypes.Structure):
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


class JobExtendedLimitInformation(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", JobBasicLimitInformation),
        ("IoInfo", IoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class _SecurityAttributes(ctypes.Structure):
    _fields_ = [
        ("length", wintypes.DWORD),
        ("descriptor", ctypes.c_void_p),
        ("inherit", wintypes.BOOL),
    ]


class _JobAccounting(ctypes.Structure):
    _fields_ = [
        ("times", ctypes.c_int64 * 4),
        ("faults", wintypes.DWORD),
        ("total", wintypes.DWORD),
        ("active", wintypes.DWORD),
        ("terminated", wintypes.DWORD),
    ]


def _last_error() -> int:
    return int(vars(ctypes)["get_last_error"]())


class NativeApi:
    k: Any
    a: Any
    u: Any
    e: Any

    def __init__(self) -> None:
        if sys.platform != "win32":
            raise AppContainerError("windows_appcontainer_requires_windows")
        self.k = ctypes.WinDLL("kernel32", use_last_error=True)
        self.a = ctypes.WinDLL("advapi32", use_last_error=True)
        self.u = ctypes.WinDLL("user32", use_last_error=True)
        self.e = ctypes.WinDLL("userenv", use_last_error=True)
        handle_t, dword_t, pointer_t, bool_t, text_t = (
            wintypes.HANDLE,
            wintypes.DWORD,
            ctypes.c_void_p,
            wintypes.BOOL,
            wintypes.LPCWSTR,
        )
        signatures = [
            (self.k, "CloseHandle", bool_t, [handle_t]),
            (self.k, "LocalFree", pointer_t, [pointer_t]),
            (self.k, "GetCurrentProcess", handle_t, []),
            (self.k, "GetCurrentThreadId", dword_t, []),
            (self.k, "CreateMutexW", handle_t, [pointer_t, bool_t, text_t]),
            (self.k, "ReleaseMutex", bool_t, [handle_t]),
            (self.k, "WaitForSingleObject", dword_t, [handle_t, dword_t]),
            (self.k, "GetStdHandle", handle_t, [dword_t]),
            (
                self.k,
                "DuplicateHandle",
                bool_t,
                [handle_t, handle_t, handle_t, ctypes.POINTER(handle_t), dword_t, bool_t, dword_t],
            ),
            (
                self.k,
                "CreatePipe",
                bool_t,
                [ctypes.POINTER(handle_t), ctypes.POINTER(handle_t), pointer_t, dword_t],
            ),
            (self.k, "SetHandleInformation", bool_t, [handle_t, dword_t, dword_t]),
            (
                self.k,
                "WriteFile",
                bool_t,
                [handle_t, pointer_t, dword_t, ctypes.POINTER(dword_t), pointer_t],
            ),
            (self.k, "OpenThread", handle_t, [dword_t, bool_t, dword_t]),
            (self.k, "CancelSynchronousIo", bool_t, [handle_t]),
            (self.k, "CreateJobObjectW", handle_t, [pointer_t, text_t]),
            (
                self.k,
                "SetInformationJobObject",
                bool_t,
                [handle_t, ctypes.c_int, pointer_t, dword_t],
            ),
            (
                self.k,
                "QueryInformationJobObject",
                bool_t,
                [handle_t, ctypes.c_int, pointer_t, dword_t, pointer_t],
            ),
            (self.k, "TerminateJobObject", bool_t, [handle_t, wintypes.UINT]),
            (self.k, "AssignProcessToJobObject", bool_t, [handle_t, handle_t]),
            (self.k, "ResumeThread", dword_t, [handle_t]),
            (self.k, "TerminateProcess", bool_t, [handle_t, wintypes.UINT]),
            (self.k, "GetExitCodeProcess", bool_t, [handle_t, ctypes.POINTER(dword_t)]),
            (
                self.k,
                "InitializeProcThreadAttributeList",
                bool_t,
                [pointer_t, dword_t, dword_t, ctypes.POINTER(ctypes.c_size_t)],
            ),
            (
                self.k,
                "UpdateProcThreadAttribute",
                bool_t,
                [
                    pointer_t,
                    dword_t,
                    ctypes.c_size_t,
                    pointer_t,
                    ctypes.c_size_t,
                    pointer_t,
                    pointer_t,
                ],
            ),
            (self.k, "DeleteProcThreadAttributeList", None, [pointer_t]),
            (
                self.k,
                "CreateProcessW",
                bool_t,
                [
                    text_t,
                    wintypes.LPWSTR,
                    pointer_t,
                    pointer_t,
                    bool_t,
                    dword_t,
                    pointer_t,
                    text_t,
                    pointer_t,
                    pointer_t,
                ],
            ),
            (self.a, "OpenProcessToken", bool_t, [handle_t, dword_t, ctypes.POINTER(handle_t)]),
            (
                self.a,
                "GetTokenInformation",
                bool_t,
                [handle_t, ctypes.c_int, pointer_t, dword_t, ctypes.POINTER(dword_t)],
            ),
            (self.a, "FreeSid", pointer_t, [pointer_t]),
            (self.a, "GetLengthSid", dword_t, [pointer_t]),
            (
                self.a,
                "GetSecurityInfo",
                dword_t,
                [
                    handle_t,
                    ctypes.c_int,
                    dword_t,
                    pointer_t,
                    pointer_t,
                    pointer_t,
                    pointer_t,
                    pointer_t,
                ],
            ),
            (
                self.a,
                "GetNamedSecurityInfoW",
                dword_t,
                [
                    text_t,
                    ctypes.c_int,
                    dword_t,
                    pointer_t,
                    pointer_t,
                    pointer_t,
                    pointer_t,
                    pointer_t,
                ],
            ),
            (
                self.a,
                "SetNamedSecurityInfoW",
                dword_t,
                [text_t, ctypes.c_int, dword_t, pointer_t, pointer_t, pointer_t, pointer_t],
            ),
            (
                self.a,
                "GetSecurityDescriptorControl",
                bool_t,
                [pointer_t, ctypes.POINTER(wintypes.WORD), pointer_t],
            ),
            (
                self.a,
                "ConvertStringSecurityDescriptorToSecurityDescriptorW",
                bool_t,
                [text_t, dword_t, pointer_t, pointer_t],
            ),
            (self.a, "ConvertStringSidToSidW", bool_t, [text_t, pointer_t]),
            (
                self.e,
                "CreateAppContainerProfile",
                ctypes.c_long,
                [text_t, text_t, text_t, pointer_t, dword_t, pointer_t],
            ),
            (
                self.e,
                "DeriveAppContainerSidFromAppContainerName",
                ctypes.c_long,
                [text_t, pointer_t],
            ),
            (self.u, "GetProcessWindowStation", handle_t, []),
            (self.u, "GetThreadDesktop", handle_t, [dword_t]),
            (self.u, "SetProcessWindowStation", bool_t, [handle_t]),
            (self.u, "SetThreadDesktop", bool_t, [handle_t]),
            (self.u, "CreateWindowStationW", handle_t, [text_t, dword_t, dword_t, pointer_t]),
            (
                self.u,
                "CreateDesktopW",
                handle_t,
                [text_t, pointer_t, pointer_t, dword_t, dword_t, pointer_t],
            ),
            (self.u, "CloseWindowStation", bool_t, [handle_t]),
            (self.u, "CloseDesktop", bool_t, [handle_t]),
            (self.u, "OpenWindowStationW", handle_t, [text_t, bool_t, dword_t]),
            (
                self.u,
                "GetUserObjectInformationW",
                bool_t,
                [handle_t, ctypes.c_int, pointer_t, dword_t, pointer_t],
            ),
        ]
        for library, name, result, arguments in signatures:
            function = getattr(library, name)
            function.restype = result
            function.argtypes = arguments

    @staticmethod
    def check(result: Any, stage: str) -> Any:
        if not result:
            fixed_stage = re.sub(r"[^a-z0-9]+", "_", stage.lower()).strip("_")
            raise AppContainerError(fixed_stage, winerror=_last_error())
        return result

    def sid_text(self, pointer: Any) -> str:
        length = self.a.GetLengthSid(pointer)
        if not length or length > 68:
            raise AppContainerError("native_sid_length_is_invalid")
        return _sid_text(ctypes.string_at(pointer, length))

    def owner_sid(self) -> str:
        token = wintypes.HANDLE()
        self.check(
            self.a.OpenProcessToken(self.k.GetCurrentProcess(), 8, ctypes.byref(token)),
            "current token",
        )
        try:
            needed = wintypes.DWORD()
            self.a.GetTokenInformation(token, 1, None, 0, ctypes.byref(needed))
            if not 0 < needed.value <= 65536:
                raise AppContainerError("token_user_size_is_invalid")
            data = ctypes.create_string_buffer(needed.value)
            self.check(
                self.a.GetTokenInformation(token, 1, data, needed, ctypes.byref(needed)),
                "token user",
            )
            return self.sid_text(ctypes.cast(data, ctypes.POINTER(ctypes.c_void_p))[0])
        finally:
            self.k.CloseHandle(token)

    @contextmanager
    def descriptor(self, sddl: str):
        pointer = ctypes.c_void_p()
        self.check(
            self.a.ConvertStringSecurityDescriptorToSecurityDescriptorW(
                sddl, 1, ctypes.byref(pointer), None
            ),
            "security descriptor creation",
        )
        try:
            yield pointer
        finally:
            self.k.LocalFree(pointer)

    def security(
        self, value: Any, kind: int, *, named: bool = False, labels: bool = False
    ) -> tuple[str, bool, tuple[Ace, ...], tuple[Ace, ...]]:
        owner, group, dacl, sacl, descriptor = (ctypes.c_void_p() for _ in range(5))
        function = self.a.GetNamedSecurityInfoW if named else self.a.GetSecurityInfo
        result = function(
            value,
            kind,
            7 | (0x10 if labels else 0),
            ctypes.byref(owner),
            ctypes.byref(group),
            ctypes.byref(dacl),
            ctypes.byref(sacl),
            ctypes.byref(descriptor),
        )
        if result or not descriptor.value or not dacl.value or not owner.value:
            raise AppContainerError("security_query_failed", winerror=int(result))
        try:
            control, revision = wintypes.WORD(), wintypes.DWORD()
            self.check(
                self.a.GetSecurityDescriptorControl(
                    descriptor, ctypes.byref(control), ctypes.byref(revision)
                ),
                "descriptor control",
            )

            def entries(pointer: Any) -> tuple[Ace, ...]:
                if not pointer.value:
                    return ()
                size = struct.unpack_from("<H", ctypes.string_at(pointer, 8), 2)[0]
                if not 8 <= size <= 65535:
                    raise AppContainerError("native_acl_size_invalid")
                return _parse_acl(ctypes.string_at(pointer, size))

            return self.sid_text(owner), bool(control.value & 0x1000), entries(dacl), entries(sacl)
        finally:
            self.k.LocalFree(descriptor)

    def set_acl(self, path: Path, entries: Sequence[Ace]) -> None:
        raw_entries = b"".join(a.raw for a in entries)
        # ACL_REVISION_DS preserves object ACEs in unrelated grants.
        data = struct.pack("<BBHHH", 4, 0, 8 + len(raw_entries), len(entries), 0) + raw_entries
        buffer = ctypes.create_string_buffer(data)
        result = self.a.SetNamedSecurityInfoW(str(path), 1, 4, None, None, buffer, None)
        if result:
            raise AppContainerError("directory_acl_update_failed", winerror=int(result))

    def package_ace(self, sid: str) -> Ace:
        pointer = ctypes.c_void_p()
        self.check(
            self.a.ConvertStringSidToSidW(sid, ctypes.byref(pointer)), "package SID conversion"
        )
        try:
            raw_sid = ctypes.string_at(pointer, self.a.GetLengthSid(pointer))
        finally:
            self.k.LocalFree(pointer)
        raw = struct.pack("<BBHI", 0, 3, 8 + len(raw_sid), _READ_EXECUTE) + raw_sid
        return Ace(0, 3, _READ_EXECUTE, sid, raw)


@dataclass
class _MutexState:
    proved: bool = True


class _Deadline:
    def __init__(self, native: NativeApi, seconds: int, cancel_handle: int | None) -> None:
        self.native = native
        self.expires = time.monotonic() + seconds
        self.cancel_handle = cancel_handle

    def check(self) -> None:
        if self.cancel_handle is not None:
            result = self.native.k.WaitForSingleObject(self.cancel_handle, 0)
            if result == 0:
                raise AppContainerError("invocation_cancelled")
            if result != 258:
                raise AppContainerError("cancellation_handle_invalid", winerror=_last_error())
        if time.monotonic() >= self.expires:
            raise AppContainerError("execution_deadline_exceeded")

    def remaining(self) -> float:
        self.check()
        return max(0.001, self.expires - time.monotonic())

    def wait(self, handle: Any) -> int:
        while True:
            self.check()
            milliseconds = max(1, min(100, int((self.expires - time.monotonic()) * 1000)))
            result = self.native.k.WaitForSingleObject(handle, milliseconds)
            if result != 258:
                return result


@contextmanager
def _mutex(native: NativeApi, deadline: _Deadline, state: _MutexState):
    deadline.check()
    with native.descriptor("O:BAG:BAD:P(A;;0x1f0001;;;SY)(A;;0x1f0001;;;BA)") as descriptor:
        attributes = _SecurityAttributes(ctypes.sizeof(_SecurityAttributes), descriptor, False)
        handle = native.check(
            native.k.CreateMutexW(ctypes.byref(attributes), False, MUTEX_NAME),
            "global mutex creation",
        )
    acquired = False
    abandoned = False
    state.proved = False
    try:
        owner, protected, entries, _ = native.security(handle, 6)
        _require_mutex_security(owner, protected, entries)
        wait = deadline.wait(handle)
        acquired = wait in (0, 0x80)
        if wait == 0x80:
            abandoned = True
            raise AppContainerError("global_mutex_abandoned")
        if wait != 0:
            raise AppContainerError("global_mutex_acquisition_failed_or_timed_out")
        yield
    finally:
        if acquired:
            native.check(native.k.ReleaseMutex(handle), "global mutex release")
        native.check(native.k.CloseHandle(handle), "global_mutex_close")
        state.proved = not abandoned


def _profile(native: NativeApi) -> Any:
    sid = ctypes.c_void_p()
    result = (
        native.e.CreateAppContainerProfile(
            PROFILE_NAME,
            "Earnings Summary Filing XBRL",
            "Network-denied captured filing processor",
            None,
            0,
            ctypes.byref(sid),
        )
        & 0xFFFFFFFF
    )
    if result == 0x800700B7:
        result = (
            native.e.DeriveAppContainerSidFromAppContainerName(PROFILE_NAME, ctypes.byref(sid))
            & 0xFFFFFFFF
        )
    if result or not sid.value:
        raise AppContainerError("appcontainer_profile_unavailable", winerror=int(result))
    return sid


@dataclass
class _Grant:
    path: Path
    ace: Ace
    applied: bool = False


def _grant(
    native: NativeApi, path: Path, package: str, owned: list[_Grant] | None = None
) -> _Grant:
    _, _, entries, _ = native.security(str(path), 1, named=True)
    for entry in entries:
        if not entry.sid:
            raise AppContainerError("unsupported_directory_ace")
        if entry.sid == package and (
            entry.kind != 0 or entry.mask & ~_READ_EXECUTE or entry.flags & ~0x13
        ):
            raise AppContainerError("unsafe_pre_existing_package_directory_grant")
    if any(a.sid == package and not a.flags & 0x10 for a in entries):
        raise AppContainerError("preexisting_explicit_package_grant")
    retained = list(entries)
    ace = native.package_ace(package)
    # Explicit grants precede inherited grants and follow explicit deny grants.
    index = next((i for i, a in enumerate(retained) if a.flags & 0x10), len(retained))
    retained.insert(index, ace)
    grant = _Grant(path, ace)
    if owned is not None:
        owned.append(grant)
    native.set_acl(path, retained)
    grant.applied = True
    _, _, actual, _ = native.security(str(path), 1, named=True)
    if [a.raw for a in actual] != [a.raw for a in retained]:
        raise AppContainerError("directory_grant_postcondition_changed")
    return grant


def _remove_grant(native: NativeApi, grant: _Grant) -> None:
    _, _, entries, _ = native.security(str(grant.path), 1, named=True)
    matches = [i for i, ace in enumerate(entries) if ace.raw == grant.ace.raw]
    if not matches and not grant.applied:
        return
    if len(matches) != 1:
        raise AppContainerError("owned_directory_grant_identity_changed")
    retained = list(entries)
    retained.pop(matches[0])
    native.set_acl(grant.path, retained)
    _, _, actual, _ = native.security(str(grant.path), 1, named=True)
    if [a.raw for a in actual] != [a.raw for a in retained]:
        raise AppContainerError("directory_grant_removal_unproved")


class _PrivateDesktop:
    def __init__(
        self, native: NativeApi, package: str, owned: list[_PrivateDesktop] | None = None
    ) -> None:
        self.n = native
        self.name = "EarningsSummaryXbrl_" + uuid.uuid4().hex
        self.path = self.name + r"\Processor"
        self.station = None
        self.desktop = None
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
                attributes = _SecurityAttributes(
                    ctypes.sizeof(_SecurityAttributes), descriptor, False
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
                attributes = _SecurityAttributes(
                    ctypes.sizeof(_SecurityAttributes), descriptor, False
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

    def _restore(self, station: Any, desktop: Any) -> None:
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

    def _verify(self, handle: Any, owner: str, package: str, mask: int, *, station: bool) -> None:
        actual_owner, protected, entries, labels = self.n.security(handle, 7, labels=True)
        if not protected:
            raise AppContainerError("private_ui_dacl_is_unprotected")
        _require_ui_security(actual_owner, owner, package, mask, entries, labels)
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
        if not self.context_restored:
            raise AppContainerError("creator_ui_context_restoration_unproved")
        n = self.n
        if self.desktop:
            n.check(n.u.CloseDesktop(self.desktop), "private desktop close")
            self.desktop = None
        if self.station:
            n.check(n.u.CloseWindowStation(self.station), "private station close")
            self.station = None
            unexpected = n.u.OpenWindowStationW(self.name, False, 0x20000)
            error = _last_error()
            if unexpected:
                n.u.CloseWindowStation(unexpected)
                raise AppContainerError("private_station_remains")
            if error != 2:
                raise AppContainerError("private_station_absence_unproved")


def _stop_job(native: NativeApi, job: Any) -> None:
    native.check(native.k.TerminateJobObject(job, 127), "owned Job termination")
    deadline = time.monotonic() + 5
    while True:
        accounting = _JobAccounting()
        native.check(
            native.k.QueryInformationJobObject(
                job, 1, ctypes.byref(accounting), ctypes.sizeof(accounting), None
            ),
            "owned Job accounting",
        )
        if accounting.active == 0:
            return
        if time.monotonic() >= deadline:
            raise AppContainerError("owned_job_retains_children")
        time.sleep(0.02)


class _InputWriter:
    def __init__(self, native: NativeApi, handle: Any, payload: bytes) -> None:
        self.n = native
        self.handle = handle
        self.payload = payload
        self.error: BaseException | None = None
        self.started = threading.Event()
        self.thread_handle = None
        self.thread = threading.Thread(target=self._write, name="filing-input", daemon=True)

    def _write(self) -> None:
        try:
            self.thread_handle = self.n.check(
                self.n.k.OpenThread(0x1, False, self.n.k.GetCurrentThreadId()),
                "input writer thread handle",
            )
            self.started.set()
            buffer = ctypes.create_string_buffer(self.payload)
            offset = 0
            while offset < len(self.payload):
                written = wintypes.DWORD()
                self.n.check(
                    self.n.k.WriteFile(
                        self.handle,
                        ctypes.byref(buffer, offset),
                        min(65536, len(self.payload) - offset),
                        ctypes.byref(written),
                        None,
                    ),
                    "bounded child input write",
                )
                if written.value == 0:
                    raise AppContainerError("child_input_write_made_no_progress")
                offset += written.value
        except BaseException as error:
            self.error = error
        finally:
            self.started.set()
            self.n.k.CloseHandle(self.handle)

    def stop(self) -> None:
        if not self.started.wait(5):
            raise AppContainerError("input_writer_startup_unproved")
        # ERROR_NOT_FOUND means the write completed before cancellation.
        if (
            self.thread.is_alive()
            and self.thread_handle
            and not self.n.k.CancelSynchronousIo(self.thread_handle)
            and _last_error() != 1168
        ):
            raise AppContainerError("input_writer_cancellation_failed")
        self.thread.join(5)
        if self.thread.is_alive():
            raise AppContainerError("input_writer_remains")
        if self.thread_handle:
            self.n.k.CloseHandle(self.thread_handle)
            self.thread_handle = None


@dataclass
class _ChildState:
    stopped: bool = False


def _start_child(
    native: NativeApi,
    command: Sequence[str],
    root: Path,
    sid: Any,
    payload: bytes,
    desktop: str,
    timeout_seconds: int,
    state: _ChildState,
    deadline: _Deadline | None = None,
) -> int:
    k = native.k
    process = ProcessInformation()
    job = None
    read = wintypes.HANDLE()
    write = wintypes.HANDLE()
    outputs: list[Any] = []
    attributes = None
    initialized = False
    writer: _InputWriter | None = None
    writer_started = False
    assigned = False
    try:
        if deadline is not None:
            deadline.check()
        job = native.check(k.CreateJobObjectW(None, None), "owned Job creation")
        limits = JobExtendedLimitInformation()
        limits.BasicLimitInformation.LimitFlags = 0x2000
        native.check(
            k.SetInformationJobObject(job, 9, ctypes.byref(limits), ctypes.sizeof(limits)),
            "owned Job configuration",
        )
        security = _SecurityAttributes(ctypes.sizeof(_SecurityAttributes), None, True)
        native.check(
            k.CreatePipe(ctypes.byref(read), ctypes.byref(write), ctypes.byref(security), 0),
            "child input pipe",
        )
        native.check(k.SetHandleInformation(write, 1, 0), "parent pipe inheritance restriction")
        for standard in (-11, -12):
            original = native.check(k.GetStdHandle(standard & 0xFFFFFFFF), "standard output handle")
            duplicate = wintypes.HANDLE()
            native.check(
                k.DuplicateHandle(
                    k.GetCurrentProcess(),
                    original,
                    k.GetCurrentProcess(),
                    ctypes.byref(duplicate),
                    0,
                    True,
                    2,
                ),
                "output handle duplication",
            )
            outputs.append(duplicate)
        size = ctypes.c_size_t()
        k.InitializeProcThreadAttributeList(None, 2, 0, ctypes.byref(size))
        if not 0 < size.value <= 65536:
            raise AppContainerError("process_attribute_size_invalid")
        attributes = ctypes.create_string_buffer(size.value)
        native.check(
            k.InitializeProcThreadAttributeList(attributes, 2, 0, ctypes.byref(size)),
            "process attributes",
        )
        initialized = True
        capabilities = SecurityCapabilities(sid.value, None, 0, 0)
        handles = (wintypes.HANDLE * 3)(read.value, outputs[0].value, outputs[1].value)
        for key, value in ((0x20009, capabilities), (0x20002, handles)):
            native.check(
                k.UpdateProcThreadAttribute(
                    attributes, 0, key, ctypes.byref(value), ctypes.sizeof(value), None, None
                ),
                "child security attributes",
            )
        startup = StartupInfoEx()
        startup.StartupInfo.cb = ctypes.sizeof(startup)
        startup.StartupInfo.dwFlags = 0x100
        startup.StartupInfo.lpDesktop = desktop
        startup.StartupInfo.hStdInput = read
        startup.StartupInfo.hStdOutput, startup.StartupInfo.hStdError = outputs
        startup.lpAttributeList = ctypes.cast(attributes, ctypes.c_void_p)
        line = ctypes.create_unicode_buffer(subprocess.list2cmdline(list(command)))
        if deadline is not None:
            deadline.check()
        native.check(
            k.CreateProcessW(
                command[0],
                line,
                None,
                None,
                True,
                0x80000 | 0x8000000 | 4,
                None,
                str(root),
                ctypes.byref(startup),
                ctypes.byref(process),
            ),
            "suspended AppContainer creation",
        )
        native.check(
            k.AssignProcessToJobObject(job, process.hProcess), "suspended child Job assignment"
        )
        assigned = True
        token = wintypes.HANDLE()
        native.check(
            native.a.OpenProcessToken(process.hProcess, 8, ctypes.byref(token)), "child token open"
        )
        try:
            value, returned = wintypes.DWORD(), wintypes.DWORD()
            native.check(
                native.a.GetTokenInformation(
                    token, 29, ctypes.byref(value), ctypes.sizeof(value), ctypes.byref(returned)
                ),
                "child AppContainer token",
            )
            if value.value != 1 or returned.value != ctypes.sizeof(value):
                raise AppContainerError("child_token_is_not_an_appcontainer")
            for kind in (30, 31):
                needed = wintypes.DWORD()
                native.a.GetTokenInformation(token, kind, None, 0, ctypes.byref(needed))
                if not 0 < needed.value <= 65536:
                    raise AppContainerError("child_token_security_size_invalid")
                data = ctypes.create_string_buffer(needed.value)
                native.check(
                    native.a.GetTokenInformation(token, kind, data, needed, ctypes.byref(needed)),
                    "child capability identity",
                )
                if kind == 30 and ctypes.cast(data, ctypes.POINTER(wintypes.DWORD))[0] != 0:
                    raise AppContainerError("child_token_has_unexpected_capabilities")
                if kind == 31:
                    actual_sid = ctypes.cast(data, ctypes.POINTER(ctypes.c_void_p))[0]
                    if native.sid_text(actual_sid) != native.sid_text(sid):
                        raise AppContainerError("child_appcontainer_sid_changed")
        finally:
            k.CloseHandle(token)
        if deadline is not None:
            deadline.check()
        if k.ResumeThread(process.hThread) == 0xFFFFFFFF:
            raise AppContainerError("child_resume_failed")
        k.CloseHandle(process.hThread)
        process.hThread = None
        k.CloseHandle(read)
        read = wintypes.HANDLE()
        writer = _InputWriter(native, write, payload)
        writer.thread.start()
        writer_started = True
        write = wintypes.HANDLE()
        result = (
            deadline.wait(process.hProcess)
            if deadline is not None
            else k.WaitForSingleObject(process.hProcess, timeout_seconds * 1000)
        )
        if result != 0:
            raise AppContainerError("child_failed_to_stop_before_deadline")
        writer.thread.join(0)
        if writer.thread.is_alive():
            raise AppContainerError("child_exited_before_input_delivery_completed")
        if writer.error:
            raise AppContainerError("child_input_delivery_failed") from writer.error
        code = wintypes.DWORD()
        native.check(
            k.GetExitCodeProcess(process.hProcess, ctypes.byref(code)), "child exit status"
        )
        return int(code.value)
    finally:
        # A failed assignment leaves a suspended process outside the Job.
        # Prove that process stopped independently before releasing any containment.
        if process.hProcess and not assigned:
            native.check(k.TerminateProcess(process.hProcess, 127), "unassigned child termination")
            if k.WaitForSingleObject(process.hProcess, 5000) != 0:
                raise AppContainerError("unassigned_child_stop_unproved")
        if job:
            _stop_job(native, job)
        if process.hProcess and k.WaitForSingleObject(process.hProcess, 5000) != 0:
            raise AppContainerError("child_stop_unproved")
        if writer and writer_started:
            writer.stop()
        state.stopped = True
        if initialized:
            k.DeleteProcThreadAttributeList(attributes)
        for handle in (process.hThread, process.hProcess, read, write, *outputs, job):
            if handle:
                k.CloseHandle(handle)


def _validate(
    command: Sequence[str],
    runtime_root: Path,
    input_root: Path,
    payload: bytes,
    timeout_seconds: int,
) -> tuple[list[str], Path, Path]:
    if (
        isinstance(command, (str, bytes))
        or not command
        or any(type(a) is not str or "\0" in a for a in command)
    ):
        raise AppContainerError("child_command_is_invalid")
    if type(timeout_seconds) is not int or not 1 <= timeout_seconds <= 3600:
        raise AppContainerError("child_timeout_is_outside_the_supported_bound")
    if type(payload) is not bytes or len(payload) > 64 * 1024 * 1024:
        raise AppContainerError("child_input_exceeds_the_supported_bound")
    roots: list[Path] = []
    for path in (runtime_root, input_root, Path(command[0])):
        path = Path(os.path.abspath(path))
        for ancestor in (path, *path.parents):
            info = ancestor.lstat()
            if ancestor.is_symlink() or getattr(info, "st_file_attributes", 0) & 0x400:
                raise AppContainerError("sandbox_path_contains_a_reparse_point")
        roots.append(path)
    runtime, source, executable = roots
    if runtime == source or runtime.is_relative_to(source) or source.is_relative_to(runtime):
        raise AppContainerError("sandbox_roots_overlap")
    if (
        not runtime.is_dir()
        or not source.is_dir()
        or not executable.is_file()
        or not executable.is_relative_to(runtime)
    ):
        raise AppContainerError("sandbox_runtime_or_executable_is_invalid")
    return [str(executable), *command[1:]], runtime, source


def _require_no_loopback_exemption(native: NativeApi, sid: str, timeout_seconds: float = 2) -> None:
    function = native.k.GetSystemDirectoryW
    function.argtypes = [wintypes.LPWSTR, wintypes.UINT]
    function.restype = wintypes.UINT
    buffer = ctypes.create_unicode_buffer(32768)
    length = function(buffer, len(buffer))
    if not 0 < length < len(buffer):
        raise AppContainerError("windows_system_directory_unavailable")
    result = subprocess.run(
        [str(Path(buffer.value) / "CheckNetIsolation.exe"), "LoopbackExempt", "-s"],
        capture_output=True,
        timeout=timeout_seconds,
        check=False,
    )
    # This command only lists exemptions. It never removes an existing entry.
    listed = set(re.findall(rb"s-1(?:-\d+)+", result.stdout.replace(b"\0", b"").lower()))
    if result.returncode or sid.casefold().encode("ascii") in listed:
        raise AppContainerError("loopback_exemption_absence_unproved")


def run_filing_appcontainer(
    command: Sequence[str],
    *,
    runtime_root: Path,
    input_root: Path,
    payload: bytes,
    timeout_seconds: int = 600,
    cancel_handle: int | None = None,
) -> int:
    """Run in a dedicated process; acknowledge only after containment cleanup.

    timeout_seconds bounds execution, including mutex acquisition. Bounded Job
    and writer drain run separately. The trusted cancellation event is excluded
    from the child's exact three-handle inheritance list.
    """
    try:
        command, runtime, source = _validate(
            command, runtime_root, input_root, payload, timeout_seconds
        )
    except AppContainerError as error:
        error.cleanup_proved = True
        raise
    except BaseException as error:
        raise AppContainerError("argument_validation_failed", cleanup_proved=True) from error
    if cancel_handle is not None and (type(cancel_handle) is not int or cancel_handle <= 0):
        raise AppContainerError("cancellation_handle_invalid", cleanup_proved=True)
    try:
        native = NativeApi()
    except AppContainerError as error:
        error.cleanup_proved = True
        raise
    except BaseException as error:
        raise AppContainerError("native_initialization_failed", cleanup_proved=True) from error
    deadline = _Deadline(native, timeout_seconds, cancel_handle)
    mutex_state = _MutexState()
    cleanup_proved = True
    try:
        with _mutex(native, deadline, mutex_state):
            deadline.check()
            sid = _profile(native)
            grants: list[_Grant] = []
            desktops: list[_PrivateDesktop] = []
            child = _ChildState(stopped=True)
            try:
                deadline.check()
                package = native.sid_text(sid)
                _require_no_loopback_exemption(native, package, min(2.0, deadline.remaining()))
                deadline.check()
                for root in dict.fromkeys((runtime, source)):
                    deadline.check()
                    _grant(native, root, package, grants)
                deadline.check()
                desktop = _PrivateDesktop(native, package, desktops)
                deadline.check()
                child.stopped = False
                return _start_child(
                    native,
                    command,
                    runtime,
                    sid,
                    payload,
                    desktop.path,
                    timeout_seconds,
                    child,
                    deadline,
                )
            finally:
                cleanup_proved = False
                if not child.stopped:
                    raise AppContainerError("child_tree_stop_unproved")
                # A remaining private UI object retains all directory grants.
                # Recovery must inspect and close it before those grants change.
                for desktop in reversed(desktops):
                    desktop.close()
                failures: list[BaseException] = []
                for grant in reversed(grants):
                    try:
                        _remove_grant(native, grant)
                    except BaseException as error:
                        failures.append(error)
                if failures:
                    raise AppContainerError("directory_cleanup_unproved") from failures[0]
                native.a.FreeSid(sid)
                cleanup_proved = True
    except BaseException as error:
        proved = cleanup_proved and mutex_state.proved
        if isinstance(error, AppContainerError):
            error.cleanup_proved = proved
            raise
        raise AppContainerError("native_unexpected_failure", cleanup_proved=proved) from error


stop_job_handle = _stop_job
