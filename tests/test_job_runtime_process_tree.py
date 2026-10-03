"""Windows process-tree ownership regressions for the shared job runtime."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Protocol, cast

import pytest

import runtime.job_runtime as job_runtime
from runtime.job_runtime import main


class WindowsJobForTest(Protocol):
    @staticmethod
    def create_for_process(pid: int) -> WindowsJobForTest: ...

    def close(self) -> None: ...


class RuntimeTestAPI(Protocol):
    run_managed_child: Callable[..., int]
    process_is_in_job: Callable[[int], bool]
    pid_is_alive: Callable[[int], bool]
    process_start_identity: Callable[[int], str | None]
    windows_job: type[WindowsJobForTest]
    create_suspended: int
    resume_process_threads: Callable[[int], None]


_RUNTIME_MEMBERS = {
    "run_managed_child": "_run_managed_child",
    "process_is_in_job": "_process_is_in_job",
    "pid_is_alive": "_pid_is_alive",
    "process_start_identity": "_process_start_identity",
    "windows_job": "_WindowsKillOnCloseJob",
    "resume_process_threads": "_resume_process_threads",
}
_runtime_members = {
    public: vars(job_runtime)[private] for public, private in _RUNTIME_MEMBERS.items()
}
assert all(callable(member) for member in _runtime_members.values())
assert isinstance(vars(job_runtime)["_CREATE_SUSPENDED"], int)
runtime_test_api = cast(
    RuntimeTestAPI,
    SimpleNamespace(**_runtime_members, create_suspended=vars(job_runtime)["_CREATE_SUSPENDED"]),
)
_run_managed_child = runtime_test_api.run_managed_child
_process_is_in_job = runtime_test_api.process_is_in_job


def current_scheduler_owner() -> tuple[int, str | None] | None:
    owner: object = vars(job_runtime)["_SCHEDULER_OWNER"]
    if owner is None:
        return None
    assert isinstance(owner, tuple)
    items = cast(tuple[object, ...], owner)
    assert len(items) == 2
    assert isinstance(items[0], int)
    assert items[1] is None or isinstance(items[1], str)
    return items[0], items[1]


class _SuspendedProcess:
    pid = 43211

    def __init__(self) -> None:
        self.resumed = False

    def poll(self) -> int | None:
        return 0 if self.resumed else None

    def wait(self, timeout: float | None = None) -> int:
        del timeout
        return 0

    def kill(self) -> None:
        self.resumed = True


class _RecordingJob:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    def close(self) -> None:
        self.events.append("close")


def test_windows_child_is_assigned_while_suspended_before_resume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    process = _SuspendedProcess()
    events: list[str] = []
    observed_flags: list[int] = []

    def fake_popen(*_args: object, **kwargs: object) -> _SuspendedProcess:
        observed_flags.append(cast("int", kwargs["creationflags"]))
        return process

    def assign(_process: object) -> _RecordingJob:
        events.append("assign")
        return _RecordingJob(events)

    def resume(_pid: int) -> None:
        events.append("resume")
        process.resumed = True

    monkeypatch.setattr(job_runtime.os, "name", "nt")
    monkeypatch.setattr(job_runtime.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(job_runtime, "_create_process_tree_job", assign)
    monkeypatch.setattr(job_runtime, "_resume_process_threads", resume)

    assert (
        _run_managed_child(
            ["python", "worker.py"],
            cwd=tmp_path,
            env={},
            scheduler_owner=(1234, "win:start"),
        )
        == 0
    )
    assert observed_flags == [runtime_test_api.create_suspended]
    assert events == ["assign", "resume", "close"]


@pytest.mark.skipif(sys.platform != "win32", reason="Windows Job Object integration")
def test_windows_kill_on_close_job_terminates_process_and_descendant(tmp_path: Path) -> None:
    """Exercise nested assignment when the test host already belongs to a job."""
    gate = tmp_path / "spawn-child"
    child_pid_file = tmp_path / "child.pid"
    code = (
        "import pathlib, subprocess, sys, time; "
        "gate=pathlib.Path(sys.argv[1]); out=pathlib.Path(sys.argv[2]); "
        "\nwhile not gate.exists(): time.sleep(0.01); "
        "\nchild=subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); "
        "out.write_text(str(child.pid), encoding='ascii'); time.sleep(60)"
    )
    # The venv launcher must not create its interpreter before job assignment.
    parent = subprocess.Popen(
        [sys.executable, "-c", code, str(gate), str(child_pid_file)],
        creationflags=runtime_test_api.create_suspended,
    )
    job: WindowsJobForTest | None = None
    try:
        inherited_parent_job = _process_is_in_job(parent.pid)
        try:
            job = runtime_test_api.windows_job.create_for_process(parent.pid)
        except OSError as exc:
            if inherited_parent_job and getattr(exc, "winerror", None) == 5:
                pytest.skip("host parent job does not permit nested child jobs")
            raise
        runtime_test_api.resume_process_threads(parent.pid)
        gate.touch()
        deadline = time.monotonic() + 5
        while not child_pid_file.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert child_pid_file.exists()
        child_pid = int(child_pid_file.read_text(encoding="ascii"))

        job.close()
        job = None
        parent.wait(timeout=5)
        deadline = time.monotonic() + 5
        while runtime_test_api.pid_is_alive(child_pid) and time.monotonic() < deadline:
            time.sleep(0.01)
        assert runtime_test_api.pid_is_alive(child_pid) is False
    finally:
        if job is not None:
            job.close()
        if parent.poll() is None:
            parent.kill()
            parent.wait(timeout=5)


def test_scheduler_wrapper_tracks_its_direct_cmd_parent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    observed: list[tuple[int, str | None] | None] = []

    def start_identity(pid: int) -> str:
        return f"start:{pid}"

    def fake_run_job(**_kwargs: object) -> int:
        observed.append(current_scheduler_owner())
        return 0

    monkeypatch.setattr(job_runtime, "_process_start_identity", start_identity)
    monkeypatch.setattr(job_runtime, "run_job", fake_run_job)
    code = main(
        [
            "--repo-root",
            str(tmp_path),
            "--scheduler-wrapper",
            "--python-executable",
            sys.executable,
            "--python-bootstrap",
            "execution/sqlite_bootstrap.py",
            "--",
            "weekly",
            "portfolio-db",
            "execution/job.py",
        ]
    )

    assert code == 0
    expected = (os.getppid(), f"start:{os.getppid()}") if os.name == "nt" else None
    assert observed == [expected]


@pytest.mark.skipif(sys.platform != "win32", reason="Windows Job Object integration")
def test_managed_child_is_suspended_assigned_resumed_and_kills_descendant(
    tmp_path: Path,
) -> None:
    """Exercise the complete Windows launch path, not only the Job wrapper."""
    child_pid_file = tmp_path / "managed-child.pid"
    code = (
        "import pathlib, subprocess, sys; "
        "child=subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); "
        "pathlib.Path(sys.argv[1]).write_text(str(child.pid), encoding='ascii')"
    )
    owner = (
        os.getpid(),
        runtime_test_api.process_start_identity(os.getpid()),
    )
    try:
        result = _run_managed_child(
            [sys.executable, "-c", code, str(child_pid_file)],
            cwd=tmp_path,
            env=dict(os.environ),
            scheduler_owner=owner,
        )
    except OSError as exc:
        if getattr(exc, "winerror", None) == 5 and _process_is_in_job(os.getpid()):
            pytest.skip("host parent job does not permit nested child jobs")
        raise
    assert result == 0
    assert child_pid_file.exists()
    child_pid = int(child_pid_file.read_text(encoding="ascii"))
    deadline = time.monotonic() + 5
    while runtime_test_api.pid_is_alive(child_pid) and time.monotonic() < deadline:
        time.sleep(0.01)
    assert runtime_test_api.pid_is_alive(child_pid) is False


@pytest.mark.skipif(sys.platform != "win32", reason="Windows Job Object integration")
def test_managed_deadline_kills_live_worker_and_descendant(tmp_path: Path) -> None:
    """The live scheduler owner cannot let an over-budget child escape its job."""
    child_pid_file = tmp_path / "deadline-child.pid"
    parent_pid_file = tmp_path / "deadline-parent.pid"
    code = (
        "import os, pathlib, subprocess, sys, time; "
        "child=subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); "
        "pathlib.Path(sys.argv[1]).write_text(str(child.pid), encoding='ascii'); "
        "pathlib.Path(sys.argv[2]).write_text(str(os.getpid()), encoding='ascii'); time.sleep(60)"
    )
    owner = (os.getpid(), runtime_test_api.process_start_identity(os.getpid()))
    try:
        with pytest.raises(job_runtime.JobDeadlineExceededError):
            _run_managed_child(
                [sys.executable, "-c", code, str(child_pid_file), str(parent_pid_file)],
                cwd=tmp_path,
                env=dict(os.environ),
                scheduler_owner=owner,
                timeout_seconds=3,
            )
    except OSError as exc:
        if getattr(exc, "winerror", None) == 5 and _process_is_in_job(os.getpid()):
            pytest.skip("host parent job does not permit nested child jobs")
        raise
    assert child_pid_file.exists()
    assert parent_pid_file.exists()
    for pid_file in (child_pid_file, parent_pid_file):
        pid = int(pid_file.read_text(encoding="ascii"))
        deadline = time.monotonic() + 5
        while runtime_test_api.pid_is_alive(pid) and time.monotonic() < deadline:
            time.sleep(0.01)
        assert not runtime_test_api.pid_is_alive(pid)
