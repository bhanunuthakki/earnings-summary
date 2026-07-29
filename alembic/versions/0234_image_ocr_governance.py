"""Add append-only governance for evidence-native standalone image OCR."""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0234_image_ocr_governance"
down_revision: str | Sequence[str] | None = "0233_search_projection_seals"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLES = (
    "image_ocr_assessments",
    "image_ocr_extraction_governance",
    "image_ocr_results",
)


def _append_only(table: str) -> None:
    op.execute(
        f"CREATE TRIGGER trg_{table}_append_only BEFORE UPDATE ON {table} "
        "BEGIN SELECT RAISE(ABORT, 'image OCR governance is append-only'); END"
    )
    op.execute(
        f"CREATE TRIGGER trg_{table}_append_only_delete BEFORE DELETE ON {table} "
        "BEGIN SELECT RAISE(ABORT, 'image OCR governance is append-only'); END"
    )


def upgrade() -> None:
    op.create_table(
        "image_ocr_assessments",
        sa.Column("assessment_id", sa.String(128), primary_key=True),
        sa.Column("idempotency_key", sa.String(256), nullable=False, unique=True),
        sa.Column(
            "document_version_id",
            sa.String(128),
            sa.ForeignKey("evidence_document_versions.document_version_id"),
            nullable=False,
        ),
        sa.Column("input_sha256", sa.String(64), nullable=False),
        sa.Column("observed_sha256", sa.String(64), nullable=True),
        sa.Column("observed_byte_size", sa.Integer(), nullable=True),
        sa.Column("media_type", sa.String(32), nullable=False),
        sa.Column("width", sa.Integer(), nullable=True),
        sa.Column("height", sa.Integer(), nullable=True),
        sa.Column("pixel_count", sa.Integer(), nullable=True),
        sa.Column("page_count", sa.Integer(), nullable=False),
        sa.Column("detector_name", sa.String(128), nullable=False),
        sa.Column("detector_config_sha256", sa.String(64), nullable=False),
        sa.Column("detector_code_version", sa.String(255), nullable=False),
        sa.Column("outcome", sa.String(32), nullable=False),
        sa.Column("reason_code", sa.String(128), nullable=True),
        sa.Column("assessed_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "length(input_sha256) = 64 "
            "AND (observed_sha256 IS NULL OR length(observed_sha256) = 64) "
            "AND length(detector_config_sha256) = 64",
            name="ck_image_ocr_assessment_hashes",
        ),
        sa.CheckConstraint(
            "observed_byte_size IS NULL OR observed_byte_size >= 0",
            name="ck_image_ocr_assessment_bytes",
        ),
        sa.CheckConstraint(
            "media_type IN ('image/jpeg', 'image/png')",
            name="ck_image_ocr_assessment_media_type",
        ),
        sa.CheckConstraint(
            "(width IS NULL AND height IS NULL AND pixel_count IS NULL) OR "
            "(width > 0 AND height > 0 AND pixel_count = width * height)",
            name="ck_image_ocr_assessment_dimensions",
        ),
        sa.CheckConstraint(
            "outcome IN ('ocr_required', 'unsupported', 'unreadable', 'quarantined')",
            name="ck_image_ocr_assessment_outcome",
        ),
        sa.CheckConstraint(
            "(outcome = 'ocr_required' AND reason_code IS NULL "
            "AND page_count = 1 AND width IS NOT NULL) OR "
            "(outcome <> 'ocr_required' AND reason_code IS NOT NULL "
            "AND page_count IN (0, 1))",
            name="ck_image_ocr_assessment_shape",
        ),
    )
    op.create_index(
        "ix_image_ocr_assessment_document",
        "image_ocr_assessments",
        ["document_version_id", "assessed_at"],
    )
    op.create_table(
        "image_ocr_extraction_governance",
        sa.Column(
            "extraction_run_id",
            sa.String(128),
            sa.ForeignKey("evidence_extraction_runs.extraction_run_id"),
            primary_key=True,
        ),
        sa.Column(
            "assessment_id",
            sa.String(128),
            sa.ForeignKey("image_ocr_assessments.assessment_id"),
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
        sa.Column("recorded_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "length(engine_binary_sha256) = 64 "
            "AND length(model_manifest_sha256) = 64 "
            "AND length(extractor_config_sha256) = 64",
            name="ck_image_ocr_governance_hashes",
        ),
    )
    op.create_index(
        "ix_image_ocr_governance_assessment",
        "image_ocr_extraction_governance",
        ["assessment_id", "recorded_at"],
    )
    op.create_table(
        "image_ocr_results",
        sa.Column(
            "extraction_run_id",
            sa.String(128),
            sa.ForeignKey("image_ocr_extraction_governance.extraction_run_id"),
            primary_key=True,
        ),
        sa.Column("page_number", sa.Integer(), nullable=False),
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
        sa.CheckConstraint("page_number = 1", name="ck_image_ocr_result_page"),
        sa.CheckConstraint(
            "outcome IN ('accepted', 'quarantined', 'failed')",
            name="ck_image_ocr_result_outcome",
        ),
        sa.CheckConstraint(
            "length(locator_sha256) = 64 "
            "AND (output_sha256 IS NULL OR length(output_sha256) = 64)",
            name="ck_image_ocr_result_hashes",
        ),
        sa.CheckConstraint(
            "mean_confidence IS NULL OR "
            "(mean_confidence >= 0.0 AND mean_confidence <= 100.0)",
            name="ck_image_ocr_result_confidence",
        ),
        sa.CheckConstraint(
            "(outcome = 'accepted' AND node_id IS NOT NULL "
            "AND output_sha256 IS NOT NULL AND mean_confidence IS NOT NULL "
            "AND reason_code IS NULL) OR "
            "(outcome = 'quarantined' AND node_id IS NULL "
            "AND output_sha256 IS NOT NULL AND mean_confidence IS NOT NULL "
            "AND reason_code IS NOT NULL) OR "
            "(outcome = 'failed' AND node_id IS NULL "
            "AND output_sha256 IS NULL AND mean_confidence IS NULL "
            "AND reason_code IS NOT NULL)",
            name="ck_image_ocr_result_shape",
        ),
    )
    op.execute(
        "CREATE TRIGGER trg_image_ocr_assessment_idempotency BEFORE INSERT "
        "ON image_ocr_assessments WHEN NEW.idempotency_key <> NEW.assessment_id "
        "BEGIN SELECT RAISE(ABORT, 'image OCR assessment identity must be canonical'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_image_ocr_assessment_input_blob BEFORE INSERT "
        "ON image_ocr_assessments WHEN "
        "(SELECT blob_sha256 FROM evidence_document_versions "
        "WHERE document_version_id = NEW.document_version_id) <> NEW.input_sha256 "
        "BEGIN SELECT RAISE(ABORT, 'image OCR assessment input must match document bytes'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_image_ocr_governance_required_assessment BEFORE INSERT "
        "ON image_ocr_extraction_governance WHEN NOT EXISTS ("
        "SELECT 1 FROM image_ocr_assessments AS assessment "
        "JOIN evidence_extraction_runs AS run "
        "ON run.extraction_run_id = NEW.extraction_run_id "
        "WHERE assessment.assessment_id = NEW.assessment_id "
        "AND assessment.outcome = 'ocr_required' "
        "AND assessment.document_version_id = run.document_version_id "
        "AND assessment.input_sha256 = run.input_sha256 "
        "AND run.extractor_name = 'governed-image-ocr' "
        "AND run.extractor_config_sha256 = NEW.extractor_config_sha256) "
        "BEGIN SELECT RAISE(ABORT, 'image OCR run must match its required assessment'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_image_ocr_result_node_run BEFORE INSERT "
        "ON image_ocr_results WHEN NEW.node_id IS NOT NULL AND NOT EXISTS ("
        "SELECT 1 FROM evidence_nodes WHERE node_id = NEW.node_id "
        "AND extraction_run_id = NEW.extraction_run_id "
        "AND node_kind = 'passage') "
        "BEGIN SELECT RAISE(ABORT, 'image OCR node must be a passage in its run'); END"
    )
    for table in _TABLES:
        _append_only(table)


def downgrade() -> None:
    for table in reversed(_TABLES):
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_append_only")
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_append_only_delete")
    for trigger in (
        "trg_image_ocr_result_node_run",
        "trg_image_ocr_governance_required_assessment",
        "trg_image_ocr_assessment_input_blob",
        "trg_image_ocr_assessment_idempotency",
    ):
        op.execute(f"DROP TRIGGER IF EXISTS {trigger}")
    op.drop_index(
        "ix_image_ocr_governance_assessment",
        table_name="image_ocr_extraction_governance",
    )
    op.drop_table("image_ocr_results")
    op.drop_table("image_ocr_extraction_governance")
    op.drop_index(
        "ix_image_ocr_assessment_document",
        table_name="image_ocr_assessments",
    )
    op.drop_table("image_ocr_assessments")
