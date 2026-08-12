"""Migration lifecycle for owner-governed IR document approval."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from pathlib import Path

import pytest
from alembic.config import Config

from alembic import command

ROOT = Path(__file__).resolve().parents[1]
REVISION = "0009_add_ir_approval_store"


def _config(path: Path) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path.as_posix()}")
    return config


def _seed_sentinel(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE preserved_owner_notes (id INTEGER PRIMARY KEY, note TEXT)")
        connection.execute("INSERT INTO preserved_owner_notes(note) VALUES ('keep-me')")


def test_upgrade_adds_immutable_ir_approval_ledgers_and_downgrades_cleanly(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    path = migrated_db(
        tmp_path / "ir-approval-migration.db",
        upgrade_from="0008_add_fmp_recovery",
        before_upgrade=_seed_sentinel,
        target=REVISION,
    )
    config = _config(path)

    with sqlite3.connect(path) as connection:
        tables = {
            str(row[0])
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        triggers = {
            str(row[0])
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='trigger'")
        }
        assert {"ir_approval_candidates", "ir_approval_decisions"} <= tables
        assert {
            "trg_ir_approval_candidates_no_update",
            "trg_ir_approval_candidates_no_delete",
            "trg_ir_approval_decisions_no_update",
            "trg_ir_approval_decisions_no_delete",
        } <= triggers
        assert connection.execute("SELECT note FROM preserved_owner_notes").fetchone() == (
            "keep-me",
        )

        connection.execute(
            """
            INSERT INTO ir_approval_candidates (
                candidate_id,request_id,request_sha256,issuer_id,ticker,
                catalog_sha256,issuer_policy_sha256,authority_url,quarter_end,
                title,candidate_url,disposition,doc_type,observation_key,
                observation_raw_sha256,evidence_locator,recorded_by,recorded_at,reason,evidence_json,
                evidence_sha256
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "a" * 64,
                "candidate-request-1",
                "b" * 64,
                "sec-cik-0001943896",
                "RBRK",
                "c" * 64,
                "d" * 64,
                "https://ir.rubrik.com/financials/quarterly-results/default.aspx",
                "2026-07-31",
                "Q2 presentation",
                "https://ir.rubrik.com/static-files/q2.pdf",
                "ir_document",
                "ir_presentation",
                "rbrk-q2",
                "9" * 64,
                "row > presentation",
                "owner@example.test",
                "2026-08-12T10:00:00",
                "candidate review",
                "[]",
                "e" * 64,
            ),
        )
        with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
            connection.execute(
                """
                INSERT INTO ir_approval_candidates (
                    candidate_id,request_id,request_sha256,issuer_id,ticker,
                    catalog_sha256,issuer_policy_sha256,authority_url,quarter_end,
                    title,candidate_url,disposition,doc_type,observation_key,
                    observation_raw_sha256,evidence_locator,recorded_by,recorded_at,reason,
                    evidence_json,evidence_sha256
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    "a" + "Z" * 63,
                    "candidate-request-invalid-hex",
                    "b" * 64,
                    "sec-cik-0001943896",
                    "RBRK",
                    "1" * 64,
                    "d" * 64,
                    "https://ir.rubrik.com/financials/quarterly-results/default.aspx",
                    "2026-04-30",
                    "Q1 presentation",
                    "https://ir.rubrik.com/static-files/q1.pdf",
                    "ir_document",
                    "ir_presentation",
                    "rbrk-q1",
                    "8" * 64,
                    "row > presentation",
                    "owner@example.test",
                    "2026-08-12T10:01:00",
                    "candidate review",
                    "[]",
                    "e" * 64,
                ),
            )
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(
                "UPDATE ir_approval_candidates SET title='changed' WHERE candidate_id=?",
                ("a" * 64,),
            )
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(
                "DELETE FROM ir_approval_candidates WHERE candidate_id=?",
                ("a" * 64,),
            )

    command.downgrade(config, "0008_add_fmp_recovery")
    with sqlite3.connect(path) as connection:
        remaining = {
            str(row[0])
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert not {"ir_approval_candidates", "ir_approval_decisions"} & remaining
        assert connection.execute("SELECT note FROM preserved_owner_notes").fetchone() == (
            "keep-me",
        )
