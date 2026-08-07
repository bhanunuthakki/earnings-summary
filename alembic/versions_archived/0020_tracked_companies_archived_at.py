"""tracked_companies_archived_at — soft-delete column for watchlist/portfolio.

Adds `archived_at` (nullable TIMESTAMP). NULL = active and refreshed by the
cacher; non-NULL = archived (data retained on disk and in DB; excluded from
the refresh queue). Lets users remove a name from active rotation without
losing captured FMP history; reactivation is a single UPDATE.

The refresh queue and `tracked_companies_for_user` filter on
`archived_at IS NULL` by default; pass `include_archived=True` to opt in.

Revision ID: 0020_tracked_companies_archived_at
Revises: 0019_transcripts_has_qa_section
Create Date: 2026-05-09
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0020_tracked_companies_archived_at"
down_revision: str | Sequence[str] | None = "0019_transcripts_has_qa_section"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "tracked_companies",
        sa.Column("archived_at", sa.TIMESTAMP(), nullable=True),
    )
    op.create_index(
        "ix_tracked_companies_active",
        "tracked_companies",
        ["user_id", "list_type"],
        unique=False,
        sqlite_where=sa.text("archived_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_tracked_companies_active", table_name="tracked_companies")
    op.drop_column("tracked_companies", "archived_at")
