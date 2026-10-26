"""Lane-scheduler tests for the morning pipeline (M3 stage lanes, audit F3).

``execution/run_morning_pipeline.py`` dispatches the manifest's stages as
bounded-parallel lanes instead of a strictly serial chain. These tests pin the
lane contracts that the serial-suite tests cannot observe:

* independent read/compute stages overlap a DB-writing stage, while a stage
  with unsettled declared dependencies never starts;
* the single-writer SQLite discipline holds under the parallelism — the
  maximum number of simultaneously in-flight ``db_writer`` stages is exactly 1
  (the scheduler's writer slot is the instrumented write lock);
* a hung lane stage still hits its own manifest timeout budget — a parallel
  dispatch never extends it;
* concurrent lane completions merge into one resume checkpoint without losing
  a key, and a resume attempt re-runs only the failed stage;
* a lane failure never cancels any other lane (always-attempt survives).

Every test drives the real scheduler through ``main()`` with a barrier fake
that stands in for ``subprocess.run``: no real processes, no DB, no LLM. The
fake records a monotonic enter/exit timeline per script, so overlap and
ordering are asserted on recorded facts, never on sleep-and-hope.
"""

from __future__ import annotations

import json
import subprocess
import threading
import time
from pathlib import Path

import pytest

from execution import run_morning_pipeline
from pipeline.morning_manifest import (
    STAGE_CANDIDATE_FIT,
    STAGE_FACTOR_PROXIES,
    STAGE_FEED,
    STAGE_FUNDAMENTALS,
    STAGE_MANIFEST,
    STAGE_POSITION_GUARD,
    STAGE_PREFLIGHT,
    STAGE_TRIGGERS,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]

# Script basenames the barrier fake reasons about (the stage identity inside a
# managed argv is the final .py token, matching the serial suite's helper).
NEWS_SCRIPT = "fetch_news.py"
FUNDAMENTALS_SCRIPT = "refresh_cockpit_fundamentals.py"
FACTOR_PROXIES_SCRIPT = "fetch_factor_proxies.py"
TRIGGERS_SCRIPT = "run_triggers.py"
STANDUP_SCRIPT = "run_standup.py"
PRE_ER_BRIEF_SCRIPT = "generate_pre_earnings_briefs.py"
POST_ER_READOUT_SCRIPT = "generate_post_earnings_readouts.py"
FEED_SCRIPT = "build_alert_feed.py"
VALIDATE_SCRIPT = "run_validation_engine.py"

_ALL_SCRIPTS = [spec.script for spec in STAGE_MANIFEST]

# Bounded wait for barrier steps: generous on CI, still a fast failure.
_WAIT_S = 10.0


class _Completed:
    """Minimal stand-in for ``subprocess.CompletedProcess``."""

    def __init__(self, returncode: int) -> None:
        self.returncode = returncode
        self.stdout = ""
        self.stderr = ""


class _LaneProbe:
    """Barrier fake for ``subprocess.run`` with a recorded event timeline.

    * ``hold`` names scripts whose fake call blocks on a per-script gate until
      the test releases it — the instrument that proves (or disproves) overlap.
    * ``timeouts`` names scripts whose fake raises ``TimeoutExpired`` with the
      exact ``timeout`` kwarg the scheduler passed (pinning the per-stage
      budget), after recording the attempted dispatch.
    * Writer scripts (the manifest's ``db_writer`` set) bracket a simulated DB
      write transaction, so ``max_concurrent_writers`` is the observed maximum
      simultaneous writer count per database file — the single-writer proof.
    """

    def __init__(
        self,
        *,
        hold: set[str] | None = None,
        fail: dict[str, int] | None = None,
        timeouts: set[str] | None = None,
        writer_scripts: set[str] | None = None,
        body_s: float = 0.0,
        writer_body_s: float = 0.0,
    ) -> None:
        self._lock = threading.Lock()
        self._gates: dict[str, threading.Event] = {
            script: threading.Event() for script in hold or ()
        }
        self._fail = fail or {}
        self._timeouts = timeouts or set()
        self._writer_scripts = writer_scripts or set()
        self._body_s = body_s
        self._writer_body_s = writer_body_s
        self.entered: dict[str, threading.Event] = {}
        self.exited: dict[str, threading.Event] = {}
        self.timeouts_seen: dict[str, float] = {}
        self.timeline: list[tuple[str, str, float]] = []
        self.calls: list[list[str]] = []
        self.in_flight = 0
        self.max_in_flight = 0
        self.writers_in_flight = 0
        self.max_concurrent_writers = 0

    def _enter(self, script: str) -> threading.Event:
        event = self.entered.setdefault(script, threading.Event())
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        self.timeline.append((script, "enter", time.monotonic()))
        event.set()
        return event

    def _exit(self, script: str) -> None:
        with self._lock:
            self.in_flight -= 1
            self.timeline.append((script, "exit", time.monotonic()))
            exited = self.exited.setdefault(script, threading.Event())
            exited.set()

    def wait_entered(self, script: str, timeout_s: float = _WAIT_S) -> None:
        assert self.entered.setdefault(script, threading.Event()).wait(timeout_s), (
            f"{script} was never dispatched within {timeout_s}s"
        )

    def wait_exited(self, script: str, timeout_s: float = _WAIT_S) -> None:
        assert self.exited.setdefault(script, threading.Event()).wait(timeout_s), (
            f"{script} never completed within {timeout_s}s"
        )

    def entered_yet(self, script: str) -> bool:
        return self.entered.setdefault(script, threading.Event()).is_set()

    def exit_time(self, script: str) -> float:
        for name, phase, stamp in self.timeline:
            if name == script and phase == "exit":
                return stamp
        raise AssertionError(f"{script} never exited")

    def enter_time(self, script: str) -> float:
        for name, phase, stamp in self.timeline:
            if name == script and phase == "enter":
                return stamp
        raise AssertionError(f"{script} never entered")

    def release(self, script: str) -> None:
        self._gates[script].set()

    def release_all(self) -> None:
        for gate in self._gates.values():
            gate.set()

    def __call__(self, argv: list[str], **kwargs: object) -> _Completed:
        script = _script_of(argv)
        with self._lock:
            self.calls.append(list(argv))
        assert script is not None, f"no stage script token in argv: {argv}"
        self._enter(script)
        try:
            if script in self._timeouts:
                timeout = kwargs.get("timeout")
                assert isinstance(timeout, int | float), "scheduler must pass a timeout"
                self.timeouts_seen[script] = timeout
                raise subprocess.TimeoutExpired(cmd=argv, timeout=timeout)
            if script in self._writer_scripts:
                with self._lock:
                    self.writers_in_flight += 1
                    self.max_concurrent_writers = max(
                        self.max_concurrent_writers, self.writers_in_flight
                    )
                try:
                    time.sleep(self._writer_body_s)
                finally:
                    with self._lock:
                        self.writers_in_flight -= 1
            else:
                time.sleep(self._body_s)
            gate = self._gates.get(script)
            if gate is not None:
                # Held stage: the test releases the gate to let the lane
                # proceed. The bounded wait keeps a broken scheduler from
                # hanging the suite.
                assert gate.wait(30.0), f"{script} was never released by its test"
            return _Completed(self._fail.get(script, 0))
        finally:
            self._exit(script)

    @property
    def scripts(self) -> list[str]:
        return [name for name, phase, _ in self.timeline if phase == "enter"]


def _script_of(argv: list[str]) -> str | None:
    """The basename of the final ``.py`` token (the stage's script)."""
    for token in reversed(argv):
        if token.endswith(".py"):
            return Path(token).name
    return None


def _writer_scripts() -> set[str]:
    return {spec.script for spec in STAGE_MANIFEST if spec.db_writer}


def _install(monkeypatch: pytest.MonkeyPatch, probe: _LaneProbe) -> None:
    monkeypatch.setattr("execution.run_morning_pipeline.subprocess.run", probe)


@pytest.fixture(autouse=True)
def _isolated_resume_state(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Keep the production resume checkpoint isolated between lane tests."""
    monkeypatch.setattr(
        run_morning_pipeline,
        "STATE_FILE",
        tmp_path / "morning_pipeline" / "state.json",
    )


@pytest.fixture(autouse=True)
def _configured_product_db(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Name a disposable product DB; the checkout-default fallback is forbidden.

    Stages are faked, so nothing opens this path.
    """
    monkeypatch.setenv("EARNINGS_SUMMARY_DB_PATH", str(tmp_path / "configured" / "portfolio.db"))


class _PipelineThread:
    """``main()`` running on a worker thread, with a fail-loud join."""

    def __init__(self, argv: list[str]) -> None:
        self.outcome: dict[str, int] = {}
        self._thread = threading.Thread(
            target=self._run,
            args=(argv,),
            name="lane-test-pipeline",
        )

    def _run(self, argv: list[str]) -> None:
        self.outcome["rc"] = run_morning_pipeline.main(argv)

    def start(self) -> None:
        self._thread.start()

    def join(self, timeout_s: float = 60.0) -> int:
        self._thread.join(timeout_s)
        assert not self._thread.is_alive(), "pipeline thread did not finish"
        return self.outcome["rc"]

    def __enter__(self) -> _PipelineThread:
        self.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        if self._thread.is_alive():
            self._thread.join(60.0)
            assert not self._thread.is_alive(), "pipeline thread leaked past cleanup"


# ---------------------------------------------------------------------------
# Manifest classification — the single-writer and independence declarations
# ---------------------------------------------------------------------------


def test_manifest_declares_the_verified_single_writer_set() -> None:
    """The lane contract is manifest-declared. The non-writer set is exactly
    the six stages verified to touch the DB through read-only connections
    only (or not at all), persisting to file caches instead; everything else
    can hold a write transaction on the pipeline DB file."""
    non_writers = {spec.key for spec in STAGE_MANIFEST if not spec.db_writer}

    assert non_writers == {
        STAGE_PREFLIGHT,
        STAGE_FUNDAMENTALS,
        STAGE_CANDIDATE_FIT,
        STAGE_FACTOR_PROXIES,
        STAGE_POSITION_GUARD,
        STAGE_FEED,
    }
    # The feed's full-run lane dependency on the trigger sweep is the one
    # scheduling supplement: selection-wise it is standalone, but a normal
    # run must not render the day's alerts before the sweep settles.
    for spec in STAGE_MANIFEST:
        if spec.key == STAGE_FEED:
            assert spec.lane_dependencies == (STAGE_TRIGGERS,)
        else:
            assert spec.lane_dependencies == ()


# ---------------------------------------------------------------------------
# Overlap — independent stages run concurrently with the writer queue
# ---------------------------------------------------------------------------


def test_independent_read_compute_stages_overlap_a_held_writer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """While stage 0 (news, a DB writer) is held, the manifest-independent
    read/compute stages it shares the first wave with must be dispatched and
    run to completion — the serial chain's wall-clock is no longer the sum of
    stage durations. Triggers, whose dependency is the held stage, must not
    start."""
    probe = _LaneProbe(
        hold={NEWS_SCRIPT},
        writer_scripts=_writer_scripts(),
        writer_body_s=0.05,
    )
    _install(monkeypatch, probe)

    pipeline = _PipelineThread([])
    with pipeline:
        probe.wait_entered(NEWS_SCRIPT)
        probe.wait_entered(FUNDAMENTALS_SCRIPT)
        probe.wait_entered(FACTOR_PROXIES_SCRIPT)
        # The sweep depends on the held news stage: give the scheduler a real
        # window to misbehave in, then assert it never dispatched triggers.
        time.sleep(0.2)
        assert not probe.entered_yet(TRIGGERS_SCRIPT)
        probe.release(NEWS_SCRIPT)
    rc = pipeline.join()

    assert rc == 0
    assert sorted(probe.scripts) == sorted(_ALL_SCRIPTS)
    # Overlap is a recorded fact, not a timing hope: both read/compute stages
    # entered before the held writer exited.
    assert probe.enter_time(FUNDAMENTALS_SCRIPT) < probe.exit_time(NEWS_SCRIPT)
    assert probe.enter_time(FACTOR_PROXIES_SCRIPT) < probe.exit_time(NEWS_SCRIPT)
    # The dependency rule held: the sweep started only after news settled.
    assert probe.enter_time(TRIGGERS_SCRIPT) > probe.exit_time(NEWS_SCRIPT)


def test_lanes_are_bounded_and_actually_parallel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An ungated full run proves the two structural bounds at once: the lane
    width never exceeds the scheduler's cap, and at least two stages were
    genuinely in flight together (the parallelism is real, not nominal)."""
    probe = _LaneProbe(
        writer_scripts=_writer_scripts(),
        body_s=0.01,
        writer_body_s=0.05,
    )
    _install(monkeypatch, probe)

    rc = run_morning_pipeline.main([])

    assert rc == 0
    assert sorted(probe.scripts) == sorted(_ALL_SCRIPTS)
    # The scheduler's cap is _MAX_CONCURRENT_STAGES == 3 (one writer lane plus
    # the read/compute lanes; the manifest's non-writer set never offers more
    # than two simultaneous overlaps today).
    assert probe.max_in_flight <= 3
    assert probe.max_in_flight >= 2


# ---------------------------------------------------------------------------
# Single-writer discipline — the instrumented write-lock proof
# ---------------------------------------------------------------------------


def test_writer_slot_never_admits_two_writers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Single-writer SQLite discipline under lane parallelism: each DB-writing
    stage's fake brackets a simulated write transaction, and the observed
    maximum simultaneous writer count per database file must be exactly 1 —
    no two stage processes may hold write transactions at once."""
    probe = _LaneProbe(
        writer_scripts=_writer_scripts(),
        body_s=0.01,
        writer_body_s=0.05,
    )
    _install(monkeypatch, probe)

    rc = run_morning_pipeline.main([])

    assert rc == 0
    assert probe.max_concurrent_writers == 1
    # The discipline did not silently serialize the whole pipeline: the
    # read-only fundamentals pass overlapped the news writer.
    assert probe.enter_time(FUNDAMENTALS_SCRIPT) < probe.exit_time(NEWS_SCRIPT)


def test_read_only_feed_overlaps_the_writer_tail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After the sweep settles, the read-only feed render overlaps the writer
    tail: while standup (a writer) is held on its gate, the feed completes,
    and the remaining writer stages wait for the single-writer slot instead of
    stacking. The feed still dispatches after the sweep — the lane
    supplement."""
    probe = _LaneProbe(
        hold={STANDUP_SCRIPT},
        writer_scripts=_writer_scripts(),
        body_s=0.01,
        writer_body_s=0.02,
    )
    _install(monkeypatch, probe)

    pipeline = _PipelineThread([])
    with pipeline:
        probe.wait_entered(STANDUP_SCRIPT)
        probe.wait_entered(FEED_SCRIPT)
        probe.wait_exited(FEED_SCRIPT)
        time.sleep(0.1)
        # The other writer stages queue on the writer slot behind the held
        # standup; none of them may start while it is in flight.
        assert not probe.entered_yet(PRE_ER_BRIEF_SCRIPT)
        assert not probe.entered_yet(POST_ER_READOUT_SCRIPT)
        assert not probe.entered_yet(VALIDATE_SCRIPT)
        probe.release(STANDUP_SCRIPT)
    rc = pipeline.join()

    assert rc == 0
    assert sorted(probe.scripts) == sorted(_ALL_SCRIPTS)
    # Overlap fact: the feed rendered while the standup writer was in flight.
    assert probe.enter_time(FEED_SCRIPT) < probe.exit_time(STANDUP_SCRIPT)
    assert probe.exit_time(FEED_SCRIPT) < probe.exit_time(STANDUP_SCRIPT)
    # Ordering fact: the feed follows the sweep even though selection-wise it
    # only depends on the preflight.
    order = run_morning_pipeline.last_dispatch_order()
    assert order.index(STAGE_TRIGGERS) < order.index(STAGE_FEED)


# ---------------------------------------------------------------------------
# Resilience — always-attempt and timeouts survive the lanes
# ---------------------------------------------------------------------------


def test_independent_lane_failures_never_cancel_other_lanes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two independent stages fail (one writer, one read/compute): every other
    stage still runs, exit code == 2, and the feed still renders after the
    sweep — the always-attempt contract holds across lanes."""
    probe = _LaneProbe(
        fail={NEWS_SCRIPT: 1, FACTOR_PROXIES_SCRIPT: 1},
        writer_scripts=_writer_scripts(),
    )
    _install(monkeypatch, probe)

    rc = run_morning_pipeline.main([])

    assert rc == 2
    assert sorted(probe.scripts) == sorted(_ALL_SCRIPTS)
    order = run_morning_pipeline.last_dispatch_order()
    assert order.index(STAGE_TRIGGERS) < order.index(STAGE_FEED)


def test_hung_lane_stage_hits_its_own_manifest_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A lane stage that never returns still hits its configured per-stage
    timeout: the scheduler passes the manifest budget to the subprocess
    contract unchanged, the stage is reported as timed out, and no other stage
    is blocked from running."""
    probe = _LaneProbe(
        timeouts={FUNDAMENTALS_SCRIPT},
        writer_scripts=_writer_scripts(),
    )
    _install(monkeypatch, probe)

    rc = run_morning_pipeline.main([])

    assert rc == 1
    # The budget is the manifest's own (stage 0d = 60s), not something the
    # lane scheduler invented or extended.
    assert probe.timeouts_seen[FUNDAMENTALS_SCRIPT] == 60
    assert sorted(probe.scripts) == sorted(_ALL_SCRIPTS)


# ---------------------------------------------------------------------------
# Resume checkpoint — concurrent completions merge without losing a key
# ---------------------------------------------------------------------------


def test_concurrent_lane_completions_merge_into_one_checkpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """While the news writer is held, two independent read/compute stages
    settle in the same dispatcher batch — the exact window where a naive
    concurrent checkpoint writer would lose a key. The merged checkpoint must
    carry every successful stage, and the retry attempt must resume by running
    exactly the failed stage."""
    probe = _LaneProbe(
        hold={NEWS_SCRIPT},
        fail={VALIDATE_SCRIPT: 2},
        writer_scripts=_writer_scripts(),
        body_s=0.01,
        writer_body_s=0.02,
    )
    _install(monkeypatch, probe)

    pipeline = _PipelineThread([])
    with pipeline:
        probe.wait_entered(NEWS_SCRIPT)
        probe.wait_exited(FUNDAMENTALS_SCRIPT)
        probe.wait_exited(FACTOR_PROXIES_SCRIPT)
        probe.release(NEWS_SCRIPT)
    rc = pipeline.join()

    assert rc == 1
    state = json.loads(run_morning_pipeline.STATE_FILE.read_text(encoding="utf-8"))
    completed = set(state["completed_stages"])
    expected_completed = {spec.key for spec in STAGE_MANIFEST} - {"stage_3_validate"}
    assert completed == expected_completed

    # The retry attempt resumes from the exact checkpoint: only the failed
    # validation gate runs.
    retry = _LaneProbe(writer_scripts=_writer_scripts())
    _install(monkeypatch, retry)

    assert run_morning_pipeline.main([]) == 0
    assert sorted(retry.scripts) == [VALIDATE_SCRIPT]
    assert not run_morning_pipeline.STATE_FILE.exists()
