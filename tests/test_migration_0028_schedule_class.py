"""Focused contract tests for the active schedule-class cleanup migration."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from alembic.config import Config

from alembic import command


def _config(db_path: Path) -> Config:
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    config.set_main_option("script_location", str(Path(__file__).resolve().parents[1] / "alembic"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{db_path.as_posix()}")
    return config


def test_0028_preserves_research_values_and_derives_downgrade_tiers(tmp_path: Path) -> None:
    db_path = tmp_path / "migration.db"
    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL);
            INSERT INTO alembic_version VALUES ('0027_add_sizing_intent_supersessions');
            CREATE TABLE tracked_companies (
                ticker TEXT, list_type TEXT, last_built_at TEXT, archived_at TEXT,
                processing_tier TEXT DEFAULT 'P3'
            );
                INSERT INTO tracked_companies VALUES
                    ('A', 'portfolio', NULL, NULL, 'P1'),
                    ('B', 'watchlist', NULL, NULL, 'P2'),
                    ('C', 'evaluation', NULL, NULL, 'P2'),
                    ('D', 'index_member', NULL, NULL, 'P3'),
                    ('E', 'none', NULL, NULL, 'P3');
            CREATE TABLE research_tasks (
                id INTEGER, cost_usd FLOAT, run_id TEXT
            );
            INSERT INTO research_tasks VALUES (1, 12.5, '{"run":"r1"}');
            CREATE INDEX idx_tracked_processing_tier
                ON tracked_companies(processing_tier,last_built_at);
            CREATE INDEX ix_tracked_companies_processing_tier
                ON tracked_companies(processing_tier);
            """
        )

    config = _config(db_path)
    command.upgrade(config, "head")
    with sqlite3.connect(db_path) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(tracked_companies)")}
        research = {row[1] for row in conn.execute("PRAGMA table_info(research_tasks)")}
        assert "processing_tier" not in columns
        assert {"estimated_cost_usd", "task_metadata_json"} <= research
        assert conn.execute(
            "SELECT estimated_cost_usd, task_metadata_json FROM research_tasks"
        ).fetchone() == (
            12.5,
            '{"run":"r1"}',
        )

    command.downgrade(config, "0027_add_sizing_intent_supersessions")
    with sqlite3.connect(db_path) as conn:
        assert {row[1] for row in conn.execute("PRAGMA table_info(research_tasks)")} >= {
            "cost_usd",
            "run_id",
        }
        assert conn.execute(
            "SELECT ticker, processing_tier FROM tracked_companies ORDER BY ticker"
        ).fetchall() == [
            ("A", "P1"),
            ("B", "P2"),
            ("C", "P2"),
            ("D", "P3"),
            ("E", "P3"),
        ]
