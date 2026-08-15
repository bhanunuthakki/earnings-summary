"""Migration contract for append-only transcript acquisition receipts."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from pathlib import Path

import pytest
from alembic.config import Config

from alembic import command

ROOT = Path(__file__).resolve().parents[1]
PRIOR_HEAD = "0017_add_owner_decision_checkpoints"
HEAD = "0018_add_transcript_acquisition_receipts"


def _config(path: Path) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path.as_posix()}")
    return config


def test_0018_receipts_are_append_only_and_projection_bound(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
) -> None:
    database = migrated_db(tmp_path / "transcript-receipts.db", target=HEAD)
    authorization = (
        '{"failure":null,"idempotency_key":"transcript:'
        + "a" * 64
        + '","provenance":{},"reason":"authorized","request":'
        '{"document_type":"earnings_call_transcript","provider":"issuer_ir",'
        '"source_regime_identity":{"contract_sha256":"'
        + "b"
        * 64
        + '","regime":"combined"},"source_type":"ir_doc"},'
        '"schema_version":"transcript-acquisition-authorization@1",'
        '"status":"authorized","stored_target":{}}'
    )
    artifact = (
        '{"authorization":'
        + authorization
        + ',"document_id":null,"schema_version":"authorized-transcript-artifact@1",'
        '"source_path":"C:/source","source_url":null,"staged":'
        '{"sha256":"' + "c" * 64 + '","size_bytes":12,"source_device":1,"source_inode":2,'
        '"source_path":"C:/source","staged_path":"C:/stage/c.transcript",'
        '"staging_root":"C:/stage","staging_root_device":3,"staging_root_inode":4}}'
    )
    values = (
        "d" * 64,
        "transcript:" + "a" * 64,
        "c" * 64,
        12,
        "issuer_ir",
        "ir_doc",
        "earnings_call_transcript",
        "combined",
        "b" * 64,
        authorization,
        artifact,
        "2026-08-15T00:00:00Z",
    )
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO transcript_acquisition_receipts "
            "(receipt_id,idempotency_key,artifact_sha256,artifact_size_bytes,provider,"
            "source_type,document_type,source_regime,source_regime_contract_sha256,"
            "authorization_json,artifact_json,recorded_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            values,
        )
        connection.commit()
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute("UPDATE transcript_acquisition_receipts SET artifact_size_bytes=13")
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute("DELETE FROM transcript_acquisition_receipts")
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO transcript_acquisition_receipts "
                "(receipt_id,idempotency_key,artifact_sha256,artifact_size_bytes,provider,"
                "source_type,document_type,source_regime,source_regime_contract_sha256,"
                "authorization_json,artifact_json,recorded_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                ("e" * 64, *values[1:9], authorization.replace("issuer_ir", "roic"), *values[10:]),
            )


def test_0018_downgrade_removes_only_transcript_receipts(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
) -> None:
    database = migrated_db(tmp_path / "transcript-receipts-down.db", target=HEAD)
    command.downgrade(_config(database), PRIOR_HEAD)
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (
            PRIOR_HEAD,
        )
        names = {
            str(row[0])
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert "transcript_acquisition_receipts" not in names
        assert "owner_decision_checkpoints" in names
