# pyright: reportPrivateUsage=false
"""Regression coverage for the shared scheduled/interactive job runtime."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

import runtime.job_runtime as job_runtime
from runtime.job_runtime import (
    JobAlreadyRunningError,
    JobLock,
    _run_managed_child,
    _scheduler_write_sets,
    _windows_mutex_name,
    _write_set_lock_path,
    allow_nested_job_locks,
    inherited_lock_is_valid,
    main,
    run_job,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]


_PORTFOLIO_DB_POLICY = {
    "backfill-earnings-surprises-fetch": "market-data-refresh",
    "backfill-earnings-surprises-ingest": "market-data-refresh",
    "backfill-transcripts": "transcript-refresh",
    "check-comp-set-drift": "comp-metrics",
    "coach-pings": "notification-delivery",
    "compute-macro-sensitivities": "market-data-refresh",
    "daily_fetch_and_brief": "research-synthesis",
    "db-gc": "portfolio-db",
    "decision-nudge": "notification-delivery",
    "disclosure-change-sweep": "research-synthesis",
    "discover-ir-documents": "ir-discovery",
    "discover-ir-failing": "ir-discovery",
    "fetch-fmp-earnings-calendar": "fmp-refresh",
    "fetch-macro-series": "market-data-refresh",
    "fetch-sec-xbrl": "sec-companyfacts",
    "grade-calibration": "llm-evaluation",
    "ledger-synthesis": "research-synthesis",
    "monthly-advisor-memos": "research-synthesis",
    "monthly-calibration-scorecard": "llm-evaluation",
    "monthly_p3_refresh": "lens-refresh",
    "morning_pipeline": "morning-orchestration",
    "refresh-business-factors": "research-synthesis",
    "refresh-dirty-artifacts": "artifact-refresh",
    "refresh-expected-earnings": "market-data-refresh",
    "refresh-ir-kpis": "ir-discovery",
    "refresh_fmp": "fmp-refresh",
    "refresh_cache": "fmp-refresh",
    "refresh_scenario_priors": "research-synthesis",
    "restore-drill": "backup-restore",
    "scan-ir-transcripts": "transcript-refresh",
    "senior-partner-brief": "notification-delivery",
    "submit-saydo-batch-prepare": "saydo-batch",
    "submit-saydo-batch-submit": "saydo-batch",
    "tenet-accountability": "research-synthesis",
    "thesis-collision": "research-synthesis",
    "track-comp-metrics-build-sets": "comp-metrics",
    "track-comp-metrics-record": "comp-metrics",
    "weekly-cleanup": "filesystem-maintenance",
    "weekly-cleanup-expire-research": "portfolio-db",
    "weekly-model-eval": "llm-evaluation",
    "weekly-p2-lens-refresh": "lens-refresh",
    "weekly-packet": "notification-delivery",
    "weekly-score-stances": "advisor-scoring",
    "weekly-synthesis": "research-synthesis",
    "weekly-validation": "portfolio-db",
}
_LONG_RUNNING_LANES = tuple(
    (job_name, lane) for job_name, lane in _PORTFOLIO_DB_POLICY.items() if lane != "portfolio-db"
)


def _wait_for(path: Path, process: subprocess.Popen[str], *, timeout_s: float = 5.0) -> None:
    deadline = time.monotonic() + timeout_s
    while not path.exists() and process.poll() is None and time.monotonic() < deadline:
        time.sleep(0.01)
    assert path.exists(), process.communicate(timeout=1)


def _subprocess_env() -> dict[str, str]:
    return {
        **os.environ,
        "PYTHONPATH": os.pathsep.join((str(PROJECT_ROOT), str(PROJECT_ROOT / "src"))),
    }


class _PollingProcess:
    pid = 43210

    def __init__(self) -> None:
        self.terminated = False

    def poll(self) -> int | None:
        return -15 if self.terminated else None

    def wait(self, timeout: float | None = None) -> int:
        del timeout
        return -15

    def kill(self) -> None:
        self.terminated = True


def test_scheduler_owner_exit_terminates_managed_child_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    process = _PollingProcess()
    terminated: list[int] = []

    def fake_popen(*_args: object, **_kwargs: object) -> _PollingProcess:
        return process

    def owner_is_dead(_owner: tuple[int, str | None]) -> bool:
        return False

    monkeypatch.setattr(job_runtime.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(job_runtime, "_process_identity_is_alive", owner_is_dead)

    def terminate_tree(target: _PollingProcess) -> None:
        terminated.append(target.pid)
        target.terminated = True

    monkeypatch.setattr(job_runtime, "_terminate_process_tree", terminate_tree)

    exit_code = _run_managed_child(
        ["python", "worker.py"],
        cwd=tmp_path,
        env={},
        scheduler_owner=(1234, "win:start"),
    )

    assert exit_code == -15
    assert terminated == [process.pid]


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
            # wait_s=0 pins this test's actual subject: the stale-break RACE
            # must leave exactly one owner, with the loser observing busy at
            # that instant. With the default bounded wait the loser would
            # simply acquire after the winner releases — correct behavior,
            # but it hides the single-winner invariant this test exists for.
            with JobLock(tmp_path, name, ["portfolio-db"], wait_s=0):
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


@pytest.mark.parametrize(("job_name", "lane"), sorted(_PORTFOLIO_DB_POLICY.items()))
def test_scheduler_applies_reviewed_portfolio_db_policy(job_name: str, lane: str) -> None:
    assert _scheduler_write_sets(job_name, ["portfolio-db"]) == [lane]


def test_scheduler_preserves_unknown_and_explicit_write_sets() -> None:
    assert _scheduler_write_sets("new-unreviewed-job", ["portfolio-db"]) == ["portfolio-db"]
    assert _scheduler_write_sets("refresh_cache", ["operator-explicit"]) == ["operator-explicit"]


def test_every_cron_portfolio_db_job_has_an_explicit_reviewed_classification() -> None:
    observed: set[str] = set()
    invocation = re.compile(
        r'run_python\.bat"\s+"([^"]+)"\s+"portfolio-db"',
        re.IGNORECASE,
    )
    for wrapper in (PROJECT_ROOT / "cron").glob("*.bat"):
        for line in wrapper.read_text(encoding="utf-8").splitlines():
            if line.lstrip().lower().startswith("rem "):
                continue
            match = invocation.search(line)
            if match is not None:
                observed.add(match.group(1))

    assert observed == set(_PORTFOLIO_DB_POLICY) - {"refresh_fmp"}


def test_only_proven_bounded_database_jobs_keep_the_coarse_mutex() -> None:
    # db-gc owns deliberate batch/VACUUM maintenance; expiry is a bounded row
    # update; weekly validation is a measured seconds-long confidence rescore.
    # Every job that reads a large file corpus or can perform external I/O is
    # intentionally absent even when it eventually writes portfolio.db.
    assert {
        job_name for job_name, lane in _PORTFOLIO_DB_POLICY.items() if lane == "portfolio-db"
    } == {
        "db-gc",
        "weekly-cleanup-expire-research",
        "weekly-validation",
    }


@pytest.mark.parametrize("lane", sorted({lane for _, lane in _LONG_RUNNING_LANES}))
def test_lane_lock_is_cross_process_single_flight(tmp_path: Path, lane: str) -> None:
    acquired = tmp_path / f"{lane}.acquired"
    release = tmp_path / f"{lane}.release"
    script = "\n".join(
        (
            "import sys, time",
            "from pathlib import Path",
            "from runtime.job_runtime import JobLock",
            "root, lane, acquired, release = Path(sys.argv[1]), sys.argv[2], Path(sys.argv[3]), Path(sys.argv[4])",
            "with JobLock(root, 'holder', [lane], wait_s=0):",
            "    acquired.write_text('held', encoding='utf-8')",
            "    while not release.exists(): time.sleep(0.01)",
        )
    )
    holder = subprocess.Popen(
        [sys.executable, "-c", script, str(tmp_path), lane, str(acquired), str(release)],
        cwd=PROJECT_ROOT,
        env=_subprocess_env(),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        _wait_for(acquired, holder)
        with pytest.raises(JobAlreadyRunningError, match=f"write set busy: {lane}"):
            JobLock(tmp_path, "contender", [lane], wait_s=0).__enter__()
    finally:
        release.touch()
        stdout, stderr = holder.communicate(timeout=5)
    assert holder.returncode == 0, (stdout, stderr)
    assert not _write_set_lock_path(tmp_path, lane).exists()


@pytest.mark.parametrize("lane", sorted({lane for _, lane in _LONG_RUNNING_LANES}))
def test_lane_lock_converges_across_checkouts_for_one_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, lane: str
) -> None:
    first = tmp_path / "checkout-one"
    second = tmp_path / "checkout-two"
    database = tmp_path / "shared" / "portfolio.db"
    monkeypatch.setenv("EARNINGS_SUMMARY_DB_PATH", str(database))

    assert _write_set_lock_path(first, lane) == _write_set_lock_path(second, lane)
    with (
        JobLock(first, "first", [lane], wait_s=0),
        pytest.raises(JobAlreadyRunningError, match=f"write set busy: {lane}"),
    ):
        JobLock(second, "second", [lane], wait_s=0).__enter__()


@pytest.mark.parametrize("lane", sorted({lane for _, lane in _LONG_RUNNING_LANES}))
def test_lane_job_leaves_global_mutex_free_and_sqlite_bounds_writer_contention(
    tmp_path: Path, lane: str
) -> None:
    db_path = tmp_path / "portfolio.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE writes (value TEXT NOT NULL)")

    slow = tmp_path / "slow-network"
    begin_write = tmp_path / "begin-write"
    writing = tmp_path / "writing"
    fail = tmp_path / "fail"
    script = "\n".join(
        (
            "import sqlite3, sys, time",
            "from pathlib import Path",
            "from runtime.job_runtime import JobLock",
            "root, db, slow, begin_write, writing, fail = map(Path, sys.argv[1:7])",
            "lane = sys.argv[7]",
            "with JobLock(root, 'lane-holder', [lane], wait_s=0):",
            "    slow.write_text('network', encoding='utf-8')",
            "    while not begin_write.exists(): time.sleep(0.01)",
            "    conn = sqlite3.connect(db, timeout=0)",
            "    try:",
            "        conn.execute('BEGIN IMMEDIATE')",
            "        conn.execute(\"INSERT INTO writes VALUES ('rolled-back')\")",
            "        writing.write_text('transaction', encoding='utf-8')",
            "        while not fail.exists(): time.sleep(0.01)",
            "        raise RuntimeError('forced child failure')",
            "    finally:",
            "        conn.rollback()",
            "        conn.close()",
        )
    )
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            script,
            str(tmp_path),
            str(db_path),
            str(slow),
            str(begin_write),
            str(writing),
            str(fail),
            lane,
        ],
        cwd=PROJECT_ROOT,
        env=_subprocess_env(),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        _wait_for(slow, holder)
        with (
            JobLock(tmp_path, "global-contender", ["portfolio-db"], wait_s=0),
            sqlite3.connect(db_path, timeout=0) as conn,
        ):
            conn.execute("INSERT INTO writes VALUES ('during-network')")

        begin_write.touch()
        _wait_for(writing, holder)
        with (
            sqlite3.connect(db_path, timeout=0) as conn,
            pytest.raises(sqlite3.OperationalError, match="database is locked"),
        ):
            conn.execute("BEGIN IMMEDIATE")
    finally:
        fail.touch()
        stdout, stderr = holder.communicate(timeout=5)

    assert holder.returncode != 0, (stdout, stderr)
    with sqlite3.connect(db_path, timeout=0) as conn:
        conn.execute("INSERT INTO writes VALUES ('after-failure')")
        rows = conn.execute("SELECT value FROM writes ORDER BY rowid").fetchall()
    assert rows == [("during-network",), ("after-failure",)]
    assert not _write_set_lock_path(tmp_path, lane).exists()


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
                "--python-bootstrap",
                "execution/sqlite_bootstrap.py",
                "--python-arg=-3.11",
                "--",
                "morning_pipeline",
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
        "job_name": "morning_pipeline",
        "write_sets": ["morning-orchestration"],
        "command": [
            "py",
            "-u",
            "-3.11",
            "execution/sqlite_bootstrap.py",
            "execution/morning pipeline.py",
            "--label",
            "two words",
        ],
        "allow_schema_drift": False,
    }


@pytest.mark.parametrize(
    ("job_name", "cli_write_sets", "expected"),
    (
        ("refresh_cache", [], ["fmp-refresh"]),
        ("refresh_cache", ["portfolio-db"], ["portfolio-db"]),
        ("refresh_cache", ["operator-explicit"], ["operator-explicit"]),
        ("new-unreviewed-job", [], ["portfolio-db"]),
    ),
)
def test_interactive_cli_applies_policy_only_to_implicit_defaults(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    job_name: str,
    cli_write_sets: list[str],
    expected: list[str],
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
    argv = ["--repo-root", str(tmp_path), "--job", job_name]
    for write_set in cli_write_sets:
        argv.extend(("--write-set", write_set))
    argv.extend(("--", sys.executable, "-c", "pass"))

    assert main(argv) == 0
    assert captured["write_sets"] == expected


def test_scheduled_and_interactive_known_job_contend_on_same_lane(tmp_path: Path) -> None:
    scheduled_lane = _scheduler_write_sets("refresh_cache", ["portfolio-db"])
    interactive_lane = _scheduler_write_sets("refresh_cache", ["portfolio-db"])
    assert scheduled_lane == interactive_lane == ["fmp-refresh"]
    with (
        JobLock(tmp_path, "scheduled-refresh", scheduled_lane, wait_s=0),
        pytest.raises(JobAlreadyRunningError, match="write set busy: fmp-refresh"),
    ):
        JobLock(tmp_path, "interactive-refresh", interactive_lane, wait_s=0).__enter__()


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
    assert record["severity"] == "info"
    assert record["write_sets"] == ["portfolio-db"]


@pytest.mark.parametrize(
    ("job_name", "child_exit", "expected_status", "expected_severity"),
    [
        ("refresh_cache", 2, "degraded_corpus", "warning"),
        ("refresh_cache", 3, "partial", "warning"),
        ("refresh_cache", 4, "failed", "error"),
        ("unrelated_job", 2, "failed", "error"),
    ],
)
def test_refresh_cache_contained_exits_do_not_mask_failures_or_unrelated_jobs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    job_name: str,
    child_exit: int,
    expected_status: str,
    expected_severity: str,
) -> None:
    def no_drift(_repo_root: Path, _job_name: str) -> str | None:
        return None

    def selected_exit(
        _command: list[str],
        *,
        cwd: Path,
        env: dict[str, str],
        scheduler_owner: tuple[int, str | None] | None,
    ) -> int:
        del cwd, env, scheduler_owner
        return child_exit

    monkeypatch.setattr(job_runtime, "_schema_preflight", no_drift)
    monkeypatch.setattr(job_runtime, "_run_managed_child", selected_exit)

    code = run_job(
        repo_root=tmp_path,
        job_name=job_name,
        write_sets=["fmp-refresh"],
        command=[sys.executable, "-c", "pass"],
    )

    assert code == child_exit
    record_path = next((tmp_path / ".tmp" / "job_health" / job_name).glob("*.json"))
    record = json.loads(record_path.read_text(encoding="utf-8"))
    assert record["status"] == expected_status
    assert record["severity"] == expected_severity
    if expected_status == "failed":
        assert record["detail"] is None


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
