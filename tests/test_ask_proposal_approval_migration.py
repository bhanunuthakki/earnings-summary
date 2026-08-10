"""Migration lifecycle for the governed Ask proposal authority."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from alembic.config import Config

from alembic import command

ROOT = Path(__file__).resolve().parents[1]
REVISION = "0006_add_ask_proposal_approval"


def _config(path: Path) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path.as_posix()}")
    return config


def test_upgrade_adds_authority_and_downgrade_preserves_proposals(tmp_path: Path) -> None:
    path = tmp_path / "proposal-approval.db"
    config = _config(path)
    command.upgrade(config, "head")
    command.upgrade(config, "head")

    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (
            REVISION,
        )
        columns = {
            str(row[1]) for row in connection.execute("PRAGMA table_info(research_proposals)")
        }
        tables = {
            str(row[0])
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        triggers = {
            str(row[0])
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='trigger'")
        }
        indexes = {
            str(row[0])
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='index'")
        }
    assert {
        "canonical_content_json",
        "canonical_content_sha256",
        "proposal_revision",
        "target_precondition_sha256",
        "target_postcondition_sha256",
        "ask_exchange_request_id",
        "actionable_at",
        "invalidated_at",
        "invalidation_reason",
    } <= columns
    assert "research_proposal_decision_receipts" in tables
    assert {
        "trg_research_proposal_canonical_content_immutable",
        "trg_research_proposal_governed_status_cas",
        "trg_ask_exchange_invalidate_governed_proposals",
    } <= triggers
    assert "ix_research_proposals_ask_exchange" in indexes

    command.downgrade(config, "0005_add_ask_exchange_store")
    with sqlite3.connect(path) as connection:
        columns = {
            str(row[1]) for row in connection.execute("PRAGMA table_info(research_proposals)")
        }
        tables = {
            str(row[0])
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    assert "research_proposals" in tables
    assert "research_proposal_decision_receipts" not in tables
    assert "canonical_content_json" not in columns
