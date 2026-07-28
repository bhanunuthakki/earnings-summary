"""Govern exact source identities and source-independent canonical metrics.

Revision ID: 0243_metric_ontology
Revises: 0242_filing_xbrl_extraction_dispositions
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0243_metric_ontology"
down_revision: str | Sequence[str] | None = "0242_filing_xbrl_extraction_dispositions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLES = (
    "canonical_metrics",
    "canonical_axes",
    "canonical_members",
    "source_taxonomy_components",
    "source_observation_taxonomy_assertions",
    "canonical_metric_definition_revisions",
    "source_dimension_mapping_revisions",
    "metric_mapping_revisions",
    "canonical_metric_cells",
    "canonical_metric_cell_dimensions",
    "canonical_metric_cell_seals",
    "fact_cell_canonical_binding_revisions",
    "ontology_snapshot_headers",
    "ontology_snapshot_members",
    "ontology_snapshot_seals",
)


def _execute(sql: str) -> None:
    op.execute(sql)


def _append_only(table: str) -> None:
    for event in ("UPDATE", "DELETE"):
        _execute(
            f"CREATE TRIGGER trg_{table}_{event.lower()}_append_only "
            f"BEFORE {event} ON {table} BEGIN SELECT RAISE(ABORT, "
            f"'{table} is append-only'); END"
        )


def _commitment_exact(table: str) -> None:
    _execute(
        f"CREATE TRIGGER trg_{table}_commitment_exact BEFORE INSERT ON {table} "
        "WHEN NEW.commitment_sha256 <> fact_sha256(NEW.commitment_json) "
        f"BEGIN SELECT RAISE(ABORT, '{table} commitment mismatch'); END"
    )


def _sequential_revision(
    table: str,
    *,
    coordinate: str,
    id_column: str,
    supersedes_column: str,
) -> None:
    _execute(
        f"CREATE TRIGGER trg_{table}_sequential BEFORE INSERT ON {table} WHEN "
        f"(NEW.revision = 1 AND (NEW.{supersedes_column} IS NOT NULL OR "
        f"EXISTS (SELECT 1 FROM {table} old WHERE old.{coordinate}=NEW.{coordinate}))) "
        f"OR (NEW.revision > 1 AND (NEW.{supersedes_column} IS NULL OR "
        f"NOT EXISTS (SELECT 1 FROM {table} parent WHERE "
        f"parent.{id_column}=NEW.{supersedes_column} AND "
        f"parent.{coordinate}=NEW.{coordinate} AND "
        "parent.revision=NEW.revision-1 AND "
        "datetime(parent.effective_at) <= datetime(NEW.effective_at) AND "
        "datetime(parent.knowledge_at) <= datetime(NEW.knowledge_at) AND "
        "datetime(parent.recorded_at) <= datetime(NEW.recorded_at)))) "
        f"BEGIN SELECT RAISE(ABORT, '{table} revision is not sequential'); END"
    )


def upgrade() -> None:
    required = {
        "fact_cells_v2",
        "fact_observations_v2",
        "fact_dimensions_normalized_v2",
        "fact_reported_observation_anchors_v2",
        "fact_cell_identity_seals_v2",
        "fact_observation_payload_commitments_v2",
        "fact_extraction_run_completeness_seals_v2",
        "evidence_extraction_runs",
    }
    missing = sorted(required - set(sa.inspect(op.get_bind()).get_table_names()))
    if missing:
        raise RuntimeError(
            "metric ontology requires the hardened fact plane: " + ", ".join(missing)
        )

    _execute(
        """
        CREATE TABLE canonical_metrics (
            metric_id TEXT PRIMARY KEY,
            idempotency_key TEXT NOT NULL UNIQUE,
            canonical_name TEXT NOT NULL UNIQUE,
            commitment_json TEXT NOT NULL
                CHECK(json_valid(commitment_json)
                      AND json_type(commitment_json)='object'),
            commitment_sha256 TEXT NOT NULL
                CHECK(length(commitment_sha256)=64
                      AND commitment_sha256 NOT GLOB '*[^0-9a-f]*'),
            effective_at DATETIME NOT NULL,
            knowledge_at DATETIME NOT NULL,
            recorded_at DATETIME NOT NULL,
            CHECK(effective_at <= knowledge_at AND knowledge_at <= recorded_at)
        )
        """
    )
    _execute(
        """
        CREATE TABLE canonical_axes (
            axis_id TEXT PRIMARY KEY,
            idempotency_key TEXT NOT NULL UNIQUE,
            canonical_name TEXT NOT NULL UNIQUE,
            commitment_json TEXT NOT NULL
                CHECK(json_valid(commitment_json)
                      AND json_type(commitment_json)='object'),
            commitment_sha256 TEXT NOT NULL
                CHECK(length(commitment_sha256)=64
                      AND commitment_sha256 NOT GLOB '*[^0-9a-f]*'),
            effective_at DATETIME NOT NULL,
            knowledge_at DATETIME NOT NULL,
            recorded_at DATETIME NOT NULL,
            CHECK(effective_at <= knowledge_at AND knowledge_at <= recorded_at)
        )
        """
    )
    _execute(
        """
        CREATE TABLE canonical_members (
            member_id TEXT PRIMARY KEY,
            idempotency_key TEXT NOT NULL UNIQUE,
            axis_id TEXT NOT NULL REFERENCES canonical_axes(axis_id),
            canonical_name TEXT NOT NULL,
            commitment_json TEXT NOT NULL
                CHECK(json_valid(commitment_json)
                      AND json_type(commitment_json)='object'),
            commitment_sha256 TEXT NOT NULL
                CHECK(length(commitment_sha256)=64
                      AND commitment_sha256 NOT GLOB '*[^0-9a-f]*'),
            effective_at DATETIME NOT NULL,
            knowledge_at DATETIME NOT NULL,
            recorded_at DATETIME NOT NULL,
            UNIQUE(axis_id, canonical_name),
            UNIQUE(axis_id, member_id),
            CHECK(effective_at <= knowledge_at AND knowledge_at <= recorded_at)
        )
        """
    )
    _execute(
        """
        CREATE TABLE source_taxonomy_components (
            component_id TEXT PRIMARY KEY,
            idempotency_key TEXT NOT NULL UNIQUE,
            component_kind TEXT NOT NULL
                CHECK(component_kind IN ('concept','axis','member')),
            taxonomy_namespace TEXT NOT NULL,
            local_name TEXT NOT NULL,
            taxonomy_name TEXT NOT NULL,
            taxonomy_version TEXT NOT NULL,
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
                   taxonomy_name, taxonomy_version, reporting_entity_scope_key),
            CHECK(reporting_entity_scope_key =
                  COALESCE(reporting_entity_id, '__global__')),
            CHECK(is_extension=0 OR reporting_entity_id IS NOT NULL),
            CHECK(effective_at <= knowledge_at AND knowledge_at <= recorded_at)
        )
        """
    )
    _execute(
        """
        CREATE TABLE source_observation_taxonomy_assertions (
            observation_id TEXT PRIMARY KEY
                REFERENCES fact_observations_v2(observation_id),
            idempotency_key TEXT NOT NULL UNIQUE,
            extraction_run_id TEXT NOT NULL
                REFERENCES evidence_extraction_runs(extraction_run_id),
            taxonomy_name TEXT NOT NULL,
            taxonomy_version TEXT NOT NULL,
            fact_cell_semantic_key_sha256 TEXT NOT NULL
                CHECK(length(fact_cell_semantic_key_sha256)=64
                      AND fact_cell_semantic_key_sha256
                          NOT GLOB '*[^0-9a-f]*'),
            anchor_payload_sha256 TEXT NOT NULL
                CHECK(length(anchor_payload_sha256)=64
                      AND anchor_payload_sha256 NOT GLOB '*[^0-9a-f]*'),
            observation_payload_sha256 TEXT NOT NULL
                CHECK(length(observation_payload_sha256)=64
                      AND observation_payload_sha256
                          NOT GLOB '*[^0-9a-f]*'),
            extraction_output_sha256 TEXT NOT NULL
                CHECK(length(extraction_output_sha256)=64
                      AND extraction_output_sha256
                          NOT GLOB '*[^0-9a-f]*'),
            raw_entry_sha256 TEXT NOT NULL
                CHECK(length(raw_entry_sha256)=64
                      AND raw_entry_sha256 NOT GLOB '*[^0-9a-f]*'),
            observation_set_sha256 TEXT NOT NULL
                CHECK(length(observation_set_sha256)=64
                      AND observation_set_sha256
                          NOT GLOB '*[^0-9a-f]*'),
            commitment_json TEXT NOT NULL
                CHECK(json_valid(commitment_json)
                      AND json_type(commitment_json)='object'),
            commitment_sha256 TEXT NOT NULL
                CHECK(length(commitment_sha256)=64
                      AND commitment_sha256 NOT GLOB '*[^0-9a-f]*'),
            knowledge_at DATETIME NOT NULL,
            recorded_at DATETIME NOT NULL,
            CHECK(knowledge_at <= recorded_at)
        )
        """
    )
    _execute(
        """
        CREATE TABLE canonical_metric_definition_revisions (
            metric_definition_revision_id TEXT PRIMARY KEY,
            idempotency_key TEXT NOT NULL UNIQUE,
            metric_id TEXT NOT NULL REFERENCES canonical_metrics(metric_id),
            revision INTEGER NOT NULL CHECK(revision > 0),
            supersedes_metric_definition_revision_id TEXT
                REFERENCES canonical_metric_definition_revisions(
                    metric_definition_revision_id),
            lifecycle TEXT NOT NULL
                CHECK(lifecycle IN ('active','deprecated','retired')),
            definition_text TEXT NOT NULL,
            aliases_json TEXT NOT NULL
                CHECK(json_valid(aliases_json) AND json_type(aliases_json)='array'),
            value_kind TEXT NOT NULL
                CHECK(value_kind IN ('numeric','text','nil')),
            period_kind TEXT NOT NULL
                CHECK(period_kind IN ('instant','duration')),
            unit_family TEXT NOT NULL,
            accounting_basis TEXT NOT NULL,
            scope_constraints_json TEXT NOT NULL
                CHECK(json_valid(scope_constraints_json)
                      AND json_type(scope_constraints_json)='object'),
            commitment_json TEXT NOT NULL
                CHECK(json_valid(commitment_json)
                      AND json_type(commitment_json)='object'),
            commitment_sha256 TEXT NOT NULL
                CHECK(length(commitment_sha256)=64
                      AND commitment_sha256 NOT GLOB '*[^0-9a-f]*'),
            effective_at DATETIME NOT NULL,
            knowledge_at DATETIME NOT NULL,
            recorded_at DATETIME NOT NULL,
            UNIQUE(metric_id, revision),
            CHECK((revision=1)=(supersedes_metric_definition_revision_id IS NULL)),
            CHECK(effective_at <= knowledge_at AND knowledge_at <= recorded_at)
        )
        """
    )
    _execute(
        """
        CREATE TABLE source_dimension_mapping_revisions (
            dimension_mapping_revision_id TEXT PRIMARY KEY,
            idempotency_key TEXT NOT NULL UNIQUE,
            source_component_id TEXT NOT NULL
                REFERENCES source_taxonomy_components(component_id),
            revision INTEGER NOT NULL CHECK(revision > 0),
            supersedes_dimension_mapping_revision_id TEXT
                REFERENCES source_dimension_mapping_revisions(
                    dimension_mapping_revision_id),
            disposition TEXT NOT NULL CHECK(disposition IN
                ('exact','equivalent','ambiguous','not_applicable','quarantined')),
            canonical_axis_id TEXT REFERENCES canonical_axes(axis_id),
            canonical_member_id TEXT,
            policy_name TEXT NOT NULL,
            policy_version TEXT NOT NULL,
            policy_config_sha256 TEXT NOT NULL
                CHECK(length(policy_config_sha256)=64
                      AND policy_config_sha256 NOT GLOB '*[^0-9a-f]*'),
            evidence_json TEXT NOT NULL
                CHECK(json_valid(evidence_json) AND json_type(evidence_json)='object'),
            evidence_sha256 TEXT NOT NULL
                CHECK(length(evidence_sha256)=64
                      AND evidence_sha256 NOT GLOB '*[^0-9a-f]*'),
            reviewer_identity TEXT,
            audited_policy_path TEXT,
            commitment_json TEXT NOT NULL
                CHECK(json_valid(commitment_json)
                      AND json_type(commitment_json)='object'),
            commitment_sha256 TEXT NOT NULL
                CHECK(length(commitment_sha256)=64
                      AND commitment_sha256 NOT GLOB '*[^0-9a-f]*'),
            effective_at DATETIME NOT NULL,
            knowledge_at DATETIME NOT NULL,
            recorded_at DATETIME NOT NULL,
            UNIQUE(source_component_id, revision),
            FOREIGN KEY(canonical_axis_id, canonical_member_id)
                REFERENCES canonical_members(axis_id, member_id),
            CHECK((revision=1)=(supersedes_dimension_mapping_revision_id IS NULL)),
            CHECK(
                (disposition IN ('exact','equivalent')
                 AND canonical_axis_id IS NOT NULL)
                OR
                (disposition IN ('ambiguous','not_applicable','quarantined')
                 AND canonical_axis_id IS NULL AND canonical_member_id IS NULL)
            ),
            CHECK(effective_at <= knowledge_at AND knowledge_at <= recorded_at)
        )
        """
    )
    _execute(
        """
        CREATE TABLE metric_mapping_revisions (
            mapping_revision_id TEXT PRIMARY KEY,
            idempotency_key TEXT NOT NULL UNIQUE,
            source_component_id TEXT NOT NULL
                REFERENCES source_taxonomy_components(component_id),
            revision INTEGER NOT NULL CHECK(revision > 0),
            supersedes_mapping_revision_id TEXT
                REFERENCES metric_mapping_revisions(mapping_revision_id),
            metric_id TEXT REFERENCES canonical_metrics(metric_id),
            disposition TEXT NOT NULL CHECK(disposition IN
                ('exact','equivalent','derived','ambiguous',
                 'not_applicable','quarantined')),
            policy_name TEXT NOT NULL,
            policy_version TEXT NOT NULL,
            policy_config_sha256 TEXT NOT NULL
                CHECK(length(policy_config_sha256)=64
                      AND policy_config_sha256 NOT GLOB '*[^0-9a-f]*'),
            method_name TEXT NOT NULL,
            method_version TEXT NOT NULL,
            constraints_json TEXT NOT NULL
                CHECK(json_valid(constraints_json)
                      AND json_type(constraints_json)='object'),
            evidence_json TEXT NOT NULL
                CHECK(json_valid(evidence_json)
                      AND json_type(evidence_json)='object'),
            evidence_sha256 TEXT NOT NULL
                CHECK(length(evidence_sha256)=64
                      AND evidence_sha256 NOT GLOB '*[^0-9a-f]*'),
            reviewer_identity TEXT,
            audited_policy_path TEXT,
            commitment_json TEXT NOT NULL
                CHECK(json_valid(commitment_json)
                      AND json_type(commitment_json)='object'),
            commitment_sha256 TEXT NOT NULL
                CHECK(length(commitment_sha256)=64
                      AND commitment_sha256 NOT GLOB '*[^0-9a-f]*'),
            effective_at DATETIME NOT NULL,
            knowledge_at DATETIME NOT NULL,
            recorded_at DATETIME NOT NULL,
            UNIQUE(source_component_id, revision),
            CHECK((revision=1)=(supersedes_mapping_revision_id IS NULL)),
            CHECK((disposition IN ('exact','equivalent','derived'))
                  =(metric_id IS NOT NULL)),
            CHECK(effective_at <= knowledge_at AND knowledge_at <= recorded_at)
        )
        """
    )
    _execute(
        """
        CREATE TABLE canonical_metric_cells (
            canonical_metric_cell_id TEXT PRIMARY KEY,
            idempotency_key TEXT NOT NULL UNIQUE,
            metric_id TEXT NOT NULL REFERENCES canonical_metrics(metric_id),
            reporting_entity_id TEXT NOT NULL REFERENCES reporting_entities(
                reporting_entity_id),
            scope_security_id TEXT REFERENCES securities(security_id),
            period_kind TEXT NOT NULL
                CHECK(period_kind IN ('instant','duration')),
            period_start DATETIME,
            period_end DATETIME NOT NULL,
            dimension_count INTEGER NOT NULL CHECK(dimension_count >= 0),
            unit_family TEXT NOT NULL,
            accounting_basis TEXT NOT NULL,
            consolidation_scope TEXT NOT NULL,
            effective_at DATETIME NOT NULL,
            knowledge_at DATETIME NOT NULL,
            recorded_at DATETIME NOT NULL,
            CHECK((period_kind='instant' AND period_start IS NULL)
                  OR (period_kind='duration' AND period_start IS NOT NULL
                      AND period_start <= period_end)),
            CHECK(effective_at <= knowledge_at AND knowledge_at <= recorded_at)
        )
        """
    )
    _execute(
        """
        CREATE TABLE canonical_metric_cell_dimensions (
            canonical_metric_cell_id TEXT NOT NULL
                REFERENCES canonical_metric_cells(canonical_metric_cell_id),
            dimension_ordinal INTEGER NOT NULL CHECK(dimension_ordinal >= 0),
            axis_id TEXT NOT NULL REFERENCES canonical_axes(axis_id),
            member_id TEXT NOT NULL,
            PRIMARY KEY(canonical_metric_cell_id, dimension_ordinal),
            UNIQUE(canonical_metric_cell_id, axis_id),
            FOREIGN KEY(axis_id, member_id)
                REFERENCES canonical_members(axis_id, member_id)
        )
        """
    )
    _execute(
        """
        CREATE TABLE canonical_metric_cell_seals (
            canonical_metric_cell_id TEXT PRIMARY KEY
                REFERENCES canonical_metric_cells(canonical_metric_cell_id),
            dimension_set_json TEXT NOT NULL
                CHECK(json_valid(dimension_set_json)
                      AND json_type(dimension_set_json)='array'),
            dimension_set_sha256 TEXT NOT NULL
                CHECK(length(dimension_set_sha256)=64
                      AND dimension_set_sha256 NOT GLOB '*[^0-9a-f]*'),
            semantic_identity_json TEXT NOT NULL
                CHECK(json_valid(semantic_identity_json)
                      AND json_type(semantic_identity_json)='object'),
            semantic_key_sha256 TEXT NOT NULL UNIQUE
                CHECK(length(semantic_key_sha256)=64
                      AND semantic_key_sha256 NOT GLOB '*[^0-9a-f]*'),
            sealed_at DATETIME NOT NULL
        )
        """
    )
    _execute(
        """
        CREATE TABLE fact_cell_canonical_binding_revisions (
            binding_revision_id TEXT PRIMARY KEY,
            idempotency_key TEXT NOT NULL UNIQUE,
            fact_cell_id TEXT NOT NULL REFERENCES fact_cells_v2(fact_cell_id),
            source_observation_id TEXT NOT NULL
                REFERENCES fact_observations_v2(observation_id),
            revision INTEGER NOT NULL CHECK(revision > 0),
            supersedes_binding_revision_id TEXT
                REFERENCES fact_cell_canonical_binding_revisions(
                    binding_revision_id),
            canonical_metric_cell_id TEXT
                REFERENCES canonical_metric_cells(canonical_metric_cell_id),
            mapping_revision_id TEXT
                REFERENCES metric_mapping_revisions(mapping_revision_id),
            source_component_id TEXT
                REFERENCES source_taxonomy_components(component_id),
            binding_status TEXT NOT NULL
                CHECK(binding_status IN ('bound','quarantined','retired')),
            reason_code TEXT,
            reason_details_json TEXT
                CHECK(reason_details_json IS NULL OR
                      (json_valid(reason_details_json)
                       AND json_type(reason_details_json)='object')),
            reason_details_sha256 TEXT
                CHECK(reason_details_sha256 IS NULL OR
                      (length(reason_details_sha256)=64
                       AND reason_details_sha256 NOT GLOB '*[^0-9a-f]*')),
            commitment_json TEXT NOT NULL
                CHECK(json_valid(commitment_json)
                      AND json_type(commitment_json)='object'),
            commitment_sha256 TEXT NOT NULL
                CHECK(length(commitment_sha256)=64
                      AND commitment_sha256 NOT GLOB '*[^0-9a-f]*'),
            effective_at DATETIME NOT NULL,
            knowledge_at DATETIME NOT NULL,
            recorded_at DATETIME NOT NULL,
            UNIQUE(source_observation_id, revision),
            CHECK((revision=1)=(supersedes_binding_revision_id IS NULL)),
            CHECK(
                (binding_status='bound'
                 AND canonical_metric_cell_id IS NOT NULL
                 AND mapping_revision_id IS NOT NULL
                 AND source_component_id IS NOT NULL
                 AND reason_code IS NULL
                 AND reason_details_json IS NULL
                 AND reason_details_sha256 IS NULL)
                OR
                (binding_status='quarantined'
                 AND canonical_metric_cell_id IS NULL
                 AND mapping_revision_id IS NULL
                 AND source_component_id IS NULL
                 AND reason_code IS NOT NULL
                 AND reason_details_json IS NOT NULL
                 AND reason_details_sha256 IS NOT NULL)
                OR
                (binding_status='retired'
                 AND canonical_metric_cell_id IS NOT NULL
                 AND mapping_revision_id IS NOT NULL
                 AND source_component_id IS NOT NULL
                 AND (
                    (reason_code IS NULL
                     AND reason_details_json IS NULL
                     AND reason_details_sha256 IS NULL)
                    OR
                    (reason_code IS NOT NULL
                     AND reason_details_json IS NOT NULL
                     AND reason_details_sha256 IS NOT NULL)
                 ))
            ),
            CHECK(effective_at <= knowledge_at AND knowledge_at <= recorded_at)
        )
        """
    )
    _execute(
        """
        CREATE TABLE ontology_snapshot_headers (
            ontology_snapshot_id TEXT PRIMARY KEY,
            idempotency_key TEXT NOT NULL UNIQUE,
            cutoff_at DATETIME NOT NULL,
            recorded_at DATETIME NOT NULL,
            CHECK(recorded_at >= cutoff_at)
        )
        """
    )
    _execute(
        """
        CREATE TABLE ontology_snapshot_members (
            ontology_snapshot_id TEXT NOT NULL
                REFERENCES ontology_snapshot_headers(ontology_snapshot_id),
            member_ordinal INTEGER NOT NULL CHECK(member_ordinal >= 0),
            member_kind TEXT NOT NULL,
            member_id TEXT NOT NULL,
            member_sha256 TEXT NOT NULL
                CHECK(length(member_sha256)=64
                      AND member_sha256 NOT GLOB '*[^0-9a-f]*'),
            PRIMARY KEY(ontology_snapshot_id, member_ordinal),
            UNIQUE(ontology_snapshot_id, member_kind, member_id)
        )
        """
    )
    _execute(
        """
        CREATE TABLE ontology_snapshot_seals (
            ontology_snapshot_id TEXT PRIMARY KEY
                REFERENCES ontology_snapshot_headers(ontology_snapshot_id),
            member_count INTEGER NOT NULL CHECK(member_count >= 0),
            canonical_member_set_json TEXT NOT NULL
                CHECK(json_valid(canonical_member_set_json)
                      AND json_type(canonical_member_set_json)='array'),
            member_set_sha256 TEXT NOT NULL
                CHECK(length(member_set_sha256)=64
                      AND member_set_sha256 NOT GLOB '*[^0-9a-f]*'),
            sealed_at DATETIME NOT NULL
        )
        """
    )

    for table in (
        "canonical_metrics",
        "canonical_axes",
        "canonical_members",
        "source_taxonomy_components",
        "source_observation_taxonomy_assertions",
        "canonical_metric_definition_revisions",
        "source_dimension_mapping_revisions",
        "metric_mapping_revisions",
        "fact_cell_canonical_binding_revisions",
    ):
        _commitment_exact(table)
    for table in _TABLES:
        _append_only(table)

    _execute(
        """
        CREATE TRIGGER trg_source_taxonomy_assertion_exact
        BEFORE INSERT ON source_observation_taxonomy_assertions
        WHEN NOT EXISTS (
            SELECT 1
            FROM fact_observations_v2 observation
            JOIN fact_cells_v2 cell
              ON cell.fact_cell_id=observation.fact_cell_id
            JOIN fact_cell_identity_seals_v2 cell_seal
              ON cell_seal.fact_cell_id=cell.fact_cell_id
            JOIN fact_reported_observation_anchors_v2 anchor
              ON anchor.observation_id=observation.observation_id
            JOIN fact_observation_payload_commitments_v2 payload
              ON payload.observation_id=observation.observation_id
            JOIN evidence_extraction_runs run
              ON run.extraction_run_id=anchor.extraction_run_id
            JOIN fact_extraction_run_completeness_seals_v2 completeness
              ON completeness.extraction_run_id=run.extraction_run_id
            WHERE observation.observation_id=NEW.observation_id
              AND observation.observation_kind='reported'
              AND run.extraction_run_id=NEW.extraction_run_id
              AND run.outcome='succeeded'
              AND cell.taxonomy_name=NEW.taxonomy_name
              AND anchor.source_taxonomy_version=NEW.taxonomy_version
              AND cell_seal.semantic_key_sha256=
                    NEW.fact_cell_semantic_key_sha256
              AND anchor.anchor_payload_sha256=NEW.anchor_payload_sha256
              AND payload.observation_payload_sha256=
                    NEW.observation_payload_sha256
              AND run.output_sha256=NEW.extraction_output_sha256
              AND anchor.extraction_output_sha256=
                    NEW.extraction_output_sha256
              AND completeness.extraction_output_sha256=
                    NEW.extraction_output_sha256
              AND anchor.raw_entry_sha256=NEW.raw_entry_sha256
              AND observation.source_entry_sha256=NEW.raw_entry_sha256
              AND completeness.observation_set_sha256=
                    NEW.observation_set_sha256
              AND EXISTS (
                    SELECT 1
                    FROM json_each(completeness.observation_set_json) member
                    WHERE member.value=NEW.observation_id)
              AND datetime(cell.recorded_at) <= datetime(NEW.knowledge_at)
              AND datetime(cell_seal.sealed_at) <= datetime(NEW.knowledge_at)
              AND datetime(observation.recorded_at)
                    <= datetime(NEW.knowledge_at)
              AND datetime(anchor.recorded_at) <= datetime(NEW.knowledge_at)
              AND datetime(payload.committed_at) <= datetime(NEW.knowledge_at)
              AND datetime(run.completed_at) <= datetime(NEW.knowledge_at)
              AND datetime(completeness.knowledge_at)
                    <= datetime(NEW.knowledge_at)
              AND datetime(completeness.recorded_at)
                    <= datetime(NEW.knowledge_at)
        )
        BEGIN
            SELECT RAISE(ABORT,
                'taxonomy assertion lacks exact committed fact evidence');
        END
        """
    )
    _execute(
        """
        CREATE TRIGGER trg_canonical_member_parent_clocks
        BEFORE INSERT ON canonical_members
        WHEN NOT EXISTS (
            SELECT 1 FROM canonical_axes axis
            WHERE axis.axis_id=NEW.axis_id
              AND datetime(axis.effective_at) <= datetime(NEW.effective_at)
              AND datetime(axis.knowledge_at) <= datetime(NEW.knowledge_at)
              AND datetime(axis.recorded_at) <= datetime(NEW.recorded_at))
        BEGIN
            SELECT RAISE(ABORT,
                'canonical member predates its axis registry record');
        END
        """
    )
    _execute(
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
    _execute(
        """
        CREATE TRIGGER trg_metric_definition_parent_clocks
        BEFORE INSERT ON canonical_metric_definition_revisions
        WHEN NOT EXISTS (
            SELECT 1 FROM canonical_metrics metric
            WHERE metric.metric_id=NEW.metric_id
              AND datetime(metric.effective_at) <= datetime(NEW.effective_at)
              AND datetime(metric.knowledge_at) <= datetime(NEW.knowledge_at)
              AND datetime(metric.recorded_at) <= datetime(NEW.recorded_at))
        BEGIN
            SELECT RAISE(ABORT,
                'metric definition predates its metric registry record');
        END
        """
    )
    _execute(
        """
        CREATE TRIGGER trg_dimension_mapping_parent_clocks
        BEFORE INSERT ON source_dimension_mapping_revisions
        WHEN NOT EXISTS (
            SELECT 1 FROM source_taxonomy_components source
            WHERE source.component_id=NEW.source_component_id
              AND datetime(source.effective_at) <= datetime(NEW.effective_at)
              AND datetime(source.knowledge_at) <= datetime(NEW.knowledge_at)
              AND datetime(source.recorded_at) <= datetime(NEW.recorded_at))
          OR (NEW.canonical_axis_id IS NOT NULL AND NOT EXISTS (
            SELECT 1 FROM canonical_axes axis
            WHERE axis.axis_id=NEW.canonical_axis_id
              AND datetime(axis.effective_at) <= datetime(NEW.effective_at)
              AND datetime(axis.knowledge_at) <= datetime(NEW.knowledge_at)
              AND datetime(axis.recorded_at) <= datetime(NEW.recorded_at)))
          OR (NEW.canonical_member_id IS NOT NULL AND NOT EXISTS (
            SELECT 1 FROM canonical_members member
            WHERE member.member_id=NEW.canonical_member_id
              AND datetime(member.effective_at) <= datetime(NEW.effective_at)
              AND datetime(member.knowledge_at) <= datetime(NEW.knowledge_at)
              AND datetime(member.recorded_at) <= datetime(NEW.recorded_at)))
        BEGIN
            SELECT RAISE(ABORT,
                'dimension mapping predates a source or canonical registry record');
        END
        """
    )
    _execute(
        """
        CREATE TRIGGER trg_metric_mapping_parent_clocks
        BEFORE INSERT ON metric_mapping_revisions
        WHEN NOT EXISTS (
            SELECT 1 FROM source_taxonomy_components source
            WHERE source.component_id=NEW.source_component_id
              AND datetime(source.effective_at) <= datetime(NEW.effective_at)
              AND datetime(source.knowledge_at) <= datetime(NEW.knowledge_at)
              AND datetime(source.recorded_at) <= datetime(NEW.recorded_at))
          OR (NEW.metric_id IS NOT NULL AND NOT EXISTS (
            SELECT 1 FROM canonical_metrics metric
            WHERE metric.metric_id=NEW.metric_id
              AND datetime(metric.effective_at) <= datetime(NEW.effective_at)
              AND datetime(metric.knowledge_at) <= datetime(NEW.knowledge_at)
              AND datetime(metric.recorded_at) <= datetime(NEW.recorded_at)))
        BEGIN
            SELECT RAISE(ABORT,
                'metric mapping predates a source or canonical registry record');
        END
        """
    )
    _execute(
        """
        CREATE TRIGGER trg_canonical_cell_parent_clocks
        BEFORE INSERT ON canonical_metric_cells
        WHEN NOT EXISTS (
            SELECT 1 FROM canonical_metrics metric
            WHERE metric.metric_id=NEW.metric_id
              AND datetime(metric.effective_at) <= datetime(NEW.effective_at)
              AND datetime(metric.knowledge_at) <= datetime(NEW.knowledge_at)
              AND datetime(metric.recorded_at) <= datetime(NEW.recorded_at))
          OR NOT EXISTS (
            SELECT 1 FROM reporting_entities entity
            WHERE entity.reporting_entity_id=NEW.reporting_entity_id
              AND datetime(entity.created_at) <= datetime(NEW.effective_at)
              AND datetime(entity.created_at) <= datetime(NEW.knowledge_at)
              AND datetime(entity.created_at) <= datetime(NEW.recorded_at))
          OR (NEW.scope_security_id IS NOT NULL AND NOT EXISTS (
            SELECT 1 FROM securities security
            WHERE security.security_id=NEW.scope_security_id
              AND datetime(security.created_at) <= datetime(NEW.effective_at)
              AND datetime(security.created_at) <= datetime(NEW.knowledge_at)
              AND datetime(security.created_at) <= datetime(NEW.recorded_at)))
        BEGIN
            SELECT RAISE(ABORT,
                'canonical cell predates a parent registry record');
        END
        """
    )

    _sequential_revision(
        "canonical_metric_definition_revisions",
        coordinate="metric_id",
        id_column="metric_definition_revision_id",
        supersedes_column="supersedes_metric_definition_revision_id",
    )
    _sequential_revision(
        "source_dimension_mapping_revisions",
        coordinate="source_component_id",
        id_column="dimension_mapping_revision_id",
        supersedes_column="supersedes_dimension_mapping_revision_id",
    )
    _sequential_revision(
        "metric_mapping_revisions",
        coordinate="source_component_id",
        id_column="mapping_revision_id",
        supersedes_column="supersedes_mapping_revision_id",
    )
    _sequential_revision(
        "fact_cell_canonical_binding_revisions",
        coordinate="source_observation_id",
        id_column="binding_revision_id",
        supersedes_column="supersedes_binding_revision_id",
    )
    _execute(
        """
        CREATE TRIGGER trg_binding_reason_details_exact
        BEFORE INSERT ON fact_cell_canonical_binding_revisions
        WHEN NEW.reason_details_json IS NOT NULL
         AND NEW.reason_details_sha256 <> fact_sha256(NEW.reason_details_json)
        BEGIN
            SELECT RAISE(ABORT, 'binding reason details mismatch');
        END
        """
    )
    _execute(
        """
        CREATE TRIGGER trg_binding_observation_coordinate
        BEFORE INSERT ON fact_cell_canonical_binding_revisions
        WHEN NOT EXISTS (
            SELECT 1 FROM fact_observations_v2 observation
            WHERE observation.observation_id=NEW.source_observation_id
              AND observation.fact_cell_id=NEW.fact_cell_id)
        BEGIN
            SELECT RAISE(ABORT,
                'binding observation does not belong to its fact cell');
        END
        """
    )
    _execute(
        """
        CREATE TRIGGER trg_binding_derived_quarantine
        BEFORE INSERT ON fact_cell_canonical_binding_revisions
        WHEN NEW.binding_status='quarantined'
         AND NOT EXISTS (
            SELECT 1 FROM fact_observations_v2 observation
            WHERE observation.observation_id=NEW.source_observation_id
              AND observation.fact_cell_id=NEW.fact_cell_id
              AND observation.observation_kind='derived')
        BEGIN
            SELECT RAISE(ABORT,
                'only derived observations use terminal ontology quarantine');
        END
        """
    )
    _execute(
        """
        CREATE TRIGGER trg_binding_quarantine_terminal
        BEFORE INSERT ON fact_cell_canonical_binding_revisions
        WHEN NEW.supersedes_binding_revision_id IS NOT NULL
         AND EXISTS (
            SELECT 1 FROM fact_cell_canonical_binding_revisions parent
            WHERE parent.binding_revision_id=NEW.supersedes_binding_revision_id
              AND parent.binding_status='quarantined')
        BEGIN
            SELECT RAISE(ABORT, 'quarantined binding revisions are terminal');
        END
        """
    )
    _execute(
        """
        CREATE TRIGGER trg_metric_mapping_extension_review
        BEFORE INSERT ON metric_mapping_revisions
        WHEN NEW.disposition IN ('exact','equivalent')
         AND EXISTS (
            SELECT 1 FROM source_taxonomy_components source
            WHERE source.component_id=NEW.source_component_id
              AND source.is_extension=1)
         AND NEW.reviewer_identity IS NULL
         AND NEW.audited_policy_path IS NULL
        BEGIN
            SELECT RAISE(ABORT,
                'extension mapping requires review or audited policy');
        END
        """
    )
    _execute(
        """
        CREATE TRIGGER trg_dimension_mapping_extension_review
        BEFORE INSERT ON source_dimension_mapping_revisions
        WHEN NEW.disposition IN ('exact','equivalent')
         AND EXISTS (
            SELECT 1 FROM source_taxonomy_components source
            WHERE source.component_id=NEW.source_component_id
              AND source.is_extension=1)
         AND NEW.reviewer_identity IS NULL
         AND NEW.audited_policy_path IS NULL
        BEGIN
            SELECT RAISE(ABORT,
                'extension dimension mapping requires review or audited policy');
        END
        """
    )
    for table in (
        "metric_mapping_revisions",
        "source_dimension_mapping_revisions",
    ):
        _execute(
            f"CREATE TRIGGER trg_{table}_evidence_exact BEFORE INSERT ON {table} "
            "WHEN NEW.evidence_sha256 <> fact_sha256(NEW.evidence_json) "
            f"BEGIN SELECT RAISE(ABORT, '{table} evidence mismatch'); END"
        )

    _execute(
        """
        CREATE TRIGGER trg_canonical_cell_dimension_unsealed
        BEFORE INSERT ON canonical_metric_cell_dimensions
        WHEN EXISTS (
            SELECT 1 FROM canonical_metric_cell_seals seal
            WHERE seal.canonical_metric_cell_id=NEW.canonical_metric_cell_id)
        BEGIN
            SELECT RAISE(ABORT, 'canonical metric cell is sealed');
        END
        """
    )
    _execute(
        """
        CREATE TRIGGER trg_canonical_cell_seal_exact
        BEFORE INSERT ON canonical_metric_cell_seals
        WHEN
            datetime(NEW.sealed_at) < datetime((
                SELECT recorded_at FROM canonical_metric_cells
                WHERE canonical_metric_cell_id=NEW.canonical_metric_cell_id))
            OR EXISTS (
                SELECT 1
                FROM canonical_metric_cell_dimensions dimension
                JOIN canonical_metric_cells cell
                  ON cell.canonical_metric_cell_id=
                     dimension.canonical_metric_cell_id
                JOIN canonical_axes axis ON axis.axis_id=dimension.axis_id
                JOIN canonical_members member
                  ON member.axis_id=dimension.axis_id
                 AND member.member_id=dimension.member_id
                WHERE dimension.canonical_metric_cell_id=
                      NEW.canonical_metric_cell_id
                  AND (
                    datetime(axis.effective_at) > datetime(cell.effective_at)
                    OR datetime(axis.knowledge_at) > datetime(cell.knowledge_at)
                    OR datetime(axis.recorded_at) > datetime(cell.recorded_at)
                    OR datetime(member.effective_at)
                        > datetime(cell.effective_at)
                    OR datetime(member.knowledge_at)
                        > datetime(cell.knowledge_at)
                    OR datetime(member.recorded_at)
                        > datetime(cell.recorded_at)))
            OR NEW.dimension_set_sha256 <> fact_sha256(NEW.dimension_set_json)
            OR NEW.semantic_key_sha256 <> fact_sha256(NEW.semantic_identity_json)
            OR json_type(NEW.semantic_identity_json, '$.concept_namespace')
                IS NOT NULL
            OR json_type(NEW.semantic_identity_json, '$.concept_name')
                IS NOT NULL
            OR json_type(NEW.semantic_identity_json, '$.taxonomy_name')
                IS NOT NULL
            OR json_type(NEW.semantic_identity_json, '$.taxonomy_version')
                IS NOT NULL
            OR json_extract(NEW.semantic_identity_json, '$.metric_id') <> (
                SELECT metric_id FROM canonical_metric_cells
                WHERE canonical_metric_cell_id=NEW.canonical_metric_cell_id)
            OR json_extract(
                NEW.semantic_identity_json, '$.reporting_entity_id') <> (
                SELECT reporting_entity_id FROM canonical_metric_cells
                WHERE canonical_metric_cell_id=NEW.canonical_metric_cell_id)
            OR json_extract(
                NEW.semantic_identity_json, '$.scope_security_id') IS NOT (
                SELECT scope_security_id FROM canonical_metric_cells
                WHERE canonical_metric_cell_id=NEW.canonical_metric_cell_id)
            OR json_extract(NEW.semantic_identity_json, '$.period_kind') <> (
                SELECT period_kind FROM canonical_metric_cells
                WHERE canonical_metric_cell_id=NEW.canonical_metric_cell_id)
            OR datetime(json_extract(
                NEW.semantic_identity_json, '$.period_start')) IS NOT datetime((
                SELECT period_start FROM canonical_metric_cells
                WHERE canonical_metric_cell_id=NEW.canonical_metric_cell_id))
            OR datetime(json_extract(
                NEW.semantic_identity_json, '$.period_end')) <> datetime((
                SELECT period_end FROM canonical_metric_cells
                WHERE canonical_metric_cell_id=NEW.canonical_metric_cell_id))
            OR json_extract(NEW.semantic_identity_json, '$.unit_family') <> (
                SELECT unit_family FROM canonical_metric_cells
                WHERE canonical_metric_cell_id=NEW.canonical_metric_cell_id)
            OR json_extract(
                NEW.semantic_identity_json, '$.accounting_basis') <> (
                SELECT accounting_basis FROM canonical_metric_cells
                WHERE canonical_metric_cell_id=NEW.canonical_metric_cell_id)
            OR json_extract(
                NEW.semantic_identity_json, '$.consolidation_scope') <> (
                SELECT consolidation_scope FROM canonical_metric_cells
                WHERE canonical_metric_cell_id=NEW.canonical_metric_cell_id)
            OR json_extract(
                NEW.semantic_identity_json, '$.canonical_dimensions')
                <> json(NEW.dimension_set_json)
            OR (SELECT dimension_count FROM canonical_metric_cells
                WHERE canonical_metric_cell_id=NEW.canonical_metric_cell_id)
               <> (SELECT COUNT(*) FROM canonical_metric_cell_dimensions
                   WHERE canonical_metric_cell_id=NEW.canonical_metric_cell_id)
            OR NEW.dimension_set_json <> COALESCE((
                SELECT json_group_array(json(item)) FROM (
                    SELECT json_object(
                        'axis_id', axis_id,
                        'member_id', member_id
                    ) AS item
                    FROM canonical_metric_cell_dimensions
                    WHERE canonical_metric_cell_id=NEW.canonical_metric_cell_id
                    ORDER BY dimension_ordinal
                )
            ), '[]')
        BEGIN
            SELECT RAISE(ABORT, 'canonical metric cell seal mismatch');
        END
        """
    )
    _execute(
        """
        CREATE TRIGGER trg_binding_exact_coordinate
        BEFORE INSERT ON fact_cell_canonical_binding_revisions
        WHEN NEW.binding_status='bound' AND NOT EXISTS (
            SELECT 1
            FROM fact_cells_v2 source_cell
            JOIN fact_observations_v2 observation
              ON observation.observation_id=NEW.source_observation_id
             AND observation.fact_cell_id=source_cell.fact_cell_id
             AND observation.observation_kind='reported'
            JOIN fact_reported_observation_anchors_v2 anchor
              ON anchor.observation_id=observation.observation_id
            JOIN source_observation_taxonomy_assertions taxonomy
              ON taxonomy.observation_id=observation.observation_id
             AND taxonomy.taxonomy_version=anchor.source_taxonomy_version
            JOIN source_taxonomy_components source
              ON source.component_id=NEW.source_component_id
             AND source.component_kind='concept'
             AND source.taxonomy_namespace=source_cell.concept_namespace
             AND source.local_name=source_cell.concept_name
             AND source.taxonomy_version=anchor.source_taxonomy_version
             AND source.taxonomy_name=taxonomy.taxonomy_name
             AND source.reporting_entity_scope_key IN (
                    '__global__', source_cell.reporting_entity_id)
            JOIN metric_mapping_revisions mapping
              ON mapping.mapping_revision_id=NEW.mapping_revision_id
             AND mapping.source_component_id=source.component_id
             AND mapping.disposition IN ('exact','equivalent','derived')
            JOIN canonical_metric_cells target
              ON target.canonical_metric_cell_id=NEW.canonical_metric_cell_id
             AND target.metric_id=mapping.metric_id
             AND target.reporting_entity_id=source_cell.reporting_entity_id
             AND target.scope_security_id IS source_cell.scope_security_id
             AND target.period_kind=source_cell.period_kind
             AND datetime(target.period_start) IS datetime(source_cell.period_start)
             AND datetime(target.period_end)=datetime(source_cell.period_end)
             AND target.accounting_basis=source_cell.accounting_basis
             AND target.consolidation_scope=source_cell.consolidation_scope
             AND target.unit_family=CASE
                    WHEN source_cell.currency IS NOT NULL THEN 'currency'
                    WHEN lower(source_cell.unit_key) IN ('pure','shares')
                        THEN lower(source_cell.unit_key)
                    ELSE source_cell.unit_key END
            JOIN canonical_metric_cell_seals target_seal
              ON target_seal.canonical_metric_cell_id=target.canonical_metric_cell_id
            WHERE source_cell.fact_cell_id=NEW.fact_cell_id
              AND datetime(mapping.effective_at) <= datetime(NEW.effective_at)
              AND datetime(mapping.knowledge_at) <= datetime(NEW.knowledge_at)
              AND datetime(mapping.recorded_at) <= datetime(NEW.recorded_at)
              AND datetime(taxonomy.knowledge_at) <= datetime(NEW.knowledge_at)
              AND datetime(taxonomy.recorded_at) <= datetime(NEW.recorded_at)
              AND datetime(source.effective_at) <= datetime(NEW.effective_at)
              AND datetime(source.knowledge_at) <= datetime(NEW.knowledge_at)
              AND datetime(source.recorded_at) <= datetime(NEW.recorded_at)
              AND datetime(source_cell.effective_at) <= datetime(NEW.effective_at)
              AND datetime(source_cell.knowledge_at) <= datetime(NEW.knowledge_at)
              AND datetime(source_cell.recorded_at) <= datetime(NEW.recorded_at)
              AND datetime(target.effective_at) <= datetime(NEW.effective_at)
              AND datetime(target.knowledge_at) <= datetime(NEW.knowledge_at)
              AND datetime(target.recorded_at) <= datetime(NEW.recorded_at)
              AND datetime(target_seal.sealed_at) <= datetime(NEW.recorded_at)
              AND target.dimension_count = (
                    SELECT COUNT(*) FROM fact_dimensions_normalized_v2 dim
                    WHERE dim.fact_cell_id=source_cell.fact_cell_id)
              AND NOT EXISTS (
                SELECT 1
                FROM fact_dimensions_normalized_v2 dim
                WHERE dim.fact_cell_id=source_cell.fact_cell_id
                  AND (
                    dim.member_kind <> 'explicit'
                    OR NOT EXISTS (
                        SELECT 1
                        FROM source_taxonomy_components source_axis
                        JOIN source_dimension_mapping_revisions axis_map
                          ON axis_map.source_component_id=source_axis.component_id
                        JOIN source_taxonomy_components source_member
                          ON source_member.component_kind='member'
                         AND source_member.taxonomy_namespace=
                             dim.explicit_member_namespace
                         AND source_member.local_name=dim.explicit_member_name
                         AND source_member.taxonomy_version=
                             anchor.source_taxonomy_version
                         AND source_member.reporting_entity_scope_key IN (
                             '__global__', source_cell.reporting_entity_id)
                        JOIN source_dimension_mapping_revisions member_map
                          ON member_map.source_component_id=
                             source_member.component_id
                        JOIN canonical_metric_cell_dimensions target_dim
                          ON target_dim.canonical_metric_cell_id=
                             target.canonical_metric_cell_id
                         AND target_dim.axis_id=axis_map.canonical_axis_id
                         AND target_dim.member_id=member_map.canonical_member_id
                        WHERE source_axis.component_kind='axis'
                          AND source_axis.taxonomy_namespace=dim.axis_namespace
                          AND source_axis.local_name=dim.axis_name
                          AND source_axis.taxonomy_version=
                              anchor.source_taxonomy_version
                          AND source_axis.reporting_entity_scope_key IN (
                              '__global__', source_cell.reporting_entity_id)
                          AND axis_map.disposition IN ('exact','equivalent')
                          AND member_map.disposition IN ('exact','equivalent')
                          AND axis_map.revision=(
                              SELECT MAX(candidate.revision)
                              FROM source_dimension_mapping_revisions candidate
                              WHERE candidate.source_component_id=
                                    source_axis.component_id
                                AND datetime(candidate.effective_at)
                                    <= datetime(NEW.effective_at)
                                AND datetime(candidate.knowledge_at)
                                    <= datetime(NEW.knowledge_at)
                                AND datetime(candidate.recorded_at)
                                    <= datetime(NEW.recorded_at))
                          AND member_map.revision=(
                              SELECT MAX(candidate.revision)
                              FROM source_dimension_mapping_revisions candidate
                              WHERE candidate.source_component_id=
                                    source_member.component_id
                                AND datetime(candidate.effective_at)
                                    <= datetime(NEW.effective_at)
                                AND datetime(candidate.knowledge_at)
                                    <= datetime(NEW.knowledge_at)
                                AND datetime(candidate.recorded_at)
                                    <= datetime(NEW.recorded_at))
                    )
                  )
              )
        )
        BEGIN
            SELECT RAISE(ABORT,
                'binding source assertion is incompatible with canonical cell');
        END
        """
    )

    _execute(
        """
        CREATE VIEW v_ontology_snapshot_expected_members AS
        SELECT header.ontology_snapshot_id, 'canonical_metric' AS member_kind,
               record.metric_id AS member_id,
               record.commitment_sha256 AS member_sha256
        FROM ontology_snapshot_headers header
        JOIN canonical_metrics record
          ON record.effective_at <= header.cutoff_at
         AND record.knowledge_at <= header.cutoff_at
         AND record.recorded_at <= header.cutoff_at
        UNION ALL
        SELECT header.ontology_snapshot_id, 'canonical_axis',
               record.axis_id, record.commitment_sha256
        FROM ontology_snapshot_headers header
        JOIN canonical_axes record
          ON record.effective_at <= header.cutoff_at
         AND record.knowledge_at <= header.cutoff_at
         AND record.recorded_at <= header.cutoff_at
        UNION ALL
        SELECT header.ontology_snapshot_id, 'canonical_member',
               record.member_id, record.commitment_sha256
        FROM ontology_snapshot_headers header
        JOIN canonical_members record
          ON record.effective_at <= header.cutoff_at
         AND record.knowledge_at <= header.cutoff_at
         AND record.recorded_at <= header.cutoff_at
        UNION ALL
        SELECT header.ontology_snapshot_id, 'source_component',
               record.component_id, record.commitment_sha256
        FROM ontology_snapshot_headers header
        JOIN source_taxonomy_components record
          ON record.effective_at <= header.cutoff_at
         AND record.knowledge_at <= header.cutoff_at
         AND record.recorded_at <= header.cutoff_at
        UNION ALL
        SELECT header.ontology_snapshot_id, 'source_taxonomy_assertion',
               record.observation_id, record.commitment_sha256
        FROM ontology_snapshot_headers header
        JOIN source_observation_taxonomy_assertions record
          ON datetime(record.knowledge_at) <= datetime(header.cutoff_at)
         AND datetime(record.recorded_at) <= datetime(header.cutoff_at)
        UNION ALL
        SELECT header.ontology_snapshot_id, 'metric_definition',
               record.metric_definition_revision_id, record.commitment_sha256
        FROM ontology_snapshot_headers header
        JOIN canonical_metric_definition_revisions record
          ON record.effective_at <= header.cutoff_at
         AND record.knowledge_at <= header.cutoff_at
         AND record.recorded_at <= header.cutoff_at
         AND NOT EXISTS (
            SELECT 1 FROM canonical_metric_definition_revisions newer
            WHERE newer.metric_id=record.metric_id
              AND newer.revision > record.revision
              AND newer.effective_at <= header.cutoff_at
              AND newer.knowledge_at <= header.cutoff_at
              AND newer.recorded_at <= header.cutoff_at)
        UNION ALL
        SELECT header.ontology_snapshot_id, 'dimension_mapping',
               record.dimension_mapping_revision_id, record.commitment_sha256
        FROM ontology_snapshot_headers header
        JOIN source_dimension_mapping_revisions record
          ON record.effective_at <= header.cutoff_at
         AND record.knowledge_at <= header.cutoff_at
         AND record.recorded_at <= header.cutoff_at
         AND NOT EXISTS (
            SELECT 1 FROM source_dimension_mapping_revisions newer
            WHERE newer.source_component_id=record.source_component_id
              AND newer.revision > record.revision
              AND newer.effective_at <= header.cutoff_at
              AND newer.knowledge_at <= header.cutoff_at
              AND newer.recorded_at <= header.cutoff_at)
        UNION ALL
        SELECT header.ontology_snapshot_id, 'metric_mapping',
               record.mapping_revision_id, record.commitment_sha256
        FROM ontology_snapshot_headers header
        JOIN metric_mapping_revisions record
          ON record.effective_at <= header.cutoff_at
         AND record.knowledge_at <= header.cutoff_at
         AND record.recorded_at <= header.cutoff_at
         AND NOT EXISTS (
            SELECT 1 FROM metric_mapping_revisions newer
            WHERE newer.source_component_id=record.source_component_id
              AND newer.revision > record.revision
              AND newer.effective_at <= header.cutoff_at
              AND newer.knowledge_at <= header.cutoff_at
              AND newer.recorded_at <= header.cutoff_at)
        UNION ALL
        SELECT header.ontology_snapshot_id, 'canonical_cell',
               seal.canonical_metric_cell_id, seal.semantic_key_sha256
        FROM ontology_snapshot_headers header
        JOIN canonical_metric_cell_seals seal
        JOIN canonical_metric_cells cell
          ON cell.canonical_metric_cell_id=seal.canonical_metric_cell_id
         AND cell.effective_at <= header.cutoff_at
         AND cell.knowledge_at <= header.cutoff_at
         AND cell.recorded_at <= header.cutoff_at
         AND seal.sealed_at <= header.cutoff_at
        UNION ALL
        SELECT header.ontology_snapshot_id, 'binding',
               record.binding_revision_id, record.commitment_sha256
        FROM ontology_snapshot_headers header
        JOIN fact_cell_canonical_binding_revisions record
          ON record.effective_at <= header.cutoff_at
         AND record.knowledge_at <= header.cutoff_at
         AND record.recorded_at <= header.cutoff_at
         AND NOT EXISTS (
            SELECT 1 FROM fact_cell_canonical_binding_revisions newer
            WHERE newer.source_observation_id=record.source_observation_id
              AND newer.revision > record.revision
              AND newer.effective_at <= header.cutoff_at
              AND newer.knowledge_at <= header.cutoff_at
              AND newer.recorded_at <= header.cutoff_at)
        """
    )
    _execute(
        """
        CREATE TRIGGER trg_ontology_snapshot_member_unsealed
        BEFORE INSERT ON ontology_snapshot_members
        WHEN EXISTS (
            SELECT 1 FROM ontology_snapshot_seals seal
            WHERE seal.ontology_snapshot_id=NEW.ontology_snapshot_id)
        BEGIN
            SELECT RAISE(ABORT, 'ontology snapshot is sealed');
        END
        """
    )
    _execute(
        """
        CREATE TRIGGER trg_ontology_snapshot_seal_exact
        BEFORE INSERT ON ontology_snapshot_seals
        WHEN
            NEW.member_set_sha256 <> fact_sha256(NEW.canonical_member_set_json)
            OR NEW.member_count <> (
                SELECT COUNT(*) FROM ontology_snapshot_members member
                WHERE member.ontology_snapshot_id=NEW.ontology_snapshot_id)
            OR (NEW.member_count > 0 AND (
                (SELECT MIN(member_ordinal) FROM ontology_snapshot_members
                 WHERE ontology_snapshot_id=NEW.ontology_snapshot_id) <> 0
                OR
                (SELECT MAX(member_ordinal) FROM ontology_snapshot_members
                 WHERE ontology_snapshot_id=NEW.ontology_snapshot_id)
                    <> NEW.member_count-1))
            OR NEW.canonical_member_set_json <> COALESCE((
                SELECT json_group_array(json(item)) FROM (
                    SELECT json_object(
                        'id', member_id,
                        'kind', member_kind,
                        'sha256', member_sha256
                    ) AS item
                    FROM ontology_snapshot_members
                    WHERE ontology_snapshot_id=NEW.ontology_snapshot_id
                    ORDER BY member_ordinal
                )
            ), '[]')
            OR EXISTS (
                SELECT member_kind, member_id, member_sha256
                FROM v_ontology_snapshot_expected_members
                WHERE ontology_snapshot_id=NEW.ontology_snapshot_id
                EXCEPT
                SELECT member_kind, member_id, member_sha256
                FROM ontology_snapshot_members
                WHERE ontology_snapshot_id=NEW.ontology_snapshot_id)
            OR EXISTS (
                SELECT member_kind, member_id, member_sha256
                FROM ontology_snapshot_members
                WHERE ontology_snapshot_id=NEW.ontology_snapshot_id
                EXCEPT
                SELECT member_kind, member_id, member_sha256
                FROM v_ontology_snapshot_expected_members
                WHERE ontology_snapshot_id=NEW.ontology_snapshot_id)
        BEGIN
            SELECT RAISE(ABORT, 'ontology snapshot seal mismatch');
        END
        """
    )
    op.create_index(
        "ix_metric_definition_as_known",
        "canonical_metric_definition_revisions",
        ["metric_id", "knowledge_at", "recorded_at", "revision"],
    )
    op.create_index(
        "ix_metric_mapping_as_known",
        "metric_mapping_revisions",
        ["source_component_id", "knowledge_at", "recorded_at", "revision"],
    )
    op.create_index(
        "ix_binding_as_known",
        "fact_cell_canonical_binding_revisions",
        ["source_observation_id", "knowledge_at", "recorded_at", "revision"],
    )
    op.create_index(
        "ix_bindings_for_fact_cell_as_known",
        "fact_cell_canonical_binding_revisions",
        ["fact_cell_id", "knowledge_at", "recorded_at", "source_observation_id"],
    )


def downgrade() -> None:
    op.execute("DROP VIEW IF EXISTS v_ontology_snapshot_expected_members")
    for table in reversed(_TABLES):
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_commitment_exact")
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_sequential")
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_evidence_exact")
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_update_append_only")
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_delete_append_only")
    for trigger in (
        "trg_ontology_snapshot_seal_exact",
        "trg_ontology_snapshot_member_unsealed",
        "trg_binding_exact_coordinate",
        "trg_binding_quarantine_terminal",
        "trg_binding_derived_quarantine",
        "trg_binding_observation_coordinate",
        "trg_binding_reason_details_exact",
        "trg_canonical_cell_parent_clocks",
        "trg_metric_mapping_parent_clocks",
        "trg_dimension_mapping_parent_clocks",
        "trg_metric_definition_parent_clocks",
        "trg_source_component_parent_clocks",
        "trg_canonical_member_parent_clocks",
        "trg_source_taxonomy_assertion_exact",
        "trg_canonical_cell_seal_exact",
        "trg_canonical_cell_dimension_unsealed",
        "trg_dimension_mapping_extension_review",
        "trg_metric_mapping_extension_review",
    ):
        op.execute(f"DROP TRIGGER IF EXISTS {trigger}")
    for table in reversed(_TABLES):
        op.drop_table(table)
