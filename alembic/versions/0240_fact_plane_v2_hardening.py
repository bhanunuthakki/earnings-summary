"""Harden the evidence-first fact plane with database-enforced commitments.

Revision ID: 0240_fact_plane_v2_hardening
Revises: 0239_structured_fact_search_projection

The 0238 plane is not yet a production write surface.  This migration therefore
fails if any v2 fact/search rows exist, then adds immutable, authoritative
sidecars rather than guessing how to rewrite evidence.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0240_fact_plane_v2_hardening"
down_revision: str | Sequence[str] | None = (
    "0239_structured_fact_search_projection"
)
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLES = (
    "fact_dimensions_normalized_v2",
    "fact_cell_identity_seals_v2",
    "fact_reported_observation_anchors_v2",
    "fact_observation_payload_commitments_v2",
    "fact_derivation_basis_commitments_v2",
    "fact_extraction_run_completeness_seals_v2",
)
_VIEWS = (
    "v_fact_cells_hardened_v2",
    "v_fact_reported_anchors_selected_v2",
    "v_fact_observations_committed_v2",
    "v_fact_extraction_runs_complete_v2",
)


def _hex_check(column: str) -> str:
    return (
        f"length({column}) = 64 AND "
        f"{column} NOT GLOB '*[^0-9a-f]*'"
    )


def _append_only(table: str, label: str) -> None:
    op.execute(
        f"CREATE TRIGGER trg_{table}_append_only BEFORE UPDATE ON {table} "
        f"BEGIN SELECT RAISE(ABORT, '{label} is append-only'); END"
    )
    op.execute(
        f"CREATE TRIGGER trg_{table}_append_only_delete BEFORE DELETE ON {table} "
        f"BEGIN SELECT RAISE(ABORT, '{label} is append-only'); END"
    )


def _require_empty(bind: sa.Connection) -> None:
    guarded = (
        "fact_resolution_revisions_v2",
        "fact_resolution_candidates_v2",
        "fact_derivation_seals_v2",
        "fact_derivation_input_edges_v2",
        "fact_observation_relations_v2",
        "fact_observations_v2",
        "fact_cells_v2",
        "search_fact_projection_seals",
        "search_fact_projection_rows",
        "search_fact_projection_memberships",
        "search_fact_projection_runs",
    )
    nonempty = [
        table
        for table in guarded
        if int(
            bind.execute(sa.text(f"SELECT COUNT(*) FROM {table}")).scalar_one()
        )
        > 0
    ]
    if nonempty:
        raise RuntimeError(
            "0240 refuses to reinterpret existing v2 evidence; export/review "
            "and rebuild the empty plane first: "
            + ", ".join(nonempty)
        )


def upgrade() -> None:
    bind = op.get_bind()
    required = {
        "fact_cells_v2",
        "fact_observations_v2",
        "fact_resolution_candidates_v2",
        "fact_resolution_revisions_v2",
        "fact_derivation_input_edges_v2",
        "fact_derivation_seals_v2",
        "recorded_subject_binding_revisions",
        "evidence_document_versions",
        "evidence_extraction_runs",
        "evidence_nodes",
        "search_fact_projection_memberships",
        "search_fact_projection_rows",
    }
    missing = sorted(required - set(sa.inspect(bind).get_table_names()))
    if missing:
        raise RuntimeError(
            "fact-plane hardening requires its complete predecessor graph: "
            + ", ".join(missing)
        )
    _require_empty(bind)

    op.create_table(
        "fact_dimensions_normalized_v2",
        sa.Column("dimension_id", sa.String(128), primary_key=True),
        sa.Column("idempotency_key", sa.String(256), nullable=False, unique=True),
        sa.Column(
            "fact_cell_id",
            sa.String(128),
            sa.ForeignKey("fact_cells_v2.fact_cell_id"),
            nullable=False,
        ),
        sa.Column("dimension_ordinal", sa.Integer(), nullable=False),
        sa.Column("axis_namespace", sa.Text(), nullable=False),
        sa.Column("axis_name", sa.Text(), nullable=False),
        sa.Column("member_kind", sa.String(16), nullable=False),
        sa.Column("explicit_member_namespace", sa.Text(), nullable=True),
        sa.Column("explicit_member_name", sa.Text(), nullable=True),
        sa.Column("typed_member_value_json", sa.Text(), nullable=True),
        sa.Column("typed_member_value_sha256", sa.String(64), nullable=True),
        sa.Column("recorded_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint(
            "fact_cell_id",
            "dimension_ordinal",
            name="uq_fact_dimension_normalized_v2_ordinal",
        ),
        sa.UniqueConstraint(
            "fact_cell_id",
            "axis_namespace",
            "axis_name",
            name="uq_fact_dimension_normalized_v2_axis",
        ),
        sa.CheckConstraint(
            "dimension_ordinal >= 0",
            name="ck_fact_dimension_normalized_v2_ordinal",
        ),
        sa.CheckConstraint(
            "length(trim(axis_namespace)) > 0 "
            "AND length(trim(axis_name)) > 0",
            name="ck_fact_dimension_normalized_v2_axis",
        ),
        sa.CheckConstraint(
            "(member_kind = 'explicit' "
            "AND explicit_member_namespace IS NOT NULL "
            "AND explicit_member_name IS NOT NULL "
            "AND typed_member_value_json IS NULL "
            "AND typed_member_value_sha256 IS NULL) OR "
            "(member_kind = 'typed' "
            "AND explicit_member_namespace IS NULL "
            "AND explicit_member_name IS NULL "
            "AND typed_member_value_json IS NOT NULL "
            "AND typed_member_value_sha256 IS NOT NULL)",
            name="ck_fact_dimension_normalized_v2_member",
        ),
        sa.CheckConstraint(
            "typed_member_value_json IS NULL OR "
            "(json_valid(typed_member_value_json) "
            "AND json_type(typed_member_value_json) = 'object')",
            name="ck_fact_dimension_normalized_v2_typed_json",
        ),
        sa.CheckConstraint(
            "typed_member_value_sha256 IS NULL OR "
            f"({_hex_check('typed_member_value_sha256')})",
            name="ck_fact_dimension_normalized_v2_typed_hash",
        ),
    )

    op.create_table(
        "fact_cell_identity_seals_v2",
        sa.Column(
            "fact_cell_id",
            sa.String(128),
            sa.ForeignKey("fact_cells_v2.fact_cell_id"),
            primary_key=True,
        ),
        sa.Column("idempotency_key", sa.String(256), nullable=False, unique=True),
        sa.Column("semantic_key_version", sa.String(64), nullable=False),
        sa.Column("semantic_identity_json", sa.Text(), nullable=False),
        sa.Column("semantic_key_sha256", sa.String(64), nullable=False, unique=True),
        sa.Column("dimension_count", sa.Integer(), nullable=False),
        sa.Column("dimension_set_json", sa.Text(), nullable=False),
        sa.Column("dimension_set_sha256", sa.String(64), nullable=False),
        sa.Column("sealed_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "semantic_key_version = 'fact_cell_semantic_key.v3'",
            name="ck_fact_cell_identity_seal_v2_version",
        ),
        sa.CheckConstraint(
            "dimension_count >= 0",
            name="ck_fact_cell_identity_seal_v2_count",
        ),
        sa.CheckConstraint(
            "json_valid(semantic_identity_json) "
            "AND json_type(semantic_identity_json) = 'object' "
            "AND json_valid(dimension_set_json) "
            "AND json_type(dimension_set_json) = 'array'",
            name="ck_fact_cell_identity_seal_v2_json",
        ),
        sa.CheckConstraint(
            _hex_check("semantic_key_sha256")
            + " AND "
            + _hex_check("dimension_set_sha256"),
            name="ck_fact_cell_identity_seal_v2_hashes",
        ),
    )

    op.create_table(
        "fact_reported_observation_anchors_v2",
        sa.Column(
            "observation_id",
            sa.String(128),
            sa.ForeignKey("fact_observations_v2.observation_id"),
            primary_key=True,
        ),
        sa.Column("idempotency_key", sa.String(256), nullable=False, unique=True),
        sa.Column(
            "subject_binding_revision_id",
            sa.String(128),
            sa.ForeignKey(
                "recorded_subject_binding_revisions.binding_revision_id"
            ),
            nullable=False,
        ),
        sa.Column(
            "extraction_run_id",
            sa.String(128),
            sa.ForeignKey("evidence_extraction_runs.extraction_run_id"),
            nullable=False,
        ),
        sa.Column("source_taxonomy_version", sa.Text(), nullable=False),
        sa.Column("extractor_name", sa.String(128), nullable=False),
        sa.Column("extractor_code_version", sa.String(255), nullable=False),
        sa.Column("extractor_config_sha256", sa.String(64), nullable=False),
        sa.Column("extraction_input_sha256", sa.String(64), nullable=False),
        sa.Column("extraction_output_sha256", sa.String(64), nullable=False),
        sa.Column("raw_entry_sha256", sa.String(64), nullable=False),
        sa.Column("anchor_payload_json", sa.Text(), nullable=False),
        sa.Column("anchor_payload_sha256", sa.String(64), nullable=False),
        sa.Column("recorded_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "length(trim(source_taxonomy_version)) > 0 "
            "AND length(trim(extractor_name)) > 0 "
            "AND length(trim(extractor_code_version)) > 0",
            name="ck_fact_reported_anchor_v2_required",
        ),
        sa.CheckConstraint(
            _hex_check("extractor_config_sha256")
            + " AND "
            + _hex_check("extraction_input_sha256")
            + " AND "
            + _hex_check("extraction_output_sha256")
            + " AND "
            + _hex_check("raw_entry_sha256")
            + " AND "
            + _hex_check("anchor_payload_sha256"),
            name="ck_fact_reported_anchor_v2_hashes",
        ),
        sa.CheckConstraint(
            "json_valid(anchor_payload_json) "
            "AND json_type(anchor_payload_json) = 'object'",
            name="ck_fact_reported_anchor_v2_json",
        ),
    )

    op.create_table(
        "fact_observation_payload_commitments_v2",
        sa.Column(
            "observation_id",
            sa.String(128),
            sa.ForeignKey("fact_observations_v2.observation_id"),
            primary_key=True,
        ),
        sa.Column("idempotency_key", sa.String(256), nullable=False, unique=True),
        sa.Column("payload_version", sa.String(64), nullable=False),
        sa.Column("canonical_payload_json", sa.Text(), nullable=False),
        sa.Column(
            "observation_payload_sha256",
            sa.String(64),
            nullable=False,
            unique=True,
        ),
        sa.Column("committed_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "payload_version = 'fact_observation_payload.v1'",
            name="ck_fact_observation_payload_v2_version",
        ),
        sa.CheckConstraint(
            "json_valid(canonical_payload_json) "
            "AND json_type(canonical_payload_json) = 'object'",
            name="ck_fact_observation_payload_v2_json",
        ),
        sa.CheckConstraint(
            _hex_check("observation_payload_sha256"),
            name="ck_fact_observation_payload_v2_hash",
        ),
    )

    op.create_table(
        "fact_derivation_basis_commitments_v2",
        sa.Column(
            "derivation_seal_id",
            sa.String(128),
            sa.ForeignKey("fact_derivation_seals_v2.derivation_seal_id"),
            primary_key=True,
        ),
        sa.Column("idempotency_key", sa.String(256), nullable=False, unique=True),
        sa.Column("input_basis", sa.String(16), nullable=False),
        sa.Column("formula_id", sa.String(128), nullable=False),
        sa.Column("formula_version", sa.String(128), nullable=False),
        sa.Column("formula_definition_sha256", sa.String(64), nullable=False),
        sa.Column("execution_config_sha256", sa.String(64), nullable=False),
        sa.Column("knowledge_cutoff", sa.DateTime(), nullable=False),
        sa.Column("canonical_basis_json", sa.Text(), nullable=False),
        sa.Column("canonical_basis_sha256", sa.String(64), nullable=False),
        sa.Column("recorded_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "input_basis IN ('as_reported', 'as_known')",
            name="ck_fact_derivation_basis_v2_kind",
        ),
        sa.CheckConstraint(
            _hex_check("formula_definition_sha256")
            + " AND "
            + _hex_check("execution_config_sha256")
            + " AND "
            + _hex_check("canonical_basis_sha256"),
            name="ck_fact_derivation_basis_v2_hashes",
        ),
        sa.CheckConstraint(
            "json_valid(canonical_basis_json) "
            "AND json_type(canonical_basis_json) = 'object'",
            name="ck_fact_derivation_basis_v2_json",
        ),
    )

    op.create_table(
        "fact_extraction_run_completeness_seals_v2",
        sa.Column("extraction_seal_id", sa.String(128), primary_key=True),
        sa.Column("idempotency_key", sa.String(256), nullable=False, unique=True),
        sa.Column(
            "extraction_run_id",
            sa.String(128),
            sa.ForeignKey("evidence_extraction_runs.extraction_run_id"),
            nullable=False,
            unique=True,
        ),
        sa.Column("expected_node_count", sa.Integer(), nullable=False),
        sa.Column("observed_node_count", sa.Integer(), nullable=False),
        sa.Column("reported_fact_count", sa.Integer(), nullable=False),
        sa.Column("node_set_json", sa.Text(), nullable=False),
        sa.Column("node_set_sha256", sa.String(64), nullable=False),
        sa.Column("observation_set_json", sa.Text(), nullable=False),
        sa.Column("observation_set_sha256", sa.String(64), nullable=False),
        sa.Column("extractor_config_sha256", sa.String(64), nullable=False),
        sa.Column("extraction_output_sha256", sa.String(64), nullable=False),
        sa.Column("completeness_policy_name", sa.String(128), nullable=False),
        sa.Column("completeness_policy_version", sa.String(64), nullable=False),
        sa.Column("completeness_policy_sha256", sa.String(64), nullable=False),
        sa.Column("knowledge_at", sa.DateTime(), nullable=False),
        sa.Column("recorded_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "expected_node_count >= 0 "
            "AND observed_node_count = expected_node_count "
            "AND reported_fact_count >= 0",
            name="ck_fact_extraction_seal_v2_counts",
        ),
        sa.CheckConstraint(
            "json_valid(node_set_json) AND json_type(node_set_json) = 'array' "
            "AND json_valid(observation_set_json) "
            "AND json_type(observation_set_json) = 'array'",
            name="ck_fact_extraction_seal_v2_json",
        ),
        sa.CheckConstraint(
            _hex_check("node_set_sha256")
            + " AND "
            + _hex_check("observation_set_sha256")
            + " AND "
            + _hex_check("extractor_config_sha256")
            + " AND "
            + _hex_check("extraction_output_sha256")
            + " AND "
            + _hex_check("completeness_policy_sha256"),
            name="ck_fact_extraction_seal_v2_hashes",
        ),
        sa.CheckConstraint(
            "recorded_at >= knowledge_at",
            name="ck_fact_extraction_seal_v2_clocks",
        ),
    )

    # Replace 0238's current-binding/canonical-view check.  Exact identity is
    # now enforced by the immutable anchor revision below; the base-row guard
    # is intentionally limited to the exact document/run/node evidence chain.
    op.execute("DROP TRIGGER IF EXISTS trg_fact_observations_v2_reported_anchor")
    op.execute(
        "CREATE TRIGGER trg_fact_observations_v2_evidence_chain_hardened "
        "BEFORE INSERT ON fact_observations_v2 "
        "WHEN NEW.observation_kind = 'reported' AND NOT EXISTS ("
        "SELECT 1 FROM evidence_document_versions AS document "
        "JOIN evidence_extraction_runs AS run "
        "ON run.document_version_id = document.document_version_id "
        "JOIN evidence_nodes AS node "
        "ON node.extraction_run_id = run.extraction_run_id "
        "WHERE document.document_version_id = NEW.document_version_id "
        "AND node.node_id = NEW.evidence_node_id "
        "AND run.outcome = 'succeeded') "
        "BEGIN SELECT RAISE(ABORT, "
        "'reported observation requires exact document and evidence node'); END"
    )

    # Typed values and all canonical commitments are checked in SQLite.  The
    # fact_sha256 scalar is registered by the sole FactPlaneV2 write boundary;
    # an ungoverned connection therefore cannot forge committed rows.
    op.execute(
        "CREATE TRIGGER trg_fact_dimensions_normalized_v2_typed_digest "
        "BEFORE INSERT ON fact_dimensions_normalized_v2 "
        "WHEN NEW.member_kind = 'typed' AND "
        "NEW.typed_member_value_sha256 <> fact_sha256(NEW.typed_member_value_json) "
        "BEGIN SELECT RAISE(ABORT, 'typed dimension digest mismatch'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_fact_cell_identity_seals_v2_complete "
        "BEFORE INSERT ON fact_cell_identity_seals_v2 WHEN "
        "NEW.dimension_count <> (SELECT COUNT(*) "
        "FROM fact_dimensions_normalized_v2 AS dimension "
        "WHERE dimension.fact_cell_id = NEW.fact_cell_id) "
        "OR NEW.dimension_set_json <> COALESCE((SELECT json_group_array("
        "json(item.dimension_json)) FROM (SELECT json_object("
        "'axis_name', dimension.axis_name, "
        "'axis_namespace', dimension.axis_namespace, "
        "'explicit_member_name', dimension.explicit_member_name, "
        "'explicit_member_namespace', dimension.explicit_member_namespace, "
        "'member_kind', dimension.member_kind, "
        "'typed_member_value', CASE "
        "WHEN dimension.typed_member_value_json IS NULL THEN NULL "
        "ELSE json(dimension.typed_member_value_json) END"
        ") AS dimension_json FROM fact_dimensions_normalized_v2 AS dimension "
        "WHERE dimension.fact_cell_id = NEW.fact_cell_id "
        "ORDER BY dimension.dimension_ordinal) AS item), '[]') "
        "OR NEW.dimension_set_sha256 <> fact_sha256(NEW.dimension_set_json) "
        "OR NEW.semantic_key_sha256 <> fact_sha256(NEW.semantic_identity_json) "
        "OR NOT EXISTS (SELECT 1 FROM fact_cells_v2 AS cell "
        "WHERE cell.fact_cell_id = NEW.fact_cell_id "
        "AND NEW.semantic_identity_json = fact_cell_semantic_identity_v3("
        "cell.reporting_entity_id, cell.scope_security_id, "
        "cell.concept_namespace, cell.concept_name, cell.taxonomy_name, "
        "cell.accounting_basis, cell.consolidation_scope, cell.period_kind, "
        "cell.period_start, cell.period_end, NEW.dimension_set_json, "
        "cell.unit_key, cell.currency) "
        "AND cell.semantic_key_sha256 = NEW.semantic_key_sha256 "
        "AND cell.canonical_dimensions_sha256 = NEW.dimension_set_sha256) "
        "BEGIN SELECT RAISE(ABORT, 'fact-cell identity seal mismatch'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_fact_reported_anchors_v2_exact "
        "BEFORE INSERT ON fact_reported_observation_anchors_v2 WHEN NOT EXISTS ("
        "SELECT 1 FROM fact_observations_v2 AS observation "
        "JOIN fact_cells_v2 AS cell "
        "ON cell.fact_cell_id = observation.fact_cell_id "
        "JOIN reporting_entities AS reporting_entity "
        "ON reporting_entity.reporting_entity_id = cell.reporting_entity_id "
        "JOIN evidence_nodes AS node "
        "ON node.node_id = observation.evidence_node_id "
        "JOIN evidence_extraction_runs AS run "
        "ON run.extraction_run_id = node.extraction_run_id "
        "AND run.document_version_id = observation.document_version_id "
        "JOIN evidence_document_versions AS document "
        "ON document.document_version_id = observation.document_version_id "
        "JOIN recorded_subject_binding_revisions AS binding "
        "ON binding.binding_revision_id = NEW.subject_binding_revision_id "
        "WHERE observation.observation_id = NEW.observation_id "
        "AND observation.observation_kind = 'reported' "
        "AND binding.outcome = 'selected' "
        "AND binding.recorded_issuer_id = document.issuer_id "
        "AND binding.issuer_id = reporting_entity.issuer_id "
        "AND binding.reporting_entity_id = cell.reporting_entity_id "
        "AND (cell.scope_security_id IS NULL "
        "OR binding.security_id = cell.scope_security_id) "
        "AND run.extraction_run_id = NEW.extraction_run_id "
        "AND run.outcome = 'succeeded' "
        "AND run.extractor_name = NEW.extractor_name "
        "AND run.extractor_code_version = NEW.extractor_code_version "
        "AND run.extractor_config_sha256 = NEW.extractor_config_sha256 "
        "AND run.input_sha256 = NEW.extraction_input_sha256 "
        "AND run.output_sha256 = NEW.extraction_output_sha256 "
        "AND observation.source_entry_sha256 = NEW.raw_entry_sha256 "
        "AND binding.knowledge_at <= observation.knowledge_at "
        "AND binding.recorded_at <= observation.recorded_at "
        "AND run.completed_at <= observation.knowledge_at "
        "AND run.completed_at <= observation.recorded_at "
        "AND document.recorded_at <= observation.recorded_at "
        "AND node.recorded_at <= observation.recorded_at "
        "AND NEW.anchor_payload_json = fact_anchor_payload_v1("
        "observation.document_version_id, observation.evidence_node_id, "
        "NEW.extraction_input_sha256, NEW.extraction_output_sha256, "
        "NEW.extraction_run_id, NEW.extractor_code_version, "
        "NEW.extractor_config_sha256, NEW.extractor_name, "
        "NEW.raw_entry_sha256, observation.source_locator_sha256, "
        "NEW.source_taxonomy_version, NEW.subject_binding_revision_id) "
        "AND NEW.anchor_payload_sha256 = fact_sha256(NEW.anchor_payload_json)) "
        "BEGIN SELECT RAISE(ABORT, "
        "'reported fact requires its exact selected subject and extraction'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_fact_observation_payload_commitments_v2_exact "
        "BEFORE INSERT ON fact_observation_payload_commitments_v2 WHEN NOT EXISTS ("
        "SELECT 1 FROM fact_observations_v2 AS observation "
        "JOIN fact_cell_identity_seals_v2 AS cell_seal "
        "ON cell_seal.fact_cell_id = observation.fact_cell_id "
        "LEFT JOIN fact_reported_observation_anchors_v2 AS anchor "
        "ON anchor.observation_id = observation.observation_id "
        "LEFT JOIN fact_derivation_seals_v2 AS derivation "
        "ON derivation.output_observation_id = observation.observation_id "
        "LEFT JOIN fact_derivation_basis_commitments_v2 AS basis "
        "ON basis.derivation_seal_id = derivation.derivation_seal_id "
        "WHERE observation.observation_id = NEW.observation_id "
        "AND NEW.canonical_payload_json = fact_observation_payload_v1("
        "observation.observation_kind, observation.effective_at, "
        "cell_seal.semantic_key_sha256, observation.is_nil, "
        "observation.knowledge_at, observation.method_config_sha256, "
        "observation.method_name, observation.method_version, "
        "observation.numeric_value, observation.precision, "
        "observation.decimals, CASE WHEN observation.observation_kind = "
        "'reported' THEN json_object("
        "'anchor_payload_sha256', anchor.anchor_payload_sha256, "
        "'document_version_id', observation.document_version_id, "
        "'evidence_node_id', observation.evidence_node_id, "
        "'source_context_id', observation.source_context_id, "
        "'source_entry_sha256', observation.source_entry_sha256, "
        "'source_locator_sha256', observation.source_locator_sha256, "
        "'source_unit_id', observation.source_unit_id) ELSE json_object("
        "'canonical_input_digest_sha256', "
        "derivation.canonical_input_digest_sha256, "
        "'derivation_basis_sha256', basis.canonical_basis_sha256, "
        "'derivation_seal_id', derivation.derivation_seal_id, "
        "'formula_id', observation.formula_id, "
        "'formula_version', observation.formula_version) END, "
        "observation.raw_lexical_value, observation.recorded_at, "
        "observation.revision_kind, observation.supersedes_observation_id, "
        "observation.text_value, observation.value_kind) "
        "AND NEW.observation_payload_sha256 = "
        "fact_sha256(NEW.canonical_payload_json) "
        "AND ((observation.observation_kind = 'reported' "
        "AND anchor.observation_id IS NOT NULL) "
        "OR (observation.observation_kind = 'derived' "
        "AND anchor.observation_id IS NULL "
        "AND derivation.derivation_seal_id IS NOT NULL "
        "AND basis.derivation_seal_id IS NOT NULL))) "
        "BEGIN SELECT RAISE(ABORT, "
        "'observation payload commitment is not internally anchored'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_fact_resolution_candidates_v2_payload_commitment "
        "BEFORE INSERT ON fact_resolution_candidates_v2 WHEN NOT EXISTS ("
        "SELECT 1 FROM fact_observation_payload_commitments_v2 AS payload "
        "WHERE payload.observation_id = NEW.observation_id "
        "AND payload.observation_payload_sha256 = NEW.candidate_payload_sha256) "
        "BEGIN SELECT RAISE(ABORT, "
        "'candidate payload must equal committed observation payload'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_fact_derivation_basis_v2_exact "
        "BEFORE INSERT ON fact_derivation_basis_commitments_v2 WHEN NOT EXISTS ("
        "SELECT 1 FROM fact_derivation_seals_v2 AS seal "
        "JOIN fact_observations_v2 AS output "
        "ON output.observation_id = seal.output_observation_id "
        "WHERE seal.derivation_seal_id = NEW.derivation_seal_id "
        "AND output.observation_kind = 'derived' "
        "AND output.formula_id = NEW.formula_id "
        "AND output.formula_version = NEW.formula_version "
        "AND seal.formula_config_sha256 = NEW.execution_config_sha256 "
        "AND seal.knowledge_at <= NEW.knowledge_cutoff "
        "AND seal.recorded_at <= NEW.recorded_at "
        "AND NEW.canonical_basis_json = fact_derivation_basis_v1("
        "seal.canonical_input_digest_sha256, NEW.execution_config_sha256, "
        "NEW.formula_definition_sha256, NEW.formula_id, "
        "NEW.formula_version, NEW.input_basis, NEW.knowledge_cutoff) "
        "AND NEW.canonical_basis_sha256 = fact_sha256(NEW.canonical_basis_json) "
        "AND NOT EXISTS (SELECT 1 "
        "FROM fact_derivation_input_edges_v2 AS edge "
        "JOIN fact_observations_v2 AS input "
        "ON input.observation_id = edge.input_observation_id "
        "LEFT JOIN fact_resolution_revisions_v2 AS resolution "
        "ON resolution.resolution_revision_id = edge.input_resolution_revision_id "
        "WHERE edge.output_observation_id = output.observation_id "
        "AND (input.knowledge_at > NEW.knowledge_cutoff "
        "OR input.recorded_at > NEW.recorded_at "
        "OR input.effective_at > output.effective_at "
        "OR (NEW.input_basis = 'as_reported' "
        "AND edge.input_resolution_revision_id IS NOT NULL) "
        "OR (NEW.input_basis = 'as_known' AND "
        "(resolution.resolution_revision_id IS NULL "
        "OR resolution.selected_observation_id <> input.observation_id "
        "OR resolution.knowledge_at > NEW.knowledge_cutoff "
        "OR resolution.recorded_at > NEW.recorded_at)))) "
        ") BEGIN SELECT RAISE(ABORT, "
        "'derivation basis violates formula or no-look-ahead commitments'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_fact_extraction_seals_v2_complete "
        "BEFORE INSERT ON fact_extraction_run_completeness_seals_v2 "
        "WHEN NOT EXISTS (SELECT 1 FROM evidence_extraction_runs AS run "
        "WHERE run.extraction_run_id = NEW.extraction_run_id "
        "AND run.outcome = 'succeeded' "
        "AND run.extractor_config_sha256 = NEW.extractor_config_sha256 "
        "AND run.output_sha256 = NEW.extraction_output_sha256) "
        "OR NEW.observed_node_count <> (SELECT COUNT(*) FROM evidence_nodes "
        "WHERE extraction_run_id = NEW.extraction_run_id) "
        "OR NEW.reported_fact_count <> (SELECT COUNT(*) "
        "FROM fact_reported_observation_anchors_v2 "
        "WHERE extraction_run_id = NEW.extraction_run_id) "
        "OR NEW.node_set_json <> COALESCE((SELECT json_group_array(node_id) "
        "FROM (SELECT node_id FROM evidence_nodes "
        "WHERE extraction_run_id = NEW.extraction_run_id "
        "ORDER BY node_id)), '[]') "
        "OR NEW.observation_set_json <> COALESCE((SELECT "
        "json_group_array(observation_id) FROM (SELECT observation_id "
        "FROM fact_reported_observation_anchors_v2 "
        "WHERE extraction_run_id = NEW.extraction_run_id "
        "ORDER BY observation_id)), '[]') "
        "OR NEW.node_set_sha256 <> fact_sha256(NEW.node_set_json) "
        "OR NEW.observation_set_sha256 <> fact_sha256(NEW.observation_set_json) "
        "BEGIN SELECT RAISE(ABORT, 'extraction completeness seal mismatch'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_evidence_nodes_fact_extraction_sealed_v2 "
        "BEFORE INSERT ON evidence_nodes WHEN EXISTS (SELECT 1 "
        "FROM fact_extraction_run_completeness_seals_v2 AS seal "
        "WHERE seal.extraction_run_id = NEW.extraction_run_id) "
        "BEGIN SELECT RAISE(ABORT, 'fact extraction run is sealed'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_fact_reported_anchors_extraction_sealed_v2 "
        "BEFORE INSERT ON fact_reported_observation_anchors_v2 WHEN EXISTS ("
        "SELECT 1 FROM fact_extraction_run_completeness_seals_v2 AS seal "
        "WHERE seal.extraction_run_id = NEW.extraction_run_id) "
        "BEGIN SELECT RAISE(ABORT, 'fact extraction run is sealed'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_search_fact_membership_hardened_v2 "
        "BEFORE INSERT ON search_fact_projection_memberships "
        "WHEN NEW.disposition = 'included' AND NOT EXISTS (SELECT 1 "
        "FROM fact_resolution_revisions_v2 AS resolution "
        "JOIN fact_observation_payload_commitments_v2 AS payload "
        "ON payload.observation_id = resolution.selected_observation_id "
        "LEFT JOIN fact_reported_observation_anchors_v2 AS anchor "
        "ON anchor.observation_id = resolution.selected_observation_id "
        "LEFT JOIN fact_extraction_run_completeness_seals_v2 AS seal "
        "ON seal.extraction_run_id = anchor.extraction_run_id "
        "WHERE resolution.resolution_revision_id = NEW.resolution_revision_id "
        "AND ((anchor.observation_id IS NULL) "
        "OR seal.extraction_seal_id IS NOT NULL)) "
        "BEGIN SELECT RAISE(ABORT, "
        "'included fact projection requires committed complete evidence'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_search_fact_rows_hardened_v2 "
        "BEFORE INSERT ON search_fact_projection_rows WHEN NOT EXISTS (SELECT 1 "
        "FROM fact_observation_payload_commitments_v2 AS payload "
        "LEFT JOIN fact_reported_observation_anchors_v2 AS anchor "
        "ON anchor.observation_id = payload.observation_id "
        "LEFT JOIN fact_extraction_run_completeness_seals_v2 AS seal "
        "ON seal.extraction_run_id = anchor.extraction_run_id "
        "WHERE payload.observation_id = NEW.observation_id "
        "AND ((anchor.observation_id IS NULL) "
        "OR seal.extraction_seal_id IS NOT NULL)) "
        "BEGIN SELECT RAISE(ABORT, "
        "'fact search row requires committed complete evidence'); END"
    )

    for table, label in (
        ("fact_dimensions_normalized_v2", "normalized fact dimension"),
        ("fact_cell_identity_seals_v2", "fact-cell identity seal"),
        ("fact_reported_observation_anchors_v2", "reported fact anchor"),
        (
            "fact_observation_payload_commitments_v2",
            "fact observation payload commitment",
        ),
        (
            "fact_derivation_basis_commitments_v2",
            "fact derivation basis commitment",
        ),
        (
            "fact_extraction_run_completeness_seals_v2",
            "fact extraction completeness seal",
        ),
    ):
        _append_only(table, label)

    op.execute(
        "CREATE VIEW v_fact_cells_hardened_v2 AS "
        "SELECT cell.*, seal.semantic_key_version AS hardened_semantic_key_version, "
        "seal.semantic_identity_json, seal.dimension_count, "
        "seal.dimension_set_json, seal.dimension_set_sha256, seal.sealed_at "
        "FROM fact_cells_v2 AS cell "
        "JOIN fact_cell_identity_seals_v2 AS seal "
        "ON seal.fact_cell_id = cell.fact_cell_id"
    )
    op.execute(
        "CREATE VIEW v_fact_reported_anchors_selected_v2 AS "
        "SELECT observation.*, anchor.*, binding.recorded_issuer_id, "
        "binding.issuer_id AS bound_issuer_id, "
        "binding.reporting_entity_id AS bound_reporting_entity_id, "
        "binding.security_id AS bound_security_id "
        "FROM fact_observations_v2 AS observation "
        "JOIN fact_reported_observation_anchors_v2 AS anchor "
        "ON anchor.observation_id = observation.observation_id "
        "JOIN recorded_subject_binding_revisions AS binding "
        "ON binding.binding_revision_id = anchor.subject_binding_revision_id "
        "WHERE binding.outcome = 'selected'"
    )
    op.execute(
        "CREATE VIEW v_fact_observations_committed_v2 AS "
        "SELECT observation.*, payload.payload_version, "
        "payload.canonical_payload_json, "
        "payload.observation_payload_sha256, payload.committed_at "
        "FROM fact_observations_v2 AS observation "
        "JOIN fact_observation_payload_commitments_v2 AS payload "
        "ON payload.observation_id = observation.observation_id"
    )
    op.execute(
        "CREATE VIEW v_fact_extraction_runs_complete_v2 AS "
        "SELECT run.*, seal.extraction_seal_id, seal.expected_node_count, "
        "seal.observed_node_count, seal.reported_fact_count, "
        "seal.node_set_sha256, seal.observation_set_sha256, "
        "seal.knowledge_at AS seal_knowledge_at, "
        "seal.recorded_at AS seal_recorded_at "
        "FROM evidence_extraction_runs AS run "
        "JOIN fact_extraction_run_completeness_seals_v2 AS seal "
        "ON seal.extraction_run_id = run.extraction_run_id"
    )
    op.execute("DROP VIEW IF EXISTS v_fact_cells_resolved_current_v2")
    op.execute("DROP VIEW IF EXISTS v_fact_observations_as_reported_v2")
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
        "JOIN fact_cell_identity_seals_v2 AS cell_seal "
        "ON cell_seal.fact_cell_id = cell.fact_cell_id "
        "JOIN fact_observations_v2 AS observation "
        "ON observation.observation_id = resolution.selected_observation_id "
        "JOIN fact_observation_payload_commitments_v2 AS payload "
        "ON payload.observation_id = observation.observation_id "
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
        "anchor.subject_binding_revision_id, anchor.source_taxonomy_version, "
        "payload.observation_payload_sha256, "
        "observation.effective_at AS observation_effective_at, "
        "observation.knowledge_at AS observation_knowledge_at, "
        "observation.recorded_at AS observation_recorded_at "
        "FROM fact_observations_v2 AS observation "
        "JOIN fact_cells_v2 AS cell "
        "ON cell.fact_cell_id = observation.fact_cell_id "
        "JOIN fact_cell_identity_seals_v2 AS cell_seal "
        "ON cell_seal.fact_cell_id = cell.fact_cell_id "
        "JOIN fact_reported_observation_anchors_v2 AS anchor "
        "ON anchor.observation_id = observation.observation_id "
        "JOIN fact_observation_payload_commitments_v2 AS payload "
        "ON payload.observation_id = observation.observation_id "
        "JOIN recorded_subject_binding_revisions AS binding "
        "ON binding.binding_revision_id = anchor.subject_binding_revision_id "
        "AND binding.outcome = 'selected' "
        "WHERE observation.observation_kind = 'reported'"
    )


def downgrade() -> None:
    op.execute("DROP VIEW IF EXISTS v_fact_cells_resolved_current_v2")
    op.execute("DROP VIEW IF EXISTS v_fact_observations_as_reported_v2")
    for view in reversed(_VIEWS):
        op.execute(f"DROP VIEW IF EXISTS {view}")
    for trigger in (
        "trg_search_fact_rows_hardened_v2",
        "trg_search_fact_membership_hardened_v2",
        "trg_fact_reported_anchors_extraction_sealed_v2",
        "trg_evidence_nodes_fact_extraction_sealed_v2",
        "trg_fact_extraction_seals_v2_complete",
        "trg_fact_derivation_basis_v2_exact",
        "trg_fact_resolution_candidates_v2_payload_commitment",
        "trg_fact_observation_payload_commitments_v2_exact",
        "trg_fact_reported_anchors_v2_exact",
        "trg_fact_cell_identity_seals_v2_complete",
        "trg_fact_dimensions_normalized_v2_typed_digest",
        "trg_fact_observations_v2_evidence_chain_hardened",
    ):
        op.execute(f"DROP TRIGGER IF EXISTS {trigger}")
    for table in reversed(_TABLES):
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_append_only")
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_append_only_delete")
        op.drop_table(table)
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
