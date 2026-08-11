"""Tests for execution/refresh_dirty_artifacts.py --execute mode.

Covers the executor wiring that operationalizes the TTL policy:
  * default (no --execute) prints a manifest and never invokes subprocess
  * --execute fires the right argv per (ticker, purpose) and dedupes shared CLIs
  * --max-cost-usd halts gracefully (exit 0) once the ledger cost crosses
    the cap, leaving the rest of the queue unprocessed
  * empty queue exits cleanly with a "fresh pipeline" message
  * unmapped purposes log a warning and are skipped rather than crashing

Subprocess invocations are mocked; the LLM cost query is exercised against a
real sqlite DB so the SQL stays honest.
"""

from __future__ import annotations

import importlib.util
import sqlite3
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _load_module() -> Any:
    """Load execution/refresh_dirty_artifacts.py as a module (not on package path)."""
    src = PROJECT_ROOT / "execution" / "refresh_dirty_artifacts.py"
    spec = importlib.util.spec_from_file_location("refresh_dirty_artifacts", src)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["refresh_dirty_artifacts"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def executor() -> Any:
    return _load_module()


def _make_portfolio_db(db_path: Path) -> sqlite3.Connection:
    """Build an llm_artifacts + llm_calls schema mirroring migrations 0034/0035/0043.

    Returns an open connection the caller must close. Sufficient column set for
    the executor's two SQL queries (dirty-breakdown + cost-sum).
    """
    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        """
        CREATE TABLE llm_artifacts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT,
            scope TEXT NOT NULL DEFAULT 'ticker',
            purpose TEXT NOT NULL,
            fiscal_period TEXT,
            content_md TEXT,
            content_json TEXT,
            input_sha256 TEXT NOT NULL,
            output_sha256 TEXT,
            model TEXT,
            prompt_version TEXT NOT NULL DEFAULT 'v1',
            generated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            expires_at TIMESTAMP,
            superseded_by_id INTEGER,
            dirty INTEGER NOT NULL DEFAULT 0,
            dirty_reason TEXT,
            source_doc_ids TEXT,
            parent_artifact_ids TEXT,
            llm_call_id INTEGER
        );
        CREATE TABLE llm_calls (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            called_at DATETIME NOT NULL,
            purpose VARCHAR(64),
            ticker VARCHAR(16),
            scope VARCHAR(64),
            model VARCHAR(64) NOT NULL,
            prompt_sha256 VARCHAR(64) NOT NULL,
            response_sha256 VARCHAR(64),
            prompt_chars INTEGER NOT NULL,
            response_chars INTEGER,
            input_tokens INTEGER,
            cache_creation_input_tokens INTEGER,
            cache_read_input_tokens INTEGER,
            output_tokens INTEGER,
            elapsed_ms INTEGER NOT NULL,
            cost_estimate_usd FLOAT,
            cache_hit BOOLEAN NOT NULL DEFAULT 0,
            fallback_used VARCHAR(16),
            artifact_id INTEGER,
            error TEXT,
            run_id VARCHAR(64)
        );
        """
    )
    conn.commit()
    return conn


def _insert_dirty(
    conn: sqlite3.Connection,
    *,
    ticker: str,
    purpose: str,
    dirty: int = 1,
    expires_at: str | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO llm_artifacts (ticker, scope, purpose, input_sha256,
                                   prompt_version, dirty, expires_at)
        VALUES (?, 'ticker', ?, 'sha', 'v1', ?, ?)
        """,
        (ticker, purpose, dirty, expires_at),
    )
    conn.commit()


def _insert_llm_call(conn: sqlite3.Connection, *, called_at: datetime, cost_usd: float) -> None:
    conn.execute(
        """
        INSERT INTO llm_calls (called_at, model, prompt_sha256, prompt_chars,
                               elapsed_ms, cost_estimate_usd)
        VALUES (?, 'claude-sonnet-4-6', '0', 100, 1000, ?)
        """,
        (called_at.isoformat(), cost_usd),
    )
    conn.commit()


class _FakeCompleted:
    """Stand-in for subprocess.CompletedProcess. Only the fields the executor
    actually reads (returncode, stderr) are populated."""

    def __init__(self, returncode: int, stderr: str) -> None:
        self.returncode = returncode
        self.stdout = ""
        self.stderr = stderr


class _CapturingFakeRun:
    """Records subprocess.run calls; returns a configurable CompletedProcess."""

    def __init__(self, returncode: int = 0, stderr: str = "") -> None:
        self.calls: list[list[str]] = []
        self._returncode = returncode
        self._stderr = stderr

    def __call__(self, argv: list[str], **kwargs: object) -> _FakeCompleted:
        self.calls.append(list(argv))
        return _FakeCompleted(returncode=self._returncode, stderr=self._stderr)


# ---------------------------------------------------------------------------
# Default mode (manifest only) — no subprocess invocation
# ---------------------------------------------------------------------------


def test_default_mode_does_not_invoke_subprocess(
    executor: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without --execute, drain prints manifest and exits 0; subprocess.run
    must not be called regardless of how many dirty artifacts exist."""
    repo_root = tmp_path
    (repo_root / "data").mkdir()
    db_path = repo_root / "data" / "portfolio.db"
    conn = _make_portfolio_db(db_path)
    try:
        _insert_dirty(conn, ticker="GOOG", purpose="bear_case")
        _insert_dirty(conn, ticker="META", purpose="qa_topics")
    finally:
        conn.close()

    fake_run = _CapturingFakeRun()
    monkeypatch.setattr(executor.subprocess, "run", fake_run)
    monkeypatch.setattr(sys, "argv", ["refresh_dirty_artifacts.py", "--repo-root", str(repo_root)])

    rc = executor.main()
    assert rc == 0
    assert fake_run.calls == [], "manifest-only mode must not spawn subprocesses"


# ---------------------------------------------------------------------------
# --execute mode invokes the correct subprocess per (ticker, purpose)
# ---------------------------------------------------------------------------


def test_execute_invokes_correct_subprocess(
    executor: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--execute must spawn the per-purpose regenerator with the substituted
    ticker. company_description has its own extractor; bear_case folds into
    build_artifacts. Both should fire exactly once."""
    repo_root = tmp_path
    (repo_root / "data").mkdir()
    db_path = repo_root / "data" / "portfolio.db"
    conn = _make_portfolio_db(db_path)
    try:
        _insert_dirty(conn, ticker="GOOG", purpose="bear_case")
        _insert_dirty(conn, ticker="META", purpose="company_description")
    finally:
        conn.close()

    fake_run = _CapturingFakeRun(returncode=0)
    monkeypatch.setattr(executor.subprocess, "run", fake_run)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "refresh_dirty_artifacts.py",
            "--repo-root",
            str(repo_root),
            "--execute",
            "--max-cost-usd",
            "100",
        ],
    )

    rc = executor.main()
    assert rc == 0
    assert len(fake_run.calls) == 2

    argvs = {tuple(c) for c in fake_run.calls}
    expected_bear = (
        sys.executable,
        str(repo_root / "execution" / "sqlite_bootstrap.py"),
        str(repo_root / "execution" / "build_artifacts.py"),
        "--ticker",
        "GOOG",
        "--enable-llm",
    )
    expected_desc = (
        sys.executable,
        str(repo_root / "execution" / "sqlite_bootstrap.py"),
        str(repo_root / "execution" / "extract_company_description.py"),
        "--ticker",
        "META",
        "--refresh",
    )
    assert expected_bear in argvs
    assert expected_desc in argvs


def test_execute_dedupes_shared_cli_for_one_ticker(
    executor: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Multiple dirty purposes for the same ticker that map to the SAME CLI
    (e.g. bear_case + qa_topics both go through build_artifacts --enable-llm)
    should invoke that subprocess exactly once."""
    repo_root = tmp_path
    (repo_root / "data").mkdir()
    db_path = repo_root / "data" / "portfolio.db"
    conn = _make_portfolio_db(db_path)
    try:
        _insert_dirty(conn, ticker="NU", purpose="bear_case")
        _insert_dirty(conn, ticker="NU", purpose="qa_topics")
        _insert_dirty(conn, ticker="NU", purpose="saydo_filter")
    finally:
        conn.close()

    fake_run = _CapturingFakeRun(returncode=0)
    monkeypatch.setattr(executor.subprocess, "run", fake_run)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "refresh_dirty_artifacts.py",
            "--repo-root",
            str(repo_root),
            "--execute",
            "--max-cost-usd",
            "100",
        ],
    )

    rc = executor.main()
    assert rc == 0
    assert len(fake_run.calls) == 1
    assert fake_run.calls[0] == [
        sys.executable,
        str(repo_root / "execution" / "sqlite_bootstrap.py"),
        str(repo_root / "execution" / "build_artifacts.py"),
        "--ticker",
        "NU",
        "--enable-llm",
    ]


# ---------------------------------------------------------------------------
# Cost-cap halt
# ---------------------------------------------------------------------------


def test_execute_halts_at_cost_cap(
    executor: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the cost-query returns a value above --max-cost-usd at the moment
    the executor checks (before each job), the drain halts gracefully without
    spawning further subprocesses. Exit 0 — this is a budget guardrail, not
    an error."""
    repo_root = tmp_path
    (repo_root / "data").mkdir()
    db_path = repo_root / "data" / "portfolio.db"
    conn = _make_portfolio_db(db_path)
    try:
        _insert_dirty(conn, ticker="GOOG", purpose="bear_case")
        _insert_dirty(conn, ticker="META", purpose="bear_case")
        _insert_dirty(conn, ticker="NU", purpose="bear_case")
    finally:
        conn.close()

    # Pretend the ledger already shows $10 of spend in this run window — well
    # over the $5 cap. The first cost check should fire, see 10 >= 5, halt.
    def _always_over(db_path: Path, since: datetime) -> float:
        return 10.0

    monkeypatch.setattr(executor, "_accrued_cost_usd", _always_over)

    fake_run = _CapturingFakeRun(returncode=0)
    monkeypatch.setattr(executor.subprocess, "run", fake_run)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "refresh_dirty_artifacts.py",
            "--repo-root",
            str(repo_root),
            "--execute",
            "--max-cost-usd",
            "5",
        ],
    )

    rc = executor.main()
    assert rc == 0
    assert fake_run.calls == [], (
        "cost cap must short-circuit BEFORE the first subprocess invocation"
    )


def test_accrued_cost_usd_sums_ledger_rows_since_window(executor: Any, tmp_path: Path) -> None:
    """The cost-query helper must SUM cost_estimate_usd for rows whose
    called_at >= since. Older rows are excluded; newer rows accrue."""
    repo_root = tmp_path
    (repo_root / "data").mkdir()
    db_path = repo_root / "data" / "portfolio.db"
    conn = _make_portfolio_db(db_path)
    try:
        cutoff = datetime(2026, 5, 27, 12, 0, 0, tzinfo=UTC)
        _insert_llm_call(conn, called_at=cutoff - timedelta(hours=1), cost_usd=99.0)
        _insert_llm_call(conn, called_at=cutoff + timedelta(minutes=1), cost_usd=1.25)
        _insert_llm_call(conn, called_at=cutoff + timedelta(minutes=5), cost_usd=0.75)
    finally:
        conn.close()

    assert executor._accrued_cost_usd(db_path, since=cutoff) == pytest.approx(2.00)


def test_cost_check_runs_before_each_job(
    executor: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The cap is consulted per-job, so a queue that goes overbudget mid-drain
    halts at the next iteration boundary — not after the entire queue runs.

    We simulate this by patching `_accrued_cost_usd` to return $0 on the first
    call and $6 on the second; the cap is $5, so exactly one subprocess fires.
    """
    repo_root = tmp_path
    (repo_root / "data").mkdir()
    db_path = repo_root / "data" / "portfolio.db"
    conn = _make_portfolio_db(db_path)
    try:
        _insert_dirty(conn, ticker="GOOG", purpose="bear_case")
        _insert_dirty(conn, ticker="META", purpose="company_description")
        _insert_dirty(conn, ticker="NU", purpose="filing_intelligence")
    finally:
        conn.close()

    cost_returns = iter([0.0, 6.0, 6.0])

    def _next_cost(db_path: Path, since: datetime) -> float:
        return next(cost_returns)

    monkeypatch.setattr(executor, "_accrued_cost_usd", _next_cost)

    fake_run = _CapturingFakeRun(returncode=0)
    monkeypatch.setattr(executor.subprocess, "run", fake_run)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "refresh_dirty_artifacts.py",
            "--repo-root",
            str(repo_root),
            "--execute",
            "--max-cost-usd",
            "5",
        ],
    )

    rc = executor.main()
    assert rc == 0
    assert len(fake_run.calls) == 1, (
        "exactly one subprocess should fire before the cost-check returns $6"
    )


# ---------------------------------------------------------------------------
# Empty queue
# ---------------------------------------------------------------------------


def test_empty_queue_exits_cleanly(
    executor: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """No dirty/expired rows → 'No dirty artifacts. Pipeline is fresh.' + exit 0.
    Subprocess.run must never be called."""
    repo_root = tmp_path
    (repo_root / "data").mkdir()
    db_path = repo_root / "data" / "portfolio.db"
    conn = _make_portfolio_db(db_path)
    conn.close()

    fake_run = _CapturingFakeRun()
    monkeypatch.setattr(executor.subprocess, "run", fake_run)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "refresh_dirty_artifacts.py",
            "--repo-root",
            str(repo_root),
            "--execute",
            "--max-cost-usd",
            "5",
        ],
    )

    rc = executor.main()
    captured = capsys.readouterr()
    assert rc == 0
    assert "No dirty artifacts" in captured.out
    assert fake_run.calls == []


# ---------------------------------------------------------------------------
# Unmapped purpose
# ---------------------------------------------------------------------------


def test_unmapped_purpose_logs_warning_and_skips(
    executor: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A purpose not in _PURPOSE_TO_REGENERATOR must not crash the drain.
    The executor logs a 'no_regenerator' warning, skips the unmapped artifact,
    and continues with mapped ones."""
    repo_root = tmp_path
    (repo_root / "data").mkdir()
    db_path = repo_root / "data" / "portfolio.db"
    conn = _make_portfolio_db(db_path)
    try:
        _insert_dirty(conn, ticker="GOOG", purpose="not_a_real_purpose")
        _insert_dirty(conn, ticker="META", purpose="bear_case")
    finally:
        conn.close()

    fake_run = _CapturingFakeRun(returncode=0)
    monkeypatch.setattr(executor.subprocess, "run", fake_run)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "refresh_dirty_artifacts.py",
            "--repo-root",
            str(repo_root),
            "--execute",
            "--max-cost-usd",
            "100",
        ],
    )

    with caplog.at_level("WARNING", logger="refresh_dirty_artifacts"):
        rc = executor.main()

    assert rc == 0
    assert len(fake_run.calls) == 1, (
        "the mapped artifact (META bear_case) should still be processed"
    )
    assert fake_run.calls[0] == [
        sys.executable,
        str(repo_root / "execution" / "sqlite_bootstrap.py"),
        str(repo_root / "execution" / "build_artifacts.py"),
        "--ticker",
        "META",
        "--enable-llm",
    ]
    no_regen_warnings = [
        r
        for r in caplog.records
        if isinstance(r.msg, dict) and r.msg.get("event") == "no_regenerator"
    ]
    assert no_regen_warnings, "expected a no_regenerator warning for the unmapped purpose"


def test_daily_scan_purpose_classified_not_warned(
    executor: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A trigger/news purpose (earnings_tone_diff) has no standalone drain
    regenerator BY DESIGN — it is recomputed as a side effect of the daily
    trigger scan, and running the trigger here would also fire alerts. The drain
    must classify it as 'refreshed_by_daily_scan' (info), NOT 'no_regenerator'
    (warning), and must never subprocess it."""
    repo_root = tmp_path
    (repo_root / "data").mkdir()
    db_path = repo_root / "data" / "portfolio.db"
    conn = _make_portfolio_db(db_path)
    try:
        _insert_dirty(conn, ticker="GOOG", purpose="earnings_tone_diff")
    finally:
        conn.close()

    fake_run = _CapturingFakeRun(returncode=0)
    monkeypatch.setattr(executor.subprocess, "run", fake_run)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "refresh_dirty_artifacts.py",
            "--repo-root",
            str(repo_root),
            "--execute",
            "--max-cost-usd",
            "100",
        ],
    )

    with caplog.at_level("INFO", logger="refresh_dirty_artifacts"):
        rc = executor.main()

    assert rc == 0
    assert fake_run.calls == [], "a daily-scan purpose must not be subprocessed by the drain"
    events: list[str | None] = []
    for record in caplog.records:
        if isinstance(record.msg, dict):
            event = cast("dict[str, object]", record.msg).get("event")
            events.append(event if isinstance(event, str) else None)
    assert "refreshed_by_daily_scan" in events
    assert "no_regenerator" not in events


# ---------------------------------------------------------------------------
# Expired-but-not-dirty artifacts ride the same drain
# ---------------------------------------------------------------------------


def test_expired_artifact_drives_execute_path(
    executor: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An artifact with dirty=0 but expires_at in the past should be drained.
    This is the load-bearing case: the whole point of --execute is to act on
    TTL expiry, not just trigger-flipped dirtiness."""
    repo_root = tmp_path
    (repo_root / "data").mkdir()
    db_path = repo_root / "data" / "portfolio.db"
    conn = _make_portfolio_db(db_path)
    try:
        past = (datetime.now(UTC) - timedelta(days=1)).isoformat()
        _insert_dirty(conn, ticker="WIX", purpose="bear_case", dirty=0, expires_at=past)
    finally:
        conn.close()

    fake_run = _CapturingFakeRun(returncode=0)
    monkeypatch.setattr(executor.subprocess, "run", fake_run)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "refresh_dirty_artifacts.py",
            "--repo-root",
            str(repo_root),
            "--execute",
            "--max-cost-usd",
            "100",
        ],
    )

    rc = executor.main()
    assert rc == 0
    assert len(fake_run.calls) == 1
    assert "WIX" in fake_run.calls[0]


# ---------------------------------------------------------------------------
# Subprocess failure does not abort the drain
# ---------------------------------------------------------------------------


def test_subprocess_failure_continues_drain(
    executor: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """One regenerator returning non-zero must not poison the queue. The
    drain logs the failure and proceeds to the next job."""
    repo_root = tmp_path
    (repo_root / "data").mkdir()
    db_path = repo_root / "data" / "portfolio.db"
    conn = _make_portfolio_db(db_path)
    try:
        _insert_dirty(conn, ticker="GOOG", purpose="bear_case")
        _insert_dirty(conn, ticker="META", purpose="company_description")
    finally:
        conn.close()

    fake_run = _CapturingFakeRun(returncode=1, stderr="exploded")
    monkeypatch.setattr(executor.subprocess, "run", fake_run)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "refresh_dirty_artifacts.py",
            "--repo-root",
            str(repo_root),
            "--execute",
            "--max-cost-usd",
            "100",
        ],
    )

    with caplog.at_level("WARNING", logger="refresh_dirty_artifacts"):
        rc = executor.main()

    assert rc == 1
    assert len(fake_run.calls) == 2
    receipt = capsys.readouterr().out
    assert '"status": "partial_failure"' in receipt
    assert '"failed": 2' in receipt
    failures = [
        r
        for r in caplog.records
        if isinstance(r.msg, dict) and r.msg.get("event") == "drain_subprocess_failed"
    ]
    assert len(failures) == 2
