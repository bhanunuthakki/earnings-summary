# pyright: reportPrivateUsage=false
"""Windows process-tree ownership regressions for the shared job runtime."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path
from typing import cast

import pytest

import runtime.job_runtime as job_runtime
from runtime.job_runtime import (
    _process_is_in_job,
    _run_managed_child,
    _WindowsKillOnCloseJob,
    main,
)


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
    assert observed_flags == [job_runtime._CREATE_SUSPENDED]
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
    parent = subprocess.Popen([sys.executable, "-c", code, str(gate), str(child_pid_file)])
    job: _WindowsKillOnCloseJob | None = None
    try:
        inherited_parent_job = _process_is_in_job(parent.pid)
        try:
            job = _WindowsKillOnCloseJob.create_for_process(parent.pid)
        except OSError as exc:
            if inherited_parent_job and getattr(exc, "winerror", None) == 5:
                pytest.skip("host parent job does not permit nested child jobs")
            raise
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
        while job_runtime._pid_is_alive(child_pid) and time.monotonic() < deadline:
            time.sleep(0.01)
        assert job_runtime._pid_is_alive(child_pid) is False
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
    parent_pid = os.getppid()

    def start_identity(pid: int) -> str:
        return f"start:{pid}"

    def fake_run_job(**_kwargs: object) -> int:
        observed.append(job_runtime._SCHEDULER_OWNER)
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
    expected = (parent_pid, f"start:{parent_pid}") if os.name == "nt" else None
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
        job_runtime._process_start_identity(os.getpid()),
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
    while job_runtime._pid_is_alive(child_pid) and time.monotonic() < deadline:
        time.sleep(0.01)
    assert job_runtime._pid_is_alive(child_pid) is False
