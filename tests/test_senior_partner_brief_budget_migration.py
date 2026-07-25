"""Round-trip test for ``0201_senior_partner_brief_budget``.

Mirrors tests/test_investment_decision_card_budget_migration.py: hand-create
the minimal ``llm_budgets`` shape, stamp the prior head, run the one
migration. Proves:

- the ``senior_partner_brief`` llm_budgets row is seeded ($6/mo,
  warn_threshold_pct=0.80, on_exceed='block'), idempotently (re-running is a
  no-op via ``ON CONFLICT(purpose) DO NOTHING``)
- downgrade removes exactly that row and nothing else

Numbered 0199 (not 0198 — see the migration file's own docstring): PR #989
independently claimed 0198 off the same 0197 parent while this branch was in
flight.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from alembic.config import Config

from alembic import command

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PRIOR_HEAD = "0197_decision_drafts"
HEAD = "0201_senior_partner_brief_budget"

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
            "VALUES ('investment_decision_card', 8.0, 'skip', '2026-07-23', '2026-07-23')"
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
            "FROM llm_budgets WHERE purpose = 'senior_partner_brief'"
        ).fetchone()
        assert row == (6.0, 0.80, 0, "block")
        # Untouched sibling row.
        other = conn.execute(
            "SELECT on_exceed FROM llm_budgets WHERE purpose = 'investment_decision_card'"
        ).fetchone()
        assert other == ("skip",)
    finally:
        conn.close()


def test_upgrade_is_idempotent(tmp_path: Path) -> None:
    db = _migrated_db(tmp_path)
    cfg = _config(db)
    command.upgrade(cfg, HEAD)
    conn = sqlite3.connect(str(db))
    try:
        n = conn.execute(
            "SELECT COUNT(*) FROM llm_budgets WHERE purpose = 'senior_partner_brief'"
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
            "SELECT COUNT(*) FROM llm_budgets WHERE purpose = 'senior_partner_brief'"
        ).fetchone()[0]
        assert gone == 0
        kept = conn.execute(
            "SELECT COUNT(*) FROM llm_budgets WHERE purpose = 'investment_decision_card'"
        ).fetchone()[0]
        assert kept == 1
    finally:
        conn.close()
