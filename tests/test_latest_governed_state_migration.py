from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path
from types import ModuleType

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine

from alembic import command
from scope_identity import derive_retrieval_scope_id
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite

ROOT = Path(__file__).resolve().parents[1]
REVISION = "0261_latest_governed_state"
SCOPE_IDENTITY_REVISION = "0263_ask_scope_identity"
PARENT = "0260_pre_earnings_brief_plumbing"
BASE_REVISION = "0213_decision_draft_provider_id"

TABLES = {
    "latest_governed_refresh_runs",
    "latest_governed_refresh_stage",
    "latest_governed_refresh_receipts",
    "latest_governed_refresh_changes",
    "latest_governed_scope_heads",
    "latest_governed_fact_entries",
    "latest_governed_document_entries",
    "latest_governed_narrative_entries",
    "latest_governed_narrative_fts",
}

HEX_A = "a" * 64
HEX_B = "b" * 64
HEX_C = "c" * 64
T1 = "2026-07-30 10:00:00"
T2 = "2026-07-30 11:00:00"


def _config(path: Path) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path.as_posix()}")
    return config


def _upgrade(path: Path) -> Config:
    conn = sqlite3.connect(path)
    try:
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
    finally:
        conn.close()
    config = _config(path)
    command.stamp(config, BASE_REVISION)
    command.upgrade(config, REVISION)
    return config


def _table_names(conn: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type IN ('table','view')")
    }


def _load_0263_module() -> ModuleType:
    path = ROOT / "alembic" / "versions" / "0263_ask_scope_identity.py"
    spec = importlib.util.spec_from_file_location("migration_0263_for_test", path)
    if spec is None or spec.loader is None:
        raise AssertionError("could not load migration 0263")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _seed_current_source_scope(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        INSERT INTO issuer_reporting_scope_revisions (
          scope_revision_id,idempotency_key,scope_key,issuer_id,revision,
          inclusion_state,history_policy,history_start,latest_years,
          require_sec,require_ir,require_earnings,decision_kind,reason_code,
          reason_details_json,effective_at,knowledge_at,recorded_at,
          supersedes_scope_revision_id
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            "scope-revision-1",
            "scope-revision-1",
            "investor-research",
            "issuer-1",
            1,
            "core",
            "all_available",
            None,
            None,
            1,
            1,
            1,
            "deterministic",
            "test",
            "{}",
            T1,
            T1,
            T1,
            None,
        ),
    )


def _promotion_insert_values(scope_id: str) -> tuple[object, ...]:
    return (
        "promotion-1",
        "promotion-1",
        scope_id,
        1,
        "issuer-1",
        "reporting-1",
        "research-1",
        HEX_A,
        "generation-1",
        HEX_B,
        "[]",
        HEX_A,
        "[]",
        HEX_B,
        T1,
        "policy-1",
        "verifier",
        "1",
        HEX_A,
        HEX_B,
        "promoted",
        None,
        T1,
        None,
        None,
        None,
        "investor-research",
        "scope-revision-1",
    )


_PROMOTION_INSERT = """
    INSERT INTO ask_retrieval_scope_promotions (
      promotion_id,idempotency_key,scope_key,revision,issuer_id,
      reporting_entity_id,research_snapshot_id,research_snapshot_sha256,
      fact_generation_id,fact_projection_seal_sha256,
      source_inventory_set_json,source_inventory_set_sha256,
      narrative_bundles_json,narrative_bundles_sha256,cutoff_at,
      policy_version,verifier_name,verifier_version,
      verifier_code_sha256,verifier_config_sha256,status,
      supersedes_promotion_id,recorded_at,population_run_id,
      population_receipt_set_sha256,population_observed_through,
      source_scope_key,source_scope_revision_id
    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
"""


def test_0263_adds_exact_composite_source_scope_evidence(tmp_path: Path) -> None:
    database = tmp_path / "scope-identity.db"
    config = _upgrade(database)
    command.upgrade(config, SCOPE_IDENTITY_REVISION)

    with sqlite3.connect(database) as conn:
        columns = {
            str(row[1]) for row in conn.execute("PRAGMA table_info(ask_retrieval_scope_promotions)")
        }
        trigger = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?",
            ("trg_ask_retrieval_scope_promotion_source_scope_exact",),
        ).fetchone()
        revision_row = conn.execute("SELECT version_num FROM alembic_version").fetchone()

    assert {"source_scope_key", "source_scope_revision_id"} <= columns
    assert trigger is not None
    assert "ask-scope:v1:" in str(trigger[0])
    assert "derive_retrieval_scope_id" in str(trigger[0])
    assert "source.scope_key=NEW.source_scope_key" in str(trigger[0])
    assert "source.issuer_id=NEW.issuer_id" in str(trigger[0])
    assert "source.scope_revision_id=NEW.source_scope_revision_id" in str(trigger[0])
    assert revision_row == (SCOPE_IDENTITY_REVISION,)


def test_0263_writer_reservation_closes_empty_check_race(tmp_path: Path) -> None:
    database = tmp_path / "scope-identity-lock.db"
    config = _upgrade(database)
    database_url = config.get_main_option("sqlalchemy.url")
    assert database_url is not None
    engine = create_engine(database_url)
    migration = _load_0263_module()

    with engine.connect() as owner, owner.begin():
        migration._acquire_writer_lock(owner)
        contender = sqlite3.connect(database, timeout=0)
        try:
            with pytest.raises(sqlite3.OperationalError, match="database is locked"):
                contender.execute("BEGIN IMMEDIATE")
        finally:
            contender.close()


def test_0263_migrated_trigger_rejects_forged_canonical_scope_id(
    tmp_path: Path,
) -> None:
    database = tmp_path / "scope-identity-trigger.db"
    config = _upgrade(database)
    command.upgrade(config, SCOPE_IDENTITY_REVISION)

    with sqlite3.connect(database) as seed:
        _seed_current_source_scope(seed)
        seed.commit()

    conn = connect_sqlite(
        database,
        role=SQLiteConnectionRole.WRITER,
        schema_preflight=False,
    )
    try:
        conn.execute("PRAGMA foreign_keys=OFF")
        valid = derive_retrieval_scope_id(
            source_scope_key="investor-research",
            issuer_id="issuer-1",
        )
        forged = valid[:-1] + ("0" if valid[-1] != "0" else "1")
        with pytest.raises(
            sqlite3.IntegrityError,
            match="exact current composite source scope",
        ):
            conn.execute(_PROMOTION_INSERT, _promotion_insert_values(forged))
    finally:
        conn.close()


def test_0263_downgrade_refuses_after_first_immutable_promotion(tmp_path: Path) -> None:
    database = tmp_path / "populated-scope-identity.db"
    config = _upgrade(database)
    command.upgrade(config, SCOPE_IDENTITY_REVISION)

    with sqlite3.connect(database) as conn:
        conn.execute("PRAGMA foreign_keys=OFF")
        trigger_names = tuple(
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger' "
                "AND tbl_name='ask_retrieval_scope_promotions'"
            )
        )
        for name in trigger_names:
            escaped = name.replace('"', '""')
            conn.execute(f'DROP TRIGGER "{escaped}"')  # nosec B608 -- sqlite_master identity
        conn.execute(
            """
            INSERT INTO ask_retrieval_scope_promotions (
              promotion_id,idempotency_key,scope_key,revision,issuer_id,
              reporting_entity_id,research_snapshot_id,research_snapshot_sha256,
              fact_generation_id,fact_projection_seal_sha256,
              source_inventory_set_json,source_inventory_set_sha256,
              narrative_bundles_json,narrative_bundles_sha256,cutoff_at,
              policy_version,verifier_name,verifier_version,
              verifier_code_sha256,verifier_config_sha256,status,
              supersedes_promotion_id,recorded_at,population_run_id,
              population_receipt_set_sha256,population_observed_through,
              source_scope_key,source_scope_revision_id
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "promotion-1",
                "promotion-1",
                "ask-scope:v1:" + "0" * 64,
                1,
                "issuer-1",
                "reporting-1",
                "research-1",
                HEX_A,
                "generation-1",
                HEX_B,
                "[]",
                HEX_A,
                "[]",
                HEX_B,
                T1,
                "policy-1",
                "verifier",
                "1",
                HEX_A,
                HEX_B,
                "promoted",
                None,
                T1,
                None,
                None,
                None,
                "investor-research",
                "scope-revision-1",
            ),
        )
        conn.commit()

    with pytest.raises(RuntimeError, match="requires an empty Ask promotion table"):
        command.downgrade(config, REVISION)


def _seed_immutable_rows(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.execute(
        """
        INSERT INTO latest_governed_refresh_runs (
            refresh_run_id, idempotency_key, scope_key, status,
            baseline_population_run_id, baseline_population_receipt_sha256,
            baseline_promotion_id, baseline_fact_generation_id,
            input_head_sha256, policy_config_sha256, knowledge_cutoff,
            observed_through, resume_cursor_json, resume_cursor_sha256,
            staged_change_count, applied_change_count, planned_at, updated_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            "run-1",
            "run-key-1",
            "issuer:1",
            "finalized",
            "population-1",
            HEX_A,
            None,
            "generation-1",
            HEX_A,
            HEX_B,
            T1,
            T1,
            "{}",
            HEX_C,
            1,
            1,
            T1,
            T2,
        ),
    )
    conn.execute(
        """
        INSERT INTO latest_governed_refresh_receipts (
            receipt_id, idempotency_key, refresh_run_id, scope_key,
            prior_receipt_id, baseline_population_run_id,
            baseline_population_receipt_sha256, baseline_promotion_id,
            fact_generation_id, input_head_sha256, prior_state_sha256,
            current_state_sha256, fact_root_sha256, document_root_sha256,
            narrative_root_sha256, change_count, fact_change_count,
            document_change_count, narrative_change_count,
            canonical_change_set_json, change_set_sha256,
            canonical_receipt_json, receipt_sha256, knowledge_cutoff,
            observed_through, sealed_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            "receipt-1",
            "receipt-key-1",
            "run-1",
            "issuer:1",
            None,
            "population-1",
            HEX_A,
            None,
            "generation-1",
            HEX_A,
            None,
            HEX_B,
            HEX_A,
            HEX_B,
            HEX_C,
            1,
            1,
            0,
            0,
            "[]",
            HEX_A,
            "{}",
            HEX_B,
            T1,
            T1,
            T2,
        ),
    )
    conn.execute(
        """
        INSERT INTO latest_governed_refresh_changes (
            change_id, idempotency_key, receipt_id, change_ordinal,
            entity_kind, change_kind, coordinate_key, digest_bucket,
            prior_commitment_sha256, current_commitment_sha256,
            selection_reason, source_evidence_json, source_evidence_sha256,
            canonical_change_json, change_sha256, knowledge_cutoff,
            observed_through, recorded_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            "change-1",
            "change-key-1",
            "receipt-1",
            0,
            "fact",
            "upsert",
            "cell-1",
            1,
            None,
            HEX_A,
            "new governed fact",
            "{}",
            HEX_B,
            "{}",
            HEX_C,
            T1,
            T1,
            T2,
        ),
    )
    conn.commit()


def test_0259_schema_parent_foreign_keys_indexes_fts_and_empty_round_trip(
    tmp_path: Path,
) -> None:
    path = tmp_path / "latest-governed-schema.db"
    config = _upgrade(path)

    revision = ScriptDirectory.from_config(config).get_revision(REVISION)
    assert revision is not None
    assert revision.down_revision == PARENT

    conn = sqlite3.connect(path)
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        assert _table_names(conn) >= TABLES
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []

        run_fks = {
            (str(row[2]), str(row[3]), str(row[4]))
            for row in conn.execute("PRAGMA foreign_key_list(latest_governed_refresh_runs)")
        }
        assert (
            "population_run_headers",
            "baseline_population_run_id",
            "population_run_id",
        ) in run_fks
        assert (
            "population_cutover_receipts",
            "baseline_population_run_id",
            "population_run_id",
        ) in run_fks
        assert (
            "canonical_fact_projection_seals",
            "baseline_fact_generation_id",
            "generation_id",
        ) in run_fks
        assert (
            "ask_retrieval_scope_promotions",
            "baseline_promotion_id",
            "promotion_id",
        ) in run_fks

        with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
            conn.execute(
                """
                INSERT INTO latest_governed_refresh_runs (
                    refresh_run_id, idempotency_key, scope_key, status,
                    baseline_population_run_id,
                    baseline_population_receipt_sha256,
                    baseline_fact_generation_id, input_head_sha256,
                    policy_config_sha256, knowledge_cutoff, observed_through,
                    resume_cursor_json, resume_cursor_sha256,
                    staged_change_count, applied_change_count,
                    planned_at, updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    "missing-baseline",
                    "missing-baseline",
                    "issuer:missing",
                    "planned",
                    "missing-population",
                    HEX_A,
                    "missing-generation",
                    HEX_A,
                    HEX_B,
                    T1,
                    T1,
                    "{}",
                    HEX_C,
                    0,
                    0,
                    T1,
                    T1,
                ),
            )
        conn.rollback()

        indexes = {
            str(row[1]) for row in conn.execute("PRAGMA index_list(latest_governed_fact_entries)")
        }
        assert {
            "ix_latest_governed_fact_search",
            "ix_latest_governed_fact_bucket",
        } <= indexes
        cell_indexes = {
            str(row[1]) for row in conn.execute("PRAGMA index_list(canonical_metric_cells)")
        }
        assert "ix_canonical_metric_cells_reporting_entity" in cell_indexes
        cell_plan = " ".join(
            str(row[3])
            for row in conn.execute(
                "EXPLAIN QUERY PLAN SELECT canonical_metric_cell_id "
                "FROM canonical_metric_cells WHERE reporting_entity_id=? "
                "ORDER BY canonical_metric_cell_id",
                ("reporting-1",),
            )
        )
        assert "ix_canonical_metric_cells_reporting_entity" in cell_plan

        plan = " ".join(
            str(row[3])
            for row in conn.execute(
                """
                EXPLAIN QUERY PLAN
                SELECT canonical_metric_cell_id
                FROM latest_governed_fact_entries
                WHERE scope_key=? AND canonical_metric_name=?
                ORDER BY period_end DESC
                """,
                ("issuer:1", "revenue"),
            )
        )
        assert "ix_latest_governed_fact_search" in plan
        assert "USE TEMP B-TREE" not in plan
        index_sql = str(
            conn.execute(
                "SELECT sql FROM sqlite_master "
                "WHERE type='index' AND name='ix_latest_governed_fact_search'"
            ).fetchone()[0]
        )
        assert "period_end DESC" in index_sql
    finally:
        conn.close()

    command.downgrade(config, PARENT)
    conn = sqlite3.connect(path)
    try:
        assert not (TABLES & _table_names(conn))
        assert "ix_canonical_metric_cells_reporting_entity" not in {
            str(row[1]) for row in conn.execute("PRAGMA index_list(canonical_metric_cells)")
        }
        assert conn.execute("SELECT version_num FROM alembic_version").fetchone() == (PARENT,)
    finally:
        conn.close()


def test_0259_receipts_and_changes_are_append_only_and_replace_safe(
    tmp_path: Path,
) -> None:
    path = tmp_path / "latest-governed-immutable.db"
    _upgrade(path)
    conn = sqlite3.connect(path)
    try:
        _seed_immutable_rows(conn)
        for statement in (
            "UPDATE latest_governed_refresh_receipts "
            "SET receipt_sha256='" + HEX_C + "' WHERE receipt_id='receipt-1'",
            "DELETE FROM latest_governed_refresh_receipts WHERE receipt_id='receipt-1'",
            "UPDATE latest_governed_refresh_changes "
            "SET change_sha256='" + HEX_A + "' WHERE change_id='change-1'",
            "DELETE FROM latest_governed_refresh_changes WHERE change_id='change-1'",
        ):
            with pytest.raises(sqlite3.IntegrityError, match="append-only"):
                conn.execute(statement)

        receipt = conn.execute(
            "SELECT * FROM latest_governed_refresh_receipts WHERE receipt_id='receipt-1'"
        ).fetchone()
        change = conn.execute(
            "SELECT * FROM latest_governed_refresh_changes WHERE change_id='change-1'"
        ).fetchone()
        assert receipt is not None
        assert change is not None
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute(
                "INSERT OR REPLACE INTO latest_governed_refresh_receipts VALUES ("
                + ",".join("?" for _ in receipt)
                + ")",
                receipt,
            )
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute(
                "INSERT OR REPLACE INTO latest_governed_refresh_changes VALUES ("
                + ",".join("?" for _ in change)
                + ")",
                change,
            )
    finally:
        conn.close()


def test_0259_current_narrative_fts_tracks_mutable_projection(tmp_path: Path) -> None:
    path = tmp_path / "latest-governed-fts.db"
    _upgrade(path)
    conn = sqlite3.connect(path)
    try:
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute(
            """
            INSERT INTO latest_governed_narrative_entries (
                scope_key, expected_document_key, chunk_key, digest_bucket,
                refresh_receipt_id, document_version_id, evidence_node_id,
                source_chunk_id, embedding_artifact_id, text, content_sha256,
                chunker_config_sha256, selection_reason,
                prior_commitment_sha256, current_commitment_sha256,
                knowledge_cutoff, observed_through, updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "issuer:1",
                "10-q:2026-q2",
                "chunk:1",
                7,
                "receipt-1",
                "document-1",
                "node-1",
                None,
                None,
                "latest governed demand accelerated",
                HEX_A,
                HEX_B,
                "selected current document",
                None,
                HEX_C,
                T1,
                T1,
                T2,
            ),
        )
        assert conn.execute(
            "SELECT scope_key FROM latest_governed_narrative_fts "
            "WHERE latest_governed_narrative_fts MATCH 'accelerated'"
        ).fetchall() == [("issuer:1",)]
        conn.execute(
            "UPDATE latest_governed_narrative_entries "
            "SET text='latest governed margins expanded' "
            "WHERE scope_key='issuer:1' AND expected_document_key='10-q:2026-q2' "
            "AND chunk_key='chunk:1'"
        )
        assert (
            conn.execute(
                "SELECT scope_key FROM latest_governed_narrative_fts "
                "WHERE latest_governed_narrative_fts MATCH 'accelerated'"
            ).fetchall()
            == []
        )
        assert conn.execute(
            "SELECT scope_key FROM latest_governed_narrative_fts "
            "WHERE latest_governed_narrative_fts MATCH 'margins'"
        ).fetchall() == [("issuer:1",)]
        conn.execute("DELETE FROM latest_governed_narrative_entries WHERE scope_key='issuer:1'")
        assert conn.execute("SELECT COUNT(*) FROM latest_governed_narrative_fts").fetchone() == (0,)
    finally:
        conn.close()


def test_0259_populated_downgrade_refuses_without_deleting_state(
    tmp_path: Path,
) -> None:
    path = tmp_path / "latest-governed-populated.db"
    config = _upgrade(path)
    conn = sqlite3.connect(path)
    try:
        _seed_immutable_rows(conn)
    finally:
        conn.close()

    with pytest.raises(RuntimeError, match="would discard latest governed state"):
        command.downgrade(config, PARENT)

    conn = sqlite3.connect(path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM latest_governed_refresh_receipts").fetchone() == (
            1,
        )
        assert conn.execute("SELECT version_num FROM alembic_version").fetchone() == (REVISION,)
    finally:
        conn.close()
