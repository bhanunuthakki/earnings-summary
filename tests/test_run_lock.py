"""Tests for src/run_lock.py — the portfolio write-set run lock.

The lock is the cross-process arbiter between db_gc and the morning pipeline
orchestrator (AGENTS.md concurrency rule): file-based, keyed to the database
file, with stale-holder (dead pid) breaking.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

import run_lock  # noqa: E402


@pytest.fixture()
def db(tmp_path: Path) -> Path:
    p = tmp_path / "portfolio.db"
    p.touch()
    return p


def _dead_pid() -> int:
    """A pid guaranteed to have exited (freshly spawned, already reaped)."""
    proc = subprocess.run(
        [sys.executable, "-c", "import os; print(os.getpid())"],
        capture_output=True,
        text=True,
        check=True,
    )
    return int(proc.stdout.strip())


def test_lock_path_sits_next_to_db(db: Path) -> None:
    assert run_lock.lock_path_for(db) == db.parent / ("portfolio.db" + run_lock.LOCK_SUFFIX)


def test_acquire_writes_holder_and_release_removes(db: Path) -> None:
    lock = run_lock.acquire_run_lock(db, owner="test-owner", timeout_s=0)
    lock_path = run_lock.lock_path_for(db)
    assert lock_path.exists()
    holder = json.loads(lock_path.read_text(encoding="utf-8"))
    assert holder["pid"] == os.getpid()
    assert holder["owner"] == "test-owner"
    lock.release()
    assert not lock_path.exists()


def test_live_holder_contention_raises(db: Path) -> None:
    lock = run_lock.acquire_run_lock(db, owner="first", timeout_s=0)
    try:
        with pytest.raises(run_lock.RunLockHeldError) as excinfo:
            run_lock.acquire_run_lock(db, owner="second", timeout_s=0)
        assert excinfo.value.holder.get("owner") == "first"
        assert excinfo.value.lock_path == run_lock.lock_path_for(db)
    finally:
        lock.release()
    # Released — a fresh acquire now succeeds.
    run_lock.acquire_run_lock(db, owner="second", timeout_s=0).release()


def test_stale_dead_pid_lock_is_broken(db: Path) -> None:
    lock_path = run_lock.lock_path_for(db)
    lock_path.write_text(
        json.dumps({"pid": _dead_pid(), "owner": "ghost", "acquired_at": "2026-01-01T00:00:00"}),
        encoding="utf-8",
    )
    lock = run_lock.acquire_run_lock(db, owner="successor", timeout_s=0)
    holder = json.loads(lock_path.read_text(encoding="utf-8"))
    assert holder["owner"] == "successor"
    lock.release()


def test_corrupt_lock_payload_is_treated_as_stale(db: Path) -> None:
    lock_path = run_lock.lock_path_for(db)
    lock_path.write_text("not json at all", encoding="utf-8")
    lock = run_lock.acquire_run_lock(db, owner="successor", timeout_s=0)
    assert json.loads(lock_path.read_text(encoding="utf-8"))["owner"] == "successor"
    lock.release()


def test_release_never_clobbers_a_successor(db: Path) -> None:
    lock = run_lock.acquire_run_lock(db, owner="original", timeout_s=0)
    lock_path = run_lock.lock_path_for(db)
    # Simulate a successor that legitimately broke our lock (e.g. this process
    # was presumed dead) and now owns the file with a different pid.
    lock_path.write_text(
        json.dumps({"pid": os.getpid() + 1, "owner": "successor", "acquired_at": "x"}),
        encoding="utf-8",
    )
    lock.release()
    assert lock_path.exists()
    assert json.loads(lock_path.read_text(encoding="utf-8"))["owner"] == "successor"
    lock_path.unlink()


def test_hold_run_lock_context_manager_releases(db: Path) -> None:
    lock_path = run_lock.lock_path_for(db)
    with run_lock.hold_run_lock(db, owner="ctx", timeout_s=0):
        assert lock_path.exists()
    assert not lock_path.exists()
