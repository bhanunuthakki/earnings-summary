"""Round-trip test for the ``0108_qualitative_conditions`` migration (L9 PR2).

Adds two columns to ``decisions``. Mirrors the repo's alembic-test pattern
(``_signals_fixtures``): hand-create the minimal ``decisions`` table, stamp at
the prior head, run the one migration — running the full chain from base is not
possible here (``0000_baseline`` assumes ``src/db.py:init_db()`` already built
the core schema). Proves upgrade adds the columns, and downgrade drops them.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from alembic.config import Config

from alembic import command

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PRIOR_HEAD = "0108_restatement_trigger_kind"
HEAD = "0109_qualitative_conditions"
_NEW_COLS = {"qualitative_conditions", "qualitative_conditions_extracted_at"}

# Minimal pre-0108 decisions table (the columns the migration's guard inspects).
_DECISIONS_DDL = (
    "CREATE TABLE decisions ("
    "id INTEGER PRIMARY KEY AUTOINCREMENT, ticker TEXT NOT NULL, "
    "recommendation_kind TEXT NOT NULL, made_at TEXT NOT NULL, "
    "decision_conditions TEXT, conditions_extracted_at TEXT)"
)


def _config(db_path: Path) -> Config:
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return cfg


def _decision_columns(db_path: Path) -> set[str]:
    conn = sqlite3.connect(str(db_path))
    try:
        return {r[1] for r in conn.execute("PRAGMA table_info(decisions)").fetchall()}
    finally:
        conn.close()


def _seed(db_path: Path) -> Config:
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(_DECISIONS_DDL)
        conn.commit()
    finally:
        conn.close()
    cfg = _config(db_path)
    command.stamp(cfg, PRIOR_HEAD)
    return cfg


def test_upgrade_adds_columns(tmp_path: Path) -> None:
    db = tmp_path / "m.db"
    cfg = _seed(db)
    assert not (_NEW_COLS & _decision_columns(db))
    command.upgrade(cfg, HEAD)
    assert _decision_columns(db) >= _NEW_COLS


def test_downgrade_drops_columns(tmp_path: Path) -> None:
    db = tmp_path / "d.db"
    cfg = _seed(db)
    command.upgrade(cfg, HEAD)
    assert _decision_columns(db) >= _NEW_COLS
    command.downgrade(cfg, PRIOR_HEAD)
    assert not (_NEW_COLS & _decision_columns(db))
