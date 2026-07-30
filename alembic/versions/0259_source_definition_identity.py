"""Separate exact source coordinates from semantic definition identity.

Revision ID: 0259_source_definition_identity
Revises: 0258_fact_anchor_run_lookup_index
"""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy.engine import Connection

from alembic import op

revision: str = "0259_source_definition_identity"
down_revision: str | None = "0258_fact_anchor_run_lookup_index"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "source_taxonomy_components"
_OLD_TABLE = "source_taxonomy_components_0259_old"
_BINDING_COORDINATE_MARKER = (
    "AND source.reporting_entity_scope_key IN (\n"
    "                    '__global__', source_cell.reporting_entity_id)"
)
_BINDING_QUALIFIER_PREDICATE = """AND source.definition_qualifier_sha256=fact_sha256(json_object(
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
_AXIS_VERSION_PREDICATE = (
    "AND source_axis.taxonomy_version=\n"
    "                              anchor.source_taxonomy_version"
)
_AXIS_EXACT_PREDICATE = (
    "AND source_axis.taxonomy_name=taxonomy.taxonomy_name\n"
    "                          " + _AXIS_VERSION_PREDICATE
)
_MEMBER_VERSION_PREDICATE = (
    "AND source_member.taxonomy_version=\n"
    "                             anchor.source_taxonomy_version"
)
_MEMBER_EXACT_PREDICATE = (
    "AND source_member.taxonomy_name=taxonomy.taxonomy_name\n"
    "                         " + _MEMBER_VERSION_PREDICATE
)


def _table_sql(*, with_definition_qualifier: bool) -> str:
    qualifier_column = (
        """
            definition_qualifier_sha256 TEXT NOT NULL
                CHECK(length(definition_qualifier_sha256)=64
                      AND definition_qualifier_sha256
                          NOT GLOB '*[^0-9a-f]*'),"""
        if with_definition_qualifier
        else ""
    )
    uniqueness = (
        "taxonomy_name, taxonomy_version, definition_qualifier_sha256, reporting_entity_scope_key"
        if with_definition_qualifier
        else "taxonomy_name, taxonomy_version, reporting_entity_scope_key"
    )
    return f"""
        CREATE TABLE {_TABLE} (
            component_id TEXT PRIMARY KEY,
            idempotency_key TEXT NOT NULL UNIQUE,
            component_kind TEXT NOT NULL
                CHECK(component_kind IN ('concept','axis','member')),
            taxonomy_namespace TEXT NOT NULL,
            local_name TEXT NOT NULL,
            taxonomy_name TEXT NOT NULL,
            taxonomy_version TEXT NOT NULL,{qualifier_column}
            reporting_entity_id TEXT REFERENCES reporting_entities(
                reporting_entity_id),
            reporting_entity_scope_key TEXT NOT NULL,
            is_extension INTEGER NOT NULL CHECK(is_extension IN (0,1)),
            data_type TEXT,
            period_type TEXT CHECK(period_type IS NULL
                                   OR period_type IN ('instant','duration')),
            balance TEXT CHECK(balance IS NULL OR balance IN ('debit','credit')),
            is_abstract INTEGER CHECK(is_abstract IS NULL
                                      OR is_abstract IN (0,1)),
            standard_label TEXT,
            definition_text TEXT,
            references_json TEXT NOT NULL
                CHECK(json_valid(references_json)
                      AND json_type(references_json)='array'),
            evidence_locator_json TEXT NOT NULL
                CHECK(json_valid(evidence_locator_json)
                      AND json_type(evidence_locator_json)='object'),
            commitment_json TEXT NOT NULL
                CHECK(json_valid(commitment_json)
                      AND json_type(commitment_json)='object'),
            commitment_sha256 TEXT NOT NULL
                CHECK(length(commitment_sha256)=64
                      AND commitment_sha256 NOT GLOB '*[^0-9a-f]*'),
            effective_at DATETIME NOT NULL,
            knowledge_at DATETIME NOT NULL,
            recorded_at DATETIME NOT NULL,
            UNIQUE(component_kind, taxonomy_namespace, local_name,
                   {uniqueness}),
            CHECK(reporting_entity_scope_key =
                  COALESCE(reporting_entity_id, '__global__')),
            CHECK(is_extension=0 OR reporting_entity_id IS NOT NULL),
            CHECK(effective_at <= knowledge_at AND knowledge_at <= recorded_at)
        )
    """


def _create_table_triggers() -> None:
    commitment_trigger_sql = f"""
        CREATE TRIGGER trg_{_TABLE}_commitment_exact
        BEFORE INSERT ON {_TABLE}
        WHEN NEW.commitment_sha256 <> fact_sha256(NEW.commitment_json)
        BEGIN
            SELECT RAISE(ABORT, '{_TABLE} commitment mismatch');
        END
        """
    op.execute(
        commitment_trigger_sql  # nosec B608 -- fixed migration-owned table and trigger names
    )
    for event in ("UPDATE", "DELETE"):
        append_only_trigger_sql = f"""
        CREATE TRIGGER trg_{_TABLE}_{event.lower()}_append_only
        BEFORE {event} ON {_TABLE}
        BEGIN
            SELECT RAISE(ABORT, '{_TABLE} is append-only');
        END
        """
        op.execute(
            append_only_trigger_sql  # nosec B608 -- fixed migration-owned table and event names
        )
    op.execute(
        """
        CREATE TRIGGER trg_source_component_parent_clocks
        BEFORE INSERT ON source_taxonomy_components
        WHEN NEW.reporting_entity_id IS NOT NULL AND NOT EXISTS (
            SELECT 1 FROM reporting_entities entity
            WHERE entity.reporting_entity_id=NEW.reporting_entity_id
              AND datetime(entity.created_at) <= datetime(NEW.effective_at)
              AND datetime(entity.created_at) <= datetime(NEW.knowledge_at)
              AND datetime(entity.created_at) <= datetime(NEW.recorded_at))
        BEGIN
            SELECT RAISE(ABORT,
                'source component predates its reporting entity registry record');
        END
        """
    )


def _set_binding_qualifier_check(connection: Connection, *, enabled: bool) -> None:
    sql = connection.exec_driver_sql(
        "SELECT sql FROM sqlite_master WHERE type='trigger' AND name='trg_binding_exact_coordinate'"
    ).scalar()
    if not isinstance(sql, str):
        raise RuntimeError("0259 requires trg_binding_exact_coordinate")
    if enabled:
        if _BINDING_QUALIFIER_PREDICATE.strip() in sql:
            return
        if _BINDING_COORDINATE_MARKER not in sql:
            raise RuntimeError("0259 binding trigger coordinate marker changed")
        replacement = _BINDING_QUALIFIER_PREDICATE + _BINDING_COORDINATE_MARKER
        sql = sql.replace(_BINDING_COORDINATE_MARKER, replacement, 1)
        for old, new in (
            (_AXIS_VERSION_PREDICATE, _AXIS_EXACT_PREDICATE),
            (_MEMBER_VERSION_PREDICATE, _MEMBER_EXACT_PREDICATE),
        ):
            if old not in sql:
                raise RuntimeError("0259 binding dimension coordinate marker changed")
            sql = sql.replace(old, new, 1)
    else:
        qualified = _BINDING_QUALIFIER_PREDICATE + _BINDING_COORDINATE_MARKER
        if qualified not in sql:
            raise RuntimeError("0259 binding qualifier predicate is missing")
        sql = sql.replace(qualified, _BINDING_COORDINATE_MARKER, 1)
        for old, new in (
            (_AXIS_EXACT_PREDICATE, _AXIS_VERSION_PREDICATE),
            (_MEMBER_EXACT_PREDICATE, _MEMBER_VERSION_PREDICATE),
        ):
            if old not in sql:
                raise RuntimeError("0259 binding dimension taxonomy predicate is missing")
            sql = sql.replace(old, new, 1)
    connection.exec_driver_sql("DROP TRIGGER trg_binding_exact_coordinate")
    connection.exec_driver_sql(sql)


def _rebuild(*, with_definition_qualifier: bool) -> None:
    connection = op.get_bind()
    context = op.get_context()
    with context.autocommit_block():
        foreign_keys = int(connection.exec_driver_sql("PRAGMA foreign_keys").scalar() or 0)
        legacy_alter = int(connection.exec_driver_sql("PRAGMA legacy_alter_table").scalar() or 0)
        connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
        connection.exec_driver_sql("PRAGMA legacy_alter_table=ON")
        connection.exec_driver_sql("BEGIN IMMEDIATE")
        try:
            connection.exec_driver_sql(f"ALTER TABLE {_TABLE} RENAME TO {_OLD_TABLE}")
            connection.exec_driver_sql(
                _table_sql(with_definition_qualifier=with_definition_qualifier)
            )
            if with_definition_qualifier:
                connection.exec_driver_sql(
                    """
                    INSERT INTO source_taxonomy_components (
                        component_id,idempotency_key,component_kind,
                        taxonomy_namespace,local_name,taxonomy_name,
                        taxonomy_version,definition_qualifier_sha256,
                        reporting_entity_id,reporting_entity_scope_key,
                        is_extension,data_type,period_type,balance,is_abstract,
                        standard_label,definition_text,references_json,
                        evidence_locator_json,commitment_json,commitment_sha256,
                        effective_at,knowledge_at,recorded_at)
                    SELECT component_id,idempotency_key,component_kind,
                           taxonomy_namespace,local_name,taxonomy_name,
                           taxonomy_version,
                           CASE
                             WHEN json_type(
                                evidence_locator_json,
                                '$.source_definition_commitment_sha256')='text'
                              AND length(json_extract(
                                evidence_locator_json,
                                '$.source_definition_commitment_sha256'))=64
                              AND json_extract(
                                evidence_locator_json,
                                '$.source_definition_commitment_sha256')
                                  NOT GLOB '*[^0-9a-f]*'
                             THEN json_extract(
                                evidence_locator_json,
                                '$.source_definition_commitment_sha256')
                             WHEN component_kind IN ('axis','member')
                             THEN commitment_sha256
                             ELSE NULL
                           END,
                           reporting_entity_id,reporting_entity_scope_key,
                           is_extension,data_type,period_type,balance,is_abstract,
                           standard_label,definition_text,references_json,
                           evidence_locator_json,commitment_json,commitment_sha256,
                           effective_at,knowledge_at,recorded_at
                    FROM source_taxonomy_components_0259_old
                    """
                )
            else:
                connection.exec_driver_sql(
                    """
                    INSERT INTO source_taxonomy_components (
                        component_id,idempotency_key,component_kind,
                        taxonomy_namespace,local_name,taxonomy_name,
                        taxonomy_version,reporting_entity_id,
                        reporting_entity_scope_key,is_extension,data_type,
                        period_type,balance,is_abstract,standard_label,
                        definition_text,references_json,evidence_locator_json,
                        commitment_json,commitment_sha256,effective_at,
                        knowledge_at,recorded_at)
                    SELECT component_id,idempotency_key,component_kind,
                           taxonomy_namespace,local_name,taxonomy_name,
                           taxonomy_version,reporting_entity_id,
                           reporting_entity_scope_key,is_extension,data_type,
                           period_type,balance,is_abstract,standard_label,
                           definition_text,references_json,evidence_locator_json,
                           commitment_json,commitment_sha256,effective_at,
                           knowledge_at,recorded_at
                    FROM source_taxonomy_components_0259_old
                    """
                )
            connection.exec_driver_sql(f"DROP TABLE {_OLD_TABLE}")
            _create_table_triggers()
            _set_binding_qualifier_check(
                connection,
                enabled=with_definition_qualifier,
            )
            violations = connection.exec_driver_sql("PRAGMA foreign_key_check").fetchall()
            if violations:
                raise RuntimeError("0259 source taxonomy rebuild created foreign-key violations")
            connection.exec_driver_sql("COMMIT")
        except Exception:
            connection.exec_driver_sql("ROLLBACK")
            raise
        finally:
            connection.exec_driver_sql(f"PRAGMA legacy_alter_table={legacy_alter}")
            connection.exec_driver_sql(f"PRAGMA foreign_keys={foreign_keys}")


def upgrade() -> None:
    connection = op.get_bind()
    unresolved_concept = connection.exec_driver_sql(
        """
        SELECT component_id
        FROM source_taxonomy_components
        WHERE component_kind='concept'
          AND (
            json_type(
                evidence_locator_json,
                '$.source_definition_commitment_sha256') IS NOT 'text'
            OR length(json_extract(
                evidence_locator_json,
                '$.source_definition_commitment_sha256'))<>64
            OR json_extract(
                evidence_locator_json,
                '$.source_definition_commitment_sha256')
                    GLOB '*[^0-9a-f]*'
          )
        LIMIT 1
        """
    ).first()
    if unresolved_concept is not None:
        raise RuntimeError("0259 cannot reconstruct an existing concept definition qualifier")
    _rebuild(with_definition_qualifier=True)


def downgrade() -> None:
    connection = op.get_bind()
    duplicate = connection.exec_driver_sql(
        """
        SELECT 1
        FROM source_taxonomy_components
        GROUP BY component_kind,taxonomy_namespace,local_name,taxonomy_name,
                 taxonomy_version,reporting_entity_scope_key
        HAVING COUNT(*) > 1
        LIMIT 1
        """
    ).first()
    if duplicate is not None:
        raise RuntimeError("0259 downgrade would collapse distinct source definition qualifiers")
    _rebuild(with_definition_qualifier=False)
