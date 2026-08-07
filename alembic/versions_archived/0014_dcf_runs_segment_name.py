"""dcf_runs_segment_name — add nullable segment_name to dcf_runs.

A segment-level DCF produces one dcf_runs row per segment plus one for the
total (segment_name = NULL). Consumers query for `segment_name IS NULL` to
get the consolidated valuation, or `segment_name = 'Google Cloud'` for one.

Revision ID: 0014_dcf_runs_segment_name
Revises: 0013_dcf_runs_table
Create Date: 2026-05-03
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0014_dcf_runs_segment_name"
down_revision: str | Sequence[str] | None = "0013_dcf_runs_table"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("dcf_runs", sa.Column("segment_name", sa.String(), nullable=True))
    op.create_index("ix_dcf_runs_ticker_segment", "dcf_runs", ["ticker", "segment_name"])


def downgrade() -> None:
    op.drop_index("ix_dcf_runs_ticker_segment", table_name="dcf_runs")
    with op.batch_alter_table("dcf_runs") as batch:
        batch.drop_column("segment_name")
