"""The coach-ping entrypoint runs one lane at a time."""

from __future__ import annotations

import sys
import threading
from pathlib import Path

import pytest
import run_coach_pings

from runtime.job_runtime import JobAlreadyRunningError, JobLock


def test_manual_run_cannot_overlap_a_run_holding_the_lane(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A manual run waits for, and here is refused by, the notification lane, so
    the governor's cap check and row creation never interleave across runs."""
    reconciled: list[object] = []
    monkeypatch.setattr("synthesis.auto_reconcile.auto_reconcile", reconciled.append)
    monkeypatch.setenv("ES_JOB_LOCK_WAIT_S", "0")
    monkeypatch.delenv("EARNINGS_SUMMARY_JOB_LOCK_PROOF", raising=False)
    monkeypatch.setattr(
        sys, "argv", ["run_coach_pings.py", "--repo-root", str(tmp_path), "--dry-run"]
    )
    outcome: list[BaseException] = []

    def manual_run() -> None:
        try:
            run_coach_pings.main()
        except BaseException as error:  # inspected below
            outcome.append(error)

    with JobLock(tmp_path, "coach-pings", ["notification-delivery"]):
        worker = threading.Thread(target=manual_run)
        worker.start()
        worker.join(timeout=30)

    assert len(outcome) == 1 and isinstance(outcome[0], JobAlreadyRunningError)
    assert reconciled == []


def test_scheduled_run_proceeds_under_the_lane_its_wrapper_holds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The scheduler wrapper maps coach-pings to the notification-delivery lane,
    holds it, and passes the proof down; the entrypoint must run under that lock
    instead of contending with its own ancestor."""
    from runtime.job_runtime import effective_scheduler_write_sets

    assert effective_scheduler_write_sets("coach-pings", ["portfolio-db"]) == (
        "notification-delivery",
    )
    ran: list[bool] = []

    def record_run(_repo_root: Path, _db_path: Path, *, dry_run: bool) -> int:
        ran.append(dry_run)
        return 0

    monkeypatch.setattr(run_coach_pings, "_run", record_run)
    monkeypatch.setenv("ES_JOB_LOCK_WAIT_S", "0")
    monkeypatch.setattr(
        sys, "argv", ["run_coach_pings.py", "--repo-root", str(tmp_path), "--dry-run"]
    )
    with JobLock(tmp_path, "coach-pings", ["notification-delivery"]) as lane:
        monkeypatch.setenv("EARNINGS_SUMMARY_JOB_LOCK_PROOF", lane.inheritance_proof())
        assert run_coach_pings.main() == 0
    assert ran == [True]
