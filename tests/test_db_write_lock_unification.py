# pyright: reportPrivateUsage=false
"""One database, one write lock — and contention waits instead of dying.

Before 2026-08-03 two independent lock systems guarded portfolio.db with two
DIFFERENT files: run_lock.py's ``portfolio.db.write.lock`` (db_gc, the morning
pipeline) and JobLock's hashed ``.job_locks`` path (the whole cron fleet).
Holding one never excluded the other, so the pairing that mattered most — a
backup versus a db_gc VACUUM — was never actually serialized, while the
fail-fast JobLock lost harmless races instead: 13 jobs skipped_locked in one
day, four consecutive nightly backups among them.

These tests pin the convergence: same file, mutually intelligible payloads,
stale-breaking across systems, and a bounded wait that outlives short holders.
"""

from __future__ import annotations

import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import run_lock  # noqa: E402
from runtime.job_runtime import (  # noqa: E402
    JobAlreadyRunningError,
    JobLock,
    _write_set_lock_path,
    portfolio_db_path,
)

ONBOARD_WRAPPER = PROJECT_ROOT / "cron" / "run_onboard_pending.bat"


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.delenv("EARNINGS_SUMMARY_DB_PATH", raising=False)
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "portfolio.db").write_bytes(b"")
    return tmp_path


def test_both_systems_share_one_lock_file(repo: Path) -> None:
    """The unification itself: JobLock's portfolio-db path IS run_lock's path."""
    db = portfolio_db_path(repo)
    assert _write_set_lock_path(repo, "portfolio-db") == run_lock.lock_path_for(db)


def test_run_lock_holder_blocks_joblock(repo: Path) -> None:
    """The exclusion that never existed before: a db_gc-style holder now
    genuinely excludes a cron-fleet acquirer."""
    db = portfolio_db_path(repo)
    held = run_lock.acquire_run_lock(db, owner="db_gc-style", timeout_s=0)
    try:
        with (
            pytest.raises(JobAlreadyRunningError, match="portfolio-db"),
            JobLock(repo, "cron-style", ["portfolio-db"], wait_s=0),
        ):
            pass
    finally:
        held.release()


def test_joblock_holder_blocks_run_lock(repo: Path) -> None:
    db = portfolio_db_path(repo)
    with (
        JobLock(repo, "cron-style", ["portfolio-db"], wait_s=0),
        pytest.raises(run_lock.RunLockHeldError),
    ):
        run_lock.acquire_run_lock(db, owner="db_gc-style", timeout_s=0)


def test_joblock_waits_out_a_short_holder(repo: Path) -> None:
    """The 12 ms give-up is gone: contention inside the wait window succeeds."""
    release_after = 1.5
    holder_ready = threading.Event()

    def hold_briefly() -> None:
        with JobLock(repo, "short-holder", ["portfolio-db"], wait_s=0):
            holder_ready.set()
            time.sleep(release_after)

    t = threading.Thread(target=hold_briefly)
    t.start()
    try:
        assert holder_ready.wait(10.0)
        start = time.monotonic()
        with JobLock(repo, "patient", ["portfolio-db"], wait_s=30.0):
            waited = time.monotonic() - start
        # It must have actually waited for the holder, not stale-broken it.
        assert waited >= release_after * 0.5
    finally:
        t.join()


def test_self_conflict_refuses_instantly_despite_wait(repo: Path) -> None:
    """Waiting on a lock this context already holds can only deadlock; the
    retry loop must not burn the whole wait window discovering that."""
    with JobLock(repo, "outer", ["portfolio-db"], wait_s=0):
        start = time.monotonic()
        with (
            pytest.raises(JobAlreadyRunningError),
            JobLock(repo, "inner", ["portfolio-db"], wait_s=30.0),
        ):
            pass
        assert time.monotonic() - start < 5.0


def test_joblock_breaks_a_dead_run_lock_holder(repo: Path) -> None:
    """Cross-system staleness: a run_lock file whose pid is dead must not
    block the fleet forever."""
    db = portfolio_db_path(repo)
    proc = subprocess.run(
        [sys.executable, "-c", "import os; print(os.getpid())"],
        capture_output=True,
        text=True,
        check=True,
    )
    dead_pid = int(proc.stdout.strip())
    lock_path = run_lock.lock_path_for(db)
    lock_path.write_text(
        f'{{"pid": {dead_pid}, "owner": "died", "token": "deadbeef"}}', encoding="utf-8"
    )
    with JobLock(repo, "survivor", ["portfolio-db"], wait_s=0):
        pass  # acquiring proves the corpse was broken


def test_run_lock_breaks_a_dead_joblock_holder(repo: Path) -> None:
    db = portfolio_db_path(repo)
    proc = subprocess.run(
        [sys.executable, "-c", "import os; print(os.getpid())"],
        capture_output=True,
        text=True,
        check=True,
    )
    dead_pid = int(proc.stdout.strip())
    lock_path = run_lock.lock_path_for(db)
    lock_path.write_text(
        f'{{"job": "died", "pid": {dead_pid}, "token": "deadbeef", "process_start": null}}',
        encoding="utf-8",
    )
    held = run_lock.acquire_run_lock(db, owner="survivor", timeout_s=0)
    held.release()


def test_run_lock_payload_carries_a_token(repo: Path) -> None:
    """Without a token, JobLock's reader treats a live run_lock file as corrupt
    and — worse — a DEAD one as unbreakable. The payload must carry one."""
    import json

    db = portfolio_db_path(repo)
    held = run_lock.acquire_run_lock(db, owner="payload-check", timeout_s=0)
    try:
        payload = json.loads(run_lock.lock_path_for(db).read_text(encoding="utf-8"))
        assert isinstance(payload.get("token"), str) and payload["token"]
    finally:
        held.release()


def test_onboard_wrapper_no_longer_holds_the_db_for_the_whole_run() -> None:
    """The scheduler retains lock PARTICIPATION (self-serialization of hourly
    runs), not a whole-pipeline hold of the DB write set — that hold starved
    13 jobs in one day. The per-ticker claim lives in the script."""
    text = ONBOARD_WRAPPER.read_text(encoding="utf-8", errors="replace")
    invocation = next(
        line
        for line in text.splitlines()
        if "run_python.bat" in line and "onboard_pending_tickers" in line
    )
    assert '"onboard-pending" "onboard-pending"' in invocation, invocation
    assert '"portfolio-db"' not in invocation, invocation
