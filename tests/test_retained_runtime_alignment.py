"""Behavior retained from the canonical host, verified with synthetic evidence."""

from __future__ import annotations

import os
import subprocess
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal, cast
from unittest.mock import MagicMock, Mock

import pytest
from flask import Flask
from pydantic import ValidationError

import execution.comments_server_tracker_routes as tracker_routes
import execution.serve_portfolio_tracker as tracker_server
from integrations.portfolio_tracker_v1 import HealthV1, V1Fetch
from operations import host_runtime
from operations.backup_observer import BackupConfig, observe_backup
from operations.topology_observer import TopologyConfig, compare_topology
from pipeline.operations_panel import render_host_runtime
from runtime import portfolio_tracker as runtime


def test_recent_positively_dead_lease_recovers_without_age_delay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "tracker.lease"
    path.write_text(f"999999|{time.time()}|{'a' * 32}")

    def dead(_pid: int) -> Literal["dead"]:
        return "dead"

    monkeypatch.setattr(runtime, "_pid_liveness", dead)
    lease = runtime.AtomicFileLease(path)
    assert lease.acquire()
    assert lease.release()
    assert path.with_name(".tracker.lease.takeover.lock").is_file()


@pytest.mark.parametrize("liveness", ["alive", "unknown"])
def test_lease_never_reclaims_unproven_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, liveness: str
) -> None:
    path = tmp_path / "tracker.lease"
    original = "999999|0|prior-owner"
    path.write_text(original)
    monkeypatch.setattr(runtime, "_pid_liveness", Mock(return_value=liveness))
    assert not runtime.AtomicFileLease(path).acquire()
    assert path.read_text() == original


def test_owned_responding_tracker_is_retained_without_claiming_data_readiness() -> None:
    now = datetime(2026, 9, 19, tzinfo=UTC)
    listener = runtime.ListenerObservation(
        healthy=False, responding=True, owner="tracker", pid=4321, health_checked_at=now
    )
    start = Mock(side_effect=AssertionError("responsive owner must not restart"))
    config = runtime.RuntimeConfig(
        listener_owner="tracker", daily_refresh_owner="refresh", idempotency_key="synthetic"
    )
    process_manager = runtime.PortfolioTrackerRuntimeManager(
        config=config,
        inspect_listener=lambda: listener,
        start_listener=start,
        now=lambda: now,
        require_data_ready=False,
    )
    result = process_manager.ensure_running()
    assert result.lifecycle_state == "already_running"
    assert not result.listener.healthy
    consumer_manager = runtime.PortfolioTrackerRuntimeManager(
        config=config, inspect_listener=lambda: listener, start_listener=start, now=lambda: now
    )
    assert consumer_manager.ensure_running().lifecycle_state == "failed"
    start.assert_not_called()


@pytest.mark.parametrize("endpoint", ["health", "portfolio-snapshot"])
def test_tracker_proxy_has_closed_queries_and_explicit_unavailable(
    monkeypatch: pytest.MonkeyPatch, endpoint: str
) -> None:
    client = Mock()
    client.get_health.return_value = V1Fetch[HealthV1](available=False, endpoint="/health")
    client.get_portfolio_snapshot.return_value = V1Fetch[HealthV1](
        available=False, endpoint="/snapshot"
    )
    factory = Mock(return_value=client)
    monkeypatch.setattr(tracker_routes, "TrackerV1Client", factory)
    app = Flask(__name__)
    tracker_routes.register_tracker_read_routes(app)
    with app.test_client() as browser:
        rejected = browser.get(f"/portfolio-tracker/api/v1/{endpoint}?url=untrusted")
        assert rejected.status_code == 400
        factory.assert_not_called()
        unavailable = browser.get(f"/portfolio-tracker/api/v1/{endpoint}")
        assert unavailable.status_code == 503
        assert unavailable.headers["Cache-Control"] == "no-store"
    factory.assert_called_once_with(base_url="http://127.0.0.1:8000")


@pytest.mark.parametrize(
    "url",
    [
        "https://127.0.0.1:8000",
        "http://example.test:8000",
        "http://a:b@127.0.0.1:8000",
        "http://127.0.0.1:8000/?secret=x",
    ],
)
def test_host_readiness_configuration_rejects_nonprivate_transport(url: str) -> None:
    with pytest.raises(ValidationError):
        host_runtime.OwnerConfig(name="Synthetic", kind="service", readiness_url=url)


def test_host_cache_is_bounded_stale_and_tamper_evident(tmp_path: Path) -> None:
    now = datetime(2026, 9, 19, tzinfo=UTC)
    assert host_runtime.read_cached(tmp_path, now) == ("missing", None)
    receipt = host_runtime.HostReceipt(
        observed_at=now,
        config_sha256="a" * 64,
        owners=(host_runtime.HostOwner(name="Synthetic", kind="service", state="Running"),),
    )
    host_runtime.publish_host_receipt(tmp_path, receipt)
    assert host_runtime.read_cached(tmp_path, now)[0] == "current"
    assert host_runtime.read_cached(tmp_path, now + timedelta(hours=1))[0] == "stale"
    bundle = host_runtime.build_host_bundle(tmp_path, now, "https://synthetic.test", "synthetic")
    payload = bundle.model_dump(mode="json")
    payload["receipt_state"] = "missing"
    with pytest.raises(ValidationError, match="hash mismatch"):
        host_runtime.HostRuntimeBundle.model_validate(payload)
    path = tmp_path / host_runtime.RELATIVE_RECEIPT
    path.write_bytes(b"x" * (host_runtime.LIMIT + 1))
    assert host_runtime.read_cached(tmp_path, now) == ("invalid", None)


def test_host_probe_failure_has_closed_findings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(host_runtime, "_system_probe", Mock(side_effect=OSError("secret payload")))
    result = host_runtime.observe_owner(
        host_runtime.OwnerConfig(name="Synthetic", kind="service"), datetime.now(UTC)
    )
    assert result.findings == ("probe_unavailable",)
    assert "secret payload" not in result.model_dump_json()
    rendered = render_host_runtime(
        "current",
        host_runtime.HostReceipt(
            observed_at=datetime.now(UTC), config_sha256="a" * 64, owners=(result,)
        ),
    )
    assert "probe unavailable" in rendered
    assert "no repair controls" in rendered


def test_host_missing_configuration_does_not_probe(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    probe = Mock(side_effect=AssertionError("must not probe unconfigured host"))
    monkeypatch.setattr(host_runtime, "_system_probe", probe)
    receipt = host_runtime.collect_host_receipt(tmp_path, datetime.now(UTC))
    assert receipt.state == "unavailable"
    probe.assert_not_called()


def test_topology_reports_missing_listener_and_public_funnel() -> None:
    config = TopologyConfig.model_validate(
        {
            "serve_authority": "synthetic.ts.net:443",
            "routes": {"/": "http://127.0.0.1:7421"},
            "listeners": [
                {
                    "name": "Synthetic",
                    "port": 7421,
                    "addresses": ["127.0.0.1"],
                    "executable": "C:\\Synthetic\\python.exe",
                    "command_sha256": "a" * 64,
                    "service": "Synthetic",
                }
            ],
        }
    )
    assert compare_topology(config, {}, {}) == ("topology_unavailable",)
    findings = compare_topology(
        config,
        {"listeners": [], "services": {}, "tasks": {}},
        {
            "AllowFunnel": {"synthetic.ts.net:443": True},
        },
    )
    assert set(findings) == {"listener_missing", "funnel_exposure", "serve_route_drift"}


def test_backup_log_proves_completion_but_never_restore(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    archive = tmp_path / f"scratch_{now.date().isoformat()}.tar.zst"
    archive.write_bytes(b"synthetic archive")
    log = tmp_path / "backup.log"
    log.write_text(
        "12:00:00  === backup_scratch (LIVE) ===\n"
        f"12:00:01  published: {archive}\n"
        "12:00:02  --- headless Drive API upload ---\n"
        "12:00:03  SUMMARY (LIVE): files=1, dbs=1, secrets-excluded=1, archive=test, ratio=1, duration=1min\n"
        "12:00:04  === done ===\n"
    )
    for path in (archive, log):
        os.utime(path, (now.timestamp(), now.timestamp()))
    config = BackupConfig(
        completion_log_path=str(log),
        archive_directory=str(tmp_path),
        max_age_seconds=86400,
        minimum_archive_size_bytes=1,
    )
    result = observe_backup(config, now)
    assert result.state == "healthy"
    assert result.restore_verification == "not_observed"
    assert observe_backup(config, now + timedelta(days=2)).state == "stale"
    log.write_text(log.read_text() + "12:00:05  FATAL: private failure\n")
    invalid = observe_backup(config, now)
    assert invalid.state == "invalid"
    assert invalid.archive_name is None
    assert "private failure" not in invalid.model_dump_json()


@pytest.mark.parametrize(
    "error,expected",
    [(87, "dead"), (5, "unknown"), (0, "unknown"), (123, "unknown"), (None, "unknown")],
)
def test_windows_only_missing_pid_error_authorizes_reclaim(
    monkeypatch: pytest.MonkeyPatch, error: int | None, expected: str
) -> None:
    kernel = Mock()
    kernel.OpenProcess.return_value = None
    monkeypatch.setattr(runtime.ctypes, "WinDLL", Mock(return_value=kernel), raising=False)
    monkeypatch.setattr(runtime, "_windows_last_error", Mock(return_value=error))
    pid_liveness = cast(Callable[[int], str], getattr(runtime, "_pid_liveness"))
    with monkeypatch.context() as windows:
        windows.setattr(runtime.sys, "platform", "win32")
        assert pid_liveness(4321) == expected


@pytest.mark.parametrize(
    ("status", "body", "expected"),
    [(200, b'{"status":"ok"}', True), (302, b'{"status":"ok"}', False), (200, b"bad", False)],
)
def test_supervisor_keeps_owned_liveness_when_data_endpoint_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status: int, body: bytes, expected: bool
) -> None:
    process = Mock()
    process.pid = 4321
    process.poll.return_value = None
    process.wait.side_effect = [subprocess.TimeoutExpired("synthetic", 300), 0]
    client = Mock()
    client.get_health.return_value = V1Fetch[HealthV1](available=False, endpoint="/health")
    monkeypatch.setattr(tracker_server, "TrackerV1Client", Mock(return_value=client))
    monkeypatch.setattr(tracker_server, "endpoint_owner_matches_pid", Mock(return_value=True))
    response = MagicMock(status=status)
    response.read.return_value = body
    response.__enter__.return_value = response
    connection = Mock()
    connection.getresponse.return_value = response
    factory = Mock(return_value=connection)
    monkeypatch.setattr(tracker_server.http.client, "HTTPConnection", factory)
    monkeypatch.setattr(tracker_server.time, "sleep", Mock())
    supervisor = tracker_server.TrackerServiceSupervisor(
        argv=("synthetic",),
        tracker_root=tmp_path,
        api_url="http://127.0.0.1:8000",
        receipt_path=tmp_path / "receipt.json",
        launch=Mock(return_value=process),
    )
    assert supervisor.run() == 1  # child exit remains failed, never greenwashed
    assert factory.call_count > 0
    assert all(call.args == ("127.0.0.1", 8000) for call in factory.call_args_list)
    assert all(call.kwargs == {"timeout": 3.0} for call in factory.call_args_list)
    assert all(call.args == ("GET", "/api/health") for call in connection.request.call_args_list)
    assert connection.close.call_count == factory.call_count
    receipt = runtime.RuntimeReceipt.model_validate_json((tmp_path / "receipt.json").read_bytes())
    assert not receipt.listener.healthy
    if expected:
        assert process.wait.call_count == 2
        process.terminate.assert_not_called()
        assert factory.call_count == 2
        assert receipt.failure_detail == "Portfolio Tracker API process exited; exit_code=0"
    else:
        process.terminate.assert_called_once()
        assert not receipt.listener.responding


def test_supervisor_bounds_unknown_ownership_retries_before_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    process = Mock()
    process.pid = 4321
    process.poll.return_value = None
    process.wait.side_effect = [subprocess.TimeoutExpired("synthetic", 300), 0]
    client = Mock()
    client.get_health.return_value = V1Fetch[HealthV1](available=False, endpoint="/health")
    monkeypatch.setattr(tracker_server, "TrackerV1Client", Mock(return_value=client))
    monkeypatch.setattr(
        tracker_server, "endpoint_owner_matches_pid", Mock(side_effect=[True, None, None, None])
    )
    monkeypatch.setattr(tracker_server, "_liveness_is_responding", Mock(return_value=True))
    sleep = Mock()
    monkeypatch.setattr(tracker_server.time, "sleep", sleep)
    supervisor = tracker_server.TrackerServiceSupervisor(
        argv=("synthetic",),
        tracker_root=tmp_path,
        api_url="http://127.0.0.1:8000",
        receipt_path=tmp_path / "receipt.json",
        launch=Mock(return_value=process),
    )
    assert supervisor.run() == 1
    assert sleep.call_count == tracker_server.PROBE_ATTEMPTS - 1
    process.terminate.assert_called_once()
    receipt = runtime.RuntimeReceipt.model_validate_json((tmp_path / "receipt.json").read_bytes())
    assert receipt.failure_detail == "ownership_probe_unavailable"
