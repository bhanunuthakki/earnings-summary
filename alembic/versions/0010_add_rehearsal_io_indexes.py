"""Add covering indexes for governed rehearsal fact admission.

Revision ID: 0010_add_rehearsal_io_indexes
Revises: 0009_add_ir_approval_store
Create Date: 2026-08-12
"""

from __future__ import annotations

from alembic import op

revision = "0010_add_rehearsal_io_indexes"
down_revision = "0009_add_ir_approval_store"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_financial_facts_source_doc_id_id "
        "ON financial_facts(source_doc_id,id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS "
        "ix_fact_observation_revisions_source_fact_observation "
        "ON fact_observation_revisions("
        "source_document_id,fact_table,fact_row_id,observation_id)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_fact_observation_revisions_source_fact_observation")
    op.execute("DROP INDEX IF EXISTS ix_financial_facts_source_doc_id_id")
