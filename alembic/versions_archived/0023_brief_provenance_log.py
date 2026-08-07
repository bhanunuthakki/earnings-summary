"""brief_provenance_log — per-render audit trail of source supersession.

One row per generate_brief invocation. Captures which sources won per metric
at render time (so when SEC XBRL supersedes FMP later, you can see the brief
was rendered with FMP at time T, then with SEC at time T+N), plus the
section status snapshot for the render and the trigger that fired it.

Schema:
  id              -- PK
  ticker          -- the brief's ticker
  generation_date -- ISO date of the render (YYYY-MM-DD)
  generated_at    -- precise UTC timestamp
  sources_used    -- JSON: {metric_or_section: source_type, ...}
  sections_status -- JSON: {section_name: status_value, ...}
  trigger         -- 'earnings' / 'news_refresh' / 'manual' / 'on_demand'
  artifact_path   -- absolute or repo-relative path to the rendered HTML

Revision ID: 0023_brief_provenance_log
Revises: 0022_brief_dirty_flag
Create Date: 2026-05-10
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0023_brief_provenance_log"
down_revision: str | Sequence[str] | None = "0022_brief_dirty_flag"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "brief_provenance_log",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("ticker", sa.String(), nullable=False),
        sa.Column("generation_date", sa.String(length=10), nullable=False),
        sa.Column(
            "generated_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.current_timestamp(),
        ),
        sa.Column("sources_used", sa.Text(), nullable=False),
        sa.Column("sections_status", sa.Text(), nullable=False),
        sa.Column("trigger", sa.String(length=32), nullable=False),
        sa.Column("artifact_path", sa.String(), nullable=False),
    )
    op.create_index(
        "ix_brief_provenance_ticker_date",
        "brief_provenance_log",
        ["ticker", "generation_date"],
    )


def downgrade() -> None:
    op.drop_index("ix_brief_provenance_ticker_date", table_name="brief_provenance_log")
    op.drop_table("brief_provenance_log")
