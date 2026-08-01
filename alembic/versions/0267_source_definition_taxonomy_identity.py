"""Bind source-definition qualifiers to exact taxonomy coordinates.

Revision ID: 0267_source_definition_taxonomy_identity
Revises: 0266_canonical_resolution_operation_ledger
"""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy.engine import Connection

from alembic import op

revision: str = "0267_source_definition_taxonomy_identity"
down_revision: str | None = "0266_canonical_resolution_operation_ledger"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TRIGGER = "trg_binding_exact_coordinate"
_OLD = """AND source.definition_qualifier_sha256=fact_sha256(json_object(
                    'accounting_basis',source_cell.accounting_basis,
                    'concept_name',source_cell.concept_name,
                    'concept_namespace',source_cell.concept_namespace,
                    'consolidation_scope',source_cell.consolidation_scope,
                    'period_kind',source_cell.period_kind,
                    'reporting_entity_id',source_cell.reporting_entity_id,
                    'unit_family',CASE
                        WHEN source_cell.currency IS NOT NULL THEN 'currency'
                        WHEN lower(source_cell.unit_key) IN ('pure','shares')
                            THEN lower(source_cell.unit_key)
                        ELSE source_cell.unit_key END,
                    'value_kind',(
                        SELECT MIN(kind.value_kind)
                        FROM fact_observations_v2 kind
                        WHERE kind.fact_cell_id=source_cell.fact_cell_id
                          AND kind.observation_kind='reported'
                          AND kind.value_kind<>'nil'
                        GROUP BY kind.fact_cell_id
                        HAVING COUNT(DISTINCT kind.value_kind)=1)))
             """
_NEW = """AND source.definition_qualifier_sha256=fact_sha256(json_object(
                    'accounting_basis',source_cell.accounting_basis,
                    'concept_name',source_cell.concept_name,
                    'concept_namespace',source_cell.concept_namespace,
                    'consolidation_scope',source_cell.consolidation_scope,
                    'period_kind',source_cell.period_kind,
                    'reporting_entity_id',source_cell.reporting_entity_id,
                    'schema_version','source-definition-identity/v1',
                    'taxonomy_name',source_cell.taxonomy_name,
                    'taxonomy_version',anchor.source_taxonomy_version,
                    'unit_family',CASE
                        WHEN source_cell.currency IS NOT NULL THEN 'currency'
                        WHEN lower(source_cell.unit_key) IN ('pure','shares')
                            THEN lower(source_cell.unit_key)
                        ELSE source_cell.unit_key END,
                    'value_kind',(
                        SELECT MIN(kind.value_kind)
                        FROM fact_observations_v2 kind
                        WHERE kind.fact_cell_id=source_cell.fact_cell_id
                          AND kind.observation_kind='reported'
                          AND kind.value_kind<>'nil'
                        GROUP BY kind.fact_cell_id
                        HAVING COUNT(DISTINCT kind.value_kind)=1)))
             """


def _acquire_writer_lock(connection: Connection) -> None:
    """Reserve SQLite's writer before checking the immutable component plane."""

    connection.exec_driver_sql(
        "UPDATE source_taxonomy_components SET component_id=component_id WHERE 0"
    )


def _replace(connection: Connection, old: str, new: str) -> None:
    sql = connection.exec_driver_sql(
        "SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?",
        (_TRIGGER,),
    ).scalar()
    if not isinstance(sql, str) or old not in sql:
        raise RuntimeError("0267 binding qualifier trigger predecessor changed")
    connection.exec_driver_sql(f'DROP TRIGGER "{_TRIGGER}"')  # nosec B608 -- fixed migration constant
    connection.exec_driver_sql(sql.replace(old, new, 1))


def upgrade() -> None:
    connection = op.get_bind()
    _acquire_writer_lock(connection)
    existing = connection.exec_driver_sql(
        "SELECT 1 FROM source_taxonomy_components WHERE component_kind='concept' LIMIT 1"
    ).first()
    if existing is not None:
        raise RuntimeError(
            "0267 requires an empty concept-component plane before taxonomy identity activation"
        )
    _replace(connection, _OLD, _NEW)


def downgrade() -> None:
    connection = op.get_bind()
    _acquire_writer_lock(connection)
    existing = connection.exec_driver_sql(
        "SELECT 1 FROM source_taxonomy_components WHERE component_kind='concept' LIMIT 1"
    ).first()
    if existing is not None:
        raise RuntimeError("0267 is forward-only once taxonomy-qualified components exist")
    _replace(connection, _NEW, _OLD)
