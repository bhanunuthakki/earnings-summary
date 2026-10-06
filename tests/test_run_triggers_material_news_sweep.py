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
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from datetime import UTC, datetime, timedelta
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
    """

    def __init__(
        self,
        *,
        db_path: Path,
        cost_usd: float = 0.5,
        delay_s: float = 0.0,
        hang_tickers: frozenset[str] = frozenset(),
    ) -> None:
        self.db_path = db_path
        self.cost_usd = cost_usd
        self.delay_s = delay_s
        self.hang_tickers = hang_tickers
        self.release = threading.Event()
        self.calls = 0
        self.finished = 0
        self.in_flight = 0
        self.max_in_flight = 0
        self._lock = threading.Lock()

    def __call__(self, prompt: str, **kwargs: object) -> str:
        ticker = str(kwargs.get("ticker"))
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
            with self._lock:
                self.in_flight -= 1
                self.finished += 1

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
