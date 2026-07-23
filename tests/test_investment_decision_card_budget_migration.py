"""Round-trip test for ``0190_investment_decision_card_budget``.

Mirrors the repo alembic-test pattern (tests/test_decisions_advice_link_migration.py):
hand-create the minimal ``llm_budgets`` shape, stamp the prior head, run the
one migration. Proves:

- the ``investment_decision_card`` llm_budgets row is seeded ($8/mo,
  warn_threshold_pct=0.80, on_exceed='skip'), idempotently (re-running is a
  no-op via ``ON CONFLICT(purpose) DO NOTHING``)
- downgrade removes exactly that row and nothing else
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from alembic.config import Config

from alembic import command

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PRIOR_HEAD = "0189_merge_0188_heads"
HEAD = "0190_investment_decision_card_budget"

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


def _migrated_db(tmp_path: Path, *, name: str = "m.db") -> Path:
    db = tmp_path / name
    conn = sqlite3.connect(str(db))
    try:
        conn.executescript(_PRE_DDL)
        conn.execute(
            "INSERT INTO llm_budgets (purpose, monthly_cap_usd, on_exceed, created_at, updated_at) "
            "VALUES ('incremental_dollar_recommendation', 10.0, 'block', '2026-07-01', '2026-07-01')"
        )
        conn.commit()
    finally:
        conn.close()
    cfg = _config(db)
    command.stamp(cfg, PRIOR_HEAD)
    command.upgrade(cfg, HEAD)
    return db


def test_upgrade_seeds_budget_row(tmp_path: Path) -> None:
    db = _migrated_db(tmp_path)
    conn = sqlite3.connect(str(db))
    try:
        row = conn.execute(
            "SELECT monthly_cap_usd, warn_threshold_pct, hard_block, on_exceed "
            "FROM llm_budgets WHERE purpose = 'investment_decision_card'"
        ).fetchone()
        assert row == (8.0, 0.80, 0, "skip")
        # Untouched sibling row.
        other = conn.execute(
            "SELECT on_exceed FROM llm_budgets WHERE purpose = 'incremental_dollar_recommendation'"
        ).fetchone()
        assert other == ("block",)
    finally:
        conn.close()


def test_upgrade_is_idempotent(tmp_path: Path) -> None:
    db = _migrated_db(tmp_path)
    cfg = _config(db)
    # Re-running upgrade against the already-migrated DB must not raise or
    # duplicate the row (ON CONFLICT DO NOTHING).
    command.upgrade(cfg, HEAD)
    conn = sqlite3.connect(str(db))
    try:
        n = conn.execute(
            "SELECT COUNT(*) FROM llm_budgets WHERE purpose = 'investment_decision_card'"
        ).fetchone()[0]
        assert n == 1
    finally:
        conn.close()


def test_downgrade_removes_only_its_row(tmp_path: Path) -> None:
    db = _migrated_db(tmp_path)
    cfg = _config(db)
    command.downgrade(cfg, PRIOR_HEAD)
    conn = sqlite3.connect(str(db))
    try:
        gone = conn.execute(
            "SELECT COUNT(*) FROM llm_budgets WHERE purpose = 'investment_decision_card'"
        ).fetchone()[0]
        assert gone == 0
        kept = conn.execute(
            "SELECT COUNT(*) FROM llm_budgets WHERE purpose = 'incremental_dollar_recommendation'"
        ).fetchone()[0]
        assert kept == 1
    finally:
        conn.close()
