"""Bind commitment scans to exact ordered transcript segment bytes."""

from __future__ import annotations

from sqlalchemy import text

from alembic import op

revision = "0037_commitment_scan_segment_coverage"
down_revision = "0036_add_data_coverage_dispositions"
branch_labels = None
depends_on = None

_LEGACY_MANIFEST = '{"schema_version":"legacy-unobserved@0","segments":[]}'
_LEGACY_MANIFEST_SHA256 = "462e7c3d4eb4e810994e1692e3a50c32b4ddf230ee4c7c9690d81f053fa3d929"
_EMPTY_SOURCE_SHA256 = "6367e24ff1e38f3e3e590763053263ca9db7d36126e99e65caba76cb9fea37e4"


def _create_receipt_table() -> None:
    op.execute(
        f"""
        CREATE TABLE commitment_scan_receipts_v2 (
            receipt_id TEXT PRIMARY KEY
                CHECK(length(receipt_id)=64 AND receipt_id NOT GLOB '*[^0-9a-f]*'),
            transcript_id INTEGER NOT NULL REFERENCES transcripts(id),
            document_id INTEGER NOT NULL REFERENCES documents(id),
            transcript_acquisition_receipt_id TEXT NOT NULL
                REFERENCES transcript_acquisition_receipts(receipt_id),
            transcript_sha256 TEXT NOT NULL
                CHECK(length(transcript_sha256)=64
                  AND transcript_sha256 NOT GLOB '*[^0-9a-f]*'),
            prompt_version TEXT NOT NULL CHECK(length(trim(prompt_version)) BETWEEN 1 AND 128),
            n_extracted INTEGER NOT NULL CHECK(n_extracted >= 0),
            output_manifest_json TEXT NOT NULL
                CHECK(json_valid(output_manifest_json)
                  AND json_type(output_manifest_json)='array'
                  AND json_array_length(output_manifest_json)=n_extracted),
            output_manifest_sha256 TEXT NOT NULL
                CHECK(length(output_manifest_sha256)=64
                  AND output_manifest_sha256 NOT GLOB '*[^0-9a-f]*'),
            observed_segments_json TEXT NOT NULL DEFAULT '{_LEGACY_MANIFEST}'
                CHECK(json_valid(observed_segments_json)
                  AND json_type(observed_segments_json)='object'),
            observed_segments_sha256 TEXT NOT NULL DEFAULT '{_LEGACY_MANIFEST_SHA256}'
                CHECK(length(observed_segments_sha256)=64
                  AND observed_segments_sha256 NOT GLOB '*[^0-9a-f]*'),
            observed_source_sha256 TEXT NOT NULL DEFAULT '{_EMPTY_SOURCE_SHA256}'
                CHECK(length(observed_source_sha256)=64
                  AND observed_source_sha256 NOT GLOB '*[^0-9a-f]*'),
            recorded_at TEXT NOT NULL CHECK(datetime(recorded_at) IS NOT NULL)
        )
        """
    )


def _create_receipt_guards() -> None:
    op.execute(
        "CREATE INDEX ix_commitment_scan_receipts_current "
        "ON commitment_scan_receipts(transcript_id,prompt_version,recorded_at,receipt_id)"
    )
    op.execute(
        "CREATE TRIGGER trg_commitment_scan_receipts_no_update "
        "BEFORE UPDATE ON commitment_scan_receipts BEGIN "
        "SELECT RAISE(ABORT,'commitment scan receipts are append-only'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_commitment_scan_receipts_no_delete "
        "BEFORE DELETE ON commitment_scan_receipts BEGIN "
        "SELECT RAISE(ABORT,'commitment scan receipts are append-only'); END"
    )


def upgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_commitment_scan_receipts_no_delete")
    op.execute("DROP TRIGGER IF EXISTS trg_commitment_scan_receipts_no_update")
    op.execute("DROP INDEX IF EXISTS ix_commitment_scan_receipts_current")
    _create_receipt_table()
    op.execute(
        "INSERT INTO commitment_scan_receipts_v2 "
        "(receipt_id,transcript_id,document_id,transcript_acquisition_receipt_id,"
        "transcript_sha256,prompt_version,n_extracted,output_manifest_json,"
        "output_manifest_sha256,recorded_at) "
        "SELECT receipt_id,transcript_id,document_id,transcript_acquisition_receipt_id,"
        "transcript_sha256,prompt_version,n_extracted,output_manifest_json,"
        "output_manifest_sha256,recorded_at FROM commitment_scan_receipts"
    )
    op.execute("DROP TABLE commitment_scan_receipts")
    op.execute("ALTER TABLE commitment_scan_receipts_v2 RENAME TO commitment_scan_receipts")
    _create_receipt_guards()
    op.execute(
        "CREATE UNIQUE INDEX ux_commitment_scan_receipts_exact_scan "
        "ON commitment_scan_receipts("
        "transcript_acquisition_receipt_id,prompt_version,observed_source_sha256) "
        "WHERE json_extract(observed_segments_json,'$.schema_version')="
        "'commitment-segment-observations@1'"
    )


def downgrade() -> None:
    bind = op.get_bind()
    populated = bind.execute(
        text(
            "SELECT 1 FROM commitment_scan_receipts "
            "WHERE json_extract(observed_segments_json,'$.schema_version')="
            "'commitment-segment-observations@1' LIMIT 1"
        )
    ).fetchone()
    if populated is not None:
        raise RuntimeError("cannot downgrade after exact segment coverage receipts exist")
    op.execute("DROP TRIGGER IF EXISTS trg_commitment_scan_receipts_no_delete")
    op.execute("DROP TRIGGER IF EXISTS trg_commitment_scan_receipts_no_update")
    op.execute("DROP INDEX IF EXISTS ux_commitment_scan_receipts_exact_scan")
    op.execute("DROP INDEX IF EXISTS ix_commitment_scan_receipts_current")
    op.execute(
        """
        CREATE TABLE commitment_scan_receipts_v1 (
            receipt_id TEXT PRIMARY KEY
                CHECK(length(receipt_id)=64 AND receipt_id NOT GLOB '*[^0-9a-f]*'),
            transcript_id INTEGER NOT NULL REFERENCES transcripts(id),
            document_id INTEGER NOT NULL REFERENCES documents(id),
            transcript_acquisition_receipt_id TEXT NOT NULL
                REFERENCES transcript_acquisition_receipts(receipt_id),
            transcript_sha256 TEXT NOT NULL
                CHECK(length(transcript_sha256)=64
                  AND transcript_sha256 NOT GLOB '*[^0-9a-f]*'),
            prompt_version TEXT NOT NULL CHECK(length(trim(prompt_version)) BETWEEN 1 AND 128),
            n_extracted INTEGER NOT NULL CHECK(n_extracted >= 0),
            output_manifest_json TEXT NOT NULL
                CHECK(json_valid(output_manifest_json)
                  AND json_type(output_manifest_json)='array'
                  AND json_array_length(output_manifest_json)=n_extracted),
            output_manifest_sha256 TEXT NOT NULL
                CHECK(length(output_manifest_sha256)=64
                  AND output_manifest_sha256 NOT GLOB '*[^0-9a-f]*'),
            recorded_at TEXT NOT NULL CHECK(datetime(recorded_at) IS NOT NULL),
            UNIQUE(transcript_id,prompt_version,output_manifest_sha256)
        )
        """
    )
    op.execute(
        "INSERT INTO commitment_scan_receipts_v1 SELECT "
        "receipt_id,transcript_id,document_id,transcript_acquisition_receipt_id,"
        "transcript_sha256,prompt_version,n_extracted,output_manifest_json,"
        "output_manifest_sha256,recorded_at FROM commitment_scan_receipts"
    )
    op.execute("DROP TABLE commitment_scan_receipts")
    op.execute("ALTER TABLE commitment_scan_receipts_v1 RENAME TO commitment_scan_receipts")
    _create_receipt_guards()
