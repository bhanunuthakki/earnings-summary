"""Pure security descriptor checks for the standalone filing launcher runtime."""

from __future__ import annotations

import struct
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from runtime import windows_appcontainer as boundary


def _returns(value: Any) -> Callable[..., Any]:
    def call(*_args: Any, **_kwargs: Any) -> Any:
        return value

    return call


def _check(value: Any, stage: str) -> Any:
    if not value:
        raise boundary.AppContainerError(stage.replace(" ", "_").lower())
    return value


def _sid(text: str) -> bytes:
    parts = [int(part) for part in text.split("-")[1:]]
    return (
        bytes((parts[0], len(parts) - 2))
        + parts[1].to_bytes(6, "big")
        + b"".join(struct.pack("<I", part) for part in parts[2:])
    )


def _ace(sid: str, *, kind: int = 0, flags: int = 0, mask: int = 0x1F0001) -> boundary.Ace:
    encoded = _sid(sid)
    raw = struct.pack("<BBHI", kind, flags, 8 + len(encoded), mask) + encoded
    return boundary.Ace(kind, flags, mask, sid, raw)


def _acl(*entries: boundary.Ace) -> bytes:
    raw = b"".join(entry.raw for entry in entries)
    return struct.pack("<BBHHH", 2, 0, len(raw) + 8, len(entries), 0) + raw


def test_mutex_exact_owner_acl_and_abandonment_contract() -> None:
    entries = (_ace("S-1-5-18"), _ace("S-1-5-32-544"))
    parsed = getattr(boundary, "_parse_acl")(_acl(*entries))
    getattr(boundary, "_require_mutex_security")("S-1-5-18", True, parsed)
    for owner, protected, actual in [
        ("S-1-5-21-1", True, parsed),
        ("S-1-5-18", False, parsed),
        ("S-1-5-18", True, parsed + parsed[:1]),
        ("S-1-5-18", True, (_ace("S-1-5-18", flags=16), parsed[1])),
        ("S-1-5-18", True, (_ace("S-1-5-18", mask=1), parsed[1])),
        ("S-1-5-18", True, (_ace("S-1-5-18", kind=1), parsed[1])),
    ]:
        with pytest.raises(boundary.AppContainerError):
            getattr(boundary, "_require_mutex_security")(owner, protected, actual)


@pytest.mark.parametrize(
    "kind,flags,mask,sid",
    [
        (0x11, 0, 1, "S-1-16-4096"),
        (0x11, 0, 1, "S-1-16-8192"),
        (0x11, 1, 1, "S-1-16-4096"),
        (0, 0, 1, "S-1-16-4096"),
        (0x11, 0, 0, "S-1-16-4096"),
    ],
)
def test_low_label_semantics(kind: int, flags: int, mask: int, sid: str) -> None:
    entries = getattr(boundary, "_parse_acl")(_acl(_ace(sid, kind=kind, flags=flags, mask=mask)))
    if (kind, flags, mask, sid) == (0x11, 0, 1, "S-1-16-4096"):
        getattr(boundary, "_require_low_label")(entries)
    else:
        with pytest.raises(boundary.AppContainerError):
            getattr(boundary, "_require_low_label")(entries)


@pytest.mark.parametrize(
    "raw", [b"", b"\0" * 8, struct.pack("<BBHHH", 2, 0, 8, 1, 0), b"\x02\0\xff\xff\0\0\0\0"]
)
def test_malformed_acl_fails_closed(raw: bytes) -> None:
    with pytest.raises(boundary.AppContainerError):
        getattr(boundary, "_parse_acl")(raw)


def test_ui_requires_exact_package_rights_and_no_extra_principal() -> None:
    owner, package = "S-1-5-18", "S-1-15-2-1"
    label = (_ace("S-1-16-4096", kind=0x11, mask=1),)
    entries = (_ace(owner, mask=0xF037F), _ace(package, mask=0x20023))
    getattr(boundary, "_require_ui_security")(owner, owner, package, 0x20023, entries, label)
    with pytest.raises(boundary.AppContainerError):
        getattr(boundary, "_require_ui_security")(owner, owner, package, 0x20087, entries, label)
    with pytest.raises(boundary.AppContainerError):
        getattr(boundary, "_require_ui_security")(
            owner, owner, package, 0x20023, (*entries, _ace("S-1-1-0")), label
        )


def test_input_paths_and_bounds(tmp_path: Path) -> None:
    runtime, source = tmp_path / "runtime", tmp_path / "input"
    runtime.mkdir()
    source.mkdir()
    executable = runtime / "python.exe"
    executable.write_bytes(b"synthetic")
    command = [str(executable), "-I", "-S", "processor.py"]
    assert getattr(boundary, "_validate")(command, runtime, source, b"{}", 1)[0] == command
    assert getattr(boundary, "_validate")(command, runtime, source, b"{}", 3600)[0] == command
    for changed, root, source_root, payload, timeout in [
        (["outside"], runtime, source, b"", 1),
        (command, runtime, runtime, b"", 1),
        (command, runtime, source, b"", 0),
        (command, runtime, source, b"", True),
        ([str(executable), "bad\0arg"], runtime, source, b"", 1),
    ]:
        with pytest.raises((boundary.AppContainerError, FileNotFoundError)):
            getattr(boundary, "_validate")(changed, root, source_root, payload, timeout)
    link = tmp_path / "link"
    link.symlink_to(runtime, target_is_directory=True)
    with pytest.raises(boundary.AppContainerError, match="reparse"):
        getattr(boundary, "_validate")([str(link / "python.exe")], link, source, b"", 1)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX fail-closed boundary")
def test_native_api_fails_closed_outside_windows() -> None:
    with pytest.raises(boundary.AppContainerError, match="requires_windows"):
        getattr(boundary, "NativeApi")()


def test_native_security_reads_acl_size_with_win32_little_endian_header() -> None:
    import ctypes
    from types import SimpleNamespace

    owner = ctypes.create_string_buffer(_sid("S-1-5-18"))
    acl = ctypes.create_string_buffer(_acl(_ace("S-1-5-18")))
    descriptor = ctypes.create_string_buffer(32)
    freed: list[Any] = []

    def write_pointer(target: Any, value: int) -> None:
        ctypes.cast(target, ctypes.POINTER(ctypes.c_void_p))[0] = value

    def get_security(
        _handle: Any,
        _kind: int,
        _requested: int,
        owner_out: Any,
        _group_out: Any,
        dacl_out: Any,
        _sacl_out: Any,
        descriptor_out: Any,
    ) -> int:
        write_pointer(owner_out, ctypes.addressof(owner))
        write_pointer(dacl_out, ctypes.addressof(acl))
        write_pointer(descriptor_out, ctypes.addressof(descriptor))
        return 0

    def get_control(_descriptor: Any, control_out: Any, _revision_out: Any) -> bool:
        ctypes.cast(control_out, ctypes.POINTER(ctypes.c_ushort))[0] = 0x1000
        return True

    native: Any = object.__new__(getattr(boundary, "NativeApi"))
    native.a = SimpleNamespace(
        GetSecurityInfo=get_security,
        GetSecurityDescriptorControl=get_control,
        GetLengthSid=_returns(len(_sid("S-1-5-18"))),
    )
    native.k = SimpleNamespace(LocalFree=freed.append)
    owner_text, protected, entries, labels = native.security(123, 6)
    assert owner_text == "S-1-5-18"
    assert protected
    assert entries[0].sid == owner_text
    assert labels == ()
    assert len(freed) == 1


@pytest.mark.parametrize(
    "mask,flags,kind",
    [
        (0x10000000, 16, 0),
        (0x40000000, 16, 0),
        (0x1200AB, 0, 0),
        (0x1200A9, 8, 0),
        (0x1200A9, 0, 5),
    ],
)
def test_unsafe_preexisting_package_grants_refuse_before_mutation(
    tmp_path: Path, mask: int, flags: int, kind: int
) -> None:
    from types import SimpleNamespace

    package = "S-1-15-2-1"
    ace = _ace(package, mask=mask, flags=flags, kind=kind)
    if kind == 5:
        ace = boundary.Ace(kind, flags, mask, "", ace.raw)
    mutations: list[Any] = []
    native: Any = SimpleNamespace(
        security=_returns(("owner", True, (ace,), ())), set_acl=mutations.append
    )
    with pytest.raises(boundary.AppContainerError):
        getattr(boundary, "_grant")(native, tmp_path, package)
    assert mutations == []


@pytest.mark.parametrize("failure", ["create", "assign", "token", "resume", "stop", "writer-start"])
def test_native_child_failure_stops_before_outer_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    import ctypes
    from types import SimpleNamespace

    events: list[str] = []

    def pointer(target: Any, ctype: Any, value: int) -> None:
        pointer_type: Any = getattr(ctypes, "POINTER")(ctype)
        ctypes.cast(target, pointer_type)[0] = value

    def attributes(_buffer: Any, _count: int, _flags: int, size: Any) -> bool:
        pointer(size, ctypes.c_size_t, 128)
        return True

    def pipe(read: Any, write: Any, *_args: Any) -> bool:
        pointer(read, ctypes.c_void_p, 201)
        pointer(write, ctypes.c_void_p, 202)
        return True

    def duplicate(_process: Any, _source: Any, _other: Any, out: Any, *_args: Any) -> bool:
        pointer(out, ctypes.c_void_p, 203)
        return True

    def create(*args: Any) -> bool:
        events.append("create")
        if failure == "create":
            return False
        info = ctypes.cast(args[-1], ctypes.POINTER(boundary.ProcessInformation)).contents
        info.hProcess, info.hThread = 101, 102
        return True

    def assign(*_args: Any) -> bool:
        events.append("assign")
        return failure != "assign"

    def token(_process: Any, _rights: Any, out: Any) -> bool:
        events.append("token")
        pointer(out, ctypes.c_void_p, 301)
        return failure != "token"

    token_sid = ctypes.create_string_buffer(_sid("S-1-15-2-1"))

    def token_information(_token: Any, kind: int, data: Any, _size: Any, needed: Any) -> bool:
        pointer(needed, ctypes.c_ulong, 32 if kind != 29 else ctypes.sizeof(ctypes.c_ulong))
        if data is not None:
            if kind == 29:
                pointer(data, ctypes.c_ulong, 1)
            elif kind == 30:
                pointer(data, ctypes.c_ulong, 0)
            else:
                pointer(data, ctypes.c_void_p, ctypes.addressof(token_sid))
        return True

    def resume(_thread: Any) -> int:
        events.append("resume")
        return 1 if failure == "writer-start" else 0xFFFFFFFF

    def terminate(*_args: Any) -> bool:
        events.append("terminate-unassigned")
        return True

    def stop(_native: Any, _job: Any) -> None:
        events.append("stop-job")
        if failure == "stop":
            raise boundary.AppContainerError("owned_job_retains_children")

    native: Any = SimpleNamespace(
        k=SimpleNamespace(
            CreateJobObjectW=_returns(401),
            SetInformationJobObject=_returns(True),
            CreatePipe=pipe,
            SetHandleInformation=_returns(True),
            GetStdHandle=_returns(500),
            GetCurrentProcess=_returns(501),
            DuplicateHandle=duplicate,
            InitializeProcThreadAttributeList=attributes,
            UpdateProcThreadAttribute=_returns(True),
            CreateProcessW=create,
            AssignProcessToJobObject=assign,
            ResumeThread=resume,
            TerminateProcess=terminate,
            WaitForSingleObject=_returns(0),
            CloseHandle=_returns(True),
            DeleteProcThreadAttributeList=_returns(None),
        ),
        a=SimpleNamespace(OpenProcessToken=token, GetTokenInformation=token_information),
        sid_text=_returns("S-1-15-2-1"),
        check=_check,
    )
    # For the "stop" case fail during token verification, then inject cleanup failure.
    if failure == "stop":
        native.a.OpenProcessToken = _returns(False)
    monkeypatch.setattr(boundary, "_stop_job", stop)
    if failure == "writer-start":

        def fail_start() -> None:
            raise RuntimeError("thread creation failed")

        monkeypatch.setattr(
            boundary,
            "_InputWriter",
            _returns(
                SimpleNamespace(
                    thread=SimpleNamespace(start=fail_start),
                    stop=lambda: pytest.fail("unstarted writer stop"),
                )
            ),
        )
    state = getattr(boundary, "_ChildState")()
    with pytest.raises((boundary.AppContainerError, RuntimeError)):
        getattr(boundary, "_start_child")(
            native,
            [str(tmp_path / "python.exe")],
            tmp_path,
            ctypes.c_void_p(1),
            b"",
            "private\\Processor",
            1,
            state,
        )
    assert events[0] == "create"
    assert "stop-job" in events
    if failure == "assign":
        assert events.index("terminate-unassigned") < events.index("stop-job")
        assert "token" not in events
    if failure == "token":
        assert "resume" not in events
    assert state.stopped == (failure != "stop")


@pytest.mark.parametrize("fail_query", [True, False])
def test_grant_ownership_precedes_mutation_and_postcondition_failure(
    tmp_path: Path, fail_query: bool
) -> None:
    from types import SimpleNamespace

    package = "S-1-15-2-1"
    owned: list[Any] = []
    writes: list[bool] = []
    query_count = 0

    def security(*_args: Any, **_kwargs: Any) -> tuple[Any, ...]:
        nonlocal query_count
        query_count += 1
        if query_count > 1 and fail_query:
            raise boundary.AppContainerError("native_query_failed")
        return "owner", True, (), ()

    def write(_path: Path, _entries: Any) -> None:
        assert len(owned) == 1
        writes.append(True)

    native: Any = SimpleNamespace(
        security=security,
        package_ace=_returns(_ace(package, mask=0x1200A9, flags=3)),
        set_acl=write,
    )
    with pytest.raises(boundary.AppContainerError):
        getattr(boundary, "_grant")(native, tmp_path, package, owned)
    assert len(owned) == 1
    assert writes == [True]


@pytest.mark.parametrize("flags", [0, 1, 3])
def test_preexisting_explicit_safe_package_ace_is_preserved_and_refused(
    tmp_path: Path, flags: int
) -> None:
    from types import SimpleNamespace

    package = "S-1-15-2-1"
    existing = _ace(package, mask=0x1200A9, flags=flags)
    writes: list[Any] = []
    native: Any = SimpleNamespace(
        security=_returns(("owner", True, (existing,), ())), set_acl=writes.append
    )
    owned: list[Any] = []
    with pytest.raises(boundary.AppContainerError, match="preexisting_explicit_package_grant"):
        getattr(boundary, "_grant")(native, tmp_path, package, owned)
    assert writes == []
    assert owned == []


def test_failed_acl_application_without_change_has_clean_recovery(tmp_path: Path) -> None:
    from types import SimpleNamespace

    package = "S-1-15-2-1"
    entries = (_ace("S-1-5-18"),)
    owned: list[Any] = []
    writes: list[Any] = []

    def fail_write(_path: Path, _entries: Any) -> None:
        raise boundary.AppContainerError("acl_application_failed")

    native: Any = SimpleNamespace(
        security=_returns(("owner", True, entries, ())),
        set_acl=fail_write,
        package_ace=_returns(_ace(package, mask=0x1200A9, flags=3)),
    )
    with pytest.raises(boundary.AppContainerError):
        getattr(boundary, "_grant")(native, tmp_path, package, owned)
    assert len(owned) == 1
    assert not owned[0].applied
    native.set_acl = writes.append
    getattr(boundary, "_remove_grant")(native, owned[0])
    assert writes == []


@pytest.mark.parametrize("ui_failure", [True, False])
def test_ui_failure_preserves_grants_and_never_proves_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ui_failure: bool
) -> None:
    import ctypes
    from contextlib import contextmanager
    from types import SimpleNamespace

    runtime, source = tmp_path / "runtime", tmp_path / "source"
    runtime.mkdir()
    source.mkdir()
    exe = runtime / "python.exe"
    exe.write_bytes(b"synthetic")
    events: list[str] = []
    native: Any = SimpleNamespace(
        a=SimpleNamespace(FreeSid=_returns(None)), sid_text=_returns("S-1-15-2-1")
    )

    @contextmanager
    def mutex(*_args: Any):
        yield

    def grant(_native: Any, path: Path, _package: str, owned: list[Any]) -> None:
        owned.append(path)

    def close() -> None:
        events.append("ui-close")
        if ui_failure:
            raise boundary.AppContainerError("private_desktop_close")

    def desktop(_native: Any, _package: str, owned: list[Any]) -> Any:
        value = SimpleNamespace(path="private\\Processor", close=close)
        owned.append(value)
        return value

    def child(*args: Any) -> int:
        args[-2].stopped = True
        raise boundary.AppContainerError("synthetic_child_failure")

    monkeypatch.setattr(boundary, "NativeApi", _returns(native))
    monkeypatch.setattr(boundary, "_mutex", mutex)
    monkeypatch.setattr(boundary, "_profile", _returns(ctypes.c_void_p(1)))
    monkeypatch.setattr(boundary, "_require_no_loopback_exemption", _returns(None))
    monkeypatch.setattr(boundary, "_grant", grant)
    monkeypatch.setattr(boundary, "_PrivateDesktop", desktop)
    monkeypatch.setattr(boundary, "_start_child", child)

    def remove_grant(*_args: Any) -> None:
        events.append("grant-remove")

    monkeypatch.setattr(boundary, "_remove_grant", remove_grant)
    with pytest.raises(boundary.AppContainerError) as caught:
        boundary.run_filing_appcontainer(
            [str(exe)], runtime_root=runtime, input_root=source, payload=b"", timeout_seconds=1
        )
    assert caught.value.cleanup_proved is (not ui_failure)
    assert events == (["ui-close"] if ui_failure else ["ui-close", "grant-remove", "grant-remove"])
    assert caught.value.stage == (
        "private_desktop_close" if ui_failure else "synthetic_child_failure"
    )


def test_cancellation_and_deadline_bound_native_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    from types import SimpleNamespace

    clock = [0.0]
    waits: list[tuple[int, int]] = []

    def wait(handle: int, milliseconds: int) -> int:
        waits.append((handle, milliseconds))
        if handle == 99:
            return 0 if clock[0] >= 0.15 else 258
        clock[0] += milliseconds / 1000
        return 258

    native: Any = SimpleNamespace(k=SimpleNamespace(WaitForSingleObject=wait))
    monkeypatch.setattr(boundary.time, "monotonic", lambda: clock[0])
    deadline = getattr(boundary, "_Deadline")(native, 1, 99)
    with pytest.raises(boundary.AppContainerError, match="invocation_cancelled"):
        deadline.wait(1)
    assert all(milliseconds <= 100 for _, milliseconds in waits)
    assert clock[0] < 1
    clock[0] = 0
    deadline = getattr(boundary, "_Deadline")(native, 1, None)
    with pytest.raises(boundary.AppContainerError, match="execution_deadline_exceeded"):
        deadline.wait(1)
    assert clock[0] < 1.2


def test_native_error_retains_fixed_stage_and_error_code(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(boundary, "_last_error", _returns(5))
    with pytest.raises(boundary.AppContainerError) as caught:
        boundary.NativeApi.check(False, "private desktop creation")
    assert caught.value.stage == "private_desktop_creation"
    assert caught.value.winerror == 5
    assert not caught.value.cleanup_proved


def test_pre_native_validation_refusal_proves_no_cleanup_needed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unavailable_native() -> Any:
        pytest.fail("native access occurred before validation")

    monkeypatch.setattr(boundary, "NativeApi", unavailable_native)
    with pytest.raises(boundary.AppContainerError) as caught:
        boundary.run_filing_appcontainer(
            ["missing"],
            runtime_root=tmp_path,
            input_root=tmp_path,
            payload=b"",
            timeout_seconds=3601,
        )
    assert caught.value.stage == "child_timeout_is_outside_the_supported_bound"
    assert caught.value.cleanup_proved


@pytest.mark.parametrize("restore_fails", [False, True])
def test_actual_private_desktop_constructor_restore_and_close_lifecycle(
    monkeypatch: pytest.MonkeyPatch, restore_fails: bool
) -> None:
    import ctypes
    from contextlib import contextmanager
    from types import SimpleNamespace

    owner, package = "S-1-5-18", "S-1-15-2-1"
    selected = [10, 20]
    events: list[str] = []
    owned: list[Any] = []

    @contextmanager
    def descriptor(_sddl: str):
        yield ctypes.c_void_p(1)

    def security(handle: int, _kind: int, **_kwargs: Any) -> tuple[Any, ...]:
        station = handle == 11
        return (
            owner,
            True,
            (
                _ace(owner, mask=0xF037F if station else 0xF01FF),
                _ace(package, mask=0x20023 if station else 0x20087),
            ),
            (_ace("S-1-16-4096", kind=0x11, mask=1),),
        )

    def select_station(handle: int) -> bool:
        events.append(f"station-{handle}")
        selected[0] = handle
        return True

    def select_desktop(handle: int) -> bool:
        events.append(f"desktop-{handle}")
        if restore_fails:
            return False
        selected[1] = handle
        return True

    def user_flags(_handle: int, _index: int, _flags: Any, size: int, needed: Any) -> bool:
        ctypes.cast(needed, ctypes.POINTER(ctypes.c_ulong))[0] = size
        return True

    def close_desktop(_handle: int) -> bool:
        events.append("close-desktop")
        return True

    def close_station(_handle: int) -> bool:
        events.append("close-station")
        return True

    def current_desktop(_tid: Any) -> int:
        return selected[1]

    native: Any = SimpleNamespace(
        owner_sid=_returns(owner),
        descriptor=descriptor,
        security=security,
        check=_check,
        k=SimpleNamespace(GetCurrentThreadId=_returns(1)),
        u=SimpleNamespace(
            GetProcessWindowStation=lambda: selected[0],
            GetThreadDesktop=current_desktop,
            CreateWindowStationW=_returns(11),
            CreateDesktopW=_returns(21),
            SetProcessWindowStation=select_station,
            SetThreadDesktop=select_desktop,
            GetUserObjectInformationW=user_flags,
            CloseDesktop=close_desktop,
            CloseWindowStation=close_station,
            OpenWindowStationW=_returns(0),
        ),
    )
    monkeypatch.setattr(boundary, "_last_error", _returns(2))
    constructor = getattr(boundary, "_PrivateDesktop")
    if restore_fails:
        with pytest.raises(boundary.AppContainerError):
            constructor(native, package, owned)
        assert len(owned) == 1
        assert not owned[0].context_restored
        with pytest.raises(
            boundary.AppContainerError, match="creator_ui_context_restoration_unproved"
        ):
            owned[0].close()
        assert "close-desktop" not in events
        assert "close-station" not in events
    else:
        desktop = constructor(native, package, owned)
        assert selected == [10, 20]
        assert desktop.context_restored
        desktop.close()
        assert desktop.desktop is None
        assert desktop.station is None
        assert events[-2:] == ["close-desktop", "close-station"]
