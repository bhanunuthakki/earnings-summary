"""Round-trip + enforcement tests for ``0071_new_table_check_constraints``.

Proves the two CHECK constraints the migration adds actually reject
out-of-vocabulary values in the running schema, and that downgrade removes them:

  * ``ir_fetch_status.last_status`` IN ('ok', 'failed', 'skipped')
  * ``saydo_historical_metrics.outcome`` IN ('hit', 'miss', 'beat', 'no_data')

The two tables originate at different revisions (0070 and b79cec08ce5b), so the
test creates them with their pre-0071 DDL, stamps the DB at 0070, and runs only
0071 — mirroring the stamp-at-prior-head pattern of the other migration tests.
The saydo index is asserted to survive the ``batch_alter_table`` recreation.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from alembic.config import Config

from alembic import command

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PRIOR_HEAD = "0070_ir_fetch_status"
NEW_HEAD = "0071_new_table_check_constraints"

_IR_DDL = """
CREATE TABLE ir_fetch_status (
    ticker TEXT PRIMARY KEY,
    last_attempt_at TEXT NOT NULL,
    last_status TEXT NOT NULL,
    discovered INTEGER,
    downloaded INTEGER,
    reason TEXT,
    updated_at TEXT NOT NULL
)
"""

_SAYDO_DDL = """
CREATE TABLE saydo_historical_metrics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker VARCHAR NOT NULL,
    period_made DATETIME NOT NULL,
    period_target DATETIME NOT NULL,
    kpi_name VARCHAR NOT NULL,
    comparator VARCHAR NOT NULL,
    target_value NUMERIC(24, 6) NOT NULL,
    realized_value NUMERIC(24, 6),
    outcome VARCHAR NOT NULL,
    guidance_narrative TEXT NOT NULL,
    realized_narrative TEXT,
    evaluated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""
_SAYDO_INDEX = (
    "CREATE INDEX ix_saydo_hist_ticker_target ON saydo_historical_metrics (ticker, period_target)"
)


def _build_config(db_path: Path) -> Config:
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return cfg


def _insert_ir(conn: sqlite3.Connection, last_status: str) -> None:
    conn.execute(
        "INSERT INTO ir_fetch_status "
        "(ticker, last_attempt_at, last_status, updated_at) VALUES (?, ?, ?, ?)",
        (f"T_{last_status}", "2026-06-04 00:00:00", last_status, "2026-06-04 00:00:00"),
    )
    conn.commit()


def _insert_saydo(conn: sqlite3.Connection, outcome: str) -> None:
    conn.execute(
        "INSERT INTO saydo_historical_metrics "
        "(ticker, period_made, period_target, kpi_name, comparator, target_value, "
        " outcome, guidance_narrative) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("NU", "2026-01-01 00:00:00", "2026-04-01 00:00:00", "ARPAC", ">=", "10.0", outcome, "n"),
    )
    conn.commit()


def _count(conn: sqlite3.Connection, table: str) -> int:
    row = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
    assert row is not None
    return int(row[0])


def _index_names(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(r[1]) for r in conn.execute(f"PRAGMA index_list({table})").fetchall()}


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    """Tmp DB with the two pre-0071 tables, stamped at 0070."""
    db = tmp_path / "m0071.db"
    conn = sqlite3.connect(str(db))
    try:
        conn.executescript(_IR_DDL)
        conn.executescript(_SAYDO_DDL)
        conn.execute(_SAYDO_INDEX)
        conn.commit()
    finally:
        conn.close()
    command.stamp(_build_config(db), PRIOR_HEAD)
    return db


def test_upgrade_adds_enforcing_check_constraints(db_path: Path) -> None:
    """After 0071, in-vocab values persist and out-of-vocab values are rejected
    by the DB itself (not just the Python writer)."""
    command.upgrade(_build_config(db_path), NEW_HEAD)
    conn = sqlite3.connect(str(db_path))
    try:
        # canonical vocabulary passes
        _insert_ir(conn, "ok")
        _insert_ir(conn, "failed")
        _insert_ir(conn, "skipped")
        _insert_saydo(conn, "hit")
        _insert_saydo(conn, "miss")
        _insert_saydo(conn, "beat")
        _insert_saydo(conn, "no_data")
        # out-of-vocabulary is rejected by the CHECK
        with pytest.raises(sqlite3.IntegrityError):
            _insert_ir(conn, "bogus")
        with pytest.raises(sqlite3.IntegrityError):
            _insert_saydo(conn, "BEAT")  # wrong case is still out-of-vocab
    finally:
        conn.close()


def test_saydo_index_survives_recreation(db_path: Path) -> None:
    """The ``batch_alter_table`` recreation must preserve the existing index."""
    command.upgrade(_build_config(db_path), NEW_HEAD)
    conn = sqlite3.connect(str(db_path))
    try:
        assert "ix_saydo_hist_ticker_target" in _index_names(conn, "saydo_historical_metrics")
    finally:
        conn.close()


def test_downgrade_removes_check_constraints(db_path: Path) -> None:
    """Downgrade to 0070 drops the CHECKs — the previously-rejected values persist."""
    cfg = _build_config(db_path)
    command.upgrade(cfg, NEW_HEAD)
    command.downgrade(cfg, PRIOR_HEAD)
    conn = sqlite3.connect(str(db_path))
    try:
        _insert_ir(conn, "bogus")
        _insert_saydo(conn, "BEAT")
        assert _count(conn, "ir_fetch_status") == 1
        assert _count(conn, "saydo_historical_metrics") == 1
    finally:
        conn.close()
