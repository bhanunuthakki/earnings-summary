"""Bind metric attempts to admitted canonical output observations.

Revision ID: 0048_metric_computation_output_observation
Revises: 0047_source_regime_measurements
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0048_metric_computation_output_observation"
down_revision = "0047_source_regime_measurements"
branch_labels = None
depends_on = None


_CANONICAL_BASIS_TRIGGER = """
CREATE TRIGGER trg_fact_derivation_basis_v2_exact
BEFORE INSERT ON fact_derivation_basis_commitments_v2
WHEN NOT EXISTS (
    SELECT 1
    FROM fact_derivation_seals_v2 AS seal
    JOIN fact_observations_v2 AS output
      ON output.observation_id = seal.output_observation_id
    WHERE seal.derivation_seal_id = NEW.derivation_seal_id
      AND output.observation_kind = 'derived'
      AND output.formula_id = NEW.formula_id
      AND output.formula_version = NEW.formula_version
      AND seal.formula_config_sha256 = NEW.execution_config_sha256
      AND julianday(seal.knowledge_at) <= julianday(NEW.knowledge_cutoff)
      AND julianday(seal.recorded_at) <= julianday(NEW.recorded_at)
      AND NEW.canonical_basis_json = fact_derivation_basis_v1(
          seal.canonical_input_digest_sha256,
          NEW.execution_config_sha256,
          NEW.formula_definition_sha256,
          NEW.formula_id,
          NEW.formula_version,
          NEW.input_basis,
          NEW.knowledge_cutoff
      )
      AND NEW.canonical_basis_sha256 = fact_sha256(NEW.canonical_basis_json)
      AND NOT EXISTS (
          SELECT 1
          FROM fact_derivation_input_edges_v2 AS edge
          JOIN fact_observations_v2 AS input
            ON input.observation_id = edge.input_observation_id
          LEFT JOIN fact_resolution_revisions_v2 AS resolution
            ON resolution.resolution_revision_id = edge.input_resolution_revision_id
          LEFT JOIN canonical_fact_resolution_revisions AS canonical_resolution
            ON canonical_resolution.canonical_resolution_revision_id =
               edge.input_canonical_resolution_revision_id
          LEFT JOIN canonical_metric_cells AS canonical_cell
            ON canonical_cell.canonical_metric_cell_id =
               canonical_resolution.canonical_metric_cell_id
          LEFT JOIN canonical_fact_candidate_dispositions AS disposition
            ON disposition.candidate_universe_id =
               canonical_resolution.candidate_universe_id
           AND disposition.observation_id = input.observation_id
           AND disposition.source_fact_cell_id = input.fact_cell_id
           AND disposition.eligibility = 'eligible'
          LEFT JOIN fact_cell_canonical_binding_revisions AS binding
            ON binding.binding_revision_id = disposition.binding_revision_id
           AND binding.source_observation_id = input.observation_id
           AND binding.fact_cell_id = input.fact_cell_id
           AND binding.canonical_metric_cell_id =
               canonical_resolution.canonical_metric_cell_id
           AND binding.binding_status = 'bound'
          LEFT JOIN canonical_fact_candidate_universe_revisions AS universe
            ON universe.candidate_universe_id =
               canonical_resolution.candidate_universe_id
           AND universe.canonical_metric_cell_id =
               canonical_resolution.canonical_metric_cell_id
          WHERE edge.output_observation_id = output.observation_id
            AND (
                julianday(input.knowledge_at) > julianday(NEW.knowledge_cutoff)
                OR julianday(input.recorded_at) > julianday(NEW.recorded_at)
                OR julianday(input.effective_at) > julianday(output.effective_at)
                OR (
                    NEW.input_basis = 'as_reported'
                    AND (
                        edge.input_resolution_revision_id IS NOT NULL
                        OR edge.input_canonical_resolution_revision_id IS NOT NULL
                    )
                )
                OR (
                    NEW.input_basis = 'as_known'
                    AND NOT (
                        (
                            edge.input_resolution_revision_id IS NOT NULL
                            AND edge.input_canonical_resolution_revision_id IS NULL
                            AND resolution.resolution_revision_id IS NOT NULL
                            AND resolution.status = 'resolved'
                            AND resolution.selected_observation_id = input.observation_id
                            AND resolution.fact_cell_id = input.fact_cell_id
                            AND julianday(resolution.knowledge_at) <=
                                julianday(NEW.knowledge_cutoff)
                            AND julianday(resolution.recorded_at) <=
                                julianday(NEW.recorded_at)
                        )
                        OR (
                            edge.input_resolution_revision_id IS NULL
                            AND edge.input_canonical_resolution_revision_id IS NOT NULL
                            AND canonical_resolution.canonical_resolution_revision_id IS NOT NULL
                            AND canonical_resolution.status = 'resolved'
                            AND canonical_resolution.selected_observation_id = input.observation_id
                            AND julianday(canonical_resolution.knowledge_at) <=
                                julianday(NEW.knowledge_cutoff)
                            AND julianday(canonical_resolution.recorded_at) <=
                                julianday(NEW.knowledge_cutoff)
                            AND julianday(input.recorded_at) <=
                                julianday(NEW.knowledge_cutoff)
                            AND canonical_cell.canonical_metric_cell_id IS NOT NULL
                            AND julianday(canonical_cell.knowledge_at) <=
                                julianday(NEW.knowledge_cutoff)
                            AND julianday(canonical_cell.recorded_at) <=
                                julianday(NEW.knowledge_cutoff)
                            AND disposition.candidate_disposition_id IS NOT NULL
                            AND julianday(disposition.knowledge_at) <=
                                julianday(NEW.knowledge_cutoff)
                            AND julianday(disposition.recorded_at) <=
                                julianday(NEW.knowledge_cutoff)
                            AND binding.binding_revision_id IS NOT NULL
                            AND julianday(binding.knowledge_at) <=
                                julianday(NEW.knowledge_cutoff)
                            AND julianday(binding.recorded_at) <=
                                julianday(NEW.knowledge_cutoff)
                            AND universe.candidate_universe_id IS NOT NULL
                            AND julianday(universe.knowledge_at) <=
                                julianday(NEW.knowledge_cutoff)
                            AND julianday(universe.recorded_at) <=
                                julianday(NEW.knowledge_cutoff)
                            AND NOT EXISTS (
                                SELECT 1
                                FROM canonical_fact_resolution_revisions AS newer_resolution
                                WHERE newer_resolution.canonical_metric_cell_id =
                                      canonical_resolution.canonical_metric_cell_id
                                  AND julianday(newer_resolution.knowledge_at) <=
                                      julianday(NEW.knowledge_cutoff)
                                  AND julianday(newer_resolution.recorded_at) <=
                                      julianday(NEW.knowledge_cutoff)
                                  AND newer_resolution.revision > canonical_resolution.revision
                            )
                        )
                    )
                )
            )
      )
)
BEGIN
    SELECT RAISE(ABORT, 'derivation basis violates formula or no-look-ahead commitments');
END
"""


_LEGACY_BASIS_TRIGGER = """
CREATE TRIGGER trg_fact_derivation_basis_v2_exact
BEFORE INSERT ON fact_derivation_basis_commitments_v2
WHEN NOT EXISTS (
    SELECT 1
    FROM fact_derivation_seals_v2 AS seal
    JOIN fact_observations_v2 AS output
      ON output.observation_id = seal.output_observation_id
    WHERE seal.derivation_seal_id = NEW.derivation_seal_id
      AND output.observation_kind = 'derived'
      AND output.formula_id = NEW.formula_id
      AND output.formula_version = NEW.formula_version
      AND seal.formula_config_sha256 = NEW.execution_config_sha256
      AND seal.knowledge_at <= NEW.knowledge_cutoff
      AND seal.recorded_at <= NEW.recorded_at
      AND NEW.canonical_basis_json = fact_derivation_basis_v1(
          seal.canonical_input_digest_sha256,
          NEW.execution_config_sha256,
          NEW.formula_definition_sha256,
          NEW.formula_id,
          NEW.formula_version,
          NEW.input_basis,
          NEW.knowledge_cutoff
      )
      AND NEW.canonical_basis_sha256 = fact_sha256(NEW.canonical_basis_json)
      AND NOT EXISTS (
          SELECT 1
          FROM fact_derivation_input_edges_v2 AS edge
          JOIN fact_observations_v2 AS input
            ON input.observation_id = edge.input_observation_id
          LEFT JOIN fact_resolution_revisions_v2 AS resolution
            ON resolution.resolution_revision_id = edge.input_resolution_revision_id
          WHERE edge.output_observation_id = output.observation_id
            AND (
                input.knowledge_at > NEW.knowledge_cutoff
                OR input.recorded_at > NEW.recorded_at
                OR input.effective_at > output.effective_at
                OR (
                    NEW.input_basis = 'as_reported'
                    AND edge.input_resolution_revision_id IS NOT NULL
                )
                OR (
                    NEW.input_basis = 'as_known'
                    AND (
                        resolution.resolution_revision_id IS NULL
                        OR resolution.selected_observation_id <> input.observation_id
                        OR resolution.knowledge_at > NEW.knowledge_cutoff
                        OR resolution.recorded_at > NEW.recorded_at
                    )
                )
            )
      )
)
BEGIN
    SELECT RAISE(ABORT, 'derivation basis violates formula or no-look-ahead commitments');
END
"""


def upgrade() -> None:
    op.add_column(
        "metric_computation_attempts",
        sa.Column("output_observation_id", sa.Text(), nullable=True),
    )
    op.create_index(
        "ix_metric_computation_attempt_output_observation",
        "metric_computation_attempts",
        ["output_observation_id"],
    )
    op.execute(
        "ALTER TABLE fact_derivation_input_edges_v2 "
        "ADD COLUMN input_canonical_resolution_revision_id TEXT "
        "REFERENCES canonical_fact_resolution_revisions("
        "canonical_resolution_revision_id)"
    )
    op.create_index(
        "ix_fact_derivation_edge_canonical_resolution",
        "fact_derivation_input_edges_v2",
        ["input_canonical_resolution_revision_id"],
    )
    op.execute("DROP TRIGGER trg_fact_derivation_basis_v2_exact")
    op.execute(_CANONICAL_BASIS_TRIGGER)
    op.execute("""
        CREATE TRIGGER trg_fact_derivation_edges_v2_resolution_domain
        BEFORE INSERT ON fact_derivation_input_edges_v2
        WHEN NEW.input_resolution_revision_id IS NOT NULL
         AND NEW.input_canonical_resolution_revision_id IS NOT NULL
        BEGIN
            SELECT RAISE(ABORT, 'derivation input cannot mix resolution domains');
        END
    """)
    op.execute("""
        CREATE TRIGGER trg_fact_derivation_edges_v2_canonical_resolution
        BEFORE INSERT ON fact_derivation_input_edges_v2
        WHEN NEW.input_canonical_resolution_revision_id IS NOT NULL
         AND NOT EXISTS (
             SELECT 1
             FROM canonical_fact_resolution_revisions AS resolution
             JOIN fact_observations_v2 AS input
               ON input.observation_id = NEW.input_observation_id
             JOIN canonical_fact_candidate_dispositions AS disposition
               ON disposition.candidate_universe_id = resolution.candidate_universe_id
              AND disposition.observation_id = input.observation_id
              AND disposition.source_fact_cell_id = input.fact_cell_id
              AND disposition.eligibility = 'eligible'
             JOIN fact_cell_canonical_binding_revisions AS binding
               ON binding.binding_revision_id = disposition.binding_revision_id
              AND binding.source_observation_id = input.observation_id
              AND binding.fact_cell_id = input.fact_cell_id
              AND binding.canonical_metric_cell_id =
                  resolution.canonical_metric_cell_id
              AND binding.binding_status = 'bound'
             WHERE resolution.canonical_resolution_revision_id =
                   NEW.input_canonical_resolution_revision_id
               AND resolution.status = 'resolved'
               AND resolution.selected_observation_id = input.observation_id
         )
        BEGIN
            SELECT RAISE(ABORT, 'canonical derivation resolution must select the exact bound input');
        END
    """)


def downgrade() -> None:
    retained = (
        op.get_bind()
        .exec_driver_sql(
            "SELECT 1 FROM metric_computation_attempts "
            "WHERE output_observation_id IS NOT NULL LIMIT 1"
        )
        .fetchone()
    )
    if retained is not None:
        raise RuntimeError("refusing to discard canonical metric output identities")
    canonical_edges = (
        op.get_bind()
        .exec_driver_sql(
            "SELECT 1 FROM fact_derivation_input_edges_v2 "
            "WHERE input_canonical_resolution_revision_id IS NOT NULL LIMIT 1"
        )
        .fetchone()
    )
    if canonical_edges is not None:
        raise RuntimeError("refusing to discard canonical derivation resolution identities")
    op.execute("DROP TRIGGER trg_fact_derivation_edges_v2_canonical_resolution")
    op.execute("DROP TRIGGER trg_fact_derivation_edges_v2_resolution_domain")
    op.execute("DROP TRIGGER trg_fact_derivation_basis_v2_exact")
    op.drop_index(
        "ix_fact_derivation_edge_canonical_resolution",
        table_name="fact_derivation_input_edges_v2",
    )
    op.drop_column(
        "fact_derivation_input_edges_v2",
        "input_canonical_resolution_revision_id",
    )
    op.execute(_LEGACY_BASIS_TRIGGER)
    op.drop_index(
        "ix_metric_computation_attempt_output_observation",
        table_name="metric_computation_attempts",
    )
    op.drop_column("metric_computation_attempts", "output_observation_id")
