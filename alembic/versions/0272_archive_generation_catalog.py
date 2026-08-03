"""Append-only catalog for verified immutable archive generations.

Archive files are sealed and verified before this operational catalog is
written.  A registration receipt is the publication boundary: incomplete
generation/table rows are invisible to the verified view, and the receipt
trigger proves table-count and predecessor-chain completeness.

Revision ID: 0272_archive_generation_catalog
Revises: 0271_disclosure_thesis_materiality
Create Date: 2026-08-02
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "0272_archive_generation_catalog"
down_revision: str | None = "0271_disclosure_thesis_materiality"
branch_labels: None = None
depends_on: None = None

_GENERATIONS = "archive_generations"
_TABLES = "archive_generation_table_commitments"
_RECEIPTS = "archive_generation_registration_receipts"
_VIEW = "v_archive_generations_verified"


def _sha_check(column: str) -> sa.CheckConstraint:
    return sa.CheckConstraint(
        f"length({column})=64 AND {column} NOT GLOB '*[^0-9a-f]*'",
        name=f"ck_{column}_sha256",
    )


def upgrade() -> None:
    op.create_table(
        _GENERATIONS,
        sa.Column("generation_id", sa.String(128), primary_key=True),
        sa.Column("predecessor_generation_id", sa.String(128), nullable=True),
        sa.Column("predecessor_manifest_sha256", sa.String(64), nullable=True),
        sa.Column("archive_uri", sa.Text(), nullable=False, unique=True),
        sa.Column("manifest_uri", sa.Text(), nullable=False, unique=True),
        sa.Column("manifest_sha256", sa.String(64), nullable=False, unique=True),
        sa.Column("database_sha256", sa.String(64), nullable=False, unique=True),
        sa.Column("schema_sha256", sa.String(64), nullable=False),
        sa.Column("schema_version", sa.String(64), nullable=False),
        sa.Column("publication_sequence_start", sa.BigInteger(), nullable=False),
        sa.Column("publication_sequence_end", sa.BigInteger(), nullable=False),
        sa.Column("recorded_at_start", sa.DateTime(), nullable=False),
        sa.Column("recorded_at_end", sa.DateTime(), nullable=False),
        sa.Column("external_reference_count", sa.BigInteger(), nullable=False),
        sa.Column("external_reference_set_sha256", sa.String(64), nullable=False),
        sa.Column("database_size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("table_count", sa.Integer(), nullable=False),
        sa.Column("sealed_at", sa.DateTime(), nullable=False),
        sa.Column("registered_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["predecessor_generation_id"],
            [f"{_GENERATIONS}.generation_id"],
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "predecessor_generation_id",
            name="uq_archive_generation_single_successor",
        ),
        sa.CheckConstraint(
            "(predecessor_generation_id IS NULL) = (predecessor_manifest_sha256 IS NULL)",
            name="ck_archive_generation_predecessor_pair",
        ),
        sa.CheckConstraint(
            "publication_sequence_start >= 0 AND "
            "publication_sequence_end >= publication_sequence_start",
            name="ck_archive_generation_sequence_range",
        ),
        sa.CheckConstraint(
            "recorded_at_end >= recorded_at_start",
            name="ck_archive_generation_recorded_range",
        ),
        sa.CheckConstraint(
            "external_reference_count >= 0 AND database_size_bytes >= 0 AND table_count > 0",
            name="ck_archive_generation_nonnegative_counts",
        ),
        sa.CheckConstraint(
            "length(archive_uri) > 0 AND length(manifest_uri) > 0",
            name="ck_archive_generation_uris",
        ),
        _sha_check("manifest_sha256"),
        _sha_check("database_sha256"),
        _sha_check("schema_sha256"),
        _sha_check("external_reference_set_sha256"),
        _sha_check("predecessor_manifest_sha256"),
    )
    op.create_index(
        "ix_archive_generation_sequence_range",
        _GENERATIONS,
        ["publication_sequence_start", "publication_sequence_end"],
    )
    op.create_index(
        "ix_archive_generation_recorded_range",
        _GENERATIONS,
        ["recorded_at_start", "recorded_at_end"],
    )

    op.create_table(
        _TABLES,
        sa.Column("generation_id", sa.String(128), nullable=False),
        sa.Column("table_name", sa.Text(), nullable=False),
        sa.Column("columns_json", sa.Text(), nullable=False),
        sa.Column("primary_key_columns_json", sa.Text(), nullable=False),
        sa.Column("row_count", sa.BigInteger(), nullable=False),
        sa.Column("content_sha256", sa.String(64), nullable=False),
        sa.ForeignKeyConstraint(
            ["generation_id"],
            [f"{_GENERATIONS}.generation_id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("generation_id", "table_name"),
        sa.CheckConstraint("length(table_name) > 0", name="ck_archive_table_name"),
        sa.CheckConstraint("row_count >= 0", name="ck_archive_table_row_count"),
        sa.CheckConstraint(
            "json_valid(columns_json) AND json_type(columns_json)='array' "
            "AND json_array_length(columns_json)>0",
            name="ck_archive_table_columns_json",
        ),
        sa.CheckConstraint(
            "json_valid(primary_key_columns_json) "
            "AND json_type(primary_key_columns_json)='array' "
            "AND json_array_length(primary_key_columns_json)>0",
            name="ck_archive_table_primary_key_json",
        ),
        _sha_check("content_sha256"),
    )

    op.create_table(
        _RECEIPTS,
        sa.Column("receipt_id", sa.String(96), primary_key=True),
        sa.Column("generation_id", sa.String(128), nullable=False, unique=True),
        sa.Column("request_sha256", sa.String(64), nullable=False),
        sa.Column("result_sha256", sa.String(64), nullable=False),
        sa.Column("receipt_sha256", sa.String(64), nullable=False, unique=True),
        sa.Column("receipt_json", sa.Text(), nullable=False),
        sa.Column("recorded_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["generation_id"],
            [f"{_GENERATIONS}.generation_id"],
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "json_valid(receipt_json) AND json_type(receipt_json)='object'",
            name="ck_archive_registration_receipt_json",
        ),
        _sha_check("request_sha256"),
        _sha_check("result_sha256"),
        _sha_check("receipt_sha256"),
    )

    op.execute(
        f"""
        CREATE TRIGGER trg_archive_registration_complete
        BEFORE INSERT ON {_RECEIPTS}
        BEGIN
          SELECT CASE WHEN NOT EXISTS (
            SELECT 1 FROM {_GENERATIONS} g WHERE g.generation_id=NEW.generation_id
          ) THEN RAISE(ABORT, 'archive generation is missing') END;
          SELECT CASE WHEN (
            SELECT COUNT(*) FROM {_TABLES} t WHERE t.generation_id=NEW.generation_id
          ) <> (
            SELECT table_count FROM {_GENERATIONS} g WHERE g.generation_id=NEW.generation_id
          ) THEN RAISE(ABORT, 'archive generation table commitments incomplete') END;
          SELECT CASE WHEN (
            SELECT predecessor_generation_id FROM {_GENERATIONS}
            WHERE generation_id=NEW.generation_id
          ) IS NULL AND EXISTS (
            SELECT 1 FROM {_RECEIPTS}
          ) THEN RAISE(ABORT, 'archive generation has multiple genesis receipts') END;
          SELECT CASE WHEN (
            SELECT predecessor_generation_id FROM {_GENERATIONS}
            WHERE generation_id=NEW.generation_id
          ) IS NOT NULL AND NOT EXISTS (
            SELECT 1 FROM {_RECEIPTS} prior
            JOIN {_GENERATIONS} current ON current.generation_id=NEW.generation_id
            JOIN {_GENERATIONS} predecessor
              ON predecessor.generation_id=current.predecessor_generation_id
            WHERE prior.generation_id=predecessor.generation_id
              AND current.predecessor_manifest_sha256=predecessor.manifest_sha256
              AND current.publication_sequence_start=predecessor.publication_sequence_end+1
          ) THEN RAISE(ABORT, 'archive generation predecessor is not verified and contiguous') END;
        END
        """
    )
    op.execute(
        f"""
        CREATE TRIGGER trg_archive_table_after_seal
        BEFORE INSERT ON {_TABLES}
        WHEN EXISTS (
          SELECT 1 FROM {_RECEIPTS} r WHERE r.generation_id=NEW.generation_id
        )
        BEGIN SELECT RAISE(ABORT, 'archive generation is sealed'); END
        """
    )
    for table in (_GENERATIONS, _TABLES, _RECEIPTS):
        op.execute(
            f"CREATE TRIGGER trg_{table}_immutable_update BEFORE UPDATE ON {table} "
            f"BEGIN SELECT RAISE(ABORT, 'immutable {table}'); END"
        )
        op.execute(
            f"CREATE TRIGGER trg_{table}_immutable_delete BEFORE DELETE ON {table} "
            f"BEGIN SELECT RAISE(ABORT, 'immutable {table}'); END"
        )

    op.execute(
        f"""
        CREATE VIEW {_VIEW} AS
        SELECT g.*, r.receipt_id, r.request_sha256, r.result_sha256,
               r.receipt_sha256, r.receipt_json, r.recorded_at AS receipt_recorded_at
        FROM {_GENERATIONS} g
        JOIN {_RECEIPTS} r ON r.generation_id=g.generation_id
        """
    )


def downgrade() -> None:
    op.execute(f"DROP VIEW IF EXISTS {_VIEW}")
    for table in (_RECEIPTS, _TABLES, _GENERATIONS):
        op.drop_table(table)
