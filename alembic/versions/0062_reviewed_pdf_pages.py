"""Retain explicit source-bound human reviews of OCR-flagged PDF pages."""

from __future__ import annotations

from alembic import op

revision = "0062_reviewed_pdf_pages"
down_revision = "0061_bulk_lexical_projection_coverage"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "CREATE TABLE IF NOT EXISTS reviewed_pdf_page_revisions ("
        "review_id TEXT PRIMARY KEY,"
        "document_version_id TEXT NOT NULL REFERENCES evidence_document_versions(document_version_id),"
        "source_sha256 TEXT NOT NULL,"
        "assessment_id TEXT NOT NULL REFERENCES ocr_document_assessments(assessment_id),"
        "page_number INTEGER NOT NULL CHECK(page_number > 0),"
        "native_text_sha256 TEXT NOT NULL,"
        "render_sha256 TEXT NOT NULL,"
        "revision INTEGER NOT NULL CHECK(revision > 0),"
        "supersedes_review_id TEXT REFERENCES reviewed_pdf_page_revisions(review_id),"
        "disposition TEXT NOT NULL CHECK(disposition IN ('no_financial_content','requires_ocr')) ,"
        "reviewer TEXT NOT NULL CHECK(length(trim(reviewer)) > 0),"
        "reviewed_at TEXT NOT NULL,"
        "recorded_at TEXT NOT NULL,"
        "rationale TEXT NOT NULL CHECK(length(trim(rationale)) >= 20),"
        "commitment_sha256 TEXT NOT NULL,"
        "UNIQUE(document_version_id,assessment_id,page_number,revision),"
        "CHECK(length(source_sha256)=64 AND source_sha256 NOT GLOB '*[^0-9a-f]*'),"
        "CHECK(length(native_text_sha256)=64 AND native_text_sha256 NOT GLOB '*[^0-9a-f]*'),"
        "CHECK(length(render_sha256)=64 AND render_sha256 NOT GLOB '*[^0-9a-f]*'),"
        "CHECK(length(commitment_sha256)=64 AND commitment_sha256 NOT GLOB '*[^0-9a-f]*'),"
        "CHECK((revision=1 AND supersedes_review_id IS NULL) OR "
        "(revision>1 AND supersedes_review_id IS NOT NULL)),"
        "CHECK(datetime(reviewed_at) IS NOT NULL AND datetime(recorded_at) IS NOT NULL "
        "AND datetime(reviewed_at)<=datetime(recorded_at)))"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_reviewed_pdf_page_head "
        "ON reviewed_pdf_page_revisions(document_version_id,assessment_id,page_number,revision)"
    )
    op.execute(
        "CREATE TRIGGER IF NOT EXISTS trg_reviewed_pdf_page_source_binding "
        "BEFORE INSERT ON reviewed_pdf_page_revisions WHEN NOT EXISTS ("
        "SELECT 1 FROM ocr_document_assessments assessment "
        "JOIN ocr_preflight_pages page ON page.assessment_id=assessment.assessment_id "
        "JOIN evidence_document_versions version ON version.document_version_id=assessment.document_version_id "
        "WHERE assessment.assessment_id=NEW.assessment_id "
        "AND assessment.document_version_id=NEW.document_version_id "
        "AND assessment.input_sha256=NEW.source_sha256 "
        "AND version.blob_sha256=NEW.source_sha256 "
        "AND assessment.outcome='ocr_required' "
        "AND page.page_number=NEW.page_number AND page.requires_ocr=1 "
        "AND page.native_text_sha256=NEW.native_text_sha256) "
        "BEGIN SELECT RAISE(ABORT, 'reviewed PDF page source binding invalid'); END"
    )
    op.execute(
        "CREATE TRIGGER IF NOT EXISTS trg_reviewed_pdf_page_revision_chain "
        "BEFORE INSERT ON reviewed_pdf_page_revisions WHEN ("
        "(NEW.revision=1 AND EXISTS (SELECT 1 FROM reviewed_pdf_page_revisions prior "
        "WHERE prior.document_version_id=NEW.document_version_id "
        "AND prior.assessment_id=NEW.assessment_id AND prior.page_number=NEW.page_number)) "
        "OR (NEW.revision>1 AND NOT EXISTS ("
        "SELECT 1 FROM reviewed_pdf_page_revisions prior "
        "WHERE prior.review_id=NEW.supersedes_review_id "
        "AND prior.document_version_id=NEW.document_version_id "
        "AND prior.assessment_id=NEW.assessment_id AND prior.page_number=NEW.page_number "
        "AND prior.revision=NEW.revision-1 "
        "AND datetime(prior.recorded_at)<=datetime(NEW.reviewed_at) "
        "AND NOT EXISTS (SELECT 1 FROM reviewed_pdf_page_revisions newer "
        "WHERE newer.document_version_id=NEW.document_version_id "
        "AND newer.assessment_id=NEW.assessment_id AND newer.page_number=NEW.page_number "
        "AND newer.revision>prior.revision)))) "
        "BEGIN SELECT RAISE(ABORT, 'reviewed PDF page revision chain invalid'); END"
    )
    op.execute(
        "CREATE TRIGGER IF NOT EXISTS trg_reviewed_pdf_page_revisions_append_only "
        "BEFORE UPDATE ON reviewed_pdf_page_revisions "
        "BEGIN SELECT RAISE(ABORT, 'reviewed PDF pages are append-only'); END"
    )
    op.execute(
        "CREATE TRIGGER IF NOT EXISTS trg_reviewed_pdf_page_revisions_append_only_delete "
        "BEFORE DELETE ON reviewed_pdf_page_revisions "
        "BEGIN SELECT RAISE(ABORT, 'reviewed PDF pages are append-only'); END"
    )


def downgrade() -> None:
    row = (
        op.get_bind()
        .exec_driver_sql("SELECT 1 FROM reviewed_pdf_page_revisions LIMIT 1")
        .fetchone()
    )
    if row is not None:
        raise RuntimeError("reviewed PDF page history cannot be discarded")
    for name in (
        "trg_reviewed_pdf_page_revisions_append_only_delete",
        "trg_reviewed_pdf_page_revisions_append_only",
        "trg_reviewed_pdf_page_revision_chain",
        "trg_reviewed_pdf_page_source_binding",
    ):
        op.execute(f"DROP TRIGGER IF EXISTS {name}")
    op.execute("DROP INDEX IF EXISTS ix_reviewed_pdf_page_head")
    op.execute("DROP TABLE IF EXISTS reviewed_pdf_page_revisions")
