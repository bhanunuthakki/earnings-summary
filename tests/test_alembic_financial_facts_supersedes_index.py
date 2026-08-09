from __future__ import annotations

import sqlite3
from pathlib import Path

from alembic.config import Config

from alembic import command

ROOT = Path(__file__).resolve().parents[1]
INDEX = "ix_0270_financial_facts_supersedes_id"


def _config(path: Path) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path.as_posix()}")
    return config


def test_squashed_head_preserves_financial_fact_supersedes_lookup(tmp_path: Path) -> None:
    """The consolidated baseline retains the formerly-0270 query contract."""
    path = tmp_path / "financial-facts-index.db"
    command.upgrade(_config(path), "head")

    with sqlite3.connect(path) as conn:
        assert conn.execute(f"PRAGMA index_info('{INDEX}')").fetchall() == [
            (0, 12, "supersedes_id")
        ]
        plan = tuple(
            str(row[3])
            for row in conn.execute(
                "EXPLAIN QUERY PLAN SELECT id FROM financial_facts WHERE supersedes_id=?",
                (1,),
            )
        )
    assert any(INDEX in step for step in plan)
    assert not any(step == "SCAN financial_facts" for step in plan)
