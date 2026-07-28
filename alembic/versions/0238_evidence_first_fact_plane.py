"""Add the evidence-first, reporting-entity keyed fact plane.

Revision ID: 0238_evidence_first_fact_plane
Revises: 0237_companyfacts_match_gated_capture

The v2 plane is intentionally additive.  It does not project from, mutate, or
silently fall back to the ticker-keyed legacy fact tables.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0238_evidence_first_fact_plane"
down_revision: str | Sequence[str] | None = "0237_companyfacts_match_gated_capture"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLES = (
    "fact_cells_v2",
    "fact_observations_v2",
    "fact_observation_relations_v2",
    "fact_resolution_candidates_v2",
    "fact_resolution_revisions_v2",
    "fact_derivation_input_edges_v2",
    "fact_derivation_seals_v2",
)
_VIEWS = (
    "v_fact_resolutions_current_v2",
    "v_fact_cells_resolved_current_v2",
    "v_fact_observations_as_reported_v2",
)


def _hex_check(column: str, *, nullable: bool = False) -> str:
    required = f"length({column}) = 64 AND {column} NOT GLOB '*[^0-9a-f]*'"
    return f"({column} IS NULL OR ({required}))" if nullable else f"({required})"


def _append_only(table: str, message: str) -> None:
    op.execute(
        f"CREATE TRIGGER trg_{table}_append_only BEFORE UPDATE ON {table} "
        f"BEGIN SELECT RAISE(ABORT, '{message} is append-only'); END"
    )
    op.execute(
        f"CREATE TRIGGER trg_{table}_append_only_delete BEFORE DELETE ON {table} "
        f"BEGIN SELECT RAISE(ABORT, '{message} is append-only'); END"
    )


def _clock_columns() -> tuple[sa.Column[object], ...]:
    return (
        sa.Column("effective_at", sa.DateTime(), nullable=False),
        sa.Column("knowledge_at", sa.DateTime(), nullable=False),
        sa.Column("recorded_at", sa.DateTime(), nullable=False),
    )


def upgrade() -> None:
    bind = op.get_bind()
    required = {
        "reporting_entities",
        "securities",
        "security_reporting_entity_revisions",
        "evidence_document_versions",
        "evidence_extraction_runs",
        "evidence_nodes",
        "legacy_fact_evidence_match_revisions",
    }
    missing = sorted(required - set(sa.inspect(bind).get_table_names()))
    if missing:
        raise RuntimeError(
            "evidence-first fact plane requires hardened predecessor tables: "
            + ", ".join(missing)
        )
    required_views = {
        "v_evidence_document_versions_canonical",
        "v_security_reporting_entities_current",
        "v_legacy_fact_evidence_matches_accepted_current",
    }
    missing_views = sorted(
        required_views - set(sa.inspect(bind).get_view_names())
    )
    if missing_views:
        raise RuntimeError(
            "evidence-first fact plane requires hardened predecessor views: "
            + ", ".join(missing_views)
        )

    op.create_table(
        "fact_cells_v2",
        sa.Column("fact_cell_id", sa.String(128), primary_key=True),
        sa.Column("idempotency_key", sa.String(256), nullable=False, unique=True),
        sa.Column(
            "reporting_entity_id",
            sa.String(128),
            sa.ForeignKey("reporting_entities.reporting_entity_id"),
            nullable=False,
        ),
        sa.Column(
            "scope_security_id",
            sa.String(128),
            sa.ForeignKey("securities.security_id"),
            nullable=True,
        ),
        sa.Column("semantic_key_version", sa.String(64), nullable=False),
        sa.Column("semantic_key_sha256", sa.String(64), nullable=False, unique=True),
        sa.Column("concept_namespace", sa.Text(), nullable=False),
        sa.Column("concept_name", sa.Text(), nullable=False),
        sa.Column("taxonomy_name", sa.Text(), nullable=False),
        sa.Column("taxonomy_version", sa.Text(), nullable=True),
        sa.Column("accounting_basis", sa.String(32), nullable=False),
        sa.Column("consolidation_scope", sa.String(32), nullable=False),
        sa.Column("period_kind", sa.String(16), nullable=False),
        sa.Column("period_start", sa.DateTime(), nullable=True),
        sa.Column("period_end", sa.DateTime(), nullable=False),
        sa.Column("fiscal_year", sa.Integer(), nullable=True),
        sa.Column("fiscal_period", sa.String(16), nullable=True),
        sa.Column("canonical_dimensions_json", sa.Text(), nullable=False),
        sa.Column("canonical_dimensions_sha256", sa.String(64), nullable=False),
        sa.Column("unit_key", sa.Text(), nullable=False),
        sa.Column("currency", sa.String(3), nullable=True),
        *_clock_columns(),
        sa.CheckConstraint(
            "semantic_key_version = 'fact_cell_semantic_key.v2'",
            name="ck_fact_cell_v2_semantic_version",
        ),
        sa.CheckConstraint(
            _hex_check("semantic_key_sha256")
            + " AND "
            + _hex_check("canonical_dimensions_sha256"),
            name="ck_fact_cell_v2_hashes",
        ),
        sa.CheckConstraint(
            "length(trim(concept_namespace)) > 0 "
            "AND length(trim(concept_name)) > 0 "
            "AND length(trim(taxonomy_name)) > 0 "
            "AND length(trim(unit_key)) > 0",
            name="ck_fact_cell_v2_required_text",
        ),
        sa.CheckConstraint(
            "accounting_basis IN "
            "('us_gaap', 'ifrs', 'local_gaap', 'management', 'non_gaap', 'other')",
            name="ck_fact_cell_v2_accounting_basis",
        ),
        sa.CheckConstraint(
            "consolidation_scope IN "
            "('consolidated', 'parent_only', 'subsidiary', 'segment', "
            "'security_specific', 'other')",
            name="ck_fact_cell_v2_consolidation_scope",
        ),
        sa.CheckConstraint(
            "(period_kind = 'instant' AND period_start IS NULL) OR "
            "(period_kind = 'duration' AND period_start IS NOT NULL "
            "AND period_end >= period_start)",
            name="ck_fact_cell_v2_period",
        ),
        sa.CheckConstraint(
            "fiscal_period IS NULL OR fiscal_period IN "
            "('FY', 'Q1', 'Q2', 'Q3', 'Q4', 'H1', 'H2', 'YTD', 'TTM', 'OTHER')",
            name="ck_fact_cell_v2_fiscal_period",
        ),
        sa.CheckConstraint(
            "currency IS NULL OR "
            "(length(currency) = 3 AND currency = upper(currency))",
            name="ck_fact_cell_v2_currency",
        ),
        sa.CheckConstraint(
            "json_valid(canonical_dimensions_json) "
            "AND json_type(canonical_dimensions_json) = 'array'",
            name="ck_fact_cell_v2_dimensions",
        ),
        sa.CheckConstraint(
            "knowledge_at >= effective_at AND recorded_at >= knowledge_at",
            name="ck_fact_cell_v2_clocks",
        ),
    )
    op.create_index(
        "ix_fact_cells_v2_entity_period",
        "fact_cells_v2",
        ["reporting_entity_id", "period_end", "concept_namespace", "concept_name"],
    )
    op.create_index(
        "ix_fact_cells_v2_security_period",
        "fact_cells_v2",
        ["scope_security_id", "period_end"],
    )

    op.create_table(
        "fact_observations_v2",
        sa.Column("observation_id", sa.String(128), primary_key=True),
        sa.Column("idempotency_key", sa.String(256), nullable=False, unique=True),
        sa.Column(
            "fact_cell_id",
            sa.String(128),
            sa.ForeignKey("fact_cells_v2.fact_cell_id"),
            nullable=False,
        ),
        sa.Column("observation_kind", sa.String(16), nullable=False),
        sa.Column("value_kind", sa.String(16), nullable=False),
        sa.Column("numeric_value", sa.Text(), nullable=True),
        sa.Column("text_value", sa.Text(), nullable=True),
        sa.Column("is_nil", sa.Boolean(), nullable=False),
        sa.Column("raw_lexical_value", sa.Text(), nullable=True),
        sa.Column(
            "document_version_id",
            sa.String(128),
            sa.ForeignKey("evidence_document_versions.document_version_id"),
            nullable=True,
        ),
        sa.Column(
            "evidence_node_id",
            sa.String(128),
            sa.ForeignKey("evidence_nodes.node_id"),
            nullable=True,
        ),
        sa.Column("source_locator_json", sa.Text(), nullable=True),
        sa.Column("source_locator_sha256", sa.String(64), nullable=True),
        sa.Column("source_entry_sha256", sa.String(64), nullable=True),
        sa.Column("source_context_id", sa.Text(), nullable=True),
        sa.Column("source_unit_id", sa.Text(), nullable=True),
        sa.Column("decimals", sa.Text(), nullable=True),
        sa.Column("precision", sa.Text(), nullable=True),
        sa.Column(
            "legacy_match_revision_id",
            sa.String(128),
            sa.ForeignKey(
                "legacy_fact_evidence_match_revisions.match_revision_id"
            ),
            nullable=True,
        ),
        sa.Column("formula_id", sa.String(128), nullable=True),
        sa.Column("formula_version", sa.String(64), nullable=True),
        sa.Column("method_name", sa.String(128), nullable=False),
        sa.Column("method_version", sa.String(64), nullable=False),
        sa.Column("method_config_sha256", sa.String(64), nullable=False),
        sa.Column("revision_kind", sa.String(32), nullable=False),
        sa.Column(
            "supersedes_observation_id",
            sa.String(128),
            sa.ForeignKey("fact_observations_v2.observation_id"),
            nullable=True,
        ),
        *_clock_columns(),
        sa.CheckConstraint(
            "observation_kind IN ('reported', 'derived')",
            name="ck_fact_observation_v2_kind",
        ),
        sa.CheckConstraint(
            "(value_kind = 'numeric' AND numeric_value IS NOT NULL "
            "AND text_value IS NULL AND is_nil = 0) OR "
            "(value_kind = 'text' AND numeric_value IS NULL "
            "AND text_value IS NOT NULL AND is_nil = 0) OR "
            "(value_kind = 'nil' AND numeric_value IS NULL "
            "AND text_value IS NULL AND is_nil = 1)",
            name="ck_fact_observation_v2_value",
        ),
        sa.CheckConstraint(
            "(observation_kind = 'reported' "
            "AND document_version_id IS NOT NULL "
            "AND evidence_node_id IS NOT NULL "
            "AND source_locator_json IS NOT NULL "
            "AND source_locator_sha256 IS NOT NULL "
            "AND source_entry_sha256 IS NOT NULL "
            "AND formula_id IS NULL AND formula_version IS NULL) OR "
            "(observation_kind = 'derived' "
            "AND document_version_id IS NULL "
            "AND evidence_node_id IS NULL "
            "AND source_locator_json IS NULL "
            "AND source_locator_sha256 IS NULL "
            "AND source_entry_sha256 IS NULL "
            "AND source_context_id IS NULL "
            "AND source_unit_id IS NULL "
            "AND decimals IS NULL AND precision IS NULL "
            "AND legacy_match_revision_id IS NULL "
            "AND formula_id IS NOT NULL AND formula_version IS NOT NULL)",
            name="ck_fact_observation_v2_provenance",
        ),
        sa.CheckConstraint(
            "source_locator_json IS NULL OR "
            "(json_valid(source_locator_json) "
            "AND json_type(source_locator_json) = 'object')",
            name="ck_fact_observation_v2_locator",
        ),
        sa.CheckConstraint(
            _hex_check("source_locator_sha256", nullable=True)
            + " AND "
            + _hex_check("source_entry_sha256", nullable=True)
            + " AND "
            + _hex_check("method_config_sha256"),
            name="ck_fact_observation_v2_hashes",
        ),
        sa.CheckConstraint(
            "revision_kind IN "
            "('initial', 'amendment', 'reissue', 'restatement', 'correction', "
            "'presentation_recast')",
            name="ck_fact_observation_v2_revision_kind",
        ),
        sa.CheckConstraint(
            "(revision_kind = 'initial' AND supersedes_observation_id IS NULL) OR "
            "(revision_kind <> 'initial' AND supersedes_observation_id IS NOT NULL)",
            name="ck_fact_observation_v2_revision_parent",
        ),
        sa.CheckConstraint(
            "knowledge_at >= effective_at AND recorded_at >= knowledge_at",
            name="ck_fact_observation_v2_clocks",
        ),
    )
    op.create_index(
        "ix_fact_observations_v2_as_known",
        "fact_observations_v2",
        ["fact_cell_id", "knowledge_at", "recorded_at", "observation_id"],
    )
    op.create_index(
        "ix_fact_observations_v2_document",
        "fact_observations_v2",
        ["document_version_id", "evidence_node_id"],
    )

    op.create_table(
        "fact_observation_relations_v2",
        sa.Column("relation_id", sa.String(128), primary_key=True),
        sa.Column("idempotency_key", sa.String(256), nullable=False, unique=True),
        sa.Column(
            "subject_observation_id",
            sa.String(128),
            sa.ForeignKey("fact_observations_v2.observation_id"),
            nullable=False,
        ),
        sa.Column(
            "object_observation_id",
            sa.String(128),
            sa.ForeignKey("fact_observations_v2.observation_id"),
            nullable=False,
        ),
        sa.Column("relation_kind", sa.String(40), nullable=False),
        sa.Column("reason_code", sa.String(128), nullable=False),
        sa.Column("reason_details_json", sa.Text(), nullable=False),
        sa.Column("policy_name", sa.String(128), nullable=False),
        sa.Column("policy_version", sa.String(64), nullable=False),
        sa.Column("policy_config_sha256", sa.String(64), nullable=False),
        *_clock_columns(),
        sa.UniqueConstraint(
            "subject_observation_id",
            "object_observation_id",
            "relation_kind",
            name="uq_fact_observation_relation_v2",
        ),
        sa.CheckConstraint(
            "subject_observation_id <> object_observation_id",
            name="ck_fact_observation_relation_v2_distinct",
        ),
        sa.CheckConstraint(
            "relation_kind IN "
            "('exact_duplicate_of', 'amends', 'reissues', "
            "'presentation_recast_of', 'conflicts_with', "
            "'supersedes_for_as_known')",
            name="ck_fact_observation_relation_v2_kind",
        ),
        sa.CheckConstraint(
            "json_valid(reason_details_json) "
            "AND json_type(reason_details_json) = 'object'",
            name="ck_fact_observation_relation_v2_reason",
        ),
        sa.CheckConstraint(
            _hex_check("policy_config_sha256"),
            name="ck_fact_observation_relation_v2_hash",
        ),
        sa.CheckConstraint(
            "knowledge_at >= effective_at AND recorded_at >= knowledge_at",
            name="ck_fact_observation_relation_v2_clocks",
        ),
    )
    op.create_index(
        "ix_fact_observation_relations_v2_subject",
        "fact_observation_relations_v2",
        ["subject_observation_id", "knowledge_at"],
    )
    op.create_index(
        "ix_fact_observation_relations_v2_object",
        "fact_observation_relations_v2",
        ["object_observation_id", "knowledge_at"],
    )

    # Candidates are staged under an immutable set id.  Inserting the
    # resolution revision finalizes that exact set; a trigger then prohibits
    # adding more members.  This avoids a mutable "finalized" flag.
    op.create_table(
        "fact_resolution_candidates_v2",
        sa.Column("candidate_id", sa.String(128), primary_key=True),
        sa.Column("idempotency_key", sa.String(256), nullable=False, unique=True),
        sa.Column("candidate_set_id", sa.String(128), nullable=False),
        sa.Column(
            "fact_cell_id",
            sa.String(128),
            sa.ForeignKey("fact_cells_v2.fact_cell_id"),
            nullable=False,
        ),
        sa.Column(
            "observation_id",
            sa.String(128),
            sa.ForeignKey("fact_observations_v2.observation_id"),
            nullable=False,
        ),
        sa.Column("candidate_ordinal", sa.Integer(), nullable=False),
        sa.Column("eligibility", sa.String(16), nullable=False),
        sa.Column("reason_code", sa.String(128), nullable=False),
        sa.Column("reason_details_json", sa.Text(), nullable=False),
        sa.Column("candidate_payload_sha256", sa.String(64), nullable=False),
        sa.Column("recorded_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint(
            "candidate_set_id",
            "candidate_ordinal",
            name="uq_fact_resolution_candidate_v2_ordinal",
        ),
        sa.UniqueConstraint(
            "candidate_set_id",
            "observation_id",
            name="uq_fact_resolution_candidate_v2_observation",
        ),
        sa.CheckConstraint(
            "candidate_ordinal >= 0",
            name="ck_fact_resolution_candidate_v2_ordinal",
        ),
        sa.CheckConstraint(
            "eligibility IN ('eligible', 'ineligible')",
            name="ck_fact_resolution_candidate_v2_eligibility",
        ),
        sa.CheckConstraint(
            "json_valid(reason_details_json) "
            "AND json_type(reason_details_json) = 'object'",
            name="ck_fact_resolution_candidate_v2_reason",
        ),
        sa.CheckConstraint(
            _hex_check("candidate_payload_sha256"),
            name="ck_fact_resolution_candidate_v2_hash",
        ),
    )
    op.create_index(
        "ix_fact_resolution_candidates_v2_set",
        "fact_resolution_candidates_v2",
        ["candidate_set_id", "fact_cell_id", "candidate_ordinal"],
    )

    op.create_table(
        "fact_resolution_revisions_v2",
        sa.Column("resolution_revision_id", sa.String(128), primary_key=True),
        sa.Column("idempotency_key", sa.String(256), nullable=False, unique=True),
        sa.Column(
            "fact_cell_id",
            sa.String(128),
            sa.ForeignKey("fact_cells_v2.fact_cell_id"),
            nullable=False,
        ),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column(
            "selected_observation_id",
            sa.String(128),
            sa.ForeignKey("fact_observations_v2.observation_id"),
            nullable=True,
        ),
        sa.Column("candidate_set_id", sa.String(128), nullable=False, unique=True),
        sa.Column("candidate_count", sa.Integer(), nullable=False),
        sa.Column("candidate_set_digest_sha256", sa.String(64), nullable=False),
        sa.Column("policy_name", sa.String(128), nullable=False),
        sa.Column("policy_version", sa.String(64), nullable=False),
        sa.Column("policy_config_sha256", sa.String(64), nullable=False),
        sa.Column("reason_code", sa.String(128), nullable=False),
        sa.Column("reason_details_json", sa.Text(), nullable=False),
        *_clock_columns(),
        sa.Column(
            "supersedes_resolution_revision_id",
            sa.String(128),
            sa.ForeignKey(
                "fact_resolution_revisions_v2.resolution_revision_id"
            ),
            nullable=True,
        ),
        sa.UniqueConstraint(
            "fact_cell_id",
            "revision",
            name="uq_fact_resolution_revision_v2",
        ),
        sa.CheckConstraint(
            "revision > 0 AND candidate_count >= 0",
            name="ck_fact_resolution_revision_v2_positive",
        ),
        sa.CheckConstraint(
            "status IN ('resolved', 'unresolved', 'retired')",
            name="ck_fact_resolution_revision_v2_status",
        ),
        sa.CheckConstraint(
            "(status = 'resolved' AND selected_observation_id IS NOT NULL "
            "AND candidate_count > 0) OR "
            "(status IN ('unresolved', 'retired') "
            "AND selected_observation_id IS NULL)",
            name="ck_fact_resolution_revision_v2_selection",
        ),
        sa.CheckConstraint(
            _hex_check("candidate_set_digest_sha256")
            + " AND "
            + _hex_check("policy_config_sha256"),
            name="ck_fact_resolution_revision_v2_hashes",
        ),
        sa.CheckConstraint(
            "json_valid(reason_details_json) "
            "AND json_type(reason_details_json) = 'object'",
            name="ck_fact_resolution_revision_v2_reason",
        ),
        sa.CheckConstraint(
            "knowledge_at >= effective_at AND recorded_at >= knowledge_at",
            name="ck_fact_resolution_revision_v2_clocks",
        ),
    )
    op.create_index(
        "ix_fact_resolution_revisions_v2_current",
        "fact_resolution_revisions_v2",
        ["fact_cell_id", "revision"],
    )
    op.create_index(
        "ix_fact_resolution_revisions_v2_as_known",
        "fact_resolution_revisions_v2",
        ["fact_cell_id", "knowledge_at", "revision"],
    )

    op.create_table(
        "fact_derivation_input_edges_v2",
        sa.Column("edge_id", sa.String(128), primary_key=True),
        sa.Column("idempotency_key", sa.String(256), nullable=False, unique=True),
        sa.Column(
            "output_observation_id",
            sa.String(128),
            sa.ForeignKey("fact_observations_v2.observation_id"),
            nullable=False,
        ),
        sa.Column(
            "input_observation_id",
            sa.String(128),
            sa.ForeignKey("fact_observations_v2.observation_id"),
            nullable=False,
        ),
        sa.Column(
            "input_resolution_revision_id",
            sa.String(128),
            sa.ForeignKey(
                "fact_resolution_revisions_v2.resolution_revision_id"
            ),
            nullable=True,
        ),
        sa.Column("input_role", sa.String(128), nullable=False),
        sa.Column("input_ordinal", sa.Integer(), nullable=False),
        sa.Column("recorded_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint(
            "output_observation_id",
            "input_ordinal",
            name="uq_fact_derivation_edge_v2_ordinal",
        ),
        sa.UniqueConstraint(
            "output_observation_id",
            "input_observation_id",
            "input_role",
            name="uq_fact_derivation_edge_v2_input",
        ),
        sa.CheckConstraint(
            "output_observation_id <> input_observation_id "
            "AND input_ordinal >= 0 AND length(trim(input_role)) > 0",
            name="ck_fact_derivation_edge_v2_shape",
        ),
    )
    op.create_index(
        "ix_fact_derivation_edges_v2_output",
        "fact_derivation_input_edges_v2",
        ["output_observation_id", "input_ordinal"],
    )
    op.create_index(
        "ix_fact_derivation_edges_v2_input",
        "fact_derivation_input_edges_v2",
        ["input_observation_id"],
    )

    op.create_table(
        "fact_derivation_seals_v2",
        sa.Column("derivation_seal_id", sa.String(128), primary_key=True),
        sa.Column("idempotency_key", sa.String(256), nullable=False, unique=True),
        sa.Column(
            "output_observation_id",
            sa.String(128),
            sa.ForeignKey("fact_observations_v2.observation_id"),
            nullable=False,
            unique=True,
        ),
        sa.Column("input_count", sa.Integer(), nullable=False),
        sa.Column("canonical_input_digest_sha256", sa.String(64), nullable=False),
        sa.Column("formula_config_sha256", sa.String(64), nullable=False),
        sa.Column("seal_method", sa.String(128), nullable=False),
        sa.Column("seal_method_version", sa.String(64), nullable=False),
        *_clock_columns(),
        sa.CheckConstraint(
            "input_count > 0",
            name="ck_fact_derivation_seal_v2_input_count",
        ),
        sa.CheckConstraint(
            _hex_check("canonical_input_digest_sha256")
            + " AND "
            + _hex_check("formula_config_sha256"),
            name="ck_fact_derivation_seal_v2_hashes",
        ),
        sa.CheckConstraint(
            "knowledge_at >= effective_at AND recorded_at >= knowledge_at",
            name="ck_fact_derivation_seal_v2_clocks",
        ),
    )

    # Cross-row identity and provenance guards.
    op.execute(
        "CREATE TRIGGER trg_fact_cells_v2_security_scope "
        "BEFORE INSERT ON fact_cells_v2 WHEN NEW.scope_security_id IS NOT NULL "
        "AND NOT EXISTS ("
        "SELECT 1 FROM reporting_entities AS entity "
        "JOIN securities AS security ON security.issuer_id = entity.issuer_id "
        "WHERE entity.reporting_entity_id = NEW.reporting_entity_id "
        "AND security.security_id = NEW.scope_security_id) "
        "BEGIN SELECT RAISE(ABORT, "
        "'fact cell security must belong to reporting entity issuer'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_fact_cells_v2_security_relationship "
        "BEFORE INSERT ON fact_cells_v2 WHEN NEW.scope_security_id IS NOT NULL "
        "AND NOT EXISTS ("
        "SELECT 1 FROM v_security_reporting_entities_current AS relation "
        "WHERE relation.reporting_entity_id = NEW.reporting_entity_id "
        "AND relation.security_id = NEW.scope_security_id) "
        "BEGIN SELECT RAISE(ABORT, "
        "'fact cell security must have a current reporting relationship'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_fact_observations_v2_revision_parent "
        "BEFORE INSERT ON fact_observations_v2 "
        "WHEN NEW.supersedes_observation_id IS NOT NULL AND NOT EXISTS ("
        "SELECT 1 FROM fact_observations_v2 AS prior "
        "WHERE prior.observation_id = NEW.supersedes_observation_id "
        "AND prior.fact_cell_id = NEW.fact_cell_id "
        "AND NEW.knowledge_at >= prior.knowledge_at) "
        "BEGIN SELECT RAISE(ABORT, "
        "'observation revision parent must be same-cell and already known'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_fact_observations_v2_reported_anchor "
        "BEFORE INSERT ON fact_observations_v2 "
        "WHEN NEW.observation_kind = 'reported' AND NOT EXISTS ("
        "SELECT 1 FROM fact_cells_v2 AS cell "
        "JOIN reporting_entities AS entity "
        "ON entity.reporting_entity_id = cell.reporting_entity_id "
        "JOIN v_evidence_document_versions_canonical AS document "
        "ON document.document_version_id = NEW.document_version_id "
        "JOIN evidence_extraction_runs AS run "
        "ON run.document_version_id = document.document_version_id "
        "JOIN evidence_nodes AS node "
        "ON node.extraction_run_id = run.extraction_run_id "
        "WHERE cell.fact_cell_id = NEW.fact_cell_id "
        "AND node.node_id = NEW.evidence_node_id "
        "AND entity.issuer_id = document.issuer_id "
        "AND document.reporting_entity_id IS NOT NULL "
        "AND document.reporting_entity_id = cell.reporting_entity_id) "
        "BEGIN SELECT RAISE(ABORT, "
        "'reported observation requires same-entity document and evidence node'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_fact_observations_v2_match_scope "
        "BEFORE INSERT ON fact_observations_v2 "
        "WHEN NEW.legacy_match_revision_id IS NOT NULL AND NOT EXISTS ("
        "SELECT 1 FROM fact_cells_v2 AS cell "
        "JOIN reporting_entities AS entity "
        "ON entity.reporting_entity_id = cell.reporting_entity_id "
        "JOIN v_legacy_fact_evidence_matches_accepted_current AS match "
        "ON match.match_revision_id = NEW.legacy_match_revision_id "
        "WHERE cell.fact_cell_id = NEW.fact_cell_id "
        "AND entity.issuer_id = match.issuer_id "
        "AND match.evidence_node_id = NEW.evidence_node_id "
        "AND match.matched_entry_sha256 = NEW.source_entry_sha256 "
        "AND NEW.knowledge_at >= match.knowledge_at) "
        "BEGIN SELECT RAISE(ABORT, "
        "'legacy match must be current and agree with the reported anchor'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_fact_observation_relations_v2_scope "
        "BEFORE INSERT ON fact_observation_relations_v2 WHEN NOT EXISTS ("
        "SELECT 1 FROM fact_observations_v2 AS subject "
        "JOIN fact_observations_v2 AS object "
        "ON object.observation_id = NEW.object_observation_id "
        "WHERE subject.observation_id = NEW.subject_observation_id "
        "AND subject.fact_cell_id = object.fact_cell_id "
        "AND NEW.knowledge_at >= subject.knowledge_at "
        "AND NEW.knowledge_at >= object.knowledge_at) "
        "BEGIN SELECT RAISE(ABORT, "
        "'observation relation requires same-cell already-known observations'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_fact_resolution_candidates_v2_scope "
        "BEFORE INSERT ON fact_resolution_candidates_v2 WHEN NOT EXISTS ("
        "SELECT 1 FROM fact_observations_v2 AS observation "
        "WHERE observation.observation_id = NEW.observation_id "
        "AND observation.fact_cell_id = NEW.fact_cell_id) "
        "BEGIN SELECT RAISE(ABORT, "
        "'resolution candidate observation must belong to fact cell'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_fact_resolution_candidates_v2_finalized "
        "BEFORE INSERT ON fact_resolution_candidates_v2 WHEN EXISTS ("
        "SELECT 1 FROM fact_resolution_revisions_v2 AS resolution "
        "WHERE resolution.candidate_set_id = NEW.candidate_set_id) "
        "BEGIN SELECT RAISE(ABORT, "
        "'resolution candidate set is finalized'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_fact_resolution_revisions_v2_first "
        "BEFORE INSERT ON fact_resolution_revisions_v2 "
        "WHEN NEW.revision = 1 "
        "AND NEW.supersedes_resolution_revision_id IS NOT NULL "
        "BEGIN SELECT RAISE(ABORT, "
        "'first fact resolution cannot supersede'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_fact_resolution_revisions_v2_parent "
        "BEFORE INSERT ON fact_resolution_revisions_v2 "
        "WHEN NEW.revision > 1 AND ("
        "NEW.supersedes_resolution_revision_id IS NULL OR NOT EXISTS ("
        "SELECT 1 FROM fact_resolution_revisions_v2 AS prior "
        "WHERE prior.resolution_revision_id = "
        "NEW.supersedes_resolution_revision_id "
        "AND prior.fact_cell_id = NEW.fact_cell_id "
        "AND prior.revision = NEW.revision - 1 "
        "AND NEW.knowledge_at >= prior.knowledge_at)) "
        "BEGIN SELECT RAISE(ABORT, "
        "'fact resolution must supersede prior same-cell revision'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_fact_resolution_revisions_v2_candidates "
        "BEFORE INSERT ON fact_resolution_revisions_v2 WHEN "
        "NEW.candidate_count <> (SELECT COUNT(*) "
        "FROM fact_resolution_candidates_v2 AS candidate "
        "WHERE candidate.candidate_set_id = NEW.candidate_set_id) "
        "OR EXISTS (SELECT 1 FROM fact_resolution_candidates_v2 AS candidate "
        "WHERE candidate.candidate_set_id = NEW.candidate_set_id "
        "AND candidate.fact_cell_id <> NEW.fact_cell_id) "
        "OR EXISTS (SELECT 1 FROM fact_resolution_candidates_v2 AS candidate "
        "JOIN fact_observations_v2 AS observation "
        "ON observation.observation_id = candidate.observation_id "
        "WHERE candidate.candidate_set_id = NEW.candidate_set_id "
        "AND (observation.knowledge_at > NEW.knowledge_at "
        "OR candidate.recorded_at > NEW.recorded_at)) "
        "BEGIN SELECT RAISE(ABORT, "
        "'resolution requires its complete same-cell already-known candidate set'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_fact_resolution_revisions_v2_selected "
        "BEFORE INSERT ON fact_resolution_revisions_v2 "
        "WHEN NEW.selected_observation_id IS NOT NULL AND NOT EXISTS ("
        "SELECT 1 FROM fact_resolution_candidates_v2 AS candidate "
        "WHERE candidate.candidate_set_id = NEW.candidate_set_id "
        "AND candidate.fact_cell_id = NEW.fact_cell_id "
        "AND candidate.observation_id = NEW.selected_observation_id "
        "AND candidate.eligibility = 'eligible') "
        "BEGIN SELECT RAISE(ABORT, "
        "'selected observation must be an eligible finalized candidate'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_fact_resolution_revisions_v2_derived_seals "
        "BEFORE INSERT ON fact_resolution_revisions_v2 WHEN EXISTS ("
        "SELECT 1 FROM fact_resolution_candidates_v2 AS candidate "
        "JOIN fact_observations_v2 AS observation "
        "ON observation.observation_id = candidate.observation_id "
        "WHERE candidate.candidate_set_id = NEW.candidate_set_id "
        "AND observation.observation_kind = 'derived' "
        "AND NOT EXISTS (SELECT 1 FROM fact_derivation_seals_v2 AS seal "
        "WHERE seal.output_observation_id = observation.observation_id "
        "AND seal.knowledge_at <= NEW.knowledge_at)) "
        "BEGIN SELECT RAISE(ABORT, "
        "'derived resolution candidates require finalized derivation seals'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_fact_derivation_edges_v2_output "
        "BEFORE INSERT ON fact_derivation_input_edges_v2 WHEN NOT EXISTS ("
        "SELECT 1 FROM fact_observations_v2 AS output "
        "JOIN fact_observations_v2 AS input "
        "ON input.observation_id = NEW.input_observation_id "
        "WHERE output.observation_id = NEW.output_observation_id "
        "AND output.observation_kind = 'derived' "
        "AND NEW.recorded_at >= output.recorded_at "
        "AND NEW.recorded_at >= input.recorded_at) "
        "BEGIN SELECT RAISE(ABORT, "
        "'derivation edge requires a derived output and recorded input'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_fact_derivation_edges_v2_resolution "
        "BEFORE INSERT ON fact_derivation_input_edges_v2 "
        "WHEN NEW.input_resolution_revision_id IS NOT NULL AND NOT EXISTS ("
        "SELECT 1 FROM fact_resolution_revisions_v2 AS resolution "
        "JOIN fact_observations_v2 AS input "
        "ON input.observation_id = NEW.input_observation_id "
        "WHERE resolution.resolution_revision_id = "
        "NEW.input_resolution_revision_id "
        "AND resolution.status = 'resolved' "
        "AND resolution.selected_observation_id = NEW.input_observation_id "
        "AND resolution.fact_cell_id = input.fact_cell_id) "
        "BEGIN SELECT RAISE(ABORT, "
        "'derivation input resolution must select the exact input observation'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_fact_derivation_edges_v2_sealed "
        "BEFORE INSERT ON fact_derivation_input_edges_v2 WHEN EXISTS ("
        "SELECT 1 FROM fact_derivation_seals_v2 AS seal "
        "WHERE seal.output_observation_id = NEW.output_observation_id) "
        "BEGIN SELECT RAISE(ABORT, 'derivation input set is sealed'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_fact_derivation_seals_v2_validate "
        "BEFORE INSERT ON fact_derivation_seals_v2 WHEN NOT EXISTS ("
        "SELECT 1 FROM fact_observations_v2 AS output "
        "WHERE output.observation_id = NEW.output_observation_id "
        "AND output.observation_kind = 'derived' "
        "AND NEW.knowledge_at >= output.knowledge_at) "
        "OR NEW.input_count <> (SELECT COUNT(*) "
        "FROM fact_derivation_input_edges_v2 AS edge "
        "WHERE edge.output_observation_id = NEW.output_observation_id) "
        "OR EXISTS (SELECT 1 FROM fact_derivation_input_edges_v2 AS edge "
        "WHERE edge.output_observation_id = NEW.output_observation_id "
        "AND edge.recorded_at > NEW.recorded_at) "
        "BEGIN SELECT RAISE(ABORT, "
        "'derivation seal requires the complete already-recorded input set'); END"
    )

    for table, message in (
        ("fact_cells_v2", "fact cell"),
        ("fact_observations_v2", "fact observation"),
        ("fact_observation_relations_v2", "fact observation relation"),
        ("fact_resolution_candidates_v2", "fact resolution candidate"),
        ("fact_resolution_revisions_v2", "fact resolution revision"),
        ("fact_derivation_input_edges_v2", "fact derivation edge"),
        ("fact_derivation_seals_v2", "fact derivation seal"),
    ):
        _append_only(table, message)

    op.execute(
        "CREATE VIEW v_fact_resolutions_current_v2 AS "
        "SELECT resolution.* FROM fact_resolution_revisions_v2 AS resolution "
        "WHERE NOT EXISTS ("
        "SELECT 1 FROM fact_resolution_revisions_v2 AS newer "
        "WHERE newer.fact_cell_id = resolution.fact_cell_id "
        "AND newer.revision > resolution.revision)"
    )
    op.execute(
        "CREATE VIEW v_fact_cells_resolved_current_v2 AS "
        "SELECT cell.*, resolution.resolution_revision_id, "
        "resolution.revision AS resolution_revision, "
        "resolution.selected_observation_id, "
        "resolution.knowledge_at AS resolution_knowledge_at, "
        "observation.observation_kind, observation.value_kind, "
        "observation.numeric_value, observation.text_value, observation.is_nil, "
        "observation.raw_lexical_value "
        "FROM v_fact_resolutions_current_v2 AS resolution "
        "JOIN fact_cells_v2 AS cell "
        "ON cell.fact_cell_id = resolution.fact_cell_id "
        "JOIN fact_observations_v2 AS observation "
        "ON observation.observation_id = resolution.selected_observation_id "
        "WHERE resolution.status = 'resolved'"
    )
    op.execute(
        "CREATE VIEW v_fact_observations_as_reported_v2 AS "
        "SELECT cell.*, observation.observation_id, observation.value_kind, "
        "observation.numeric_value, observation.text_value, observation.is_nil, "
        "observation.raw_lexical_value, observation.document_version_id, "
        "observation.evidence_node_id, observation.source_locator_json, "
        "observation.source_locator_sha256, observation.source_entry_sha256, "
        "observation.source_context_id, observation.source_unit_id, "
        "observation.decimals, observation.precision, "
        "observation.legacy_match_revision_id, observation.revision_kind, "
        "observation.effective_at AS observation_effective_at, "
        "observation.knowledge_at AS observation_knowledge_at, "
        "observation.recorded_at AS observation_recorded_at "
        "FROM fact_observations_v2 AS observation "
        "JOIN fact_cells_v2 AS cell "
        "ON cell.fact_cell_id = observation.fact_cell_id "
        "WHERE observation.observation_kind = 'reported'"
    )


def downgrade() -> None:
    for view in _VIEWS:
        op.execute(f"DROP VIEW IF EXISTS {view}")
    for table in reversed(_TABLES):
        op.drop_table(table)
