"""Migration lifecycle for the durable FMP recovery substrate."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
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


def _seed_sentinel(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE preserved_fmp_corpus (id INTEGER PRIMARY KEY, note TEXT)")
        connection.execute("INSERT INTO preserved_fmp_corpus(note) VALUES ('keep-me')")


def test_upgrade_constraints_and_downgrade_preserve_existing_schema(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    path = migrated_db(
        tmp_path / "fmp-recovery-migration.db",
        upgrade_from="0007_add_earnings_surprise_observations",
        before_upgrade=_seed_sentinel,
        target=REVISION,
    )
    config = _config(path)
    expected = {
        "provider_circuit_state",
        "fmp_work_backlog",
        "fmp_work_attempts",
        "fmp_recovery_events",
    }
    with sqlite3.connect(path) as connection:
        tables = {
            str(row[0])
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert expected <= tables
        assert connection.execute("SELECT note FROM preserved_fmp_corpus").fetchone() == (
            "keep-me",
        )
        attempt_columns = {
            str(row[1]) for row in connection.execute("PRAGMA table_info(fmp_work_attempts)")
        }
        assert {
            "resolution_source",
            "resolution_policy_sha256",
            "coverage_proof_sha256",
            "evidence_ids_json",
            "fact_ids_json",
            "corpus_content_sha256",
            "fmp_snapshot_captured_at",
            "resolution_endpoint_key",
            "resolution_period_key",
            "resolution_concept_keys_json",
            "resolution_evidence_fresh_at",
            "resolution_source_authorized",
            "resolution_has_disagreement",
        } <= attempt_columns
        assert (
            not {
                "raw_body",
                "response_body",
                "exception_text",
                "api_key",
                "secret",
                "url",
            }
            & attempt_columns
        )

    command.downgrade(config, "0007_add_earnings_surprise_observations")
    with sqlite3.connect(path) as connection:
        remaining = {
            str(row[0])
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert not expected & remaining
        assert connection.execute("SELECT note FROM preserved_fmp_corpus").fetchone() == (
            "keep-me",
        )
