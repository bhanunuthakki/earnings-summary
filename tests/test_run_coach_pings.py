"""The coach-ping entrypoint runs one lane at a time."""

from __future__ import annotations

import sqlite3
import sys
import threading
from collections.abc import Callable
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

    def record_run(_repo_root: Path, _db_path: Path) -> int:
        ran.append(True)
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


@pytest.mark.parametrize("legacy_dry_run", [False, True])
def test_run_retains_reconciliation_and_digest_without_telegram(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    legacy_dry_run: bool,
) -> None:
    from capture import telegram, token_store
    from research import governor

    (tmp_path / "data").mkdir()
    db_path = migrated_db(tmp_path / "data" / "portfolio.db")
    argv = ["run_coach_pings.py", "--repo-root", str(tmp_path)]
    if legacy_dry_run:
        argv.append("--dry-run")
    monkeypatch.setattr(sys, "argv", argv)
    monkeypatch.delenv("EARNINGS_SUMMARY_JOB_LOCK_PROOF", raising=False)
    calls: list[str] = []

    def reconcile(path: Path) -> dict[str, int]:
        assert path == db_path
        calls.append("reconcile")
        return {"resolved": 0}

    def collect(*args: object, **kwargs: object) -> list[governor.Moment]:
        calls.append("collect")
        return [
            governor.Moment("calibration_finding", "retirement-test", None, "finding", "test:1")
        ]

    def forbidden(*args: object, **kwargs: object) -> None:
        calls.append("telegram_boundary")
        raise AssertionError("digest pass must not use Telegram")

    monkeypatch.setattr("synthesis.auto_reconcile.auto_reconcile", reconcile)
    monkeypatch.setattr(governor, "collect_moments", collect)

    def fresh(*args: object, **kwargs: object) -> bool:
        return True

    monkeypatch.setattr(governor, "freshness_ok", fresh)
    monkeypatch.setattr(token_store, "load_token", forbidden)
    monkeypatch.setattr(token_store, "load_chat_id", forbidden)
    monkeypatch.setattr(telegram, "send_message", forbidden)

    assert run_coach_pings.main() == 0
    assert calls == ["reconcile", "collect"]
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT status FROM coach_pings").fetchall() == [("digest",)]
    assert len(governor.digest_pings(db_path)) == 1
    assert run_coach_pings.main() == 0
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM coach_pings").fetchone() == (1,)


def test_legacy_dry_run_warns_that_digest_pass_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        sys, "argv", ["run_coach_pings.py", "--repo-root", str(tmp_path), "--dry-run"]
    )

    def run(_repo_root: Path, _db_path: Path) -> int:
        return 0

    monkeypatch.setattr(run_coach_pings, "_run", run)
    assert run_coach_pings.main() == 0
    assert "deprecated" in capsys.readouterr().err
