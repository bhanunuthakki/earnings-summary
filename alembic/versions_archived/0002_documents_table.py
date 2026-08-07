"""documents_table — provenance anchor for every fact in the system.

Per directives/data_provenance.md, every row in financial_facts, segment_facts,
kpi_facts, transcript_segments, dcf_runs, etc. must reference documents.id via
source_doc_id. Schema mirrors src/models/documents.py::Document.

Revision ID: 0002_documents_table
Revises: 0001_companies_provenance
Create Date: 2026-05-03
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0002_documents_table"
down_revision: str | Sequence[str] | None = "0001_companies_provenance"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "documents",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("ticker", sa.String(), nullable=False),
        sa.Column("source_type", sa.String(), nullable=False),
        sa.Column("doc_type", sa.String(), nullable=False),
        sa.Column("period_start", sa.DateTime(), nullable=True),
        sa.Column("period_end", sa.DateTime(), nullable=True),
        sa.Column("file_path", sa.String(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("fetched_at", sa.DateTime(), nullable=False),
        sa.Column("fetch_status", sa.String(), nullable=False),
        sa.Column("http_code", sa.Integer(), nullable=True),
        sa.Column("raw_bytes_size", sa.Integer(), nullable=False),
        sa.Column("source_url", sa.String(), nullable=True),
        sa.Column("parent_document_id", sa.Integer(), sa.ForeignKey("documents.id"), nullable=True),
        sa.UniqueConstraint("sha256", name="uq_documents_sha256"),
    )
    op.create_index(
        "ix_documents_ticker_doctype_period",
        "documents",
        ["ticker", "doc_type", "period_end"],
    )
    op.create_index("ix_documents_source_type", "documents", ["source_type"])


def downgrade() -> None:
    op.drop_index("ix_documents_source_type", table_name="documents")
    op.drop_index("ix_documents_ticker_doctype_period", table_name="documents")
    op.drop_table("documents")
