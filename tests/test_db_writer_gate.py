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


@pytest.mark.parametrize("successor_pid", [os.getpid(), 999999])
def test_release_never_removes_a_successors_lock(tmp_path: Path, successor_pid: int) -> None:
    db = tmp_path / "portfolio.db"
    with hold_writer_gate(db, owner="suspended"):
        gate = writer_gate_path_for(db)
        gate.write_text(
            json.dumps(
                {"pid": successor_pid, "owner": "successor", "acquired_at": "x", "token": "new"}
            ),
            encoding="utf-8",
        )
    # Even a same-process successor has its own token and must remain owned.
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


_STALE_CONTENDER = r"""
import json
import os
import sys
import time
from pathlib import Path
from pipeline import db_writer_gate as gate
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite

root, tag = Path(sys.argv[1]), sys.argv[2]
db = root / "two-contenders.db"
lock = gate.writer_gate_path_for(db)
real_unlink, real_open, real_read = Path.unlink, os.open, gate._read_holder
paused = False

def wait_for(name):
    deadline = time.monotonic() + 15
    while not (root / name).exists():
        if time.monotonic() >= deadline:
            raise RuntimeError("synthetic coordination deadline")
        time.sleep(0.005)

def unlink(path, missing_ok=False):
    global paused
    if path == lock and not paused:
        paused = True
        (root / (tag + "-at-delete")).touch()
        wait_for(tag + "-allow-delete")
    return real_unlink(path, missing_ok=missing_ok)

def opened(path, *args, **kwargs):
    if tag == "B" and os.fspath(path).endswith(".guard"):
        (root / "B-at-guard").touch()
    return real_open(path, *args, **kwargs)

def read_holder(path):
    holder = real_read(path)
    if tag == "B" and holder and isinstance(holder.get("pid"), int):
        if holder["pid"] > 0 and holder["pid"] != os.getpid():
            (root / "B-saw-live-holder").touch()
    return holder

Path.unlink, os.open, gate._read_holder = unlink, opened, read_holder
os.environ[gate.CHAIN_WRITER_DB_ENV] = str(db)
os.environ["EARNINGS_SUMMARY_WRITER_GATE_TRACE"] = str(root / "writer-events.jsonl")
with gate.chain_writer_scope(db):
    conn = connect_sqlite(db, role=SQLiteConnectionRole.WRITER, schema_preflight=False)
    try:
        (root / (tag + "-writer-open")).touch()
        wait_for(tag + "-allow-close")
        conn.execute("INSERT INTO sample VALUES (?)", (tag,))
        conn.commit()
    finally:
        conn.close()
"""


def _wait_for_signal(paths: tuple[Path, ...], processes: list[subprocess.Popen[bytes]]) -> Path:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        for path in paths:
            if path.exists():
                return path
        assert all(process.poll() is None for process in processes), "contender exited early"
        time.sleep(0.005)
    pytest.fail(f"missing synthetic signal: {[path.name for path in paths]}")


def test_stale_takeover_serializes_two_real_contenders(tmp_path: Path) -> None:
    """Pause contender B at the exact stale-delete race, then let A claim first."""
    db = tmp_path / "two-contenders.db"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE sample (value TEXT)")
    gate = writer_gate_path_for(db)
    gate.write_text(json.dumps({"pid": -1, "token": "dead-owner"}), encoding="utf-8")
    env = dict(os.environ)
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
    processes: list[subprocess.Popen[bytes]] = []

    def start(tag: str) -> None:
        processes.append(
            subprocess.Popen(
                [sys.executable, "-c", _STALE_CONTENDER, str(tmp_path), tag],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
            )
        )

    try:
        start("A")
        _wait_for_signal((tmp_path / "A-at-delete",), processes)
        start("B")
        # Old code reaches unlink with stale evidence. Correct code queues on
        # A's OS guard and must read ownership again after that guard is free.
        _wait_for_signal((tmp_path / "B-at-delete", tmp_path / "B-at-guard"), processes)
        (tmp_path / "A-allow-delete").touch()
        _wait_for_signal((tmp_path / "A-writer-open",), processes)
        (tmp_path / "B-allow-delete").touch()
        observed = _wait_for_signal(
            (tmp_path / "B-writer-open", tmp_path / "B-saw-live-holder"), processes
        )
        assert observed.name == "B-saw-live-holder", "B removed A's new live claim"
        assert not (tmp_path / "B-writer-open").exists()
        (tmp_path / "A-allow-close").touch()
        _wait_for_signal((tmp_path / "B-writer-open",), processes[1:])
        (tmp_path / "B-allow-close").touch()
        for process in processes:
            assert process.wait(timeout=5) == 0
        rows = [
            json.loads(line) for line in (tmp_path / "writer-events.jsonl").read_text().splitlines()
        ]
        opened: set[int] = set()
        for row in sorted(rows, key=lambda item: item["t"]):
            if row["event"] == "writer_open":
                assert not opened, "writer-role lifetimes overlapped across processes"
                opened.add(row["pid"])
            elif row["event"] == "writer_close":
                opened.remove(row["pid"])
        assert not opened
        with sqlite3.connect(db) as conn:
            assert conn.execute("SELECT value FROM sample ORDER BY value").fetchall() == [
                ("A",),
                ("B",),
            ]
        assert not gate.exists()
    finally:
        for tag in ("A", "B"):
            (tmp_path / (tag + "-allow-delete")).touch()
            (tmp_path / (tag + "-allow-close")).touch()
        for process in processes:
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
            if process.stdout is not None:
                process.stdout.close()
            if process.stderr is not None:
                process.stderr.close()


def test_transition_guard_denial_fails_closed_then_recovers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = tmp_path / "denied-guard.db"
    gate = writer_gate_path_for(db)
    real_open = os.open

    def denied_open(path: str | os.PathLike[str], flags: int, mode: int = 0o777) -> int:
        if os.fspath(path).endswith(".guard"):
            raise PermissionError("controlled guard access denial")
        return real_open(path, flags, mode)

    with monkeypatch.context() as patch:
        patch.setattr(os, "open", denied_open)
        with (
            pytest.raises(PermissionError, match="controlled guard access denial"),
            hold_writer_gate(db, owner="denied", timeout_s=0.05),
        ):
            pytest.fail("guard denial must not grant ownership")
    assert not gate.exists()
    assert not list(tmp_path.glob("*.tmp"))
    with hold_writer_gate(db, owner="recovered", timeout_s=0.1):
        pass
    assert not gate.exists()


def test_busy_transition_guard_uses_writer_deadline_and_releases_after_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db, ready = tmp_path / "busy-guard.db", tmp_path / "guard-ready"
    code = """import sys, time
from pathlib import Path
from pipeline import db_writer_gate as gate
lock = gate.writer_gate_path_for(sys.argv[1])
with gate._ownership_transition_guard(lock, deadline=time.monotonic()+5):
    Path(sys.argv[2]).touch()
    sys.stdin.buffer.read(1)
"""
    env = dict(os.environ)
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
    process = subprocess.Popen(
        [sys.executable, "-c", code, str(db), str(ready)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )
    monkeypatch.setattr(db_writer_gate, "WRITER_GATE_TIMEOUT_S", 0.05)
    try:
        _wait_for_signal((ready,), [process])
        started = time.monotonic()
        with (
            pytest.raises(WriterGateHeldError),
            hold_writer_gate(db, owner="waiting", timeout_s=0.05),
        ):
            pytest.fail("busy transition guard must not grant ownership")
        assert 0.04 <= time.monotonic() - started < 1.0
        assert process.poll() is None
        assert not writer_gate_path_for(db).exists()
        process.kill()
        process.wait(timeout=5)
        with hold_writer_gate(db, owner="after-crash", timeout_s=0.1):
            pass
        assert not writer_gate_path_for(db).exists()
        assert len(list(tmp_path.glob("*.guard"))) == 1
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream is not None:
                stream.close()


@pytest.mark.parametrize("error", [PermissionError("denied"), OSError("unknown owner read")])
def test_unreadable_gate_owner_is_never_evicted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: OSError
) -> None:
    db = tmp_path / "unreadable-owner.db"
    gate = writer_gate_path_for(db)
    original = json.dumps({"pid": os.getpid(), "owner": "live", "token": "live-token"})
    gate.write_text(original, encoding="utf-8")
    real_read = Path.read_text

    def read_text(path: Path, encoding: str | None = None, errors: str | None = None) -> str:
        if path == gate:
            raise error
        return real_read(path, encoding=encoding, errors=errors)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "read_text", read_text)
        with (
            pytest.raises(OSError),
            hold_writer_gate(db, owner="unknown-contender", timeout_s=0.05),
        ):
            pytest.fail("unreadable ownership must not authorize takeover")
    assert gate.read_text(encoding="utf-8") == original
    with (
        pytest.raises(WriterGateHeldError),
        hold_writer_gate(db, owner="known-live-contender", timeout_s=0),
    ):
        pytest.fail("the known live holder must remain owned")
    assert gate.read_text(encoding="utf-8") == original
    gate.write_text(json.dumps({"pid": -1, "token": "now-proved-dead"}), encoding="utf-8")
    with hold_writer_gate(db, owner="after-proved-death", timeout_s=0.1):
        pass
    assert not gate.exists()
