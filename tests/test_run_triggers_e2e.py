"""End-to-end tests for execution/run_triggers.py — the daily trigger driver.

Each test runs ``run_triggers.main()`` against a fresh tmp SQLite DB with a
mocked ``call_llm`` so no real Anthropic calls are made. The fixture creates
exactly the subset of schema the driver touches (alerts, queued_actions,
llm_calls, user_kpi_registry, position_sizing_intent, plus the transcript /
documents / llm_artifacts trio the earnings_tone trigger reads through).

Verifies the eight scenarios described in PR-N9:
  * happy path  — one fresh transcript fires one alert + persisted actions
  * dedup hit   — a second invocation against unchanged DB state is a no-op
  * dry-run     — driver runs cleanly but persists nothing
  * isolation   — an LLM exception on ticker A doesn't block ticker B
  * cost cap    — patched cost query returning above-cap halts the loop
  * idle trigger — kpi_inflection finds no registered KPIs here, so scan→[]
                   and it contributes nothing alongside an active earnings_tone
  * no scan hit — no transcripts in window → no_candidates, no alert
  * empty list  — --tickers '' → exits 0 with zero processed
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from execution import run_triggers

# ---------------------------------------------------------------------------
# Schema fixture — minimal subset of migrations 0034, 0035, 0060, 0061,
# 0063, 0064 plus the transcript triple (documents / transcripts /
# transcript_segments).
# ---------------------------------------------------------------------------


def _create_schema(conn: sqlite3.Connection) -> None:
    """Create the tables the driver + trigger + alerts CRUD touch.

    Mirrors the production schema column-for-column for the fields under
    test; defaults / triggers are dropped where they aren't load-bearing
    for these tests.
    """
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
    _ = conn.execute(
        "CREATE TABLE user_kpi_registry ("
        + "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        + "user_id TEXT NOT NULL DEFAULT 'bhanu', "
        + "ticker TEXT NOT NULL, "
        + "kpi_name TEXT NOT NULL, "
        + "threshold_direction TEXT, "
        + "threshold_value REAL, "
        + "is_thesis_breaker INTEGER NOT NULL DEFAULT 0, "
        + "scaffold_source TEXT, "
        + "notes TEXT, "
        + "created_at TEXT NOT NULL, "
        + "updated_at TEXT NOT NULL)"
    )
    _ = conn.execute(
        "CREATE TABLE position_sizing_intent ("
        + "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        + "user_id TEXT NOT NULL DEFAULT 'bhanu', "
        + "ticker TEXT NOT NULL, "
        + "intent_kind TEXT NOT NULL, "
        + "intent_value REAL, "
        + "narrative TEXT, "
        + "created_at TEXT NOT NULL, "
        + "updated_at TEXT NOT NULL)"
    )
    _ = conn.execute("PRAGMA journal_mode=WAL")


def _insert_transcript(
    conn: sqlite3.Connection,
    *,
    ticker: str,
    fiscal_period_type: str,
    period_end: str,
    fetched_at: datetime,
    has_qa_section: bool | None = True,
    segments: list[tuple[str, str]] | None = None,
) -> int:
    """Insert a documents + transcripts + segments triple. Returns transcript id."""
    cur = conn.execute(
        "INSERT INTO documents (fetched_at) VALUES (?)",
        (fetched_at.strftime("%Y-%m-%d %H:%M:%S"),),
    )
    doc_id = cur.lastrowid
    cur = conn.execute(
        "INSERT INTO transcripts "
        "(document_id, ticker, fiscal_period_type, period_end, has_qa_section) "
        "VALUES (?, ?, ?, ?, ?)",
        (
            doc_id,
            ticker,
            fiscal_period_type,
            period_end,
            None if has_qa_section is None else (1 if has_qa_section else 0),
        ),
    )
    transcript_id = int(cur.lastrowid) if cur.lastrowid is not None else 0
    if segments is None:
        segments = [
            ("CEO", "Welcome everyone. Revenue grew 25 percent this quarter."),
            ("CFO", "Operating margin expanded 200 basis points."),
            ("Operator", "Thank you. Our first question comes from an analyst."),
            ("Analyst", "Can you walk through the cloud margin trajectory?"),
            ("CEO", "We expect continued margin expansion through the year."),
        ]
    for seq, (speaker, text) in enumerate(segments):
        _ = conn.execute(
            "INSERT INTO transcript_segments (transcript_id, seq, speaker, text) "
            "VALUES (?, ?, ?, ?)",
            (transcript_id, seq, speaker, text),
        )
    conn.commit()
    return transcript_id


def _seed_five_quarters(conn: sqlite3.Connection, *, ticker: str) -> int:
    """Insert current + 4 prior transcripts. Returns the current transcript id."""
    now = datetime.now(UTC).replace(tzinfo=None)
    priors = [
        ("Q1", "2025-03-31"),
        ("Q2", "2025-06-30"),
        ("Q3", "2025-09-30"),
        ("Q4", "2025-12-31"),
    ]
    for fpt, period_end in priors:
        _ = _insert_transcript(
            conn,
            ticker=ticker,
            fiscal_period_type=fpt,
            period_end=period_end,
            fetched_at=now - timedelta(days=90),
        )
    return _insert_transcript(
        conn,
        ticker=ticker,
        fiscal_period_type="Q1",
        period_end="2026-03-31",
        fetched_at=now - timedelta(hours=1),
    )


# ---------------------------------------------------------------------------
# Fixtures + LLM mock
# ---------------------------------------------------------------------------


@pytest.fixture
def db_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Build the fresh test DB and route every ``DB_PATH`` consumer to it."""
    path = tmp_path / "test.db"
    conn = sqlite3.connect(str(path))
    try:
        _create_schema(conn)
        conn.commit()
    finally:
        conn.close()
    monkeypatch.setattr("db.DB_PATH", str(path))
    return path


def _diff_payload(
    *,
    summary: str = "Margin commentary softened materially this quarter.",
    shifts: list[dict[str, object]] | None = None,
    no_shifts: bool = False,
) -> str:
    """Canned JSON shaped like the earnings_tone_diff prompt's expected response."""
    if no_shifts:
        return json.dumps(
            {
                "summary": "No material tone shifts detected this quarter.",
                "shifts": [],
                "no_material_shifts_detected": True,
            }
        )
    if shifts is None:
        shifts = [
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
        ]
    return json.dumps(
        {
            "summary": summary,
            "shifts": shifts,
            "no_material_shifts_detected": False,
        }
    )


class _CountingLLM:
    """Stateful mock that returns the canned response and counts invocations."""

    def __init__(self, responses: list[str]) -> None:
        self.responses = responses
        self.call_count = 0

    def __call__(self, _prompt: str, **_kwargs: object) -> str:
        idx = min(self.call_count, len(self.responses) - 1)
        self.call_count += 1
        return self.responses[idx]


class _RaisingLLM:
    """Per-ticker mock: raises for one ticker, returns canned for the others."""

    def __init__(self, fail_ticker: str, response: str) -> None:
        self.fail_ticker = fail_ticker
        self.response = response
        self.call_count = 0

    def __call__(self, _prompt: str, **kwargs: object) -> str:
        self.call_count += 1
        if kwargs.get("ticker") == self.fail_ticker:
            raise RuntimeError(f"simulated LLM failure on {self.fail_ticker}")
        return self.response


# ---------------------------------------------------------------------------
# 1. Happy path
# ---------------------------------------------------------------------------


def test_happy_path_fires_alert_and_actions(
    db_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """One fresh transcript → one alert + actions persisted."""
    conn = sqlite3.connect(str(db_path))
    try:
        _ = _seed_five_quarters(conn, ticker="MELI")
    finally:
        conn.close()

    monkeypatch.setattr("triggers.earnings_tone.call_llm", _CountingLLM([_diff_payload()]))

    exit_code = run_triggers.main(
        [
            "--tickers",
            "MELI",
            "--db-path",
            str(db_path),
            "--max-cost-usd",
            "10",
        ]
    )
    assert exit_code == 0

    conn = sqlite3.connect(str(db_path))
    try:
        alerts = conn.execute("SELECT * FROM alerts").fetchall()
        actions = conn.execute("SELECT * FROM queued_actions").fetchall()
    finally:
        conn.close()

    assert len(alerts) == 1
    (
        _id,
        user_id,
        ticker,
        trigger_kind,
        _fired_at,
        status,
        _memo_artifact_id,
        evidence_json,
        signature_sha,
        _dismissed_at,
        _approved_at,
    ) = alerts[0]
    assert user_id == "bhanu"
    assert ticker == "MELI"
    assert trigger_kind == "earnings_tone"
    assert status == "pending"
    assert signature_sha  # non-empty
    evidence = json.loads(evidence_json)
    assert evidence["no_material_shifts_detected"] is False
    assert len(evidence["shifts"]) == 2

    # Default _diff_payload produces two machine-analysis prep reminders only.
    kinds = [a[2] for a in actions]
    assert kinds.count("earnings_prep_append") == 2
    assert set(kinds) == {"earnings_prep_append"}

    # Summary line on stdout reports 1 alert fired.
    captured = capsys.readouterr()
    summary = json.loads(captured.out)
    assert summary["alerts_fired"] == 1
    assert summary["tickers_processed"] == 1
    assert summary["errors"] == 0


# ---------------------------------------------------------------------------
# 2. Dedup
# ---------------------------------------------------------------------------


def test_dedup_second_run_persists_nothing(db_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A second invocation against unchanged state hits find_by_signature → no insert."""
    conn = sqlite3.connect(str(db_path))
    try:
        _ = _seed_five_quarters(conn, ticker="MELI")
    finally:
        conn.close()

    llm = _CountingLLM([_diff_payload()])
    monkeypatch.setattr("triggers.earnings_tone.call_llm", llm)

    assert (
        run_triggers.main(
            [
                "--tickers",
                "MELI",
                "--db-path",
                str(db_path),
                "--max-cost-usd",
                "10",
            ]
        )
        == 0
    )
    assert (
        run_triggers.main(
            [
                "--tickers",
                "MELI",
                "--db-path",
                str(db_path),
                "--max-cost-usd",
                "10",
            ]
        )
        == 0
    )

    conn = sqlite3.connect(str(db_path))
    try:
        alert_count = conn.execute("SELECT COUNT(*) FROM alerts").fetchone()[0]
        action_count = conn.execute("SELECT COUNT(*) FROM queued_actions").fetchone()[0]
    finally:
        conn.close()

    assert alert_count == 1, "dedup failed — duplicate alert persisted"
    assert action_count == 2, "dedup failed — duplicate actions persisted"


# ---------------------------------------------------------------------------
# 3. Dry-run
# ---------------------------------------------------------------------------


def test_dry_run_persists_nothing(
    db_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """--dry-run produces an AlertDraft log but skips fire_alert / queue_action."""
    conn = sqlite3.connect(str(db_path))
    try:
        _ = _seed_five_quarters(conn, ticker="MELI")
    finally:
        conn.close()

    monkeypatch.setattr("triggers.earnings_tone.call_llm", _CountingLLM([_diff_payload()]))

    exit_code = run_triggers.main(
        [
            "--tickers",
            "MELI",
            "--db-path",
            str(db_path),
            "--dry-run",
            "--max-cost-usd",
            "10",
        ]
    )
    assert exit_code == 0

    conn = sqlite3.connect(str(db_path))
    try:
        alert_count = conn.execute("SELECT COUNT(*) FROM alerts").fetchone()[0]
        action_count = conn.execute("SELECT COUNT(*) FROM queued_actions").fetchone()[0]
    finally:
        conn.close()
    assert alert_count == 0
    assert action_count == 0

    captured = capsys.readouterr()
    summary = json.loads(captured.out)
    assert summary["alerts_fired"] == 0
    assert summary["dry_run_alerts"] == 1


# ---------------------------------------------------------------------------
# 3b. No-material gate: an earnings_tone draft whose own evidence says
# nothing material happened must not persist as a pending alert.
# ---------------------------------------------------------------------------


def test_no_material_shifts_alert_not_persisted(
    db_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """LLM diff sets no_material_shifts_detected=true → the driver skips the
    persist (counted as no_material_skips), instead of parking a pending card
    whose memo says "nothing changed"."""
    conn = sqlite3.connect(str(db_path))
    try:
        _ = _seed_five_quarters(conn, ticker="MELI")
    finally:
        conn.close()

    monkeypatch.setattr(
        "triggers.earnings_tone.call_llm", _CountingLLM([_diff_payload(no_shifts=True)])
    )

    exit_code = run_triggers.main(
        [
            "--tickers",
            "MELI",
            "--db-path",
            str(db_path),
            "--max-cost-usd",
            "10",
        ]
    )
    assert exit_code == 0

    conn = sqlite3.connect(str(db_path))
    try:
        alert_count = conn.execute("SELECT COUNT(*) FROM alerts").fetchone()[0]
        action_count = conn.execute("SELECT COUNT(*) FROM queued_actions").fetchone()[0]
    finally:
        conn.close()
    assert alert_count == 0
    assert action_count == 0

    captured = capsys.readouterr()
    summary = json.loads(captured.out)
    assert summary["alerts_fired"] == 0
    assert summary["no_material_skips"] == 1
    assert '"event": "no_material_shifts_skip"' in captured.err


# ---------------------------------------------------------------------------
# 4. Per-ticker isolation
# ---------------------------------------------------------------------------


def test_per_ticker_isolation_continues_after_failure(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """LLM raises for ticker A; ticker B still persists its alert. Exit 0."""
    conn = sqlite3.connect(str(db_path))
    try:
        _ = _seed_five_quarters(conn, ticker="MELI")
        _ = _seed_five_quarters(conn, ticker="GOOG")
    finally:
        conn.close()

    monkeypatch.setattr(
        "triggers.earnings_tone.call_llm",
        _RaisingLLM(fail_ticker="MELI", response=_diff_payload()),
    )

    exit_code = run_triggers.main(
        [
            "--tickers",
            "MELI,GOOG",
            "--db-path",
            str(db_path),
            "--max-cost-usd",
            "10",
        ]
    )
    assert exit_code == 0

    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute("SELECT ticker FROM alerts").fetchall()
    finally:
        conn.close()
    tickers_with_alerts = [r[0] for r in rows]
    assert tickers_with_alerts == ["GOOG"]


# ---------------------------------------------------------------------------
# 5. Cost cap
# ---------------------------------------------------------------------------


def test_cost_cap_halts_cleanly(
    db_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Mocked cost query returning above-cap → driver halts with exit 0, no insert."""
    conn = sqlite3.connect(str(db_path))
    try:
        _ = _seed_five_quarters(conn, ticker="MELI")
    finally:
        conn.close()

    # If the LLM were called, this would assert; the cost cap should fire first.
    def _fail_llm(_prompt: str, **_kwargs: object) -> str:
        raise AssertionError("LLM should not be called once the cost cap is hit")

    monkeypatch.setattr("triggers.earnings_tone.call_llm", _fail_llm)

    def _over_cap(_db: Path, _ts: datetime) -> Decimal:
        return Decimal("11.00")

    monkeypatch.setattr("execution.run_triggers.query_run_cost_usd", _over_cap)

    exit_code = run_triggers.main(
        [
            "--tickers",
            "MELI",
            "--db-path",
            str(db_path),
            "--max-cost-usd",
            "10",
        ]
    )
    assert exit_code == 0

    conn = sqlite3.connect(str(db_path))
    try:
        alert_count = conn.execute("SELECT COUNT(*) FROM alerts").fetchone()[0]
    finally:
        conn.close()
    assert alert_count == 0

    summary = json.loads(capsys.readouterr().out)
    assert summary["cost_cap_reached"] is True
    assert summary["alerts_fired"] == 0


# ---------------------------------------------------------------------------
# 6. Idle trigger doesn't interfere with an active one
# ---------------------------------------------------------------------------


def test_idle_trigger_does_not_block_active_one(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """kpi_inflection finds no registered KPIs in this fixture, so scan→[] and
    it contributes nothing. The driver must tolerate an idle trigger: no crash,
    earnings_tone for the same ticker still fires normally.
    ``--triggers earnings_tone,kpi_inflection`` forces both onto the run.
    """
    conn = sqlite3.connect(str(db_path))
    try:
        _ = _seed_five_quarters(conn, ticker="MELI")
    finally:
        conn.close()

    monkeypatch.setattr("triggers.earnings_tone.call_llm", _CountingLLM([_diff_payload()]))

    exit_code = run_triggers.main(
        [
            "--tickers",
            "MELI",
            "--triggers",
            "earnings_tone,kpi_inflection",
            "--db-path",
            str(db_path),
            "--max-cost-usd",
            "10",
        ]
    )
    assert exit_code == 0

    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute("SELECT trigger_kind FROM alerts ORDER BY id").fetchall()
    finally:
        conn.close()
    kinds = [r[0] for r in rows]
    assert kinds == ["earnings_tone"]


# ---------------------------------------------------------------------------
# 7. No transcripts in window
# ---------------------------------------------------------------------------


def test_no_candidates_exits_cleanly(
    db_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A ticker with only stale transcripts → every default trigger scans to
    []; driver moves on. earnings_tone finds no fresh transcript,
    kpi_inflection finds no registered KPIs, and material_news finds no news
    table, so each ENABLED trigger records a no-candidate skip (one per trigger
    for the one ticker; saydo_due is not in the enabled set)."""
    now = datetime.now(UTC).replace(tzinfo=None)
    conn = sqlite3.connect(str(db_path))
    try:
        # 5-day-old transcript — outside the 24h arrival window.
        _ = _insert_transcript(
            conn,
            ticker="MELI",
            fiscal_period_type="Q4",
            period_end="2025-12-31",
            fetched_at=now - timedelta(days=5),
        )
    finally:
        conn.close()

    def _fail_llm(_prompt: str, **_kwargs: object) -> str:
        raise AssertionError("LLM should not be called when scan returns []")

    monkeypatch.setattr("triggers.earnings_tone.call_llm", _fail_llm)

    exit_code = run_triggers.main(
        [
            "--tickers",
            "MELI",
            "--db-path",
            str(db_path),
            "--max-cost-usd",
            "10",
        ]
    )
    assert exit_code == 0

    summary = json.loads(capsys.readouterr().out)
    assert summary["alerts_fired"] == 0
    # Every enabled trigger no-candidates on MELI (count tracks the registry,
    # so this holds regardless of which sensors are in the enabled set).
    assert summary["no_candidate_skips"] == len(run_triggers.ENABLED_TRIGGERS)
    assert summary["tickers_processed"] == 1


# ---------------------------------------------------------------------------
# 8. Empty portfolio
# ---------------------------------------------------------------------------


def test_empty_ticker_list_exits_cleanly(
    db_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """--tickers '' → driver does nothing and reports zero processed."""

    def _fail_llm(_prompt: str, **_kwargs: object) -> str:
        raise AssertionError("LLM should not be called for an empty portfolio")

    monkeypatch.setattr("triggers.earnings_tone.call_llm", _fail_llm)

    exit_code = run_triggers.main(
        [
            "--tickers",
            "",
            "--db-path",
            str(db_path),
            "--max-cost-usd",
            "10",
        ]
    )
    assert exit_code == 0

    summary = json.loads(capsys.readouterr().out)
    assert summary["tickers_processed"] == 0
    assert summary["alerts_fired"] == 0
    assert summary["errors"] == 0
