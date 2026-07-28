"""Add a sealed, structured search projection for evidence-first facts.

Narrative chunks and typed facts remain separate retrieval lanes.  A fact
projection is an immutable as-known snapshot, bound to the same complete
document corpus used by grounded narrative search.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0239_structured_fact_search_projection"
down_revision: str | Sequence[str] | None = "0238_evidence_first_fact_plane"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLES = (
    "search_fact_projection_runs",
    "search_fact_projection_memberships",
    "search_fact_projection_rows",
    "search_fact_projection_seals",
    "ask_retrieval_trace_hits",
)
_VIEWS = (
    "v_search_fact_projection_current_sealed",
    "v_search_fact_hits_current",
)


def _hex_check(column: str, *, nullable: bool = False) -> str:
    required = f"length({column}) = 64 AND {column} NOT GLOB '*[^0-9a-f]*'"
    return f"({column} IS NULL OR ({required}))" if nullable else f"({required})"


def _append_only(table: str, label: str) -> None:
    op.execute(
        f"CREATE TRIGGER trg_{table}_append_only BEFORE UPDATE ON {table} "
        f"BEGIN SELECT RAISE(ABORT, '{label} is append-only'); END"
    )
    op.execute(
        f"CREATE TRIGGER trg_{table}_append_only_delete BEFORE DELETE ON {table} "
        f"BEGIN SELECT RAISE(ABORT, '{label} is append-only'); END"
    )


def upgrade() -> None:
    bind = op.get_bind()
    required = {
        "search_corpus_manifests",
        "search_corpus_document_memberships",
        "search_corpus_manifest_seals",
        "search_chunks",
        "ask_retrieval_traces",
        "ask_retrieval_trace_items",
        "fact_cells_v2",
        "fact_observations_v2",
        "fact_resolution_candidates_v2",
        "fact_resolution_revisions_v2",
        "fact_derivation_input_edges_v2",
        "fact_derivation_seals_v2",
        "evidence_document_versions",
        "evidence_extraction_runs",
        "evidence_nodes",
    }
    missing = sorted(required - set(sa.inspect(bind).get_table_names()))
    if missing:
        raise RuntimeError(
            "structured fact search requires the sealed corpus and v2 fact plane: "
            + ", ".join(missing)
        )

    op.create_table(
        "search_fact_projection_runs",
        sa.Column("projection_run_id", sa.String(128), primary_key=True),
        sa.Column("idempotency_key", sa.String(256), nullable=False, unique=True),
        sa.Column("projection_key", sa.String(256), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column(
            "manifest_id",
            sa.String(128),
            sa.ForeignKey("search_corpus_manifests.manifest_id"),
            nullable=False,
        ),
        sa.Column("knowledge_cutoff", sa.DateTime(), nullable=False),
        sa.Column("config_sha256", sa.String(64), nullable=False),
        sa.Column("code_version", sa.String(255), nullable=False),
        sa.Column(
            "supersedes_projection_run_id",
            sa.String(128),
            sa.ForeignKey("search_fact_projection_runs.projection_run_id"),
            nullable=True,
        ),
        sa.Column("recorded_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint(
            "projection_key",
            "revision",
            name="uq_search_fact_projection_run_revision",
        ),
        sa.CheckConstraint(
            "revision > 0", name="ck_search_fact_projection_run_revision"
        ),
        sa.CheckConstraint(
            _hex_check("config_sha256"),
            name="ck_search_fact_projection_run_hash",
        ),
        sa.CheckConstraint(
            "recorded_at >= knowledge_cutoff",
            name="ck_search_fact_projection_run_clock",
        ),
    )
    op.create_index(
        "ix_search_fact_projection_runs_manifest",
        "search_fact_projection_runs",
        ["manifest_id", "knowledge_cutoff", "revision"],
    )

    op.create_table(
        "search_fact_projection_memberships",
        sa.Column("membership_id", sa.String(128), primary_key=True),
        sa.Column(
            "projection_run_id",
            sa.String(128),
            sa.ForeignKey("search_fact_projection_runs.projection_run_id"),
            nullable=False,
        ),
        sa.Column(
            "fact_cell_id",
            sa.String(128),
            sa.ForeignKey("fact_cells_v2.fact_cell_id"),
            nullable=False,
        ),
        sa.Column("disposition", sa.String(32), nullable=False),
        sa.Column(
            "resolution_revision_id",
            sa.String(128),
            sa.ForeignKey(
                "fact_resolution_revisions_v2.resolution_revision_id"
            ),
            nullable=True,
        ),
        sa.Column("reason_code", sa.String(128), nullable=False),
        sa.Column("reason_details_json", sa.Text(), nullable=False),
        sa.Column("membership_bundle_sha256", sa.String(64), nullable=False),
        sa.Column("recorded_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint(
            "projection_run_id",
            "fact_cell_id",
            name="uq_search_fact_projection_membership_cell",
        ),
        sa.CheckConstraint(
            "disposition IN "
            "('included', 'unresolved_material', 'missing_provenance', "
            "'quarantined')",
            name="ck_search_fact_projection_membership_disposition",
        ),
        sa.CheckConstraint(
            "(disposition IN ('included', 'missing_provenance') "
            "AND resolution_revision_id IS NOT NULL) OR "
            "(disposition IN ('unresolved_material', 'quarantined'))",
            name="ck_search_fact_projection_membership_resolution",
        ),
        sa.CheckConstraint(
            "json_valid(reason_details_json) "
            "AND json_type(reason_details_json) = 'object'",
            name="ck_search_fact_projection_membership_reason",
        ),
        sa.CheckConstraint(
            _hex_check("membership_bundle_sha256"),
            name="ck_search_fact_projection_membership_hash",
        ),
    )
    op.create_index(
        "ix_search_fact_projection_memberships_disposition",
        "search_fact_projection_memberships",
        ["projection_run_id", "disposition", "fact_cell_id"],
    )

    op.create_table(
        "search_fact_projection_rows",
        sa.Column("fact_hit_id", sa.String(128), primary_key=True),
        sa.Column("idempotency_key", sa.String(256), nullable=False, unique=True),
        sa.Column(
            "projection_run_id",
            sa.String(128),
            sa.ForeignKey("search_fact_projection_runs.projection_run_id"),
            nullable=False,
        ),
        sa.Column(
            "fact_cell_id",
            sa.String(128),
            sa.ForeignKey("fact_cells_v2.fact_cell_id"),
            nullable=False,
        ),
        sa.Column(
            "resolution_revision_id",
            sa.String(128),
            sa.ForeignKey(
                "fact_resolution_revisions_v2.resolution_revision_id"
            ),
            nullable=False,
        ),
        sa.Column(
            "observation_id",
            sa.String(128),
            sa.ForeignKey("fact_observations_v2.observation_id"),
            nullable=False,
        ),
        sa.Column("reporting_entity_id", sa.String(128), nullable=False),
        sa.Column("scope_security_id", sa.String(128), nullable=True),
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
        sa.Column("observation_kind", sa.String(16), nullable=False),
        sa.Column("value_kind", sa.String(16), nullable=False),
        sa.Column("numeric_value", sa.Text(), nullable=True),
        sa.Column("text_value", sa.Text(), nullable=True),
        sa.Column("is_nil", sa.Boolean(), nullable=False),
        sa.Column("raw_lexical_value", sa.Text(), nullable=True),
        sa.Column("candidate_set_id", sa.String(128), nullable=False),
        sa.Column("candidate_count", sa.Integer(), nullable=False),
        sa.Column("candidate_set_digest_sha256", sa.String(64), nullable=False),
        sa.Column("document_version_id", sa.String(128), nullable=True),
        sa.Column("evidence_node_id", sa.String(128), nullable=True),
        sa.Column("source_locator_json", sa.Text(), nullable=True),
        sa.Column("source_locator_sha256", sa.String(64), nullable=True),
        sa.Column("source_entry_sha256", sa.String(64), nullable=True),
        sa.Column("legacy_match_revision_id", sa.String(128), nullable=True),
        sa.Column("derivation_seal_id", sa.String(128), nullable=True),
        sa.Column("derivation_input_count", sa.Integer(), nullable=True),
        sa.Column("derivation_input_digest_sha256", sa.String(64), nullable=True),
        sa.Column("cell_knowledge_at", sa.DateTime(), nullable=False),
        sa.Column("observation_knowledge_at", sa.DateTime(), nullable=False),
        sa.Column("resolution_knowledge_at", sa.DateTime(), nullable=False),
        sa.Column("row_bundle_json", sa.Text(), nullable=False),
        sa.Column("row_bundle_sha256", sa.String(64), nullable=False),
        sa.Column("recorded_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint(
            "projection_run_id",
            "fact_cell_id",
            name="uq_search_fact_projection_row_cell",
        ),
        sa.CheckConstraint(
            "(value_kind = 'numeric' AND numeric_value IS NOT NULL "
            "AND text_value IS NULL AND is_nil = 0) OR "
            "(value_kind = 'text' AND numeric_value IS NULL "
            "AND text_value IS NOT NULL AND is_nil = 0) OR "
            "(value_kind = 'nil' AND numeric_value IS NULL "
            "AND text_value IS NULL AND is_nil = 1)",
            name="ck_search_fact_projection_row_value",
        ),
        sa.CheckConstraint(
            "(observation_kind = 'reported' "
            "AND document_version_id IS NOT NULL "
            "AND evidence_node_id IS NOT NULL "
            "AND source_locator_json IS NOT NULL "
            "AND source_locator_sha256 IS NOT NULL "
            "AND source_entry_sha256 IS NOT NULL "
            "AND derivation_seal_id IS NULL "
            "AND derivation_input_count IS NULL "
            "AND derivation_input_digest_sha256 IS NULL) OR "
            "(observation_kind = 'derived' "
            "AND document_version_id IS NULL AND evidence_node_id IS NULL "
            "AND source_locator_json IS NULL "
            "AND source_locator_sha256 IS NULL "
            "AND source_entry_sha256 IS NULL "
            "AND legacy_match_revision_id IS NULL "
            "AND derivation_seal_id IS NOT NULL "
            "AND derivation_input_count > 0 "
            "AND derivation_input_digest_sha256 IS NOT NULL)",
            name="ck_search_fact_projection_row_provenance",
        ),
        sa.CheckConstraint(
            "json_valid(canonical_dimensions_json) "
            "AND json_type(canonical_dimensions_json) = 'array' "
            "AND json_valid(row_bundle_json) "
            "AND json_type(row_bundle_json) = 'object' "
            "AND (source_locator_json IS NULL OR "
            "(json_valid(source_locator_json) "
            "AND json_type(source_locator_json) = 'object'))",
            name="ck_search_fact_projection_row_json",
        ),
        sa.CheckConstraint(
            _hex_check("canonical_dimensions_sha256")
            + " AND "
            + _hex_check("candidate_set_digest_sha256")
            + " AND "
            + _hex_check("source_locator_sha256", nullable=True)
            + " AND "
            + _hex_check("source_entry_sha256", nullable=True)
            + " AND "
            + _hex_check("derivation_input_digest_sha256", nullable=True)
            + " AND "
            + _hex_check("row_bundle_sha256"),
            name="ck_search_fact_projection_row_hashes",
        ),
        sa.CheckConstraint(
            "candidate_count > 0",
            name="ck_search_fact_projection_row_candidates",
        ),
    )
    op.create_index(
        "ix_search_fact_projection_rows_entity_concept",
        "search_fact_projection_rows",
        [
            "projection_run_id",
            "reporting_entity_id",
            "concept_namespace",
            "concept_name",
            "period_end",
        ],
    )
    op.create_index(
        "ix_search_fact_projection_rows_security_period",
        "search_fact_projection_rows",
        ["projection_run_id", "scope_security_id", "period_end"],
    )

    op.create_table(
        "search_fact_projection_seals",
        sa.Column("projection_seal_id", sa.String(128), primary_key=True),
        sa.Column("idempotency_key", sa.String(256), nullable=False, unique=True),
        sa.Column(
            "projection_run_id",
            sa.String(128),
            sa.ForeignKey("search_fact_projection_runs.projection_run_id"),
            nullable=False,
            unique=True,
        ),
        sa.Column(
            "manifest_id",
            sa.String(128),
            sa.ForeignKey("search_corpus_manifests.manifest_id"),
            nullable=False,
        ),
        sa.Column("eligible_fact_cell_count", sa.Integer(), nullable=False),
        sa.Column("membership_count", sa.Integer(), nullable=False),
        sa.Column("included_count", sa.Integer(), nullable=False),
        sa.Column("unresolved_material_count", sa.Integer(), nullable=False),
        sa.Column("missing_provenance_count", sa.Integer(), nullable=False),
        sa.Column("quarantined_count", sa.Integer(), nullable=False),
        sa.Column("row_count", sa.Integer(), nullable=False),
        sa.Column("membership_set_sha256", sa.String(64), nullable=False),
        sa.Column("row_set_sha256", sa.String(64), nullable=False),
        sa.Column("config_sha256", sa.String(64), nullable=False),
        sa.Column("sealed_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "eligible_fact_cell_count >= 0 AND membership_count >= 0 "
            "AND included_count >= 0 AND unresolved_material_count >= 0 "
            "AND missing_provenance_count >= 0 AND quarantined_count >= 0 "
            "AND row_count >= 0",
            name="ck_search_fact_projection_seal_counts",
        ),
        sa.CheckConstraint(
            _hex_check("membership_set_sha256")
            + " AND "
            + _hex_check("row_set_sha256")
            + " AND "
            + _hex_check("config_sha256"),
            name="ck_search_fact_projection_seal_hashes",
        ),
    )

    op.create_table(
        "ask_retrieval_trace_hits",
        sa.Column(
            "trace_id",
            sa.String(128),
            sa.ForeignKey("ask_retrieval_traces.trace_id"),
            primary_key=True,
        ),
        sa.Column("rank", sa.Integer(), primary_key=True),
        sa.Column("hit_kind", sa.String(16), nullable=False),
        sa.Column(
            "manifest_id",
            sa.String(128),
            sa.ForeignKey("search_corpus_manifests.manifest_id"),
            nullable=True,
        ),
        sa.Column(
            "chunk_id",
            sa.String(128),
            sa.ForeignKey("search_chunks.chunk_id"),
            nullable=True,
        ),
        sa.Column(
            "projection_run_id",
            sa.String(128),
            sa.ForeignKey("search_fact_projection_runs.projection_run_id"),
            nullable=True,
        ),
        sa.Column(
            "fact_hit_id",
            sa.String(128),
            sa.ForeignKey("search_fact_projection_rows.fact_hit_id"),
            nullable=True,
        ),
        sa.Column("score", sa.Float(), nullable=False),
        sa.Column("bundle_sha256", sa.String(64), nullable=False),
        sa.Column("recorded_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("rank > 0", name="ck_ask_retrieval_trace_hit_rank"),
        sa.CheckConstraint(
            "(hit_kind = 'document' AND manifest_id IS NOT NULL "
            "AND chunk_id IS NOT NULL AND projection_run_id IS NULL "
            "AND fact_hit_id IS NULL) OR "
            "(hit_kind = 'fact' AND manifest_id IS NULL AND chunk_id IS NULL "
            "AND projection_run_id IS NOT NULL AND fact_hit_id IS NOT NULL)",
            name="ck_ask_retrieval_trace_hit_source",
        ),
        sa.CheckConstraint(
            _hex_check("bundle_sha256"),
            name="ck_ask_retrieval_trace_hit_hash",
        ),
    )
    op.create_index(
        "ix_ask_retrieval_trace_hits_source",
        "ask_retrieval_trace_hits",
        ["hit_kind", "manifest_id", "projection_run_id"],
    )

    # A run is usable only when its narrative corpus is complete at precisely
    # the same as-known boundary.
    op.execute(
        "CREATE TRIGGER trg_search_fact_projection_runs_manifest "
        "BEFORE INSERT ON search_fact_projection_runs WHEN NOT EXISTS ("
        "SELECT 1 FROM search_corpus_manifests AS manifest "
        "JOIN search_corpus_manifest_seals AS seal "
        "ON seal.manifest_id = manifest.manifest_id "
        "WHERE manifest.manifest_id = NEW.manifest_id "
        "AND manifest.knowledge_cutoff IS NOT NULL "
        "AND manifest.knowledge_cutoff = NEW.knowledge_cutoff "
        "AND seal.completion_status = 'complete') "
        "BEGIN SELECT RAISE(ABORT, "
        "'fact projection requires a complete corpus at the same cutoff'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_search_fact_projection_runs_first "
        "BEFORE INSERT ON search_fact_projection_runs "
        "WHEN NEW.revision = 1 "
        "AND NEW.supersedes_projection_run_id IS NOT NULL "
        "BEGIN SELECT RAISE(ABORT, "
        "'first fact projection cannot supersede'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_search_fact_projection_runs_parent "
        "BEFORE INSERT ON search_fact_projection_runs "
        "WHEN NEW.revision > 1 AND ("
        "NEW.supersedes_projection_run_id IS NULL OR NOT EXISTS ("
        "SELECT 1 FROM search_fact_projection_runs AS prior "
        "WHERE prior.projection_run_id = NEW.supersedes_projection_run_id "
        "AND prior.projection_key = NEW.projection_key "
        "AND prior.revision = NEW.revision - 1 "
        "AND NEW.recorded_at >= prior.recorded_at)) "
        "BEGIN SELECT RAISE(ABORT, "
        "'fact projection must supersede its prior revision'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_search_fact_projection_memberships_unsealed "
        "BEFORE INSERT ON search_fact_projection_memberships WHEN EXISTS ("
        "SELECT 1 FROM search_fact_projection_seals AS seal "
        "WHERE seal.projection_run_id = NEW.projection_run_id) "
        "BEGIN SELECT RAISE(ABORT, "
        "'sealed fact projection cannot receive memberships'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_search_fact_projection_memberships_scope "
        "BEFORE INSERT ON search_fact_projection_memberships WHEN NOT EXISTS ("
        "SELECT 1 FROM search_fact_projection_runs AS run "
        "JOIN fact_cells_v2 AS cell ON cell.fact_cell_id = NEW.fact_cell_id "
        "WHERE run.projection_run_id = NEW.projection_run_id "
        "AND cell.knowledge_at <= run.knowledge_cutoff "
        "AND NEW.recorded_at >= cell.recorded_at "
        "AND NEW.recorded_at <= run.recorded_at) "
        "BEGIN SELECT RAISE(ABORT, "
        "'fact projection membership requires a cell known at cutoff'); END"
    )
    # Included is deliberately strict: the latest as-known resolution must be
    # complete and selected, and its selected observation must have exact
    # evidence in the bound corpus or a complete derivation seal.
    op.execute(
        "CREATE TRIGGER trg_search_fact_projection_memberships_included "
        "BEFORE INSERT ON search_fact_projection_memberships "
        "WHEN NEW.disposition = 'included' AND NOT EXISTS ("
        "SELECT 1 FROM search_fact_projection_runs AS run "
        "JOIN fact_resolution_revisions_v2 AS resolution "
        "ON resolution.resolution_revision_id = NEW.resolution_revision_id "
        "JOIN fact_observations_v2 AS observation "
        "ON observation.observation_id = resolution.selected_observation_id "
        "WHERE run.projection_run_id = NEW.projection_run_id "
        "AND resolution.fact_cell_id = NEW.fact_cell_id "
        "AND resolution.status = 'resolved' "
        "AND resolution.knowledge_at <= run.knowledge_cutoff "
        "AND resolution.recorded_at <= run.knowledge_cutoff "
        "AND observation.knowledge_at <= run.knowledge_cutoff "
        "AND observation.recorded_at <= run.knowledge_cutoff "
        "AND resolution.candidate_count = (SELECT COUNT(*) "
        "FROM fact_resolution_candidates_v2 AS candidate "
        "WHERE candidate.candidate_set_id = resolution.candidate_set_id) "
        "AND NOT EXISTS (SELECT 1 FROM fact_resolution_revisions_v2 AS newer "
        "WHERE newer.fact_cell_id = resolution.fact_cell_id "
        "AND newer.knowledge_at <= run.knowledge_cutoff "
        "AND newer.recorded_at <= run.knowledge_cutoff "
        "AND newer.revision > resolution.revision) "
        "AND ("
        "(observation.observation_kind = 'reported' AND EXISTS ("
        "SELECT 1 FROM search_corpus_document_memberships AS membership "
        "JOIN evidence_extraction_runs AS extraction "
        "ON extraction.document_version_id = membership.document_version_id "
        "JOIN evidence_nodes AS node "
        "ON node.extraction_run_id = extraction.extraction_run_id "
        "JOIN evidence_document_versions AS document "
        "ON document.document_version_id = membership.document_version_id "
        "WHERE membership.manifest_id = run.manifest_id "
        "AND membership.membership_status = 'included' "
        "AND membership.document_version_id = observation.document_version_id "
        "AND node.node_id = observation.evidence_node_id "
        "AND (document.document_type <> 'sec_companyfacts' "
        "OR observation.legacy_match_revision_id IS NOT NULL))) "
        "OR (observation.observation_kind = 'derived' AND EXISTS ("
        "SELECT 1 FROM fact_derivation_seals_v2 AS derivation "
        "WHERE derivation.output_observation_id = observation.observation_id "
        "AND derivation.knowledge_at <= run.knowledge_cutoff "
        "AND derivation.recorded_at <= run.knowledge_cutoff "
        "AND derivation.input_count = (SELECT COUNT(*) "
        "FROM fact_derivation_input_edges_v2 AS edge "
        "WHERE edge.output_observation_id = observation.observation_id) "
        "AND NOT EXISTS ("
        "SELECT 1 FROM fact_derivation_input_edges_v2 AS edge "
        "JOIN fact_observations_v2 AS input "
        "ON input.observation_id = edge.input_observation_id "
        "WHERE edge.output_observation_id = observation.observation_id "
        "AND (edge.recorded_at > run.knowledge_cutoff "
        "OR input.knowledge_at > run.knowledge_cutoff "
        "OR input.recorded_at > run.knowledge_cutoff "
        "OR (input.observation_kind = 'reported' AND NOT EXISTS ("
        "SELECT 1 FROM search_corpus_document_memberships AS input_membership "
        "JOIN evidence_extraction_runs AS input_run "
        "ON input_run.document_version_id = input_membership.document_version_id "
        "JOIN evidence_nodes AS input_node "
        "ON input_node.extraction_run_id = input_run.extraction_run_id "
        "WHERE input_membership.manifest_id = run.manifest_id "
        "AND input_membership.membership_status = 'included' "
        "AND input_membership.document_version_id = input.document_version_id "
        "AND input_node.node_id = input.evidence_node_id)) "
        "OR (input.observation_kind = 'derived' AND NOT EXISTS ("
        "SELECT 1 FROM fact_derivation_seals_v2 AS input_seal "
        "WHERE input_seal.output_observation_id = input.observation_id "
        "AND input_seal.knowledge_at <= run.knowledge_cutoff "
        "AND input_seal.recorded_at <= run.knowledge_cutoff)))))"
        "))"
        ") "
        "BEGIN SELECT RAISE(ABORT, "
        "'included fact requires a complete as-known resolution and provenance'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_search_fact_projection_memberships_unresolved "
        "BEFORE INSERT ON search_fact_projection_memberships "
        "WHEN NEW.disposition = 'unresolved_material' AND EXISTS ("
        "SELECT 1 FROM search_fact_projection_runs AS run "
        "JOIN fact_resolution_revisions_v2 AS resolution "
        "ON resolution.fact_cell_id = NEW.fact_cell_id "
        "WHERE run.projection_run_id = NEW.projection_run_id "
        "AND resolution.status = 'resolved' "
        "AND resolution.knowledge_at <= run.knowledge_cutoff "
        "AND resolution.recorded_at <= run.knowledge_cutoff "
        "AND NOT EXISTS (SELECT 1 FROM fact_resolution_revisions_v2 AS newer "
        "WHERE newer.fact_cell_id = resolution.fact_cell_id "
        "AND newer.knowledge_at <= run.knowledge_cutoff "
        "AND newer.recorded_at <= run.knowledge_cutoff "
        "AND newer.revision > resolution.revision)) "
        "BEGIN SELECT RAISE(ABORT, "
        "'resolved as-known fact cannot be marked unresolved'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_search_fact_projection_memberships_resolution_scope "
        "BEFORE INSERT ON search_fact_projection_memberships "
        "WHEN NEW.resolution_revision_id IS NOT NULL AND NOT EXISTS ("
        "SELECT 1 FROM search_fact_projection_runs AS run "
        "JOIN fact_resolution_revisions_v2 AS resolution "
        "ON resolution.resolution_revision_id = NEW.resolution_revision_id "
        "WHERE run.projection_run_id = NEW.projection_run_id "
        "AND resolution.fact_cell_id = NEW.fact_cell_id "
        "AND resolution.knowledge_at <= run.knowledge_cutoff "
        "AND resolution.recorded_at <= run.knowledge_cutoff "
        "AND NOT EXISTS (SELECT 1 FROM fact_resolution_revisions_v2 AS newer "
        "WHERE newer.fact_cell_id = resolution.fact_cell_id "
        "AND newer.knowledge_at <= run.knowledge_cutoff "
        "AND newer.recorded_at <= run.knowledge_cutoff "
        "AND newer.revision > resolution.revision)) "
        "BEGIN SELECT RAISE(ABORT, "
        "'membership resolution must be latest as known at cutoff'); END"
    )

    op.execute(
        "CREATE TRIGGER trg_search_fact_projection_rows_unsealed "
        "BEFORE INSERT ON search_fact_projection_rows WHEN EXISTS ("
        "SELECT 1 FROM search_fact_projection_seals AS seal "
        "WHERE seal.projection_run_id = NEW.projection_run_id) "
        "BEGIN SELECT RAISE(ABORT, "
        "'sealed fact projection cannot receive rows'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_search_fact_projection_rows_membership "
        "BEFORE INSERT ON search_fact_projection_rows WHEN NOT EXISTS ("
        "SELECT 1 FROM search_fact_projection_memberships AS membership "
        "WHERE membership.projection_run_id = NEW.projection_run_id "
        "AND membership.fact_cell_id = NEW.fact_cell_id "
        "AND membership.disposition = 'included' "
        "AND membership.resolution_revision_id = NEW.resolution_revision_id) "
        "BEGIN SELECT RAISE(ABORT, "
        "'fact projection row requires its included membership'); END"
    )
    # Every denormalized field is checked against the immutable fact-plane
    # source.  The JSON bundle hash itself is recomputed by the deterministic
    # builder/auditor; SQL prevents source substitution.
    op.execute(
        "CREATE TRIGGER trg_search_fact_projection_rows_exact "
        "BEFORE INSERT ON search_fact_projection_rows WHEN NOT EXISTS ("
        "SELECT 1 FROM search_fact_projection_runs AS run "
        "JOIN fact_cells_v2 AS cell ON cell.fact_cell_id = NEW.fact_cell_id "
        "JOIN fact_resolution_revisions_v2 AS resolution "
        "ON resolution.resolution_revision_id = NEW.resolution_revision_id "
        "JOIN fact_observations_v2 AS observation "
        "ON observation.observation_id = NEW.observation_id "
        "LEFT JOIN fact_derivation_seals_v2 AS derivation "
        "ON derivation.output_observation_id = observation.observation_id "
        "WHERE run.projection_run_id = NEW.projection_run_id "
        "AND resolution.fact_cell_id = cell.fact_cell_id "
        "AND resolution.status = 'resolved' "
        "AND resolution.selected_observation_id = observation.observation_id "
        "AND observation.fact_cell_id = cell.fact_cell_id "
        "AND NEW.reporting_entity_id = cell.reporting_entity_id "
        "AND NEW.scope_security_id IS cell.scope_security_id "
        "AND NEW.concept_namespace = cell.concept_namespace "
        "AND NEW.concept_name = cell.concept_name "
        "AND NEW.taxonomy_name = cell.taxonomy_name "
        "AND NEW.taxonomy_version IS cell.taxonomy_version "
        "AND NEW.accounting_basis = cell.accounting_basis "
        "AND NEW.consolidation_scope = cell.consolidation_scope "
        "AND NEW.period_kind = cell.period_kind "
        "AND NEW.period_start IS cell.period_start "
        "AND NEW.period_end = cell.period_end "
        "AND NEW.fiscal_year IS cell.fiscal_year "
        "AND NEW.fiscal_period IS cell.fiscal_period "
        "AND NEW.canonical_dimensions_json = cell.canonical_dimensions_json "
        "AND NEW.canonical_dimensions_sha256 = cell.canonical_dimensions_sha256 "
        "AND NEW.unit_key = cell.unit_key AND NEW.currency IS cell.currency "
        "AND NEW.observation_kind = observation.observation_kind "
        "AND NEW.value_kind = observation.value_kind "
        "AND NEW.numeric_value IS observation.numeric_value "
        "AND NEW.text_value IS observation.text_value "
        "AND NEW.is_nil = observation.is_nil "
        "AND NEW.raw_lexical_value IS observation.raw_lexical_value "
        "AND NEW.candidate_set_id = resolution.candidate_set_id "
        "AND NEW.candidate_count = resolution.candidate_count "
        "AND NEW.candidate_set_digest_sha256 = "
        "resolution.candidate_set_digest_sha256 "
        "AND NEW.document_version_id IS observation.document_version_id "
        "AND NEW.evidence_node_id IS observation.evidence_node_id "
        "AND NEW.source_locator_json IS observation.source_locator_json "
        "AND NEW.source_locator_sha256 IS observation.source_locator_sha256 "
        "AND NEW.source_entry_sha256 IS observation.source_entry_sha256 "
        "AND NEW.legacy_match_revision_id IS observation.legacy_match_revision_id "
        "AND NEW.derivation_seal_id IS derivation.derivation_seal_id "
        "AND NEW.derivation_input_count IS derivation.input_count "
        "AND NEW.derivation_input_digest_sha256 IS "
        "derivation.canonical_input_digest_sha256 "
        "AND NEW.cell_knowledge_at = cell.knowledge_at "
        "AND NEW.observation_knowledge_at = observation.knowledge_at "
        "AND NEW.resolution_knowledge_at = resolution.knowledge_at "
        "AND NEW.recorded_at <= run.recorded_at) "
        "BEGIN SELECT RAISE(ABORT, "
        "'fact projection row must exactly match its resolved source'); END"
    )

    op.execute(
        "CREATE TRIGGER trg_search_fact_projection_seals_contract "
        "BEFORE INSERT ON search_fact_projection_seals WHEN NOT EXISTS ("
        "SELECT 1 FROM search_fact_projection_runs AS run "
        "JOIN search_corpus_manifest_seals AS corpus_seal "
        "ON corpus_seal.manifest_id = run.manifest_id "
        "WHERE run.projection_run_id = NEW.projection_run_id "
        "AND run.manifest_id = NEW.manifest_id "
        "AND run.config_sha256 = NEW.config_sha256 "
        "AND corpus_seal.completion_status = 'complete' "
        "AND NEW.sealed_at >= run.recorded_at) "
        "BEGIN SELECT RAISE(ABORT, "
        "'fact projection seal requires its complete configured run'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_search_fact_projection_seals_counts "
        "BEFORE INSERT ON search_fact_projection_seals WHEN "
        "NEW.eligible_fact_cell_count <> ("
        "SELECT COUNT(*) FROM fact_cells_v2 AS cell "
        "JOIN search_fact_projection_runs AS run "
        "ON run.projection_run_id = NEW.projection_run_id "
        "WHERE cell.knowledge_at <= run.knowledge_cutoff "
        "AND cell.recorded_at <= run.recorded_at) "
        "OR NEW.membership_count <> (SELECT COUNT(*) "
        "FROM search_fact_projection_memberships AS membership "
        "WHERE membership.projection_run_id = NEW.projection_run_id) "
        "OR NEW.membership_count <> NEW.eligible_fact_cell_count "
        "OR NEW.included_count <> (SELECT COUNT(*) "
        "FROM search_fact_projection_memberships AS membership "
        "WHERE membership.projection_run_id = NEW.projection_run_id "
        "AND membership.disposition = 'included') "
        "OR NEW.unresolved_material_count <> (SELECT COUNT(*) "
        "FROM search_fact_projection_memberships AS membership "
        "WHERE membership.projection_run_id = NEW.projection_run_id "
        "AND membership.disposition = 'unresolved_material') "
        "OR NEW.missing_provenance_count <> (SELECT COUNT(*) "
        "FROM search_fact_projection_memberships AS membership "
        "WHERE membership.projection_run_id = NEW.projection_run_id "
        "AND membership.disposition = 'missing_provenance') "
        "OR NEW.quarantined_count <> (SELECT COUNT(*) "
        "FROM search_fact_projection_memberships AS membership "
        "WHERE membership.projection_run_id = NEW.projection_run_id "
        "AND membership.disposition = 'quarantined') "
        "OR NEW.membership_count <> (NEW.included_count "
        "+ NEW.unresolved_material_count + NEW.missing_provenance_count "
        "+ NEW.quarantined_count) "
        "OR NEW.row_count <> (SELECT COUNT(*) "
        "FROM search_fact_projection_rows AS row "
        "WHERE row.projection_run_id = NEW.projection_run_id) "
        "OR NEW.row_count <> NEW.included_count "
        "BEGIN SELECT RAISE(ABORT, "
        "'fact projection seal counts do not match complete membership'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_search_fact_projection_seals_coverage "
        "BEFORE INSERT ON search_fact_projection_seals WHEN EXISTS ("
        "SELECT 1 FROM fact_cells_v2 AS cell "
        "JOIN search_fact_projection_runs AS run "
        "ON run.projection_run_id = NEW.projection_run_id "
        "LEFT JOIN search_fact_projection_memberships AS membership "
        "ON membership.projection_run_id = run.projection_run_id "
        "AND membership.fact_cell_id = cell.fact_cell_id "
        "WHERE cell.knowledge_at <= run.knowledge_cutoff "
        "AND cell.recorded_at <= run.recorded_at "
        "AND membership.membership_id IS NULL) "
        "OR EXISTS ("
        "SELECT 1 FROM search_fact_projection_memberships AS membership "
        "LEFT JOIN search_fact_projection_rows AS row "
        "ON row.projection_run_id = membership.projection_run_id "
        "AND row.fact_cell_id = membership.fact_cell_id "
        "WHERE membership.projection_run_id = NEW.projection_run_id "
        "AND ((membership.disposition = 'included' AND row.fact_hit_id IS NULL) "
        "OR (membership.disposition <> 'included' "
        "AND row.fact_hit_id IS NOT NULL))) "
        "OR EXISTS (SELECT 1 FROM search_fact_projection_memberships AS membership "
        "WHERE membership.projection_run_id = NEW.projection_run_id "
        "AND membership.recorded_at > NEW.sealed_at) "
        "OR EXISTS (SELECT 1 FROM search_fact_projection_rows AS row "
        "WHERE row.projection_run_id = NEW.projection_run_id "
        "AND row.recorded_at > NEW.sealed_at) "
        "BEGIN SELECT RAISE(ABORT, "
        "'fact projection seal requires exact disposition and row coverage'); END"
    )

    op.execute(
        "CREATE TRIGGER trg_ask_retrieval_trace_hits_rank_legacy "
        "BEFORE INSERT ON ask_retrieval_trace_hits WHEN EXISTS ("
        "SELECT 1 FROM ask_retrieval_trace_items AS item "
        "WHERE item.trace_id = NEW.trace_id AND item.rank = NEW.rank) "
        "BEGIN SELECT RAISE(ABORT, "
        "'retrieval rank already belongs to a legacy document hit'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_ask_retrieval_trace_items_rank_v2 "
        "BEFORE INSERT ON ask_retrieval_trace_items WHEN EXISTS ("
        "SELECT 1 FROM ask_retrieval_trace_hits AS hit "
        "WHERE hit.trace_id = NEW.trace_id AND hit.rank = NEW.rank) "
        "BEGIN SELECT RAISE(ABORT, "
        "'retrieval rank already belongs to a heterogeneous hit'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_ask_retrieval_trace_hits_document "
        "BEFORE INSERT ON ask_retrieval_trace_hits "
        "WHEN NEW.hit_kind = 'document' AND NOT EXISTS ("
        "SELECT 1 FROM search_chunks AS chunk "
        "JOIN search_corpus_manifest_seals AS seal "
        "ON seal.manifest_id = chunk.manifest_id "
        "WHERE chunk.chunk_id = NEW.chunk_id "
        "AND chunk.manifest_id = NEW.manifest_id "
        "AND seal.completion_status = 'complete') "
        "BEGIN SELECT RAISE(ABORT, "
        "'document hit requires a chunk from its complete corpus'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_ask_retrieval_trace_hits_fact "
        "BEFORE INSERT ON ask_retrieval_trace_hits "
        "WHEN NEW.hit_kind = 'fact' AND NOT EXISTS ("
        "SELECT 1 FROM search_fact_projection_rows AS row "
        "JOIN search_fact_projection_seals AS seal "
        "ON seal.projection_run_id = row.projection_run_id "
        "WHERE row.fact_hit_id = NEW.fact_hit_id "
        "AND row.projection_run_id = NEW.projection_run_id) "
        "BEGIN SELECT RAISE(ABORT, "
        "'fact hit requires a row from its sealed projection'); END"
    )

    for table, label in (
        ("search_fact_projection_runs", "fact projection run"),
        ("search_fact_projection_memberships", "fact projection membership"),
        ("search_fact_projection_rows", "fact projection row"),
        ("search_fact_projection_seals", "fact projection seal"),
        ("ask_retrieval_trace_hits", "heterogeneous retrieval trace"),
    ):
        _append_only(table, label)

    op.execute(
        "CREATE VIEW v_search_fact_projection_current_sealed AS "
        "SELECT run.*, seal.projection_seal_id, "
        "seal.eligible_fact_cell_count, seal.membership_count, "
        "seal.included_count, seal.unresolved_material_count, "
        "seal.missing_provenance_count, seal.quarantined_count, "
        "seal.row_count, seal.membership_set_sha256, seal.row_set_sha256, "
        "seal.sealed_at "
        "FROM search_fact_projection_runs AS run "
        "JOIN search_fact_projection_seals AS seal "
        "ON seal.projection_run_id = run.projection_run_id "
        "WHERE NOT EXISTS ("
        "SELECT 1 FROM search_fact_projection_runs AS newer "
        "JOIN search_fact_projection_seals AS newer_seal "
        "ON newer_seal.projection_run_id = newer.projection_run_id "
        "WHERE newer.projection_key = run.projection_key "
        "AND newer.revision > run.revision)"
    )
    op.execute(
        "CREATE VIEW v_search_fact_hits_current AS "
        "SELECT row.* FROM search_fact_projection_rows AS row "
        "JOIN v_search_fact_projection_current_sealed AS current "
        "ON current.projection_run_id = row.projection_run_id"
    )


def downgrade() -> None:
    for view in _VIEWS:
        op.execute(f"DROP VIEW IF EXISTS {view}")
    op.execute("DROP TRIGGER IF EXISTS trg_ask_retrieval_trace_items_rank_v2")
    for table in reversed(_TABLES):
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_append_only")
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_append_only_delete")
    for trigger in (
        "trg_ask_retrieval_trace_hits_fact",
        "trg_ask_retrieval_trace_hits_document",
        "trg_ask_retrieval_trace_hits_rank_legacy",
        "trg_search_fact_projection_seals_coverage",
        "trg_search_fact_projection_seals_counts",
        "trg_search_fact_projection_seals_contract",
        "trg_search_fact_projection_rows_exact",
        "trg_search_fact_projection_rows_membership",
        "trg_search_fact_projection_rows_unsealed",
        "trg_search_fact_projection_memberships_resolution_scope",
        "trg_search_fact_projection_memberships_unresolved",
        "trg_search_fact_projection_memberships_included",
        "trg_search_fact_projection_memberships_scope",
        "trg_search_fact_projection_memberships_unsealed",
        "trg_search_fact_projection_runs_parent",
        "trg_search_fact_projection_runs_first",
        "trg_search_fact_projection_runs_manifest",
    ):
        op.execute(f"DROP TRIGGER IF EXISTS {trigger}")
    for table in reversed(_TABLES):
        op.drop_table(table)
