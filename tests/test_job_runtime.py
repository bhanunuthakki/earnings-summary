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
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Protocol, TypedDict, cast

import pytest

import runtime.job_runtime as job_runtime
from runtime.job_runtime import (
    SCHEMA_DRIFT_TOLERANT_JOBS,
    JobAlreadyRunningError,
    JobLock,
    allow_nested_job_locks,
    inherited_lock_is_valid,
    main,
    run_job,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]


class JournalHandleForTest(Protocol):
    operation_id: str
    trace_id: str
    accepted: bool
    started: bool


class RuntimeTestAPI(Protocol):
    run_managed_child: Callable[..., int]
    scheduler_write_sets: Callable[[str, list[str]], list[str]]
    windows_mutex_name: Callable[[Path], str]
    write_set_lock_path: Callable[[Path, str], Path]
    schema_preflight: Callable[..., str | None]
    pid_is_alive: Callable[[int], bool]
    write_health: Callable[[Path, job_runtime.HealthRecord], Path]
    scheduled_deadline_seconds: Callable[[Path, str], float | None]
    journal_handle: Callable[..., JournalHandleForTest]


# This test boundary retains the actual private implementations. Validate the
# callable shape once, then give each projection its test-facing static type.
_RUNTIME_MEMBERS = {
    "run_managed_child": "_run_managed_child",
    "scheduler_write_sets": "_scheduler_write_sets",
    "windows_mutex_name": "_windows_mutex_name",
    "write_set_lock_path": "_write_set_lock_path",
    "schema_preflight": "_schema_preflight",
    "pid_is_alive": "_pid_is_alive",
    "write_health": "_write_health",
    "scheduled_deadline_seconds": "_scheduled_deadline_seconds",
    "journal_handle": "_JournalHandle",
}
_runtime_members = {
    public: vars(job_runtime)[private] for public, private in _RUNTIME_MEMBERS.items()
}
assert all(callable(member) for member in _runtime_members.values())
runtime_test_api = cast(RuntimeTestAPI, SimpleNamespace(**_runtime_members))
_run_managed_child = runtime_test_api.run_managed_child
_scheduler_write_sets = runtime_test_api.scheduler_write_sets
_windows_mutex_name = runtime_test_api.windows_mutex_name
_write_set_lock_path = runtime_test_api.write_set_lock_path


class _RequestFields(TypedDict):
    idempotency_key: str
    actor: str
    trace_id: str
    scope: dict[str, str]


def _request_fields(job_name: str, *, trace_id: str = "a" * 32) -> _RequestFields:
    return {
        "idempotency_key": f"unit:{job_name}",
        "actor": "operator",
        "trace_id": trace_id,
        "scope": {"job": job_name},
    }


def _no_schema_drift(
    _repo_root: Path,
    _job_name: str,
    *,
    code_root: Path | None = None,
) -> None:
    del code_root


def test_schema_preflight_uses_state_database_and_code_migrations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_root = tmp_path / "state"
    code_root = tmp_path / "runtime"
    expected_db = state_root / "data" / "portfolio.db"
    captured: dict[str, Path] = {}

    def describe_drift(db_path: Path, *, project_root: Path) -> SimpleNamespace:
        captured.update(db_path=db_path, project_root=project_root)
        return SimpleNamespace(message="blocked")

    def portfolio_db_path(root: Path) -> Path:
        return root / "data/portfolio.db"

    import schema_compat

    monkeypatch.setattr(job_runtime, "portfolio_db_path", portfolio_db_path)
    monkeypatch.setattr(schema_compat, "describe_drift", describe_drift)

    assert (
        runtime_test_api.schema_preflight(state_root, "unit-job", code_root=code_root) == "blocked"
    )
    assert captured == {"db_path": expected_db, "project_root": code_root}


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
    monkeypatch.setenv("EARNINGS_SUMMARY_DB_PATH", str(tmp_path / "isolated" / "portfolio.db"))
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
    monkeypatch.setenv("EARNINGS_SUMMARY_DB_PATH", str(tmp_path / "isolated" / "portfolio.db"))
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
    monkeypatch.setenv("EARNINGS_SUMMARY_DB_PATH", str(tmp_path / "isolated" / "portfolio.db"))
    lock_path = _write_set_lock_path(tmp_path, "portfolio-db")
    lock_path.parent.mkdir(parents=True)
    lock_path.write_text(
        json.dumps({"job": "dead", "pid": 12345, "token": "stale-owner"}),
        encoding="utf-8",
    )
    real_pid_is_alive = runtime_test_api.pid_is_alive

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


def test_operations_runtime_collector_scheduler_lane_is_preserved() -> None:
    assert _scheduler_write_sets(
        "collect-operations-runtime-observations", ["operations-runtime-receipts"]
    ) == ["operations-runtime-receipts"]
    assert "collect-operations-runtime-observations" in SCHEMA_DRIFT_TOLERANT_JOBS


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
        code_root: Path | None,
        job_name: str,
        write_sets: list[str],
        command: list[str],
        idempotency_key: str,
        actor: str,
        trace_id: str,
        scope: dict[str, object],
        allow_schema_drift: bool,
        trigger_kind: str,
    ) -> int:
        captured.update(
            repo_root=repo_root,
            code_root=code_root,
            job_name=job_name,
            write_sets=write_sets,
            command=command,
            idempotency_key=idempotency_key,
            actor=actor,
            trace_id=trace_id,
            scope=scope,
            allow_schema_drift=allow_schema_drift,
            trigger_kind=trigger_kind,
        )
        return 0

    monkeypatch.setattr(job_runtime, "run_job", fake_run_job)
    assert (
        main(
            [
                "--repo-root",
                str(tmp_path),
                "--code-root",
                str(tmp_path / "runtime"),
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
    assert {
        key: captured[key]
        for key in (
            "repo_root",
            "code_root",
            "job_name",
            "write_sets",
            "command",
            "allow_schema_drift",
            "trigger_kind",
        )
    } == {
        "repo_root": tmp_path.resolve(),
        "code_root": (tmp_path / "runtime").resolve(),
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
        "trigger_kind": "scheduled",
    }
    assert captured["actor"] == "task_scheduler"
    assert captured["scope"] == {"job": "morning_pipeline", "origin": "scheduled"}
    assert isinstance(captured["idempotency_key"], str)
    assert isinstance(captured["trace_id"], str)


def test_capture_poller_main_uses_validated_service_origin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, object] = {}

    def fake_run_job(**kwargs: object) -> int:
        captured.update(kwargs)
        return 0

    monkeypatch.setattr(job_runtime, "run_job", fake_run_job)
    base = [
        "--repo-root",
        str(tmp_path),
        "--trigger-kind",
        "service",
        "--scheduler-wrapper",
        "--python-executable",
        "py",
        "--python-bootstrap",
        "execution/sqlite_bootstrap.py",
        "--",
    ]
    assert main([*base, "capture-poller", "capture-poller", "execution/capture_poller.py"]) == 0
    assert captured["trigger_kind"] == "service"
    assert captured["actor"] == "managed_service"
    assert captured["scope"] == {"job": "capture-poller", "origin": "service"}
    with pytest.raises(SystemExit):
        main([*base, "refresh_cache", "portfolio-db", "execution/refresh_cache.py"])
    wrapper = (PROJECT_ROOT / "cron" / "run_capture_poller.bat").read_text(encoding="utf-8")
    assert 'set "ES_JOB_TRIGGER_KIND=service"' in wrapper
    assert 'run_python.bat" "capture-poller" "capture-poller"' in wrapper
    shared_wrapper = (PROJECT_ROOT / "cron" / "run_python.bat").read_text(encoding="utf-8")
    assert 'if /I "%ES_JOB_TRIGGER_KIND%"=="service"' in shared_wrapper
    assert 'set "ES_JOB_TRIGGER_KIND="' in shared_wrapper


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
        code_root: Path | None,
        job_name: str,
        write_sets: list[str],
        command: list[str],
        idempotency_key: str,
        actor: str,
        trace_id: str,
        scope: dict[str, object],
        allow_schema_drift: bool,
        trigger_kind: str,
    ) -> int:
        captured.update(
            repo_root=repo_root,
            code_root=code_root,
            job_name=job_name,
            write_sets=write_sets,
            command=command,
            idempotency_key=idempotency_key,
            actor=actor,
            trace_id=trace_id,
            scope=scope,
            allow_schema_drift=allow_schema_drift,
            trigger_kind=trigger_kind,
        )
        return 0

    monkeypatch.setattr(job_runtime, "run_job", fake_run_job)
    argv = ["--repo-root", str(tmp_path), "--job", job_name]
    for write_set in cli_write_sets:
        argv.extend(("--write-set", write_set))
    argv.extend(("--", sys.executable, "-c", "pass"))

    assert main(argv) == 0
    assert captured["write_sets"] == expected
    assert captured["trigger_kind"] == "manual"


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
        **_request_fields("unit-job"),
    )
    assert code == 0
    directory = tmp_path / ".tmp" / "job_health" / "unit-job"
    records = [path for path in directory.glob("*.json") if path.name != "latest.json"]
    assert len(records) == 1
    record = json.loads(records[0].read_text(encoding="utf-8"))
    latest = json.loads((directory / "latest.json").read_text(encoding="utf-8"))
    assert latest == record
    assert latest["schema_version"] == "2"
    assert latest["trigger_kind"] == "manual"
    assert latest["operation_id"].startswith("operation:")
    assert latest["journal_state"] == "unavailable"
    assert latest["journal_detail_code"] == "request_unavailable"
    assert latest["journal_reason"]
    assert record["status"] == "ok"
    assert record["severity"] == "info"
    assert record["write_sets"] == ["portfolio-db"]


def test_semantic_review_scheduler_runtime_keeps_lock_and_health_in_state_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_root = tmp_path / "product-state"
    code_root = tmp_path / "deployed-code"
    monkeypatch.setattr(job_runtime, "_schema_preflight", _no_schema_drift)

    code = run_job(
        repo_root=state_root,
        code_root=code_root,
        job_name="prepare-kpi-semantic-review",
        write_sets=["kpi-semantic-review-export"],
        command=[sys.executable, "-c", "print('ok')"],
        **_request_fields("prepare-kpi-semantic-review"),
        trigger_kind="scheduled",
    )

    assert code == 0
    health = state_root / ".tmp" / "job_health" / "prepare-kpi-semantic-review" / "latest.json"
    receipt = json.loads(health.read_text(encoding="utf-8"))
    assert receipt["write_sets"] == ["kpi-semantic-review-export"]
    assert receipt["trigger_kind"] == "scheduled"
    assert not (code_root / ".tmp" / "job_health").exists()
    assert not (state_root / ".tmp" / "job_locks" / "kpi-semantic-review-export.lock").exists()


def test_run_job_propagates_complete_journal_context_and_service_origin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    operation_id = "operation:" + "a" * 64
    trace_id = "b" * 32
    terminal: dict[str, object] = {}
    monkeypatch.setattr(job_runtime, "_schema_preflight", _no_schema_drift)

    def accept(**_kwargs: object) -> JournalHandleForTest:
        return runtime_test_api.journal_handle(operation_id, trace_id, True)

    def mark_started(**_kwargs: object) -> None:
        return None

    monkeypatch.setattr(
        job_runtime,
        "_accept_operation_journal",
        accept,
    )
    monkeypatch.setattr(job_runtime, "_mark_operation_journal_started", mark_started)

    def finish(**kwargs: object) -> None:
        terminal.update(kwargs)

    def child(
        _command: list[str],
        *,
        cwd: Path,
        env: dict[str, str],
        scheduler_owner: tuple[int, str | None] | None,
    ) -> int:
        del cwd, scheduler_owner
        assert env["ES_OPERATION_ID"] == operation_id
        assert env["ES_TRACE_ID"] == trace_id
        assert env["ES_STAGE"] == "unit-service"
        return 0

    monkeypatch.setattr(job_runtime, "_finish_operation_journal", finish)
    monkeypatch.setattr(job_runtime, "_run_managed_child", child)
    assert (
        run_job(
            repo_root=tmp_path,
            job_name="unit-service",
            write_sets=["unit-lane"],
            command=[sys.executable, "-c", "pass"],
            **_request_fields("unit-service", trace_id=trace_id),
            trigger_kind="service",
        )
        == 0
    )
    assert terminal["operation_id"] == operation_id
    receipt = json.loads(
        (tmp_path / ".tmp" / "job_health" / "unit-service" / "latest.json").read_text(
            encoding="utf-8"
        )
    )
    assert receipt["journal_state"] == "complete"
    assert receipt["trigger_kind"] == "service"


def test_lock_skipped_request_has_terminal_but_no_started_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    operation_id = "operation:" + "c" * 64
    trace_id = "d" * 32
    events: list[tuple[str, object]] = []
    monkeypatch.setenv("ES_JOB_LOCK_WAIT_S", "0")
    monkeypatch.setattr(job_runtime, "_schema_preflight", _no_schema_drift)

    def accept(**_kwargs: object) -> JournalHandleForTest:
        return runtime_test_api.journal_handle(operation_id, trace_id, True)

    def started(**kwargs: object) -> None:
        events.append(("started", kwargs))

    def terminal_event(**kwargs: object) -> None:
        events.append(("terminal", kwargs))

    def child_must_not_run(
        _command: list[str],
        *,
        cwd: Path,
        env: dict[str, str],
        scheduler_owner: tuple[int, str | None] | None,
    ) -> int:
        del cwd, env, scheduler_owner
        pytest.fail("lock-skipped job must not run child")

    monkeypatch.setattr(
        job_runtime,
        "_accept_operation_journal",
        accept,
    )
    monkeypatch.setattr(job_runtime, "_mark_operation_journal_started", started)
    monkeypatch.setattr(job_runtime, "_finish_operation_journal", terminal_event)
    monkeypatch.setattr(job_runtime, "_run_managed_child", child_must_not_run)
    with JobLock(tmp_path, "holder", ["unit-lane"], wait_s=0):
        assert (
            run_job(
                repo_root=tmp_path,
                job_name="contender",
                write_sets=["unit-lane"],
                command=[sys.executable, "-c", "pass"],
                **_request_fields("contender", trace_id=trace_id),
            )
            == 75
        )
    assert [kind for kind, _payload in events] == ["terminal"]
    terminal = events[0][1]
    assert isinstance(terminal, dict)
    assert terminal["status"] == "skipped_locked"


def test_journal_failure_never_changes_child_outcome(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(job_runtime, "_schema_preflight", _no_schema_drift)

    def fail_journal(**_kwargs: object) -> JournalHandleForTest:
        raise RuntimeError("journal unavailable")

    monkeypatch.setattr(job_runtime, "_accept_operation_journal", fail_journal)

    def child(
        _command: list[str],
        *,
        cwd: Path,
        env: dict[str, str],
        scheduler_owner: tuple[int, str | None] | None,
    ) -> int:
        del cwd, scheduler_owner
        assert "ES_OPERATION_ID" not in env
        assert len(env["ES_TRACE_ID"]) == 32
        assert env["ES_STAGE"] == "refresh_cache"
        return 3

    monkeypatch.setattr(job_runtime, "_run_managed_child", child)
    assert (
        run_job(
            repo_root=tmp_path,
            job_name="refresh_cache",
            write_sets=["fmp-refresh"],
            command=[sys.executable, "-c", "pass"],
            **_request_fields("refresh_cache"),
            trigger_kind="scheduled",
        )
        == 3
    )
    receipt = json.loads(
        (tmp_path / ".tmp" / "job_health" / "refresh_cache" / "latest.json").read_text(
            encoding="utf-8"
        )
    )
    assert receipt["status"] == "partial"
    assert receipt["exit_code"] == 3
    assert receipt["journal_state"] == "unavailable"
    assert receipt["journal_detail_code"] == "request_unavailable"
    assert "RuntimeError" in receipt["journal_reason"]
    assert receipt["trigger_kind"] == "scheduled"


def test_health_receipt_serialization_redacts_and_bounds_detail(tmp_path: Path) -> None:
    sentinel = "HEALTH-RAW-CREDENTIAL-7319"
    journal_sentinel = "JOURNAL-RAW-CREDENTIAL-7319"
    record = job_runtime.HealthRecord(
        job="unit-job",
        write_sets=["unit-lane"],
        started_at="2026-08-13T12:00:00+00:00",
        ended_at="2026-08-13T12:01:00+00:00",
        status="failed",
        exit_code=1,
        severity="error",
        detail=(
            f"https://example.test/private?api_key={sentinel} "
            + r"C:\private\owner\job.py "
            + "x" * 500
        ),
        journal_reason=f"x-api-key: {journal_sentinel} " + "y" * 500,
    )

    runtime_test_api.write_health(tmp_path, record)

    receipt = json.loads(
        (tmp_path / ".tmp" / "job_health" / "unit-job" / "latest.json").read_text(encoding="utf-8")
    )
    assert receipt["status"] == "failed"
    assert receipt["exit_code"] == 1
    assert sentinel not in receipt["detail"]
    assert "https://example.test" not in receipt["detail"]
    assert "C:\\private" not in receipt["detail"]
    assert len(receipt["detail"]) <= 240
    assert journal_sentinel not in receipt["journal_reason"]
    assert len(receipt["journal_reason"]) <= 240


def test_health_receipt_masks_complete_header_and_assignment_values(tmp_path: Path) -> None:
    sentinel = "ALPHABETONLYCREDENTIAL"
    record = job_runtime.HealthRecord(
        job="unit-job",
        write_sets=["unit-lane"],
        started_at="2026-08-13T12:00:00+00:00",
        ended_at="2026-08-13T12:01:00+00:00",
        status="failed",
        exit_code=1,
        severity="error",
        detail=(
            "Authorization: "
            + "Basic "
            + sentinel
            + f'; {"api" + "_key"} = "prefix {sentinel} suffix"'
        ),
        journal_reason=f"x-api-key: prefix {sentinel} suffix; retry=closed",
    )

    runtime_test_api.write_health(tmp_path, record)

    receipt = json.loads(
        (tmp_path / ".tmp" / "job_health" / "unit-job" / "latest.json").read_text(encoding="utf-8")
    )
    assert sentinel not in receipt["detail"]
    assert sentinel not in receipt["journal_reason"]
    assert "retry=closed" in receipt["journal_reason"]


def test_health_receipt_masks_bearer_b64token_and_preserves_safe_suffix(tmp_path: Path) -> None:
    credential = "ALPHABETONLY" + "~+TAILONLY/=="
    record = job_runtime.HealthRecord(
        job="unit-job",
        write_sets=["unit-lane"],
        started_at="2026-08-13T12:00:00+00:00",
        ended_at="2026-08-13T12:01:00+00:00",
        status="failed",
        exit_code=1,
        severity="error",
        detail="Authorization: " + "Bearer " + credential + "\ntrace=safe",
        journal_reason="request failed Bearer " + credential + "; retry=closed",
    )

    runtime_test_api.write_health(tmp_path, record)

    receipt = json.loads(
        (tmp_path / ".tmp" / "job_health" / "unit-job" / "latest.json").read_text(encoding="utf-8")
    )
    assert credential not in receipt["detail"]
    assert credential not in receipt["journal_reason"]
    assert "TAILONLY" not in receipt["detail"]
    assert "TAILONLY" not in receipt["journal_reason"]
    assert "trace=safe" in receipt["detail"]
    assert "retry=closed" in receipt["journal_reason"]


@pytest.mark.parametrize("delimiter", [")", "]", "}", '"', "'", ":", ";"])
def test_health_receipt_preserves_non_b64token_bearer_delimiter(
    tmp_path: Path, delimiter: str
) -> None:
    credential = "ALPHABETONLY" + "~+TAILONLY/=="
    record = job_runtime.HealthRecord(
        job="unit-job",
        write_sets=["unit-lane"],
        started_at="2026-08-13T12:00:00+00:00",
        ended_at="2026-08-13T12:01:00+00:00",
        status="failed",
        exit_code=1,
        severity="error",
        detail="request failed Bearer " + credential + delimiter + " suffix=safe",
    )

    runtime_test_api.write_health(tmp_path, record)

    receipt = json.loads(
        (tmp_path / ".tmp" / "job_health" / "unit-job" / "latest.json").read_text(encoding="utf-8")
    )
    assert "TAILONLY" not in receipt["detail"]
    assert delimiter + " suffix=safe" in receipt["detail"]


@pytest.mark.parametrize(
    ("job_name", "child_exit", "expected_status", "expected_severity"),
    [
        ("refresh_cache", 2, "degraded_corpus", "warning"),
        ("refresh_cache", 3, "partial", "warning"),
        ("refresh_cache", 4, "failed", "error"),
        ("fetch-macro-series", 2, "degraded_corpus", "warning"),
        ("fetch-macro-series", 3, "partial", "warning"),
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
    def no_drift(
        _repo_root: Path,
        _job_name: str,
        *,
        code_root: Path | None = None,
    ) -> str | None:
        del code_root
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
        **_request_fields(job_name),
    )

    assert code == child_exit
    record_path = tmp_path / ".tmp" / "job_health" / job_name / "latest.json"
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


def test_sec_scheduled_deadline_uses_code_root_task_policy(tmp_path: Path) -> None:
    code_root = tmp_path / "code"
    task = code_root / "cron/fetch_sec_xbrl.task.xml"
    task.parent.mkdir(parents=True)
    task.write_text(
        '<Task xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">'
        "<Settings><ExecutionTimeLimit>PT1H2M3S</ExecutionTimeLimit></Settings></Task>",
        encoding="utf-8",
    )
    assert runtime_test_api.scheduled_deadline_seconds(code_root, "fetch-sec-xbrl") == 3723
    assert runtime_test_api.scheduled_deadline_seconds(tmp_path, "unrelated-job") is None
    assert runtime_test_api.scheduled_deadline_seconds(PROJECT_ROOT, "fetch-sec-xbrl") == 7200


@pytest.mark.parametrize("duration", ["PT0S", "P0D", "invalid", "PT", ""])
def test_sec_scheduled_deadline_rejects_unbounded_policy(tmp_path: Path, duration: str) -> None:
    task = tmp_path / "cron/fetch_sec_xbrl.task.xml"
    task.parent.mkdir()
    task.write_text(
        '<Task xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">'
        f"<Settings><ExecutionTimeLimit>{duration}</ExecutionTimeLimit></Settings></Task>",
        encoding="utf-8",
    )
    with pytest.raises(OSError, match="ExecutionTimeLimit"):
        runtime_test_api.scheduled_deadline_seconds(tmp_path, "fetch-sec-xbrl")


def test_managed_child_deadline_kills_tree_with_live_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    process = _PollingProcess()
    elapsed = iter([0.0, 0.0, 7200.0])
    terminated: list[int] = []

    def popen(*_args: object, **_kwargs: object) -> _PollingProcess:
        return process

    def owner_alive(_owner: tuple[int, str | None]) -> bool:
        return True

    def monotonic() -> float:
        return next(elapsed)

    def sleep(_seconds: float) -> None:
        pass

    monkeypatch.setattr(job_runtime.subprocess, "Popen", popen)
    monkeypatch.setattr(job_runtime, "_process_identity_is_alive", owner_alive)
    monkeypatch.setattr(job_runtime.time, "monotonic", monotonic)
    monkeypatch.setattr(job_runtime.time, "sleep", sleep)

    def terminate_tree(target: _PollingProcess) -> None:
        terminated.append(target.pid)
        target.terminated = True

    monkeypatch.setattr(job_runtime, "_terminate_process_tree", terminate_tree)
    with pytest.raises(job_runtime.JobDeadlineExceededError, match="7200"):
        _run_managed_child(
            ["python", "worker.py"],
            cwd=tmp_path,
            env={},
            scheduler_owner=(1234, "win:start"),
            timeout_seconds=7200,
        )
    assert terminated == [process.pid]
    assert process.poll() == -15


def test_sec_deadline_persists_failed_health_and_journal_then_releases_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    terminal: dict[str, object] = {}
    monkeypatch.setattr(job_runtime, "_schema_preflight", _no_schema_drift)

    def accept(**_kwargs: object) -> JournalHandleForTest:
        return runtime_test_api.journal_handle("operation:" + "a" * 64, "b" * 32, True)

    def started(**_kwargs: object) -> None:
        pass

    def finish(**kwargs: object) -> None:
        terminal.update(kwargs)

    monkeypatch.setattr(job_runtime, "_accept_operation_journal", accept)
    monkeypatch.setattr(job_runtime, "_mark_operation_journal_started", started)
    monkeypatch.setattr(job_runtime, "_finish_operation_journal", finish)

    def timeout(*_args: object, **kwargs: object) -> int:
        assert kwargs["timeout_seconds"] == 7200
        raise job_runtime.JobDeadlineExceededError(7200)

    monkeypatch.setattr(job_runtime, "_run_managed_child", timeout)
    code = run_job(
        repo_root=tmp_path,
        code_root=PROJECT_ROOT,
        job_name="fetch-sec-xbrl",
        write_sets=["sec-companyfacts"],
        command=[sys.executable, "-c", "pass"],
        trigger_kind="scheduled",
        **_request_fields("fetch-sec-xbrl"),
    )
    assert code == 124
    health = json.loads(
        (tmp_path / ".tmp/job_health/fetch-sec-xbrl/latest.json").read_text(encoding="utf-8")
    )
    assert health["status"] == terminal["status"] == "failed"
    assert health["exit_code"] == terminal["exit_code"] == 124
    assert health["journal_state"] == "complete"
    assert "deadline" in health["detail"]
    assert terminal["detail_reason"] == health["detail"]
    with JobLock(tmp_path, "next-run", ["sec-companyfacts"], wait_s=0):
        pass


def test_manual_sec_run_keeps_interactive_contract_without_scheduler_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(job_runtime, "_schema_preflight", _no_schema_drift)
    assert (
        run_job(
            repo_root=tmp_path,
            job_name="fetch-sec-xbrl",
            write_sets=["sec-companyfacts"],
            command=[sys.executable, "-c", "pass"],
            **_request_fields("fetch-sec-xbrl"),
        )
        == 0
    )


def test_missing_sec_deadline_policy_fails_without_child_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(job_runtime, "_schema_preflight", _no_schema_drift)

    def child_must_not_run(*_args: object, **_kwargs: object) -> int:
        pytest.fail("invalid deadline policy launched child")

    monkeypatch.setattr(job_runtime, "_run_managed_child", child_must_not_run)
    assert (
        run_job(
            repo_root=tmp_path,
            job_name="fetch-sec-xbrl",
            write_sets=["sec-companyfacts"],
            command=[sys.executable, "-c", "pass"],
            trigger_kind="scheduled",
            **_request_fields("fetch-sec-xbrl"),
        )
        == 1
    )
    receipt = json.loads(
        (tmp_path / ".tmp/job_health/fetch-sec-xbrl/latest.json").read_text(encoding="utf-8")
    )
    assert receipt["status"] == "failed"
    assert "ExecutionTimeLimit" in receipt["detail"]


@pytest.mark.parametrize("ambiguous", [False, True])
def test_deadline_finishes_only_child_sec_attempt_and_allows_next_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    migrated_db: Callable[..., Path],
    ambiguous: bool,
) -> None:
    from models.runs import StageStatus
    from operations.context import activate
    from pipeline.run_accounting import end_run, start_run

    db_path = migrated_db(tmp_path / "deadline-ledger.db")
    monkeypatch.setenv("EARNINGS_SUMMARY_DB_PATH", str(db_path))
    runs: dict[str, str] = {}
    with sqlite3.connect(db_path) as conn:
        runs["foreign"] = start_run(conn, "fetch_sec_xbrl", ["FOREIGN"])

    def child(
        _command: list[str],
        *,
        cwd: Path,
        env: dict[str, str],
        scheduler_owner: tuple[int, str | None] | None,
        timeout_seconds: float,
    ) -> int:
        del cwd, scheduler_owner
        with (
            activate(
                operation_id=env["ES_OPERATION_ID"],
                trace_id=env["ES_TRACE_ID"],
                stage="fetch-sec-xbrl",
            ),
            sqlite3.connect(db_path) as conn,
        ):
            runs["own"] = start_run(conn, "fetch_sec_xbrl", ["NU"])
            if ambiguous:
                runs["own_second"] = start_run(conn, "fetch_sec_xbrl", ["AMBIGUOUS"])
            runs["other_directive"] = start_run(conn, "different_directive", ["NU"])
            runs["complete"] = start_run(conn, "fetch_sec_xbrl", ["COMPLETE"])
            end_run(conn, runs["complete"], StageStatus.OK)
        raise job_runtime.JobDeadlineExceededError(timeout_seconds)

    monkeypatch.setattr(job_runtime, "_run_managed_child", child)
    assert (
        run_job(
            repo_root=tmp_path,
            code_root=PROJECT_ROOT,
            job_name="fetch-sec-xbrl",
            write_sets=["sec-companyfacts"],
            command=[sys.executable, "-c", "pass"],
            trigger_kind="scheduled",
            **_request_fields("fetch-sec-xbrl"),
        )
        == 124
    )
    with sqlite3.connect(db_path) as conn:
        for key, run_id in runs.items():
            expected = (
                StageStatus.FAILED
                if key == "own" and not ambiguous
                else (StageStatus.OK if key == "complete" else StageStatus.IN_PROGRESS)
            )
            for table, identity in (
                ("ingestion_runs", "run_id"),
                ("pipeline_attempts", "attempt_id"),
            ):
                row = conn.execute(
                    f"SELECT status,error_summary FROM {table} WHERE {identity}=?", (run_id,)
                ).fetchone()
                assert row is not None and row[0] == expected.value
                if key == "own" and not ambiguous:
                    assert "deadline exceeded" in row[1]
            if key == "own" and not ambiguous:
                assert start_run(conn, "fetch_sec_xbrl", ["NU"]) != run_id
    receipt = json.loads(
        (tmp_path / ".tmp/job_health/fetch-sec-xbrl/latest.json").read_text(encoding="utf-8")
    )
    assert receipt["journal_state"] == "complete"
    if ambiguous:
        assert "attempt reconciliation unavailable" in receipt["detail"]
        assert "multiple SEC attempts" in receipt["detail"]
    else:
        assert "child SEC attempt marked failed" in receipt["detail"]


def test_sec_deadline_without_journal_keeps_failed_receipt_and_does_not_sweep(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(job_runtime, "_schema_preflight", _no_schema_drift)

    def unavailable(**_kwargs: object) -> JournalHandleForTest:
        raise OSError("operation journal unavailable")

    def child(*_args: object, **kwargs: object) -> int:
        assert kwargs["timeout_seconds"] == 7200
        raise job_runtime.JobDeadlineExceededError(7200)

    def must_not_reconcile(*_args: object, **_kwargs: object) -> str:
        pytest.fail("unattributed timeout must not change any ingestion attempt")

    monkeypatch.setattr(job_runtime, "_accept_operation_journal", unavailable)
    monkeypatch.setattr(job_runtime, "_run_managed_child", child)
    monkeypatch.setattr(job_runtime, "_finish_timed_out_sec_attempt", must_not_reconcile)
    assert (
        run_job(
            repo_root=tmp_path,
            code_root=PROJECT_ROOT,
            job_name="fetch-sec-xbrl",
            write_sets=["sec-companyfacts"],
            command=[sys.executable, "-c", "pass"],
            trigger_kind="scheduled",
            **_request_fields("fetch-sec-xbrl"),
        )
        == 124
    )
    receipt = json.loads(
        (tmp_path / ".tmp/job_health/fetch-sec-xbrl/latest.json").read_text(encoding="utf-8")
    )
    assert receipt["status"] == "failed"
    assert receipt["journal_state"] == "unavailable"
    assert "attempt reconciliation unavailable: operation journal not accepted" in receipt["detail"]
