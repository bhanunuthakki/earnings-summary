"""Migration lifecycle for durable Ask exchange orchestration."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from alembic.config import Config

from alembic import command

ROOT = Path(__file__).resolve().parents[1]
REVISION = "0008_add_fmp_recovery"


def _config(path: Path) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path.as_posix()}")
    return config


def test_upgrade_is_idempotent_and_downgrade_removes_only_exchange_tables(
    tmp_path: Path,
) -> None:
    path = tmp_path / "ask-exchange-migration.db"
    config = _config(path)
    command.upgrade(config, "head")
    # The migration itself must also tolerate an interrupted runner retry.
    command.upgrade(config, "head")

    with sqlite3.connect(path) as connection:
        revision = connection.execute("SELECT version_num FROM alembic_version").fetchone()
        tables = {
            str(row[0])
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        indexes = {
            str(row[0])
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='index'")
        }
        foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchall()

    assert revision == (REVISION,)
    assert {"ask_session_contexts", "ask_exchanges", "ask_exchange_artifacts"} <= tables
    assert {
        "uq_ask_exchanges_one_pending_per_session",
        "ix_ask_exchanges_session_created",
        "ix_ask_exchanges_status_updated",
    } <= indexes
    assert foreign_keys == []

    command.downgrade(config, "0004_add_llm_circuit_breakers")
    with sqlite3.connect(path) as connection:
        remaining = {
            str(row[0])
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert {
            "ask_session_contexts",
            "ask_exchanges",
            "ask_exchange_artifacts",
        }.isdisjoint(remaining)
        assert "ask_sessions" in remaining
