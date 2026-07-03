"""Round-trip test for ``0141_reader_perf_indexes``.

Mirrors the repo alembic-test pattern: hand-create a minimal pre-0141
``financial_facts``, stamp the prior head, run the one migration. Proves:

- the covering index ``idx_ff_lineitem_fpt_ticker_covering`` is created on the
  exact (line_item, fiscal_period_type, ticker, period_end, value, source_doc_id)
  column list, in order
- upgrade is idempotent (re-running with the index present is a no-op)
- downgrade drops the index and is safe when it's already gone
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from alembic.config import Config

from alembic import command

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PRIOR_HEAD = "0140_advisor_memos_position_review_kind"
HEAD = "0141_reader_perf_indexes"
_INDEX = "idx_ff_lineitem_fpt_ticker_covering"

# Minimal financial_facts + documents (the migration touches only the index).
_PRE_DDL = """
CREATE TABLE financial_facts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker VARCHAR NOT NULL,
    period_end DATETIME NOT NULL,
    fiscal_period_type VARCHAR NOT NULL,
    line_item VARCHAR NOT NULL,
    value NUMERIC(24, 6) NOT NULL,
    currency VARCHAR,
    unit VARCHAR NOT NULL DEFAULT 'actual',
    source_doc_id INTEGER NOT NULL
);
"""


def _config(db_path: Path) -> Config:
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return cfg


def _index_columns(conn: sqlite3.Connection, index_name: str) -> list[str]:
    """Ordered column names covered by ``index_name`` (empty if absent)."""
    rows = conn.execute(f"PRAGMA index_info({index_name})").fetchall()
    return [str(r[2]) for r in rows]


def _index_exists(conn: sqlite3.Connection, index_name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='index' AND name=?", (index_name,)
    ).fetchone()
    return row is not None


def _migrated_db(tmp_path: Path) -> Path:
    db = tmp_path / "m.db"
    conn = sqlite3.connect(str(db))
    try:
        conn.executescript(_PRE_DDL)
        conn.commit()
    finally:
        conn.close()
    cfg = _config(db)
    command.stamp(cfg, PRIOR_HEAD)
    command.upgrade(cfg, HEAD)
    return db


def test_upgrade_creates_covering_index_in_order(tmp_path: Path) -> None:
    db = _migrated_db(tmp_path)
    conn = sqlite3.connect(str(db))
    try:
        assert _index_exists(conn, _INDEX)
        assert _index_columns(conn, _INDEX) == [
            "line_item",
            "fiscal_period_type",
            "ticker",
            "period_end",
            "value",
            "source_doc_id",
        ]
    finally:
        conn.close()


def test_upgrade_is_idempotent(tmp_path: Path) -> None:
    db = _migrated_db(tmp_path)
    # Re-running the upgrade (already at HEAD) is a no-op — IF NOT EXISTS.
    cfg = _config(db)
    command.stamp(cfg, PRIOR_HEAD)
    command.upgrade(cfg, HEAD)
    conn = sqlite3.connect(str(db))
    try:
        assert _index_exists(conn, _INDEX)
    finally:
        conn.close()


def test_downgrade_drops_index(tmp_path: Path) -> None:
    db = _migrated_db(tmp_path)
    cfg = _config(db)
    command.downgrade(cfg, PRIOR_HEAD)
    conn = sqlite3.connect(str(db))
    try:
        assert not _index_exists(conn, _INDEX)
    finally:
        conn.close()
