"""Ticker-overlap pool tests for ``execution/daily_fetch_and_brief.py`` (M3, audit F4).

The daily brief worker used to run each dirty ticker's 4-5 step synth-and-publish
chain strictly serially. It now overlaps independent tickers on a small bounded
worker pool (``--workers``). These tests pin the contracts the serial gate tests
in ``tests/test_daily_fetch_brief_gates.py`` cannot observe:

* independent tickers overlap, while the steps INSIDE one ticker stay ordered
  (evaluator -> commitments -> DCF -> artifacts -> history sweep);
* the pool width, and so the peak subprocess count, never exceeds ``--workers``;
* parent-process database writes never overlap: an instrumented counter around
  the driver's WRITER-role connections and the artifact-dirty pass observes a
  maximum of one writer per database file;
* a hung ticker fails alone on its unchanged per-step timeout without blocking
  the other tickers' completion accounting;
* the run summary is identical for ``--workers 1`` and ``--workers 3`` and stays
  in candidate order.

Every test drives the real ``main()`` with a barrier fake standing in for
``subprocess.run`` — no real processes, no LLM, no network.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import TracebackType
from typing import Any

import pytest

from execution import daily_fetch_and_brief

THESIS = "run_thesis_evaluator.py"
MATCH = "match_commitments.py"
DCF = "refresh_dcf.py"
BUILD = "build_artifacts.py"
SWEEP = "sweep_output_history.py"

CHAIN_ORDER = [THESIS, MATCH, DCF, BUILD, SWEEP]
# The friendly stage names the run summary reports, in chain order.
CHAIN_STEP_NAMES = [
    "thesis_evaluator",
    "match_commitments",
    "refresh_dcf",
    "build_artifacts",
    "sweep_output_history",
]
_WAIT_S = 10.0


class _Completed:
    """Minimal stand-in for ``subprocess.CompletedProcess``."""

    def __init__(self, returncode: int) -> None:
        self.returncode = returncode
        self.stdout = ""
        self.stderr = ""


def _script_of(argv: list[str]) -> str:
    for token in reversed(argv):
        if token.endswith(".py"):
            return Path(token).name
    raise AssertionError(f"no stage script token in argv: {argv}")


def _ticker_of(argv: list[str]) -> str:
    return argv[argv.index("--ticker") + 1]


class _StepProbe:
    """Barrier fake for ``subprocess.run`` with a recorded per-ticker timeline.

    * ``hold`` names scripts whose fake call blocks on a per-script gate until
      the test releases it — the instrument that proves (or disproves) overlap.
    * ``timeouts`` names ``(ticker, script)`` pairs whose fake raises
      ``TimeoutExpired`` with the exact ``timeout`` kwarg ``_run_step`` passed,
      pinning the per-step budget.
    """

    def __init__(
        self,
        *,
        hold: set[str] | None = None,
        timeouts: set[tuple[str, str]] | None = None,
        body_s: float = 0.0,
    ) -> None:
        self._lock = threading.Lock()
        self._gates = {script: threading.Event() for script in (hold or ())}
        self._timeouts = timeouts or set()
        self._body_s = body_s
        self.entered: dict[tuple[str, str], threading.Event] = {}
        self.timeouts_seen: dict[tuple[str, str], float] = {}
        self.timeline: list[tuple[str, str, str, float]] = []  # ticker, script, phase, stamp
        self.calls: list[list[str]] = []
        self.in_flight = 0
        self.max_in_flight = 0

    def _enter(self, ticker: str, script: str) -> None:
        with self._lock:
            self.in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self.in_flight)
            self.timeline.append((ticker, script, "enter", time.monotonic()))
        self.entered.setdefault((ticker, script), threading.Event()).set()

    def _exit(self, ticker: str, script: str) -> None:
        with self._lock:
            self.in_flight -= 1
            self.timeline.append((ticker, script, "exit", time.monotonic()))

    def wait_entered(self, ticker: str, script: str, timeout_s: float = _WAIT_S) -> None:
        event = self.entered.setdefault((ticker, script), threading.Event())
        assert event.wait(timeout_s), f"{ticker}:{script} was never dispatched within {timeout_s}s"

    def release(self, script: str) -> None:
        self._gates[script].set()

    def steps_for(self, ticker: str) -> list[str]:
        return [
            script
            for name, script, phase, _ in self.timeline
            if name == ticker and phase == "enter"
        ]

    def enter_stamps(self, ticker: str, script: str) -> list[float]:
        return [
            stamp
            for name, step, phase, stamp in self.timeline
            if name == ticker and step == script and phase == "enter"
        ]

    def __call__(self, argv: list[str], **kwargs: object) -> _Completed:
        script = _script_of(argv)
        ticker = _ticker_of(argv)
        with self._lock:
            self.calls.append(list(argv))
        self._enter(ticker, script)
        try:
            if (ticker, script) in self._timeouts:
                timeout = kwargs.get("timeout")
                assert isinstance(timeout, int | float), "_run_step must pass a timeout"
                self.timeouts_seen[(ticker, script)] = float(timeout)
                raise subprocess.TimeoutExpired(cmd=argv, timeout=timeout)
            time.sleep(self._body_s)
            gate = self._gates.get(script)
            if gate is not None:
                assert gate.wait(30.0), f"{script} was never released by its test"
            return _Completed(0)
        finally:
            self._exit(ticker, script)


class _RunThread:
    """``main()`` running on a worker thread so the test can drive the gates."""

    def __init__(self, argv: list[str]) -> None:
        self._argv = argv
        self.outcome: dict[str, int] = {}
        self._thread = threading.Thread(target=self._run, name="brief-run-test")

    def _run(self) -> None:
        previous = sys.argv
        sys.argv = ["daily_fetch_and_brief.py", *self._argv]
        try:
            self.outcome["rc"] = daily_fetch_and_brief.main()
        finally:
            sys.argv = previous

    def start(self) -> None:
        self._thread.start()

    def join(self, timeout_s: float = 60.0) -> int:
        self._thread.join(timeout_s)
        assert not self._thread.is_alive(), "daily_fetch_and_brief run did not finish"
        return self.outcome["rc"]

    def __enter__(self) -> _RunThread:
        self.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        if self._thread.is_alive():
            self._thread.join(60.0)
            assert not self._thread.is_alive(), "daily_fetch_and_brief run leaked past cleanup"


@pytest.fixture()
def brief_repo(tmp_path: Path, migrated_db: Any) -> Path:
    """A sandboxed repo root whose data/portfolio.db is a real migrated schema."""
    root = tmp_path / "repo"
    (root / "data").mkdir(parents=True, exist_ok=True)
    (root / "micro_thesis" / "holdings").mkdir(parents=True, exist_ok=True)
    (root / "dcf").mkdir(parents=True, exist_ok=True)
    migrated_db(root / "data" / "portfolio.db")
    return root


def _seed(repo_root: Path, ticker: str, *, list_type: str = "portfolio", **extras: object) -> None:
    conn = sqlite3.connect(str(repo_root / "data" / "portfolio.db"))
    columns = ["ticker", "name", "list_type"]
    values: list[object] = [ticker, f"{ticker} Co", list_type]
    for key, value in extras.items():
        columns.append(key)
        values.append(value)
    placeholders = ", ".join("?" * len(columns))
    conn.execute(
        f"INSERT INTO tracked_companies ({', '.join(columns)}) VALUES ({placeholders})",
        values,
    )
    conn.commit()
    conn.close()


def _install(monkeypatch: pytest.MonkeyPatch, probe: _StepProbe) -> None:
    monkeypatch.setattr(daily_fetch_and_brief.subprocess, "run", probe)


def _queue_argv(repo_root: Path, *, workers: int, extra: list[str] | None = None) -> list[str]:
    return ["--repo-root", str(repo_root), "--workers", str(workers), *(extra or [])]


def _run_with_argv(argv: list[str]) -> int:
    """Run ``main()`` with ``argv`` (main reads sys.argv), restoring it after."""
    previous = sys.argv
    sys.argv = ["daily_fetch_and_brief.py", *argv]
    try:
        return daily_fetch_and_brief.main()
    finally:
        sys.argv = previous


# ---------------------------------------------------------------------------
# Overlap + per-ticker ordering
# ---------------------------------------------------------------------------


def test_independent_tickers_overlap_while_each_chain_stays_ordered(
    brief_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """While every ticker's first step is held, all three chains are in flight
    together; inside each ticker the five steps still run in order."""
    for ticker in ("AAA", "BBB", "CCC"):
        _seed(brief_repo, ticker)
    probe = _StepProbe(hold={THESIS}, body_s=0.01)
    _install(monkeypatch, probe)

    with _RunThread(_queue_argv(brief_repo, workers=3, extra=["--force"])) as run:
        for ticker in ("AAA", "BBB", "CCC"):
            probe.wait_entered(ticker, THESIS)
        probe.release(THESIS)
    rc = run.join()

    assert rc == 0
    assert probe.max_in_flight >= 2, "tickers did not overlap"
    for ticker in ("AAA", "BBB", "CCC"):
        assert probe.steps_for(ticker) == CHAIN_ORDER
        stamps = [probe.enter_stamps(ticker, script)[0] for script in CHAIN_ORDER]
        assert stamps == sorted(stamps), f"{ticker} step order was reordered"


# ---------------------------------------------------------------------------
# Bounded width / peak subprocess count
# ---------------------------------------------------------------------------


def test_pool_width_never_exceeds_the_configured_worker_count(
    brief_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Four tickers through a two-worker pool: genuine parallelism, but the
    observed in-flight (subprocess) count never exceeds the configured bound."""
    for ticker in ("AAA", "BBB", "CCC", "DDD"):
        _seed(brief_repo, ticker)
    probe = _StepProbe(body_s=0.02)
    _install(monkeypatch, probe)

    rc = _run_with_argv(_queue_argv(brief_repo, workers=2, extra=["--force"]))

    assert rc == 0
    assert probe.max_in_flight == 2, probe.max_in_flight
    assert {ticker for ticker, _ in probe.entered} == {"AAA", "BBB", "CCC", "DDD"}


def test_serial_worker_count_matches_the_pre_change_chain(
    brief_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--workers 1`` restores the strictly serial sweep (peak in-flight == 1)."""
    for ticker in ("AAA", "BBB"):
        _seed(brief_repo, ticker)
    probe = _StepProbe(body_s=0.01)
    _install(monkeypatch, probe)

    rc = _run_with_argv(_queue_argv(brief_repo, workers=1, extra=["--force"]))

    assert rc == 0
    assert probe.max_in_flight == 1


def test_worker_count_below_one_fails_closed(
    brief_repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _seed(brief_repo, "AAA")
    probe = _StepProbe()
    _install(monkeypatch, probe)

    rc = _run_with_argv(_queue_argv(brief_repo, workers=0, extra=["--force"]))

    assert rc == 2
    assert probe.calls == []
    assert "--workers must be >= 1" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Single-writer discipline — the instrumented write-lock proofs
# ---------------------------------------------------------------------------


def test_writer_connections_per_database_file_never_overlap(
    brief_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Count every parent-process write directly: the driver's WRITER-role
    connections (skip record + completed-build record) and the artifact-dirty
    pass. At most one writer per database file is open at a time."""
    for ticker in ("AAA", "BBB", "CCC"):
        _seed(brief_repo, ticker)
    probe = _StepProbe(body_s=0.02)
    _install(monkeypatch, probe)

    real_connect = daily_fetch_and_brief.connect_sqlite
    real_mark = daily_fetch_and_brief.mark_artifacts_dirty_for_fact_change
    guard = threading.Lock()
    state = {"open": 0, "max": 0}

    def enter_writer() -> None:
        with guard:
            state["open"] += 1
            state["max"] = max(state["max"], state["open"])

    def exit_writer() -> None:
        with guard:
            state["open"] -= 1

    class _TrackedConn:
        def __init__(self, conn: sqlite3.Connection) -> None:
            self._conn = conn

        def __enter__(self) -> sqlite3.Connection:
            return self._conn

        def __exit__(
            self,
            exc_type: type[BaseException] | None,
            exc: BaseException | None,
            tb: TracebackType | None,
        ) -> bool:
            try:
                return bool(self._conn.__exit__(exc_type, exc, tb))
            finally:
                exit_writer()
                self._conn.close()

    def counting_connect(path: Any, *, role: Any, schema_preflight: bool | None = None) -> Any:
        conn = real_connect(path, role=role, schema_preflight=schema_preflight)
        if role is daily_fetch_and_brief.SQLiteConnectionRole.WRITER:
            enter_writer()
            time.sleep(0.02)  # widen the window a missing lock would expose
            return _TrackedConn(conn)
        return conn

    def counting_mark(**kwargs: Any) -> int:
        enter_writer()
        try:
            time.sleep(0.02)
            return real_mark(**kwargs)
        finally:
            exit_writer()

    monkeypatch.setattr(daily_fetch_and_brief, "connect_sqlite", counting_connect)
    monkeypatch.setattr(
        daily_fetch_and_brief, "mark_artifacts_dirty_for_fact_change", counting_mark
    )

    rc = _run_with_argv(_queue_argv(brief_repo, workers=3, extra=["--force"]))

    assert rc == 0
    assert state["max"] == 1, f"concurrent writer connections observed: {state['max']}"


# ---------------------------------------------------------------------------
# Failure isolation and the unchanged per-step budget
# ---------------------------------------------------------------------------


def test_hung_ticker_fails_alone_on_its_unchanged_timeout(
    brief_repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """One ticker's first step times out on the unchanged 600s budget; that
    ticker is reported failed, the others complete, and the run exits 0."""
    for ticker in ("AAA", "BBB"):
        _seed(brief_repo, ticker)
    probe = _StepProbe(timeouts={("AAA", THESIS)}, body_s=0.01)
    _install(monkeypatch, probe)

    rc = _run_with_argv(_queue_argv(brief_repo, workers=2, extra=["--force"]))

    assert rc == 0
    assert probe.timeouts_seen[("AAA", THESIS)] == 600
    summary = json.loads(capsys.readouterr().out)
    by_ticker = {row["ticker"]: row for row in summary["tickers"]}
    assert by_ticker["AAA"]["status"] == "failed"
    assert by_ticker["AAA"]["stages"][0]["error"] == "timeout after 600s"
    assert by_ticker["BBB"]["status"] == "ok"
    assert probe.steps_for("BBB") == CHAIN_ORDER


# ---------------------------------------------------------------------------
# Output parity and summary ordering
# ---------------------------------------------------------------------------


def test_summary_is_identical_for_serial_and_pooled_runs(
    brief_repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The same fixture through ``--workers 1`` and ``--workers 3`` produces the
    same ``tickers`` payload in candidate order; only wall-clock differs."""
    for ticker in ("AAA", "BBB", "CCC"):
        _seed(brief_repo, ticker)

    _install(monkeypatch, _StepProbe(body_s=0.01))
    rc_serial = _run_with_argv(_queue_argv(brief_repo, workers=1, extra=["--force"]))
    serial = json.loads(capsys.readouterr().out)

    _install(monkeypatch, _StepProbe(body_s=0.01))
    rc_pooled = _run_with_argv(_queue_argv(brief_repo, workers=3, extra=["--force"]))
    pooled = json.loads(capsys.readouterr().out)

    assert rc_serial == 0
    assert rc_pooled == 0
    assert serial["tickers"] == pooled["tickers"]
    assert [row["ticker"] for row in pooled["tickers"]] == ["AAA", "BBB", "CCC"]
    for row in pooled["tickers"]:
        assert row["status"] == "ok"
        assert [stage["name"] for stage in row["stages"]] == CHAIN_STEP_NAMES
