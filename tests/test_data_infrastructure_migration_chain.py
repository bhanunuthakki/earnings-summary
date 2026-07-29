"""The additive evidence/search roadmap remains one reversible migration chain."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

from alembic import command

ROOT = Path(__file__).resolve().parents[1]
BASE_REVISION = "0213_decision_draft_provider_id"
HEAD = "0255_scoped_canonical_resolution_snapshots"
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
        assert tables >= ADDITIVE_TABLES_0245_0248
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
