"""Add sealed, native-row-backed document-processing evidence publications.

Revision ID: 0248_native_processing_closure_adapters
Revises: 0247_bounded_canonical_retrieval
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0248_native_processing_closure_adapters"
down_revision: str | Sequence[str] | None = "0247_bounded_canonical_retrieval"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLES = (
    "document_processing_evidence_headers",
    "document_processing_evidence_members",
    "document_processing_evidence_seals",
)
_PDF_TABLES = (
    "pdf_table_extraction_artifact_headers",
    "pdf_table_extraction_artifact_members",
    "pdf_table_extraction_artifact_seals",
)


def _hex(column: str) -> str:
    return f"length({column})=64 AND {column} NOT GLOB '*[^0-9a-f]*'"


def _append_only(table: str) -> None:
    for event in ("UPDATE", "DELETE"):
        op.execute(
            f"CREATE TRIGGER trg_{table}_{event.lower()}_append_only "
            f"BEFORE {event} ON {table} BEGIN SELECT RAISE(ABORT, "
            f"'{table} is append-only'); END"
        )


def _exact_json(table: str, json_column: str, sha_column: str) -> None:
    op.execute(
        f"CREATE TRIGGER trg_{table}_{sha_column}_exact BEFORE INSERT ON {table} "
        f"WHEN NEW.{sha_column} <> fact_sha256(NEW.{json_column}) "
        f"BEGIN SELECT RAISE(ABORT, '{table} commitment mismatch'); END"
    )


def upgrade() -> None:
    required = {
        "evidence_content_blobs",
        "evidence_document_versions",
        "evidence_extraction_runs",
        "evidence_nodes",
        "ocr_document_assessments",
        "ocr_preflight_pages",
        "ocr_extraction_governance",
        "ocr_page_results",
        "image_ocr_assessments",
        "image_ocr_extraction_governance",
        "image_ocr_results",
    }
    existing = set(sa.inspect(op.get_bind()).get_table_names())
    missing = sorted(required - existing)
    if missing:
        raise RuntimeError(
            "native processing closure requires governed evidence tables: " + ", ".join(missing)
        )

    op.create_table(
        "pdf_table_extraction_artifact_headers",
        sa.Column("artifact_id", sa.String(128), primary_key=True),
        sa.Column(
            "document_version_id",
            sa.String(128),
            sa.ForeignKey("evidence_document_versions.document_version_id"),
            nullable=False,
        ),
        sa.Column(
            "extraction_run_id",
            sa.String(128),
            sa.ForeignKey("evidence_extraction_runs.extraction_run_id"),
            nullable=False,
            unique=True,
        ),
        sa.Column("schema_version", sa.String(64), nullable=False),
        sa.Column("disposition", sa.String(32), nullable=False),
        sa.Column("quarantine_reason", sa.String(128), nullable=True),
        sa.Column("raw_pdf_sha256", sa.String(64), nullable=False),
        sa.Column("raw_byte_count", sa.Integer, nullable=False),
        sa.Column("pdf_page_count", sa.Integer, nullable=True),
        sa.Column("detector_name", sa.String(128), nullable=False),
        sa.Column("detector_version", sa.String(128), nullable=False),
        sa.Column("pymupdf_version", sa.String(64), nullable=False),
        sa.Column("mupdf_version", sa.String(64), nullable=False),
        sa.Column("extractor_code_version", sa.String(255), nullable=False),
        sa.Column("detector_config_json", sa.Text, nullable=False),
        sa.Column("detector_config_sha256", sa.String(64), nullable=False),
        sa.Column("detector_identity_sha256", sa.String(64), nullable=False),
        sa.Column("ordered_page_table_seal_sha256", sa.String(64), nullable=False),
        sa.Column("artifact_json", sa.Text, nullable=False),
        sa.Column("artifact_sha256", sa.String(64), nullable=False),
        sa.Column("recorded_at", sa.DateTime, nullable=False),
        sa.CheckConstraint(
            "schema_version='pdf-table-extraction@1' "
            "AND detector_name='PyMuPDF.Page.find_tables' "
            "AND detector_version='pymupdf-dual-table-detector@1'",
            name="ck_pdf_table_artifact_identity",
        ),
        sa.CheckConstraint(
            "(disposition='sealed' AND quarantine_reason IS NULL "
            "AND pdf_page_count IS NOT NULL) OR "
            "(disposition='quarantined' AND quarantine_reason IS NOT NULL)",
            name="ck_pdf_table_artifact_disposition",
        ),
        sa.CheckConstraint(
            "raw_byte_count>=0 AND (pdf_page_count IS NULL OR pdf_page_count>=0) AND "
            + _hex("raw_pdf_sha256")
            + " AND "
            + _hex("detector_config_sha256")
            + " AND "
            + _hex("detector_identity_sha256")
            + " AND "
            + _hex("ordered_page_table_seal_sha256")
            + " AND "
            + _hex("artifact_sha256"),
            name="ck_pdf_table_artifact_hashes",
        ),
        sa.CheckConstraint(
            "json_valid(detector_config_json) "
            "AND json_type(detector_config_json)='object' "
            "AND json_valid(artifact_json) "
            "AND json_type(artifact_json)='object'",
            name="ck_pdf_table_artifact_json",
        ),
    )
    op.create_table(
        "pdf_table_extraction_artifact_members",
        sa.Column(
            "artifact_id",
            sa.String(128),
            sa.ForeignKey("pdf_table_extraction_artifact_headers.artifact_id"),
            primary_key=True,
        ),
        sa.Column("member_ordinal", sa.Integer, primary_key=True),
        sa.Column("member_kind", sa.String(16), nullable=False),
        sa.Column("native_id", sa.String(512), nullable=False),
        sa.Column("native_parent_id", sa.String(512), nullable=True),
        sa.Column("locator_json", sa.Text, nullable=False),
        sa.Column("locator_sha256", sa.String(64), nullable=False),
        sa.Column("disposition", sa.String(32), nullable=True),
        sa.Column("canonical_object_json", sa.Text, nullable=False),
        sa.Column("canonical_object_sha256", sa.String(64), nullable=False),
        sa.Column("recorded_at", sa.DateTime, nullable=False),
        sa.UniqueConstraint(
            "artifact_id",
            "native_id",
            name="uq_pdf_table_artifact_native_member",
        ),
        sa.CheckConstraint(
            "member_ordinal>=0 "
            "AND member_kind IN ('page','table','row','cell') "
            "AND ((member_kind='page' AND disposition IN "
            "('tables_detected','no_tables_detected','quarantined')) "
            "OR (member_kind<>'page' AND disposition IS NULL)) "
            "AND json_valid(locator_json) AND json_type(locator_json)='object' "
            "AND json_valid(canonical_object_json) "
            "AND json_type(canonical_object_json)='object' AND "
            + _hex("locator_sha256")
            + " AND "
            + _hex("canonical_object_sha256"),
            name="ck_pdf_table_artifact_member",
        ),
    )
    op.create_table(
        "pdf_table_extraction_artifact_seals",
        sa.Column(
            "artifact_id",
            sa.String(128),
            sa.ForeignKey("pdf_table_extraction_artifact_headers.artifact_id"),
            primary_key=True,
        ),
        sa.Column("member_count", sa.Integer, nullable=False),
        sa.Column("canonical_member_set_json", sa.Text, nullable=False),
        sa.Column("member_set_sha256", sa.String(64), nullable=False),
        sa.Column("sealed_at", sa.DateTime, nullable=False),
        sa.CheckConstraint(
            "member_count>=0 AND json_valid(canonical_member_set_json) "
            "AND json_type(canonical_member_set_json)='array' AND "
            + _hex("member_set_sha256"),
            name="ck_pdf_table_artifact_seal",
        ),
    )

    op.create_table(
        "document_processing_evidence_headers",
        sa.Column("evidence_seal_id", sa.String(128), primary_key=True),
        sa.Column("idempotency_key", sa.String(256), nullable=False, unique=True),
        sa.Column(
            "document_version_id",
            sa.String(128),
            sa.ForeignKey("evidence_document_versions.document_version_id"),
            nullable=False,
        ),
        sa.Column("processing_lane", sa.String(64), nullable=False),
        sa.Column(
            "extraction_run_id",
            sa.String(128),
            sa.ForeignKey("evidence_extraction_runs.extraction_run_id"),
            nullable=False,
        ),
        sa.Column("assessment_table", sa.String(64), nullable=True),
        sa.Column("assessment_id", sa.String(128), nullable=True),
        sa.Column("adapter_name", sa.String(128), nullable=False),
        sa.Column("adapter_version", sa.String(64), nullable=False),
        sa.Column("adapter_config_sha256", sa.String(64), nullable=False),
        sa.Column("input_blob_sha256", sa.String(64), nullable=False),
        sa.Column("native_output_sha256", sa.String(64), nullable=False),
        sa.Column("native_scope_json", sa.Text, nullable=False),
        sa.Column("native_scope_sha256", sa.String(64), nullable=False),
        sa.Column("canonical_header_json", sa.Text, nullable=False),
        sa.Column("header_sha256", sa.String(64), nullable=False),
        sa.Column("cutoff_at", sa.DateTime, nullable=False),
        sa.Column("knowledge_at", sa.DateTime, nullable=False),
        sa.Column("recorded_at", sa.DateTime, nullable=False),
        sa.UniqueConstraint(
            "document_version_id",
            "processing_lane",
            "cutoff_at",
            name="uq_document_processing_evidence_coordinate",
        ),
        sa.CheckConstraint(
            "processing_lane IN ("
            "'html_native_hierarchy','pdf_text','pdf_ocr','pdf_table','image_ocr',"
            "'pptx_slides','pptx_charts','pptx_tables',"
            "'xlsx_workbook','xlsx_sheets','xlsx_tables',"
            "'transcript_turns','transcript_speakers')",
            name="ck_document_processing_evidence_lane",
        ),
        sa.CheckConstraint(
            "(assessment_table IS NULL AND assessment_id IS NULL) OR "
            "(assessment_table IN ('ocr_document_assessments',"
            "'image_ocr_assessments','pdf_table_extraction_artifact_headers') "
            "AND assessment_id IS NOT NULL)",
            name="ck_document_processing_evidence_assessment",
        ),
        sa.CheckConstraint(
            _hex("adapter_config_sha256")
            + " AND "
            + _hex("input_blob_sha256")
            + " AND "
            + _hex("native_output_sha256")
            + " AND "
            + _hex("native_scope_sha256")
            + " AND "
            + _hex("header_sha256"),
            name="ck_document_processing_evidence_header_hashes",
        ),
        sa.CheckConstraint(
            "json_valid(native_scope_json) "
            "AND json_type(native_scope_json)='object' "
            "AND json_valid(canonical_header_json) "
            "AND json_type(canonical_header_json)='object'",
            name="ck_document_processing_evidence_header_json",
        ),
        sa.CheckConstraint(
            "knowledge_at <= cutoff_at AND cutoff_at <= recorded_at",
            name="ck_document_processing_evidence_header_clocks",
        ),
    )
    op.create_index(
        "ix_document_processing_evidence_coordinate",
        "document_processing_evidence_headers",
        ["document_version_id", "processing_lane", "cutoff_at"],
    )

    op.create_table(
        "document_processing_evidence_members",
        sa.Column(
            "evidence_seal_id",
            sa.String(128),
            sa.ForeignKey("document_processing_evidence_headers.evidence_seal_id"),
            primary_key=True,
        ),
        sa.Column("member_ordinal", sa.Integer, primary_key=True),
        sa.Column("native_table", sa.String(64), nullable=False),
        sa.Column("native_id", sa.String(256), nullable=False),
        sa.Column("native_parent_id", sa.String(256), nullable=True),
        sa.Column("locator_json", sa.Text, nullable=False),
        sa.Column("locator_sha256", sa.String(64), nullable=False),
        sa.Column("content_sha256", sa.String(64), nullable=False),
        sa.Column("native_commitment_json", sa.Text, nullable=False),
        sa.Column("native_commitment_sha256", sa.String(64), nullable=False),
        sa.Column("canonical_member_json", sa.Text, nullable=False),
        sa.Column("member_sha256", sa.String(64), nullable=False),
        sa.Column("native_knowledge_at", sa.DateTime, nullable=False),
        sa.Column("native_recorded_at", sa.DateTime, nullable=False),
        sa.UniqueConstraint(
            "evidence_seal_id",
            "native_table",
            "native_id",
            name="uq_document_processing_evidence_native_member",
        ),
        sa.CheckConstraint(
            "member_ordinal >= 0 AND "
            + _hex("locator_sha256")
            + " AND "
            + _hex("content_sha256")
            + " AND "
            + _hex("native_commitment_sha256")
            + " AND "
            + _hex("member_sha256")
            + " AND json_valid(locator_json) "
            "AND json_type(locator_json)='object' "
            "AND json_valid(native_commitment_json) "
            "AND json_type(native_commitment_json)='object' "
            "AND json_valid(canonical_member_json) "
            "AND json_type(canonical_member_json)='object' "
            "AND native_knowledge_at <= native_recorded_at",
            name="ck_document_processing_evidence_member",
        ),
    )

    op.create_table(
        "document_processing_evidence_seals",
        sa.Column(
            "evidence_seal_id",
            sa.String(128),
            sa.ForeignKey("document_processing_evidence_headers.evidence_seal_id"),
            primary_key=True,
        ),
        sa.Column("member_count", sa.Integer, nullable=False),
        sa.Column("canonical_member_set_json", sa.Text, nullable=False),
        sa.Column("member_set_sha256", sa.String(64), nullable=False),
        sa.Column("sealed_at", sa.DateTime, nullable=False),
        sa.CheckConstraint(
            "member_count >= 0 AND json_valid(canonical_member_set_json) "
            "AND json_type(canonical_member_set_json)='array' AND " + _hex("member_set_sha256"),
            name="ck_document_processing_evidence_seal",
        ),
    )

    for table in (*_PDF_TABLES, *_TABLES):
        _append_only(table)
    for table, json_column, sha_column in (
        (
            "document_processing_evidence_headers",
            "native_scope_json",
            "native_scope_sha256",
        ),
        (
            "document_processing_evidence_headers",
            "canonical_header_json",
            "header_sha256",
        ),
        (
            "document_processing_evidence_members",
            "locator_json",
            "locator_sha256",
        ),
        (
            "document_processing_evidence_members",
            "native_commitment_json",
            "native_commitment_sha256",
        ),
        (
            "document_processing_evidence_members",
            "canonical_member_json",
            "member_sha256",
        ),
        (
            "pdf_table_extraction_artifact_headers",
            "detector_config_json",
            "detector_config_sha256",
        ),
        (
            "pdf_table_extraction_artifact_headers",
            "artifact_json",
            "artifact_sha256",
        ),
        (
            "pdf_table_extraction_artifact_members",
            "locator_json",
            "locator_sha256",
        ),
        (
            "pdf_table_extraction_artifact_members",
            "canonical_object_json",
            "canonical_object_sha256",
        ),
    ):
        _exact_json(table, json_column, sha_column)

    op.execute(
        "CREATE TRIGGER trg_pdf_table_artifact_native_parent "
        "BEFORE INSERT ON pdf_table_extraction_artifact_headers WHEN "
        "NOT EXISTS (SELECT 1 FROM evidence_extraction_runs run "
        "JOIN evidence_document_versions document "
        "ON document.document_version_id=run.document_version_id "
        "JOIN evidence_content_blobs blob ON blob.sha256=document.blob_sha256 "
        "WHERE run.extraction_run_id=NEW.extraction_run_id "
        "AND run.document_version_id=NEW.document_version_id "
        "AND run.input_sha256=NEW.raw_pdf_sha256 "
        "AND run.output_sha256=NEW.ordered_page_table_seal_sha256 "
        "AND run.extractor_name=NEW.detector_name "
        "AND run.extractor_code_version=NEW.extractor_code_version "
        "AND run.extractor_config_sha256=NEW.detector_config_sha256 "
        "AND run.outcome='succeeded' "
        "AND document.blob_sha256=NEW.raw_pdf_sha256 "
        "AND blob.byte_size=NEW.raw_byte_count "
        "AND datetime(run.completed_at)<=datetime(NEW.recorded_at)) "
        "BEGIN SELECT RAISE(ABORT, "
        "'PDF table artifact does not match native run/document bytes'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_pdf_table_artifact_member_unsealed "
        "BEFORE INSERT ON pdf_table_extraction_artifact_members WHEN EXISTS ("
        "SELECT 1 FROM pdf_table_extraction_artifact_seals seal "
        "WHERE seal.artifact_id=NEW.artifact_id) "
        "BEGIN SELECT RAISE(ABORT, 'PDF table artifact is sealed'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_pdf_table_artifact_final_seal "
        "BEFORE INSERT ON pdf_table_extraction_artifact_seals WHEN "
        "NEW.member_count <> (SELECT COUNT(*) "
        "FROM pdf_table_extraction_artifact_members member "
        "WHERE member.artifact_id=NEW.artifact_id) "
        "OR (NEW.member_count > 0 AND NEW.member_count <> 1 + ("
        "SELECT MAX(member_ordinal) "
        "FROM pdf_table_extraction_artifact_members member "
        "WHERE member.artifact_id=NEW.artifact_id)) "
        "OR NEW.canonical_member_set_json <> COALESCE(("
        "SELECT json_group_array(json(canonical_member_json)) FROM ("
        "SELECT json_object("
        "'canonical_object_sha256',canonical_object_sha256,"
        "'disposition',disposition,"
        "'locator_sha256',locator_sha256,"
        "'member_kind',member_kind,"
        "'member_ordinal',member_ordinal,"
        "'native_id',native_id,"
        "'native_parent_id',native_parent_id) AS canonical_member_json "
        "FROM pdf_table_extraction_artifact_members "
        "WHERE artifact_id=NEW.artifact_id ORDER BY member_ordinal)), '[]') "
        "OR NEW.member_set_sha256<>fact_sha256(NEW.canonical_member_set_json) "
        "OR datetime(NEW.sealed_at)<datetime((SELECT recorded_at "
        "FROM pdf_table_extraction_artifact_headers header "
        "WHERE header.artifact_id=NEW.artifact_id)) "
        "BEGIN SELECT RAISE(ABORT, 'PDF table artifact final seal mismatch'); END"
    )

    op.execute(
        "CREATE TRIGGER trg_document_processing_evidence_header_native_parent "
        "BEFORE INSERT ON document_processing_evidence_headers WHEN "
        "NOT EXISTS (SELECT 1 FROM evidence_extraction_runs run "
        "JOIN evidence_document_versions document "
        "ON document.document_version_id=run.document_version_id "
        "WHERE run.extraction_run_id=NEW.extraction_run_id "
        "AND run.document_version_id=NEW.document_version_id "
        "AND run.input_sha256=NEW.input_blob_sha256 "
        "AND run.output_sha256=NEW.native_output_sha256 "
        "AND run.outcome='succeeded' "
        "AND datetime(run.completed_at)<=datetime(NEW.cutoff_at) "
        "AND datetime(document.recorded_at)<=datetime(NEW.recorded_at)) "
        "BEGIN SELECT RAISE(ABORT, "
        "'processing evidence header does not match its native run'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_document_processing_evidence_member_unsealed "
        "BEFORE INSERT ON document_processing_evidence_members WHEN EXISTS ("
        "SELECT 1 FROM document_processing_evidence_seals seal "
        "WHERE seal.evidence_seal_id=NEW.evidence_seal_id) "
        "BEGIN SELECT RAISE(ABORT, 'processing evidence is sealed'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_document_processing_evidence_final_seal "
        "BEFORE INSERT ON document_processing_evidence_seals WHEN "
        "NEW.member_count <> (SELECT COUNT(*) "
        "FROM document_processing_evidence_members member "
        "WHERE member.evidence_seal_id=NEW.evidence_seal_id) "
        "OR (NEW.member_count > 0 AND NEW.member_count <> 1 + ("
        "SELECT MAX(member_ordinal) "
        "FROM document_processing_evidence_members member "
        "WHERE member.evidence_seal_id=NEW.evidence_seal_id)) "
        "OR NEW.canonical_member_set_json <> COALESCE(("
        "SELECT json_group_array(json(canonical_member_json)) FROM ("
        "SELECT canonical_member_json "
        "FROM document_processing_evidence_members "
        "WHERE evidence_seal_id=NEW.evidence_seal_id "
        "ORDER BY member_ordinal)), '[]') "
        "OR NEW.member_set_sha256 <> "
        "fact_sha256(NEW.canonical_member_set_json) "
        "OR datetime(NEW.sealed_at) < datetime((SELECT recorded_at "
        "FROM document_processing_evidence_headers header "
        "WHERE header.evidence_seal_id=NEW.evidence_seal_id)) "
        "BEGIN SELECT RAISE(ABORT, "
        "'document processing evidence final seal mismatch'); END"
    )

    # Once a final publication exists, its exact native set cannot grow.  New
    # runs remain possible and cannot alter an older pinned publication.
    for table, run_column in (
        ("evidence_nodes", "extraction_run_id"),
        ("ocr_extraction_governance", "extraction_run_id"),
        ("ocr_page_results", "extraction_run_id"),
        ("image_ocr_extraction_governance", "extraction_run_id"),
        ("image_ocr_results", "extraction_run_id"),
    ):
        op.execute(
            f"CREATE TRIGGER trg_{table}_processing_evidence_frozen "
            f"BEFORE INSERT ON {table} WHEN EXISTS ("
            "SELECT 1 FROM document_processing_evidence_headers header "
            "JOIN document_processing_evidence_seals seal "
            "ON seal.evidence_seal_id=header.evidence_seal_id "
            f"WHERE header.extraction_run_id=NEW.{run_column}) "
            "BEGIN SELECT RAISE(ABORT, "
            "'native processing evidence run is sealed'); END"
        )
    for table in ("ocr_preflight_pages",):
        op.execute(
            f"CREATE TRIGGER trg_{table}_processing_evidence_frozen "
            f"BEFORE INSERT ON {table} WHEN EXISTS ("
            "SELECT 1 FROM document_processing_evidence_headers header "
            "JOIN document_processing_evidence_seals seal "
            "ON seal.evidence_seal_id=header.evidence_seal_id "
            "WHERE header.assessment_table='ocr_document_assessments' "
            "AND header.assessment_id=NEW.assessment_id) "
            "BEGIN SELECT RAISE(ABORT, "
            "'native processing evidence assessment is sealed'); END"
        )


def downgrade() -> None:
    for table in (
        "evidence_nodes",
        "ocr_extraction_governance",
        "ocr_page_results",
        "image_ocr_extraction_governance",
        "image_ocr_results",
        "ocr_preflight_pages",
    ):
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_processing_evidence_frozen")
    for table in reversed(_TABLES):
        op.drop_table(table)
    for table in reversed(_PDF_TABLES):
        op.drop_table(table)
