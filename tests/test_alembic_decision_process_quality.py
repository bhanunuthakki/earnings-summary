"""Round-trip test for ``0114_decision_process_quality`` (Track B seam 8).

Adds the nullable ``process_quality`` column to ``decisions``. Mirrors the
repo's alembic-test pattern: hand-create a minimal pre-0114 ``decisions`` table,
stamp the prior head, run the one migration. Proves the column lands (nullable,
existing rows read NULL) and downgrade drops it cleanly.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from alembic.config import Config

from alembic import command

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PRIOR_HEAD = "0113_kpi_definition_origin"
HEAD = "0114_decision_process_quality"

_DECISIONS_DDL = """
CREATE TABLE decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker VARCHAR(16) NOT NULL,
    recommendation_kind VARCHAR(32) NOT NULL,
    made_at DATETIME NOT NULL,
    outcome_label VARCHAR(16),
    created_at DATETIME NOT NULL
);
"""


def _config(db_path: Path) -> Config:
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return cfg


def _columns(db_path: Path) -> set[str]:
    conn = sqlite3.connect(str(db_path))
    try:
        return {r[1] for r in conn.execute("PRAGMA table_info(decisions)").fetchall()}
    finally:
        conn.close()


def _seed(db_path: Path) -> Config:
    conn = sqlite3.connect(str(db_path))
    try:
        conn.executescript(_DECISIONS_DDL)
        conn.execute(
            "INSERT INTO decisions(ticker, recommendation_kind, made_at, created_at) "
            "VALUES ('NU', 'add', '2026-06-14', '2026-06-14')"
        )
        conn.commit()
    finally:
        conn.close()
    cfg = _config(db_path)
    command.stamp(cfg, PRIOR_HEAD)
    return cfg


def test_upgrade_adds_process_quality(tmp_path: Path) -> None:
    db = tmp_path / "m.db"
    cfg = _seed(db)
    assert "process_quality" not in _columns(db)
    command.upgrade(cfg, HEAD)
    assert "process_quality" in _columns(db)
    conn = sqlite3.connect(str(db))
    try:
        # Existing rows read NULL; the column accepts the rubric labels.
        assert conn.execute("SELECT process_quality FROM decisions").fetchone()[0] is None
        conn.execute("UPDATE decisions SET process_quality = 'lucky' WHERE ticker = 'NU'")
        conn.commit()
        assert conn.execute("SELECT process_quality FROM decisions").fetchone()[0] == "lucky"
    finally:
        conn.close()


def test_downgrade_drops_process_quality(tmp_path: Path) -> None:
    db = tmp_path / "d.db"
    cfg = _seed(db)
    command.upgrade(cfg, HEAD)
    assert "process_quality" in _columns(db)
    command.downgrade(cfg, PRIOR_HEAD)
    assert "process_quality" not in _columns(db)
