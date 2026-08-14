"""Migration contract for immutable owner-decision checkpoints."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from pathlib import Path

import pytest
from alembic.config import Config

from alembic import command

ROOT = Path(__file__).resolve().parents[1]
PRIOR_HEAD = "0016_add_ask_grounding_traces"
HEAD = "0017_add_owner_decision_checkpoints"


def _config(path: Path) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path.as_posix()}")
    return config


def test_0017_adds_collision_safe_append_only_checkpoint_tables(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    database = migrated_db(tmp_path / "checkpoint-migration.db", target=HEAD)
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (HEAD,)
        connection.execute(
            "INSERT INTO owner_decision_checkpoints"
            "(id,user_id,source_channel,source_event_id,checkpoint_schema_version,"
            "payload_sha256,payload_json,retrospective,created_at,confirmed_at) "
            "VALUES (1,'bhanu','claude_session','turn-1','owner-decision-checkpoint/v1',"
            "?,'{}',0,'2026-08-14','2026-08-14')",
            ("a" * 64,),
        )
        connection.commit()
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO owner_decision_checkpoints"
                "(user_id,source_channel,source_event_id,checkpoint_schema_version,"
                "payload_sha256,payload_json,retrospective,created_at,confirmed_at) "
                "VALUES ('bhanu','claude_session','turn-1','owner-decision-checkpoint/v1',"
                "?,'{}',0,'2026-08-14','2026-08-14')",
                ("b" * 64,),
            )
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute(
                "UPDATE owner_decision_checkpoints SET source_event_id='changed' WHERE id=1"
            )
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute("DELETE FROM owner_decision_checkpoints WHERE id=1")


def test_0016_downgrade_preserves_preexisting_decisions(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    database = migrated_db(tmp_path / "checkpoint-downgrade.db", target=HEAD)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO decisions"
            "(id,ticker,recommendation_kind,decided_by,scope,made_at,created_at) "
            "VALUES (135,'WIX','sell','owner','ticker','2026-08-14','2026-08-14')"
        )
        connection.commit()
    command.downgrade(_config(database), PRIOR_HEAD)

    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (
            PRIOR_HEAD,
        )
        assert connection.execute("SELECT ticker FROM decisions WHERE id=135").fetchone() == (
            "WIX",
        )
        names = {
            str(row[0])
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert "owner_decision_checkpoints" not in names
