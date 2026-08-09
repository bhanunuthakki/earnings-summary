from __future__ import annotations

import shutil
import sqlite3
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import cast

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory

from alembic import command
from tests import test_fact_plane_v2_migration as fact_plane_v2

H = fact_plane_v2.H
H2 = fact_plane_v2.H2
T0 = fact_plane_v2.T0

InsertHelper = Callable[..., None]
SeedFactory = Callable[[pytest.TempPathFactory], Path]
_insert_candidate = cast(InsertHelper, getattr(fact_plane_v2, "_insert_candidate"))
_insert_cell = cast(InsertHelper, getattr(fact_plane_v2, "_insert_cell"))
_insert_reported = cast(InsertHelper, getattr(fact_plane_v2, "_insert_reported"))
_insert_resolution = cast(InsertHelper, getattr(fact_plane_v2, "_insert_resolution"))

ROOT = Path(__file__).resolve().parents[1]
REVISION = "0239_structured_fact_search_projection"
PREDECESSOR = "0238_evidence_first_fact_plane"


def _config(path: Path) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    config.set_main_option("version_locations", str(ROOT / "alembic" / "versions_archived"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path}")
    return config


@pytest.fixture(scope="module")
def upgraded_seed(
    tmp_path_factory: pytest.TempPathFactory,
) -> Path:
    seed_factory = cast(SeedFactory, getattr(fact_plane_v2.upgraded_seed, "__wrapped__"))
    fact_plane_v2_seed = seed_factory(tmp_path_factory)
    path = tmp_path_factory.mktemp("structured-fact-search") / "seed.db"
    shutil.copy2(fact_plane_v2_seed, path)
    command.upgrade(_config(path), REVISION)
    return path


@pytest.fixture()
def conn(
    upgraded_seed: Path,
    tmp_path: Path,
) -> Iterator[sqlite3.Connection]:
    path = tmp_path / "test.db"
    shutil.copy2(upgraded_seed, path)
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        yield connection
    finally:
        connection.close()


def _seed_complete_manifest(
    conn: sqlite3.Connection,
    *,
    manifest_id: str = "manifest-1",
    include_document: bool = True,
    include_chunk: bool = False,
) -> None:
    conn.execute(
        "INSERT INTO search_corpus_manifests "
        "(manifest_id,idempotency_key,corpus_key,revision,"
        "selection_config_sha256,selector_code_version,knowledge_cutoff,"
        "supersedes_manifest_id,recorded_at) "
        "VALUES (?,?,?,1,?,'test',?,NULL,?)",
        (
            manifest_id,
            f"manifest:{manifest_id}",
            f"corpus:{manifest_id}",
            H,
            T0,
            T0,
        ),
    )
    if include_document:
        conn.execute(
            "INSERT INTO search_corpus_document_memberships "
            "(membership_id,manifest_id,expected_document_key,"
            "document_version_id,membership_status,reason,recorded_at) "
            "VALUES (?,?,?,'doc-1','included','selected',?)",
            (
                f"doc-membership:{manifest_id}",
                manifest_id,
                f"expected:{manifest_id}",
                T0,
            ),
        )
    if include_chunk:
        conn.execute(
            "INSERT INTO search_chunks "
            "(chunk_id,idempotency_key,manifest_id,evidence_node_id,chunk_key,"
            "chunk_revision,text,content_sha256,char_start,char_end,"
            "chunker_config_sha256,chunker_code_version,available_at,recorded_at) "
            "VALUES (?,?,?,'node-1','node-1:0-2',1,'{}',?,0,2,?,'test',?,?)",
            (
                f"chunk:{manifest_id}",
                f"chunk:{manifest_id}",
                manifest_id,
                H2,
                H,
                T0,
                T0,
            ),
        )
    conn.execute(
        "INSERT INTO search_corpus_manifest_seals "
        "(manifest_id,expected_document_count,membership_digest_sha256,"
        "completion_status,sealed_at) VALUES (?,?,?,?,?)",
        (
            manifest_id,
            1 if include_document else 0,
            H,
            "complete",
            T0,
        ),
    )


def _insert_projection_run(
    conn: sqlite3.Connection,
    *,
    run_id: str = "projection-1",
    manifest_id: str = "manifest-1",
    cutoff: str = T0,
) -> None:
    conn.execute(
        "INSERT INTO search_fact_projection_runs "
        "(projection_run_id,idempotency_key,projection_key,revision,manifest_id,"
        "knowledge_cutoff,config_sha256,code_version,"
        "supersedes_projection_run_id,recorded_at) "
        "VALUES (?,?,?,1,?,?,?,'test',NULL,?)",
        (
            run_id,
            f"projection:{run_id}",
            f"projection-key:{run_id}",
            manifest_id,
            cutoff,
            H,
            T0,
        ),
    )


def _insert_membership(
    conn: sqlite3.Connection,
    cell_id: str,
    disposition: str,
    *,
    run_id: str = "projection-1",
    resolution_id: str | None = None,
) -> None:
    conn.execute(
        "INSERT INTO search_fact_projection_memberships "
        "(membership_id,projection_run_id,fact_cell_id,disposition,"
        "resolution_revision_id,reason_code,reason_details_json,"
        "membership_bundle_sha256,recorded_at) "
        "VALUES (?,?,?,?,?,'test','{}',?,?)",
        (
            f"membership:{run_id}:{cell_id}",
            run_id,
            cell_id,
            disposition,
            resolution_id,
            H,
            T0,
        ),
    )


def _insert_seal(
    conn: sqlite3.Connection,
    *,
    eligible: int,
    included: int = 0,
    unresolved: int = 0,
    missing: int = 0,
    quarantined: int = 0,
) -> None:
    conn.execute(
        "INSERT INTO search_fact_projection_seals "
        "(projection_seal_id,idempotency_key,projection_run_id,manifest_id,"
        "eligible_fact_cell_count,membership_count,included_count,"
        "unresolved_material_count,missing_provenance_count,quarantined_count,"
        "row_count,membership_set_sha256,row_set_sha256,config_sha256,sealed_at) "
        "VALUES ('seal-1','seal-1','projection-1','manifest-1',?,?,?,?,?,?,"
        "?,?,?, ?,?)",
        (
            eligible,
            eligible,
            included,
            unresolved,
            missing,
            quarantined,
            included,
            H,
            H2,
            H,
            T0,
        ),
    )


def _insert_trace(conn: sqlite3.Connection, trace_id: str) -> None:
    conn.execute(
        "INSERT INTO ask_retrieval_traces "
        "(trace_id,idempotency_key,question_sha256,scope_sha256,"
        "retrieval_config_sha256,outcome,reason_code,manifest_ids_json,"
        "filters_json,created_at) "
        "VALUES (?,?,?,?,?,'ready','test','[\"manifest-1\"]','{}',?)",
        (trace_id, f"trace:{trace_id}", H, H, H, T0),
    )


def test_migration_is_reversible_single_head_and_additive(tmp_path: Path) -> None:
    script = ScriptDirectory.from_config(_config(tmp_path / "unused.db"))
    assert len(script.get_heads()) == 1
    assert script.get_revision(REVISION) is not None

    path = tmp_path / "chain.db"
    legacy = sqlite3.connect(path)
    try:
        legacy.executescript(
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
        legacy.commit()
    finally:
        legacy.close()
    base_revision = "0213_decision_draft_provider_id"
    config = _config(path)
    command.stamp(config, base_revision)
    command.upgrade(config, REVISION)
    conn = sqlite3.connect(path)
    try:
        tables = {
            row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        assert (
            set(
                (
                    "search_fact_projection_runs",
                    "search_fact_projection_memberships",
                    "search_fact_projection_rows",
                    "search_fact_projection_seals",
                    "ask_retrieval_trace_hits",
                )
            )
            <= tables
        )
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        conn.close()

    command.downgrade(config, PREDECESSOR)
    conn = sqlite3.connect(path)
    try:
        assert (
            conn.execute(
                "SELECT 1 FROM sqlite_master WHERE name = 'search_fact_projection_runs'"
            ).fetchone()
            is None
        )
        assert conn.execute("SELECT version_num FROM alembic_version").fetchone() == (PREDECESSOR,)
    finally:
        conn.close()


def test_run_requires_complete_manifest_at_exact_cutoff(
    conn: sqlite3.Connection,
) -> None:
    _seed_complete_manifest(conn)
    with pytest.raises(sqlite3.IntegrityError, match="same cutoff"):
        _insert_projection_run(conn, cutoff="2026-07-26T12:00:00")
    _insert_projection_run(conn)


def test_seal_requires_one_disposition_for_every_as_known_cell(
    conn: sqlite3.Connection,
) -> None:
    _seed_complete_manifest(conn)
    _insert_projection_run(conn)
    _insert_cell(conn, "cell-a", semantic_hash="d" * 64)
    _insert_cell(conn, "cell-b", semantic_hash="e" * 64)
    _insert_membership(conn, "cell-a", "quarantined")
    with pytest.raises(sqlite3.IntegrityError, match=r"coverage|counts"):
        _insert_seal(conn, eligible=1, quarantined=1)

    _insert_membership(conn, "cell-b", "unresolved_material")
    _insert_seal(conn, eligible=2, unresolved=1, quarantined=1)
    assert conn.execute(
        "SELECT projection_run_id, membership_count, row_count "
        "FROM v_search_fact_projection_current_sealed"
    ).fetchone() == ("projection-1", 2, 0)
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("UPDATE search_fact_projection_memberships SET reason_code = 'changed'")
    with pytest.raises(sqlite3.IntegrityError, match="sealed"):
        _insert_membership(conn, "cell-a", "quarantined")


def test_companyfacts_without_exact_match_cannot_masquerade_as_fact_hit(
    conn: sqlite3.Connection,
) -> None:
    _seed_complete_manifest(conn)
    _insert_projection_run(conn)
    _insert_cell(conn, "cell-reported")
    _insert_reported(conn, "observation-reported", "cell-reported")
    _insert_candidate(
        conn,
        "candidate-reported",
        "candidate-set-reported",
        "cell-reported",
        "observation-reported",
    )
    _insert_resolution(
        conn,
        "resolution-reported",
        "cell-reported",
        "candidate-set-reported",
        selected="observation-reported",
    )
    with pytest.raises(sqlite3.IntegrityError, match="provenance"):
        _insert_membership(
            conn,
            "cell-reported",
            "included",
            resolution_id="resolution-reported",
        )
    _insert_membership(
        conn,
        "cell-reported",
        "missing_provenance",
        resolution_id="resolution-reported",
    )
    _insert_seal(conn, eligible=1, missing=1)
    assert conn.execute("SELECT COUNT(*) FROM v_search_fact_hits_current").fetchone() == (0,)


def test_heterogeneous_trace_sources_and_ranks_are_fail_closed(
    conn: sqlite3.Connection,
) -> None:
    _seed_complete_manifest(conn, include_chunk=True)
    _insert_trace(conn, "trace-new-first")
    conn.execute(
        "INSERT INTO ask_retrieval_trace_hits "
        "(trace_id,rank,hit_kind,manifest_id,chunk_id,projection_run_id,"
        "fact_hit_id,score,bundle_sha256,recorded_at) "
        "VALUES ('trace-new-first',1,'document','manifest-1',"
        "'chunk:manifest-1',NULL,NULL,1.0,?,?)",
        (H, T0),
    )
    with pytest.raises(sqlite3.IntegrityError, match="rank"):
        conn.execute(
            "INSERT INTO ask_retrieval_trace_items "
            "(trace_id,rank,manifest_id,chunk_id,score,bundle_sha256,recorded_at) "
            "VALUES ('trace-new-first',1,'manifest-1','chunk:manifest-1',1.0,?,?)",
            (H, T0),
        )

    _insert_trace(conn, "trace-legacy-first")
    conn.execute(
        "INSERT INTO ask_retrieval_trace_items "
        "(trace_id,rank,manifest_id,chunk_id,score,bundle_sha256,recorded_at) "
        "VALUES ('trace-legacy-first',1,'manifest-1','chunk:manifest-1',1.0,?,?)",
        (H, T0),
    )
    with pytest.raises(sqlite3.IntegrityError, match="rank"):
        conn.execute(
            "INSERT INTO ask_retrieval_trace_hits "
            "(trace_id,rank,hit_kind,manifest_id,chunk_id,projection_run_id,"
            "fact_hit_id,score,bundle_sha256,recorded_at) "
            "VALUES ('trace-legacy-first',1,'document','manifest-1',"
            "'chunk:manifest-1',NULL,NULL,1.0,?,?)",
            (H, T0),
        )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO ask_retrieval_trace_hits "
            "(trace_id,rank,hit_kind,manifest_id,chunk_id,projection_run_id,"
            "fact_hit_id,score,bundle_sha256,recorded_at) "
            "VALUES ('trace-legacy-first',2,'fact','manifest-1',"
            "'chunk:manifest-1',NULL,NULL,1.0,?,?)",
            (H, T0),
        )
