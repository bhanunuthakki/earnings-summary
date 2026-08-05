# pyright: reportPrivateUsage=false
"""The additive evidence/search roadmap remains one reversible migration chain."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory

from alembic import command
from provenance import population_document_processing as document_population

ROOT = Path(__file__).resolve().parents[1]
BASE_REVISION = "0213_decision_draft_provider_id"
HEAD = "0273_post_earnings_readout_budget"
ADDITIVE_TABLES_0245_0248 = {
    "document_processing_obligation_revisions",
    "document_processing_disposition_headers",
    "document_processing_disposition_members",
    "document_processing_disposition_seals",
    "document_processing_snapshot_headers",
    "document_processing_snapshot_members",
    "document_processing_snapshot_seals",
    "research_snapshot_headers",
    "research_snapshot_members",
    "research_snapshot_seals",
    "source_fact_publication_stream_clock",
    "source_fact_publication_stream",
    "canonical_fact_resolution_snapshot_watermarks",
    "canonical_fact_projection_generations",
    "canonical_fact_projection_entries",
    "canonical_fact_projection_batches",
    "canonical_fact_projection_buckets",
    "canonical_fact_projection_seals",
    "canonical_fact_projection_audit_receipts",
    "research_snapshot_admission_receipts",
    "heterogeneous_retrieval_trace_headers",
    "heterogeneous_retrieval_trace_candidates",
    "heterogeneous_retrieval_trace_results",
    "heterogeneous_retrieval_trace_seals",
    "document_processing_evidence_headers",
    "document_processing_evidence_members",
    "document_processing_evidence_seals",
    "pdf_table_extraction_artifact_headers",
    "pdf_table_extraction_artifact_members",
    "pdf_table_extraction_artifact_seals",
}
ADDITIVE_TABLES_0261 = {
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
ADDITIVE_TABLES_0264 = {
    "database_runtime_identity",
    "document_processing_operation_ledger",
}
ADDITIVE_TABLES_0265 = {"metric_ontology_operation_ledger"}
ADDITIVE_TABLES_0266 = {"canonical_resolution_operation_ledger"}
ADDITIVE_TABLES_0268_0269 = {
    "latest_governed_population_operation_ledger",
    "latest_governed_population_operation_ledger_v2",
}
ADDITIVE_TABLES_0272 = {
    "archive_generations",
    "archive_generation_table_commitments",
    "archive_generation_registration_receipts",
}


def _config(path: Path) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path}")
    return config


def test_evidence_search_migrations_have_one_reversible_head(tmp_path: Path) -> None:
    path = tmp_path / "migration-chain.db"
    config = _config(path)

    assert ScriptDirectory.from_config(config).get_heads() == [HEAD]
    legacy = sqlite3.connect(path)
    try:
        legacy.executescript(
            """
            CREATE TABLE financial_facts (
                id INTEGER PRIMARY KEY,
                source_doc_id INTEGER NOT NULL,
                supersedes_id INTEGER REFERENCES financial_facts(id)
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
    command.stamp(config, BASE_REVISION)
    command.upgrade(config, "head")

    conn = sqlite3.connect(path)
    try:
        revision = conn.execute("SELECT version_num FROM alembic_version").fetchone()
        tables = {
            str(row[0])
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        assert revision == (HEAD,)
        assert {
            "evidence_content_blobs",
            "reported_observations",
            "search_corpus_manifests",
            "source_inventory_snapshots",
            "source_inventory_snapshot_seals",
            "ask_retrieval_traces",
            "population_run_headers",
            "population_plane_receipts",
            "population_parity_receipts",
            "population_cutover_receipts",
            "ocr_document_assessments",
            "search_embedding_model_promotions",
            "expected_document_lifecycle_revisions",
            "fact_observation_revisions",
            "fact_resolution_outcomes",
            "issuer_entities",
            "issuer_identifier_assertions",
            "issuer_identifier_resolution_outcomes",
            "securities",
            "security_listing_assertions",
            "security_listing_resolution_outcomes",
            "issuer_authority_surface_revisions",
            "issuer_reporting_scope_revisions",
            "legacy_issuer_binding_revisions",
            "reporting_entities",
            "reporting_entity_identifier_assertions",
            "reporting_entity_identifier_resolution_outcomes",
            "security_identifier_assertions",
            "security_identifier_resolution_outcomes",
            "security_reporting_entity_revisions",
            "source_obligation_revisions",
            "recorded_subject_binding_revisions",
            "legacy_document_evidence_binding_revisions",
            "document_semantic_disposition_revisions",
            "search_projection_seals",
            "image_ocr_assessments",
            "image_ocr_extraction_governance",
            "image_ocr_results",
            "legacy_fact_evidence_match_revisions",
            "fact_observation_match_proofs",
            "fact_cells_v2",
            "fact_observations_v2",
            "fact_observation_relations_v2",
            "fact_resolution_candidates_v2",
            "fact_resolution_revisions_v2",
            "fact_derivation_input_edges_v2",
            "fact_derivation_seals_v2",
            "search_fact_projection_runs",
            "search_fact_projection_memberships",
            "search_fact_projection_rows",
            "search_fact_projection_seals",
            "ask_retrieval_trace_hits",
            "fact_dimensions_normalized_v2",
            "fact_cell_identity_seals_v2",
            "fact_reported_observation_anchors_v2",
            "fact_observation_payload_commitments_v2",
            "fact_derivation_basis_commitments_v2",
            "fact_extraction_run_completeness_seals_v2",
            "source_fact_publications",
            "source_fact_publication_members",
            "source_fact_publication_seals",
            "filing_xbrl_extraction_dispositions",
            "filing_xbrl_extraction_disposition_seals",
            "source_taxonomy_components",
            "canonical_metric_definition_revisions",
            "metric_mapping_revisions",
            "canonical_metric_cells",
            "fact_cell_canonical_binding_revisions",
            "ontology_snapshot_seals",
            "ontology_snapshot_members",
            "canonical_fact_candidate_universe_revisions",
            "canonical_fact_candidate_dispositions",
            "canonical_fact_relation_set_revisions",
            "canonical_fact_relation_assertions",
            "canonical_fact_resolution_revisions",
            "canonical_fact_resolution_snapshot_seals",
            "canonical_fact_resolution_snapshot_members",
        } <= tables
        anchor_indexes = {
            str(row[1])
            for row in conn.execute("PRAGMA index_list('fact_reported_observation_anchors_v2')")
        }
        assert "ix_fact_reported_anchors_v2_extraction_observation" in anchor_indexes
        assert conn.execute(
            "PRAGMA index_info('ix_0270_financial_facts_supersedes_id')"
        ).fetchall() == [(0, 2, "supersedes_id")]
        assert tables >= ADDITIVE_TABLES_0245_0248
        assert tables >= ADDITIVE_TABLES_0261
        assert tables >= ADDITIVE_TABLES_0264
        assert tables >= ADDITIVE_TABLES_0265
        assert tables >= ADDITIVE_TABLES_0266
        assert tables >= ADDITIVE_TABLES_0268_0269
        assert tables >= ADDITIVE_TABLES_0272
        read_set_sha = document_population._input_commitment(
            conn,
            datetime(2026, 7, 31, tzinfo=UTC),
            datetime(2026, 7, 31, tzinfo=UTC),
            (),
        )
        assert len(read_set_sha) == 64
        identity_row = conn.execute(
            "SELECT database_instance_id FROM database_runtime_identity WHERE singleton=1"
        ).fetchone()
        assert identity_row is not None
        assert str(identity_row[0]).startswith("database-instance:")
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            conn.execute(
                "UPDATE database_runtime_identity SET database_instance_id=? WHERE singleton=1",
                ("database-instance:" + "f" * 32,),
            )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO document_processing_operation_ledger VALUES (?,?,?,?,?,?,?)",
                (
                    "document-processing-operation:" + "a" * 64,
                    "document-processing-operation:" + "a" * 64,
                    str(identity_row[0]),
                    "b" * 64,
                    "c" * 64,
                    "d" * 64,
                    "{}",
                ),
            )
        view_sql = str(
            conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='view' "
                "AND name='v_population_cutover_current'"
            ).fetchone()[0]
        ).upper()
        assert "ORDER BY DATETIME(RUN.KNOWLEDGE_CUTOFF) DESC" in view_sql
        assert "DATETIME(RUN.OBSERVED_THROUGH) DESC" in view_sql
        assert "DATETIME(RECEIPT.SEALED_AT) DESC" in view_sql
        assert "RECEIPT.POPULATION_RUN_ID DESC LIMIT 1" in view_sql
        plane_trigger_sql = str(
            conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='trigger' "
                "AND name='trg_population_plane_receipt_exact'"
            ).fetchone()[0]
        )
        assert "$.result" in plane_trigger_sql
        assert "fact_sha256(json_extract" in plane_trigger_sql
        audit_trigger_sql = str(
            conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='trigger' "
                "AND name='trg_population_cutover_audit_receipt_exact'"
            ).fetchone()[0]
        )
        assert "$.gate_evidence" in audit_trigger_sql
        assert "$.watermark_material" in audit_trigger_sql
        assert "<>13" in audit_trigger_sql
        watermark_trigger_sql = str(
            conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='trigger' "
                "AND name='trg_canonical_resolution_snapshot_watermark_exact'"
            ).fetchone()[0]
        )
        assert "included.assigned_at" in watermark_trigger_sql
        assert "event.assigned_at" in watermark_trigger_sql
        assert "later.assigned_at" in watermark_trigger_sql
        assert "NEW.recorded_at" in watermark_trigger_sql
        ask_promotion_columns = {
            str(row[1]) for row in conn.execute("PRAGMA table_info(ask_retrieval_scope_promotions)")
        }
        assert {
            "population_run_id",
            "population_receipt_set_sha256",
            "population_observed_through",
        } <= ask_promotion_columns
        ask_population_trigger_sql = str(
            conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='trigger' "
                "AND name="
                "'trg_ask_retrieval_scope_promotion_population_cutover'"
            ).fetchone()[0]
        )
        assert "population_run_headers" in ask_population_trigger_sql
        assert "population_cutover_receipts" in ask_population_trigger_sql
        assert "v_population_cutover_current" in ask_population_trigger_sql
        trace_population_columns = {
            str(row[1])
            for row in conn.execute("PRAGMA table_info(heterogeneous_retrieval_trace_headers)")
        }
        assert {
            "population_run_id",
            "population_receipt_set_sha256",
            "population_observed_through",
        } <= trace_population_columns
        trace_population_trigger_sql = str(
            conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='trigger' "
                "AND name='trg_heterogeneous_trace_population_cutover'"
            ).fetchone()[0]
        )
        assert "population_run_headers" in trace_population_trigger_sql
        assert "population_cutover_receipts" in trace_population_trigger_sql
        assert "NEW.population_observed_through" in trace_population_trigger_sql
        with pytest.raises(
            sqlite3.IntegrityError,
            match="heterogeneous trace population cutover mismatch",
        ):
            conn.execute(
                "INSERT INTO heterogeneous_retrieval_trace_headers ("
                "trace_id,idempotency_key,research_snapshot_id,"
                "research_snapshot_sha256,fact_generation_id,"
                "fact_projection_seal_sha256,narrative_commitments_json,"
                "narrative_commitments_sha256,semantic_receipts_json,"
                "semantic_receipts_sha256,query_sha256,query_json,ranker_json,"
                "ranker_sha256,filters_json,filters_sha256,candidate_limit,"
                "result_limit,cutoff_at,recorded_at,population_run_id,"
                "population_receipt_set_sha256,population_observed_through"
                ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "trace:invalid-population",
                    "trace:invalid-population",
                    "research:missing",
                    "a" * 64,
                    "generation:missing",
                    "b" * 64,
                    "[]",
                    "c" * 64,
                    "[]",
                    "d" * 64,
                    "e" * 64,
                    "{}",
                    "{}",
                    "f" * 64,
                    "{}",
                    "0" * 64,
                    1,
                    1,
                    "2026-07-29 12:00:00",
                    "2026-07-29 12:00:00",
                    "population-run:missing",
                    "1" * 64,
                    "2026-07-29 12:00:00",
                ),
            )
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        conn.close()

    downgrade_checks = (
        (
            "0247_bounded_canonical_retrieval",
            "document_processing_evidence_headers",
            "canonical_fact_projection_generations",
        ),
        (
            "0246_source_fact_publication_stream",
            "canonical_fact_projection_generations",
            "source_fact_publication_stream",
        ),
        (
            "0245_document_processing_research_snapshots",
            "source_fact_publication_stream",
            "document_processing_obligation_revisions",
        ),
        (
            "0244_canonical_fact_resolution",
            "document_processing_obligation_revisions",
            "canonical_fact_candidate_universe_revisions",
        ),
        (
            "0243_metric_ontology",
            "canonical_fact_candidate_universe_revisions",
            "canonical_metric_definition_revisions",
        ),
    )
    for revision, removed_table, retained_table in downgrade_checks:
        command.downgrade(config, revision)
        conn = sqlite3.connect(path)
        try:
            assert conn.execute("SELECT version_num FROM alembic_version").fetchone() == (revision,)
            tables = {
                str(row[0])
                for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
            }
            assert removed_table not in tables
            assert retained_table in tables
            if revision == "0247_bounded_canonical_retrieval":
                watermark_trigger_sql = str(
                    conn.execute(
                        "SELECT sql FROM sqlite_master WHERE type='trigger' "
                        "AND name="
                        "'trg_canonical_resolution_snapshot_watermark_exact'"
                    ).fetchone()[0]
                )
                assert "assigned_at" not in watermark_trigger_sql
            assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        finally:
            conn.close()

    command.downgrade(config, BASE_REVISION)
    conn = sqlite3.connect(path)
    try:
        assert conn.execute("SELECT version_num FROM alembic_version").fetchone() == (
            BASE_REVISION,
        )
        assert (
            conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' "
                "AND name = 'evidence_content_blobs'"
            ).fetchone()
            is None
        )
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        conn.close()
