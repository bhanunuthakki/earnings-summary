"""Round-trip test for the post-earnings readout purpose budget."""

from __future__ import annotations

import runpy
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import cast

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

PROJECT_ROOT = Path(__file__).resolve().parents[1]
_MIGRATION = runpy.run_path(
    str(PROJECT_ROOT / "alembic" / "versions" / "0273_post_earnings_readout_budget.py")
)
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


def _invoke_migration(db: Path, operation: str) -> None:
    migration_fn = cast("Callable[[], None]", _MIGRATION[operation])
    engine = sa.create_engine(f"sqlite:///{db}")
    try:
        with engine.begin() as connection:
            context = MigrationContext.configure(connection)
            with Operations.context(context):
                migration_fn()
    finally:
        engine.dispose()


def test_upgrade_seeds_skip_mode_budget_and_downgrade_is_scoped(tmp_path: Path) -> None:
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

    _invoke_migration(db, "upgrade")
    conn = sqlite3.connect(db)
    try:
        row = conn.execute(
            "SELECT monthly_cap_usd, warn_threshold_pct, hard_block, on_exceed "
            "FROM llm_budgets WHERE purpose = 'post_earnings_readout'"
        ).fetchone()
        assert row == (5.0, 0.80, 0, "skip")
    finally:
        conn.close()

    _invoke_migration(db, "downgrade")
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
