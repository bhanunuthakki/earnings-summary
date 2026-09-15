"""Add immutable governed DCF forecast output series.

Revision ID: 0039_add_dcf_forecast_series
Revises: 0038_add_kpi_definition_revisions
"""

from __future__ import annotations

from alembic import op

revision = "0039_add_dcf_forecast_series"
down_revision = "0038_add_kpi_definition_revisions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE dcf_forecast_metric_mapping_revisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL CHECK(ticker=UPPER(ticker) AND length(trim(ticker)) > 0),
            engine_family TEXT NOT NULL CHECK(length(trim(engine_family)) > 0),
            engine_version TEXT NOT NULL CHECK(length(trim(engine_version)) > 0),
            series_key TEXT NOT NULL CHECK(length(trim(series_key)) > 0),
            viewspec_metric_token TEXT NOT NULL
                CHECK(length(trim(viewspec_metric_token)) > 0),
            canonical_metric_definition_revision_id TEXT NOT NULL
                REFERENCES canonical_metric_definition_revisions(metric_definition_revision_id),
            period_kind TEXT NOT NULL CHECK(period_kind='duration'),
            unit_family TEXT NOT NULL CHECK(length(trim(unit_family)) > 0),
            value_scale TEXT NOT NULL
                CHECK(value_scale IN ('ones','thousands','millions','billions')),
            currency TEXT NOT NULL CHECK(length(trim(currency)) > 0),
            accounting_basis TEXT NOT NULL CHECK(length(trim(accounting_basis)) > 0),
            consolidation_scope TEXT NOT NULL CHECK(length(trim(consolidation_scope)) > 0),
            dimensions_sha256 TEXT NOT NULL
                CHECK(length(dimensions_sha256)=64
                  AND dimensions_sha256 NOT GLOB '*[^0-9a-f]*'),
            dimensions_json TEXT NOT NULL
                CHECK(json_valid(dimensions_json) AND json_type(dimensions_json)='array'),
            admission_status TEXT NOT NULL
                CHECK(admission_status IN ('admitted','quarantined','retired')),
            confidence TEXT NOT NULL CHECK(confidence IN ('high','medium','low')),
            policy_name TEXT NOT NULL CHECK(length(trim(policy_name)) > 0),
            policy_version TEXT NOT NULL CHECK(length(trim(policy_version)) > 0),
            policy_config_sha256 TEXT NOT NULL
                CHECK(length(policy_config_sha256)=64
                  AND policy_config_sha256 NOT GLOB '*[^0-9a-f]*'),
            evidence_json TEXT NOT NULL
                CHECK(json_valid(evidence_json) AND json_type(evidence_json)='object'),
            evidence_sha256 TEXT NOT NULL
                CHECK(length(evidence_sha256)=64
                  AND evidence_sha256 NOT GLOB '*[^0-9a-f]*'),
            reviewer_identity TEXT,
            revision INTEGER NOT NULL CHECK(revision > 0),
            supersedes_mapping_revision_id INTEGER UNIQUE
                REFERENCES dcf_forecast_metric_mapping_revisions(id),
            effective_at TEXT NOT NULL,
            knowledge_at TEXT NOT NULL,
            recorded_at TEXT NOT NULL,
            UNIQUE(ticker,engine_family,engine_version,series_key,revision),
            CHECK((revision=1)=(supersedes_mapping_revision_id IS NULL)),
            CHECK(effective_at <= knowledge_at AND knowledge_at <= recorded_at),
            CHECK(admission_status <> 'admitted' OR (
                confidence='high'
                AND reviewer_identity IS NOT NULL
                AND length(trim(reviewer_identity)) > 0
                AND COALESCE(json_type(evidence_json,'$.basis'),'')='text'
                AND length(trim(json_extract(evidence_json,'$.basis'))) > 0
            ))
        )
        """
    )
    op.execute(
        """
        CREATE TABLE dcf_forecast_series_points (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            dcf_run_id INTEGER NOT NULL REFERENCES dcf_runs(id) ON DELETE RESTRICT,
            mapping_revision_id INTEGER NOT NULL
                REFERENCES dcf_forecast_metric_mapping_revisions(id) ON DELETE RESTRICT,
            series_key TEXT NOT NULL CHECK(length(trim(series_key)) > 0),
            period_start TEXT NOT NULL,
            period_end TEXT NOT NULL,
            value REAL NOT NULL CHECK(value=value),
            recorded_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
            UNIQUE(dcf_run_id,mapping_revision_id,period_end),
            CHECK(period_start <= period_end)
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_dcf_forecast_points_run "
        "ON dcf_forecast_series_points(dcf_run_id,mapping_revision_id,period_end)"
    )
    op.execute(
        "CREATE INDEX ix_dcf_forecast_mapping_current "
        "ON dcf_forecast_metric_mapping_revisions"
        "(ticker,engine_family,engine_version,series_key,revision DESC)"
    )
    op.execute(
        """
        CREATE TRIGGER trg_dcf_forecast_mapping_commitments
        BEFORE INSERT ON dcf_forecast_metric_mapping_revisions BEGIN
          SELECT CASE WHEN fact_sha256(NEW.dimensions_json) != NEW.dimensions_sha256
          THEN RAISE(ABORT,'DCF forecast dimensions commitment mismatch') END;
          SELECT CASE WHEN fact_sha256(NEW.evidence_json) != NEW.evidence_sha256
          THEN RAISE(ABORT,'DCF forecast evidence commitment mismatch') END;
        END
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_dcf_forecast_mapping_current_token_coordinate
        BEFORE INSERT ON dcf_forecast_metric_mapping_revisions
        WHEN NEW.admission_status='admitted' AND NEW.confidence='high' BEGIN
          SELECT CASE WHEN EXISTS (
            SELECT 1 FROM dcf_forecast_metric_mapping_revisions existing
            WHERE existing.ticker=NEW.ticker
              AND existing.viewspec_metric_token=NEW.viewspec_metric_token
              AND existing.canonical_metric_definition_revision_id=
                  NEW.canonical_metric_definition_revision_id
              AND existing.period_kind=NEW.period_kind
              AND existing.unit_family=NEW.unit_family
              AND existing.value_scale=NEW.value_scale
              AND existing.currency=NEW.currency
              AND existing.accounting_basis=NEW.accounting_basis
              AND existing.consolidation_scope=NEW.consolidation_scope
              AND existing.dimensions_sha256=NEW.dimensions_sha256
              AND NOT (
                existing.engine_family=NEW.engine_family
                AND existing.engine_version=NEW.engine_version
                AND existing.series_key=NEW.series_key
              )
              AND existing.admission_status='admitted'
              AND existing.confidence='high'
              AND NOT EXISTS (
                SELECT 1 FROM dcf_forecast_metric_mapping_revisions newer
                WHERE newer.ticker=existing.ticker
                  AND newer.engine_family=existing.engine_family
                  AND newer.engine_version=existing.engine_version
                  AND newer.series_key=existing.series_key
                  AND newer.revision>existing.revision
              )
          ) THEN RAISE(ABORT,
            'DCF forecast token/semantic coordinate already has a current mapping') END;
        END
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_dcf_forecast_mapping_predecessor BEFORE INSERT ON
        dcf_forecast_metric_mapping_revisions WHEN NEW.revision > 1 BEGIN
          SELECT CASE WHEN NOT EXISTS (
            SELECT 1 FROM dcf_forecast_metric_mapping_revisions prior
            WHERE prior.id=NEW.supersedes_mapping_revision_id
              AND prior.ticker=NEW.ticker
              AND prior.engine_family=NEW.engine_family
              AND prior.engine_version=NEW.engine_version
              AND prior.series_key=NEW.series_key
              AND prior.revision=NEW.revision-1
          ) THEN RAISE(ABORT,'DCF forecast mapping predecessor mismatch') END;
        END
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_dcf_forecast_mapping_no_update BEFORE UPDATE ON
        dcf_forecast_metric_mapping_revisions BEGIN
          SELECT RAISE(ABORT,'DCF forecast mappings are append-only');
        END
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_dcf_forecast_mapping_no_delete BEFORE DELETE ON
        dcf_forecast_metric_mapping_revisions BEGIN
          SELECT RAISE(ABORT,'DCF forecast mappings are append-only');
        END
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_dcf_forecast_point_mapping BEFORE INSERT ON
        dcf_forecast_series_points BEGIN
          SELECT CASE WHEN date(NEW.period_start) IS NULL
            OR date(NEW.period_end) IS NULL
            OR date(NEW.period_start) != NEW.period_start
            OR date(NEW.period_end) != NEW.period_end
            OR date(NEW.period_start) != date(NEW.period_end,'-1 year','+1 day')
          THEN RAISE(ABORT,'DCF forecast point requires an exact annual fiscal period') END;
          SELECT CASE WHEN NOT EXISTS (
            SELECT 1 FROM dcf_forecast_metric_mapping_revisions mapping
            WHERE mapping.id=NEW.mapping_revision_id
              AND mapping.series_key=NEW.series_key
              AND mapping.ticker=(
                SELECT UPPER(run.ticker) FROM dcf_runs run WHERE run.id=NEW.dcf_run_id
              )
              AND mapping.engine_version=(
                SELECT run.engine_version FROM dcf_runs run WHERE run.id=NEW.dcf_run_id
              )
              AND mapping.admission_status='admitted'
              AND mapping.confidence='high'
              AND EXISTS (
                SELECT 1 FROM canonical_metric_definition_revisions definition
                WHERE definition.metric_definition_revision_id=
                    mapping.canonical_metric_definition_revision_id
                  AND definition.lifecycle='active'
                  AND definition.period_kind=mapping.period_kind
                  AND definition.unit_family=mapping.unit_family
                  AND definition.accounting_basis=mapping.accounting_basis
                  AND NOT EXISTS (
                    SELECT 1 FROM canonical_metric_definition_revisions newer_definition
                    WHERE newer_definition.metric_id=definition.metric_id
                      AND newer_definition.revision>definition.revision
                  )
              )
              AND NOT EXISTS (
                SELECT 1 FROM dcf_forecast_metric_mapping_revisions newer
                WHERE newer.engine_family=mapping.engine_family
                  AND newer.ticker=mapping.ticker
                  AND newer.engine_version=mapping.engine_version
                  AND newer.series_key=mapping.series_key
                  AND newer.revision>mapping.revision
              )
          ) THEN RAISE(ABORT,'DCF forecast point requires current admitted mapping') END;
        END
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_dcf_forecast_point_no_update BEFORE UPDATE ON
        dcf_forecast_series_points BEGIN
          SELECT RAISE(ABORT,'DCF forecast points are append-only');
        END
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_dcf_forecast_point_no_delete BEFORE DELETE ON
        dcf_forecast_series_points BEGIN
          SELECT RAISE(ABORT,'DCF forecast points are append-only');
        END
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_dcf_forecast_point_no_delete")
    op.execute("DROP TRIGGER IF EXISTS trg_dcf_forecast_point_no_update")
    op.execute("DROP TRIGGER IF EXISTS trg_dcf_forecast_point_mapping")
    op.execute("DROP TRIGGER IF EXISTS trg_dcf_forecast_mapping_current_token_coordinate")
    op.execute("DROP TRIGGER IF EXISTS trg_dcf_forecast_mapping_commitments")
    op.execute("DROP TRIGGER IF EXISTS trg_dcf_forecast_mapping_no_delete")
    op.execute("DROP TRIGGER IF EXISTS trg_dcf_forecast_mapping_no_update")
    op.execute("DROP TRIGGER IF EXISTS trg_dcf_forecast_mapping_predecessor")
    op.execute("DROP INDEX IF EXISTS ix_dcf_forecast_points_run")
    op.execute("DROP INDEX IF EXISTS ix_dcf_forecast_mapping_current")
    op.execute("DROP TABLE IF EXISTS dcf_forecast_series_points")
    op.execute("DROP TABLE IF EXISTS dcf_forecast_metric_mapping_revisions")
