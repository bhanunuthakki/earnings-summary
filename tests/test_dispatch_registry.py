"""Tests for src/dispatch_registry.py.

Pass `spawn=False` to skip the real subprocess.Popen and inject lines /
exit code directly. Live-subprocess behavior is covered by the smoke
test on PR 2's end-to-end path.
"""

from __future__ import annotations

import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

from dispatch_registry import Job, Registry, RegistryConflict


def _quick_job(registry: Registry, *, ticker: str = "NU", kind: str = "refresh-stale") -> Job:
    return registry.start(ticker=ticker, kind=kind, argv=["noop"], spawn=False)


def _force_complete(job: Job, *, lines: list[str], exit_code: int = 0) -> None:
    """Simulate the reader thread completing without ever running a subprocess."""
    job.lines.extend(lines)
    job.exit_code = exit_code
    job._done.set()


# ---- registry ------------------------------------------------------------


def test_start_creates_a_job():
    r = Registry()
    job = _quick_job(r)
    assert job.job_id.startswith("job_")
    assert job.ticker == "NU"
    assert job.kind == "refresh-stale"
    assert r.get(job.job_id) is job


def test_start_uppercases_ticker():
    r = Registry()
    job = r.start(ticker="goog", kind="refresh-full", argv=["noop"], spawn=False)
    assert job.ticker == "GOOG"


def test_start_conflict_when_same_slot_still_running():
    r = Registry()
    _quick_job(r)
    with pytest.raises(RegistryConflict, match="job already running"):
        _quick_job(r)
    # Different kind for the same ticker is allowed.
    _quick_job(r, kind="refresh-full")


def test_start_different_tickers_run_concurrently():
    r = Registry()
    j_nu = r.start(ticker="NU", kind="refresh-stale", argv=["noop"], spawn=False)
    j_goog = r.start(ticker="GOOG", kind="refresh-stale", argv=["noop"], spawn=False)
    assert j_nu.job_id != j_goog.job_id


def test_start_allows_reuse_of_slot_after_previous_completes():
    r = Registry()
    job1 = _quick_job(r)
    _force_complete(job1, lines=["done"], exit_code=0)
    job2 = _quick_job(r)
    assert job2.job_id != job1.job_id


def test_start_blocks_when_global_cap_reached():
    r = Registry(max_concurrent=2)
    r.start(ticker="A", kind="x", argv=["noop"], spawn=False)
    r.start(ticker="B", kind="x", argv=["noop"], spawn=False)
    with pytest.raises(RegistryConflict, match="concurrent job cap reached"):
        r.start(ticker="C", kind="x", argv=["noop"], spawn=False)


def test_gc_drops_completed_jobs_past_cutoff():
    r = Registry()
    job = _quick_job(r)
    _force_complete(job, lines=[], exit_code=0)
    # Backdate the start so it qualifies for collection.
    job.started_at = datetime.now(UTC) - timedelta(hours=2)
    removed = r.gc_completed(older_than_sec=60.0)
    assert removed == 1
    assert r.get(job.job_id) is None


def test_gc_preserves_running_jobs():
    r = Registry()
    job = _quick_job(r)
    job.started_at = datetime.now(UTC) - timedelta(hours=2)  # old but running
    removed = r.gc_completed(older_than_sec=60.0)
    assert removed == 0
    assert r.get(job.job_id) is job


# ---- job stream ----------------------------------------------------------


def test_stream_yields_start_log_done_in_order():
    r = Registry()
    job = _quick_job(r)

    captured: list[str] = []

    def collector():
        for frame in job.stream_events():
            captured.append(frame)

    t = threading.Thread(target=collector, daemon=True)
    t.start()
    time.sleep(0.05)
    # Push two log lines + complete the job
    with job._lock:
        job.lines.append("first")
        job.lines.append("second")
    time.sleep(0.2)
    _force_complete(job, lines=["third"], exit_code=0)
    t.join(timeout=2.0)

    assert '"event": "start"' in captured[0]
    assert any('"line": "first"' in f for f in captured)
    assert any('"line": "second"' in f for f in captured)
    assert any('"line": "third"' in f for f in captured)
    assert captured[-1].startswith("data: ") and '"event": "done"' in captured[-1]
    assert '"exit_code": 0' in captured[-1]


def test_snapshot_reports_running_state_and_line_count():
    r = Registry()
    job = _quick_job(r)
    with job._lock:
        job.lines.extend(["a", "b", "c"])
    snap = job.snapshot()
    assert snap["is_running"] is True
    assert snap["line_count"] == 3
    assert snap["exit_code"] is None
    _force_complete(job, lines=[], exit_code=1)
    snap = job.snapshot()
    assert snap["is_running"] is False
    assert snap["exit_code"] == 1


def test_list_jobs_returns_snapshots():
    r = Registry()
    _quick_job(r, ticker="NU")
    _quick_job(r, ticker="GOOG")
    snaps = r.list_jobs()
    assert {s["ticker"] for s in snaps} == {"NU", "GOOG"}


def test_repo_registry_wraps_interactive_writer_with_shared_lock(tmp_path: Path):
    r = Registry(repo_root=tmp_path)
    with patch("dispatch_registry.subprocess.Popen") as popen:
        process = popen.return_value
        process.stdout = iter(())
        process.wait.return_value = 0
        process.returncode = 0
        job = r.start(ticker="NU", kind="refresh-full", argv=["python", "writer.py"])
        assert job._reader is not None
        job._reader.join(timeout=2)

    command = popen.call_args.args[0]
    assert command[1] == str(tmp_path / "src/runtime/job_runtime.py")
    assert command[2:6] == ["--job", "interactive-refresh-full", "--write-set", "portfolio-db"]
    assert command[-3:] == ["--", "python", "writer.py"]


def test_explicit_read_only_job_is_not_wrapped(tmp_path: Path):
    r = Registry(repo_root=tmp_path)
    job = r.start(
        ticker="_REPO",
        kind="tracker-server",
        argv=["server", "--port", "8000"],
        write_sets=[],
        spawn=False,
    )
    assert job.write_sets == ()
