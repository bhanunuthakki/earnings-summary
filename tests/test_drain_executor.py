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
import json
import os
import sqlite3
import sys
import time
from collections.abc import Callable
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


def _clear_all_obligations(db_path: Path) -> None:
    """Model a successful regenerator by clearing every current queued row."""
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            """
            UPDATE llm_artifacts
            SET dirty = 0, expires_at = ?
            WHERE superseded_by_id IS NULL
            """,
            ((datetime.now(UTC) + timedelta(days=30)).isoformat(),),
        )
        conn.commit()
    finally:
        conn.close()


def _write_valid_bear_cache(repo_root: Path) -> None:
    path = repo_root / "data" / "bear_case" / "ABNB.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "failure_modes": [
                    {
                        "hypothesis": "Demand slows",
                        "evidence_in_data": "Nights decelerate",
                        "leading_indicator": "Bookings",
                        "quantitative_impact": "Revenue below plan",
                        "refutation_criteria": "Bookings reaccelerate",
                    }
                ],
                "most_underweighted": "Competition",
                "out_of_scope_flags": [],
            }
        ),
        encoding="utf-8",
    )
    refreshed_at = time.time() + 0.01
    os.utime(path, (refreshed_at, refreshed_at))


class _FakeCompleted:
    """Stand-in for subprocess.CompletedProcess. Only the fields the executor
    actually reads (returncode, stderr) are populated."""

    def __init__(self, returncode: int, stderr: str) -> None:
        self.returncode = returncode
        self.stdout = ""
        self.stderr = stderr


class _CapturingFakeRun:
    """Records subprocess.run calls; returns a configurable CompletedProcess."""

    def __init__(
        self,
        returncode: int = 0,
        stderr: str = "",
        on_run: Callable[[], None] | None = None,
    ) -> None:
        self.calls: list[list[str]] = []
        self._returncode = returncode
        self._stderr = stderr
        self._on_run = on_run

    def __call__(self, argv: list[str], **kwargs: object) -> _FakeCompleted:
        self.calls.append(list(argv))
        if self._on_run is not None:
            self._on_run()
        return _FakeCompleted(returncode=self._returncode, stderr=self._stderr)


def _dict_log_events(caplog: pytest.LogCaptureFixture) -> list[dict[str, object]]:
    events: list[dict[str, object]] = []
    for record in caplog.records:
        if isinstance(record.msg, dict):
            events.append(cast("dict[str, object]", record.msg))
    return events


def _projection_ok(*args: object, **kwargs: object) -> None:
    del args, kwargs


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

    fake_run = _CapturingFakeRun(returncode=0, on_run=lambda: _clear_all_obligations(db_path))
    monkeypatch.setattr(executor.subprocess, "run", fake_run)
    monkeypatch.setattr(executor, "_project_native_job", _projection_ok)
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
        "--regenerate-purpose",
        "bear_case",
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
    """Multiple rows for the same exact ticker/purpose invoke one subprocess."""
    repo_root = tmp_path
    (repo_root / "data").mkdir()
    db_path = repo_root / "data" / "portfolio.db"
    conn = _make_portfolio_db(db_path)
    try:
        _insert_dirty(conn, ticker="NU", purpose="saydo_filter")
        _insert_dirty(conn, ticker="NU", purpose="saydo_filter")
    finally:
        conn.close()

    fake_run = _CapturingFakeRun(returncode=0, on_run=lambda: _clear_all_obligations(db_path))
    monkeypatch.setattr(executor.subprocess, "run", fake_run)
    monkeypatch.setattr(executor, "_project_native_job", _projection_ok)
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
        "--regenerate-purpose",
        "saydo_filter",
    ]


# ---------------------------------------------------------------------------
# Cost-cap halt
# ---------------------------------------------------------------------------


def test_bear_case_regenerator_bypasses_the_on_disk_cache(executor: Any) -> None:
    """Dirty report work must force only its exact native-purpose cache."""
    argv = executor._PURPOSE_TO_REGENERATOR["bear_case"]
    assert argv[-2:] == ["--regenerate-purpose", "bear_case"]


@pytest.mark.parametrize(
    "purpose",
    ["bear_case", "exec_comp_alignment", "qa_topics", "saydo_filter", "valuation_basis"],
)
def test_report_regenerator_names_the_exact_native_purpose(executor: Any, purpose: str) -> None:
    argv = executor._PURPOSE_TO_REGENERATOR[purpose]
    assert argv[-2:] == ["--regenerate-purpose", purpose]


def test_zero_exit_without_clearing_obligation_fails_closed(
    executor: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A child process exit code is not proof that the dirty row was refreshed."""
    repo_root = tmp_path
    (repo_root / "data").mkdir()
    db_path = repo_root / "data" / "portfolio.db"
    conn = _make_portfolio_db(db_path)
    try:
        _insert_dirty(conn, ticker="ABNB", purpose="bear_case")
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

    assert rc == 1
    receipt = capsys.readouterr().out
    assert '"status": "partial_failure"' in receipt
    assert '"failed": 1' in receipt
    assert '"no_progress": 1' in receipt
    events = _dict_log_events(caplog)
    assert any(event.get("event") == "drain_no_progress" for event in events)


def test_zero_exit_projects_a_fresh_typed_native_cache_before_progress_check(
    executor: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root = tmp_path
    (repo_root / "data").mkdir()
    db_path = repo_root / "data" / "portfolio.db"
    conn = _make_portfolio_db(db_path)
    try:
        _insert_dirty(conn, ticker="ABNB", purpose="bear_case")
        old_id = int(conn.execute("SELECT id FROM llm_artifacts").fetchone()[0])
    finally:
        conn.close()

    fake_run = _CapturingFakeRun(
        returncode=0,
        on_run=lambda: _write_valid_bear_cache(repo_root),
    )
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

    assert executor.main() == 0
    conn = sqlite3.connect(db_path)
    try:
        old = conn.execute(
            "SELECT superseded_by_id FROM llm_artifacts WHERE id = ?", (old_id,)
        ).fetchone()
        current = conn.execute(
            """
            SELECT purpose, dirty, superseded_by_id
            FROM llm_artifacts WHERE id = ?
            """,
            (old[0],),
        ).fetchone()
    finally:
        conn.close()
    assert old[0] is not None
    assert current == ("bear_case", 0, None)


def test_native_projection_failure_cannot_be_hidden_by_clearing_the_old_row(
    executor: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root = tmp_path
    (repo_root / "data").mkdir()
    db_path = repo_root / "data" / "portfolio.db"
    conn = _make_portfolio_db(db_path)
    try:
        _insert_dirty(conn, ticker="ABNB", purpose="bear_case")
    finally:
        conn.close()

    fake_run = _CapturingFakeRun(
        returncode=0,
        on_run=lambda: _clear_all_obligations(db_path),
    )
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

    assert executor.main() == 1


def test_duplicate_native_obligations_share_one_producer_and_one_successor(
    executor: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root = tmp_path
    (repo_root / "data").mkdir()
    db_path = repo_root / "data" / "portfolio.db"
    conn = _make_portfolio_db(db_path)
    try:
        _insert_dirty(conn, ticker="ABNB", purpose="bear_case")
        _insert_dirty(conn, ticker="ABNB", purpose="bear_case")
        predecessor_ids = [
            int(row[0])
            for row in conn.execute("SELECT id FROM llm_artifacts ORDER BY id").fetchall()
        ]
    finally:
        conn.close()

    fake_run = _CapturingFakeRun(
        returncode=0,
        on_run=lambda: _write_valid_bear_cache(repo_root),
    )
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

    assert executor.main() == 0
    assert len(fake_run.calls) == 1
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT superseded_by_id FROM llm_artifacts WHERE id IN (?, ?)",
            predecessor_ids,
        ).fetchall()
    finally:
        conn.close()
    assert all(row[0] is not None for row in rows)
    successors = {int(row[0]) for row in rows}
    assert len(successors) == 1


@pytest.mark.parametrize("failure_mode", ["stderr", "exception"])
def test_child_failure_text_is_redacted_before_structured_logging(
    executor: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    failure_mode: str,
) -> None:
    """API keys, bearer tokens, and signed URLs never survive child failures."""
    repo_root = tmp_path
    (repo_root / "data").mkdir()
    db_path = repo_root / "data" / "portfolio.db"
    conn = _make_portfolio_db(db_path)
    try:
        _insert_dirty(conn, ticker="ABNB", purpose="bear_case")
    finally:
        conn.close()

    secret_text = (
        "https://example.test/report?apikey=plain-secret&X-Amz-Signature=signed-secret "
        "Authorization: Bearer bearer-secret"
    )
    if failure_mode == "stderr":
        runner: object = _CapturingFakeRun(returncode=1, stderr=secret_text)
    else:

        def _raise_spawn_error(argv: list[str], **kwargs: object) -> _FakeCompleted:
            del argv, kwargs
            raise OSError(secret_text)

        runner = _raise_spawn_error
    monkeypatch.setattr(executor.subprocess, "run", runner)
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
    serialized_events = str(_dict_log_events(caplog))
    assert "plain-secret" not in serialized_events
    assert "signed-secret" not in serialized_events
    assert "bearer-secret" not in serialized_events
    assert "example.test" in serialized_events


def test_cost_ledger_error_halts_before_child_and_is_redacted(
    executor: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Spend cannot fail open when the ledger query is unavailable."""
    repo_root = tmp_path
    (repo_root / "data").mkdir()
    db_path = repo_root / "data" / "portfolio.db"
    conn = _make_portfolio_db(db_path)
    try:
        _insert_dirty(conn, ticker="ABNB", purpose="bear_case")
    finally:
        conn.close()

    secret_text = (
        "https://ledger.test/query?api_key=ledger-secret&X-Amz-Signature=signed-secret "
        "Bearer ledger-bearer"
    )

    def _raise_ledger_error(*args: object, **kwargs: object) -> sqlite3.Connection:
        del args, kwargs
        raise sqlite3.OperationalError(secret_text)

    fake_run = _CapturingFakeRun(returncode=0)
    monkeypatch.setattr(executor, "connect_sqlite", _raise_ledger_error)
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

    captured = capsys.readouterr().out
    serialized = captured + str(_dict_log_events(caplog))
    assert rc == 1
    assert fake_run.calls == []
    assert '"status": "blocked_cost_ledger"' in captured
    assert "ledger-secret" not in serialized
    assert "signed-secret" not in serialized
    assert "ledger-bearer" not in serialized
    assert "ledger.test" in serialized


def test_deduped_job_requires_every_original_obligation_to_progress(
    executor: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """One shared child cannot hide a second exact-row obligation."""
    repo_root = tmp_path
    (repo_root / "data").mkdir()
    db_path = repo_root / "data" / "portfolio.db"
    conn = _make_portfolio_db(db_path)
    try:
        _insert_dirty(conn, ticker="NU", purpose="saydo_filter")
        _insert_dirty(conn, ticker="NU", purpose="saydo_filter")
        unresolved_id = int(
            conn.execute(
                "SELECT MAX(id) FROM llm_artifacts WHERE purpose = 'saydo_filter'"
            ).fetchone()[0]
        )
    finally:
        conn.close()

    def _clear_only_first_row() -> None:
        update_conn = sqlite3.connect(str(db_path))
        try:
            update_conn.execute(
                "UPDATE llm_artifacts SET dirty = 0 WHERE id = (SELECT MIN(id) FROM llm_artifacts)"
            )
            update_conn.commit()
        finally:
            update_conn.close()

    fake_run = _CapturingFakeRun(returncode=0, on_run=_clear_only_first_row)
    monkeypatch.setattr(executor.subprocess, "run", fake_run)
    monkeypatch.setattr(executor, "_project_native_job", _projection_ok)
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
    assert len(fake_run.calls) == 1
    events = _dict_log_events(caplog)
    no_progress = [event for event in events if event.get("event") == "drain_no_progress"]
    assert len(no_progress) == 1
    assert no_progress[0]["unresolved_artifact_ids"] == [unresolved_id]


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
    def _always_over(db_path: Path, since: datetime) -> object:
        del db_path, since
        return executor._CostAvailable(10.0)

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

    result = executor._accrued_cost_usd(db_path, since=cutoff)
    assert isinstance(result, executor._CostAvailable)
    assert result.cost_usd == pytest.approx(2.00)


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

    def _next_cost(db_path: Path, since: datetime) -> object:
        del db_path, since
        return executor._CostAvailable(next(cost_returns))

    monkeypatch.setattr(executor, "_accrued_cost_usd", _next_cost)

    fake_run = _CapturingFakeRun(returncode=0, on_run=lambda: _clear_all_obligations(db_path))
    monkeypatch.setattr(executor.subprocess, "run", fake_run)
    monkeypatch.setattr(executor, "_project_native_job", _projection_ok)
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

    fake_run = _CapturingFakeRun(returncode=0, on_run=lambda: _clear_all_obligations(db_path))
    monkeypatch.setattr(executor.subprocess, "run", fake_run)
    monkeypatch.setattr(executor, "_project_native_job", _projection_ok)
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
        "--regenerate-purpose",
        "bear_case",
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

    fake_run = _CapturingFakeRun(returncode=0, on_run=lambda: _clear_all_obligations(db_path))
    monkeypatch.setattr(executor.subprocess, "run", fake_run)
    monkeypatch.setattr(executor, "_project_native_job", _projection_ok)
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


def test_expiry_equal_to_progress_check_time_remains_unresolved(
    executor: Any,
    tmp_path: Path,
) -> None:
    """An expiry must be strictly later than the check time to prove refresh."""
    db_path = tmp_path / "portfolio.db"
    conn = _make_portfolio_db(db_path)
    queued_at = datetime(2026, 8, 11, 12, 0, tzinfo=UTC)
    checked_at = queued_at + timedelta(minutes=1)
    try:
        _insert_dirty(
            conn,
            ticker="WIX",
            purpose="bear_case",
            dirty=0,
            expires_at=(queued_at - timedelta(days=1)).isoformat(),
        )
        artifact_id = int(conn.execute("SELECT id FROM llm_artifacts").fetchone()[0])
    finally:
        conn.close()

    artifacts = executor.drain_dirty(db_path=db_path, now=queued_at)
    jobs = executor._build_pending_jobs(artifacts, queued_at=queued_at)
    update_conn = sqlite3.connect(str(db_path))
    try:
        update_conn.execute(
            "UPDATE llm_artifacts SET expires_at = ? WHERE id = ?",
            (checked_at.isoformat(), artifact_id),
        )
        update_conn.commit()
    finally:
        update_conn.close()

    progress = executor._check_job_progress(jobs[0], db_path=db_path, checked_at=checked_at)
    assert progress.satisfied is False
    assert progress.unresolved_artifact_ids == (artifact_id,)


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
