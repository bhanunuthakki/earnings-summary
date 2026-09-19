"""Supervise the configured Portfolio Tracker API under its managed environment.

This is the long-running Scheduler entrypoint for the tracker.  It accepts no
implicit sibling checkout or network bind: both the tracker root and the
loopback API URL must be configured explicitly before it launches uvicorn.
Its mutable receipt is rooted at the explicit product-state root or the root
containing the configured canonical database, never at the code checkout.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from integrations.portfolio_tracker_v1 import TrackerV1Client  # noqa: E402
from operations.paths import (  # noqa: E402
    configured_product_state_root,
    portfolio_tracker_receipt_path,
)
from runtime.portfolio_tracker import (  # noqa: E402
    AtomicFileLease,
    ListenerObservation,
    PortfolioTrackerRuntimeManager,
    RuntimeConfig,
    RuntimeReceipt,
    derive_daily_refresh_idempotency_key,
    endpoint_owner_matches_pid,
    health_is_healthy,
    parse_tracker_bind_url,
    write_runtime_receipt_under_lease,
)

LISTENER_OWNER = "portfolio-tracker-service"
HEARTBEAT_SECONDS = 300.0
PROBE_ATTEMPTS = 3
PROBE_RETRY_SECONDS = 5.0
LifecycleState = Literal["already_running", "started", "ownership_conflict", "failed"]


def _liveness_is_responding() -> bool:
    """Fixed local, data-independent probe; never log response/exception text."""
    try:
        with urllib.request.urlopen("http://127.0.0.1:8000/api/health", timeout=3.0) as response:
            return response.status == 200 and json.loads(response.read(1024)) == {"status": "ok"}
    except (OSError, ValueError, urllib.error.URLError):
        return False


def tracker_server_argv(
    *,
    tracker_root_raw: str | None,
    api_url: str | None,
    windows: bool | None = None,
) -> tuple[str, ...]:
    """Return the only supported tracker server command or fail closed."""

    if not tracker_root_raw or not tracker_root_raw.strip():
        raise ValueError("PORTFOLIO_TRACKER_ROOT is required for tracker activation")
    if not api_url or not api_url.strip():
        raise ValueError("PORTFOLIO_TRACKER_API_URL is required for tracker activation")
    tracker_root = Path(tracker_root_raw).expanduser().resolve()
    if not tracker_root.is_dir():
        raise ValueError(f"configured Portfolio Tracker root not found at {tracker_root}")
    bind = parse_tracker_bind_url(api_url)
    if bind != ("127.0.0.1", 8000):
        raise ValueError(
            "configured Portfolio Tracker API URL must be exactly http://127.0.0.1:8000"
        )
    host, port = "127.0.0.1", 8000
    is_windows = os.name == "nt" if windows is None else windows
    python = tracker_root / (".venv/Scripts/python.exe" if is_windows else ".venv/bin/python")
    if not python.is_file():
        raise ValueError(f"managed Portfolio Tracker Python not found at {python}")
    return (
        str(python),
        "-m",
        "uvicorn",
        "portfolio_tracker.api.main:app",
        "--host",
        host,
        "--port",
        str(port),
    )


class TrackerServiceSupervisor:
    """Keep a Scheduler-owned tracker process and its ownership receipt aligned."""

    def __init__(
        self,
        *,
        argv: tuple[str, ...],
        tracker_root: Path,
        api_url: str,
        receipt_path: Path,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        launch: Callable[..., subprocess.Popen[bytes]] = subprocess.Popen,
    ) -> None:
        self._argv = argv
        self._tracker_root = tracker_root
        self._api_url = api_url
        self._receipt_path = receipt_path
        self._now = now
        self._launch = launch
        self._process: subprocess.Popen[bytes] | None = None
        self._ownership_proof: bool | None = None
        self._responding = False

    def _inspect_listener(self) -> ListenerObservation:
        process = self._process
        pid = process.pid if process is not None and process.poll() is None else None
        if pid is None:
            self._ownership_proof = None
            self._responding = False
            return ListenerObservation(healthy=False, health_checked_at=self._now())
        health_fetch = TrackerV1Client(base_url=self._api_url).get_health()
        health = health_fetch.data
        checked_at = self._now()
        healthy = bool(health_fetch.available) and health_is_healthy(health, now=checked_at)
        bind = parse_tracker_bind_url(self._api_url)
        self._ownership_proof = (
            endpoint_owner_matches_pid(bind[0], bind[1], pid, require_exclusive=True)
            if bind is not None
            else False
        )
        owned = self._ownership_proof is True
        self._responding = bool(health_fetch.available) and health is not None
        if owned and not self._responding:
            # A failed database query can make /api/v1/health return HTTP500.
            # The owned process is still live if its independent probe works;
            # restarting it cannot repair the database or refresh stale data.
            self._responding = _liveness_is_responding()
        return ListenerObservation(
            # Health without a matching child PID and loopback endpoint does
            # not prove this scheduler-owned runtime is healthy.
            healthy=healthy and owned,
            responding=self._responding and owned,
            owner=LISTENER_OWNER if owned else None,
            pid=pid if owned else None,
            health_checked_at=checked_at,
            health=health,
        )

    def _probe_failure(self) -> str | None:
        if self._ownership_proof is False:
            return "listener_ownership_mismatch"
        if self._ownership_proof is None:
            return "ownership_probe_unavailable"
        if not self._responding:
            return "listener_response_unavailable"
        return None

    def _event(
        self, event: str, *, reason: str | None = None, exit_code: int | None = None
    ) -> None:
        # Fixed operational fields only: never health payloads, exception text,
        # argv, environment, URLs, or financial data. The task log retains the
        # cause after the mutable latest receipt has been replaced by recovery.
        print(
            json.dumps(
                {
                    "event": event,
                    "at": self._now().isoformat(),
                    "reason": reason,
                    "child_exit_code": exit_code,
                }
            ),
            flush=True,
        )

    def _start_listener(self, _config: RuntimeConfig) -> None:
        self._process = self._launch(list(self._argv), cwd=self._tracker_root)

    def _write(
        self,
        *,
        lifecycle_state: LifecycleState,
        listener: ListenerObservation,
        failure: str | None,
    ) -> RuntimeReceipt | None:
        now = self._now()
        return write_runtime_receipt_under_lease(
            self._receipt_path,
            RuntimeReceipt(
                idempotency_key=derive_daily_refresh_idempotency_key(now),
                lifecycle_state=lifecycle_state,
                recorded_at=now,
                listener=listener,
                failure_detail=failure,
            ),
        )

    def _stop_child(self) -> str | None:
        """Terminate the owned child before Scheduler retries this supervisor."""

        process = self._process
        if process is None or process.poll() is not None:
            return None
        try:
            process.terminate()
            process.wait(timeout=10.0)
        except subprocess.TimeoutExpired:
            try:
                process.kill()
                process.wait(timeout=10.0)
            except (OSError, subprocess.SubprocessError) as exc:
                return f"child cleanup failed: {type(exc).__name__}"
        except (OSError, subprocess.SubprocessError) as exc:
            return f"child cleanup failed: {type(exc).__name__}"
        return None

    def _write_failure_after_cleanup(
        self,
        *,
        listener: ListenerObservation,
        cleanup_error: str | None,
        failure: str,
    ) -> None:
        failure_detail = (
            failure
            if cleanup_error is None or cleanup_error in failure
            else f"{failure}; {cleanup_error}"
        )
        persisted = self._write(
            lifecycle_state="failed",
            listener=listener,
            failure=failure_detail,
        )
        if cleanup_error is not None and (
            persisted is None
            or persisted.lifecycle_state != "failed"
            or persisted.failure_detail is None
            or cleanup_error not in persisted.failure_detail
        ):
            raise RuntimeError(f"{cleanup_error}; failure receipt evidence unavailable")

    def run(self) -> int:
        try:
            return self._run()
        except Exception as exc:
            cleanup_error = self._stop_child()
            if cleanup_error is not None:
                raise RuntimeError(f"tracker supervisor failed and {cleanup_error}") from exc
            raise

    def _run(self) -> int:
        manager = PortfolioTrackerRuntimeManager(
            config=RuntimeConfig(
                listener_owner=LISTENER_OWNER,
                daily_refresh_owner="portfolio-tracker-refresh",
                idempotency_key=derive_daily_refresh_idempotency_key(self._now()),
            ),
            inspect_listener=self._inspect_listener,
            start_listener=self._start_listener,
            now=self._now,
            lease=AtomicFileLease(self._receipt_path.with_suffix(".lease")),
            require_data_ready=False,
        )
        started = manager.ensure_running(receipt_path=self._receipt_path)
        if started.lifecycle_state == "ownership_conflict":
            if self._process is None:
                # Losing the initial decision is terminal for this supervisor.
                # Reacquiring later to report failure would make the loser a
                # second writer after the canonical owner releases.
                return 1
            cleanup_error = self._stop_child()
            if cleanup_error is not None:
                self._write_failure_after_cleanup(
                    listener=started.listener.model_copy(update={"healthy": False}),
                    cleanup_error=cleanup_error,
                    failure=(
                        f"{started.failure_detail or 'listener ownership conflict'}; "
                        f"{cleanup_error}"
                    ),
                )
            return 1
        if (
            started.lifecycle_state not in {"started", "already_running"}
            or started.listener.owner != LISTENER_OWNER
            or started.listener.pid is None
        ):
            cleanup_error = self._stop_child()
            self._write_failure_after_cleanup(
                listener=started.listener,
                cleanup_error=cleanup_error,
                failure=started.failure_detail or "listener ownership proof is missing",
            )
            return 1
        last_listener = started.listener
        child_exit_code: int | None = None
        while self._process is not None:
            try:
                child_exit_code = self._process.wait(timeout=HEARTBEAT_SECONDS)
            except subprocess.TimeoutExpired:
                pass
            else:
                break
            listener = self._inspect_listener()
            failure = self._probe_failure()
            for _attempt in range(1, PROBE_ATTEMPTS):
                if failure is None or failure == "listener_ownership_mismatch":
                    break
                self._event("tracker_probe_retry", reason=failure)
                self._write(lifecycle_state="already_running", listener=listener, failure=failure)
                time.sleep(PROBE_RETRY_SECONDS)
                listener = self._inspect_listener()
                failure = self._probe_failure()
            if failure is not None:
                self._event("tracker_supervisor_stopping", reason=failure)
                cleanup_error = self._stop_child()
                self._write_failure_after_cleanup(
                    listener=listener,
                    cleanup_error=cleanup_error,
                    failure=failure,
                )
                return 1
            if not listener.healthy:
                self._event("tracker_data_unready", reason="data_readiness_degraded")
            self._write(
                lifecycle_state="already_running",
                listener=listener,
                failure=None if listener.healthy else "data_readiness_degraded",
            )
            last_listener = listener
        listener = last_listener.model_copy(
            update={"healthy": False, "health_checked_at": self._now()}
        )
        self._event("tracker_child_exited", exit_code=child_exit_code)
        self._write(
            lifecycle_state="failed",
            listener=listener,
            failure=f"Portfolio Tracker API process exited; exit_code={child_exit_code}",
        )
        return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tracker-root", default=os.environ.get("PORTFOLIO_TRACKER_ROOT"))
    parser.add_argument("--api-url", default=os.environ.get("PORTFOLIO_TRACKER_API_URL"))
    parser.add_argument(
        "--repo-root",
        type=Path,
        help=(
            "Product-state root that owns the supervisor receipt; defaults to the root "
            "containing EARNINGS_SUMMARY_DB_PATH"
        ),
    )
    args = parser.parse_args(argv)
    try:
        server_argv = tracker_server_argv(
            tracker_root_raw=args.tracker_root,
            api_url=args.api_url,
        )
        state_root = (
            args.repo_root.resolve()
            if args.repo_root is not None
            else configured_product_state_root(PROJECT_ROOT)
        )
    except ValueError as exc:
        parser.error(str(exc))
    assert args.tracker_root is not None
    tracker_root = Path(args.tracker_root).expanduser().resolve()
    return TrackerServiceSupervisor(
        argv=server_argv,
        tracker_root=tracker_root,
        api_url=str(args.api_url),
        receipt_path=portfolio_tracker_receipt_path(state_root),
    ).run()


if __name__ == "__main__":
    raise SystemExit(main())
