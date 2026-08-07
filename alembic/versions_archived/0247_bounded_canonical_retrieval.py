"""Add bounded canonical-fact projections and heterogeneous retrieval traces.

Revision ID: 0247_bounded_canonical_retrieval
Revises: 0246_source_fact_publication_stream
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0247_bounded_canonical_retrieval"
down_revision: str | Sequence[str] | None = "0246_source_fact_publication_stream"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLES = (
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
)


def _hex(column: str) -> str:
    return f"length({column})=64 AND {column} NOT GLOB '*[^0-9a-f]*'"


def _append_only(table: str) -> None:
    op.execute(
        f"CREATE TRIGGER trg_{table}_append_only BEFORE UPDATE ON {table} "
        f"BEGIN SELECT RAISE(ABORT, '{table} is append-only'); END"
    )
    op.execute(
        f"CREATE TRIGGER trg_{table}_append_only_delete BEFORE DELETE ON {table} "
        f"BEGIN SELECT RAISE(ABORT, '{table} is append-only'); END"
    )


def upgrade() -> None:
    bind = op.get_bind()
    required = {
        "canonical_fact_resolution_snapshot_seals",
        "canonical_fact_resolution_snapshot_watermarks",
        "ontology_snapshot_seals",
        "research_snapshot_seals",
        "search_corpus_manifest_seals",
        "search_projection_seals",
    }
    missing = sorted(required - set(sa.inspect(bind).get_table_names()))
    if missing:
        raise RuntimeError(
            "bounded canonical retrieval requires sealed 0243-0246 state: "
            + ", ".join(missing)
        )

    op.create_table(
        "canonical_fact_projection_generations",
        sa.Column("generation_id", sa.String(128), primary_key=True),
        sa.Column("idempotency_key", sa.String(256), nullable=False, unique=True),
        sa.Column("generation_kind", sa.String(16), nullable=False),
        sa.Column(
            "parent_generation_id",
            sa.String(128),
            sa.ForeignKey("canonical_fact_projection_generations.generation_id"),
        ),
        sa.Column("delta_depth", sa.Integer, nullable=False),
        sa.Column(
            "resolution_snapshot_id",
            sa.String(128),
            sa.ForeignKey(
                "canonical_fact_resolution_snapshot_seals.resolution_snapshot_id"
            ),
            nullable=False,
        ),
        sa.Column("resolution_snapshot_sha256", sa.String(64), nullable=False),
        sa.Column("resolution_watermark_sha256", sa.String(64), nullable=False),
        sa.Column(
            "ontology_snapshot_id",
            sa.String(128),
            sa.ForeignKey("ontology_snapshot_seals.ontology_snapshot_id"),
            nullable=False,
        ),
        sa.Column("ontology_snapshot_sha256", sa.String(64), nullable=False),
        sa.Column("cutoff_at", sa.DateTime, nullable=False),
        sa.Column("config_json", sa.Text, nullable=False),
        sa.Column("config_sha256", sa.String(64), nullable=False),
        sa.Column("digest_bucket_count", sa.Integer, nullable=False),
        sa.Column("max_batch_facts", sa.Integer, nullable=False),
        sa.Column("max_batch_bytes", sa.Integer, nullable=False),
        sa.Column("max_batch_milliseconds", sa.Integer, nullable=False),
        sa.Column("generation_json", sa.Text, nullable=False),
        sa.Column("generation_sha256", sa.String(64), nullable=False),
        sa.Column("recorded_at", sa.DateTime, nullable=False),
        sa.CheckConstraint(
            "generation_kind IN ('checkpoint','delta') "
            "AND ((generation_kind='checkpoint' AND parent_generation_id IS NULL "
            "AND delta_depth=0) OR (generation_kind='delta' "
            "AND parent_generation_id IS NOT NULL AND delta_depth>0))",
            name="ck_canonical_fact_projection_generation_chain",
        ),
        sa.CheckConstraint(
            "digest_bucket_count=4096 AND max_batch_facts BETWEEN 1 AND 1000 "
            "AND max_batch_bytes BETWEEN 1024 AND 16777216 "
            "AND max_batch_milliseconds BETWEEN 1 AND 1000",
            name="ck_canonical_fact_projection_generation_bounds",
        ),
        sa.CheckConstraint(
            "json_valid(config_json) AND json_type(config_json)='object' "
            "AND json_valid(generation_json) AND json_type(generation_json)='object'",
            name="ck_canonical_fact_projection_generation_json",
        ),
        sa.CheckConstraint(
            _hex("resolution_snapshot_sha256")
            + " AND "
            + _hex("resolution_watermark_sha256")
            + " AND "
            + _hex("ontology_snapshot_sha256")
            + " AND "
            + _hex("config_sha256")
            + " AND "
            + _hex("generation_sha256"),
            name="ck_canonical_fact_projection_generation_hashes",
        ),
        sa.CheckConstraint(
            "recorded_at>=cutoff_at",
            name="ck_canonical_fact_projection_generation_clocks",
        ),
    )
    op.create_table(
        "canonical_fact_projection_entries",
        sa.Column(
            "generation_id",
            sa.String(128),
            sa.ForeignKey("canonical_fact_projection_generations.generation_id"),
            primary_key=True,
        ),
        sa.Column("entry_ordinal", sa.Integer, primary_key=True),
        sa.Column("change_kind", sa.String(16), nullable=False),
        sa.Column("digest_bucket", sa.Integer, nullable=False),
        sa.Column(
            "canonical_metric_cell_id",
            sa.String(128),
            sa.ForeignKey("canonical_metric_cells.canonical_metric_cell_id"),
            nullable=False,
        ),
        sa.Column(
            "canonical_resolution_revision_id",
            sa.String(128),
            sa.ForeignKey(
                "canonical_fact_resolution_revisions."
                "canonical_resolution_revision_id"
            ),
        ),
        sa.Column(
            "selected_observation_id",
            sa.String(128),
            sa.ForeignKey("fact_observations_v2.observation_id"),
        ),
        sa.Column(
            "source_fact_cell_id",
            sa.String(128),
            sa.ForeignKey("fact_cells_v2.fact_cell_id"),
        ),
        sa.Column(
            "source_publication_id",
            sa.String(128),
            sa.ForeignKey("source_fact_publications.publication_id"),
        ),
        sa.Column(
            "source_publication_seal_id",
            sa.String(128),
            sa.ForeignKey("source_fact_publication_seals.publication_seal_id"),
        ),
        sa.Column(
            "source_publication_member_id",
            sa.String(128),
            sa.ForeignKey(
                "source_fact_publication_members.publication_member_id"
            ),
        ),
        sa.Column("source_publication_member_sha256", sa.String(64)),
        sa.Column("source_record_commitment_sha256", sa.String(64)),
        sa.Column(
            "binding_revision_id",
            sa.String(128),
            sa.ForeignKey(
                "fact_cell_canonical_binding_revisions.binding_revision_id"
            ),
        ),
        sa.Column("binding_commitment_sha256", sa.String(64)),
        sa.Column(
            "mapping_revision_id",
            sa.String(128),
            sa.ForeignKey("metric_mapping_revisions.mapping_revision_id"),
        ),
        sa.Column("mapping_commitment_sha256", sa.String(64)),
        sa.Column(
            "metric_definition_revision_id",
            sa.String(128),
            sa.ForeignKey(
                "canonical_metric_definition_revisions."
                "metric_definition_revision_id"
            ),
        ),
        sa.Column("metric_definition_commitment_sha256", sa.String(64)),
        sa.Column("reporting_entity_id", sa.String(128)),
        sa.Column("scope_security_id", sa.String(128)),
        sa.Column("canonical_metric_name", sa.Text),
        sa.Column("period_kind", sa.String(16)),
        sa.Column("period_start", sa.Text),
        sa.Column("period_end", sa.Text),
        sa.Column("dimensions_json", sa.Text),
        sa.Column("unit_key", sa.Text),
        sa.Column("currency", sa.String(3)),
        sa.Column("value_kind", sa.String(16)),
        sa.Column("canonical_value", sa.Text),
        sa.Column(
            "evidence_document_version_id",
            sa.String(128),
            sa.ForeignKey("evidence_document_versions.document_version_id"),
        ),
        sa.Column(
            "evidence_node_id",
            sa.String(128),
            sa.ForeignKey("evidence_nodes.node_id"),
        ),
        sa.Column("evidence_locator_json", sa.Text),
        sa.Column("evidence_locator_sha256", sa.String(64)),
        sa.Column("canonical_search_text", sa.Text),
        sa.Column("entry_json", sa.Text, nullable=False),
        sa.Column("entry_sha256", sa.String(64), nullable=False),
        sa.UniqueConstraint(
            "generation_id",
            "canonical_metric_cell_id",
            name="uq_canonical_fact_projection_generation_coordinate",
        ),
        sa.CheckConstraint(
            "entry_ordinal>=0 AND digest_bucket BETWEEN 0 AND 4095 "
            "AND change_kind IN ('upsert','delete') "
            "AND ((change_kind='delete' "
            "AND canonical_resolution_revision_id IS NULL "
            "AND selected_observation_id IS NULL "
            "AND source_fact_cell_id IS NULL "
            "AND source_publication_id IS NULL "
            "AND source_publication_seal_id IS NULL "
            "AND source_publication_member_id IS NULL "
            "AND source_publication_member_sha256 IS NULL "
            "AND source_record_commitment_sha256 IS NULL "
            "AND binding_revision_id IS NULL "
            "AND binding_commitment_sha256 IS NULL "
            "AND mapping_revision_id IS NULL "
            "AND mapping_commitment_sha256 IS NULL "
            "AND metric_definition_revision_id IS NULL "
            "AND metric_definition_commitment_sha256 IS NULL "
            "AND reporting_entity_id IS NULL "
            "AND scope_security_id IS NULL "
            "AND canonical_metric_name IS NULL "
            "AND period_kind IS NULL "
            "AND period_start IS NULL "
            "AND period_end IS NULL "
            "AND dimensions_json IS NULL "
            "AND unit_key IS NULL "
            "AND currency IS NULL "
            "AND value_kind IS NULL "
            "AND canonical_value IS NULL "
            "AND evidence_document_version_id IS NULL "
            "AND evidence_node_id IS NULL "
            "AND evidence_locator_json IS NULL "
            "AND evidence_locator_sha256 IS NULL "
            "AND canonical_search_text IS NULL) "
            "OR (change_kind='upsert' "
            "AND canonical_resolution_revision_id IS NOT NULL "
            "AND selected_observation_id IS NOT NULL "
            "AND source_publication_id IS NOT NULL "
            "AND source_publication_seal_id IS NOT NULL "
            "AND source_publication_member_id IS NOT NULL "
            "AND source_publication_member_sha256 IS NOT NULL "
            "AND source_record_commitment_sha256 IS NOT NULL "
            "AND binding_revision_id IS NOT NULL "
            "AND binding_commitment_sha256 IS NOT NULL "
            "AND mapping_revision_id IS NOT NULL "
            "AND mapping_commitment_sha256 IS NOT NULL "
            "AND metric_definition_revision_id IS NOT NULL "
            "AND metric_definition_commitment_sha256 IS NOT NULL "
            "AND canonical_metric_name IS NOT NULL "
            "AND evidence_document_version_id IS NOT NULL "
            "AND evidence_node_id IS NOT NULL "
            "AND evidence_locator_json IS NOT NULL "
            "AND evidence_locator_sha256 IS NOT NULL "
            "AND value_kind IN ('numeric','text','nil') "
            "AND canonical_search_text IS NOT NULL))",
            name="ck_canonical_fact_projection_entry_shape",
        ),
        sa.CheckConstraint(
            "json_valid(entry_json) AND json_type(entry_json)='object' "
            "AND (dimensions_json IS NULL OR "
            "(json_valid(dimensions_json) AND json_type(dimensions_json)='array')) "
            "AND (evidence_locator_json IS NULL OR "
            "(json_valid(evidence_locator_json) "
            "AND json_type(evidence_locator_json)='object'))",
            name="ck_canonical_fact_projection_entry_json",
        ),
        sa.CheckConstraint(
            _hex("entry_sha256")
            + " AND (source_publication_member_sha256 IS NULL OR ("
            + _hex("source_publication_member_sha256")
            + ")) AND (source_record_commitment_sha256 IS NULL OR ("
            + _hex("source_record_commitment_sha256")
            + ")) AND (binding_commitment_sha256 IS NULL OR ("
            + _hex("binding_commitment_sha256")
            + ")) AND (mapping_commitment_sha256 IS NULL OR ("
            + _hex("mapping_commitment_sha256")
            + ")) AND (metric_definition_commitment_sha256 IS NULL OR ("
            + _hex("metric_definition_commitment_sha256")
            + ")) AND (evidence_locator_sha256 IS NULL OR ("
            + _hex("evidence_locator_sha256")
            + "))",
            name="ck_canonical_fact_projection_entry_hashes",
        ),
    )
    op.create_index(
        "ix_canonical_fact_projection_entry_keyset",
        "canonical_fact_projection_entries",
        ["generation_id", "canonical_metric_cell_id"],
    )
    op.create_index(
        "ix_canonical_fact_projection_entry_metric_period",
        "canonical_fact_projection_entries",
        ["generation_id", "canonical_metric_name", "period_end"],
    )
    op.create_table(
        "canonical_fact_projection_batches",
        sa.Column(
            "generation_id",
            sa.String(128),
            sa.ForeignKey("canonical_fact_projection_generations.generation_id"),
            primary_key=True,
        ),
        sa.Column("batch_ordinal", sa.Integer, primary_key=True),
        sa.Column("first_entry_ordinal", sa.Integer, nullable=False),
        sa.Column("last_entry_ordinal", sa.Integer, nullable=False),
        sa.Column("first_coordinate", sa.String(128), nullable=False),
        sa.Column("last_coordinate", sa.String(128), nullable=False),
        sa.Column("entry_count", sa.Integer, nullable=False),
        sa.Column("serialized_bytes", sa.Integer, nullable=False),
        sa.Column("elapsed_milliseconds", sa.Integer, nullable=False),
        sa.Column("canonical_entry_set_json", sa.Text, nullable=False),
        sa.Column("entry_set_sha256", sa.String(64), nullable=False),
        sa.CheckConstraint(
            "batch_ordinal>=0 AND first_entry_ordinal>=0 "
            "AND last_entry_ordinal>=first_entry_ordinal "
            "AND entry_count=last_entry_ordinal-first_entry_ordinal+1 "
            "AND entry_count BETWEEN 1 AND 1000 "
            "AND serialized_bytes BETWEEN 1 AND 16777216 "
            "AND elapsed_milliseconds BETWEEN 0 AND 1000",
            name="ck_canonical_fact_projection_batch_bounds",
        ),
        sa.CheckConstraint(
            "json_valid(canonical_entry_set_json) "
            "AND json_type(canonical_entry_set_json)='array' "
            "AND " + _hex("entry_set_sha256"),
            name="ck_canonical_fact_projection_batch_commitment",
        ),
    )
    op.create_table(
        "canonical_fact_projection_buckets",
        sa.Column(
            "generation_id",
            sa.String(128),
            sa.ForeignKey("canonical_fact_projection_generations.generation_id"),
            primary_key=True,
        ),
        sa.Column("digest_bucket", sa.Integer, primary_key=True),
        sa.Column("entry_count", sa.Integer, nullable=False),
        sa.Column("canonical_entry_set_json", sa.Text, nullable=False),
        sa.Column("entry_set_sha256", sa.String(64), nullable=False),
        sa.CheckConstraint(
            "digest_bucket BETWEEN 0 AND 4095 AND entry_count>=0",
            name="ck_canonical_fact_projection_bucket_shape",
        ),
        sa.CheckConstraint(
            "json_valid(canonical_entry_set_json) "
            "AND json_type(canonical_entry_set_json)='array' "
            "AND " + _hex("entry_set_sha256"),
            name="ck_canonical_fact_projection_bucket_commitment",
        ),
    )
    op.create_table(
        "canonical_fact_projection_seals",
        sa.Column(
            "generation_id",
            sa.String(128),
            sa.ForeignKey("canonical_fact_projection_generations.generation_id"),
            primary_key=True,
        ),
        sa.Column("projection_seal_id", sa.String(128), nullable=False, unique=True),
        sa.Column("change_count", sa.Integer, nullable=False),
        sa.Column("upsert_count", sa.Integer, nullable=False),
        sa.Column("tombstone_count", sa.Integer, nullable=False),
        sa.Column("effective_entry_count", sa.Integer, nullable=False),
        sa.Column("batch_count", sa.Integer, nullable=False),
        sa.Column("stored_bucket_count", sa.Integer, nullable=False),
        sa.Column("logical_bucket_count", sa.Integer, nullable=False),
        sa.Column("ordered_batch_set_json", sa.Text, nullable=False),
        sa.Column("batch_set_sha256", sa.String(64), nullable=False),
        sa.Column("ordered_bucket_set_json", sa.Text, nullable=False),
        sa.Column("bucket_set_sha256", sa.String(64), nullable=False),
        sa.Column("ordered_entry_set_sha256", sa.String(64), nullable=False),
        sa.Column("projection_seal_json", sa.Text, nullable=False),
        sa.Column("projection_seal_sha256", sa.String(64), nullable=False),
        sa.Column("sealed_at", sa.DateTime, nullable=False),
        sa.CheckConstraint(
            "change_count>=0 AND upsert_count>=0 AND tombstone_count>=0 "
            "AND change_count=upsert_count+tombstone_count "
            "AND effective_entry_count>=0 AND batch_count>=0 "
            "AND stored_bucket_count BETWEEN 0 AND 4096 "
            "AND logical_bucket_count=4096",
            name="ck_canonical_fact_projection_seal_counts",
        ),
        sa.CheckConstraint(
            "json_valid(ordered_batch_set_json) "
            "AND json_type(ordered_batch_set_json)='array' "
            "AND json_valid(ordered_bucket_set_json) "
            "AND json_type(ordered_bucket_set_json)='array' "
            "AND json_valid(projection_seal_json) "
            "AND json_type(projection_seal_json)='object'",
            name="ck_canonical_fact_projection_seal_json",
        ),
        sa.CheckConstraint(
            _hex("batch_set_sha256")
            + " AND "
            + _hex("bucket_set_sha256")
            + " AND "
            + _hex("ordered_entry_set_sha256")
            + " AND "
            + _hex("projection_seal_sha256"),
            name="ck_canonical_fact_projection_seal_hashes",
        ),
    )
    op.create_table(
        "canonical_fact_projection_audit_receipts",
        sa.Column(
            "generation_id",
            sa.String(128),
            sa.ForeignKey("canonical_fact_projection_seals.generation_id"),
            primary_key=True,
        ),
        sa.Column("projection_seal_sha256", sa.String(64), nullable=False),
        sa.Column("verifier_name", sa.String(128), nullable=False),
        sa.Column("verifier_version", sa.String(64), nullable=False),
        sa.Column("verifier_code_sha256", sa.String(64), nullable=False),
        sa.Column("verifier_config_sha256", sa.String(64), nullable=False),
        sa.Column("audit_payload_json", sa.Text, nullable=False),
        sa.Column("audit_payload_sha256", sa.String(64), nullable=False),
        sa.Column("audited_at", sa.DateTime, nullable=False),
        sa.CheckConstraint(
            "json_valid(audit_payload_json) "
            "AND json_type(audit_payload_json)='object' "
            "AND " + _hex("projection_seal_sha256")
            + " AND " + _hex("verifier_code_sha256")
            + " AND " + _hex("verifier_config_sha256")
            + " AND " + _hex("audit_payload_sha256"),
            name="ck_canonical_fact_projection_audit_receipt",
        ),
    )
    op.create_table(
        "research_snapshot_admission_receipts",
        sa.Column(
            "research_snapshot_id",
            sa.String(128),
            sa.ForeignKey("research_snapshot_seals.research_snapshot_id"),
            primary_key=True,
        ),
        sa.Column("research_snapshot_sha256", sa.String(64), nullable=False),
        sa.Column("verifier_name", sa.String(128), nullable=False),
        sa.Column("verifier_version", sa.String(64), nullable=False),
        sa.Column("verifier_code_sha256", sa.String(64), nullable=False),
        sa.Column("verifier_config_sha256", sa.String(64), nullable=False),
        sa.Column("audit_payload_json", sa.Text, nullable=False),
        sa.Column("audit_payload_sha256", sa.String(64), nullable=False),
        sa.Column("audited_at", sa.DateTime, nullable=False),
        sa.CheckConstraint(
            "json_valid(audit_payload_json) "
            "AND json_type(audit_payload_json)='object' "
            "AND " + _hex("research_snapshot_sha256")
            + " AND " + _hex("verifier_code_sha256")
            + " AND " + _hex("verifier_config_sha256")
            + " AND " + _hex("audit_payload_sha256"),
            name="ck_research_snapshot_admission_receipt",
        ),
    )

    op.create_table(
        "heterogeneous_retrieval_trace_headers",
        sa.Column("trace_id", sa.String(128), primary_key=True),
        sa.Column("idempotency_key", sa.String(256), nullable=False, unique=True),
        sa.Column(
            "research_snapshot_id",
            sa.String(128),
            sa.ForeignKey("research_snapshot_seals.research_snapshot_id"),
            nullable=False,
        ),
        sa.Column("research_snapshot_sha256", sa.String(64), nullable=False),
        sa.Column(
            "fact_generation_id",
            sa.String(128),
            sa.ForeignKey("canonical_fact_projection_seals.generation_id"),
            nullable=False,
        ),
        sa.Column("fact_projection_seal_sha256", sa.String(64), nullable=False),
        sa.Column("narrative_commitments_json", sa.Text, nullable=False),
        sa.Column("narrative_commitments_sha256", sa.String(64), nullable=False),
        sa.Column("semantic_receipts_json", sa.Text, nullable=False),
        sa.Column("semantic_receipts_sha256", sa.String(64), nullable=False),
        sa.Column("query_sha256", sa.String(64), nullable=False),
        sa.Column("query_json", sa.Text, nullable=False),
        sa.Column("ranker_json", sa.Text, nullable=False),
        sa.Column("ranker_sha256", sa.String(64), nullable=False),
        sa.Column("filters_json", sa.Text, nullable=False),
        sa.Column("filters_sha256", sa.String(64), nullable=False),
        sa.Column("candidate_limit", sa.Integer, nullable=False),
        sa.Column("result_limit", sa.Integer, nullable=False),
        sa.Column("cutoff_at", sa.DateTime, nullable=False),
        sa.Column("recorded_at", sa.DateTime, nullable=False),
        sa.CheckConstraint(
            "candidate_limit BETWEEN 1 AND 1000 "
            "AND result_limit BETWEEN 1 AND candidate_limit",
            name="ck_heterogeneous_retrieval_trace_bounds",
        ),
        sa.CheckConstraint(
            "json_valid(query_json) AND json_type(query_json)='object' "
            "AND json_valid(narrative_commitments_json) "
            "AND json_type(narrative_commitments_json)='array' "
            "AND json_valid(semantic_receipts_json) "
            "AND json_type(semantic_receipts_json)='array' "
            "AND json_valid(ranker_json) AND json_type(ranker_json)='object' "
            "AND json_valid(filters_json) AND json_type(filters_json)='object'",
            name="ck_heterogeneous_retrieval_trace_json",
        ),
        sa.CheckConstraint(
            _hex("research_snapshot_sha256")
            + " AND "
            + _hex("fact_projection_seal_sha256")
            + " AND "
            + _hex("narrative_commitments_sha256")
            + " AND "
            + _hex("semantic_receipts_sha256")
            + " AND "
            + _hex("query_sha256")
            + " AND "
            + _hex("ranker_sha256")
            + " AND "
            + _hex("filters_sha256"),
            name="ck_heterogeneous_retrieval_trace_hashes",
        ),
        sa.CheckConstraint(
            "recorded_at>=cutoff_at",
            name="ck_heterogeneous_retrieval_trace_clocks",
        ),
    )
    op.create_table(
        "heterogeneous_retrieval_trace_candidates",
        sa.Column(
            "trace_id",
            sa.String(128),
            sa.ForeignKey("heterogeneous_retrieval_trace_headers.trace_id"),
            primary_key=True,
        ),
        sa.Column("candidate_ordinal", sa.Integer, primary_key=True),
        sa.Column("candidate_kind", sa.String(16), nullable=False),
        sa.Column("candidate_id", sa.String(128), nullable=False),
        sa.Column("source_commitment_sha256", sa.String(64), nullable=False),
        sa.Column("lexical_score", sa.Text),
        sa.Column("semantic_score", sa.Text),
        sa.Column("normalized_score", sa.Text, nullable=False),
        sa.Column("ranker_name", sa.String(128), nullable=False),
        sa.Column("filter_outcome", sa.String(16), nullable=False),
        sa.Column("filter_reason", sa.String(128)),
        sa.Column("evidence_locator_json", sa.Text, nullable=False),
        sa.Column("lineage_json", sa.Text, nullable=False),
        sa.Column("lineage_sha256", sa.String(64), nullable=False),
        sa.Column("candidate_json", sa.Text, nullable=False),
        sa.Column("candidate_sha256", sa.String(64), nullable=False),
        sa.UniqueConstraint(
            "trace_id",
            "candidate_kind",
            "candidate_id",
            name="uq_heterogeneous_retrieval_trace_candidate",
        ),
        sa.CheckConstraint(
            "candidate_ordinal>=0 AND candidate_kind IN ('narrative','fact') "
            "AND filter_outcome IN ('included','filtered') "
            "AND ((filter_outcome='included' AND filter_reason IS NULL) "
            "OR (filter_outcome='filtered' AND filter_reason IS NOT NULL))",
            name="ck_heterogeneous_retrieval_candidate_shape",
        ),
        sa.CheckConstraint(
            "json_valid(evidence_locator_json) "
            "AND json_type(evidence_locator_json)='object' "
            "AND json_valid(lineage_json) AND json_type(lineage_json)='object' "
            "AND json_valid(candidate_json) AND json_type(candidate_json)='object'",
            name="ck_heterogeneous_retrieval_candidate_json",
        ),
        sa.CheckConstraint(
            _hex("source_commitment_sha256")
            + " AND "
            + _hex("lineage_sha256")
            + " AND "
            + _hex("candidate_sha256"),
            name="ck_heterogeneous_retrieval_candidate_hashes",
        ),
    )
    op.create_table(
        "heterogeneous_retrieval_trace_results",
        sa.Column(
            "trace_id",
            sa.String(128),
            sa.ForeignKey("heterogeneous_retrieval_trace_headers.trace_id"),
            primary_key=True,
        ),
        sa.Column("result_ordinal", sa.Integer, primary_key=True),
        sa.Column("candidate_ordinal", sa.Integer, nullable=False),
        sa.Column("final_score", sa.Text, nullable=False),
        sa.Column("result_json", sa.Text, nullable=False),
        sa.Column("result_sha256", sa.String(64), nullable=False),
        sa.ForeignKeyConstraint(
            ["trace_id", "candidate_ordinal"],
            [
                "heterogeneous_retrieval_trace_candidates.trace_id",
                "heterogeneous_retrieval_trace_candidates.candidate_ordinal",
            ],
        ),
        sa.UniqueConstraint(
            "trace_id",
            "candidate_ordinal",
            name="uq_heterogeneous_retrieval_result_candidate",
        ),
        sa.CheckConstraint(
            "result_ordinal>=0 AND candidate_ordinal>=0 "
            "AND json_valid(result_json) AND json_type(result_json)='object' "
            "AND " + _hex("result_sha256"),
            name="ck_heterogeneous_retrieval_result_shape",
        ),
    )
    op.create_table(
        "heterogeneous_retrieval_trace_seals",
        sa.Column(
            "trace_id",
            sa.String(128),
            sa.ForeignKey("heterogeneous_retrieval_trace_headers.trace_id"),
            primary_key=True,
        ),
        sa.Column("candidate_count", sa.Integer, nullable=False),
        sa.Column("result_count", sa.Integer, nullable=False),
        sa.Column("canonical_candidate_set_json", sa.Text, nullable=False),
        sa.Column("candidate_set_sha256", sa.String(64), nullable=False),
        sa.Column("canonical_result_set_json", sa.Text, nullable=False),
        sa.Column("result_set_sha256", sa.String(64), nullable=False),
        sa.Column("trace_json", sa.Text, nullable=False),
        sa.Column("trace_sha256", sa.String(64), nullable=False),
        sa.Column("sealed_at", sa.DateTime, nullable=False),
        sa.CheckConstraint(
            "candidate_count>=0 AND result_count>=0 "
            "AND json_valid(canonical_candidate_set_json) "
            "AND json_type(canonical_candidate_set_json)='array' "
            "AND json_valid(canonical_result_set_json) "
            "AND json_type(canonical_result_set_json)='array' "
            "AND json_valid(trace_json) AND json_type(trace_json)='object'",
            name="ck_heterogeneous_retrieval_seal_shape",
        ),
        sa.CheckConstraint(
            _hex("candidate_set_sha256")
            + " AND "
            + _hex("result_set_sha256")
            + " AND "
            + _hex("trace_sha256"),
            name="ck_heterogeneous_retrieval_seal_hashes",
        ),
    )

    for table in _TABLES:
        _append_only(table)
    op.execute(
        "CREATE TRIGGER trg_canonical_fact_projection_entries_unsealed "
        "BEFORE INSERT ON canonical_fact_projection_entries WHEN EXISTS "
        "(SELECT 1 FROM canonical_fact_projection_seals seal "
        "WHERE seal.generation_id=NEW.generation_id) "
        "BEGIN SELECT RAISE(ABORT, 'canonical fact projection is sealed'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_canonical_fact_projection_batches_unsealed "
        "BEFORE INSERT ON canonical_fact_projection_batches WHEN EXISTS "
        "(SELECT 1 FROM canonical_fact_projection_seals seal "
        "WHERE seal.generation_id=NEW.generation_id) "
        "BEGIN SELECT RAISE(ABORT, 'canonical fact projection is sealed'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_canonical_fact_projection_buckets_unsealed "
        "BEFORE INSERT ON canonical_fact_projection_buckets WHEN EXISTS "
        "(SELECT 1 FROM canonical_fact_projection_seals seal "
        "WHERE seal.generation_id=NEW.generation_id) "
        "BEGIN SELECT RAISE(ABORT, 'canonical fact projection is sealed'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_canonical_fact_projection_generation_parent "
        "BEFORE INSERT ON canonical_fact_projection_generations "
        "WHEN NEW.parent_generation_id IS NOT NULL AND NOT EXISTS "
        "(SELECT 1 FROM canonical_fact_projection_generations parent "
        "JOIN canonical_fact_projection_seals seal "
        "ON seal.generation_id=parent.generation_id "
        "WHERE parent.generation_id=NEW.parent_generation_id "
        "AND parent.delta_depth+1=NEW.delta_depth "
        "AND parent.cutoff_at<=NEW.cutoff_at) "
        "BEGIN SELECT RAISE(ABORT, 'projection delta requires exact sealed parent'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_heterogeneous_retrieval_candidates_unsealed "
        "BEFORE INSERT ON heterogeneous_retrieval_trace_candidates WHEN EXISTS "
        "(SELECT 1 FROM heterogeneous_retrieval_trace_seals seal "
        "WHERE seal.trace_id=NEW.trace_id) "
        "BEGIN SELECT RAISE(ABORT, 'retrieval trace is sealed'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_heterogeneous_retrieval_results_unsealed "
        "BEFORE INSERT ON heterogeneous_retrieval_trace_results WHEN EXISTS "
        "(SELECT 1 FROM heterogeneous_retrieval_trace_seals seal "
        "WHERE seal.trace_id=NEW.trace_id) "
        "BEGIN SELECT RAISE(ABORT, 'retrieval trace is sealed'); END"
    )


def downgrade() -> None:
    for trigger in (
        "trg_heterogeneous_retrieval_results_unsealed",
        "trg_heterogeneous_retrieval_candidates_unsealed",
        "trg_canonical_fact_projection_generation_parent",
        "trg_canonical_fact_projection_buckets_unsealed",
        "trg_canonical_fact_projection_batches_unsealed",
        "trg_canonical_fact_projection_entries_unsealed",
    ):
        op.execute(f"DROP TRIGGER IF EXISTS {trigger}")
    for table in reversed(_TABLES):
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_append_only")
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_append_only_delete")
        op.drop_table(table)
