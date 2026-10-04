"""Admit sealed, explicitly reviewed financial derivations without changing raw facts.

Revision ID: 0053_reviewed_financial_derivations
Revises: 0052_kpi_source_precision
"""

from __future__ import annotations

from alembic import op

revision = "0053_reviewed_financial_derivations"
down_revision = "0052_kpi_source_precision"
branch_labels = None
depends_on = None

_DERIVED_TRIGGER = """
CREATE TRIGGER trg_binding_reviewed_derived_coordinate
BEFORE INSERT ON fact_cell_canonical_binding_revisions
WHEN NEW.binding_status='bound'
 AND EXISTS (SELECT 1 FROM fact_observations_v2 o
             WHERE o.observation_id=NEW.source_observation_id AND o.observation_kind='derived')
 AND NOT EXISTS (
 SELECT 1 FROM fact_observations_v2 observation
 JOIN fact_cells_v2 cell ON cell.fact_cell_id=observation.fact_cell_id
 JOIN fact_derivation_seals_v2 seal ON seal.output_observation_id=observation.observation_id
 JOIN fact_derivation_basis_commitments_v2 basis ON basis.derivation_seal_id=seal.derivation_seal_id
 JOIN source_taxonomy_components source ON source.component_id=NEW.source_component_id
 JOIN metric_mapping_revisions mapping ON mapping.mapping_revision_id=NEW.mapping_revision_id
 JOIN canonical_metric_cells target ON target.canonical_metric_cell_id=NEW.canonical_metric_cell_id
 JOIN canonical_metric_cell_seals target_seal ON target_seal.canonical_metric_cell_id=target.canonical_metric_cell_id
 WHERE observation.observation_id=NEW.source_observation_id AND cell.fact_cell_id=NEW.fact_cell_id
 AND source.component_kind='concept' AND source.taxonomy_namespace=cell.concept_namespace
 AND source.local_name=cell.concept_name AND source.taxonomy_name=cell.taxonomy_name
 AND source.taxonomy_version=cell.taxonomy_version
 AND source.reporting_entity_scope_key=cell.reporting_entity_id
 AND mapping.source_component_id=source.component_id AND mapping.disposition='derived'
 AND length(mapping.reviewer_identity)>0
 AND json_extract(mapping.constraints_json,'$.derived_formula.formula_id')=basis.formula_id
 AND json_extract(mapping.constraints_json,'$.derived_formula.formula_version')=basis.formula_version
 AND json_extract(mapping.constraints_json,'$.derived_formula.formula_definition_sha256')=basis.formula_definition_sha256
 AND basis.input_basis='as_known' AND basis.formula_id=observation.formula_id
 AND basis.formula_version=observation.formula_version
 AND seal.input_count>0 AND seal.input_count=(SELECT COUNT(*) FROM fact_derivation_input_edges_v2 e WHERE e.output_observation_id=observation.observation_id)
 AND NOT EXISTS (SELECT 1 FROM fact_derivation_input_edges_v2 e
                 WHERE e.output_observation_id=observation.observation_id AND e.input_canonical_resolution_revision_id IS NULL)
 AND target.metric_id=mapping.metric_id AND target.reporting_entity_id=cell.reporting_entity_id
 AND target.scope_security_id IS cell.scope_security_id AND target.period_kind=cell.period_kind
 AND datetime(target.period_start) IS datetime(cell.period_start)
 AND datetime(target.period_end)=datetime(cell.period_end)
 AND target.accounting_basis=cell.accounting_basis AND target.consolidation_scope=cell.consolidation_scope
 AND target.unit_family=CASE WHEN cell.currency IS NOT NULL THEN 'currency' ELSE cell.unit_key END
 AND (mapping.policy_name<>'reviewed_financial_metric' OR (
   json_extract(mapping.constraints_json,'$.source_currency')=cell.currency
   AND EXISTS (SELECT 1 FROM canonical_metric_definition_revisions definition
     WHERE definition.metric_id=target.metric_id AND definition.lifecycle='active'
     AND json_extract(definition.scope_constraints_json,'$.currency')=cell.currency
     AND json_extract(definition.scope_constraints_json,'$.reporting_entity_id')=cell.reporting_entity_id
     AND definition.accounting_basis=cell.accounting_basis
     AND datetime(definition.knowledge_at)<=datetime(NEW.knowledge_at)
     AND datetime(definition.recorded_at)<=datetime(NEW.recorded_at)
     AND NOT EXISTS (SELECT 1 FROM canonical_metric_definition_revisions newer
       WHERE newer.metric_id=definition.metric_id AND newer.revision>definition.revision
       AND datetime(newer.knowledge_at)<=datetime(NEW.knowledge_at)
       AND datetime(newer.recorded_at)<=datetime(NEW.recorded_at)))
 ))
 AND target.dimension_count=0 AND NOT EXISTS (SELECT 1 FROM fact_dimensions_normalized_v2 d WHERE d.fact_cell_id=cell.fact_cell_id)
 AND datetime(observation.knowledge_at)<=datetime(NEW.knowledge_at)
 AND datetime(observation.recorded_at)<=datetime(NEW.recorded_at)
 AND datetime(seal.knowledge_at)<=datetime(NEW.knowledge_at) AND datetime(seal.recorded_at)<=datetime(NEW.recorded_at)
 AND datetime(source.effective_at)<=datetime(NEW.effective_at)
 AND datetime(source.knowledge_at)<=datetime(NEW.knowledge_at) AND datetime(source.recorded_at)<=datetime(NEW.recorded_at)
 AND datetime(mapping.effective_at)<=datetime(NEW.effective_at)
 AND datetime(mapping.knowledge_at)<=datetime(NEW.knowledge_at) AND datetime(mapping.recorded_at)<=datetime(NEW.recorded_at)
 AND datetime(cell.effective_at)<=datetime(NEW.effective_at)
 AND datetime(cell.knowledge_at)<=datetime(NEW.knowledge_at) AND datetime(cell.recorded_at)<=datetime(NEW.recorded_at)
 AND datetime(target.effective_at)<=datetime(NEW.effective_at)
 AND datetime(target.knowledge_at)<=datetime(NEW.knowledge_at) AND datetime(target.recorded_at)<=datetime(NEW.recorded_at)
 AND datetime(target_seal.sealed_at)<=datetime(NEW.recorded_at)
 )
BEGIN SELECT RAISE(ABORT,'derived binding requires exact sealed reviewed formula and coordinates'); END
"""


def _replace_candidate_lane(*, upgrading: bool) -> None:
    """Copy every immutable row and preserve dependent triggers and explicit indexes."""
    conn = op.get_bind()
    table = "canonical_fact_candidate_dispositions"
    ddl = str(
        conn.exec_driver_sql(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).scalar_one()
    )
    old = "'derived_terminal_exclusion'"
    new = "'derived_terminal_exclusion','derived_source_publication'"
    ddl = ddl.replace(old, new) if upgrading else ddl.replace(new, old)
    ddl = ddl.replace(
        "CREATE TABLE canonical_fact_candidate_dispositions",
        "CREATE TABLE canonical_fact_candidate_dispositions_rebuilt",
        1,
    )
    ddl = ddl.replace(
        'CREATE TABLE "canonical_fact_candidate_dispositions"',
        "CREATE TABLE canonical_fact_candidate_dispositions_rebuilt",
        1,
    )
    triggers = conn.exec_driver_sql(
        "SELECT name,sql FROM sqlite_master WHERE type='trigger' AND instr(sql,?)>0", (table,)
    ).fetchall()
    indexes = conn.exec_driver_sql(
        "SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name=? AND sql IS NOT NULL",
        (table,),
    ).fetchall()
    for name, _ in triggers:
        conn.exec_driver_sql(f'DROP TRIGGER "{name}"')
    conn.exec_driver_sql(ddl)
    conn.exec_driver_sql(
        "INSERT INTO canonical_fact_candidate_dispositions_rebuilt SELECT * FROM canonical_fact_candidate_dispositions"
    )
    conn.exec_driver_sql("DROP TABLE canonical_fact_candidate_dispositions")
    conn.exec_driver_sql(
        "ALTER TABLE canonical_fact_candidate_dispositions_rebuilt RENAME TO canonical_fact_candidate_dispositions"
    )
    for row in indexes:
        conn.exec_driver_sql(str(row[0]))
    for _, sql in triggers:
        conn.exec_driver_sql(str(sql))


def upgrade() -> None:
    conn = op.get_bind()
    reported_sql = str(
        conn.exec_driver_sql(
            "SELECT sql FROM sqlite_master WHERE type='trigger' AND name='trg_binding_exact_coordinate'"
        ).scalar_one()
    )
    conn.exec_driver_sql("DROP TRIGGER trg_binding_exact_coordinate")
    reported_sql = reported_sql.replace(
        "WHEN NEW.binding_status='bound' AND NOT EXISTS",
        "WHEN NEW.binding_status='bound' AND EXISTS (SELECT 1 FROM fact_observations_v2 o WHERE o.observation_id=NEW.source_observation_id AND o.observation_kind='reported') AND NOT EXISTS",
        1,
    )
    conn.exec_driver_sql(reported_sql)
    conn.exec_driver_sql(_DERIVED_TRIGGER)
    _replace_candidate_lane(upgrading=True)


def downgrade() -> None:
    conn = op.get_bind()
    if (
        conn.exec_driver_sql(
            "SELECT 1 FROM fact_cell_canonical_binding_revisions b JOIN fact_observations_v2 o ON o.observation_id=b.source_observation_id WHERE b.binding_status='bound' AND o.observation_kind='derived' LIMIT 1"
        ).first()
        is not None
    ):
        raise RuntimeError("refusing to discard reviewed derived financial bindings")
    conn.exec_driver_sql("DROP TRIGGER trg_binding_reviewed_derived_coordinate")
    reported_sql = str(
        conn.exec_driver_sql(
            "SELECT sql FROM sqlite_master WHERE type='trigger' AND name='trg_binding_exact_coordinate'"
        ).scalar_one()
    )
    conn.exec_driver_sql("DROP TRIGGER trg_binding_exact_coordinate")
    reported_sql = reported_sql.replace(
        " AND EXISTS (SELECT 1 FROM fact_observations_v2 o WHERE o.observation_id=NEW.source_observation_id AND o.observation_kind='reported')",
        "",
        1,
    )
    conn.exec_driver_sql(reported_sql)
    _replace_candidate_lane(upgrading=False)
