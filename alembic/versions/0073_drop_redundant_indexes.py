"""Drop verified exact-duplicate and strict-prefix redundant SQLite indexes.

Every dropped index is either a 100% column/order/collation duplicate of an
existing UNIQUE autoindex or a strict leading prefix of a UNIQUE index, with
zero rows touched, zero ``INDEXED BY`` references, and zero assertions in
``integrity_audit.py``.

Revision ID: 0073_drop_redundant_indexes
Revises: 0072_evidence_node_external_archives
"""

from __future__ import annotations

from alembic import op

revision = "0073_drop_redundant_indexes"
down_revision = "0072_evidence_node_external_archives"
branch_labels = None
depends_on = None

# Ordered tuple of (index_name, exact_recreate_ddl_for_downgrade).
# Intentionally excludes ix_fact_resolution_revisions_v2_current and
# ix_fact_derivation_edges_v2_output, which provenance/integrity_audit.py asserts.
_REDUNDANT_INDEXES: tuple[tuple[str, str], ...] = (
    # Primary high-impact redundant indexes (~200-500+ MB on canonical host).
    (
        "ix_evidence_nodes_key_revision",
        "CREATE INDEX IF NOT EXISTS ix_evidence_nodes_key_revision "
        "ON evidence_nodes (evidence_key, revision)",
    ),
    (
        "ix_source_coverage_expected_revision",
        "CREATE INDEX IF NOT EXISTS ix_source_coverage_expected_revision "
        "ON source_coverage_assessments (expected_document_id, revision)",
    ),
    (
        "ix_expected_document_lifecycle_current",
        "CREATE INDEX IF NOT EXISTS ix_expected_document_lifecycle_current "
        "ON expected_document_lifecycle_revisions "
        "(inventory_key, expected_document_key, revision)",
    ),
    (
        "ix_filing_section_items_section",
        "CREATE INDEX IF NOT EXISTS ix_filing_section_items_section "
        "ON filing_section_items (section_id)",
    ),
    (
        "idx_financial_facts_restatement_lookup",
        "CREATE INDEX IF NOT EXISTS idx_financial_facts_restatement_lookup "
        "ON financial_facts (ticker, period_end, fiscal_period_type, line_item)",
    ),
    # Additional verified exact UNIQUE-duplicate indexes from 0001_initial_schema.py.
    (
        "idx_llm_budgets_purpose",
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_llm_budgets_purpose ON llm_budgets (purpose)",
    ),
    (
        "idx_macro_series_lookup",
        "CREATE INDEX IF NOT EXISTS idx_macro_series_lookup ON macro_series (series_id, rate_date)",
    ),
    (
        "ix_ask_retrieval_scope_promotion_current",
        "CREATE INDEX IF NOT EXISTS ix_ask_retrieval_scope_promotion_current "
        "ON ask_retrieval_scope_promotions (scope_key, revision)",
    ),
    (
        "ix_canonical_fact_projection_entry_keyset",
        "CREATE INDEX IF NOT EXISTS ix_canonical_fact_projection_entry_keyset "
        "ON canonical_fact_projection_entries (generation_id, canonical_metric_cell_id)",
    ),
    (
        "ix_canonical_fact_universe_coordinate_cutoff",
        "CREATE INDEX IF NOT EXISTS ix_canonical_fact_universe_coordinate_cutoff "
        "ON canonical_fact_candidate_universe_revisions (canonical_metric_cell_id, cutoff_at)",
    ),
    (
        "ix_commitment_scan_log_transcript",
        "CREATE UNIQUE INDEX IF NOT EXISTS ix_commitment_scan_log_transcript "
        "ON commitment_scan_log (transcript_id)",
    ),
    (
        "ix_document_processing_evidence_coordinate",
        "CREATE INDEX IF NOT EXISTS ix_document_processing_evidence_coordinate "
        "ON document_processing_evidence_headers (document_version_id, processing_lane, cutoff_at)",
    ),
    (
        "ix_document_semantic_disposition_current",
        "CREATE INDEX IF NOT EXISTS ix_document_semantic_disposition_current "
        "ON document_semantic_disposition_revisions (document_version_id, revision)",
    ),
    (
        "ix_evidence_location_current_lookup",
        "CREATE INDEX IF NOT EXISTS ix_evidence_location_current_lookup "
        "ON evidence_blob_location_observations (blob_sha256, storage_uri, location_sequence)",
    ),
    (
        "ix_fact_selection_target_revision",
        "CREATE INDEX IF NOT EXISTS ix_fact_selection_target_revision "
        "ON fact_selection_decisions (target_table, target_row_id, revision)",
    ),
    (
        "ix_filing_xbrl_dispositions_run_order",
        "CREATE UNIQUE INDEX IF NOT EXISTS ix_filing_xbrl_dispositions_run_order "
        "ON filing_xbrl_extraction_dispositions (extraction_run_id, input_ordinal)",
    ),
    (
        "ix_issuer_authority_surface_current",
        "CREATE INDEX IF NOT EXISTS ix_issuer_authority_surface_current "
        "ON issuer_authority_surface_revisions (issuer_id, surface_key, revision)",
    ),
    (
        "ix_issuer_identifier_resolution_current",
        "CREATE INDEX IF NOT EXISTS ix_issuer_identifier_resolution_current "
        "ON issuer_identifier_resolution_outcomes (resolution_key, revision)",
    ),
    (
        "ix_issuer_profile_current",
        "CREATE INDEX IF NOT EXISTS ix_issuer_profile_current "
        "ON issuer_profile_revisions (issuer_id, revision)",
    ),
    (
        "ix_issuer_reporting_scope_current",
        "CREATE INDEX IF NOT EXISTS ix_issuer_reporting_scope_current "
        "ON issuer_reporting_scope_revisions (scope_key, issuer_id, revision)",
    ),
    (
        "ix_legacy_document_evidence_binding_current",
        "CREATE INDEX IF NOT EXISTS ix_legacy_document_evidence_binding_current "
        "ON legacy_document_evidence_binding_revisions (legacy_document_id, revision)",
    ),
    (
        "ix_legacy_issuer_binding_current",
        "CREATE INDEX IF NOT EXISTS ix_legacy_issuer_binding_current "
        "ON legacy_issuer_binding_revisions (recorded_issuer_id, revision)",
    ),
    (
        "ix_observation_resolution_logical_revision",
        "CREATE INDEX IF NOT EXISTS ix_observation_resolution_logical_revision "
        "ON observation_resolution_revisions (logical_key, revision)",
    ),
    (
        "ix_recorded_subject_binding_current",
        "CREATE INDEX IF NOT EXISTS ix_recorded_subject_binding_current "
        "ON recorded_subject_binding_revisions (recorded_issuer_id, revision)",
    ),
    (
        "ix_reporting_entity_identifier_resolution_outcomes_current",
        "CREATE INDEX IF NOT EXISTS ix_reporting_entity_identifier_resolution_outcomes_current "
        "ON reporting_entity_identifier_resolution_outcomes (resolution_key, revision)",
    ),
    (
        "ix_security_identifier_resolution_outcomes_current",
        "CREATE INDEX IF NOT EXISTS ix_security_identifier_resolution_outcomes_current "
        "ON security_identifier_resolution_outcomes (resolution_key, revision)",
    ),
    (
        "ix_security_listing_resolution_current",
        "CREATE INDEX IF NOT EXISTS ix_security_listing_resolution_current "
        "ON security_listing_resolution_outcomes (resolution_key, revision)",
    ),
    (
        "ix_security_reporting_entity_current",
        "CREATE INDEX IF NOT EXISTS ix_security_reporting_entity_current "
        "ON security_reporting_entity_revisions (relationship_key, revision)",
    ),
    (
        "ix_source_fact_publication_members_order",
        "CREATE UNIQUE INDEX IF NOT EXISTS ix_source_fact_publication_members_order "
        "ON source_fact_publication_members (publication_id, member_ordinal)",
    ),
    (
        "ix_source_inventory_component_snapshot",
        "CREATE INDEX IF NOT EXISTS ix_source_inventory_component_snapshot "
        "ON source_inventory_components (snapshot_id, ordinal)",
    ),
)


def upgrade() -> None:
    for index_name, _ in _REDUNDANT_INDEXES:
        op.execute(f"DROP INDEX IF EXISTS {index_name}")


def downgrade() -> None:
    for _, create_sql in reversed(_REDUNDANT_INDEXES):
        op.execute(create_sql)
