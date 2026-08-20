"""Persist immutable issuer-document fact coverage receipts.

Revision ID: 0019_issuer_fact_coverage_receipts
Revises: 0018_add_transcript_acquisition_receipts
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0019_issuer_fact_coverage_receipts"
down_revision = "0018_add_transcript_acquisition_receipts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "issuer_fact_coverage_receipts",
        sa.Column("record_id", sa.String(length=128), primary_key=True),
        sa.Column("idempotency_key", sa.String(length=64), nullable=False, unique=True),
        sa.Column("reconciliation_key", sa.String(length=512), nullable=False, unique=True),
        sa.Column("document_id", sa.Integer(), nullable=False),
        sa.Column("ticker", sa.String(length=16), nullable=False),
        sa.Column("fact_identity", sa.String(length=512), nullable=False),
        sa.Column("receipt_json", sa.Text(), nullable=False),
        sa.Column("receipt_sha256", sa.String(length=64), nullable=False),
        sa.Column("recorded_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(("document_id",), ("documents.id",)),
    )
    op.execute(
        "CREATE TRIGGER issuer_fact_coverage_receipts_no_update "
        "BEFORE UPDATE ON issuer_fact_coverage_receipts "
        "BEGIN SELECT RAISE(ABORT, 'issuer fact coverage receipts are append-only'); END"
    )
    op.execute(
        "CREATE TRIGGER issuer_fact_coverage_receipts_no_delete "
        "BEFORE DELETE ON issuer_fact_coverage_receipts "
        "BEGIN SELECT RAISE(ABORT, 'issuer fact coverage receipts are append-only'); END"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER issuer_fact_coverage_receipts_no_delete")
    op.execute("DROP TRIGGER issuer_fact_coverage_receipts_no_update")
    op.drop_table("issuer_fact_coverage_receipts")
