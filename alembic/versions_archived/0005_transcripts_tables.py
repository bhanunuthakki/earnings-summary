"""transcripts_tables — transcripts and transcript_segments.

Speaker-attributed, time-coded transcript storage. Each transcript row links to
a documents row via document_id; each segment row links back to transcripts.id.

Revision ID: 0005_transcripts_tables
Revises: 0004_facts_tables
Create Date: 2026-05-03
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0005_transcripts_tables"
down_revision: str | Sequence[str] | None = "0004_facts_tables"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "transcripts",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("document_id", sa.Integer(), sa.ForeignKey("documents.id"), nullable=False),
        sa.Column("ticker", sa.String(), nullable=False),
        sa.Column("call_date", sa.DateTime(), nullable=True),
        sa.Column("fiscal_period_type", sa.String(), nullable=True),
        sa.Column("period_end", sa.DateTime(), nullable=True),
        sa.Column("source_url", sa.String(), nullable=True),
        sa.UniqueConstraint("document_id", name="uq_transcripts_document_id"),
    )
    op.create_index(
        "ix_transcripts_ticker_period",
        "transcripts",
        ["ticker", "period_end"],
    )

    op.create_table(
        "transcript_segments",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("transcript_id", sa.Integer(), sa.ForeignKey("transcripts.id"), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("speaker", sa.String(), nullable=True),
        sa.Column("speaker_role", sa.String(), nullable=True),
        sa.Column("time_code_start", sa.String(length=12), nullable=True),
        sa.Column("time_code_end", sa.String(length=12), nullable=True),
        sa.Column("text", sa.Text(), nullable=False),
        sa.UniqueConstraint("transcript_id", "seq", name="uq_transcript_segments_transcript_seq"),
    )


def downgrade() -> None:
    op.drop_table("transcript_segments")
    op.drop_index("ix_transcripts_ticker_period", table_name="transcripts")
    op.drop_table("transcripts")
