"""Tests for the --gate predicate of execution/run_validation_engine.py.

``--gate`` makes the post-hoc validation engine enforceable: a cron / CI step
exits non-zero when a run produced HALT-severity issues. The engine's checks are
population-level (they compare across rows/sources), so this gate-on-output is
the safe enforcement form — a per-row insert gate is not structurally possible.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

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
