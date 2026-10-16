"""Unit tests for ``src/pipeline/db_writer_gate.py`` (daily-chain writer gate).

The gate serializes the daily brief chain's SQLite write windows across
processes. These tests pin the lock-file protocol itself; the chain-level
cross-process proof (real driver + real child subprocesses, trace-instrumented)
lives in ``tests/test_daily_fetch_brief_cross_process_writers.py``.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from pipeline import db_writer_gate
from pipeline.db_writer_gate import (
    WRITER_GATE_TIMEOUT_S,
    WriterGateHeldError,
    chain_writer_scope,
    hold_writer_gate,
    writer_gate_path_for,
)
from run_lock import lock_path_for
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite


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


@pytest.mark.parametrize("payload", ["invalid-json", '{"pid": -1, "owner": "dead"}'])
def test_stale_cleanup_failure_is_bounded_and_recovers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, payload: str
) -> None:
    db = tmp_path / "stale.db"
    gate = writer_gate_path_for(db)
    gate.write_text(payload, encoding="utf-8")
    real_unlink = Path.unlink
    blocked = True

    def unlink(path: Path, missing_ok: bool = False) -> None:
        if path == gate and blocked:
            raise PermissionError("controlled stale cleanup denial")
        real_unlink(path, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", unlink)
    started = time.monotonic()
    with (
        pytest.raises(WriterGateHeldError) as caught,
        hold_writer_gate(db, owner="waiting", timeout_s=0.05, poll_s=0.5),
    ):
        pytest.fail("an unremovable lock must not grant ownership")
    assert 0.04 <= time.monotonic() - started < 1.0
    assert caught.value.lock_path == gate
    assert gate.read_text(encoding="utf-8") == payload
    assert not list(tmp_path.glob("*.tmp"))

    blocked = False
    with hold_writer_gate(db, owner="recovered", timeout_s=0.2):
        assert json.loads(gate.read_text(encoding="utf-8"))["owner"] == "recovered"
    assert not gate.exists()


def test_long_held_writer_bounds_sibling_wait_then_recovers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = tmp_path / "held.db"
    entered, release = threading.Event(), threading.Event()
    failures: list[BaseException] = []
    monkeypatch.setattr(db_writer_gate, "WRITER_GATE_TIMEOUT_S", 0.06)

    def holder() -> None:
        try:
            conn = connect_sqlite(db, role=SQLiteConnectionRole.WRITER, schema_preflight=False)
            try:
                conn.execute("CREATE TABLE sample (value INTEGER)")
                conn.commit()
                entered.set()
                if not release.wait(5):
                    raise RuntimeError("test holder release was not signaled")
            finally:
                conn.close()
        except BaseException as error:
            failures.append(error)
            entered.set()

    with chain_writer_scope(db):
        thread = threading.Thread(target=holder)
        thread.start()
        try:
            assert entered.wait(5)
            assert not failures
            started = time.monotonic()
            with pytest.raises(WriterGateHeldError) as caught:
                connect_sqlite(db, role=SQLiteConnectionRole.WRITER, schema_preflight=False)
            assert 0.04 <= time.monotonic() - started < 1.0
            assert caught.value.holder.get("owner")
            assert isinstance(caught.value.holder["pid"], int)
            assert writer_gate_path_for(db).exists()
            independent = connect_sqlite(
                tmp_path / "independent.db",
                role=SQLiteConnectionRole.WRITER,
                schema_preflight=False,
            )
            independent.close()
            reader = connect_sqlite(db, role=SQLiteConnectionRole.READ_ONLY)
            assert reader.execute("SELECT COUNT(*) FROM sample").fetchone()[0] == 0
            reader.close()
        finally:
            release.set()
            thread.join(5)
        assert not thread.is_alive()
        assert not failures
        assert not writer_gate_path_for(db).exists()
        sibling = connect_sqlite(db, role=SQLiteConnectionRole.WRITER, schema_preflight=False)
        try:
            sibling.execute("INSERT INTO sample VALUES (1)")
            sibling.commit()
        finally:
            sibling.close()
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM sample").fetchone()[0] == 1
    assert not writer_gate_path_for(db).exists()


def test_live_process_holder_timeout_recovers_after_connection_close(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db, ready = tmp_path / "process.db", tmp_path / "ready"
    code = (
        "import sys; from pathlib import Path; "
        "from sqlite_runtime import SQLiteConnectionRole, connect_sqlite; "
        "conn=connect_sqlite(sys.argv[1], role=SQLiteConnectionRole.WRITER, schema_preflight=False); "
        "conn.execute('CREATE TABLE sample (value INTEGER)'); conn.commit(); "
        "Path(sys.argv[2]).write_text('ready'); sys.stdin.buffer.read(1); conn.close()"
    )
    env = dict(os.environ)
    env[db_writer_gate.CHAIN_WRITER_DB_ENV] = str(db)
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
    monkeypatch.setattr(db_writer_gate, "WRITER_GATE_TIMEOUT_S", 0.06)
    process = subprocess.Popen(
        [sys.executable, "-c", code, str(db), str(ready)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )
    try:
        deadline = time.monotonic() + 10
        while not ready.exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.01)
        assert ready.exists(), "synthetic process did not open its writer connection"
        with chain_writer_scope(db):
            started = time.monotonic()
            with pytest.raises(WriterGateHeldError) as caught:
                connect_sqlite(db, role=SQLiteConnectionRole.WRITER, schema_preflight=False)
            assert 0.04 <= time.monotonic() - started < 1.0
            assert caught.value.holder["pid"] == process.pid
            assert process.poll() is None
            assert process.stdin is not None
            process.stdin.write(b"x")
            process.stdin.flush()
            assert process.wait(timeout=5) == 0
            sibling = connect_sqlite(db, role=SQLiteConnectionRole.WRITER, schema_preflight=False)
            sibling.close()
        assert not writer_gate_path_for(db).exists()
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        if process.stdin is not None:
            process.stdin.close()
        if process.stdout is not None:
            process.stdout.close()
        if process.stderr is not None:
            process.stderr.close()


@pytest.mark.parametrize("event", ["enter", "exit"])
def test_trace_failure_releases_explicit_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, event: str
) -> None:
    db = tmp_path / "trace-failure.db"
    primary = RuntimeError("controlled gate trace failure")

    def fail_trace(current_event: str, **_kwargs: object) -> None:
        if current_event == event:
            raise primary

    with monkeypatch.context() as patch:
        patch.setattr(db_writer_gate, "_trace_event", fail_trace)
        with (
            pytest.raises(RuntimeError) as caught,
            hold_writer_gate(db, owner="failed-trace", timeout_s=0.1),
        ):
            pass
    assert caught.value is primary
    assert not writer_gate_path_for(db).exists()
    with hold_writer_gate(db, owner="recovered", timeout_s=0.1):
        pass


def test_body_failure_stays_primary_when_trace_cleanup_also_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = tmp_path / "primary-failure.db"
    primary = ValueError("controlled body failure")

    def fail_trace(event: str, **_kwargs: object) -> None:
        if event == "exit":
            raise OSError("controlled trace cleanup failure")

    monkeypatch.setattr(db_writer_gate, "_trace_event", fail_trace)
    with (
        pytest.raises(ValueError) as caught,
        hold_writer_gate(db, owner="body-failure", timeout_s=0.1),
    ):
        raise primary
    assert caught.value is primary
    assert any("OSError" in note for note in getattr(primary, "__notes__", []))
    assert not writer_gate_path_for(db).exists()
