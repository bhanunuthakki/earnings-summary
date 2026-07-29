"""Migration contract for immutable source-fact publication receipts."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Generator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic.config import Config

from alembic import command
from provenance.source_fact_repository import SourceFactRepository

ROOT = Path(__file__).resolve().parents[1]
REVISION = "0241_source_fact_publication_ledger"
BASE_REVISION = "0213_decision_draft_provider_id"
STAMP = datetime(2026, 7, 27, 12, 0, tzinfo=UTC)


def _config(path: Path) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path}")
    return config


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _payload(
    publication_id: str,
    idempotency_key: str,
    member_set_sha256: str,
    *,
    cell_count: int,
    member_count: int,
) -> str:
    return _canonical(
        {
            "created_at": STAMP.isoformat(),
            "graph_counts": {
                "cell_count": cell_count,
                "derivation_seal_count": 0,
                "extraction_seal_count": 0,
                "member_count": member_count,
                "observation_count": 0,
                "relation_count": 0,
                "resolution_revision_count": 0,
            },
            "idempotency_key": idempotency_key,
            "member_set_sha256": member_set_sha256,
            "payload_version": "source_fact_publication.v1",
            "publication_id": publication_id,
            "recorded_at": STAMP.isoformat(),
        }
    )


def _insert_header(
    conn: sqlite3.Connection,
    publication_id: str,
    idempotency_key: str,
    *,
    member_set_sha256: str,
    cell_count: int,
    member_count: int,
) -> str:
    payload = _payload(
        publication_id,
        idempotency_key,
        member_set_sha256,
        cell_count=cell_count,
        member_count=member_count,
    )
    conn.execute(
        "INSERT INTO source_fact_publications VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            publication_id,
            idempotency_key,
            "source_fact_publication.v1",
            payload,
            _sha(payload),
            member_set_sha256,
            cell_count,
            0,
            0,
            0,
            0,
            0,
            member_count,
            STAMP,
            STAMP,
        ),
    )
    return payload


@pytest.fixture
def migrated(
    tmp_path: Path,
) -> Generator[tuple[Path, sqlite3.Connection], None, None]:
    path = tmp_path / "source-fact-publication-migration.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE financial_facts (
            id INTEGER PRIMARY KEY,
            source_doc_id INTEGER NOT NULL
        );
        CREATE TABLE kpi_facts (
            id INTEGER PRIMARY KEY,
            source_doc_id INTEGER NOT NULL
        );
        """
    )
    conn.commit()
    conn.close()
    config = _config(path)
    command.stamp(config, BASE_REVISION)
    command.upgrade(config, REVISION)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys = ON")
    SourceFactRepository(conn)
    try:
        yield path, conn
    finally:
        conn.close()


def test_migration_adds_one_sealed_publication_head(
    migrated: tuple[Path, sqlite3.Connection],
) -> None:
    _path, conn = migrated
    tables = {
        str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    assert {
        "source_fact_publications",
        "source_fact_publication_members",
        "source_fact_publication_seals",
    } <= tables
    assert conn.execute("SELECT version_num FROM alembic_version").fetchone() == (REVISION,)
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_database_rejects_dangling_members_and_sealed_mutation(
    migrated: tuple[Path, sqlite3.Connection],
) -> None:
    _path, conn = migrated
    empty_members = "[]"
    empty_digest = _sha(empty_members)
    payload = _insert_header(
        conn,
        "publication-empty",
        "publication-empty-key",
        member_set_sha256=empty_digest,
        cell_count=0,
        member_count=0,
    )
    conn.execute(
        "INSERT INTO source_fact_publication_seals VALUES (?,?,?,?,?,?,?,?)",
        (
            "seal-empty",
            "seal-empty-key",
            "publication-empty",
            0,
            empty_members,
            empty_digest,
            _sha(payload),
            STAMP,
        ),
    )
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute(
            "UPDATE source_fact_publications SET member_count = 1 "
            "WHERE publication_id = 'publication-empty'"
        )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO source_fact_publication_members VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "member-after-seal",
                "member-after-seal-key",
                "publication-empty",
                0,
                "fact_cell",
                "missing-cell",
                "missing-cell-key",
                "source_fact_record_commitment.v1",
                "0" * 64,
                "{}",
                _sha("{}"),
                STAMP,
            ),
        )

    member_payload = _canonical(
        {
            "member_ordinal": 0,
            "record_commitment_sha256": "1" * 64,
            "record_commitment_version": "source_fact_record_commitment.v1",
            "record_id": "missing-cell",
            "record_idempotency_key": "missing-cell-key",
            "record_kind": "fact_cell",
        }
    )
    member_set = f"[{member_payload}]"
    _insert_header(
        conn,
        "publication-dangling",
        "publication-dangling-key",
        member_set_sha256=_sha(member_set),
        cell_count=1,
        member_count=1,
    )
    with pytest.raises(sqlite3.IntegrityError, match="does not exist"):
        conn.execute(
            "INSERT INTO source_fact_publication_members VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "member-dangling",
                "member-dangling-key",
                "publication-dangling",
                0,
                "fact_cell",
                "missing-cell",
                "missing-cell-key",
                "source_fact_record_commitment.v1",
                "1" * 64,
                member_payload,
                _sha(member_payload),
                STAMP,
            ),
        )


def test_publication_ledger_downgrades_cleanly(
    migrated: tuple[Path, sqlite3.Connection],
) -> None:
    path, conn = migrated
    conn.close()
    command.downgrade(_config(path), "0240_fact_plane_v2_hardening")
    downgraded = sqlite3.connect(path)
    try:
        assert (
            downgraded.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' "
                "AND name = 'source_fact_publications'"
            ).fetchone()
            is None
        )
        assert downgraded.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        downgraded.close()
