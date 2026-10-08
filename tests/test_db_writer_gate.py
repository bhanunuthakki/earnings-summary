"""Unit tests for ``src/pipeline/db_writer_gate.py`` (daily-chain writer gate).

The gate serializes the daily brief chain's SQLite write windows across
processes. These tests pin the lock-file protocol itself; the chain-level
cross-process proof (real driver + real child subprocesses, trace-instrumented)
lives in ``tests/test_daily_fetch_brief_cross_process_writers.py``.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from pipeline.db_writer_gate import (
    WRITER_GATE_TIMEOUT_S,
    WriterGateHeldError,
    hold_writer_gate,
    writer_gate_path_for,
)
from run_lock import lock_path_for


def test_gate_lock_file_is_distinct_from_the_run_lock(tmp_path: Path) -> None:
    db = tmp_path / "portfolio.db"
    assert writer_gate_path_for(db) == tmp_path / "portfolio.db.writer-gate.lock"
    assert lock_path_for(db) == tmp_path / "portfolio.db.write.lock"
    assert writer_gate_path_for(db) != lock_path_for(db)


def test_second_waiter_blocks_until_the_holder_releases(tmp_path: Path) -> None:
    """A held gate excludes a second (thread-level) contender; release unblocks.

    Same-process threads exercise the same file protocol a second process
    would: the contender reads the live holder's pid, waits, and only claims
    the lock file after the holder's release removes it."""
    db = tmp_path / "a" / "portfolio.db"
    entered = threading.Event()
    outcome: dict[str, object] = {}

    def holder() -> None:
        with hold_writer_gate(db, owner="holder"):
            entered.set()
            time.sleep(0.3)
        outcome["released"] = True

    thread = threading.Thread(target=holder, name="gate-holder")
    thread.start()
    assert entered.wait(10.0)
    assert writer_gate_path_for(db).exists()

    started = time.monotonic()
    with hold_writer_gate(db, owner="waiter", timeout_s=30.0):
        outcome["waited_s"] = time.monotonic() - started
        outcome["acquired"] = True

    thread.join(10.0)
    assert not thread.is_alive()
    assert outcome["acquired"] is True
    assert float(outcome["waited_s"]) > 0.05
    assert outcome["released"] is True
    assert not writer_gate_path_for(db).exists()


def test_bounded_wait_fails_loud_with_holder_identity(tmp_path: Path) -> None:
    db = tmp_path / "portfolio.db"
    entered = threading.Event()
    release = threading.Event()

    def holder() -> None:
        with hold_writer_gate(db, owner="stuck-step:XYZ"):
            entered.set()
            assert release.wait(10.0)

    thread = threading.Thread(target=holder, name="gate-holder-loud")
    thread.start()
    assert entered.wait(10.0)
    try:
        with (
            pytest.raises(WriterGateHeldError) as caught,
            hold_writer_gate(db, owner="waiter", timeout_s=0.1),
        ):
            pass
    finally:
        release.set()
        thread.join(10.0)
    message = str(caught.value)
    assert "stuck-step:XYZ" in message
    assert str(writer_gate_path_for(db)) in message


def test_default_wait_is_generous_beyond_legit_chain_windows() -> None:
    """The default timeout must dwarf the chain's longest window (seconds)."""
    assert WRITER_GATE_TIMEOUT_S >= 120.0


def test_nested_hold_of_the_same_database_does_not_self_deadlock(tmp_path: Path) -> None:
    db = tmp_path / "portfolio.db"
    with (
        hold_writer_gate(db, owner="outer", timeout_s=5.0),
        hold_writer_gate(db, owner="outer", timeout_s=0.1),
    ):
        pass
    # The outer release removed the lock file.
    assert not writer_gate_path_for(db).exists()


def test_independent_databases_hold_independent_gates(tmp_path: Path) -> None:
    first = tmp_path / "one" / "portfolio.db"
    second = tmp_path / "two" / "portfolio.db"
    with (
        hold_writer_gate(first, owner="one"),
        hold_writer_gate(second, owner="two", timeout_s=0.1),
    ):
        pass


def test_stale_dead_holder_lock_is_broken(tmp_path: Path) -> None:
    db = tmp_path / "portfolio.db"
    gate = writer_gate_path_for(db)
    gate.parent.mkdir(parents=True, exist_ok=True)
    gate.write_text(
        json.dumps({"pid": -1, "owner": "crashed-step", "acquired_at": "2026-10-07T00:00:00"}),
        encoding="utf-8",
    )
    with hold_writer_gate(db, owner="successor", timeout_s=5.0):
        holder = json.loads(gate.read_text(encoding="utf-8"))
    assert holder["owner"] == "successor"
    assert not gate.exists()


def test_release_never_removes_a_successors_lock(tmp_path: Path) -> None:
    db = tmp_path / "portfolio.db"
    with hold_writer_gate(db, owner="suspended"):
        gate = writer_gate_path_for(db)
        gate.write_text(
            json.dumps({"pid": 999999, "owner": "successor", "acquired_at": "x"}),
            encoding="utf-8",
        )
    # The suspended holder saw a foreign pid and left the file alone.
    assert json.loads(gate.read_text(encoding="utf-8"))["owner"] == "successor"


def test_trace_records_enter_and_exit_lines(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = tmp_path / "portfolio.db"
    trace = tmp_path / "gate-trace.jsonl"
    monkeypatch.setenv("EARNINGS_SUMMARY_WRITER_GATE_TRACE", str(trace))
    with hold_writer_gate(db, owner="traced:AAA"):
        pass
    lines = [json.loads(row) for row in trace.read_text(encoding="utf-8").splitlines()]
    assert [row["event"] for row in lines] == ["enter", "exit"]
    for row in lines:
        assert row["owner"] == "traced:AAA"
        assert row["db"] == str(db)
        assert row["pid"] > 0
    assert lines[0]["t"] <= lines[1]["t"]


def test_no_trace_file_without_the_env_var(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("EARNINGS_SUMMARY_WRITER_GATE_TRACE", raising=False)
    db = tmp_path / "portfolio.db"
    with hold_writer_gate(db, owner="quiet"):
        pass
    assert not (tmp_path / "gate-trace.jsonl").exists()


def test_in_memory_database_passes_through_ungated() -> None:
    with hold_writer_gate(":memory:", owner="mem", timeout_s=0.0):
        pass
    with pytest.raises(ValueError):
        writer_gate_path_for(":memory:")
