"""Tests for the --gate predicate of execution/run_validation_engine.py.

``--gate`` makes the post-hoc validation engine enforceable: a cron / CI step
exits non-zero when a run produced HALT-severity issues. The engine's checks are
population-level (they compare across rows/sources), so this gate-on-output is
the safe enforcement form — a per-row insert gate is not structurally possible.
"""

from __future__ import annotations

import sqlite3
import sys
from datetime import datetime
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "execution"))

import run_validation_engine as rve  # noqa: E402

_CREATE = """
CREATE TABLE validation_issues (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    source_doc_id INTEGER,
    ticker TEXT,
    severity TEXT NOT NULL,
    rule TEXT,
    raw_value TEXT,
    expected TEXT,
    raised_at TEXT,
    resolved_at TEXT
)
"""


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute(_CREATE)
    return conn


def test_halt_issue_count_scopes_to_run_and_severity() -> None:
    conn = _conn()
    conn.executemany(
        "INSERT INTO validation_issues (run_id, severity, rule) VALUES (?, ?, ?)",
        [
            ("r1", "halt", "plausible_range"),
            ("r1", "warn", "plausible_range"),  # warn must not count
            ("r1", "halt", "magnitude_jump"),
            ("r2", "halt", "plausible_range"),  # other run must not count
        ],
    )
    conn.commit()
    assert rve.halt_issue_count(conn, "r1") == 2
    assert rve.halt_issue_count(conn, "r2") == 1
    assert rve.halt_issue_count(conn, "r3") == 0


def test_gate_failure_exit_is_distinct_non_one() -> None:
    # A distinct non-1 exit so a scheduler tells "data failed validation" from
    # "the script crashed".
    assert rve.GATE_FAILURE_EXIT == 2


def test_db_path_is_accepted_as_alias_for_db() -> None:
    """The morning-pipeline orchestrator forwards --db-path to every stage; the
    validation engine must accept it as an alias for --db (both land on .db)."""
    parser = rve.build_parser()
    assert parser.parse_args(["--db-path", "/tmp/x.db"]).db == "/tmp/x.db"
    assert parser.parse_args(["--db", "/tmp/y.db"]).db == "/tmp/y.db"
    # --gate stays a flag and defaults off.
    assert parser.parse_args([]).gate is False
    assert parser.parse_args(["--gate"]).gate is True


def test_gate_fails_closed_on_empty_owner_scope(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from pipeline.validation_engine import ValidationReport, check_kpi_semantic_coverage

    conn = _conn()

    def _open_db(_db: object) -> sqlite3.Connection:
        return conn

    def _start_run(*_args: object, **_kwargs: object) -> str:
        return "empty-owner"

    def _end_run(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(rve, "open_db", _open_db)
    monkeypatch.setattr(rve, "start_run", _start_run)
    monkeypatch.setattr(rve, "end_run", _end_run)

    def _empty_owner_report(
        connection: sqlite3.Connection,
        *,
        run_id: str,
        ticker: str | None,
        user_id: str,
    ) -> ValidationReport:
        started = datetime.now()
        outcome = check_kpi_semantic_coverage(
            connection,
            run_id=run_id,
            ticker=ticker,
            user_id=user_id,
        )
        return ValidationReport(
            run_id=run_id,
            started_at=started,
            ended_at=datetime.now(),
            outcomes=(outcome,),
        )

    monkeypatch.setattr(rve, "run_all_checks", _empty_owner_report)
    monkeypatch.setattr(
        sys,
        "argv",
        ["run_validation_engine.py", "--gate", "--user-id", "unknown"],
    )

    assert rve.main() == rve.GATE_FAILURE_EXIT
    captured = capsys.readouterr()
    assert "GATE FAILED: 1 HALT-severity" in captured.err
    assert '"user_id": "unknown"' in captured.out
