# pyright: reportPrivateUsage=false
"""Regression coverage for the shared scheduled/interactive job runtime."""

from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path

import pytest

import runtime.job_runtime as job_runtime
from runtime.job_runtime import (
    JobAlreadyRunningError,
    JobLock,
    _windows_mutex_name,
    _write_set_lock_path,
    allow_nested_job_locks,
    inherited_lock_is_valid,
    main,
    run_job,
)


def test_lock_excludes_same_mutable_write_set(tmp_path: Path) -> None:
    with (
        JobLock(tmp_path, "scheduled", ["portfolio-db"]),
        pytest.raises(JobAlreadyRunningError),
        JobLock(tmp_path, "interactive", ["portfolio-db"]),
    ):
        pass


def test_different_write_sets_can_run_together(tmp_path: Path) -> None:
    with (
        JobLock(tmp_path, "scheduled", ["portfolio-db"]),
        JobLock(tmp_path, "interactive", ["report-output"]),
    ):
        pass


def test_explicit_synchronous_nested_owner_borrows_and_preserves_outer_lock(
    tmp_path: Path,
) -> None:
    lock_path = _write_set_lock_path(tmp_path, "portfolio-db")
    with JobLock(tmp_path, "outer", ["portfolio-db"]):
        outer = json.loads(lock_path.read_text(encoding="utf-8"))
        with allow_nested_job_locks(), JobLock(tmp_path, "inner", ["portfolio-db"]):
            assert json.loads(lock_path.read_text(encoding="utf-8")) == outer
        assert json.loads(lock_path.read_text(encoding="utf-8")) == outer
    assert not lock_path.exists()


def test_distinct_checkouts_share_lock_for_same_canonical_db(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = tmp_path / "checkout-one"
    second = tmp_path / "checkout-two"
    database = tmp_path / "shared" / "portfolio.db"
    monkeypatch.setenv("EARNINGS_SUMMARY_DB_PATH", str(database))
    with (
        JobLock(first, "first", ["portfolio-db"]),
        pytest.raises(JobAlreadyRunningError),
        JobLock(second, "second", ["portfolio-db"]),
    ):
        pass


def test_spoofed_inheritance_name_does_not_bypass_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EARNINGS_SUMMARY_JOB_LOCKS", "portfolio-db")
    monkeypatch.delenv("EARNINGS_SUMMARY_JOB_LOCK_PROOF", raising=False)
    assert inherited_lock_is_valid(tmp_path, "portfolio-db") is False


def test_stale_lock_is_reclaimed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    lock_path = _write_set_lock_path(tmp_path, "portfolio-db")
    lock_path.parent.mkdir(parents=True)
    lock_path.write_text(
        json.dumps({"job": "dead", "pid": 12345, "token": "stale-owner"}),
        encoding="utf-8",
    )

    def dead_pid(_pid: int) -> bool:
        return False

    monkeypatch.setattr(job_runtime, "_pid_is_alive", dead_pid)

    with JobLock(tmp_path, "successor", ["portfolio-db"]):
        owner = json.loads(lock_path.read_text(encoding="utf-8"))
        assert owner["token"] != "stale-owner"


def test_reused_pid_with_different_process_start_is_reclaimed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lock_path = _write_set_lock_path(tmp_path, "portfolio-db")
    lock_path.parent.mkdir(parents=True)
    lock_path.write_text(
        json.dumps(
            {
                "job": "dead",
                "pid": 12345,
                "token": "stale-owner",
                "process_start": "proc:old",
            }
        ),
        encoding="utf-8",
    )

    def live_pid(_pid: int) -> bool:
        return True

    def replacement_start(_pid: int) -> str:
        return "proc:new"

    monkeypatch.setattr(job_runtime, "_pid_is_alive", live_pid)
    monkeypatch.setattr(job_runtime, "_process_start_identity", replacement_start)

    with JobLock(tmp_path, "replacement", ["portfolio-db"]):
        owner = json.loads(lock_path.read_text(encoding="utf-8"))
        assert owner["token"] != "stale-owner"
        assert owner["process_start"] == "proc:new"


def test_release_does_not_delete_successor_lock(tmp_path: Path) -> None:
    lock = JobLock(tmp_path, "first", ["portfolio-db"])
    lock.__enter__()
    lock_path = _write_set_lock_path(tmp_path, "portfolio-db")
    successor = {"job": "successor", "pid": 99999, "token": "successor-token"}
    lock_path.write_text(json.dumps(successor), encoding="utf-8")

    lock.__exit__(None, None, None)

    assert json.loads(lock_path.read_text(encoding="utf-8")) == successor


def test_concurrent_stale_lock_contenders_leave_one_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lock_path = _write_set_lock_path(tmp_path, "portfolio-db")
    lock_path.parent.mkdir(parents=True)
    lock_path.write_text(
        json.dumps({"job": "dead", "pid": 12345, "token": "stale-owner"}),
        encoding="utf-8",
    )
    real_pid_is_alive = job_runtime._pid_is_alive

    def pid_is_alive(pid: int) -> bool:
        return False if pid == 12345 else real_pid_is_alive(pid)

    monkeypatch.setattr(job_runtime, "_pid_is_alive", pid_is_alive)
    start = threading.Barrier(3)
    release = threading.Event()
    outcomes: list[str] = []
    thread_errors: list[BaseException] = []

    def contend(name: str) -> None:
        start.wait()
        try:
            with JobLock(tmp_path, name, ["portfolio-db"]):
                outcomes.append(f"acquired:{name}")
                release.wait(timeout=2)
        except JobAlreadyRunningError:
            outcomes.append(f"busy:{name}")
        except BaseException as exc:
            thread_errors.append(exc)

    contenders = [threading.Thread(target=contend, args=(name,)) for name in ("one", "two")]
    for contender in contenders:
        contender.start()
    start.wait()
    deadline = time.monotonic() + 2
    while len(outcomes) + len(thread_errors) < 2 and time.monotonic() < deadline:
        time.sleep(0.001)
    release.set()
    for contender in contenders:
        contender.join(timeout=2)

    assert thread_errors == []
    assert len([outcome for outcome in outcomes if outcome.startswith("acquired:")]) == 1
    assert len([outcome for outcome in outcomes if outcome.startswith("busy:")]) == 1


def test_windows_mutex_is_cross_session(tmp_path: Path) -> None:
    assert _windows_mutex_name(tmp_path / "portfolio-db.lock").startswith(
        "Global\\earnings-summary-"
    )


def test_scheduler_wrapper_preserves_script_arguments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, object] = {}

    def fake_run_job(
        *,
        repo_root: Path,
        job_name: str,
        write_sets: list[str],
        command: list[str],
        allow_schema_drift: bool,
    ) -> int:
        captured.update(
            repo_root=repo_root,
            job_name=job_name,
            write_sets=write_sets,
            command=command,
            allow_schema_drift=allow_schema_drift,
        )
        return 0

    monkeypatch.setattr(job_runtime, "run_job", fake_run_job)
    assert (
        main(
            [
                "--repo-root",
                str(tmp_path),
                "--scheduler-wrapper",
                "--python-executable",
                "py",
                "--python-arg=-3.11",
                "--",
                "morning",
                "portfolio-db",
                "execution/morning pipeline.py",
                "--label",
                "two words",
            ]
        )
        == 0
    )
    assert captured == {
        "repo_root": tmp_path.resolve(),
        "job_name": "morning",
        "write_sets": ["portfolio-db"],
        "command": [
            "py",
            "-3.11",
            "execution/morning pipeline.py",
            "--label",
            "two words",
        ],
        "allow_schema_drift": False,
    }


def test_run_job_writes_machine_readable_health(tmp_path: Path) -> None:
    code = run_job(
        repo_root=tmp_path,
        job_name="unit-job",
        write_sets=["portfolio-db"],
        command=[sys.executable, "-c", "print('ok')"],
    )
    assert code == 0
    records = list((tmp_path / ".tmp" / "job_health" / "unit-job").glob("*.json"))
    assert len(records) == 1
    record = json.loads(records[0].read_text(encoding="utf-8"))
    assert record["status"] == "ok"
    assert record["write_sets"] == ["portfolio-db"]


def test_inherited_lock_valid_when_holder_is_grandparent_not_direct_parent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: the scheduler wrapper spawns the job through an intermediate
    process, so the lock holder is an ANCESTOR, not ``os.getppid()``. Requiring
    direct parentage made every wrapped job re-acquire the lock its own ancestor
    held and deadlock ("write set busy"), which silently stopped the nightly
    backup for three days."""
    lock_path = _write_set_lock_path(tmp_path, "portfolio-db")
    with JobLock(tmp_path, "wrapper", ["portfolio-db"]):
        owner = json.loads(lock_path.read_text(encoding="utf-8"))
        monkeypatch.setenv(
            "EARNINGS_SUMMARY_JOB_LOCK_PROOF",
            json.dumps(
                {
                    "portfolio-db": {
                        "path": str(lock_path),
                        "token": owner["token"],
                        "pid": owner["pid"],
                    }
                }
            ),
        )
        # A shim sits between the holder and this process: getppid() is NOT the
        # holder. The proof must still validate.
        monkeypatch.setattr(job_runtime.os, "getppid", lambda: owner["pid"] + 10_000)
        assert inherited_lock_is_valid(tmp_path, "portfolio-db") is True


def test_inherited_lock_rejected_when_token_does_not_match_on_disk_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The token — not the pid relationship — is what proves inheritance."""
    lock_path = _write_set_lock_path(tmp_path, "portfolio-db")
    with JobLock(tmp_path, "wrapper", ["portfolio-db"]):
        owner = json.loads(lock_path.read_text(encoding="utf-8"))
        monkeypatch.setenv(
            "EARNINGS_SUMMARY_JOB_LOCK_PROOF",
            json.dumps(
                {
                    "portfolio-db": {
                        "path": str(lock_path),
                        "token": "forged-token",
                        "pid": owner["pid"],
                    }
                }
            ),
        )
        monkeypatch.setattr(job_runtime.os, "getppid", lambda: owner["pid"])
        assert inherited_lock_is_valid(tmp_path, "portfolio-db") is False
