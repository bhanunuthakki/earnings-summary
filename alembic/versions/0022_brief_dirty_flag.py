"""brief_dirty_flag — per-ticker flag for brief regeneration triggering.

Adds `brief_dirty BOOLEAN DEFAULT 0` to tracked_companies. Set to 1 by fact
writers (kpi_persistence, financial_facts inserter, etc.) on any insert/update
for a ticker. The brief refresh worker drains rows where brief_dirty=1, runs
build_artifacts.py, and clears the flag.

A partial index on `brief_dirty = 1` keeps queue draining fast even as the
table grows.

Revision ID: 0022_brief_dirty_flag
Revises: 0021_backfill_tracked_companies_fiscal_year_end
Create Date: 2026-05-10
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0022_brief_dirty_flag"
down_revision: str | Sequence[str] | None = "0021_backfill_tracked_companies_fiscal_year_end"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "tracked_companies",
        sa.Column(
            "brief_dirty",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )
    op.create_index(
        "ix_tracked_companies_brief_dirty",
        "tracked_companies",
        ["brief_dirty"],
        sqlite_where=sa.text("brief_dirty = 1"),
    )


def downgrade() -> None:
    op.drop_index("ix_tracked_companies_brief_dirty", table_name="tracked_companies")
    with op.batch_alter_table("tracked_companies") as batch:
        batch.drop_column("brief_dirty")
