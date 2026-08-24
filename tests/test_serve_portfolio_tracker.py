"""Fail-closed command construction for the scheduled Portfolio Tracker API."""

from __future__ import annotations

import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest

import execution.refresh_portfolio_tracker as refresh
import execution.serve_portfolio_tracker as server
from integrations.portfolio_tracker_v1 import HealthV1, V1Fetch
from runtime.portfolio_tracker import RuntimeReceipt

tracker_server_argv = server.tracker_server_argv


def test_scheduled_refresh_context_requires_running_canonical_windows_task(tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def running_task(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    assert refresh.canonical_scheduler_task_is_running(tmp_path, windows=False) is False
    assert (
        refresh.canonical_scheduler_task_is_running(tmp_path, windows=True, run=running_task)
        is True
    )
    assert calls and calls[0][:3] == ["powershell.exe", "-NoProfile", "-NonInteractive"]
    assert "refresh_portfolio_tracker" in calls[0][-1]
    assert str(os.getpid()) in calls[0][-1]
    assert "Win32_Process" in calls[0][-1]
    assert "ParentProcessId" in calls[0][-1]
    assert "GetInstances(0)" in calls[0][-1]
    assert "EnginePID" in calls[0][-1]

    def stopped_task(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, 1, "", "")

    assert (
        refresh.canonical_scheduler_task_is_running(tmp_path, windows=True, run=stopped_task)
        is False
    )


def test_tracker_server_argv_requires_explicit_existing_root_and_safe_loopback_url(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="PORTFOLIO_TRACKER_ROOT"):
        tracker_server_argv(tracker_root_raw=None, api_url="http://127.0.0.1:8000")
    with pytest.raises(ValueError, match="PORTFOLIO_TRACKER_API_URL"):
        tracker_server_argv(tracker_root_raw=str(tmp_path), api_url=None)
    with pytest.raises(ValueError, match="not found"):
        tracker_server_argv(
            tracker_root_raw=str(tmp_path / "absent"), api_url="http://127.0.0.1:8000"
        )
    with pytest.raises(ValueError, match=r"exactly http://127\.0\.0\.1:8000"):
        tracker_server_argv(tracker_root_raw=str(tmp_path), api_url="http://0.0.0.0:8000")
    with pytest.raises(ValueError, match=r"exactly http://127\.0\.0\.1:8000"):
        tracker_server_argv(tracker_root_raw=str(tmp_path), api_url="http://127.0.0.1:8123")


def test_tracker_server_argv_uses_tracker_managed_python_and_exact_loopback_bind(
    tmp_path: Path,
) -> None:
    python = tmp_path / ".venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.touch()

    argv = tracker_server_argv(
        tracker_root_raw=str(tmp_path), api_url="http://127.0.0.1:8000", windows=False
    )

    assert argv == (
        str(python),
        "-m",
        "uvicorn",
        "portfolio_tracker.api.main:app",
        "--host",
        "127.0.0.1",
        "--port",
        "8000",
    )


def test_supervisor_persists_fresh_pid_attribution_and_fails_after_child_exit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    now = datetime(2026, 8, 24, 15, tzinfo=UTC)
    health = HealthV1.model_validate(
        {
            "status": "ok",
            "schema_version": "1.0.0",
            "generated_at": now.isoformat(),
            "database_ok": True,
            "migration_version": "0023",
            "providers": [],
            "active_account_count": 1,
            "latest_snapshot_date": now.date().isoformat(),
            "is_stale": False,
            "links": {},
        }
    )

    class _Client:
        def __init__(self, **_: object) -> None:
            pass

        def get_health(self) -> V1Fetch[HealthV1]:
            return V1Fetch(available=True, endpoint="/health", data=health)

    class _Process:
        pid = 4321

        def __init__(self) -> None:
            self._polls = [None, None]
            self._waits = [True, False]

        def poll(self) -> int | None:
            return self._polls.pop(0) if self._polls else 0

        def wait(self, *, timeout: float) -> int:
            assert timeout == server.HEARTBEAT_SECONDS
            if self._waits.pop(0):
                raise subprocess.TimeoutExpired("uvicorn", timeout)
            return 0

    process = _Process()

    def endpoint_owner_matches(_host: str, _port: int, _pid: int, **kwargs: object) -> bool:
        assert kwargs == {"require_exclusive": True}
        return True

    def parse_bind(_api_url: str) -> tuple[str, int]:
        return "127.0.0.1", 8000

    def launch(*_args: object, **_kwargs: object) -> subprocess.Popen[bytes]:
        return cast(subprocess.Popen[bytes], process)

    monkeypatch.setattr(server, "TrackerV1Client", _Client)
    monkeypatch.setattr(server, "endpoint_owner_matches_pid", endpoint_owner_matches)
    monkeypatch.setattr(server, "parse_tracker_bind_url", parse_bind)
    supervisor = server.TrackerServiceSupervisor(
        argv=("python", "-m", "uvicorn"),
        tracker_root=tmp_path,
        api_url="http://127.0.0.1:8000",
        receipt_path=tmp_path / "receipt.json",
        now=lambda: now,
        launch=launch,
    )

    assert supervisor.run() == 1
    receipt = RuntimeReceipt.model_validate_json((tmp_path / "receipt.json").read_bytes())
    assert receipt.listener.pid == 4321
    assert receipt.listener.owner == "portfolio-tracker-service"
    assert receipt.listener.health_checked_at == now
    assert receipt.scheduler is not None
    assert receipt.scheduler.terminal_result == "activation_required"
    assert receipt.failure_detail == "Portfolio Tracker API process exited"


def test_supervisor_terminates_child_before_retry_after_ownership_proof_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    now = datetime(2026, 8, 24, 15, tzinfo=UTC)
    health = HealthV1.model_validate(
        {
            "status": "ok",
            "schema_version": "1.0.0",
            "generated_at": now.isoformat(),
            "database_ok": True,
            "migration_version": "0023",
            "providers": [],
            "active_account_count": 1,
            "latest_snapshot_date": now.date().isoformat(),
            "is_stale": False,
            "links": {},
        }
    )

    class _Client:
        def __init__(self, **_: object) -> None:
            pass

        def get_health(self) -> V1Fetch[HealthV1]:
            return V1Fetch(available=True, endpoint="/health", data=health)

    class _Process:
        pid = 4321

        def __init__(self) -> None:
            self._polls = [None, None, None]
            self._timed_out = False
            self.terminated = False
            self.waited = False

        def poll(self) -> int | None:
            return self._polls.pop(0) if self._polls else None

        def terminate(self) -> None:
            self.terminated = True

        def wait(self, *, timeout: float) -> int:
            if timeout == server.HEARTBEAT_SECONDS and not self._timed_out:
                self._timed_out = True
                raise subprocess.TimeoutExpired("uvicorn", timeout)
            assert timeout == 10.0
            self.waited = True
            return 1

    process = _Process()
    endpoint_results = iter((True, False))

    def endpoint_owner_matches(_host: str, _port: int, _pid: int, **kwargs: object) -> bool:
        assert kwargs == {"require_exclusive": True}
        return next(endpoint_results)

    def parse_bind(_api_url: str) -> tuple[str, int]:
        return "127.0.0.1", 8000

    def launch(*_args: object, **_kwargs: object) -> subprocess.Popen[bytes]:
        return cast(subprocess.Popen[bytes], process)

    monkeypatch.setattr(server, "TrackerV1Client", _Client)
    monkeypatch.setattr(server, "endpoint_owner_matches_pid", endpoint_owner_matches)
    monkeypatch.setattr(server, "parse_tracker_bind_url", parse_bind)
    supervisor = server.TrackerServiceSupervisor(
        argv=("python", "-m", "uvicorn"),
        tracker_root=tmp_path,
        api_url="http://127.0.0.1:8000",
        receipt_path=tmp_path / "receipt.json",
        now=lambda: now,
        launch=launch,
    )

    assert supervisor.run() == 1
    assert process.terminated is True
    assert process.waited is True
    receipt = RuntimeReceipt.model_validate_json((tmp_path / "receipt.json").read_bytes())
    assert receipt.failure_detail == "listener health or endpoint ownership proof is missing"
