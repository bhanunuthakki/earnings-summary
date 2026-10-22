"""Migration lifecycle for the governed Ask proposal authority."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
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


def test_upgrade_adds_authority_and_downgrade_preserves_proposals(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    path = migrated_db(tmp_path / "proposal-approval.db", target=REVISION)
    config = _config(path)

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


OWNER_PRIOR = "0059_scoped_research_universe_commitments"
OWNER_REVISION = "0060_owner_governed_proposals"


def _proposal_triggers(path: Path) -> list[tuple[str, str]]:
    with sqlite3.connect(path) as connection:
        return connection.execute(
            "SELECT name,sql FROM sqlite_master WHERE type='trigger' "
            "AND name LIKE 'trg_research_proposal_%' ORDER BY name"
        ).fetchall()


def test_owner_authority_round_trip_and_downgrade_refusal(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    prior_triggers = _proposal_triggers(
        migrated_db(tmp_path / "owner-prior.db", target=OWNER_PRIOR)
    )

    path = migrated_db(tmp_path / "owner-current.db", target=OWNER_REVISION)
    with sqlite3.connect(path) as connection:
        columns = {
            str(row[1]) for row in connection.execute("PRAGMA table_info(research_proposals)")
        }
        assert "owner_authorization_id" in columns
        assert connection.execute("PRAGMA foreign_key_list(research_proposals)").fetchall() == []
    command.downgrade(_config(path), OWNER_PRIOR)
    assert _proposal_triggers(path) == prior_triggers
    with sqlite3.connect(path) as connection:
        columns = {
            str(row[1]) for row in connection.execute("PRAGMA table_info(research_proposals)")
        }
    assert "owner_authorization_id" not in columns

    held = migrated_db(tmp_path / "owner-held.db", target=OWNER_REVISION)
    with sqlite3.connect(held) as connection:
        connection.execute(
            "INSERT INTO owner_proposal_authorizations (authorization_id,ticker,user_id,"
            "statement,source_ref,evidence_json,evidence_sha256,canonical_content_sha256,"
            "created_at) VALUES ('owner-1','NU','bhanu','approved','test',?,?,?,'2026-10-22')",
            (f'[{{"path":"a","sha256":"{"0" * 64}"}}]', "1" * 64, "2" * 64),
        )
    try:
        command.downgrade(_config(held), OWNER_PRIOR)
    except RuntimeError as exc:
        assert "owner" in str(exc)
    else:
        raise AssertionError("downgrade must refuse while owner authorizations exist")
