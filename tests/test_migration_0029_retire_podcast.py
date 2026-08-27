"""The active baseline and upgrade path retire the podcast LLM budget."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from pathlib import Path

from alembic.config import Config

from alembic import command

HEAD = "0029_retire_podcast_prototype"
PRIOR = "0028_remove_processing_tier_and_rename_research_tasks"


def _config(db_path: Path) -> Config:
    root = Path(__file__).resolve().parents[1]
    cfg = Config(str(root / "alembic.ini"))
    cfg.set_main_option("script_location", str(root / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path.as_posix()}")
    return cfg


def test_upgrade_removes_budget_and_downgrade_restores_it(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    db_path = migrated_db(tmp_path / "podcast-retirement.db", target=HEAD)
    with sqlite3.connect(db_path) as conn:
        assert (
            conn.execute(
                "SELECT 1 FROM llm_budgets WHERE purpose='podcast_takeaway_summary'"
            ).fetchone()
            is None
        )

    cfg = _config(db_path)
    downgrade = getattr(command, "downgrade")
    upgrade = getattr(command, "upgrade")
    downgrade(cfg, PRIOR)
    with sqlite3.connect(db_path) as conn:
        assert conn.execute(
            "SELECT monthly_cap_usd, on_exceed FROM llm_budgets "
            "WHERE purpose='podcast_takeaway_summary'"
        ).fetchone() == (5, "skip")

    upgrade(cfg, HEAD)
    with sqlite3.connect(db_path) as conn:
        assert (
            conn.execute(
                "SELECT 1 FROM llm_budgets WHERE purpose='podcast_takeaway_summary'"
            ).fetchone()
            is None
        )
