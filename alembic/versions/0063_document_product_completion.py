"""Add exact document identity and stage receipts to existing artifact owners.

This migration only adds forward-compatible nullable columns. It does not adopt
historical summaries or create a parallel artifact or completion table.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0063_document_product_completion"
down_revision = "0062_explicit_decision_journal"
branch_labels = None
depends_on = None


_PRODUCTS = "'transcript_summary'"


def upgrade() -> None:
    bind = op.get_bind()
    bind.exec_driver_sql(
        "INSERT INTO llm_budgets "
        "(purpose,monthly_cap_usd,warn_threshold_pct,hard_block,on_exceed,created_at,updated_at,notes) "
        "VALUES ('transcript_summary_claim_audit',5,0.8,1,'block',CURRENT_TIMESTAMP,"
        "CURRENT_TIMESTAMP,'Membership audit consumption cap; no metered fallback') "
        "ON CONFLICT(purpose) DO NOTHING"
    )
    # SQLite accepts an inline REFERENCES on ADD COLUMN when the new column is
    # nullable and has no non-NULL default. Alembic's separate-constraint path
    # attempts unsupported ALTER TABLE ADD CONSTRAINT here.
    bind.exec_driver_sql(
        "ALTER TABLE llm_artifacts ADD COLUMN issuer_id VARCHAR(128) "
        "REFERENCES issuer_entities(issuer_id)"
    )
    bind.exec_driver_sql(
        "ALTER TABLE llm_artifacts ADD COLUMN document_version_id VARCHAR(128) "
        "REFERENCES evidence_document_versions(document_version_id)"
    )
    op.add_column("llm_artifacts", sa.Column("document_period_start", sa.DateTime()))
    op.add_column("llm_artifacts", sa.Column("document_period_end", sa.DateTime()))
    op.add_column("llm_artifacts", sa.Column("document_period_type", sa.String(8)))
    op.add_column("llm_artifacts", sa.Column("document_fiscal_year", sa.Integer()))
    op.add_column("llm_artifacts", sa.Column("document_fiscal_quarter", sa.Integer()))

    bind.exec_driver_sql(
        "ALTER TABLE pipeline_stage_transitions ADD COLUMN artifact_id INTEGER "
        "REFERENCES llm_artifacts(id) ON DELETE RESTRICT"
    )
    op.add_column("pipeline_stage_transitions", sa.Column("receipt_json", sa.Text()))
    op.add_column("pipeline_stage_transitions", sa.Column("receipt_sha256", sa.String(64)))

    op.create_index(
        "idx_document_artifact_exact_current",
        "llm_artifacts",
        [
            "issuer_id",
            "document_version_id",
            "document_period_start",
            "document_period_end",
            "document_period_type",
            "document_fiscal_year",
            "document_fiscal_quarter",
            "purpose",
            "superseded_by_id",
        ],
        sqlite_where=sa.text("scope = 'document'"),
    )
    op.create_index(
        "idx_pipeline_stage_artifact_receipt",
        "pipeline_stage_transitions",
        ["artifact_id"],
        sqlite_where=sa.text("artifact_id IS NOT NULL"),
    )

    op.execute(
        f"""
        CREATE TRIGGER trg_llm_artifact_document_shape_insert
        BEFORE INSERT ON llm_artifacts
        WHEN COALESCE((
          (NEW.scope <> 'document'
           AND NEW.issuer_id IS NULL AND NEW.document_version_id IS NULL
           AND NEW.document_period_start IS NULL AND NEW.document_period_end IS NULL
           AND NEW.document_period_type IS NULL AND NEW.document_fiscal_year IS NULL
           AND NEW.document_fiscal_quarter IS NULL)
          OR
          (NEW.scope = 'document'
           AND NEW.issuer_id IS NOT NULL AND NEW.document_version_id IS NOT NULL
           AND NEW.document_period_end IS NOT NULL
           AND NEW.document_period_type IN ('Q1','Q2','Q3','Q4')
           AND NEW.document_fiscal_year BETWEEN 1 AND 9999
           AND NEW.document_fiscal_quarter BETWEEN 1 AND 4
           AND NEW.document_period_type = 'Q' || NEW.document_fiscal_quarter
           AND NEW.purpose IN ({_PRODUCTS})
           AND NEW.content_md IS NULL
           AND NEW.content_json IS NOT NULL
           AND json_valid(NEW.content_json)
           AND json_type(NEW.content_json) = 'object'
           AND json_extract(NEW.content_json,'$.schema_version') = 'document-product-envelope@1'
           AND length(NEW.input_sha256) = 64 AND NEW.input_sha256 NOT GLOB '*[^0-9a-f]*'
           AND length(NEW.output_sha256) = 64 AND NEW.output_sha256 NOT GLOB '*[^0-9a-f]*'
           AND length(NEW.prompt_version) > 0 AND length(NEW.model) > 0
           AND json_extract(NEW.content_json,'$.key.issuer_id') = NEW.issuer_id
           AND json_extract(NEW.content_json,'$.key.document_version_id') = NEW.document_version_id
           AND json_extract(NEW.content_json,'$.key.period_start') IS NEW.document_period_start
           AND json_extract(NEW.content_json,'$.key.period_end') = NEW.document_period_end
           AND json_extract(NEW.content_json,'$.key.period_type') = NEW.document_period_type
           AND json_extract(NEW.content_json,'$.key.fiscal_year') = NEW.document_fiscal_year
           AND json_extract(NEW.content_json,'$.key.fiscal_quarter') = NEW.document_fiscal_quarter
           AND json_extract(NEW.content_json,'$.key.product') = NEW.purpose)
        ), 0) = 0
        BEGIN
          SELECT RAISE(ABORT, 'invalid native document artifact identity or envelope');
        END
        """
    )
    op.execute(
        f"""
        CREATE TRIGGER trg_llm_artifact_document_shape_update
        BEFORE UPDATE OF scope, purpose, issuer_id, document_version_id,
          document_period_start, document_period_end, document_period_type,
          document_fiscal_year, document_fiscal_quarter, content_md, content_json,
          input_sha256, output_sha256, prompt_version, model ON llm_artifacts
        WHEN COALESCE((
          (NEW.scope <> 'document'
           AND NEW.issuer_id IS NULL AND NEW.document_version_id IS NULL
           AND NEW.document_period_start IS NULL AND NEW.document_period_end IS NULL
           AND NEW.document_period_type IS NULL AND NEW.document_fiscal_year IS NULL
           AND NEW.document_fiscal_quarter IS NULL)
          OR
          (NEW.scope = 'document'
           AND NEW.issuer_id IS NOT NULL AND NEW.document_version_id IS NOT NULL
           AND NEW.document_period_end IS NOT NULL
           AND NEW.document_period_type IN ('Q1','Q2','Q3','Q4')
           AND NEW.document_fiscal_year BETWEEN 1 AND 9999
           AND NEW.document_fiscal_quarter BETWEEN 1 AND 4
           AND NEW.document_period_type = 'Q' || NEW.document_fiscal_quarter
           AND NEW.purpose IN ({_PRODUCTS})
           AND NEW.content_md IS NULL
           AND NEW.content_json IS NOT NULL
           AND json_valid(NEW.content_json)
           AND json_type(NEW.content_json) = 'object'
           AND json_extract(NEW.content_json,'$.schema_version') = 'document-product-envelope@1'
           AND length(NEW.input_sha256) = 64 AND NEW.input_sha256 NOT GLOB '*[^0-9a-f]*'
           AND length(NEW.output_sha256) = 64 AND NEW.output_sha256 NOT GLOB '*[^0-9a-f]*'
           AND length(NEW.prompt_version) > 0 AND length(NEW.model) > 0
           AND json_extract(NEW.content_json,'$.key.issuer_id') = NEW.issuer_id
           AND json_extract(NEW.content_json,'$.key.document_version_id') = NEW.document_version_id
           AND json_extract(NEW.content_json,'$.key.period_start') IS NEW.document_period_start
           AND json_extract(NEW.content_json,'$.key.period_end') = NEW.document_period_end
           AND json_extract(NEW.content_json,'$.key.period_type') = NEW.document_period_type
           AND json_extract(NEW.content_json,'$.key.fiscal_year') = NEW.document_fiscal_year
           AND json_extract(NEW.content_json,'$.key.fiscal_quarter') = NEW.document_fiscal_quarter
           AND json_extract(NEW.content_json,'$.key.product') = NEW.purpose)
        ), 0) = 0
        BEGIN
          SELECT RAISE(ABORT, 'invalid native document artifact identity or envelope');
        END
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_pipeline_stage_document_receipt_insert
        BEFORE INSERT ON pipeline_stage_transitions
        WHEN COALESCE((
          (NEW.artifact_id IS NULL AND NEW.receipt_json IS NULL AND NEW.receipt_sha256 IS NULL)
          OR
          (NEW.artifact_id IS NOT NULL AND NEW.receipt_json IS NOT NULL
           AND NEW.receipt_sha256 IS NOT NULL
           AND json_valid(NEW.receipt_json) AND json_type(NEW.receipt_json)='object'
           AND length(NEW.receipt_sha256)=64 AND NEW.receipt_sha256 NOT GLOB '*[^0-9a-f]*'
           AND EXISTS (SELECT 1 FROM llm_artifacts a
                       WHERE a.id=NEW.artifact_id AND a.scope='document'))
        ), 0) = 0
        BEGIN
          SELECT RAISE(ABORT, 'artifact stage transition requires a document receipt');
        END
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_pipeline_stage_document_receipt_update
        BEFORE UPDATE OF artifact_id, receipt_json, receipt_sha256 ON pipeline_stage_transitions
        WHEN COALESCE((
          (NEW.artifact_id IS NULL AND NEW.receipt_json IS NULL AND NEW.receipt_sha256 IS NULL)
          OR
          (NEW.artifact_id IS NOT NULL AND NEW.receipt_json IS NOT NULL
           AND NEW.receipt_sha256 IS NOT NULL
           AND json_valid(NEW.receipt_json) AND json_type(NEW.receipt_json)='object'
           AND length(NEW.receipt_sha256)=64 AND NEW.receipt_sha256 NOT GLOB '*[^0-9a-f]*'
           AND EXISTS (SELECT 1 FROM llm_artifacts a
                       WHERE a.id=NEW.artifact_id AND a.scope='document'))
        ), 0) = 0
        BEGIN
          SELECT RAISE(ABORT, 'artifact stage transition requires a document receipt');
        END
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_llm_artifact_document_immutable
        BEFORE UPDATE ON llm_artifacts
        WHEN OLD.scope='document' AND (
          NEW.ticker IS NOT OLD.ticker OR NEW.scope IS NOT OLD.scope
          OR NEW.purpose IS NOT OLD.purpose
          OR NEW.fiscal_period IS NOT OLD.fiscal_period
          OR NEW.content_md IS NOT OLD.content_md
          OR NEW.content_json IS NOT OLD.content_json
          OR NEW.input_sha256 IS NOT OLD.input_sha256
          OR NEW.output_sha256 IS NOT OLD.output_sha256
          OR NEW.model IS NOT OLD.model
          OR NEW.prompt_version IS NOT OLD.prompt_version
          OR NEW.generated_at IS NOT OLD.generated_at
          OR NEW.expires_at IS NOT OLD.expires_at
          OR NEW.issuer_id IS NOT OLD.issuer_id
          OR NEW.document_version_id IS NOT OLD.document_version_id
          OR NEW.document_period_start IS NOT OLD.document_period_start
          OR NEW.document_period_end IS NOT OLD.document_period_end
          OR NEW.document_period_type IS NOT OLD.document_period_type
          OR NEW.document_fiscal_year IS NOT OLD.document_fiscal_year
          OR NEW.document_fiscal_quarter IS NOT OLD.document_fiscal_quarter
          OR NEW.source_doc_ids IS NOT OLD.source_doc_ids
          OR NEW.parent_artifact_ids IS NOT OLD.parent_artifact_ids
          OR NEW.llm_call_id IS NOT OLD.llm_call_id
          OR (OLD.superseded_by_id IS NOT NULL
              AND NEW.superseded_by_id IS NOT OLD.superseded_by_id)
        )
        BEGIN
          SELECT RAISE(ABORT, 'document artifact envelope and identity are immutable');
        END
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_pipeline_stage_document_receipt_immutable
        BEFORE UPDATE ON pipeline_stage_transitions
        WHEN OLD.artifact_id IS NOT NULL AND (
          NEW.artifact_id IS NOT OLD.artifact_id
          OR NEW.receipt_json IS NOT OLD.receipt_json
          OR NEW.receipt_sha256 IS NOT OLD.receipt_sha256
        )
        BEGIN
          SELECT RAISE(ABORT, 'document artifact stage receipt is immutable');
        END
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_llm_artifact_document_successor_restrict
        BEFORE DELETE ON llm_artifacts
        WHEN OLD.scope='document' AND EXISTS (
          SELECT 1 FROM llm_artifacts predecessor
          WHERE predecessor.scope='document' AND predecessor.superseded_by_id=OLD.id
        )
        BEGIN
          SELECT RAISE(ABORT, 'cannot delete a document successor and revive its predecessor');
        END
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_llm_artifact_document_supersession_exact
        BEFORE UPDATE OF superseded_by_id ON llm_artifacts
        WHEN NEW.superseded_by_id IS NOT NULL AND (
          (NEW.scope='document' AND (
            NEW.superseded_by_id <= NEW.id OR NOT EXISTS (
            SELECT 1 FROM llm_artifacts successor
            WHERE successor.id=NEW.superseded_by_id AND successor.scope='document'
              AND successor.id > NEW.id AND successor.superseded_by_id IS NULL
              AND successor.issuer_id=NEW.issuer_id
              AND successor.document_version_id=NEW.document_version_id
              AND successor.document_period_start IS NEW.document_period_start
              AND successor.document_period_end=NEW.document_period_end
              AND successor.document_period_type=NEW.document_period_type
              AND successor.document_fiscal_year=NEW.document_fiscal_year
              AND successor.document_fiscal_quarter=NEW.document_fiscal_quarter
              AND successor.purpose=NEW.purpose)))
          OR (NEW.scope<>'document' AND EXISTS (
            SELECT 1 FROM llm_artifacts successor
            WHERE successor.id=NEW.superseded_by_id AND successor.scope='document'))
        )
        BEGIN
          SELECT RAISE(ABORT, 'document artifacts can only supersede exact document products');
        END
        """
    )


def downgrade() -> None:
    raise RuntimeError(
        "Restore the reviewed predecessor code/database snapshot to remove document completion fields"
    )
