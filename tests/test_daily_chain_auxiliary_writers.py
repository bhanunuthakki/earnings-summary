"""Auxiliary daily-chain writers: gate scope, observable drops, real evidence.

The daily-brief chain serializes its own explicit write windows per resolved
database path (``pipeline.db_writer_gate``). WRITER-role connections opened by
AUXILIARY helpers inside the same chain — source telemetry, the
``--enable-llm`` ledger/artifact/budget/quota-breaker writes — are not windows
the driver controls, so they must be serialized at the connection seam itself
(``sqlite_runtime.connect_sqlite``) for chain participants only.

These tests pin that in three layers:

* the chain marker + seam semantics: non-chain processes keep their previous
  behavior, chain processes hold the gate for a WRITER connection's whole
  lifetime, nesting never self-deadlocks, and a stuck gate fails loud
  (``WriterGateHeldError``) instead of blocking forever;
* observable loss: every auxiliary best-effort writer that loses its row to a
  gate timeout leaves a WARNING plus a durable drop counter beside the
  database — never a silent drop (the round-2 scrutiny finding);
* the real chain: a real width-3 ``--enable-llm`` run over a disposable
  migrated database with a local stub ``claude`` CLI exercises source_calls,
  llm_calls, llm_artifacts and llm_budget_alerts, and the opt-in writer trace
  proves the maximum simultaneous WRITER-role open per database path is 1
  ACROSS PIDS, with every chain open holding the gate.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import threading
from collections.abc import Callable, Generator
from contextlib import contextmanager
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import cast

import pytest

from execution import daily_fetch_and_brief
from pipeline import db_writer_gate
from pipeline.db_writer_gate import (
    CHAIN_WRITER_DB_ENV,
    WriterGateHeldError,
    hold_writer_gate,
)
from sources import registry
from sources.telemetry import SourceAttemptMeasurement
from telemetry_health import (
    DROPPED_LLM_ARTIFACT_WRITE,
    DROPPED_LLM_BUDGET_ALERT_WRITE,
    DROPPED_LLM_LEDGER_WRITE,
    DROPPED_QUOTA_BREAKER_WRITE,
    DROPPED_SOURCE_ATTEMPT_WRITE,
    DROPPED_SOURCE_CALL_WRITE,
    dropped_writes_since,
)
from tests.fixtures.dcf_fmp_payloads import write_fmp_multi

PROJECT_ROOT = Path(__file__).resolve().parent.parent
TICKERS = ("TESTA", "TESTB", "TESTC")
CHAIN_STAGE_NAMES = [
    "thesis_evaluator",
    "match_commitments",
    "refresh_dcf",
    "build_artifacts",
    "sweep_output_history",
]
# A dead local proxy endpoint: connection refused, fast, no egress (the live
# price lookup then resolves from the seeded FMP cache).
_DEAD_PROXY = "http://127.0.0.1:9"
# Short bounded wait so a deliberately stuck gate fails fast in tests.
_GATE_TIMEOUT_S = 0.2


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _seed_chain_database(
    db_path: Path,
    tickers: tuple[str, ...] = TICKERS,
    *,
    seed_prior_alignment_spend: bool = True,
    alignment_cap_usd: float = 0.06,
) -> None:
    """Seed the rows the chain needs to reach its auxiliary writers.

    * ``tracked_companies`` — the pool the driver selects from;
    * ``exec_comp_packages`` — enough §13 executive-comp input for the
      ``--enable-llm`` alignment narrative (the ``llm_artifacts`` writer);
    * a tight ``llm_budgets`` row for that purpose plus a prior same-month
      ``llm_calls`` row — so the chain's own call crosses the warn threshold
      and records a real ``llm_budget_alerts`` row deterministically.
    """
    now = datetime.now(UTC).isoformat()
    conn = sqlite3.connect(str(db_path))
    try:
        conn.executemany(
            "INSERT INTO tracked_companies (ticker, name, list_type) VALUES (?, ?, ?)",
            [(ticker, f"{ticker} Co", "portfolio") for ticker in tickers],
        )
        conn.executemany(
            "INSERT INTO exec_comp_packages (ticker, fiscal_year, executive_name, role, is_ceo,"
            " currency, total_comp_granted, total_comp_realized, performance_metrics_json,"
            " extracted_at) VALUES (?, 2025, 'Jane Doe', 'Chief Executive Officer', 1, 'USD',"
            " ?, ?, ?, ?)",
            [
                (ticker, 1_000_000 + index, 2_000_000 + index, '[{"name": "Gross Margin"}]', now)
                for index, ticker in enumerate(tickers)
            ],
        )
        updated = conn.execute(
            "UPDATE llm_budgets SET monthly_cap_usd = ?, warn_threshold_pct = 0.8,"
            " hard_block = 0, on_exceed = 'warn' WHERE purpose = 'exec_comp_alignment'",
            (alignment_cap_usd,),
        ).rowcount
        assert updated == 1, "migrated schema must seed the exec_comp_alignment budget row"
        if seed_prior_alignment_spend:
            conn.execute(
                "INSERT INTO llm_calls (called_at, model, prompt_sha256, prompt_chars,"
                " elapsed_ms, purpose, cost_estimate_usd) VALUES (?, 'claude-sonnet-4-6',"
                " 'seed', 1, 1, 'exec_comp_alignment', ?)",
                (now, alignment_cap_usd * 0.9),
            )
        conn.commit()
    finally:
        conn.close()


def _make_chain_repo(
    tmp_path: Path, migrated_db: Callable[..., Path], tickers: tuple[str, ...] = TICKERS
) -> Path:
    repo = tmp_path / "repo"
    (repo / "data").mkdir(parents=True)
    (repo / "micro_thesis" / "holdings").mkdir(parents=True)
    (repo / "dcf").mkdir(parents=True)
    (repo / "output").mkdir(parents=True)
    template = migrated_db(tmp_path / "schema" / "template.sqlite")
    write_fmp_multi(template, repo, tickers)
    _seed_chain_database(repo / "data" / "portfolio.db", tickers)
    return repo


def _run_with_argv(argv: list[str]) -> int:
    """Run ``daily_fetch_and_brief.main()`` with argv (main reads sys.argv)."""
    previous = sys.argv
    sys.argv = ["daily_fetch_and_brief.py", *argv]
    try:
        return daily_fetch_and_brief.main()
    finally:
        sys.argv = previous


def _write_stub_claude(bin_dir: Path, *, mode: str) -> tuple[Path, Path]:
    """Write a local ``claude`` stub; returns (executable, call-log path).

    ``mode="success"`` answers the ``claude -p --output-format json`` envelope
    the transport parses (with structured answers where a purpose expects
    JSON). ``mode="usage_limit"`` fails the way an exhausted subscription
    window does, which is what engages the quota breaker.
    """
    calls = bin_dir.parent / "stub-calls.txt"
    if mode == "success":
        body = f"""#!{sys.executable}
import json, os, sys

prompt = sys.stdin.read()
with open(os.environ["STUB_CLAUDE_CALLS"], "a", encoding="utf-8") as handle:
    handle.write("call\\n")
lowered = prompt.lower()
if "suggest" in lowered and "comparable" in lowered:
    text = json.dumps([{{"ticker": "MSFT", "name": "Microsoft", "why": "cloud comparables"}}])
else:
    text = "Management's compensation design rewards gross-margin expansion."
print(json.dumps({{
    "type": "result",
    "subtype": "success",
    "is_error": False,
    "result": text,
    "total_cost_usd": 0.05,
    "usage": {{
        "input_tokens": 10,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
        "output_tokens": 5,
    }},
}}))
"""
    else:
        body = f"""#!{sys.executable}
import os, sys

sys.stdin.read()
with open(os.environ["STUB_CLAUDE_CALLS"], "a", encoding="utf-8") as handle:
    handle.write("call\\n")
print("Claude AI usage limit reached|2000000000", file=sys.stderr)
sys.exit(1)
"""
    stub = bin_dir / "claude"
    stub.write_text(body, encoding="utf-8")
    stub.chmod(0o755)
    return stub, calls


def _hermetic_child_env(
    monkeypatch: pytest.MonkeyPatch, trace: Path, stub_dir: Path, calls: Path
) -> None:
    """What the real children inherit from the driver.

    The stub ``claude`` comes first on PATH (asserted below — the machine has a
    real one, and no test may ever spawn it), the writer trace is on, the LLM
    routes deterministically to the stubbed subscription backend, and a dead
    proxy keeps the live-price lookup hermetic.
    """
    monkeypatch.delenv("EARNINGS_SUMMARY_DB_PATH", raising=False)
    monkeypatch.setenv("EARNINGS_SUMMARY_WRITER_GATE_TRACE", str(trace))
    monkeypatch.setenv("PATH", f"{stub_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("STUB_CLAUDE_CALLS", str(calls))
    monkeypatch.setenv("LLM_PRIMARY_SUBSCRIPTION_BACKEND", "claude")
    monkeypatch.setenv("LLM_SUBSCRIPTION_FALLBACK_DISABLED", "1")
    monkeypatch.setenv("LLM_FALLBACK_DISABLED", "1")
    for var in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.setenv(var, _DEAD_PROXY)
    resolved = shutil_which_claude(Path(os.environ["PATH"]))
    assert resolved is not None and resolved.parent == stub_dir, (
        f"stub claude is not first on PATH (resolved {resolved})"
    )


def shutil_which_claude(path: Path) -> Path | None:
    import shutil

    found = shutil.which("claude", path=str(path))
    return Path(found) if found else None


def _stub_call_count(calls: Path) -> int:
    if not calls.exists():
        return 0
    return len([line for line in calls.read_text(encoding="utf-8").splitlines() if line.strip()])


def _drops(db_path: Path, kind: str, since: datetime) -> int:
    summary = dropped_writes_since(kind, db_path=db_path, since=since)
    return 0 if summary is None else summary.count


@contextmanager
def _held_gate(db_path: Path, *, owner: str = "test:stuck-window") -> Generator[None]:
    """Hold the chain writer gate on a sibling thread for the block."""
    entered = threading.Event()
    release = threading.Event()

    def hold() -> None:
        with hold_writer_gate(db_path, owner=owner):
            entered.set()
            release.wait(60.0)

    thread = threading.Thread(target=hold, name="test-gate-holder")
    thread.start()
    assert entered.wait(10.0), "holder thread never took the gate"
    try:
        yield
    finally:
        release.set()
        thread.join(30.0)
        assert not thread.is_alive(), "holder thread failed to release the gate"


@contextmanager
def _chain_process(monkeypatch: pytest.MonkeyPatch, db_path: Path) -> Generator[None]:
    """Mark THIS process as a daily-chain participant on ``db_path``."""
    monkeypatch.setenv(CHAIN_WRITER_DB_ENV, str(db_path))
    monkeypatch.setattr(db_writer_gate, "WRITER_GATE_TIMEOUT_S", _GATE_TIMEOUT_S)
    yield


# ---------------------------------------------------------------------------
# Chain marker + connection-seam semantics
# ---------------------------------------------------------------------------


def test_non_chain_process_writer_open_is_never_gated(
    migrated_db: Callable[..., Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Outside the daily chain, a WRITER open keeps its previous behavior: it
    opens and writes immediately even while another process/thread holds the
    gate, and the chain-only trace stays silent about it."""
    db_path = migrated_db(tmp_path / "portfolio.sqlite")
    trace = tmp_path / "trace.jsonl"
    monkeypatch.setenv("EARNINGS_SUMMARY_WRITER_GATE_TRACE", str(trace))
    monkeypatch.delenv(CHAIN_WRITER_DB_ENV, raising=False)
    monkeypatch.setattr(db_writer_gate, "WRITER_GATE_TIMEOUT_S", _GATE_TIMEOUT_S)

    with _held_gate(db_path):
        conn = connect_writer(db_path)
        try:
            conn.execute("SELECT 1").fetchone()
        finally:
            conn.close()

    assert trace.exists(), "the trace env var was not honored at all"
    events = [json.loads(line) for line in trace.read_text(encoding="utf-8").splitlines()]
    assert [event["event"] for event in events] == ["enter", "exit"], events


def test_chain_writer_open_holds_the_gate_for_the_connection_lifetime(
    migrated_db: Callable[..., Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Inside a chain participant the WRITER open takes the gate and holds it
    until close: a sibling thread's open times out loud, and the lock file
    names the connection's owner while it is open."""
    db_path = migrated_db(tmp_path / "portfolio.sqlite")
    trace = tmp_path / "trace.jsonl"
    monkeypatch.setenv("EARNINGS_SUMMARY_WRITER_GATE_TRACE", str(trace))

    with _chain_process(monkeypatch, db_path):
        conn = connect_writer(db_path)
        try:
            lock_path = db_writer_gate.writer_gate_path_for(db_path)
            assert lock_path.exists(), "chain writer open did not take the gate"
            holder = json.loads(lock_path.read_text(encoding="utf-8"))
            assert holder["owner"] == f"connect:{__name__}", holder
            # A sibling thread (another pool worker) queues behind the open
            # connection and fails loud instead of opening a second writer.
            assert isinstance(_open_writer_on_other_thread(db_path), WriterGateHeldError)
        finally:
            conn.close()
        assert not db_writer_gate.writer_gate_path_for(db_path).exists()

    events = [json.loads(line) for line in trace.read_text(encoding="utf-8").splitlines()]
    assert [event["event"] for event in events] == ["enter", "writer_open", "writer_close", "exit"]
    assert all(event["chain"] for event in events if event["event"].startswith("writer_"))
    assert events[1]["file_claim"] is True, events[1]


def test_nested_chain_windows_and_connections_never_self_deadlock(
    migrated_db: Callable[..., Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A WRITER connection opened inside an explicit window (the driver's
    pattern) rides the window's claim instead of timing out on itself, and the
    nested connection closes before the window does."""
    db_path = migrated_db(tmp_path / "portfolio.sqlite")
    trace = tmp_path / "trace.jsonl"
    monkeypatch.setenv("EARNINGS_SUMMARY_WRITER_GATE_TRACE", str(trace))

    with (
        _chain_process(monkeypatch, db_path),
        hold_writer_gate(db_path, owner="test:explicit-window"),
    ):
        conn = connect_writer(db_path)
        conn.execute("SELECT 1").fetchone()
        conn.close()

    events = [json.loads(line) for line in trace.read_text(encoding="utf-8").splitlines()]
    assert [event["event"] for event in events] == [
        "enter",
        "writer_open",
        "writer_close",
        "exit",
    ], events
    assert events[1]["file_claim"] is False, events[1]


def test_chain_writer_gate_is_released_by_garbage_collection(
    migrated_db: Callable[..., Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A chain connection nobody closes must not wedge the gate forever: the
    finalizer backstop releases it when the object is collected."""
    import gc

    db_path = migrated_db(tmp_path / "portfolio.sqlite")

    with _chain_process(monkeypatch, db_path):
        conn = connect_writer(db_path)
        lock_path = db_writer_gate.writer_gate_path_for(db_path)
        assert lock_path.exists()
        del conn
        gc.collect()
        assert not lock_path.exists(), "collected connection left the gate held"


def connect_writer(db_path: Path) -> sqlite3.Connection:
    from sqlite_runtime import SQLiteConnectionRole, connect_sqlite

    return connect_sqlite(db_path, role=SQLiteConnectionRole.WRITER, schema_preflight=True)


def _open_writer_on_other_thread(db_path: Path) -> BaseException | None:
    """Try one WRITER open on a sibling thread; return what it raised."""
    raised: list[BaseException] = []

    def attempt() -> None:
        try:
            connect_writer(db_path).close()
        except BaseException as exc:
            raised.append(exc)

    thread = threading.Thread(target=attempt, name="test-sibling-writer")
    thread.start()
    thread.join(30.0)
    assert not thread.is_alive(), "sibling writer open never returned"
    return raised[0] if raised else None


# ---------------------------------------------------------------------------
# Observable drops: auxiliary best-effort writers inside the chain
# ---------------------------------------------------------------------------


def test_source_telemetry_gate_timeouts_are_observable(
    migrated_db: Callable[..., Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``log_call``, ``log_calls_batch`` and ``log_http_measurement`` keep the
    row best-effort, but a gate timeout now leaves a warning AND a durable
    counter instead of vanishing."""
    db_path = migrated_db(tmp_path / "portfolio.sqlite")
    started = datetime.now(UTC)
    monkeypatch.setattr(registry, "_DB_PATH", db_path)

    with _chain_process(monkeypatch, db_path), _held_gate(db_path), caplog.at_level("WARNING"):
        registry.log_call(
            source_name="fmp", kind="income", ticker="TESTA", status=registry.CallStatus.OK
        )
        registry.log_calls_batch(
            [
                registry.PendingSourceCall(
                    source_name="fmp",
                    kind="balance",
                    ticker="TESTA",
                    status=registry.CallStatus.OK,
                )
            ]
        )
        persisted = registry.log_http_measurement(
            SourceAttemptMeasurement(
                run_id="test-run",
                provider="fmp",
                endpoint="/income",
                latency_ms=3,
                retry_count=0,
                status="ok",
            )
        )

    assert persisted is False, "the attempt row was written despite the gate being held"
    assert _drops(db_path, DROPPED_SOURCE_CALL_WRITE, started) == 2, caplog.text
    assert _drops(db_path, DROPPED_SOURCE_ATTEMPT_WRITE, started) == 1, caplog.text
    assert "source_call_dropped" in caplog.text, caplog.text
    assert "source_attempt_dropped" in caplog.text, caplog.text

    conn = sqlite3.connect(str(db_path))
    try:
        assert conn.execute("SELECT COUNT(*) FROM source_calls").fetchone()[0] == 0
    finally:
        conn.close()


def test_llm_ledger_and_artifact_gate_timeouts_are_observable(
    migrated_db: Callable[..., Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The ``--enable-llm`` ledger row and the artifact upsert are best-effort
    too: a gate timeout leaves a counter beside the database for each."""
    db_path = migrated_db(tmp_path / "portfolio.sqlite")
    started = datetime.now(UTC)

    with _chain_process(monkeypatch, db_path), _held_gate(db_path):
        from llm_artifact_store import UpsertRequest, upsert
        from llm_call_ledger import LlmCallRecord, record_call

        with caplog.at_level("WARNING"):
            inserted = record_call(
                LlmCallRecord(
                    called_at=datetime.now(UTC),
                    model="claude-sonnet-4-6",
                    prompt_sha256="deadbeef",
                    prompt_chars=10,
                    elapsed_ms=5,
                    purpose="exec_comp_alignment",
                    ticker="TESTA",
                ),
                db_path=db_path,
            )
            artifact_id, cache_hit = upsert(
                UpsertRequest(
                    ticker="TESTA",
                    purpose="exec_comp_alignment",
                    content_md="narrative",
                    cache_inputs=[b"inputs"],
                ),
                db_path=db_path,
            )

    assert inserted is None, "the ledger row was written despite the gate being held"
    assert (artifact_id, cache_hit) == (None, False)
    assert _drops(db_path, DROPPED_LLM_LEDGER_WRITE, started) == 1, caplog.text
    assert _drops(db_path, DROPPED_LLM_ARTIFACT_WRITE, started) == 1, caplog.text
    assert "llm_call_ledger_write_failed" in caplog.text, caplog.text
    assert "artifact_store_open_gate_timeout" in caplog.text, caplog.text


def test_budget_alert_gate_timeout_is_observable(
    migrated_db: Callable[..., Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A threshold-crossing budget alert that cannot take the gate is counted
    rather than silently lost."""
    db_path = migrated_db(tmp_path / "portfolio.sqlite")
    started = datetime.now(UTC)

    with _chain_process(monkeypatch, db_path), _held_gate(db_path):
        from llm_budget import record_alert

        with caplog.at_level("WARNING"):
            written = record_alert("exec_comp_alignment", 0.80, Decimal("0.05"), db_path=db_path)

    assert written is False
    assert _drops(db_path, DROPPED_LLM_BUDGET_ALERT_WRITE, started) == 1, caplog.text
    assert "record_alert_gate_timeout" in caplog.text, caplog.text


def test_quota_breaker_gate_timeout_is_observable_through_call_llm(
    migrated_db: Callable[..., Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The production path: a stubbed ``claude`` reports an exhausted usage
    window while the chain gate is stuck. The call still fails fast
    (``LLMQuotaExhausted``), the breaker row's loss is counted, and the final
    ledger row's loss is counted too — nothing is swallowed."""
    import llm_client
    from llm.cli import LLMQuotaExhausted, call_llm

    # Named ``.db``: the breaker's table path (vs its loose-JSON compatibility
    # path for non-database files) is what production uses.
    db_path = migrated_db(tmp_path / "portfolio.db")
    started = datetime.now(UTC)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub, calls = _write_stub_claude(bin_dir, mode="usage_limit")
    monkeypatch.setattr(llm_client, "_setup_verified", True)
    monkeypatch.setattr(llm_client, "_claude_cli_path", str(stub))
    monkeypatch.setenv("STUB_CLAUDE_CALLS", str(calls))
    monkeypatch.setenv("LLM_FALLBACK_DISABLED", "1")
    monkeypatch.setenv("EARNINGS_SUMMARY_DB_PATH", str(db_path))

    with (
        _chain_process(monkeypatch, db_path),
        _held_gate(db_path),
        caplog.at_level("WARNING"),
        pytest.raises(LLMQuotaExhausted),
    ):
        call_llm("Summarize the quarter.", purpose="exec_comp_alignment", db_path=db_path)

    assert _stub_call_count(calls) == 1, "the stub provider was not actually spawned"
    assert _drops(db_path, DROPPED_QUOTA_BREAKER_WRITE, started) == 1, caplog.text
    assert _drops(db_path, DROPPED_LLM_LEDGER_WRITE, started) == 1, caplog.text
    assert "llm_quota_breaker_write_gate_timeout" in caplog.text, caplog.text


# ---------------------------------------------------------------------------
# The real chain: width-3, --enable-llm, stubbed provider, traced writers
# ---------------------------------------------------------------------------


def _trace_events(trace: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in trace.read_text(encoding="utf-8").splitlines()]


def _max_simultaneous_writer_opens(events: list[dict[str, object]], db_key: str) -> int:
    """Maximum simultaneous chain WRITER-role opens on ``db_key``.

    Events are appended in file order, and a process appends its own
    ``writer_open`` before the matching ``writer_close``, so the running count
    of unmatched opens on a database IS the simultaneous writer count — across
    every pid at once.
    """
    depth = 0
    peak = 0
    for event in events:
        if str(event.get("db")) != db_key or not event.get("chain"):
            continue
        if event["event"] == "writer_open":
            depth += 1
            peak = max(peak, depth)
        elif event["event"] == "writer_close":
            depth -= 1
            assert depth >= 0, events
    assert depth == 0, "a chain writer open never closed"
    return peak


def _chain_writer_intervals(
    events: list[dict[str, object]], db_key: str
) -> list[dict[str, object]]:
    """Every chain WRITER open paired with its close, in file order."""
    open_by_conn: dict[str, dict[str, object]] = {}
    intervals: list[dict[str, object]] = []
    for index, event in enumerate(events):
        if str(event.get("db")) != db_key or not event.get("chain"):
            continue
        if event["event"] == "writer_open":
            open_by_conn[str(event["conn"])] = event | {"index": index}
        elif event["event"] == "writer_close":
            opened = open_by_conn.pop(str(event["conn"]), None)
            if opened is not None:
                intervals.append(opened | {"close_index": index})
    assert not open_by_conn, f"unclosed chain writer opens: {sorted(open_by_conn)}"
    return intervals


def _gate_window_intervals(
    events: list[dict[str, object]], db_key: str
) -> list[tuple[int, int, int]]:
    """(enter, exit, pid) line-index pairs for every explicit gate window."""
    open_stacks: dict[int, list[int]] = {}
    windows: list[tuple[int, int, int]] = []
    for index, event in enumerate(events):
        if str(event.get("db")) != db_key:
            continue
        pid = int(cast("int", event["pid"]))
        if event["event"] == "enter":
            open_stacks.setdefault(pid, []).append(index)
        elif event["event"] == "exit" and open_stacks.get(pid):
            windows.append((open_stacks[pid].pop(), index, pid))
    return windows


def _ungated_chain_writer_opens(
    events: list[dict[str, object]], db_key: str
) -> list[dict[str, object]]:
    """Chain writer opens that held NO chain gate, which would be the bug.

    A chain open is gated when it took the lock file itself (``file_claim``)
    or when it ran inside an interval the same pid already held — an explicit
    window (``enter``/``exit``) or an enclosing chain writer claim. Everything
    else means a WRITER connection opened without the gate.
    """
    windows = _gate_window_intervals(events, db_key)
    chain_intervals = _chain_writer_intervals(events, db_key)
    enclosing = [
        (
            int(cast("int", interval["pid"])),
            int(cast("int", interval["index"])),
            int(cast("int", interval["close_index"])),
        )
        for interval in chain_intervals
        if not interval.get("file_claim")
    ]
    violations: list[dict[str, object]] = []
    for interval in chain_intervals:
        if interval.get("file_claim"):
            continue
        index = int(cast("int", interval["index"]))
        pid = int(cast("int", interval["pid"]))
        inside_window = any(
            start < index < end and window_pid == pid for start, end, window_pid in windows
        )
        inside_claim = any(
            start < index < end and claim_pid == pid
            for claim_pid, start, end in enclosing
            if start != index
        )
        if not (inside_window or inside_claim):
            violations.append(interval)
    return violations


def test_width_three_enable_llm_chain_serializes_every_writer_open(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A REAL width-3 ``--enable-llm`` chain over a disposable migrated
    database with a stub provider. Its auxiliary writers (source telemetry,
    llm_calls, llm_artifacts, llm_budget_alerts) really land, and the opt-in
    trace proves at the connection seam that the maximum simultaneous
    WRITER-role open per database path was 1 across every participating pid,
    with every chain open holding the gate."""
    trace = tmp_path / "gate-trace.jsonl"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _stub, calls = _write_stub_claude(bin_dir, mode="success")
    repo = _make_chain_repo(tmp_path, migrated_db)
    _hermetic_child_env(monkeypatch, trace, bin_dir, calls)

    rc = _run_with_argv(["--repo-root", str(repo), "--workers", "3", "--force", "--enable-llm"])

    summary = json.loads(capsys.readouterr().out)
    assert rc == 0
    rows = cast("list[dict[str, object]]", summary["tickers"])
    by_ticker = {str(row["ticker"]): row for row in rows}
    assert set(by_ticker) == set(TICKERS)
    for ticker in TICKERS:
        stages = cast("list[dict[str, object]]", by_ticker[ticker]["stages"])
        assert [str(stage["name"]) for stage in stages] == CHAIN_STAGE_NAMES, ticker
        failed = [stage for stage in stages if stage.get("exit_code") != 0]
        assert not failed, (ticker, failed)

    # The stub provider was really used, so the --enable-llm paths ran for real.
    assert _stub_call_count(calls) >= len(TICKERS), calls

    db_path = repo / "data" / "portfolio.db"
    db_key = str(db_path.resolve())
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        source_calls = conn.execute("SELECT COUNT(*) FROM source_calls").fetchone()[0]
        purposes = {
            str(row["purpose"]) for row in conn.execute("SELECT DISTINCT purpose FROM llm_calls")
        }
        artifacts = {
            str(row["purpose"])
            for row in conn.execute("SELECT DISTINCT purpose FROM llm_artifacts")
        }
        alerts = conn.execute(
            "SELECT COUNT(*) FROM llm_budget_alerts WHERE purpose = 'exec_comp_alignment'"
        ).fetchone()[0]
        breakers = conn.execute("SELECT COUNT(*) FROM llm_circuit_breakers").fetchone()[0]
    finally:
        conn.close()

    assert source_calls > 0, "no source_calls telemetry landed"
    assert "exec_comp_alignment" in purposes, purposes
    assert "peer_selection" in purposes, purposes
    assert "exec_comp_alignment" in artifacts, artifacts
    assert alerts >= 1, "the budget warn threshold never recorded an alert"
    assert breakers == 0, "a successful chain must not trip the quota breaker"

    events = _trace_events(trace)
    chain_opens = [event for event in events if event["event"] == "writer_open" and event["chain"]]
    assert chain_opens, "the trace recorded no chain writer opens"
    # Every auxiliary writer module is named by its own open events.
    owners = {str(event["owner"]) for event in chain_opens}
    assert {
        "connect:sources.registry",
        "connect:llm_call_ledger",
        "connect:llm_artifact_store",
        "connect:llm_budget",
    } <= owners, sorted(owners)
    # ... from more than one process: the driver AND its step children.
    pids = {int(cast("int", event["pid"])) for event in chain_opens}
    assert len(pids) > 1, sorted(pids)

    assert _max_simultaneous_writer_opens(events, db_key) == 1, (
        "simultaneous chain WRITER-role opens exceeded one per database path"
    )
    assert _ungated_chain_writer_opens(events, db_key) == []


def test_daily_chain_records_the_quota_breaker_when_the_provider_is_exhausted(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A REAL chain whose stubbed provider reports an exhausted usage window:
    the breaker row lands in the disposable database (so every later process
    fails fast instead of spawning doomed CLIs), the failure is classified in
    the ledger, and the writer serialization still holds."""
    trace = tmp_path / "gate-trace.jsonl"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _stub, calls = _write_stub_claude(bin_dir, mode="usage_limit")
    repo = _make_chain_repo(tmp_path, migrated_db, ("TESTA",))
    _hermetic_child_env(monkeypatch, trace, bin_dir, calls)

    rc = _run_with_argv(["--repo-root", str(repo), "--workers", "1", "--force", "--enable-llm"])

    summary = json.loads(capsys.readouterr().out)
    assert rc == 0
    rows = cast("list[dict[str, object]]", summary["tickers"])
    stages = cast("list[dict[str, object]]", rows[0]["stages"])
    assert [str(stage["name"]) for stage in stages] == CHAIN_STAGE_NAMES
    assert _stub_call_count(calls) >= 1, "the stub provider was never spawned"

    db_path = repo / "data" / "portfolio.db"
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        breakers = [
            dict(row)
            for row in conn.execute(
                "SELECT provider, blocked_until, reason FROM llm_circuit_breakers"
            )
        ]
        quota_rows = [
            dict(row)
            for row in conn.execute(
                "SELECT purpose, failure_class, error FROM llm_calls"
                " WHERE failure_class IS NOT NULL"
            )
        ]
    finally:
        conn.close()

    assert breakers and breakers[0]["provider"] == "anthropic", breakers
    assert quota_rows, "the quota failure was never classified into the ledger"
    assert any(row["failure_class"] == "usage_limit" for row in quota_rows), quota_rows

    events = _trace_events(trace)
    assert _max_simultaneous_writer_opens(events, str(db_path.resolve())) == 1
