"""Add the baseline-bound latest-governed-state materialization plane.

Revision ID: 0261_latest_governed_state
Revises: 0260_pre_earnings_brief_plumbing

The new plane is additive. Immutable refresh receipts and changed-only audit
rows preserve rollback evidence while the run, staging, head, and current
projection tables remain rebuildable. No historical source or projection data
is removed and this migration does not cut readers over to the new plane.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0261_latest_governed_state"
down_revision: str | None = "0260_pre_earnings_brief_plumbing"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_NORMAL_TABLES = (
    "latest_governed_refresh_runs",
    "latest_governed_refresh_stage",
    "latest_governed_refresh_receipts",
    "latest_governed_refresh_changes",
    "latest_governed_scope_heads",
    "latest_governed_fact_entries",
    "latest_governed_document_entries",
    "latest_governed_narrative_entries",
)
_FTS_TABLE = "latest_governed_narrative_fts"
_FTS_TRIGGERS = (
    "trg_latest_governed_narrative_fts_insert",
    "trg_latest_governed_narrative_fts_update",
    "trg_latest_governed_narrative_fts_delete",
)
_IMMUTABLE_TABLES = (
    "latest_governed_refresh_receipts",
    "latest_governed_refresh_changes",
)


def _hex(column: str) -> str:
    return f"length({column})=64 AND lower({column}) NOT GLOB '*[^0-9a-f]*'"


def _optional_hex(column: str) -> str:
    return f"({column} IS NULL OR ({_hex(column)}))"


def _json_object(column: str) -> str:
    return f"json_valid({column}) AND json_type({column})='object'"


def _json_array(column: str) -> str:
    return f"json_valid({column}) AND json_type({column})='array'"


def _create_immutable_triggers(
    table: str,
    *,
    duplicate_predicate: str,
) -> None:
    op.execute(
        f"CREATE TRIGGER trg_{table}_append_only_update "
        f"BEFORE UPDATE ON {table} "
        "BEGIN SELECT RAISE(ABORT, 'latest governed audit is append-only'); END"
    )
    op.execute(
        f"CREATE TRIGGER trg_{table}_append_only_delete "
        f"BEFORE DELETE ON {table} "
        "BEGIN SELECT RAISE(ABORT, 'latest governed audit is append-only'); END"
    )
    # SQLite REPLACE only fires DELETE triggers when recursive triggers are
    # enabled. Rejecting the duplicate before INSERT makes replacement unsafe
    # under every supported connection configuration.
    op.execute(
        f"CREATE TRIGGER trg_{table}_append_only_replace "
        f"BEFORE INSERT ON {table} WHEN EXISTS ("
        f"SELECT 1 FROM {table} existing WHERE {duplicate_predicate}"
        ") BEGIN SELECT RAISE(ABORT, "
        "'latest governed audit is append-only'); END"
    )


def upgrade() -> None:
    bind = op.get_bind()
    required = {
        "population_run_headers",
        "population_cutover_receipts",
        "ask_retrieval_scope_promotions",
        "canonical_fact_projection_generations",
        "canonical_fact_projection_seals",
        "canonical_metric_cells",
        "reporting_entities",
        "research_snapshot_universe_commitments",
        "source_inventory_snapshots",
        "source_inventory_snapshot_seals",
        "canonical_fact_resolution_revisions",
        "fact_observations_v2",
        "expected_documents",
        "evidence_document_versions",
        "evidence_nodes",
        "search_chunks",
        "search_embedding_artifacts",
    }
    missing = sorted(required - set(sa.inspect(bind).get_table_names()))
    if missing:
        raise RuntimeError(
            "latest governed state requires the sealed architecture plane: " + ", ".join(missing)
        )

    op.create_index(
        "ix_canonical_metric_cells_reporting_entity",
        "canonical_metric_cells",
        ["reporting_entity_id", "canonical_metric_cell_id"],
    )

    op.create_table(
        "latest_governed_refresh_runs",
        sa.Column("refresh_run_id", sa.String(128), primary_key=True),
        sa.Column("idempotency_key", sa.String(256), nullable=False, unique=True),
        sa.Column("scope_key", sa.String(256), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column(
            "baseline_population_run_id",
            sa.String(128),
            sa.ForeignKey("population_run_headers.population_run_id"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["baseline_population_run_id"],
            ["population_cutover_receipts.population_run_id"],
            name="fk_latest_governed_run_cutover_receipt",
        ),
        sa.Column(
            "baseline_population_receipt_sha256",
            sa.String(64),
            nullable=False,
        ),
        sa.Column(
            "baseline_promotion_id",
            sa.String(128),
            sa.ForeignKey("ask_retrieval_scope_promotions.promotion_id"),
        ),
        sa.Column(
            "baseline_fact_generation_id",
            sa.String(128),
            sa.ForeignKey("canonical_fact_projection_generations.generation_id"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["baseline_fact_generation_id"],
            ["canonical_fact_projection_seals.generation_id"],
            name="fk_latest_governed_run_fact_seal",
        ),
        sa.Column("input_head_sha256", sa.String(64), nullable=False),
        sa.Column("policy_config_sha256", sa.String(64), nullable=False),
        sa.Column("knowledge_cutoff", sa.DateTime(), nullable=False),
        sa.Column("observed_through", sa.DateTime(), nullable=False),
        sa.Column("resume_cursor_json", sa.Text(), nullable=False),
        sa.Column("resume_cursor_sha256", sa.String(64), nullable=False),
        sa.Column("staged_change_count", sa.Integer(), nullable=False),
        sa.Column("applied_change_count", sa.Integer(), nullable=False),
        sa.Column("planned_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "status IN ('planned','staging','ready','finalized','failed') "
            "AND staged_change_count>=0 AND applied_change_count>=0 "
            "AND applied_change_count<=staged_change_count",
            name="ck_latest_governed_run_state",
        ),
        sa.CheckConstraint(
            "observed_through>=knowledge_cutoff "
            "AND updated_at>=planned_at AND updated_at>=observed_through",
            name="ck_latest_governed_run_clocks",
        ),
        sa.CheckConstraint(
            _hex("baseline_population_receipt_sha256")
            + " AND "
            + _hex("input_head_sha256")
            + " AND "
            + _hex("policy_config_sha256")
            + " AND "
            + _hex("resume_cursor_sha256")
            + " AND "
            + _json_object("resume_cursor_json"),
            name="ck_latest_governed_run_commitments",
        ),
    )
    op.create_index(
        "ix_latest_governed_run_scope_status",
        "latest_governed_refresh_runs",
        ["scope_key", "status", "updated_at"],
    )

    op.create_table(
        "latest_governed_refresh_stage",
        sa.Column(
            "refresh_run_id",
            sa.String(128),
            sa.ForeignKey("latest_governed_refresh_runs.refresh_run_id"),
            primary_key=True,
        ),
        sa.Column("stage_ordinal", sa.Integer(), primary_key=True),
        sa.Column("entity_kind", sa.String(16), nullable=False),
        sa.Column("change_kind", sa.String(16), nullable=False),
        sa.Column("coordinate_key", sa.String(512), nullable=False),
        sa.Column("digest_bucket", sa.Integer(), nullable=False),
        sa.Column("prior_commitment_sha256", sa.String(64)),
        sa.Column("current_commitment_sha256", sa.String(64)),
        sa.Column("canonical_payload_json", sa.Text(), nullable=False),
        sa.Column("payload_sha256", sa.String(64), nullable=False),
        sa.Column("stage_status", sa.String(16), nullable=False),
        sa.Column("staged_at", sa.DateTime(), nullable=False),
        sa.Column("applied_at", sa.DateTime()),
        sa.UniqueConstraint(
            "refresh_run_id",
            "entity_kind",
            "coordinate_key",
            name="uq_latest_governed_stage_coordinate",
        ),
        sa.CheckConstraint(
            "stage_ordinal>=0 AND digest_bucket BETWEEN 0 AND 4095 "
            "AND entity_kind IN ('fact','document','narrative','head') "
            "AND change_kind IN ('upsert','delete') "
            "AND stage_status IN ('staged','applied','failed')",
            name="ck_latest_governed_stage_shape",
        ),
        sa.CheckConstraint(
            "(change_kind='upsert' AND current_commitment_sha256 IS NOT NULL) "
            "OR (change_kind='delete' AND current_commitment_sha256 IS NULL)",
            name="ck_latest_governed_stage_change",
        ),
        sa.CheckConstraint(
            _optional_hex("prior_commitment_sha256")
            + " AND "
            + _optional_hex("current_commitment_sha256")
            + " AND "
            + _hex("payload_sha256")
            + " AND "
            + _json_object("canonical_payload_json"),
            name="ck_latest_governed_stage_payload",
        ),
        sa.CheckConstraint(
            "(stage_status='staged' AND applied_at IS NULL) "
            "OR (stage_status IN ('applied','failed') AND applied_at IS NOT NULL)",
            name="ck_latest_governed_stage_clocks",
        ),
    )
    op.create_index(
        "ix_latest_governed_stage_resume",
        "latest_governed_refresh_stage",
        ["refresh_run_id", "stage_status", "stage_ordinal"],
    )
    op.create_index(
        "ix_latest_governed_stage_bucket",
        "latest_governed_refresh_stage",
        ["refresh_run_id", "entity_kind", "digest_bucket"],
    )

    op.create_table(
        "latest_governed_refresh_receipts",
        sa.Column("receipt_id", sa.String(128), primary_key=True),
        sa.Column("idempotency_key", sa.String(256), nullable=False, unique=True),
        sa.Column(
            "refresh_run_id",
            sa.String(128),
            sa.ForeignKey("latest_governed_refresh_runs.refresh_run_id"),
            nullable=False,
            unique=True,
        ),
        sa.Column("scope_key", sa.String(256), nullable=False),
        sa.Column(
            "prior_receipt_id",
            sa.String(128),
            sa.ForeignKey("latest_governed_refresh_receipts.receipt_id"),
        ),
        sa.Column(
            "baseline_population_run_id",
            sa.String(128),
            sa.ForeignKey("population_run_headers.population_run_id"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["baseline_population_run_id"],
            ["population_cutover_receipts.population_run_id"],
            name="fk_latest_governed_receipt_cutover",
        ),
        sa.Column(
            "baseline_population_receipt_sha256",
            sa.String(64),
            nullable=False,
        ),
        sa.Column(
            "baseline_promotion_id",
            sa.String(128),
            sa.ForeignKey("ask_retrieval_scope_promotions.promotion_id"),
        ),
        sa.Column(
            "fact_generation_id",
            sa.String(128),
            sa.ForeignKey("canonical_fact_projection_generations.generation_id"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["fact_generation_id"],
            ["canonical_fact_projection_seals.generation_id"],
            name="fk_latest_governed_receipt_fact_seal",
        ),
        sa.Column("input_head_sha256", sa.String(64), nullable=False),
        sa.Column("prior_state_sha256", sa.String(64)),
        sa.Column("current_state_sha256", sa.String(64), nullable=False),
        sa.Column("fact_root_sha256", sa.String(64), nullable=False),
        sa.Column("document_root_sha256", sa.String(64), nullable=False),
        sa.Column("narrative_root_sha256", sa.String(64), nullable=False),
        sa.Column("change_count", sa.Integer(), nullable=False),
        sa.Column("fact_change_count", sa.Integer(), nullable=False),
        sa.Column("document_change_count", sa.Integer(), nullable=False),
        sa.Column("narrative_change_count", sa.Integer(), nullable=False),
        sa.Column("canonical_change_set_json", sa.Text(), nullable=False),
        sa.Column("change_set_sha256", sa.String(64), nullable=False),
        sa.Column("canonical_receipt_json", sa.Text(), nullable=False),
        sa.Column("receipt_sha256", sa.String(64), nullable=False, unique=True),
        sa.Column("knowledge_cutoff", sa.DateTime(), nullable=False),
        sa.Column("observed_through", sa.DateTime(), nullable=False),
        sa.Column("sealed_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "change_count>=0 AND fact_change_count>=0 "
            "AND document_change_count>=0 AND narrative_change_count>=0 "
            "AND change_count=fact_change_count+document_change_count"
            "+narrative_change_count",
            name="ck_latest_governed_receipt_counts",
        ),
        sa.CheckConstraint(
            "(prior_receipt_id IS NULL AND prior_state_sha256 IS NULL) "
            "OR (prior_receipt_id IS NOT NULL AND prior_state_sha256 IS NOT NULL)",
            name="ck_latest_governed_receipt_chain",
        ),
        sa.CheckConstraint(
            "observed_through>=knowledge_cutoff AND sealed_at>=observed_through",
            name="ck_latest_governed_receipt_clocks",
        ),
        sa.CheckConstraint(
            _hex("baseline_population_receipt_sha256")
            + " AND "
            + _hex("input_head_sha256")
            + " AND "
            + _optional_hex("prior_state_sha256")
            + " AND "
            + _hex("current_state_sha256")
            + " AND "
            + _hex("fact_root_sha256")
            + " AND "
            + _hex("document_root_sha256")
            + " AND "
            + _hex("narrative_root_sha256")
            + " AND "
            + _hex("change_set_sha256")
            + " AND "
            + _hex("receipt_sha256")
            + " AND "
            + _json_array("canonical_change_set_json")
            + " AND "
            + _json_object("canonical_receipt_json"),
            name="ck_latest_governed_receipt_commitments",
        ),
    )
    op.create_index(
        "ix_latest_governed_receipt_scope_clock",
        "latest_governed_refresh_receipts",
        ["scope_key", "observed_through", "sealed_at"],
    )

    op.create_table(
        "latest_governed_refresh_changes",
        sa.Column("change_id", sa.String(128), primary_key=True),
        sa.Column("idempotency_key", sa.String(256), nullable=False, unique=True),
        sa.Column(
            "receipt_id",
            sa.String(128),
            sa.ForeignKey("latest_governed_refresh_receipts.receipt_id"),
            nullable=False,
        ),
        sa.Column("change_ordinal", sa.Integer(), nullable=False),
        sa.Column("entity_kind", sa.String(16), nullable=False),
        sa.Column("change_kind", sa.String(16), nullable=False),
        sa.Column("coordinate_key", sa.String(512), nullable=False),
        sa.Column("digest_bucket", sa.Integer(), nullable=False),
        sa.Column("prior_commitment_sha256", sa.String(64)),
        sa.Column("current_commitment_sha256", sa.String(64)),
        sa.Column("selection_reason", sa.Text(), nullable=False),
        sa.Column("source_evidence_json", sa.Text(), nullable=False),
        sa.Column("source_evidence_sha256", sa.String(64), nullable=False),
        sa.Column("canonical_change_json", sa.Text(), nullable=False),
        sa.Column("change_sha256", sa.String(64), nullable=False),
        sa.Column("knowledge_cutoff", sa.DateTime(), nullable=False),
        sa.Column("observed_through", sa.DateTime(), nullable=False),
        sa.Column("recorded_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint(
            "receipt_id",
            "change_ordinal",
            name="uq_latest_governed_change_ordinal",
        ),
        sa.UniqueConstraint(
            "receipt_id",
            "entity_kind",
            "coordinate_key",
            name="uq_latest_governed_change_coordinate",
        ),
        sa.CheckConstraint(
            "change_ordinal>=0 AND digest_bucket BETWEEN 0 AND 4095 "
            "AND entity_kind IN ('fact','document','narrative','head') "
            "AND change_kind IN ('upsert','delete')",
            name="ck_latest_governed_change_shape",
        ),
        sa.CheckConstraint(
            "(change_kind='upsert' AND current_commitment_sha256 IS NOT NULL) "
            "OR (change_kind='delete' AND current_commitment_sha256 IS NULL)",
            name="ck_latest_governed_change_operation",
        ),
        sa.CheckConstraint(
            "observed_through>=knowledge_cutoff AND recorded_at>=observed_through",
            name="ck_latest_governed_change_clocks",
        ),
        sa.CheckConstraint(
            _optional_hex("prior_commitment_sha256")
            + " AND "
            + _optional_hex("current_commitment_sha256")
            + " AND "
            + _hex("source_evidence_sha256")
            + " AND "
            + _hex("change_sha256")
            + " AND "
            + _json_object("source_evidence_json")
            + " AND "
            + _json_object("canonical_change_json"),
            name="ck_latest_governed_change_commitments",
        ),
    )
    op.create_index(
        "ix_latest_governed_change_bucket",
        "latest_governed_refresh_changes",
        ["receipt_id", "entity_kind", "digest_bucket", "change_ordinal"],
    )

    op.create_table(
        "latest_governed_scope_heads",
        sa.Column("scope_key", sa.String(256), primary_key=True),
        sa.Column(
            "refresh_receipt_id",
            sa.String(128),
            sa.ForeignKey("latest_governed_refresh_receipts.receipt_id"),
            nullable=False,
            unique=True,
        ),
        sa.Column(
            "population_run_id",
            sa.String(128),
            sa.ForeignKey("population_cutover_receipts.population_run_id"),
            nullable=False,
        ),
        sa.Column(
            "promotion_id",
            sa.String(128),
            sa.ForeignKey("ask_retrieval_scope_promotions.promotion_id"),
        ),
        sa.Column(
            "fact_generation_id",
            sa.String(128),
            sa.ForeignKey("canonical_fact_projection_seals.generation_id"),
            nullable=False,
        ),
        sa.Column("source_heads_json", sa.Text(), nullable=False),
        sa.Column("source_heads_sha256", sa.String(64), nullable=False),
        sa.Column("state_sha256", sa.String(64), nullable=False),
        sa.Column("fact_root_sha256", sa.String(64), nullable=False),
        sa.Column("document_root_sha256", sa.String(64), nullable=False),
        sa.Column("narrative_root_sha256", sa.String(64), nullable=False),
        sa.Column("fact_count", sa.Integer(), nullable=False),
        sa.Column("document_count", sa.Integer(), nullable=False),
        sa.Column("narrative_count", sa.Integer(), nullable=False),
        sa.Column("knowledge_cutoff", sa.DateTime(), nullable=False),
        sa.Column("observed_through", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "fact_count>=0 AND document_count>=0 AND narrative_count>=0",
            name="ck_latest_governed_head_counts",
        ),
        sa.CheckConstraint(
            "observed_through>=knowledge_cutoff AND updated_at>=observed_through",
            name="ck_latest_governed_head_clocks",
        ),
        sa.CheckConstraint(
            _json_object("source_heads_json")
            + " AND "
            + _hex("source_heads_sha256")
            + " AND "
            + _hex("state_sha256")
            + " AND "
            + _hex("fact_root_sha256")
            + " AND "
            + _hex("document_root_sha256")
            + " AND "
            + _hex("narrative_root_sha256"),
            name="ck_latest_governed_head_commitments",
        ),
    )
    op.create_index(
        "ix_latest_governed_head_clock",
        "latest_governed_scope_heads",
        ["observed_through", "scope_key"],
    )

    op.create_table(
        "latest_governed_fact_entries",
        sa.Column("scope_key", sa.String(256), primary_key=True),
        sa.Column(
            "canonical_metric_cell_id",
            sa.String(128),
            sa.ForeignKey("canonical_metric_cells.canonical_metric_cell_id"),
            primary_key=True,
        ),
        sa.Column("digest_bucket", sa.Integer(), nullable=False),
        sa.Column(
            "refresh_receipt_id",
            sa.String(128),
            sa.ForeignKey("latest_governed_refresh_receipts.receipt_id"),
            nullable=False,
        ),
        sa.Column(
            "fact_generation_id",
            sa.String(128),
            sa.ForeignKey("canonical_fact_projection_seals.generation_id"),
            nullable=False,
        ),
        sa.Column(
            "canonical_resolution_revision_id",
            sa.String(128),
            sa.ForeignKey("canonical_fact_resolution_revisions.canonical_resolution_revision_id"),
            nullable=False,
        ),
        sa.Column(
            "selected_observation_id",
            sa.String(128),
            sa.ForeignKey("fact_observations_v2.observation_id"),
            nullable=False,
        ),
        sa.Column("canonical_metric_name", sa.Text(), nullable=False),
        sa.Column("period_kind", sa.String(16), nullable=False),
        sa.Column("period_start", sa.Text()),
        sa.Column("period_end", sa.Text()),
        sa.Column("unit_key", sa.Text()),
        sa.Column("currency", sa.String(3)),
        sa.Column("value_kind", sa.String(16), nullable=False),
        sa.Column("canonical_value", sa.Text()),
        sa.Column("canonical_search_text", sa.Text(), nullable=False),
        sa.Column("selection_reason", sa.Text(), nullable=False),
        sa.Column("source_evidence_json", sa.Text(), nullable=False),
        sa.Column("source_evidence_sha256", sa.String(64), nullable=False),
        sa.Column("prior_commitment_sha256", sa.String(64)),
        sa.Column("current_commitment_sha256", sa.String(64), nullable=False),
        sa.Column("knowledge_cutoff", sa.DateTime(), nullable=False),
        sa.Column("observed_through", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "digest_bucket BETWEEN 0 AND 4095 AND value_kind IN ('numeric','text','nil')",
            name="ck_latest_governed_fact_shape",
        ),
        sa.CheckConstraint(
            "observed_through>=knowledge_cutoff AND updated_at>=observed_through",
            name="ck_latest_governed_fact_clocks",
        ),
        sa.CheckConstraint(
            _json_object("source_evidence_json")
            + " AND "
            + _hex("source_evidence_sha256")
            + " AND "
            + _optional_hex("prior_commitment_sha256")
            + " AND "
            + _hex("current_commitment_sha256"),
            name="ck_latest_governed_fact_commitments",
        ),
    )
    op.create_index(
        "ix_latest_governed_fact_search",
        "latest_governed_fact_entries",
        [
            "scope_key",
            "canonical_metric_name",
            sa.text("period_end DESC"),
            "canonical_metric_cell_id",
        ],
    )
    op.create_index(
        "ix_latest_governed_fact_bucket",
        "latest_governed_fact_entries",
        ["scope_key", "digest_bucket", "canonical_metric_cell_id"],
    )

    op.create_table(
        "latest_governed_document_entries",
        sa.Column("scope_key", sa.String(256), primary_key=True),
        sa.Column("expected_document_key", sa.String(256), primary_key=True),
        sa.Column("digest_bucket", sa.Integer(), nullable=False),
        sa.Column(
            "refresh_receipt_id",
            sa.String(128),
            sa.ForeignKey("latest_governed_refresh_receipts.receipt_id"),
            nullable=False,
        ),
        sa.Column(
            "expected_document_id",
            sa.String(128),
            sa.ForeignKey("expected_documents.expected_document_id"),
            nullable=False,
        ),
        sa.Column(
            "document_version_id",
            sa.String(128),
            sa.ForeignKey("evidence_document_versions.document_version_id"),
            nullable=False,
        ),
        sa.Column("source_kind", sa.String(32), nullable=False),
        sa.Column("document_type", sa.String(64), nullable=False),
        sa.Column("period_start", sa.DateTime()),
        sa.Column("period_end", sa.DateTime()),
        sa.Column("selection_reason", sa.Text(), nullable=False),
        sa.Column("source_evidence_json", sa.Text(), nullable=False),
        sa.Column("source_evidence_sha256", sa.String(64), nullable=False),
        sa.Column("prior_commitment_sha256", sa.String(64)),
        sa.Column("current_commitment_sha256", sa.String(64), nullable=False),
        sa.Column("knowledge_cutoff", sa.DateTime(), nullable=False),
        sa.Column("observed_through", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "digest_bucket BETWEEN 0 AND 4095 "
            "AND source_kind IN ('sec_filing','ir_document','earnings_call')",
            name="ck_latest_governed_document_shape",
        ),
        sa.CheckConstraint(
            "period_start IS NULL OR period_end IS NULL OR period_end>=period_start",
            name="ck_latest_governed_document_period",
        ),
        sa.CheckConstraint(
            "observed_through>=knowledge_cutoff AND updated_at>=observed_through",
            name="ck_latest_governed_document_clocks",
        ),
        sa.CheckConstraint(
            _json_object("source_evidence_json")
            + " AND "
            + _hex("source_evidence_sha256")
            + " AND "
            + _optional_hex("prior_commitment_sha256")
            + " AND "
            + _hex("current_commitment_sha256"),
            name="ck_latest_governed_document_commitments",
        ),
    )
    op.create_index(
        "ix_latest_governed_document_lookup",
        "latest_governed_document_entries",
        ["scope_key", "source_kind", "document_type", "period_end"],
    )
    op.create_index(
        "ix_latest_governed_document_bucket",
        "latest_governed_document_entries",
        ["scope_key", "digest_bucket", "expected_document_key"],
    )

    op.create_table(
        "latest_governed_narrative_entries",
        sa.Column("scope_key", sa.String(256), primary_key=True),
        sa.Column("expected_document_key", sa.String(256), primary_key=True),
        sa.Column("chunk_key", sa.String(256), primary_key=True),
        sa.Column("digest_bucket", sa.Integer(), nullable=False),
        sa.Column(
            "refresh_receipt_id",
            sa.String(128),
            sa.ForeignKey("latest_governed_refresh_receipts.receipt_id"),
            nullable=False,
        ),
        sa.Column(
            "document_version_id",
            sa.String(128),
            sa.ForeignKey("evidence_document_versions.document_version_id"),
            nullable=False,
        ),
        sa.Column(
            "evidence_node_id",
            sa.String(128),
            sa.ForeignKey("evidence_nodes.node_id"),
            nullable=False,
        ),
        sa.Column(
            "source_chunk_id",
            sa.String(128),
            sa.ForeignKey("search_chunks.chunk_id"),
        ),
        sa.Column(
            "embedding_artifact_id",
            sa.String(128),
            sa.ForeignKey("search_embedding_artifacts.embedding_artifact_id"),
        ),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("content_sha256", sa.String(64), nullable=False),
        sa.Column("chunker_config_sha256", sa.String(64), nullable=False),
        sa.Column("selection_reason", sa.Text(), nullable=False),
        sa.Column("prior_commitment_sha256", sa.String(64)),
        sa.Column("current_commitment_sha256", sa.String(64), nullable=False),
        sa.Column("knowledge_cutoff", sa.DateTime(), nullable=False),
        sa.Column("observed_through", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["scope_key", "expected_document_key"],
            [
                "latest_governed_document_entries.scope_key",
                "latest_governed_document_entries.expected_document_key",
            ],
            name="fk_latest_governed_narrative_current_document",
        ),
        sa.CheckConstraint(
            "digest_bucket BETWEEN 0 AND 4095 AND length(text)>0",
            name="ck_latest_governed_narrative_shape",
        ),
        sa.CheckConstraint(
            "observed_through>=knowledge_cutoff AND updated_at>=observed_through",
            name="ck_latest_governed_narrative_clocks",
        ),
        sa.CheckConstraint(
            _hex("content_sha256")
            + " AND "
            + _hex("chunker_config_sha256")
            + " AND "
            + _optional_hex("prior_commitment_sha256")
            + " AND "
            + _hex("current_commitment_sha256"),
            name="ck_latest_governed_narrative_commitments",
        ),
    )
    op.create_index(
        "ix_latest_governed_narrative_document",
        "latest_governed_narrative_entries",
        ["scope_key", "expected_document_key", "chunk_key"],
    )
    op.create_index(
        "ix_latest_governed_narrative_bucket",
        "latest_governed_narrative_entries",
        ["scope_key", "digest_bucket", "expected_document_key", "chunk_key"],
    )
    op.create_index(
        "ix_latest_governed_narrative_content",
        "latest_governed_narrative_entries",
        ["content_sha256", "chunker_config_sha256"],
    )

    try:
        op.execute(
            f"CREATE VIRTUAL TABLE {_FTS_TABLE} USING fts5("
            "scope_key UNINDEXED, expected_document_key UNINDEXED, "
            "chunk_key UNINDEXED, text, "
            "content='latest_governed_narrative_entries', content_rowid='rowid')"
        )
    except Exception as exc:  # pragma: no cover - unsupported SQLite build
        raise RuntimeError("SQLite FTS5 is required for latest governed narrative search") from exc
    op.execute(
        "CREATE TRIGGER trg_latest_governed_narrative_fts_insert "
        "AFTER INSERT ON latest_governed_narrative_entries BEGIN "
        "INSERT INTO latest_governed_narrative_fts("
        "rowid,scope_key,expected_document_key,chunk_key,text) VALUES ("
        "NEW.rowid,NEW.scope_key,NEW.expected_document_key,NEW.chunk_key,NEW.text); END"
    )
    op.execute(
        "CREATE TRIGGER trg_latest_governed_narrative_fts_update "
        "AFTER UPDATE ON latest_governed_narrative_entries BEGIN "
        "INSERT INTO latest_governed_narrative_fts("
        "latest_governed_narrative_fts,rowid,scope_key,"
        "expected_document_key,chunk_key,text) VALUES ("
        "'delete',OLD.rowid,OLD.scope_key,OLD.expected_document_key,"
        "OLD.chunk_key,OLD.text); "
        "INSERT INTO latest_governed_narrative_fts("
        "rowid,scope_key,expected_document_key,chunk_key,text) VALUES ("
        "NEW.rowid,NEW.scope_key,NEW.expected_document_key,NEW.chunk_key,NEW.text); END"
    )
    op.execute(
        "CREATE TRIGGER trg_latest_governed_narrative_fts_delete "
        "AFTER DELETE ON latest_governed_narrative_entries BEGIN "
        "INSERT INTO latest_governed_narrative_fts("
        "latest_governed_narrative_fts,rowid,scope_key,"
        "expected_document_key,chunk_key,text) VALUES ("
        "'delete',OLD.rowid,OLD.scope_key,OLD.expected_document_key,"
        "OLD.chunk_key,OLD.text); END"
    )

    _create_immutable_triggers(
        "latest_governed_refresh_receipts",
        duplicate_predicate=(
            "existing.receipt_id=NEW.receipt_id "
            "OR existing.idempotency_key=NEW.idempotency_key "
            "OR existing.refresh_run_id=NEW.refresh_run_id "
            "OR existing.receipt_sha256=NEW.receipt_sha256"
        ),
    )
    _create_immutable_triggers(
        "latest_governed_refresh_changes",
        duplicate_predicate=(
            "existing.change_id=NEW.change_id "
            "OR existing.idempotency_key=NEW.idempotency_key "
            "OR (existing.receipt_id=NEW.receipt_id "
            "AND existing.change_ordinal=NEW.change_ordinal) "
            "OR (existing.receipt_id=NEW.receipt_id "
            "AND existing.entity_kind=NEW.entity_kind "
            "AND existing.coordinate_key=NEW.coordinate_key)"
        ),
    )


def downgrade() -> None:
    bind = op.get_bind()
    populated = []
    for table in (*_NORMAL_TABLES, _FTS_TABLE):
        if bind.execute(sa.text(f'SELECT EXISTS(SELECT 1 FROM "{table}")')).scalar():
            populated.append(table)
    if populated:
        raise RuntimeError("downgrade would discard latest governed state: " + ", ".join(populated))

    for table in _IMMUTABLE_TABLES:
        for operation in ("replace", "delete", "update"):
            op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_append_only_{operation}")
    for trigger in _FTS_TRIGGERS:
        op.execute(f"DROP TRIGGER IF EXISTS {trigger}")
    op.execute(f"DROP TABLE IF EXISTS {_FTS_TABLE}")
    op.drop_index(
        "ix_canonical_metric_cells_reporting_entity",
        table_name="canonical_metric_cells",
    )

    for table in (
        "latest_governed_narrative_entries",
        "latest_governed_document_entries",
        "latest_governed_fact_entries",
        "latest_governed_scope_heads",
        "latest_governed_refresh_changes",
        "latest_governed_refresh_receipts",
        "latest_governed_refresh_stage",
        "latest_governed_refresh_runs",
    ):
        op.drop_table(table)
