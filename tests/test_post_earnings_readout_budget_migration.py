"""Current-chain contract for the post-earnings readout budget."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from alembic.config import Config

from alembic import command

ROOT = Path(__file__).resolve().parents[1]


def _config(path: Path) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path.as_posix()}")
    return config


def test_current_head_seeds_skip_mode_budget_and_downgrade_preserves_it(
    tmp_path: Path,
) -> None:
    db = tmp_path / "readout-budget.db"
    config = _config(db)
    command.upgrade(config, "head")

    with sqlite3.connect(db) as conn:
        revision = conn.execute("SELECT version_num FROM alembic_version").fetchone()
        row = conn.execute(
            "SELECT monthly_cap_usd, warn_threshold_pct, hard_block, on_exceed "
            "FROM llm_budgets WHERE purpose='post_earnings_readout'"
        ).fetchone()
    assert revision == ("0018_add_transcript_acquisition_receipts",)
    assert row == (5, 0.80, 0, "skip")

    command.downgrade(config, "0002_drop_dead_tables")
    with sqlite3.connect(db) as conn:
        assert conn.execute(
            "SELECT monthly_cap_usd, on_exceed FROM llm_budgets "
            "WHERE purpose='post_earnings_readout'"
        ).fetchone() == (5, "skip")
