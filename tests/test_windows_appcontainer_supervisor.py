from __future__ import annotations

import io
import json
import threading
from collections.abc import Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from runtime import windows_appcontainer_supervisor as module


@pytest.fixture
def supervised(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> module.SourceLauncherSupervisor:
    state, runtime, inputs = (tmp_path / name for name in ("state", "runtime", "input"))
    for path in (state, runtime, inputs):
        path.mkdir()

    def require(*_args: object) -> None:
        pass

    def census(_roots: Sequence[Path], **_kwargs: object) -> dict[str, object]:
        return {"exact": ["owner", "group", "dacl"]}

    monkeypatch.setattr(module, "_require_state_root", require)
    monkeypatch.setattr(module, "_acl_census", census)
    return module.SourceLauncherSupervisor(
        sandbox_state_root=state, runtime_root=runtime, input_root=inputs, installation_id="b" * 64
    )


def test_pending_or_foreign_hold_never_overwritten(
    supervised: module.SourceLauncherSupervisor,
) -> None:
    owner = supervised
    owner.path.write_bytes(b"foreign")
    with pytest.raises(module.SandboxRecoveryRequiredError, match="hold"):
        owner.__enter__()
    assert owner.path.read_bytes() == b"foreign"


def test_acl_and_caller_completion_required_before_hold_removal(
    supervised: module.SourceLauncherSupervisor, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner = supervised
    with owner:
        with pytest.raises(module.SandboxRecoveryRequiredError):
            owner.complete()
        assert owner.path.exists()
        owner.clean = True

        def changed(_roots: Sequence[Path], **_kwargs: object) -> dict[str, object]:
            return {"changed": []}

        monkeypatch.setattr(module, "_acl_census", changed)
        with pytest.raises(module.SandboxRecoveryRequiredError):
            owner.complete()
        assert owner.path.exists()

        def original(_roots: Sequence[Path], **_kwargs: object) -> dict[str, object]:
            return owner.baseline

        monkeypatch.setattr(module, "_acl_census", original)
        owner.complete()
    assert not owner.path.exists()


def _transport(
    monkeypatch: pytest.MonkeyPatch,
    owner: module.SourceLauncherSupervisor,
    *,
    forged: bool = False,
    overflow: bool = False,
    timeout: bool = False,
    stop_failure: bool = False,
    forced: bool = False,
    blocked_stdin: bool = False,
    assignment_failure: bool = False,
    kill_failure: bool = False,
    wait_timeout: bool = False,
    job_close_failure: bool = False,
    thread_start_failure: bool = False,
) -> list[str]:
    calls: list[str] = []
    writer_released = threading.Event()

    class Control:
        cancel = 100
        write = 101

        def __init__(self):
            self.reader = io.BytesIO(
                b""
                if forged
                else json.dumps(
                    {
                        "invocation_id": owner.nonce,
                        "installation_id": owner.installation,
                        "cleanup_proved": True,
                    }
                ).encode()
            )

        def close_write(self):
            calls.append("close-inherited-writer")

        def signal(self):
            calls.append("cancel")
            writer_released.set()

        def close(self):
            calls.append("close-controls")

    class BlockedWriter(io.BytesIO):
        def write(self, data: Any) -> int:
            if not writer_released.wait(timeout=3):
                raise OSError("writer not cancelled")
            calls.append("writer-stopped")
            return super().write(data)

    class Process:
        pid = 50
        returncode = 0

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            calls.append("suspended-create")
            assert kwargs["creationflags"] == 4
            assert kwargs["startupinfo"].lpAttributeList["handle_list"] == [100, 101]
            self.stdin = BlockedWriter() if blocked_stdin else io.BytesIO()
            self.stdout = io.BytesIO(
                b"x" * 100 if overflow else b"forged stdout ACK" if forged else b"ok"
            )
            self.stderr = io.BytesIO()

        def poll(self):
            return None if timeout and "cancel" not in calls else 0

        def wait(self, **_kwargs: Any) -> int:
            calls.append("wait")
            if wait_timeout:
                raise module.subprocess.TimeoutExpired("synthetic", 5)
            return 0

        def kill(self):
            calls.append("kill")
            if kill_failure:
                raise OSError("synthetic kill failure")

    class Job:
        @classmethod
        def create_for_process(cls, pid: int) -> Job:
            calls.append("assign")
            if assignment_failure:
                raise OSError("synthetic assignment failure")
            return cls()

        def stop_and_wait(self, **kwargs: Any) -> None:
            calls.append("force-stop" if kwargs.get("terminate") else "prove-stop")
            if stop_failure or (forced and not kwargs.get("terminate")):
                raise OSError("unknown stop")

        def close(self):
            calls.append("close-job")
            if job_close_failure:
                raise OSError("synthetic job close failure")

    if thread_start_failure:

        class CannotStart(threading.Thread):
            def start(self) -> None:
                calls.append("thread-start-failure")
                raise OSError("synthetic thread start failure")

        monkeypatch.setattr(module.threading, "Thread", CannotStart)

    monkeypatch.setattr(module, "_Control", Control)
    monkeypatch.setattr(module, "WindowsKillOnCloseJob", Job)

    def resume(_pid: int) -> None:
        calls.append("resume")

    monkeypatch.setattr(module, "resume_suspended_process", resume)
    monkeypatch.setattr(module, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(module.subprocess, "STARTUPINFO", SimpleNamespace, raising=False)
    monkeypatch.setattr(module.subprocess, "Popen", Process)
    if timeout:
        owner.deadline = 3
        tick = iter(range(1000))
        monkeypatch.setattr(module.time, "monotonic", lambda: float(next(tick)))
    return calls


def _run(owner: module.SourceLauncherSupervisor) -> tuple[int, bytes, bytes]:
    return owner.run(
        ["python", "launcher", "--", "parser"],
        b"payload",
        environment={},
        timeout_seconds=1,
        maximum_stdout_bytes=10,
        maximum_stderr_bytes=10,
    )


def test_assign_before_resume_and_ack_before_completion(
    supervised: module.SourceLauncherSupervisor, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner = supervised
    calls = _transport(monkeypatch, owner)
    with owner:
        assert _run(owner) == (0, b"ok", b"")
        assert calls.index("suspended-create") < calls.index("assign") < calls.index("resume")
        assert owner.path.exists()
        owner.complete()
    assert not owner.path.exists()


@pytest.mark.parametrize("failure", ["forged", "overflow", "timeout", "stop_failure"])
def test_transport_failures_preserve_canonical_hold(
    supervised: module.SourceLauncherSupervisor, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    owner = supervised
    calls = _transport(monkeypatch, owner, **{failure: True})
    with owner, pytest.raises((module.SandboxRecoveryRequiredError, OSError)):
        _run(owner)
    assert owner.path.exists()
    assert owner.stop_proved is (failure != "stop_failure")
    if failure == "timeout":
        assert "cancel" in calls
    if failure == "stop_failure":
        assert "force-stop" in calls and "close-job" not in calls


def test_forced_stop_retains_hold_even_with_trusted_ack(
    supervised: module.SourceLauncherSupervisor, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner = supervised
    calls = _transport(monkeypatch, owner, forced=True)
    with owner, pytest.raises(module.SandboxRecoveryRequiredError, match="acknowledgement"):
        _run(owner)
    assert owner.stop_proved and owner.path.exists() and "force-stop" in calls


def test_blocked_stdin_runs_in_cancelled_writer_thread(
    supervised: module.SourceLauncherSupervisor, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner = supervised
    calls = _transport(monkeypatch, owner, timeout=True, blocked_stdin=True)
    with owner, pytest.raises(module.SandboxRecoveryRequiredError):
        _run(owner)
    assert "cancel" in calls and "writer-stopped" in calls and owner.stop_proved
    assert owner.path.exists()


def test_numeric_true_cannot_forge_cleanup_ack() -> None:
    from collections.abc import Callable
    from typing import cast

    acknowledged = cast(Callable[[bytes, str, str], bool], getattr(module, "_acknowledged"))
    assert not acknowledged(
        json.dumps({"invocation_id": "a", "installation_id": "b", "cleanup_proved": 1}).encode(),
        "a",
        "b",
    )


def test_owned_job_requires_zero_native_active_processes(monkeypatch: pytest.MonkeyPatch) -> None:
    from collections.abc import Callable

    from runtime import job_runtime as jobs

    observed: list[str] = []
    active = iter((2, 0))

    class Function:
        argtypes: object = None
        restype: object = None

        def __init__(self, callback: Callable[..., int]) -> None:
            self.callback = callback

        def __call__(self, *args: Any) -> int:
            return self.callback(*args)

    def terminate(*_args: Any) -> int:
        observed.append("terminate")
        return 1

    def query(*args: Any) -> int:
        args[2]._obj.active = next(active)
        observed.append("query")
        return 1

    fake = SimpleNamespace(
        TerminateJobObject=Function(terminate), QueryInformationJobObject=Function(query)
    )

    def load(*_args: object, **_kwargs: object) -> object:
        return fake

    monkeypatch.setattr(jobs, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(jobs.ctypes, "WinDLL", load, raising=False)

    def pause(_seconds: float) -> None:
        return None

    monkeypatch.setattr(jobs.time, "sleep", pause)
    job = jobs.WindowsKillOnCloseJob(123)
    job.stop_and_wait(terminate=True)
    assert observed == ["terminate", "query", "query"]


@pytest.mark.parametrize(
    "failure,stage,stopped",
    [
        ("kill_failure", "suspended-root-kill", True),
        ("wait_timeout", "suspended-root-wait", False),
        ("job_close_failure", "job-close", True),
    ],
)
def test_cleanup_failures_always_retain_owner_resources(
    supervised: module.SourceLauncherSupervisor,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
    stage: str,
    stopped: bool,
) -> None:
    owner = supervised
    options = {failure: True}
    if failure != "job_close_failure":
        options["assignment_failure"] = True
    _transport(monkeypatch, owner, **options)
    with (
        owner,
        pytest.raises(module.SandboxRecoveryRequiredError, match="owner resources retained"),
    ):
        _run(owner)
    assert owner.path.exists() and owner.stop_proved is stopped
    assert any(error.stage == stage for error in owner.cleanup_errors)
    assert owner.retained_resources and getattr(module, "_RETAINED_OWNERS")[owner.root] is owner
    assert not owner.clean


def test_thread_start_failure_cannot_skip_unproved_owner_retention(
    supervised: module.SourceLauncherSupervisor, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner = supervised
    calls = _transport(monkeypatch, owner, thread_start_failure=True, stop_failure=True)
    with owner, pytest.raises(module.SandboxRecoveryRequiredError):
        _run(owner)
    assert "thread-start-failure" in calls and "force-stop" in calls
    assert owner.path.exists() and not owner.stop_proved
    assert owner.retained_resources and getattr(module, "_RETAINED_OWNERS")[owner.root] is owner
    assert any(error.stage == "job-stop" for error in owner.cleanup_errors)
