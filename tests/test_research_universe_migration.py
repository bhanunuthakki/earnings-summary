from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic.config import Config

from alembic import command

ROOT = Path(__file__).resolve().parents[1]
BASE_REVISION = "0213_decision_draft_provider_id"
PRE_UNIVERSE_REVISION = "0249_embedding_runtime_artifact_binding"
UNIVERSE_REVISION = "0252_research_universe_closure"
STAMP = datetime(2026, 7, 28, 12, 0, tzinfo=UTC)


def _sha(value: object) -> str:
    return hashlib.sha256(str(value).encode()).hexdigest()


def _config(path: Path) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path}")
    return config


def _database_at_0249(path: Path) -> Config:
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE financial_facts (
            id INTEGER PRIMARY KEY, source_doc_id INTEGER NOT NULL
        );
        CREATE TABLE kpi_facts (
            id INTEGER PRIMARY KEY, source_doc_id INTEGER NOT NULL
        );
        """
    )
    conn.close()
    config = _config(path)
    command.stamp(config, BASE_REVISION)
    command.upgrade(config, PRE_UNIVERSE_REVISION)
    return config


def _connection(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys=ON")
    conn.create_function("fact_sha256", 1, _sha)
    return conn


def test_0252_empty_upgrade_and_downgrade_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "round-trip.db"
    config = _database_at_0249(path)
    command.upgrade(config, UNIVERSE_REVISION)
    conn = _connection(path)
    assert {
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    } >= {
        "expected_document_obligation_bindings",
        "research_snapshot_universe_commitments",
    }
    conn.close()
    command.downgrade(config, PRE_UNIVERSE_REVISION)
    conn = _connection(path)
    assert (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='research_snapshot_universe_commitments'"
        ).fetchone()
        is None
    )
    conn.close()


def test_0252_backfills_and_enforces_embedding_promotion_clocks(
    tmp_path: Path,
) -> None:
    path = tmp_path / "promotion-clocks.db"
    config = _database_at_0249(path)
    conn = _connection(path)
    conn.execute(
        "INSERT INTO search_embedding_model_promotions ("
        "promotion_id,idempotency_key,purpose,revision,provider,model,dimensions,"
        "golden_sha256,evaluation_artifact_sha256,evaluation_metrics_json,"
        "approved_by,approved_at,supersedes_promotion_id,runtime_artifact_json,"
        "runtime_artifact_sha256) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "promotion-1",
            "promotion-1",
            "evidence_vector_retrieval",
            1,
            "local",
            "embed-v1",
            8,
            "a" * 64,
            "b" * 64,
            "{}",
            "owner",
            STAMP,
            None,
            "{}",
            "c" * 64,
        ),
    )
    conn.commit()
    conn.close()

    command.upgrade(config, UNIVERSE_REVISION)
    conn = _connection(path)
    clocks = conn.execute(
        "SELECT approved_at,knowledge_at,recorded_at "
        "FROM search_embedding_model_promotions WHERE promotion_id='promotion-1'"
    ).fetchone()
    assert clocks is not None
    assert str(clocks[0]) == str(clocks[1]) == str(clocks[2])
    with pytest.raises(sqlite3.IntegrityError, match="clocks are invalid"):
        conn.execute(
            "INSERT INTO search_embedding_model_promotions ("
            "promotion_id,idempotency_key,purpose,revision,provider,model,dimensions,"
            "golden_sha256,evaluation_artifact_sha256,evaluation_metrics_json,"
            "approved_by,approved_at,supersedes_promotion_id,runtime_artifact_json,"
            "runtime_artifact_sha256) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "promotion-2",
                "promotion-2",
                "evidence_vector_retrieval",
                2,
                "local",
                "embed-v1",
                8,
                "a" * 64,
                "b" * 64,
                "{}",
                "owner",
                STAMP,
                "promotion-1",
                "{}",
                "c" * 64,
            ),
        )
    conn.close()
    command.downgrade(config, PRE_UNIVERSE_REVISION)
    conn = _connection(path)
    columns = {
        str(row[1])
        for row in conn.execute("PRAGMA table_info(search_embedding_model_promotions)")
    }
    assert "knowledge_at" not in columns
    assert "recorded_at" not in columns
    conn.close()


def test_0252_upgrade_refuses_unverifiable_sealed_history(
    tmp_path: Path,
) -> None:
    path = tmp_path / "old-seal.db"
    config = _database_at_0249(path)
    conn = _connection(path)
    request_json = "{}"
    conn.execute(
        "INSERT INTO research_snapshot_headers VALUES (?,?,?,?,?,?)",
        (
            "legacy-research",
            "legacy-research",
            request_json,
            _sha(request_json),
            STAMP,
            STAMP,
        ),
    )
    member_json = "{}"
    conn.execute(
        "INSERT INTO research_snapshot_members VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            "legacy-research",
            0,
            "legacy",
            "legacy",
            "legacy",
            "a" * 64,
            STAMP,
            STAMP,
            member_json,
            _sha(member_json),
        ),
    )
    member_set_json = "[{}]"
    conn.execute(
        "INSERT INTO research_snapshot_seals VALUES (?,?,?,?,?)",
        (
            "legacy-research",
            1,
            member_set_json,
            _sha(member_set_json),
            STAMP,
        ),
    )
    conn.commit()
    conn.close()
    with pytest.raises(RuntimeError, match="cannot infer an exact universe"):
        command.upgrade(config, UNIVERSE_REVISION)
    conn = _connection(path)
    assert (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='research_snapshot_universe_commitments'"
        ).fetchone()
        is None
    )
    conn.close()


def test_0252_downgrade_refuses_committed_universe(tmp_path: Path) -> None:
    path = tmp_path / "downgrade-refusal.db"
    config = _database_at_0249(path)
    command.upgrade(config, UNIVERSE_REVISION)
    conn = _connection(path)
    conn.execute(
        "INSERT INTO issuer_entities VALUES (?,?,?,?)",
        ("issuer-1", "issuer-1", "operating_company", STAMP),
    )
    universe = {
        "document_version_ids": ["document-1"],
        "issuer_id": "issuer-1",
        "reporting_entity_ids": ["reporting-1"],
        "source_obligation_revision_ids": ["obligation-1"],
    }
    canonical = json.dumps(
        universe,
        sort_keys=True,
        separators=(",", ":"),
    )
    request_json = json.dumps(
        {"research_universe": universe},
        sort_keys=True,
        separators=(",", ":"),
    )
    conn.execute(
        "INSERT INTO research_snapshot_headers VALUES (?,?,?,?,?,?)",
        (
            "research-1",
            "research-1",
            request_json,
            _sha(request_json),
            STAMP,
            STAMP,
        ),
    )
    conn.execute("DROP TRIGGER trg_research_snapshot_universe_exact")
    conn.execute(
        "INSERT INTO research_snapshot_universe_commitments VALUES "
        "(?,?,?,?,?,?,?,?,?)",
        (
            "research-1",
            "issuer-1",
            '["reporting-1"]',
            '["document-1"]',
            '["obligation-1"]',
            canonical,
            _sha(canonical),
            STAMP,
            STAMP,
        ),
    )
    conn.commit()
    conn.close()
    with pytest.raises(RuntimeError, match="downgrade refused"):
        command.downgrade(config, PRE_UNIVERSE_REVISION)
