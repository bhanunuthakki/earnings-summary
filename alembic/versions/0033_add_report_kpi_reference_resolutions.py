"""Track append-only dispositions for report KPI references.

Revision ID: 0033_add_report_kpi_reference_resolutions
Revises: 0032_allow_source_reviewed_kpi_supersessions
"""

from __future__ import annotations

from alembic import op

revision = "0033_add_report_kpi_reference_resolutions"
down_revision = "0032_allow_source_reviewed_kpi_supersessions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE report_kpi_reference_resolution_revisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL CHECK(length(trim(user_id)) > 0),
            ticker TEXT NOT NULL CHECK(length(trim(ticker)) > 0),
            source_path TEXT NOT NULL CHECK(length(trim(source_path)) > 0),
            json_pointer TEXT NOT NULL CHECK(length(trim(json_pointer)) > 0),
            reference_kind TEXT NOT NULL CHECK(reference_kind IN
                ('chart_priority','tier_1_kpi','tier_2_kpi','tier_3_kpi','break_rule')),
            requested_label TEXT NOT NULL CHECK(length(trim(requested_label)) > 0),
            reference_content_sha256 TEXT NOT NULL
                CHECK(length(reference_content_sha256)=64
                  AND reference_content_sha256 NOT GLOB '*[^0-9a-f]*'),
            status TEXT NOT NULL CHECK(status='unresolved'),
            kpi_definition_id INTEGER REFERENCES kpi_definitions(id) CHECK(kpi_definition_id IS NULL),
            reason_code TEXT NOT NULL CHECK(length(trim(reason_code)) > 0),
            revision INTEGER NOT NULL CHECK(revision > 0),
            supersedes_resolution_id INTEGER UNIQUE
                REFERENCES report_kpi_reference_resolution_revisions(id),
            reviewed_by TEXT NOT NULL CHECK(length(trim(reviewed_by)) > 0),
            knowledge_at TEXT NOT NULL CHECK(length(trim(knowledge_at)) > 0),
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
            UNIQUE(user_id,ticker,source_path,json_pointer,revision),
            CHECK((revision=1 AND supersedes_resolution_id IS NULL)
               OR (revision>1 AND supersedes_resolution_id IS NOT NULL)),
            CHECK(status='unresolved' AND kpi_definition_id IS NULL)
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_report_kpi_reference_resolution_current ON "
        "report_kpi_reference_resolution_revisions(user_id,ticker,source_path,json_pointer,revision)"
    )
    op.execute(
        "CREATE TRIGGER trg_report_kpi_reference_resolution_predecessor BEFORE INSERT ON "
        "report_kpi_reference_resolution_revisions WHEN NEW.revision > 1 BEGIN "
        "SELECT CASE WHEN NOT EXISTS (SELECT 1 FROM report_kpi_reference_resolution_revisions prior "
        "WHERE prior.id=NEW.supersedes_resolution_id AND prior.user_id=NEW.user_id "
        "AND prior.ticker=NEW.ticker AND prior.source_path=NEW.source_path "
        "AND prior.json_pointer=NEW.json_pointer AND prior.revision=NEW.revision-1) "
        "THEN RAISE(ABORT,'report KPI reference predecessor mismatch') END; END"
    )
    op.execute(
        "CREATE TRIGGER trg_report_kpi_reference_resolution_no_update BEFORE UPDATE ON "
        "report_kpi_reference_resolution_revisions BEGIN SELECT RAISE(ABORT,"
        "'report KPI reference resolutions are append-only'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_report_kpi_reference_resolution_no_delete BEFORE DELETE ON "
        "report_kpi_reference_resolution_revisions BEGIN SELECT RAISE(ABORT,"
        "'report KPI reference resolutions are append-only'); END"
    )
    op.execute(
        """
        CREATE TABLE kpi_semantic_disposition_commits (
            manifest_sha256 TEXT PRIMARY KEY
                CHECK(length(manifest_sha256)=64 AND manifest_sha256 NOT GLOB '*[^0-9a-f]*'),
            logical_idempotency_key_sha256 TEXT NOT NULL UNIQUE
                CHECK(length(logical_idempotency_key_sha256)=64
                  AND logical_idempotency_key_sha256 NOT GLOB '*[^0-9a-f]*'),
            review_bundle_sha256 TEXT NOT NULL
                CHECK(length(review_bundle_sha256)=64
                  AND review_bundle_sha256 NOT GLOB '*[^0-9a-f]*'),
            backup_restore_evidence_id TEXT NOT NULL
                CHECK(length(backup_restore_evidence_id)=64
                  AND backup_restore_evidence_id NOT GLOB '*[^0-9a-f]*'),
            executor_code_sha256 TEXT NOT NULL
                CHECK(length(executor_code_sha256)=64
                  AND executor_code_sha256 NOT GLOB '*[^0-9a-f]*'),
            fact_disposition_count INTEGER NOT NULL CHECK(fact_disposition_count >= 0),
            reference_disposition_count INTEGER NOT NULL CHECK(reference_disposition_count >= 0),
            inserted_context_rows INTEGER NOT NULL CHECK(inserted_context_rows >= 0),
            inserted_reference_rows INTEGER NOT NULL CHECK(inserted_reference_rows >= 0),
            committed_at TEXT NOT NULL CHECK(length(trim(committed_at)) > 0)
        )
        """
    )
    op.execute(
        "CREATE TRIGGER trg_kpi_semantic_disposition_commits_no_update BEFORE UPDATE ON "
        "kpi_semantic_disposition_commits BEGIN SELECT RAISE(ABORT,"
        "'KPI semantic disposition commits are append-only'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_kpi_semantic_disposition_commits_no_delete BEFORE DELETE ON "
        "kpi_semantic_disposition_commits BEGIN SELECT RAISE(ABORT,"
        "'KPI semantic disposition commits are append-only'); END"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_kpi_semantic_disposition_commits_no_delete")
    op.execute("DROP TRIGGER IF EXISTS trg_kpi_semantic_disposition_commits_no_update")
    op.execute("DROP TABLE IF EXISTS kpi_semantic_disposition_commits")
    op.execute("DROP TRIGGER IF EXISTS trg_report_kpi_reference_resolution_no_delete")
    op.execute("DROP TRIGGER IF EXISTS trg_report_kpi_reference_resolution_no_update")
    op.execute("DROP TRIGGER IF EXISTS trg_report_kpi_reference_resolution_predecessor")
    op.execute("DROP INDEX IF EXISTS ix_report_kpi_reference_resolution_current")
    op.execute("DROP TABLE IF EXISTS report_kpi_reference_resolution_revisions")
