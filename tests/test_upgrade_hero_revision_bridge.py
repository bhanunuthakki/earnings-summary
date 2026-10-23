"""Recover only the exact pre-integration Hero schema, without losing evidence."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from pathlib import Path

import pytest
import upgrade_database as upgrade_module
from alembic.script import ScriptDirectory
from sqlalchemy import event
from sqlalchemy.engine import Engine
from upgrade_database import UpgradeDatabaseError, upgrade_database

from provenance.evidence_ledger import EvidenceLedger, EvidenceNode
from search.corpus_builder import build_grounded_search_corpus
from sqlite_runtime import register_sqlite_integrity_functions
from tests import test_search_corpus_builder as corpus

ROOT = Path(__file__).resolve().parents[1]
LEGACY = "0060_bulk_lexical_projection_coverage"
PRIOR = "0059_scoped_research_universe_commitments"
TRIGGER = "trg_search_projection_seals_lexical_coverage"
config = getattr(upgrade_module, "_config")
seed_corpus: Callable[..., None] = getattr(corpus, "_seed")


@pytest.fixture
def legacy_db(tmp_path: Path, migrated_db: Callable[..., Path]) -> Path:
    path = migrated_db(tmp_path / "legacy.db", target=PRIOR)
    script = ScriptDirectory.from_config(config(ROOT, path, archived=False))
    lexical = script.get_revision("0061_bulk_lexical_projection_coverage")
    assert lexical is not None
    # The retained Hero branch differs from 0059 only by this exact trigger.
    with sqlite3.connect(path) as conn:
        conn.execute(f"DROP TRIGGER {TRIGGER}")
        conn.execute(lexical.module._BULK_SQL)
        conn.execute("UPDATE alembic_version SET version_num=?", (LEGACY,))
        register_sqlite_integrity_functions(conn)
        seed_corpus(conn)
        build_grounded_search_corpus(conn, getattr(corpus, "_request")(apply=True))
        conn.execute(
            "INSERT INTO documents (id,ticker,source_type,doc_type,file_path,sha256,"
            "fetched_at,fetch_status,raw_bytes_size) VALUES "
            "(1,'ACME','sec','10-Q','fixture',?,'2026-07-27','success',1)",
            ("d" * 64,),
        )
        EvidenceLedger(conn).persist(
            EvidenceNode(
                node_id="document-node",
                evidence_key="document-node",
                revision=1,
                extraction_run_id="run",
                node_kind="document",
                text="Revenue 100 USD",
                recorded_at=corpus.STAMP,
            )
        )
        conn.execute(
            "INSERT INTO issuer_entities VALUES ('issuer','issuer','operating_company','2026-07-27')"
        )
        conn.execute(
            "INSERT INTO legacy_document_evidence_binding_revisions "
            "(binding_revision_id,idempotency_key,legacy_document_id,revision,"
            "document_version_id,evidence_node_id,scope_locator_json,scope_locator_sha256,"
            "scope_content_sha256,effective_at,knowledge_at,recorded_at) "
            "VALUES ('binding','binding',1,1,'doc','document-node','{}',?, ?, "
            "'2026-07-27','2026-07-27','2026-07-27')",
            ("a" * 64, "b" * 64),
        )
        conn.execute(
            "INSERT INTO financial_facts (ticker,period_end,fiscal_period_type,line_item,"
            "value,unit,source_doc_id) VALUES ('ACME','2026-06-30','Q2','revenue',100,'USD',1)"
        )
    return path


def retained_rows(path: Path) -> dict[str, list[tuple[object, ...]]]:
    with sqlite3.connect(path) as conn:
        return {
            table: conn.execute(f"SELECT * FROM {table}").fetchall()
            for table in (
                "financial_facts",
                "evidence_source_observations",
                "evidence_nodes",
                "search_chunks",
                "search_corpus_manifest_seals",
                "search_projection_seals",
            )
        }


def recover(path: Path) -> upgrade_module.UpgradeReceipt:
    return upgrade_database(path, repo_root=ROOT, runtime_root=ROOT, allow_isolated_db=True)


def test_bridge_applies_missing_owner_schema_and_preserves_financial_rows_and_seals(
    legacy_db: Path,
) -> None:
    before = retained_rows(legacy_db)
    assert before["financial_facts"] and before["search_projection_seals"]
    receipt = upgrade_database(legacy_db, repo_root=ROOT, runtime_root=ROOT, allow_isolated_db=True)
    assert receipt.status == "bridged"
    assert receipt.from_revision == LEGACY
    assert receipt.to_revision == upgrade_module.ACTIVE_HEAD
    assert receipt.backup_path is not None
    assert retained_rows(Path(receipt.backup_path)) == before
    assert retained_rows(legacy_db) == before
    with sqlite3.connect(legacy_db) as conn:
        assert "owner_authorization_id" in {
            row[1] for row in conn.execute("PRAGMA table_info(research_proposals)")
        }
        with pytest.raises(sqlite3.IntegrityError, match="invalid governed"):
            conn.execute(
                "INSERT INTO research_proposals (id,ticker,kind,status,canonical_content_json,"
                "canonical_content_sha256,target_precondition_sha256,proposal_revision,"
                "created_at,updated_at) VALUES (1,'ACME','owner_thesis_edit','pending','{}',"
                "?, ?, 0,'2026-10-22','2026-10-22')",
                ("a" * 64, "b" * 64),
            )
    assert recover(legacy_db).status == "already_current"


@pytest.mark.parametrize("damage", ["missing", "altered", "extra", "partial_owner"])
def test_bridge_rejects_unknown_schema_before_revision_or_evidence_mutation(
    legacy_db: Path, damage: str
) -> None:
    with sqlite3.connect(legacy_db) as conn:
        if damage == "missing":
            conn.execute("DROP TRIGGER trg_search_projection_seals_append_only")
        elif damage == "altered":
            conn.execute(f"DROP TRIGGER {TRIGGER}")
            conn.execute(
                f"CREATE TRIGGER {TRIGGER} BEFORE INSERT ON search_projection_seals "
                "BEGIN SELECT 1; END"
            )
        elif damage == "extra":
            conn.execute("CREATE TABLE unexpected (id TEXT)")
        else:
            conn.execute("ALTER TABLE research_proposals ADD COLUMN owner_authorization_id TEXT")
    before = retained_rows(legacy_db)
    with pytest.raises(UpgradeDatabaseError, match=r"pre-integration.*schema"):
        recover(legacy_db)
    assert retained_rows(legacy_db) == before
    with sqlite3.connect(legacy_db) as conn:
        assert conn.execute("SELECT version_num FROM alembic_version").fetchone() == (LEGACY,)
        assert not conn.execute(
            "SELECT 1 FROM sqlite_schema WHERE name='owner_proposal_authorizations'"
        ).fetchall()


def test_bridge_rolls_back_owner_ddl_and_revision_on_interruption(legacy_db: Path) -> None:
    before = retained_rows(legacy_db)

    def interrupt(
        _conn: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _many: bool,
    ) -> None:
        if "CREATE TRIGGER IF NOT EXISTS trg_owner_research_proposal_no_delete" in statement:
            raise RuntimeError("test interrupted owner migration")

    event.listen(Engine, "before_cursor_execute", interrupt)
    try:
        with pytest.raises(RuntimeError, match="test interrupted"):
            recover(legacy_db)
    finally:
        event.remove(Engine, "before_cursor_execute", interrupt)
    assert retained_rows(legacy_db) == before
    with sqlite3.connect(legacy_db) as conn:
        assert conn.execute("SELECT version_num FROM alembic_version").fetchone() == (LEGACY,)
        assert not conn.execute(
            "SELECT 1 FROM sqlite_schema WHERE name='owner_proposal_authorizations'"
        ).fetchall()
        assert "owner_authorization_id" not in {
            row[1] for row in conn.execute("PRAGMA table_info(research_proposals)")
        }


def test_ordinary_merged_predecessor_uses_normal_upgrade(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    path = migrated_db(tmp_path / "ordinary.db", target=PRIOR)
    receipt = recover(path)
    assert receipt.status == "upgraded"
    assert receipt.from_revision == PRIOR
    assert receipt.to_revision == upgrade_module.ACTIVE_HEAD
    assert recover(path).status == "already_current"
