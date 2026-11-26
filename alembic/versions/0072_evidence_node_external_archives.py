"""Add external archive receipts for compacted evidence_nodes runs.

Revision ID: 0072_evidence_node_external_archives
Revises: 0071_admit_aggregator_transcript_providers
"""

from __future__ import annotations

from alembic import op

revision = "0072_evidence_node_external_archives"
down_revision = "0071_admit_aggregator_transcript_providers"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS evidence_node_external_archives (
            extraction_run_id TEXT PRIMARY KEY
                REFERENCES evidence_extraction_runs(extraction_run_id),
            storage_relpath TEXT NOT NULL
                CHECK (
                    length(storage_relpath) > 0
                    AND substr(storage_relpath, 1, 1) <> '/'
                    AND substr(storage_relpath, 1, 1) <> '\\'
                    AND instr(storage_relpath, '\\') = 0
                    AND instr(storage_relpath, ':') = 0
                    AND ('/' || storage_relpath || '/') NOT LIKE '%/../%'
                    AND ('/' || storage_relpath || '/') NOT LIKE '%/./%'
                ),
            archive_sha256 TEXT NOT NULL
                CHECK (
                    length(archive_sha256) = 64
                    AND archive_sha256 NOT GLOB '*[^0-9a-f]*'
                ),
            archived_node_count INTEGER NOT NULL
                CHECK (archived_node_count > 0),
            archived_node_kinds_json TEXT NOT NULL
                CHECK (
                    json_valid(archived_node_kinds_json)
                    AND json_type(archived_node_kinds_json) = 'array'
                    AND json_array_length(archived_node_kinds_json) > 0
                ),
            original_output_sha256 TEXT NOT NULL
                CHECK (
                    length(original_output_sha256) = 64
                    AND original_output_sha256 NOT GLOB '*[^0-9a-f]*'
                ),
            archived_at TEXT NOT NULL
                CHECK (length(archived_at) > 0)
        )
        """
    )
    connection = op.get_bind()
    if (
        connection.exec_driver_sql(
            "PRAGMA foreign_key_check(evidence_node_external_archives)"
        ).fetchone()
        is not None
    ):
        raise RuntimeError(
            "evidence_node_external_archives failed table-scoped foreign-key verification"
        )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS evidence_node_external_archives")
