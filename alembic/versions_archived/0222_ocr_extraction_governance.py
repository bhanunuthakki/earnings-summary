"""Add append-only governance for deterministic PDF OCR extraction."""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0222_ocr_extraction_governance"
down_revision: str | Sequence[str] | None = "0221_ask_retrieval_traces"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLES = (
    "ocr_document_assessments",
    "ocr_preflight_pages",
    "ocr_extraction_governance",
    "ocr_page_results",
)


def _append_only(table: str) -> None:
    op.execute(
        f"CREATE TRIGGER trg_{table}_append_only BEFORE UPDATE ON {table} "
        "BEGIN SELECT RAISE(ABORT, 'OCR governance is append-only'); END"
    )
    op.execute(
        f"CREATE TRIGGER trg_{table}_append_only_delete BEFORE DELETE ON {table} "
        "BEGIN SELECT RAISE(ABORT, 'OCR governance is append-only'); END"
    )


def upgrade() -> None:
    op.create_table(
        "ocr_document_assessments",
        sa.Column("assessment_id", sa.String(128), primary_key=True),
        sa.Column("idempotency_key", sa.String(256), nullable=False, unique=True),
        sa.Column(
            "document_version_id",
            sa.String(128),
            sa.ForeignKey("evidence_document_versions.document_version_id"),
            nullable=False,
        ),
        sa.Column("input_sha256", sa.String(64), nullable=False),
        sa.Column("detector_name", sa.String(128), nullable=False),
        sa.Column("detector_config_sha256", sa.String(64), nullable=False),
        sa.Column("detector_code_version", sa.String(255), nullable=False),
        sa.Column("native_output_sha256", sa.String(64), nullable=False),
        sa.Column("page_count", sa.Integer(), nullable=False),
        sa.Column("outcome", sa.String(32), nullable=False),
        sa.Column("reason_code", sa.String(128), nullable=True),
        sa.Column("assessed_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint(
            "document_version_id",
            "input_sha256",
            "detector_config_sha256",
            "detector_code_version",
            name="uq_ocr_document_assessment_semantic",
        ),
        sa.CheckConstraint(
            "length(input_sha256) = 64 AND length(detector_config_sha256) = 64 "
            "AND length(native_output_sha256) = 64",
            name="ck_ocr_document_assessment_hashes",
        ),
        sa.CheckConstraint("page_count >= 0", name="ck_ocr_document_assessment_page_count"),
        sa.CheckConstraint(
            "outcome IN ('native_sufficient', 'ocr_required', 'encrypted', "
            "'unreadable', 'unsupported')",
            name="ck_ocr_document_assessment_outcome",
        ),
        sa.CheckConstraint(
            "(outcome IN ('native_sufficient', 'ocr_required') AND reason_code IS NULL) "
            "OR (outcome IN ('encrypted', 'unreadable', 'unsupported') "
            "AND reason_code IS NOT NULL)",
            name="ck_ocr_document_assessment_reason",
        ),
    )
    op.create_table(
        "ocr_preflight_pages",
        sa.Column(
            "assessment_id",
            sa.String(128),
            sa.ForeignKey("ocr_document_assessments.assessment_id"),
            primary_key=True,
        ),
        sa.Column("page_number", sa.Integer(), primary_key=True),
        sa.Column("native_character_count", sa.Integer(), nullable=False),
        sa.Column("native_text_sha256", sa.String(64), nullable=False),
        sa.Column("requires_ocr", sa.Boolean(), nullable=False),
        sa.CheckConstraint("page_number > 0", name="ck_ocr_preflight_page_number"),
        sa.CheckConstraint(
            "native_character_count >= 0", name="ck_ocr_preflight_native_characters"
        ),
        sa.CheckConstraint(
            "length(native_text_sha256) = 64", name="ck_ocr_preflight_text_hash"
        ),
    )
    op.create_table(
        "ocr_extraction_governance",
        sa.Column(
            "extraction_run_id",
            sa.String(128),
            sa.ForeignKey("evidence_extraction_runs.extraction_run_id"),
            primary_key=True,
        ),
        sa.Column(
            "assessment_id",
            sa.String(128),
            sa.ForeignKey("ocr_document_assessments.assessment_id"),
            nullable=False,
        ),
        sa.Column("engine_name", sa.String(128), nullable=False),
        sa.Column("engine_version", sa.String(255), nullable=False),
        sa.Column("engine_binary_sha256", sa.String(64), nullable=False),
        sa.Column("model_name", sa.String(128), nullable=False),
        sa.Column("model_version", sa.String(255), nullable=False),
        sa.Column("model_manifest_sha256", sa.String(64), nullable=False),
        sa.Column("model_artifacts_json", sa.Text(), nullable=False),
        sa.Column("languages_json", sa.Text(), nullable=False),
        sa.Column("engine_config_json", sa.Text(), nullable=False),
        sa.Column("extractor_config_sha256", sa.String(64), nullable=False),
        sa.Column("renderer_name", sa.String(128), nullable=False),
        sa.Column("renderer_version", sa.String(255), nullable=False),
        sa.Column("renderer_binary_sha256", sa.String(64), nullable=False),
        sa.Column("recorded_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "length(engine_binary_sha256) = 64 "
            "AND length(model_manifest_sha256) = 64 "
            "AND length(renderer_binary_sha256) = 64 "
            "AND length(extractor_config_sha256) = 64",
            name="ck_ocr_governance_hashes",
        ),
    )
    op.create_table(
        "ocr_page_results",
        sa.Column(
            "extraction_run_id",
            sa.String(128),
            sa.ForeignKey("ocr_extraction_governance.extraction_run_id"),
            primary_key=True,
        ),
        sa.Column("page_number", sa.Integer(), primary_key=True),
        sa.Column(
            "node_id",
            sa.String(128),
            sa.ForeignKey("evidence_nodes.node_id"),
            nullable=True,
            unique=True,
        ),
        sa.Column("outcome", sa.String(32), nullable=False),
        sa.Column("output_sha256", sa.String(64), nullable=True),
        sa.Column("mean_confidence", sa.Float(), nullable=True),
        sa.Column("locator_json", sa.Text(), nullable=False),
        sa.Column("locator_sha256", sa.String(64), nullable=False),
        sa.Column("reason_code", sa.String(128), nullable=True),
        sa.Column("recorded_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("page_number > 0", name="ck_ocr_page_result_page"),
        sa.CheckConstraint(
            "outcome IN ('accepted', 'quarantined', 'failed')",
            name="ck_ocr_page_result_outcome",
        ),
        sa.CheckConstraint(
            "length(locator_sha256) = 64 AND "
            "(output_sha256 IS NULL OR length(output_sha256) = 64)",
            name="ck_ocr_page_result_hashes",
        ),
        sa.CheckConstraint(
            "mean_confidence IS NULL OR "
            "(mean_confidence >= 0.0 AND mean_confidence <= 100.0)",
            name="ck_ocr_page_result_confidence",
        ),
        sa.CheckConstraint(
            "(outcome = 'accepted' AND node_id IS NOT NULL "
            "AND output_sha256 IS NOT NULL AND mean_confidence IS NOT NULL "
            "AND reason_code IS NULL) "
            "OR (outcome = 'quarantined' AND node_id IS NULL "
            "AND output_sha256 IS NOT NULL AND mean_confidence IS NOT NULL "
            "AND reason_code IS NOT NULL) "
            "OR (outcome = 'failed' AND node_id IS NULL "
            "AND output_sha256 IS NULL AND mean_confidence IS NULL "
            "AND reason_code IS NOT NULL)",
            name="ck_ocr_page_result_shape",
        ),
    )
    op.create_index(
        "ix_ocr_assessment_document",
        "ocr_document_assessments",
        ["document_version_id", "assessed_at"],
    )
    op.create_index(
        "ix_ocr_governance_assessment",
        "ocr_extraction_governance",
        ["assessment_id", "recorded_at"],
    )

    op.execute(
        "CREATE TRIGGER trg_ocr_assessment_input_blob BEFORE INSERT "
        "ON ocr_document_assessments WHEN "
        "(SELECT blob_sha256 FROM evidence_document_versions "
        "WHERE document_version_id = NEW.document_version_id) <> NEW.input_sha256 "
        "BEGIN SELECT RAISE(ABORT, 'OCR assessment input must match document bytes'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_ocr_governance_required_assessment BEFORE INSERT "
        "ON ocr_extraction_governance WHEN NOT EXISTS ("
        "SELECT 1 FROM ocr_document_assessments AS assessment "
        "JOIN evidence_extraction_runs AS run "
        "ON run.extraction_run_id = NEW.extraction_run_id "
        "WHERE assessment.assessment_id = NEW.assessment_id "
        "AND assessment.outcome = 'ocr_required' "
        "AND assessment.document_version_id = run.document_version_id "
        "AND assessment.input_sha256 = run.input_sha256 "
        "AND run.extractor_name = 'governed-pdf-ocr' "
        "AND run.extractor_config_sha256 = NEW.extractor_config_sha256) "
        "BEGIN SELECT RAISE(ABORT, 'OCR run must match an OCR-required assessment'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_ocr_page_preflight_membership BEFORE INSERT "
        "ON ocr_page_results WHEN NOT EXISTS ("
        "SELECT 1 FROM ocr_extraction_governance AS governance "
        "JOIN ocr_preflight_pages AS page "
        "ON page.assessment_id = governance.assessment_id "
        "WHERE governance.extraction_run_id = NEW.extraction_run_id "
        "AND page.page_number = NEW.page_number AND page.requires_ocr = 1) "
        "BEGIN SELECT RAISE(ABORT, 'OCR result page must be required by preflight'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_ocr_page_node_run BEFORE INSERT ON ocr_page_results "
        "WHEN NEW.node_id IS NOT NULL AND NOT EXISTS ("
        "SELECT 1 FROM evidence_nodes WHERE node_id = NEW.node_id "
        "AND extraction_run_id = NEW.extraction_run_id AND node_kind = 'pdf_page') "
        "BEGIN SELECT RAISE(ABORT, 'OCR page node must belong to its extraction run'); END"
    )
    for table in _TABLES:
        _append_only(table)


def downgrade() -> None:
    for table in reversed(_TABLES):
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_append_only")
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_append_only_delete")
    for trigger in (
        "trg_ocr_page_node_run",
        "trg_ocr_page_preflight_membership",
        "trg_ocr_governance_required_assessment",
        "trg_ocr_assessment_input_blob",
    ):
        op.execute(f"DROP TRIGGER IF EXISTS {trigger}")
    op.drop_index("ix_ocr_governance_assessment", table_name="ocr_extraction_governance")
    op.drop_index("ix_ocr_assessment_document", table_name="ocr_document_assessments")
    for table in reversed(_TABLES):
        op.drop_table(table)
