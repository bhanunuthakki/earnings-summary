"""facts_tables — financial_facts, segment_facts, kpi_facts.

Long-form measurement tables. Every row carries source_doc_id (FK to documents)
for provenance. Schemas mirror src/models/facts.py.

Revision ID: 0004_facts_tables
Revises: 0003_backfill_documents_from_fmp_files
Create Date: 2026-05-03
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0004_facts_tables"
down_revision: str | Sequence[str] | None = "0003_backfill_documents_from_fmp_files"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "financial_facts",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("ticker", sa.String(), nullable=False),
        sa.Column("period_end", sa.DateTime(), nullable=False),
        sa.Column("fiscal_period_type", sa.String(), nullable=False),
        sa.Column("line_item", sa.String(), nullable=False),
        sa.Column("value", sa.Numeric(precision=24, scale=6), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=True),
        sa.Column("unit", sa.String(), nullable=False),
        sa.Column("source_doc_id", sa.Integer(), sa.ForeignKey("documents.id"), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False, server_default="1.0"),
    )
    op.create_index(
        "ix_financial_facts_ticker_lineitem_period",
        "financial_facts",
        ["ticker", "line_item", "period_end"],
    )

    op.create_table(
        "segment_facts",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("ticker", sa.String(), nullable=False),
        sa.Column("period_end", sa.DateTime(), nullable=False),
        sa.Column("fiscal_period_type", sa.String(), nullable=False),
        sa.Column("segment_name", sa.String(), nullable=False),
        sa.Column("metric", sa.String(), nullable=False),
        sa.Column("value", sa.Numeric(precision=24, scale=6), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=True),
        sa.Column("unit", sa.String(), nullable=False),
        sa.Column("source_doc_id", sa.Integer(), sa.ForeignKey("documents.id"), nullable=False),
    )
    op.create_index(
        "ix_segment_facts_ticker_segment_period",
        "segment_facts",
        ["ticker", "segment_name", "period_end"],
    )

    op.create_table(
        "kpi_facts",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("ticker", sa.String(), nullable=False),
        sa.Column("period_end", sa.DateTime(), nullable=False),
        sa.Column("fiscal_period_type", sa.String(), nullable=False),
        sa.Column("kpi_definition_id", sa.Integer(), nullable=False),
        sa.Column("value", sa.Numeric(precision=24, scale=6), nullable=False),
        sa.Column("unit", sa.String(), nullable=False),
        sa.Column("source_doc_id", sa.Integer(), sa.ForeignKey("documents.id"), nullable=False),
    )
    op.create_index(
        "ix_kpi_facts_ticker_kpi_period",
        "kpi_facts",
        ["ticker", "kpi_definition_id", "period_end"],
    )


def downgrade() -> None:
    op.drop_index("ix_kpi_facts_ticker_kpi_period", table_name="kpi_facts")
    op.drop_table("kpi_facts")
    op.drop_index("ix_segment_facts_ticker_segment_period", table_name="segment_facts")
    op.drop_table("segment_facts")
    op.drop_index("ix_financial_facts_ticker_lineitem_period", table_name="financial_facts")
    op.drop_table("financial_facts")
