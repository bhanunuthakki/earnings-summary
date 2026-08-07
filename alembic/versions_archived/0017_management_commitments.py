"""management_commitments — Say-Do tracking: things mgmt said, what actually happened.

Each row captures a forward-looking commitment management made on a call
(transcript_segment_id), the KPI it targets, the threshold/value, the
period it should be evaluated against, and (after match_commitments runs)
the realized value + outcome.

Revision ID: 0017_management_commitments
Revises: 0016_thesis_evaluations_table
Create Date: 2026-05-04
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0017_management_commitments"
down_revision: str | Sequence[str] | None = "0016_thesis_evaluations_table"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "management_commitments",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("ticker", sa.String(), nullable=False),
        # When management said it
        sa.Column("period_made", sa.DateTime(), nullable=False),
        sa.Column("transcript_segment_id", sa.Integer(), nullable=False),
        # When it should be evaluated against (the period management was guiding for)
        sa.Column("period_target", sa.DateTime(), nullable=False),
        # The KPI being committed on
        sa.Column("kpi_name", sa.String(), nullable=False),
        sa.Column("comparator", sa.String(), nullable=False),
        sa.Column("target_value", sa.Numeric(precision=24, scale=6), nullable=False),
        sa.Column("unit", sa.String(), nullable=False),
        # The exact quoted/paraphrased commitment text
        sa.Column("narrative", sa.Text(), nullable=False),
        # Outcome (filled by match_commitments after period_target's data lands)
        sa.Column("realized_value", sa.Numeric(precision=24, scale=6)),
        sa.Column("realized_doc_id", sa.Integer()),
        sa.Column("outcome", sa.String()),
        sa.Column("evaluated_at", sa.DateTime()),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.current_timestamp()),
    )
    op.create_index(
        "ix_mgmt_commitments_ticker_target",
        "management_commitments",
        ["ticker", "period_target"],
    )
    op.create_index(
        "ix_mgmt_commitments_ticker_made",
        "management_commitments",
        ["ticker", "period_made"],
    )


def downgrade() -> None:
    op.drop_index("ix_mgmt_commitments_ticker_made", table_name="management_commitments")
    op.drop_index("ix_mgmt_commitments_ticker_target", table_name="management_commitments")
    op.drop_table("management_commitments")
