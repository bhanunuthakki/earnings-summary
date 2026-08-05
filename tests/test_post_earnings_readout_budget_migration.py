"""Round-trip test for the post-earnings readout purpose budget."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from alembic.config import Config

from alembic import command

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PRIOR_HEAD = "0272_archive_generation_catalog"
HEAD = "0273_post_earnings_readout_budget"

_PRE_DDL = """
CREATE TABLE llm_budgets (
    purpose TEXT PRIMARY KEY,
    monthly_cap_usd REAL NOT NULL,
    warn_threshold_pct REAL NOT NULL DEFAULT 0.80,
    hard_block INTEGER NOT NULL DEFAULT 0,
    on_exceed TEXT NOT NULL DEFAULT 'warn',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    notes TEXT
);
"""


def _config(db_path: Path) -> Config:
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return cfg


def _migrated_db(tmp_path: Path) -> Path:
    db = tmp_path / "readout-budget.db"
    conn = sqlite3.connect(db)
    try:
        conn.executescript(_PRE_DDL)
        conn.execute(
            "INSERT INTO llm_budgets "
            "(purpose, monthly_cap_usd, on_exceed, created_at, updated_at) "
            "VALUES ('pre_earnings_brief', 5.0, 'skip', '2026-08-04', '2026-08-04')"
        )
        conn.commit()
    finally:
        conn.close()
    cfg = _config(db)
    command.stamp(cfg, PRIOR_HEAD)
    command.upgrade(cfg, HEAD)
    return db


def test_upgrade_seeds_skip_mode_budget_and_downgrade_is_scoped(tmp_path: Path) -> None:
    db = _migrated_db(tmp_path)
    conn = sqlite3.connect(db)
    try:
        row = conn.execute(
            "SELECT monthly_cap_usd, warn_threshold_pct, hard_block, on_exceed "
            "FROM llm_budgets WHERE purpose = 'post_earnings_readout'"
        ).fetchone()
        assert row == (5.0, 0.80, 0, "skip")
    finally:
        conn.close()

    command.downgrade(_config(db), PRIOR_HEAD)
    conn = sqlite3.connect(db)
    try:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM llm_budgets WHERE purpose = 'post_earnings_readout'"
            ).fetchone()[0]
            == 0
        )
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM llm_budgets WHERE purpose = 'pre_earnings_brief'"
            ).fetchone()[0]
            == 1
        )
    finally:
        conn.close()
