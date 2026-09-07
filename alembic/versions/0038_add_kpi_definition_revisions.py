"""Add effective-dated issuer KPI definition and comparability revisions.

Revision ID: 0038_add_kpi_definition_revisions
Revises: 0037_commitment_scan_segment_coverage
"""

from __future__ import annotations

from alembic import op

revision = "0038_add_kpi_definition_revisions"
down_revision = "0037_commitment_scan_segment_coverage"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE kpi_definition_revisions (
            kpi_definition_revision_id TEXT PRIMARY KEY,
            idempotency_key TEXT NOT NULL UNIQUE,
            kpi_definition_id INTEGER NOT NULL REFERENCES kpi_definitions(id),
            reporting_entity_id TEXT NOT NULL
                REFERENCES reporting_entities(reporting_entity_id),
            scope_security_id TEXT REFERENCES securities(security_id),
            revision INTEGER NOT NULL CHECK(revision > 0),
            supersedes_definition_revision_id TEXT UNIQUE
                REFERENCES kpi_definition_revisions(kpi_definition_revision_id),
            status TEXT NOT NULL CHECK(status IN ('admitted','quarantined')),
            lifecycle TEXT NOT NULL CHECK(lifecycle IN ('active','discontinued')),
            reported_label TEXT NOT NULL CHECK(length(trim(reported_label)) > 0),
            reported_definition_text TEXT,
            definition_text_status TEXT NOT NULL
                CHECK(definition_text_status IN ('verbatim','not_stated')),
            period_kind TEXT NOT NULL CHECK(period_kind IN ('instant','duration','unknown')),
            stock_flow_behavior TEXT NOT NULL
                CHECK(stock_flow_behavior IN ('stock','flow','ratio','other','unknown')),
            unit_family TEXT NOT NULL
                CHECK(unit_family IN
                    ('currency','count','percentage','ratio','basis_points','unknown')),
            unit_key TEXT NOT NULL
                CHECK(unit_key IN
                    ('actual','thousands','millions','billions','percent','ratio','bps','count','unknown')),
            unit_scale TEXT NOT NULL
                CHECK(unit_scale IN ('none','thousands','millions','billions','unknown')),
            currency_disposition TEXT NOT NULL
                CHECK(currency_disposition IN ('explicit','not_applicable','unknown')),
            currency TEXT CHECK(currency IS NULL OR currency IN
                ('USD','EUR','GBP','DKK','BRL','CAD','INR','AUD','KRW','JPY','CHF','TWD')),
            accounting_basis TEXT NOT NULL
                CHECK(accounting_basis IN ('gaap','non_gaap','management','unknown')),
            consolidation_scope TEXT NOT NULL
                CHECK(consolidation_scope IN
                    ('consolidated','geography','segment','product','other','unknown')),
            dimensions_json TEXT NOT NULL
                CHECK(json_valid(dimensions_json) AND json_type(dimensions_json)='object'),
            source_document_version_id TEXT NOT NULL
                REFERENCES evidence_document_versions(document_version_id),
            source_evidence_node_id TEXT NOT NULL REFERENCES evidence_nodes(node_id),
            source_locator_json TEXT NOT NULL
                CHECK(json_valid(source_locator_json)
                  AND json_type(source_locator_json)='object'
                  AND json(source_locator_json)<>'{}'),
            source_locator_sha256 TEXT NOT NULL
                CHECK(length(source_locator_sha256)=64
                  AND source_locator_sha256 NOT GLOB '*[^0-9a-f]*'),
            reason_code TEXT,
            reviewed_by TEXT NOT NULL CHECK(length(trim(reviewed_by)) > 0),
            commitment_json TEXT NOT NULL
                CHECK(json_valid(commitment_json) AND json_type(commitment_json)='object'),
            commitment_sha256 TEXT NOT NULL
                CHECK(length(commitment_sha256)=64
                  AND commitment_sha256 NOT GLOB '*[^0-9a-f]*'),
            effective_at TEXT NOT NULL CHECK(datetime(effective_at) IS NOT NULL),
            knowledge_at TEXT NOT NULL CHECK(datetime(knowledge_at) IS NOT NULL),
            recorded_at TEXT NOT NULL CHECK(datetime(recorded_at) IS NOT NULL),
            UNIQUE(kpi_definition_id,revision),
            CHECK((revision=1)=(supersedes_definition_revision_id IS NULL)),
            CHECK(datetime(knowledge_at)<=datetime(recorded_at)),
            CHECK((definition_text_status='verbatim'
                   AND reported_definition_text IS NOT NULL
                   AND length(trim(reported_definition_text))>0)
               OR (definition_text_status='not_stated'
                   AND reported_definition_text IS NULL)),
            CHECK((status='admitted' AND reason_code IS NULL
                   AND period_kind<>'unknown' AND stock_flow_behavior<>'unknown'
                   AND unit_family<>'unknown' AND unit_key<>'unknown'
                   AND unit_scale<>'unknown' AND accounting_basis<>'unknown'
                   AND consolidation_scope<>'unknown'
                   AND currency_disposition<>'unknown')
               OR (status='quarantined' AND reason_code IS NOT NULL
                   AND length(trim(reason_code))>0)),
            CHECK(stock_flow_behavior<>'stock' OR period_kind='instant'),
            CHECK(stock_flow_behavior<>'flow' OR period_kind='duration'),
            CHECK(consolidation_scope NOT IN ('geography','segment','product')
                  OR json(dimensions_json)<>'{}'),
            CHECK((unit_key IN ('actual','thousands','millions','billions')
                   AND unit_family IN ('currency','unknown'))
               OR (unit_key='count' AND unit_family IN ('count','unknown'))
               OR (unit_key='percent' AND unit_family IN ('percentage','unknown'))
               OR (unit_key='ratio' AND unit_family IN ('ratio','unknown'))
               OR (unit_key='bps' AND unit_family IN ('basis_points','unknown'))
               OR unit_key='unknown'),
            CHECK((unit_key='actual' AND unit_scale IN ('none','unknown'))
               OR (unit_key='thousands' AND unit_scale IN ('thousands','unknown'))
               OR (unit_key='millions' AND unit_scale IN ('millions','unknown'))
               OR (unit_key='billions' AND unit_scale IN ('billions','unknown'))
               OR (unit_key IN ('percent','ratio','bps')
                   AND unit_scale IN ('none','unknown'))
               OR (unit_key='count'
                   AND unit_scale IN ('none','thousands','millions','billions','unknown'))
               OR unit_key='unknown'),
            CHECK((unit_key IN ('actual','thousands','millions','billions')
                   AND ((status='admitted' AND currency_disposition='explicit'
                         AND currency IS NOT NULL)
                     OR status='quarantined'))
               OR (unit_key IN ('percent','ratio','bps','count')
                   AND currency_disposition='not_applicable' AND currency IS NULL)
               OR unit_key='unknown'),
            CHECK((currency_disposition='explicit' AND currency IS NOT NULL)
               OR (currency_disposition<>'explicit' AND currency IS NULL))
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_kpi_definition_revisions_current "
        "ON kpi_definition_revisions(kpi_definition_id,revision,effective_at,knowledge_at)"
    )
    op.execute(
        "CREATE INDEX ix_kpi_definition_revisions_source "
        "ON kpi_definition_revisions(source_document_version_id,source_evidence_node_id)"
    )
    op.execute(
        """
        CREATE TRIGGER trg_kpi_definition_revisions_json_shape
        BEFORE INSERT ON kpi_definition_revisions
        WHEN EXISTS (
            SELECT 1 FROM json_each(NEW.dimensions_json)
            WHERE type<>'text'
        )
        BEGIN
            SELECT RAISE(ABORT,'KPI definition dimensions require string values');
        END
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_kpi_definition_revisions_predecessor
        BEFORE INSERT ON kpi_definition_revisions
        BEGIN
            SELECT CASE WHEN NEW.revision=1 AND EXISTS (
                SELECT 1 FROM kpi_definition_revisions
                WHERE kpi_definition_id=NEW.kpi_definition_id
            ) THEN RAISE(ABORT,'KPI definition revision 1 conflicts with existing root') END;
            SELECT CASE WHEN NEW.revision>1 AND NOT EXISTS (
                SELECT 1 FROM kpi_definition_revisions prior
                WHERE prior.kpi_definition_revision_id=
                        NEW.supersedes_definition_revision_id
                  AND prior.kpi_definition_id=NEW.kpi_definition_id
                  AND prior.revision=NEW.revision-1
                  AND datetime(prior.knowledge_at)<=datetime(NEW.knowledge_at)
                  AND datetime(prior.recorded_at)<=datetime(NEW.recorded_at)
                  AND EXISTS (
                    SELECT 1 FROM reporting_entities prior_entity
                    JOIN reporting_entities new_entity
                      ON new_entity.reporting_entity_id=NEW.reporting_entity_id
                     AND new_entity.issuer_id=prior_entity.issuer_id
                    WHERE prior_entity.reporting_entity_id=prior.reporting_entity_id)
                  AND NOT EXISTS (
                    SELECT 1 FROM kpi_definition_revisions successor
                    WHERE successor.supersedes_definition_revision_id=
                            prior.kpi_definition_revision_id)
            ) THEN RAISE(ABORT,'KPI definition revision predecessor mismatch') END;
        END
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_kpi_definition_revisions_source
        BEFORE INSERT ON kpi_definition_revisions
        WHEN NOT EXISTS (
            SELECT 1
            FROM evidence_document_versions document
            JOIN evidence_source_observations source
              ON source.observation_id=document.observation_id
            JOIN evidence_extraction_runs run
              ON run.document_version_id=document.document_version_id
             AND run.outcome='succeeded'
            JOIN evidence_nodes node
              ON node.extraction_run_id=run.extraction_run_id
             AND node.node_id=NEW.source_evidence_node_id
            JOIN reporting_entities entity
              ON entity.reporting_entity_id=NEW.reporting_entity_id
             AND entity.issuer_id=document.issuer_id
            LEFT JOIN securities security
              ON security.security_id=NEW.scope_security_id
             AND security.issuer_id=document.issuer_id
            WHERE document.document_version_id=NEW.source_document_version_id
              AND (NEW.scope_security_id IS NULL OR security.security_id IS NOT NULL)
              AND node.locator_json=NEW.source_locator_json
              AND node.locator_sha256=NEW.source_locator_sha256
              AND NEW.source_locator_sha256=fact_sha256(NEW.source_locator_json)
              AND datetime(source.retrieved_at)<=datetime(NEW.knowledge_at)
              AND datetime(document.recorded_at)<=datetime(NEW.recorded_at)
              AND datetime(node.recorded_at)<=datetime(NEW.recorded_at)
        )
        BEGIN
            SELECT RAISE(ABORT,'KPI definition source evidence mismatch');
        END
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_kpi_definition_revisions_commitment_keys
        BEFORE INSERT ON kpi_definition_revisions
        WHEN json_type(NEW.commitment_json,'$.kpi_definition_revision_id') IS NULL
          OR json_type(NEW.commitment_json,'$.idempotency_key') IS NULL
          OR json_type(NEW.commitment_json,'$.kpi_definition_id') IS NULL
          OR json_type(NEW.commitment_json,'$.reporting_entity_id') IS NULL
          OR json_type(NEW.commitment_json,'$.scope_security_id') IS NULL
          OR json_type(NEW.commitment_json,'$.revision') IS NULL
          OR json_type(NEW.commitment_json,'$.supersedes_definition_revision_id') IS NULL
          OR json_type(NEW.commitment_json,'$.status') IS NULL
          OR json_type(NEW.commitment_json,'$.lifecycle') IS NULL
          OR json_type(NEW.commitment_json,'$.reported_label') IS NULL
          OR json_type(NEW.commitment_json,'$.reported_definition_text') IS NULL
          OR json_type(NEW.commitment_json,'$.definition_text_status') IS NULL
          OR json_type(NEW.commitment_json,'$.period_kind') IS NULL
          OR json_type(NEW.commitment_json,'$.stock_flow_behavior') IS NULL
          OR json_type(NEW.commitment_json,'$.unit_family') IS NULL
          OR json_type(NEW.commitment_json,'$.unit_key') IS NULL
          OR json_type(NEW.commitment_json,'$.unit_scale') IS NULL
          OR json_type(NEW.commitment_json,'$.currency_disposition') IS NULL
          OR json_type(NEW.commitment_json,'$.currency') IS NULL
          OR json_type(NEW.commitment_json,'$.accounting_basis') IS NULL
          OR json_type(NEW.commitment_json,'$.consolidation_scope') IS NULL
          OR json_type(NEW.commitment_json,'$.dimensions') IS NULL
          OR json_type(NEW.commitment_json,'$.source_document_version_id') IS NULL
          OR json_type(NEW.commitment_json,'$.source_evidence_node_id') IS NULL
          OR json_type(NEW.commitment_json,'$.source_locator') IS NULL
          OR json_type(NEW.commitment_json,'$.reason_code') IS NULL
          OR json_type(NEW.commitment_json,'$.reviewed_by') IS NULL
          OR json_type(NEW.commitment_json,'$.effective_at') IS NULL
          OR json_type(NEW.commitment_json,'$.knowledge_at') IS NULL
          OR json_type(NEW.commitment_json,'$.recorded_at') IS NULL
        BEGIN
            SELECT RAISE(ABORT,'KPI definition commitment keys are incomplete');
        END
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_kpi_definition_revisions_commitment
        BEFORE INSERT ON kpi_definition_revisions
        WHEN NEW.commitment_sha256<>fact_sha256(NEW.commitment_json)
          OR json_extract(NEW.commitment_json,'$.kpi_definition_revision_id')
                IS NOT NEW.kpi_definition_revision_id
          OR json_extract(NEW.commitment_json,'$.idempotency_key') IS NOT NEW.idempotency_key
          OR json_extract(NEW.commitment_json,'$.kpi_definition_id')
                IS NOT NEW.kpi_definition_id
          OR json_extract(NEW.commitment_json,'$.reporting_entity_id')
                IS NOT NEW.reporting_entity_id
          OR json_extract(NEW.commitment_json,'$.scope_security_id')
                IS NOT NEW.scope_security_id
          OR json_extract(NEW.commitment_json,'$.revision') IS NOT NEW.revision
          OR json_extract(NEW.commitment_json,'$.supersedes_definition_revision_id')
                IS NOT NEW.supersedes_definition_revision_id
          OR json_extract(NEW.commitment_json,'$.status') IS NOT NEW.status
          OR json_extract(NEW.commitment_json,'$.lifecycle') IS NOT NEW.lifecycle
          OR json_extract(NEW.commitment_json,'$.reported_label') IS NOT NEW.reported_label
          OR json_extract(NEW.commitment_json,'$.reported_definition_text')
                IS NOT NEW.reported_definition_text
          OR json_extract(NEW.commitment_json,'$.definition_text_status')
                IS NOT NEW.definition_text_status
          OR json_extract(NEW.commitment_json,'$.period_kind') IS NOT NEW.period_kind
          OR json_extract(NEW.commitment_json,'$.stock_flow_behavior')
                IS NOT NEW.stock_flow_behavior
          OR json_extract(NEW.commitment_json,'$.unit_family') IS NOT NEW.unit_family
          OR json_extract(NEW.commitment_json,'$.unit_key') IS NOT NEW.unit_key
          OR json_extract(NEW.commitment_json,'$.unit_scale') IS NOT NEW.unit_scale
          OR json_extract(NEW.commitment_json,'$.currency_disposition')
                IS NOT NEW.currency_disposition
          OR json_extract(NEW.commitment_json,'$.currency') IS NOT NEW.currency
          OR json_extract(NEW.commitment_json,'$.accounting_basis')
                IS NOT NEW.accounting_basis
          OR json_extract(NEW.commitment_json,'$.consolidation_scope')
                IS NOT NEW.consolidation_scope
          OR json_extract(NEW.commitment_json,'$.dimensions') IS NOT json(NEW.dimensions_json)
          OR json_extract(NEW.commitment_json,'$.source_document_version_id')
                IS NOT NEW.source_document_version_id
          OR json_extract(NEW.commitment_json,'$.source_evidence_node_id')
                IS NOT NEW.source_evidence_node_id
          OR json_extract(NEW.commitment_json,'$.source_locator')
                IS NOT json(NEW.source_locator_json)
          OR json_extract(NEW.commitment_json,'$.reason_code') IS NOT NEW.reason_code
          OR json_extract(NEW.commitment_json,'$.reviewed_by') IS NOT NEW.reviewed_by
          OR json_extract(NEW.commitment_json,'$.effective_at') IS NOT NEW.effective_at
          OR json_extract(NEW.commitment_json,'$.knowledge_at') IS NOT NEW.knowledge_at
          OR json_extract(NEW.commitment_json,'$.recorded_at') IS NOT NEW.recorded_at
        BEGIN
            SELECT RAISE(ABORT,'KPI definition commitment mismatch');
        END
        """
    )
    op.execute(
        "CREATE TRIGGER trg_kpi_definition_revisions_no_update BEFORE UPDATE ON "
        "kpi_definition_revisions BEGIN SELECT RAISE(ABORT,"
        "'KPI definition revisions are append-only'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_kpi_definition_revisions_no_delete BEFORE DELETE ON "
        "kpi_definition_revisions BEGIN SELECT RAISE(ABORT,"
        "'KPI definition revisions are append-only'); END"
    )

    op.execute(
        """
        CREATE TABLE kpi_definition_comparability_revisions (
            comparability_revision_id TEXT PRIMARY KEY,
            idempotency_key TEXT NOT NULL UNIQUE,
            predecessor_definition_revision_id TEXT NOT NULL
                REFERENCES kpi_definition_revisions(kpi_definition_revision_id),
            successor_definition_revision_id TEXT NOT NULL
                REFERENCES kpi_definition_revisions(kpi_definition_revision_id),
            revision INTEGER NOT NULL CHECK(revision > 0),
            supersedes_comparability_revision_id TEXT UNIQUE
                REFERENCES kpi_definition_comparability_revisions(comparability_revision_id),
            relation_kind TEXT NOT NULL CHECK(relation_kind IN
                ('same_definition','renamed','redefined','recast','restated','split','combined')),
            disposition TEXT NOT NULL CHECK(disposition IN
                ('continuous','comparable_with_break','not_comparable')),
            reason_code TEXT NOT NULL CHECK(length(trim(reason_code)) > 0),
            reviewed_by TEXT NOT NULL CHECK(length(trim(reviewed_by)) > 0),
            source_document_version_id TEXT NOT NULL
                REFERENCES evidence_document_versions(document_version_id),
            source_evidence_node_id TEXT NOT NULL REFERENCES evidence_nodes(node_id),
            source_locator_json TEXT NOT NULL
                CHECK(json_valid(source_locator_json)
                  AND json_type(source_locator_json)='object'
                  AND json(source_locator_json)<>'{}'),
            source_locator_sha256 TEXT NOT NULL
                CHECK(length(source_locator_sha256)=64
                  AND source_locator_sha256 NOT GLOB '*[^0-9a-f]*'),
            commitment_json TEXT NOT NULL
                CHECK(json_valid(commitment_json) AND json_type(commitment_json)='object'),
            commitment_sha256 TEXT NOT NULL
                CHECK(length(commitment_sha256)=64
                  AND commitment_sha256 NOT GLOB '*[^0-9a-f]*'),
            effective_at TEXT NOT NULL CHECK(datetime(effective_at) IS NOT NULL),
            knowledge_at TEXT NOT NULL CHECK(datetime(knowledge_at) IS NOT NULL),
            recorded_at TEXT NOT NULL CHECK(datetime(recorded_at) IS NOT NULL),
            UNIQUE(predecessor_definition_revision_id,
                   successor_definition_revision_id,revision),
            CHECK(predecessor_definition_revision_id<>successor_definition_revision_id),
            CHECK((revision=1)=(supersedes_comparability_revision_id IS NULL)),
            CHECK(datetime(knowledge_at)<=datetime(recorded_at)),
            CHECK(disposition<>'continuous'
                  OR relation_kind IN ('same_definition','renamed'))
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_kpi_definition_comparability_current ON "
        "kpi_definition_comparability_revisions("
        "predecessor_definition_revision_id,successor_definition_revision_id,revision,"
        "effective_at,knowledge_at)"
    )
    op.execute(
        """
        CREATE TRIGGER trg_kpi_definition_comparability_predecessor
        BEFORE INSERT ON kpi_definition_comparability_revisions
        BEGIN
            SELECT CASE WHEN EXISTS (
                SELECT 1 FROM kpi_definition_comparability_revisions reverse
                WHERE reverse.predecessor_definition_revision_id=
                        NEW.successor_definition_revision_id
                  AND reverse.successor_definition_revision_id=
                        NEW.predecessor_definition_revision_id
            ) THEN RAISE(ABORT,'KPI comparability reverse pair already exists') END;
            SELECT CASE WHEN NEW.revision=1 AND EXISTS (
                SELECT 1 FROM kpi_definition_comparability_revisions
                WHERE predecessor_definition_revision_id=
                        NEW.predecessor_definition_revision_id
                  AND successor_definition_revision_id=
                        NEW.successor_definition_revision_id
            ) THEN RAISE(ABORT,'KPI comparability revision 1 conflicts with existing pair') END;
            SELECT CASE WHEN NEW.revision>1 AND NOT EXISTS (
                SELECT 1 FROM kpi_definition_comparability_revisions prior
                WHERE prior.comparability_revision_id=
                        NEW.supersedes_comparability_revision_id
                  AND prior.predecessor_definition_revision_id=
                        NEW.predecessor_definition_revision_id
                  AND prior.successor_definition_revision_id=
                        NEW.successor_definition_revision_id
                  AND prior.revision=NEW.revision-1
                  AND datetime(prior.knowledge_at)<=datetime(NEW.knowledge_at)
                  AND datetime(prior.recorded_at)<=datetime(NEW.recorded_at)
                  AND NOT EXISTS (
                    SELECT 1 FROM kpi_definition_comparability_revisions successor
                    WHERE successor.supersedes_comparability_revision_id=
                            prior.comparability_revision_id)
            ) THEN RAISE(ABORT,'KPI comparability revision predecessor mismatch') END;
        END
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_kpi_definition_comparability_source
        BEFORE INSERT ON kpi_definition_comparability_revisions
        WHEN NOT EXISTS (
            SELECT 1
            FROM kpi_definition_revisions predecessor
            JOIN reporting_entities predecessor_entity
              ON predecessor_entity.reporting_entity_id=predecessor.reporting_entity_id
            JOIN kpi_definition_revisions successor
              ON successor.kpi_definition_revision_id=
                    NEW.successor_definition_revision_id
            JOIN reporting_entities successor_entity
              ON successor_entity.reporting_entity_id=successor.reporting_entity_id
             AND successor_entity.issuer_id=predecessor_entity.issuer_id
            JOIN evidence_document_versions document
              ON document.document_version_id=NEW.source_document_version_id
             AND document.issuer_id=predecessor_entity.issuer_id
            JOIN evidence_source_observations source
              ON source.observation_id=document.observation_id
            JOIN evidence_extraction_runs run
              ON run.document_version_id=document.document_version_id
             AND run.outcome='succeeded'
            JOIN evidence_nodes node
              ON node.extraction_run_id=run.extraction_run_id
             AND node.node_id=NEW.source_evidence_node_id
            WHERE predecessor.kpi_definition_revision_id=
                    NEW.predecessor_definition_revision_id
              AND datetime(predecessor.knowledge_at)<=datetime(NEW.knowledge_at)
              AND datetime(predecessor.recorded_at)<=datetime(NEW.recorded_at)
              AND datetime(successor.knowledge_at)<=datetime(NEW.knowledge_at)
              AND datetime(successor.recorded_at)<=datetime(NEW.recorded_at)
              AND node.locator_json=NEW.source_locator_json
              AND node.locator_sha256=NEW.source_locator_sha256
              AND NEW.source_locator_sha256=fact_sha256(NEW.source_locator_json)
              AND datetime(source.retrieved_at)<=datetime(NEW.knowledge_at)
              AND datetime(document.recorded_at)<=datetime(NEW.recorded_at)
              AND datetime(node.recorded_at)<=datetime(NEW.recorded_at)
        )
        BEGIN
            SELECT RAISE(ABORT,'KPI comparability source evidence mismatch');
        END
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_kpi_definition_comparability_semantic_axes
        BEFORE INSERT ON kpi_definition_comparability_revisions
        WHEN NEW.disposition='continuous' AND EXISTS (
            SELECT 1 FROM kpi_definition_revisions predecessor
            JOIN kpi_definition_revisions successor
              ON successor.kpi_definition_revision_id=
                    NEW.successor_definition_revision_id
            WHERE predecessor.kpi_definition_revision_id=
                    NEW.predecessor_definition_revision_id
              AND (predecessor.period_kind<>successor.period_kind
                   OR predecessor.stock_flow_behavior<>successor.stock_flow_behavior
                   OR predecessor.accounting_basis<>successor.accounting_basis
                   OR predecessor.consolidation_scope<>successor.consolidation_scope
                   OR predecessor.dimensions_json<>successor.dimensions_json)
        )
        BEGIN
            SELECT RAISE(ABORT,'KPI semantic-axis change requires comparability break');
        END
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_kpi_definition_comparability_unit
        BEFORE INSERT ON kpi_definition_comparability_revisions
        WHEN NEW.disposition='continuous' AND EXISTS (
            SELECT 1 FROM kpi_definition_revisions predecessor
            JOIN kpi_definition_revisions successor
              ON successor.kpi_definition_revision_id=
                    NEW.successor_definition_revision_id
            WHERE predecessor.kpi_definition_revision_id=
                    NEW.predecessor_definition_revision_id
              AND (predecessor.unit_family<>successor.unit_family
                   OR predecessor.unit_key<>successor.unit_key
                   OR predecessor.unit_scale<>successor.unit_scale)
        )
        BEGIN
            SELECT RAISE(ABORT,'KPI unit or scale change requires comparability break');
        END
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_kpi_definition_comparability_currency
        BEFORE INSERT ON kpi_definition_comparability_revisions
        WHEN NEW.disposition='continuous' AND EXISTS (
            SELECT 1 FROM kpi_definition_revisions predecessor
            JOIN kpi_definition_revisions successor
              ON successor.kpi_definition_revision_id=
                    NEW.successor_definition_revision_id
            WHERE predecessor.kpi_definition_revision_id=
                    NEW.predecessor_definition_revision_id
              AND (predecessor.currency_disposition<>successor.currency_disposition
                   OR predecessor.currency IS NOT successor.currency)
        )
        BEGIN
            SELECT RAISE(ABORT,'KPI currency change requires comparability break');
        END
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_kpi_definition_comparability_commitment_keys
        BEFORE INSERT ON kpi_definition_comparability_revisions
        WHEN json_type(NEW.commitment_json,'$.comparability_revision_id') IS NULL
          OR json_type(NEW.commitment_json,'$.idempotency_key') IS NULL
          OR json_type(NEW.commitment_json,'$.predecessor_definition_revision_id') IS NULL
          OR json_type(NEW.commitment_json,'$.successor_definition_revision_id') IS NULL
          OR json_type(NEW.commitment_json,'$.revision') IS NULL
          OR json_type(NEW.commitment_json,'$.supersedes_comparability_revision_id') IS NULL
          OR json_type(NEW.commitment_json,'$.relation_kind') IS NULL
          OR json_type(NEW.commitment_json,'$.disposition') IS NULL
          OR json_type(NEW.commitment_json,'$.reason_code') IS NULL
          OR json_type(NEW.commitment_json,'$.reviewed_by') IS NULL
          OR json_type(NEW.commitment_json,'$.source_document_version_id') IS NULL
          OR json_type(NEW.commitment_json,'$.source_evidence_node_id') IS NULL
          OR json_type(NEW.commitment_json,'$.source_locator') IS NULL
          OR json_type(NEW.commitment_json,'$.effective_at') IS NULL
          OR json_type(NEW.commitment_json,'$.knowledge_at') IS NULL
          OR json_type(NEW.commitment_json,'$.recorded_at') IS NULL
        BEGIN
            SELECT RAISE(ABORT,'KPI comparability commitment keys are incomplete');
        END
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_kpi_definition_comparability_commitment
        BEFORE INSERT ON kpi_definition_comparability_revisions
        WHEN NEW.commitment_sha256<>fact_sha256(NEW.commitment_json)
          OR json_extract(NEW.commitment_json,'$.comparability_revision_id')
                IS NOT NEW.comparability_revision_id
          OR json_extract(NEW.commitment_json,'$.idempotency_key') IS NOT NEW.idempotency_key
          OR json_extract(NEW.commitment_json,'$.predecessor_definition_revision_id')
                IS NOT NEW.predecessor_definition_revision_id
          OR json_extract(NEW.commitment_json,'$.successor_definition_revision_id')
                IS NOT NEW.successor_definition_revision_id
          OR json_extract(NEW.commitment_json,'$.revision') IS NOT NEW.revision
          OR json_extract(NEW.commitment_json,'$.supersedes_comparability_revision_id')
                IS NOT NEW.supersedes_comparability_revision_id
          OR json_extract(NEW.commitment_json,'$.relation_kind') IS NOT NEW.relation_kind
          OR json_extract(NEW.commitment_json,'$.disposition') IS NOT NEW.disposition
          OR json_extract(NEW.commitment_json,'$.reason_code') IS NOT NEW.reason_code
          OR json_extract(NEW.commitment_json,'$.reviewed_by') IS NOT NEW.reviewed_by
          OR json_extract(NEW.commitment_json,'$.source_document_version_id')
                IS NOT NEW.source_document_version_id
          OR json_extract(NEW.commitment_json,'$.source_evidence_node_id')
                IS NOT NEW.source_evidence_node_id
          OR json_extract(NEW.commitment_json,'$.source_locator')
                IS NOT json(NEW.source_locator_json)
          OR json_extract(NEW.commitment_json,'$.effective_at') IS NOT NEW.effective_at
          OR json_extract(NEW.commitment_json,'$.knowledge_at') IS NOT NEW.knowledge_at
          OR json_extract(NEW.commitment_json,'$.recorded_at') IS NOT NEW.recorded_at
        BEGIN
            SELECT RAISE(ABORT,'KPI comparability commitment mismatch');
        END
        """
    )
    op.execute(
        "CREATE TRIGGER trg_kpi_definition_comparability_no_update BEFORE UPDATE ON "
        "kpi_definition_comparability_revisions BEGIN SELECT RAISE(ABORT,"
        "'KPI definition comparability is append-only'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_kpi_definition_comparability_no_delete BEFORE DELETE ON "
        "kpi_definition_comparability_revisions BEGIN SELECT RAISE(ABORT,"
        "'KPI definition comparability is append-only'); END"
    )

    op.execute(
        "ALTER TABLE kpi_fact_semantic_contexts ADD COLUMN "
        "kpi_definition_revision_id TEXT REFERENCES "
        "kpi_definition_revisions(kpi_definition_revision_id)"
    )
    op.execute(
        "CREATE INDEX ix_kpi_fact_semantic_definition_revision ON "
        "kpi_fact_semantic_contexts(kpi_definition_revision_id,kpi_fact_id,revision)"
    )
    op.execute(
        """
        CREATE TRIGGER trg_kpi_fact_semantic_definition_exact
        BEFORE INSERT ON kpi_fact_semantic_contexts
        WHEN NEW.kpi_definition_revision_id IS NOT NULL AND NOT EXISTS (
            SELECT 1
            FROM kpi_definition_revisions definition
            JOIN kpi_facts fact ON fact.id=NEW.kpi_fact_id
            JOIN reporting_entities entity
              ON entity.reporting_entity_id=definition.reporting_entity_id
            WHERE definition.kpi_definition_revision_id=NEW.kpi_definition_revision_id
              AND definition.kpi_definition_id=fact.kpi_definition_id
              AND NEW.status='admitted'
              AND definition.status='admitted'
              AND definition.lifecycle='active'
              AND definition.reported_label=NEW.metric_name_as_reported
              AND definition.accounting_basis=NEW.accounting_basis
              AND definition.consolidation_scope=NEW.consolidation_scope
              AND definition.dimensions_json=NEW.dimensions_json
              AND definition.unit_scale=NEW.unit_scale
              AND definition.unit_key=fact.unit
              AND date(NEW.reported_period_end)=date(fact.period_end)
              AND ((definition.currency_disposition='explicit'
                    AND definition.currency=fact.currency)
                   OR (definition.currency_disposition='not_applicable'
                       AND definition.currency IS NULL AND fact.currency IS NULL))
              AND datetime(definition.effective_at)<=datetime(fact.period_end)
              AND datetime(definition.knowledge_at)<=datetime(NEW.knowledge_at)
              AND datetime(definition.recorded_at)<=datetime(NEW.knowledge_at)
              AND datetime(NEW.knowledge_at) IS NOT NULL
              AND definition.kpi_definition_revision_id=(
                    SELECT selected.kpi_definition_revision_id
                    FROM kpi_definition_revisions selected
                    WHERE selected.kpi_definition_id=fact.kpi_definition_id
                      AND datetime(selected.effective_at)<=datetime(fact.period_end)
                      AND datetime(selected.knowledge_at)<=datetime(NEW.knowledge_at)
                      AND datetime(selected.recorded_at)<=datetime(NEW.knowledge_at)
                    ORDER BY datetime(selected.effective_at) DESC,
                             datetime(selected.knowledge_at) DESC,
                             selected.revision DESC
                    LIMIT 1)
              AND entity.issuer_id=COALESCE(
                    (SELECT document.issuer_id
                     FROM v_legacy_document_evidence_bindings_current binding
                     JOIN evidence_document_versions document
                       ON document.document_version_id=binding.document_version_id
                     WHERE binding.legacy_document_id=fact.source_doc_id LIMIT 1),
                    (SELECT document.issuer_id
                     FROM evidence_document_versions document
                     WHERE document.legacy_document_id=fact.source_doc_id
                     ORDER BY document.version_sequence DESC LIMIT 1))
        )
        BEGIN
            SELECT RAISE(ABORT,'KPI semantic definition binding mismatch');
        END
        """
    )


def downgrade() -> None:
    bind = op.get_bind()
    retained = bind.exec_driver_sql(
        "SELECT (SELECT COUNT(*) FROM kpi_definition_revisions)"
        "+(SELECT COUNT(*) FROM kpi_definition_comparability_revisions)"
        "+(SELECT COUNT(*) FROM kpi_fact_semantic_contexts "
        "WHERE kpi_definition_revision_id IS NOT NULL)"
    ).scalar_one()
    if int(retained) > 0:
        raise RuntimeError("cannot downgrade KPI definition revision rows without losing history")
    op.execute("DROP TRIGGER IF EXISTS trg_kpi_fact_semantic_definition_exact")
    op.execute("DROP INDEX IF EXISTS ix_kpi_fact_semantic_definition_revision")
    op.execute("ALTER TABLE kpi_fact_semantic_contexts DROP COLUMN kpi_definition_revision_id")
    op.execute("DROP TRIGGER IF EXISTS trg_kpi_definition_comparability_no_delete")
    op.execute("DROP TRIGGER IF EXISTS trg_kpi_definition_comparability_no_update")
    op.execute("DROP TRIGGER IF EXISTS trg_kpi_definition_comparability_commitment")
    op.execute("DROP TRIGGER IF EXISTS trg_kpi_definition_comparability_commitment_keys")
    op.execute("DROP TRIGGER IF EXISTS trg_kpi_definition_comparability_currency")
    op.execute("DROP TRIGGER IF EXISTS trg_kpi_definition_comparability_unit")
    op.execute("DROP TRIGGER IF EXISTS trg_kpi_definition_comparability_semantic_axes")
    op.execute("DROP TRIGGER IF EXISTS trg_kpi_definition_comparability_source")
    op.execute("DROP TRIGGER IF EXISTS trg_kpi_definition_comparability_predecessor")
    op.execute("DROP INDEX IF EXISTS ix_kpi_definition_comparability_current")
    op.execute("DROP TABLE IF EXISTS kpi_definition_comparability_revisions")
    op.execute("DROP TRIGGER IF EXISTS trg_kpi_definition_revisions_no_delete")
    op.execute("DROP TRIGGER IF EXISTS trg_kpi_definition_revisions_no_update")
    op.execute("DROP TRIGGER IF EXISTS trg_kpi_definition_revisions_commitment")
    op.execute("DROP TRIGGER IF EXISTS trg_kpi_definition_revisions_commitment_keys")
    op.execute("DROP TRIGGER IF EXISTS trg_kpi_definition_revisions_source")
    op.execute("DROP TRIGGER IF EXISTS trg_kpi_definition_revisions_predecessor")
    op.execute("DROP TRIGGER IF EXISTS trg_kpi_definition_revisions_json_shape")
    op.execute("DROP INDEX IF EXISTS ix_kpi_definition_revisions_source")
    op.execute("DROP INDEX IF EXISTS ix_kpi_definition_revisions_current")
    op.execute("DROP TABLE IF EXISTS kpi_definition_revisions")
