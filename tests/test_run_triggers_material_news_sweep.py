"""Bounded-concurrency material-news sweep + cost-cap accounting in the driver.

Exercises the two stage_1 fixes for the material_news blind spot (audit
``.tmp/audit/performance-pipeline.md`` F1/F1b) entirely through the STUBBED
LLM boundary — no real LLM calls anywhere:

  * ``material_news`` per-ticker classification scans run in the driver's
    bounded-concurrency scan sweep (waves of at most ``--scan-workers``
    simultaneous tickers), so a ~100-ticker book no longer pays ~100 serial
    Opus calls inside the 1800s stage budget.
  * The sweep's spend is cap-accounted: a wave is admitted only while the
    ``llm_calls`` ledger total since run start is below ``--max-cost-usd``,
    so the cap that previously lived only in the per-candidate loop (which
    material_news never reaches — it emits zero candidates by design) now
    bounds the pipeline's biggest LLM consumer too.
  * Nothing is silently truncated: every ticker the cap or the per-ticker
    timeout leaves unscanned gets an explicit ``scan_deferred`` event plus a
    run-summary entry (reason ``cost_cap`` / ``per_ticker_timeout``), and a
    hung ticker defers only itself while the rest of the wave book completes.

The stub writes the ``llm_calls`` ledger row the real transport would write
(``llm.ledger.record_llm_call`` -> ``llm_call_ledger.record_call`` writes
exactly this row after each call), because that row is what the driver's cap
accounting reads; an entry that only existed post hoc would not prove the cap.

The last section makes VAL-PIPE-003's owner-approved "at most one final wave
crosses the cap" allowance directly observable at this stubbed boundary: a
TEST-LOCAL tracer wraps the public ``run_triggers.query_run_cost_usd`` seam so
every admission decision is captured with the committed shared-ledger spend it
read (split by lane), each stubbed call carries the wave index that was in
flight when it entered, and the trace proves pre-dispatch admission
(``S < C``), settlement before the next admission read, the bounded
``T <= C + F`` overshoot, and per-ticker deferrals. Production carries no wave
id, log line, or schema change for this — the wave indexes are test-local, as
VAL-PIPE-003 permits.
"""

from __future__ import annotations

import inspect
import json
import sqlite3
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from execution import run_triggers

# ---------------------------------------------------------------------------
# Schema fixture — the news triple (news / news_events / llm_artifacts), the
# llm_calls ledger the cap accounting reads, plus the transcript trio and
# alert stores the earnings_tone cross-accounting case drives.
# ---------------------------------------------------------------------------


def _create_schema(conn: sqlite3.Connection) -> None:
    """Create the driver-facing table subset (mirrors the production columns
    the code under test reads and writes; defaults/triggers dropped where not
    load-bearing)."""
    _ = conn.execute(
        "CREATE TABLE news ("
        + "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        + "ticker TEXT NOT NULL, "
        + "headline TEXT NOT NULL, "
        + "url TEXT NOT NULL, "
        + "published_at TEXT NOT NULL, "
        + "snippet TEXT)"
    )
    _ = conn.execute(
        "CREATE TABLE news_events ("
        + "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        + "ticker TEXT NOT NULL, "
        + "news_id INTEGER NOT NULL UNIQUE, "
        + "headline TEXT NOT NULL, "
        + "url TEXT NOT NULL, "
        + "published_at TEXT NOT NULL, "
        + "event_key TEXT NOT NULL DEFAULT '', "
        + "event_type TEXT NOT NULL, "
        + "relevance REAL NOT NULL, "
        + "why_material TEXT NOT NULL DEFAULT '', "
        + "classified_at TEXT NOT NULL)"
    )
    _ = conn.execute(
        "CREATE TABLE llm_artifacts ("
        + "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        + "ticker TEXT, "
        + "scope TEXT NOT NULL DEFAULT 'ticker', "
        + "purpose TEXT NOT NULL, "
        + "fiscal_period TEXT, "
        + "content_md TEXT, "
        + "content_json TEXT, "
        + "input_sha256 TEXT NOT NULL, "
        + "output_sha256 TEXT, "
        + "model TEXT, "
        + "prompt_version TEXT NOT NULL DEFAULT 'v1', "
        + "generated_at TEXT NOT NULL, "
        + "expires_at TEXT, "
        + "superseded_by_id INTEGER, "
        + "dirty INTEGER NOT NULL DEFAULT 0, "
        + "dirty_reason TEXT, "
        + "source_doc_ids TEXT, "
        + "parent_artifact_ids TEXT, "
        + "llm_call_id INTEGER)"
    )
    _ = conn.execute(
        "CREATE TABLE llm_calls ("
        + "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        + "called_at TEXT NOT NULL, "
        + "purpose TEXT, "
        + "ticker TEXT, "
        + "model TEXT, "
        + "prompt_sha256 TEXT, "
        + "prompt_chars INTEGER, "
        + "elapsed_ms INTEGER, "
        + "cost_estimate_usd REAL, "
        + "error TEXT)"
    )
    _ = conn.execute(
        "CREATE TABLE documents ("
        + "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        + "fetched_at TEXT NOT NULL)"
    )
    _ = conn.execute(
        "CREATE TABLE transcripts ("
        + "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        + "document_id INTEGER NOT NULL, "
        + "ticker TEXT NOT NULL, "
        + "fiscal_period_type TEXT, "
        + "period_end TEXT, "
        + "has_qa_section INTEGER)"
    )
    _ = conn.execute(
        "CREATE TABLE transcript_segments ("
        + "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        + "transcript_id INTEGER NOT NULL, "
        + "seq INTEGER NOT NULL, "
        + "speaker TEXT, "
        + "text TEXT NOT NULL)"
    )
    _ = conn.execute(
        "CREATE TABLE alerts ("
        + "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        + "user_id TEXT NOT NULL DEFAULT 'bhanu', "
        + "ticker TEXT NOT NULL, "
        + "trigger_kind TEXT NOT NULL, "
        + "fired_at TEXT NOT NULL, "
        + "status TEXT NOT NULL DEFAULT 'pending', "
        + "memo_artifact_id INTEGER, "
        + "evidence_json TEXT NOT NULL, "
        + "signature_sha TEXT NOT NULL, "
        + "dismissed_at TEXT, "
        + "approved_at TEXT)"
    )
    _ = conn.execute(
        "CREATE TABLE queued_actions ("
        + "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        + "alert_id INTEGER NOT NULL REFERENCES alerts(id), "
        + "action_kind TEXT NOT NULL, "
        + "payload_json TEXT NOT NULL, "
        + "status TEXT NOT NULL DEFAULT 'pending', "
        + "created_at TEXT NOT NULL, "
        + "applied_at TEXT, "
        + "cancelled_at TEXT)"
    )
    _ = conn.execute("PRAGMA journal_mode=WAL")


def _build_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Fresh fixture DB with the driver-facing schema, DB_PATH-routed."""
    path = tmp_path / "sweep.db"
    conn = sqlite3.connect(str(path))
    try:
        _create_schema(conn)
        conn.commit()
    finally:
        conn.close()
    monkeypatch.setattr("db.DB_PATH", str(path))
    return path


def _seed_news(db_path: Path, tickers: list[str]) -> None:
    """One recent story (1h old) per ticker — each triggers one batched call."""
    now = datetime.now(UTC).replace(tzinfo=None)
    conn = sqlite3.connect(str(db_path))
    try:
        for index, ticker in enumerate(tickers):
            _ = conn.execute(
                "INSERT INTO news (ticker, headline, url, published_at, snippet) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    ticker,
                    f"{ticker} announces a primary development",
                    f"https://news.example/{ticker}",
                    (now - timedelta(hours=1, seconds=index)).strftime("%Y-%m-%d %H:%M:%S"),
                    "Issuer announcement with thesis impact.",
                ),
            )
        conn.commit()
    finally:
        conn.close()


def _seed_five_quarters(db_path: Path, *, ticker: str) -> None:
    """Current + 4 prior transcripts so earnings_tone emits a live candidate."""
    now = datetime.now(UTC).replace(tzinfo=None)
    conn = sqlite3.connect(str(db_path))
    try:
        for fiscal_period_type, period_end, fetched_at in (
            ("Q1", "2025-03-31", now - timedelta(days=90)),
            ("Q2", "2025-06-30", now - timedelta(days=90)),
            ("Q3", "2025-09-30", now - timedelta(days=90)),
            ("Q4", "2025-12-31", now - timedelta(days=90)),
            ("Q1", "2026-03-31", now - timedelta(hours=1)),
        ):
            cur = conn.execute(
                "INSERT INTO documents (fetched_at) VALUES (?)",
                (fetched_at.strftime("%Y-%m-%d %H:%M:%S"),),
            )
            doc_id = cur.lastrowid
            cur = conn.execute(
                "INSERT INTO transcripts (document_id, ticker, fiscal_period_type, "
                "period_end, has_qa_section) VALUES (?, ?, ?, ?, ?)",
                (doc_id, ticker, fiscal_period_type, period_end, 1),
            )
            transcript_id = cur.lastrowid
            for seq, (speaker, text) in enumerate(
                (
                    ("CEO", "Welcome everyone. Revenue grew 25 percent this quarter."),
                    ("CFO", "Operating margin expanded 200 basis points."),
                    ("Operator", "Thank you. First question comes from an analyst."),
                    ("Analyst", "Can you walk through the cloud margin trajectory?"),
                    ("CEO", "We expect continued margin expansion through the year."),
                )
            ):
                _ = conn.execute(
                    "INSERT INTO transcript_segments (transcript_id, seq, speaker, text) "
                    "VALUES (?, ?, ?, ?)",
                    (transcript_id, seq, speaker, text),
                )
        conn.commit()
    finally:
        conn.close()


def _news_events(db_path: Path) -> list[tuple[str, str, str, float]]:
    """Persisted news_events rows as (ticker, event_key, event_type, relevance)."""
    conn = sqlite3.connect(str(db_path))
    try:
        return [
            (str(row[0]), str(row[1]), str(row[2]), float(row[3]))
            for row in conn.execute(
                "SELECT ticker, event_key, event_type, relevance FROM news_events ORDER BY ticker"
            ).fetchall()
        ]
    finally:
        conn.close()


def _llm_call_rows(db_path: Path) -> list[tuple[str, str, float]]:
    """Ledger rows as (purpose, ticker, cost) — the cap-accounting surface."""
    conn = sqlite3.connect(str(db_path))
    try:
        return [
            (str(row[0]), str(row[1]), float(row[2]))
            for row in conn.execute(
                "SELECT purpose, ticker, cost_estimate_usd FROM llm_calls ORDER BY id"
            ).fetchall()
        ]
    finally:
        conn.close()


def _summary_events(
    capsys: pytest.CaptureFixture[str],
) -> tuple[dict[str, object], list[dict[str, object]]]:
    """The run-summary JSON from stdout plus the structured events from stderr."""
    captured = capsys.readouterr()
    summary = json.loads(captured.out)
    events: list[dict[str, object]] = []
    for line in captured.err.splitlines():
        if not line.startswith("{"):
            continue
        try:
            events.append(json.loads(line))
        except ValueError:
            continue
    return summary, events


def _diff_payload() -> str:
    """Canned JSON shaped like the earnings_tone_diff prompt's expected response."""
    return json.dumps(
        {
            "summary": "Margin commentary softened materially this quarter.",
            "shifts": [
                {
                    "topic": "Cloud operating margin trajectory",
                    "direction": "softer",
                    "confidence": 0.82,
                    "citations": [],
                    "related_thesis_kpi": "Cloud Op Margin",
                },
                {
                    "topic": "Capex pace commentary",
                    "direction": "firmer",
                    "confidence": 0.74,
                    "citations": [],
                    "related_thesis_kpi": None,
                },
            ],
            "no_material_shifts_detected": False,
        }
    )


# ---------------------------------------------------------------------------
# Test-local wave trace records
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _SweepCall:
    """One stubbed material-news call, stamped with its test-local wave index.

    ``wave`` is ``-1`` when no tracer is attached. Otherwise it is the index of
    the wave whose dispatch was in flight when the call entered the stub — the
    number of sweep admission reads recorded at that instant minus one. This
    is a pure test-local label; production carries no wave identity.
    """

    ticker: str
    cost_usd: float
    wave: int
    started: float
    finished: float


@dataclass(frozen=True, slots=True)
class _AdmissionRead:
    """One ``query_run_cost_usd`` read the driver made, as the trace saw it."""

    spend: Decimal
    at: float
    caller: str


# ---------------------------------------------------------------------------
# The stubbed LLM boundary
# ---------------------------------------------------------------------------


class _StubMaterialNewsBoundary:
    """Stubbed material-news LLM boundary: fixed classifications + ledger rows.

    Mirrors the production transport's observable effects at the sanctioned
    injection seam (``triggers.material_news.call_llm``): one attributable
    ``llm_calls`` row per call — the transport's ``record_llm_call`` writes
    exactly this row after each call — plus a fixed one-entry classification
    payload. Tracks the in-flight count to prove the worker bound, and can
    hold specified tickers inside a call until ``release`` is set, simulating
    a hung call without blocking interpreter exit on an abandoned thread.

    When ``wave_probe`` is attached, each call also records its entry/exit
    monotonic times and the wave index the probe reports at entry, so the
    caller can reconstruct admitted/completed calls per wave without any
    production wave id.
    """

    def __init__(
        self,
        *,
        db_path: Path,
        cost_usd: float = 0.5,
        delay_s: float = 0.0,
        hang_tickers: frozenset[str] = frozenset(),
        wave_probe: Callable[[], int] | None = None,
    ) -> None:
        self.db_path = db_path
        self.cost_usd = cost_usd
        self.delay_s = delay_s
        self.hang_tickers = hang_tickers
        self.wave_probe = wave_probe
        self.trace: list[_SweepCall] = []
        self.release = threading.Event()
        self.calls = 0
        self.finished = 0
        self.in_flight = 0
        self.max_in_flight = 0
        self._lock = threading.Lock()

    def __call__(self, prompt: str, **kwargs: object) -> str:
        ticker = str(kwargs.get("ticker"))
        started = time.monotonic()
        wave = self.wave_probe() if self.wave_probe is not None else -1
        with self._lock:
            self.calls += 1
            self.in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            if ticker in self.hang_tickers:
                self.release.wait()
            elif self.delay_s:
                time.sleep(self.delay_s)
            self._record_ledger_row(prompt, ticker)
            return json.dumps(
                [
                    {
                        "news_index": 0,
                        "event_type": "primary",
                        "event_key": f"{ticker.lower()}_primary_event",
                        "relevance": 0.9,
                        "why_material": "New primary information.",
                    }
                ]
            )
        finally:
            finished = time.monotonic()
            with self._lock:
                self.in_flight -= 1
                self.finished += 1
            self.trace.append(
                _SweepCall(
                    ticker=ticker,
                    cost_usd=self.cost_usd,
                    wave=wave,
                    started=started,
                    finished=finished,
                )
            )

    def _record_ledger_row(self, prompt: str, ticker: str) -> None:
        conn = sqlite3.connect(str(self.db_path))
        try:
            _ = conn.execute(
                "INSERT INTO llm_calls(called_at, purpose, ticker, model, "
                "prompt_sha256, prompt_chars, elapsed_ms, cost_estimate_usd) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    datetime.now(UTC).isoformat(),
                    "material_news_classification",
                    ticker,
                    "claude-opus-4-8",
                    "0" * 64,
                    len(prompt),
                    1,
                    self.cost_usd,
                ),
            )
            conn.commit()
        finally:
            conn.close()


class _StubEarningsToneBoundary:
    """Stubbed earnings_tone boundary: fixed diff payload + one ledger row.

    The earnings_tone LLM call runs inside ``build_alert`` (the per-candidate
    loop), so its row models candidate spend that the sweep's cap admission
    must account after the serial loop finishes.
    """

    def __init__(self, *, db_path: Path, cost_usd: float) -> None:
        self.db_path = db_path
        self.cost_usd = cost_usd
        self.calls = 0

    def __call__(self, _prompt: str, **kwargs: object) -> str:
        self.calls += 1
        conn = sqlite3.connect(str(self.db_path))
        try:
            _ = conn.execute(
                "INSERT INTO llm_calls(called_at, purpose, ticker, model, "
                "prompt_sha256, prompt_chars, elapsed_ms, cost_estimate_usd) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    datetime.now(UTC).isoformat(),
                    "earnings_tone_diff",
                    str(kwargs.get("ticker")),
                    "claude-opus-4-8",
                    "0" * 64,
                    0,
                    1,
                    self.cost_usd,
                ),
            )
            conn.commit()
        finally:
            conn.close()
        return _diff_payload()


# ---------------------------------------------------------------------------
# 1. Bounded concurrency, full coverage, ledger attribution
# ---------------------------------------------------------------------------


def test_sweep_covers_all_tickers_within_worker_bound(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Ten tickers classify under a 3-worker bound: every ticker covered, the
    observed maximum simultaneous calls never exceeds the bound, and every
    call leaves an attributable material-news ledger row."""
    db_path = _build_db(tmp_path, monkeypatch)
    tickers = [f"T{i:02d}" for i in range(10)]
    _seed_news(db_path, tickers)
    stub = _StubMaterialNewsBoundary(db_path=db_path, delay_s=0.04)
    monkeypatch.setattr("triggers.material_news.call_llm", stub)

    exit_code = run_triggers.main(
        [
            "--tickers",
            ",".join(tickers),
            "--triggers",
            "material_news",
            "--db-path",
            str(db_path),
            "--scan-workers",
            "3",
            "--max-cost-usd",
            "100",
        ]
    )
    assert exit_code == 0

    assert stub.calls == 10
    assert 2 <= stub.max_in_flight <= 3

    # Every ticker classified and persisted exactly one qualifying event.
    assert [row[0] for row in _news_events(db_path)] == tickers

    # Every call is attributable in the ledger: purpose + ticker + cost.
    rows = _llm_call_rows(db_path)
    assert len(rows) == 10
    assert {row[0] for row in rows} == {"material_news_classification"}
    assert sorted(row[1] for row in rows) == tickers

    summary, events = _summary_events(capsys)
    assert summary["tickers_processed"] == 10
    assert summary["no_candidate_skips"] == 10
    assert summary["scan_deferrals"] == []
    assert summary["cost_cap_reached"] is False
    sweep_starts = [e for e in events if e.get("event") == "scan_sweep_start"]
    assert len(sweep_starts) == 1
    assert sweep_starts[0]["workers"] == 3


def test_single_worker_sweep_runs_one_call_at_a_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--scan-workers 1 degrades the sweep to strictly serial scans: the
    in-flight bound of 1 is honored and every ticker still classifies."""
    db_path = _build_db(tmp_path, monkeypatch)
    tickers = ["TA", "TB", "TC"]
    _seed_news(db_path, tickers)
    stub = _StubMaterialNewsBoundary(db_path=db_path, delay_s=0.02)
    monkeypatch.setattr("triggers.material_news.call_llm", stub)

    exit_code = run_triggers.main(
        [
            "--tickers",
            ",".join(tickers),
            "--triggers",
            "material_news",
            "--db-path",
            str(db_path),
            "--scan-workers",
            "1",
            "--max-cost-usd",
            "100",
        ]
    )
    assert exit_code == 0
    assert stub.calls == 3
    assert stub.max_in_flight == 1
    assert [row[0] for row in _news_events(db_path)] == tickers


# ---------------------------------------------------------------------------
# 2. Cost cap: the sweep's own spend gates admission; deferrals are explicit
# ---------------------------------------------------------------------------


def test_over_cap_sweep_stops_admitting_waves_and_defers_remaining(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Eight tickers at $0.50/call under a $2.00 cap with 4 workers: exactly
    one wave (4 calls, $2.00 total — at the cap, not over it) is admitted; the
    other four tickers are explicitly deferred with reason cost_cap and never
    dispatched."""
    db_path = _build_db(tmp_path, monkeypatch)
    tickers = [f"U{i:02d}" for i in range(8)]
    _seed_news(db_path, tickers)
    stub = _StubMaterialNewsBoundary(db_path=db_path, cost_usd=0.5)
    monkeypatch.setattr("triggers.material_news.call_llm", stub)

    exit_code = run_triggers.main(
        [
            "--tickers",
            ",".join(tickers),
            "--triggers",
            "material_news",
            "--db-path",
            str(db_path),
            "--scan-workers",
            "4",
            "--max-cost-usd",
            "2.0",
        ]
    )
    assert exit_code == 0

    # Exactly one wave dispatched: no excess calls, total spend at the cap.
    assert stub.calls == 4
    total = sum(row[2] for row in _llm_call_rows(db_path))
    assert total == pytest.approx(2.0)
    assert [row[0] for row in _news_events(db_path)] == tickers[:4]

    summary, events = _summary_events(capsys)
    assert summary["cost_cap_reached"] is True
    assert summary["scan_deferrals"] == [
        {"ticker": ticker, "trigger_kind": "material_news", "reason": "cost_cap"}
        for ticker in tickers[4:]
    ]
    cap_events = [e for e in events if e.get("event") == "cost_cap_reached"]
    assert len(cap_events) == 1
    deferred = [e for e in events if e.get("event") == "scan_deferred"]
    assert {e["reason"] for e in deferred} == {"cost_cap"}
    assert [e["ticker"] for e in deferred] == tickers[4:]


def test_zero_cap_defers_every_ticker_without_dispatching_any_call(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A cap of 0 admits no wave at all: every ticker is explicitly deferred
    with reason cost_cap, zero calls are dispatched, zero events persisted."""
    db_path = _build_db(tmp_path, monkeypatch)
    tickers = ["ZA", "ZB", "ZC", "ZD"]
    _seed_news(db_path, tickers)
    stub = _StubMaterialNewsBoundary(db_path=db_path, cost_usd=0.5)
    monkeypatch.setattr("triggers.material_news.call_llm", stub)

    exit_code = run_triggers.main(
        [
            "--tickers",
            ",".join(tickers),
            "--triggers",
            "material_news",
            "--db-path",
            str(db_path),
            "--max-cost-usd",
            "0",
        ]
    )
    assert exit_code == 0
    assert stub.calls == 0
    assert _news_events(db_path) == []
    summary, _events = _summary_events(capsys)
    assert summary["cost_cap_reached"] is True
    assert summary["scan_deferrals"] == [
        {"ticker": ticker, "trigger_kind": "material_news", "reason": "cost_cap"}
        for ticker in tickers
    ]


def test_candidate_spend_gates_the_sweep_and_alerts_keep_priority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Cross-lane accounting: the alert lane (earnings_tone build_alert spend)
    is cap-gated exactly as before and fires first, and its ledger spend then
    gates the material-news sweep's wave admission — the cap decision accounts
    material-news spend rather than only emitted candidates."""
    db_path = _build_db(tmp_path, monkeypatch)
    _seed_five_quarters(db_path, ticker="MSFT")
    news_tickers = [f"N{i:02d}" for i in range(6)]
    _seed_news(db_path, news_tickers)
    tone_stub = _StubEarningsToneBoundary(db_path=db_path, cost_usd=8.0)
    news_stub = _StubMaterialNewsBoundary(db_path=db_path, cost_usd=0.5)
    monkeypatch.setattr("triggers.earnings_tone.call_llm", tone_stub)
    monkeypatch.setattr("triggers.material_news.call_llm", news_stub)
    tickers = ["MSFT", *news_tickers]

    exit_code = run_triggers.main(
        [
            "--tickers",
            ",".join(tickers),
            "--triggers",
            "earnings_tone,material_news",
            "--db-path",
            str(db_path),
            "--scan-workers",
            "4",
            "--max-cost-usd",
            "9.0",
        ]
    )
    assert exit_code == 0

    # The alert lane fired first and spent first (today's priority preserved).
    assert tone_stub.calls == 1
    conn = sqlite3.connect(str(db_path))
    try:
        alert_rows = conn.execute("SELECT ticker, trigger_kind FROM alerts").fetchall()
    finally:
        conn.close()
    assert alert_rows == [("MSFT", "earnings_tone")]

    # Wave 1 (MSFT's empty-news scan + three news tickers) was admitted at
    # $8.00 < $9.00 and landed at $9.50; wave 2 admission deferred the rest.
    assert news_stub.calls == 3
    assert [row[0] for row in _news_events(db_path)] == news_tickers[:3]
    total = sum(row[2] for row in _llm_call_rows(db_path))
    assert total == pytest.approx(9.5)

    summary, _events = _summary_events(capsys)
    assert summary["cost_cap_reached"] is True
    assert summary["scan_deferrals"] == [
        {"ticker": ticker, "trigger_kind": "material_news", "reason": "cost_cap"}
        for ticker in news_tickers[3:]
    ]


# ---------------------------------------------------------------------------
# 3. Per-ticker timeout: a hung ticker defers only itself
# ---------------------------------------------------------------------------


def test_hung_ticker_hits_per_ticker_timeout_while_others_complete(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A ticker whose call hangs is deferred at its per-ticker timeout with
    an explicit per_ticker_timeout disposition while the rest of the book
    completes; the run does not wait on the hung call."""
    db_path = _build_db(tmp_path, monkeypatch)
    tickers = ["HANG", "TB", "TC", "TD"]
    _seed_news(db_path, tickers)
    stub = _StubMaterialNewsBoundary(db_path=db_path, hang_tickers=frozenset({"HANG"}))
    monkeypatch.setattr("triggers.material_news.call_llm", stub)

    started = time.monotonic()
    exit_code = run_triggers.main(
        [
            "--tickers",
            ",".join(tickers),
            "--triggers",
            "material_news",
            "--db-path",
            str(db_path),
            "--scan-workers",
            "2",
            "--scan-timeout-s",
            "0.3",
            "--max-cost-usd",
            "100",
        ]
    )
    elapsed = time.monotonic() - started
    assert exit_code == 0
    # The run returned after the 0.3s timeout, not the (infinite) hang.
    assert elapsed < 5.0

    summary, events = _summary_events(capsys)
    assert summary["scan_deferrals"] == [
        {"ticker": "HANG", "trigger_kind": "material_news", "reason": "per_ticker_timeout"}
    ]
    timeout_events = [e for e in events if e.get("event") == "scan_deferred"]
    assert timeout_events == [
        {
            "event": "scan_deferred",
            "ticker": "HANG",
            "trigger_kind": "material_news",
            "reason": "per_ticker_timeout",
        }
    ]

    # The rest of the book completed while the hung ticker only deferred itself.
    assert [row[0] for row in _news_events(db_path)] == ["TB", "TC", "TD"]
    assert summary["cost_cap_reached"] is False

    # The abandoned worker thread is not waited on: releasing the hang lets it
    # finish its already-dispatched scan (idempotent late completion) without
    # the run ever having blocked on it.
    stub.release.set()
    for _ in range(250):
        if stub.finished >= 4:
            break
        time.sleep(0.02)
    assert stub.finished == 4
    assert [row[0] for row in _news_events(db_path)] == ["HANG", "TB", "TC", "TD"]


# ---------------------------------------------------------------------------
# 4. CLI contract
# ---------------------------------------------------------------------------


def test_scan_worker_flags_fail_closed_on_invalid_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--scan-workers < 1 and --scan-timeout-s <= 0 are rejected before any
    DB or LLM work (fail closed on misconfiguration)."""
    _build_db(tmp_path, monkeypatch)
    with pytest.raises(SystemExit) as workers_exc:
        run_triggers.main(
            [
                "--tickers",
                "",
                "--db-path",
                str(tmp_path / "sweep.db"),
                "--scan-workers",
                "0",
            ]
        )
    assert workers_exc.value.code == 2
    with pytest.raises(SystemExit) as timeout_exc:
        run_triggers.main(
            [
                "--tickers",
                "",
                "--db-path",
                str(tmp_path / "sweep.db"),
                "--scan-timeout-s",
                "0",
            ]
        )
    assert timeout_exc.value.code == 2


# ---------------------------------------------------------------------------
# 5. Wave accounting at the stubbed boundary: VAL-PIPE-003's pre-dispatch
#    admission, settlement ordering, one-final-wave overshoot bound, and
#    per-ticker deferrals made directly observable. Test-local instrumentation
#    only — no production wave id, log line, or schema.
# ---------------------------------------------------------------------------


def _caller_name() -> str:
    """Name of the driver function two frames above this helper.

    ``_caller_name`` -> the tracer's ``recording`` wrapper -> the driver
    function that performed the ledger read: ``_run_scan_sweep`` for a wave
    admission, ``_process_ticker_trigger`` for the serial candidate lane. Used
    only to attribute a read to a lane in the test trace.
    """
    frame = inspect.currentframe()
    if frame is None or frame.f_back is None:
        return ""
    caller = frame.f_back.f_back
    if caller is None:
        return ""
    return caller.f_code.co_name


class _WaveAdmissionTracer:
    """Test-local record of every shared-ledger read the driver performs.

    Wraps the public, test-monkeypatchable seam the driver already exposes
    (``run_triggers.query_run_cost_usd``) and returns the real value unchanged;
    it only records ``(spend, monotonic time, caller)``. The sweep performs
    exactly one read per wave and the serial candidate lane one read per
    candidate, which is what makes the wave structure observable here without
    adding any production wave identity, logging, or schema.
    """

    SWEEP_CALLER = "_run_scan_sweep"
    SERIAL_CALLER = "_process_ticker_trigger"

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.reads: list[_AdmissionRead] = []
        original = run_triggers.query_run_cost_usd

        def recording(db_path: Path, run_started_at: datetime) -> Decimal:
            spend = original(db_path, run_started_at)
            self.reads.append(
                _AdmissionRead(spend=spend, at=time.monotonic(), caller=_caller_name())
            )
            return spend

        monkeypatch.setattr(run_triggers, "query_run_cost_usd", recording)

    def sweep_reads(self) -> list[_AdmissionRead]:
        """The sweep lane's per-wave admission reads, in dispatch order."""
        return [read for read in self.reads if read.caller == self.SWEEP_CALLER]

    def serial_reads(self) -> list[_AdmissionRead]:
        """The serial candidate lane's per-candidate reads, in dispatch order."""
        return [read for read in self.reads if read.caller == self.SERIAL_CALLER]

    def next_wave_index(self) -> int:
        """Test-local wave index for the calls about to be dispatched.

        Called from inside a stubbed call at entry, when exactly that wave's
        admission read (and no later one) has been recorded.
        """
        return len(self.sweep_reads()) - 1


def _calls_by_wave(stub: _StubMaterialNewsBoundary) -> dict[int, list[_SweepCall]]:
    """Group the stub's call trace by its test-local wave index."""
    grouped: dict[int, list[_SweepCall]] = {}
    for call in stub.trace:
        grouped.setdefault(call.wave, []).append(call)
    return grouped


def test_wave_trace_proves_preadmission_spend_and_bounded_final_wave(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Eight tickers at $0.50 under a $2.00 cap with three workers, traced
    call-by-call: each wave is admitted only on committed shared-ledger spend
    below the cap, settles before the next admission read, never exceeds the
    worker width, and the single crossing wave's actual cost bounds the only
    permitted overshoot (C < T <= C + F) with every unstarted ticker deferred."""
    db_path = _build_db(tmp_path, monkeypatch)
    tickers = [f"W{i:02d}" for i in range(8)]
    _seed_news(db_path, tickers)
    tracer = _WaveAdmissionTracer(monkeypatch)
    stub = _StubMaterialNewsBoundary(
        db_path=db_path,
        cost_usd=0.5,
        delay_s=0.05,
        wave_probe=tracer.next_wave_index,
    )
    monkeypatch.setattr("triggers.material_news.call_llm", stub)

    cap_usd = 2.0
    workers = 3
    exit_code = run_triggers.main(
        [
            "--tickers",
            ",".join(tickers),
            "--triggers",
            "material_news",
            "--db-path",
            str(db_path),
            "--scan-workers",
            str(workers),
            "--max-cost-usd",
            str(cap_usd),
        ]
    )
    assert exit_code == 0

    # One admission read per attempted wave, from the sweep lane only.
    sweep_reads = tracer.sweep_reads()
    assert tracer.serial_reads() == []
    assert len(tracer.reads) == len(sweep_reads) == 3
    spends = [float(read.spend) for read in sweep_reads]
    assert spends == pytest.approx([0.0, 1.5, 3.0], abs=1e-9)

    # Waves 0 and 1 were admitted on pre-dispatch spend strictly below the
    # cap; the third read already saw spend at/over it, so no wave dispatched.
    assert spends[0] < cap_usd
    assert spends[1] < cap_usd
    assert spends[2] >= cap_usd

    by_wave = _calls_by_wave(stub)
    assert sorted(by_wave) == [0, 1]
    assert [sorted(call.ticker for call in by_wave[index]) for index in (0, 1)] == [
        ["W00", "W01", "W02"],
        ["W03", "W04", "W05"],
    ]
    for members in by_wave.values():
        assert len(members) <= workers
    assert stub.max_in_flight == workers

    # Each admitted wave fully settled before the next admission read.
    assert max(call.finished for call in by_wave[0]) < sweep_reads[1].at
    assert max(call.finished for call in by_wave[1]) < sweep_reads[2].at

    # Costs reconcile: each wave's trace cost equals the ledger delta its
    # admission produced, and the trace total equals the run's ledger total.
    ledger_total = sum(row[2] for row in _llm_call_rows(db_path))
    assert ledger_total == pytest.approx(3.0)
    assert sum(call.cost_usd for call in stub.trace) == pytest.approx(ledger_total)
    for index in (0, 1):
        wave_cost = sum(call.cost_usd for call in by_wave[index])
        delta = float(sweep_reads[index + 1].spend - sweep_reads[index].spend)
        assert wave_cost == pytest.approx(delta)

    # The unique final wave — the one whose committed cost first brought the
    # ledger to/over the cap — bounds the total overshoot: C < T <= C + F.
    final_wave_index = max(by_wave)
    final_wave_cost = sum(call.cost_usd for call in by_wave[final_wave_index])
    assert final_wave_index == 1
    assert final_wave_cost == pytest.approx(1.5)
    assert ledger_total > cap_usd
    assert ledger_total <= cap_usd + final_wave_cost

    # Every unstarted ticker carries its own explicit cap-stop deferral.
    summary, _events = _summary_events(capsys)
    assert summary["cost_cap_reached"] is True
    assert summary["scan_deferrals"] == [
        {"ticker": ticker, "trigger_kind": "material_news", "reason": "cost_cap"}
        for ticker in ("W06", "W07")
    ]
    assert summary["tickers_processed"] == 6


def test_wave_trace_counts_serial_candidate_spend_before_admission(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Cross-lane admission traced at the boundary: the serial candidate
    lane's committed spend is in the shared ledger before the sweep runs, the
    sweep's admitted wave is gated on that combined spend, and the single
    crossing wave's actual cost bounds total run spend across both lanes."""
    db_path = _build_db(tmp_path, monkeypatch)
    _seed_five_quarters(db_path, ticker="MSFT")
    news_tickers = [f"N{i:02d}" for i in range(5)]
    _seed_news(db_path, ["MSFT", *news_tickers])
    tracer = _WaveAdmissionTracer(monkeypatch)
    tone_stub = _StubEarningsToneBoundary(db_path=db_path, cost_usd=8.0)
    news_stub = _StubMaterialNewsBoundary(
        db_path=db_path,
        cost_usd=0.5,
        delay_s=0.05,
        wave_probe=tracer.next_wave_index,
    )
    monkeypatch.setattr("triggers.earnings_tone.call_llm", tone_stub)
    monkeypatch.setattr("triggers.material_news.call_llm", news_stub)
    tickers = ["MSFT", *news_tickers]

    cap_usd = 9.0
    workers = 4
    exit_code = run_triggers.main(
        [
            "--tickers",
            ",".join(tickers),
            "--triggers",
            "earnings_tone,material_news",
            "--db-path",
            str(db_path),
            "--scan-workers",
            str(workers),
            "--max-cost-usd",
            str(cap_usd),
        ]
    )
    assert exit_code == 0

    # The lanes' reads are separable: one serial per-candidate read ($0.00)
    # then one sweep admission read per wave.
    assert [float(read.spend) for read in tracer.serial_reads()] == [0.0]
    sweep_spends = [float(read.spend) for read in tracer.sweep_reads()]
    assert sweep_spends == pytest.approx([8.0, 10.0], abs=1e-9)

    # The sweep's first — and only — wave was admitted on the shared ledger
    # that already held the serial lane's $8.00, i.e. below the $9.00 cap;
    # the next read saw the crossing at $10.00 and stopped dispatching.
    assert sweep_spends[0] < cap_usd
    assert sweep_spends[1] >= cap_usd

    by_wave = _calls_by_wave(news_stub)
    assert sorted(by_wave) == [0]
    assert sorted(call.ticker for call in by_wave[0]) == ["MSFT", "N00", "N01", "N02"]
    assert news_stub.max_in_flight == workers

    ledger_rows = _llm_call_rows(db_path)
    assert {row[0] for row in ledger_rows} == {
        "earnings_tone_diff",
        "material_news_classification",
    }
    total_run_spend = sum(row[2] for row in ledger_rows)
    assert tone_stub.calls == 1
    final_wave_cost = sum(call.cost_usd for call in by_wave[0])
    assert final_wave_cost == pytest.approx(2.0)
    assert total_run_spend == pytest.approx(10.0)
    assert total_run_spend <= cap_usd + final_wave_cost

    summary, _events = _summary_events(capsys)
    assert summary["cost_cap_reached"] is True
    assert summary["scan_deferrals"] == [
        {"ticker": ticker, "trigger_kind": "material_news", "reason": "cost_cap"}
        for ticker in ("N03", "N04")
    ]


def test_serial_candidate_lane_keeps_its_per_candidate_cap_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The wave allowance stays sweep-only: the serial candidate lane admits
    only while committed spend is below the cap and dispatches no candidate
    once it is at/over, and with the cap already reached the sweep is granted
    no wave at all — every unstarted ticker is explicitly deferred."""
    db_path = _build_db(tmp_path, monkeypatch)
    tickers = ["S1", "S2", "S3", "S4"]
    for ticker in tickers:
        _seed_five_quarters(db_path, ticker=ticker)
    _seed_news(db_path, tickers)
    tracer = _WaveAdmissionTracer(monkeypatch)
    tone_stub = _StubEarningsToneBoundary(db_path=db_path, cost_usd=5.0)
    news_stub = _StubMaterialNewsBoundary(
        db_path=db_path,
        cost_usd=0.5,
        wave_probe=tracer.next_wave_index,
    )
    monkeypatch.setattr("triggers.earnings_tone.call_llm", tone_stub)
    monkeypatch.setattr("triggers.material_news.call_llm", news_stub)

    cap_usd = 9.0
    exit_code = run_triggers.main(
        [
            "--tickers",
            ",".join(tickers),
            "--triggers",
            "earnings_tone,material_news",
            "--db-path",
            str(db_path),
            "--scan-workers",
            "4",
            "--max-cost-usd",
            str(cap_usd),
        ]
    )
    assert exit_code == 0

    # Serial admission is per candidate: two candidates dispatched on spend
    # below the cap; the third read saw spend at/over the cap and halted
    # before any call — no wave-shaped allowance was applied to the lane.
    serial_spends = [float(read.spend) for read in tracer.serial_reads()]
    assert serial_spends == pytest.approx([0.0, 5.0, 10.0], abs=1e-9)
    assert [spend for spend in serial_spends if spend < cap_usd] == pytest.approx([0.0, 5.0])
    assert tone_stub.calls == 2

    # No material-news wave was admitted: the sweep's first read already saw
    # spend at/over the cap, so every sweep ticker was deferred, no call ran.
    assert [float(read.spend) for read in tracer.sweep_reads()] == pytest.approx([10.0])
    assert news_stub.calls == 0
    assert not news_stub.trace

    total_run_spend = sum(row[2] for row in _llm_call_rows(db_path))
    assert total_run_spend == pytest.approx(10.0)
    # The overshoot is bounded by one serial candidate unit, never a wave.
    assert total_run_spend <= cap_usd + 5.0

    summary, _events = _summary_events(capsys)
    assert summary["cost_cap_reached"] is True
    assert summary["scan_deferrals"] == [
        {"ticker": "S4", "trigger_kind": "earnings_tone", "reason": "cost_cap"},
        {"ticker": "S1", "trigger_kind": "material_news", "reason": "cost_cap"},
        {"ticker": "S2", "trigger_kind": "material_news", "reason": "cost_cap"},
        {"ticker": "S3", "trigger_kind": "material_news", "reason": "cost_cap"},
        {"ticker": "S4", "trigger_kind": "material_news", "reason": "cost_cap"},
    ]
