"""create_saydo_historical_metrics

Revision ID: b79cec08ce5b
Revises: 0033_kpi_facts_source_excerpt
Create Date: 2026-05-24 21:31:30.714950

"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = 'b79cec08ce5b'
down_revision: str | Sequence[str] | None = '0033_kpi_facts_source_excerpt'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "saydo_historical_metrics",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("ticker", sa.String(), nullable=False),
        sa.Column("period_made", sa.DateTime(), nullable=False),
        sa.Column("period_target", sa.DateTime(), nullable=False),
        sa.Column("kpi_name", sa.String(), nullable=False),
        sa.Column("comparator", sa.String(), nullable=False),
        sa.Column("target_value", sa.Numeric(precision=24, scale=6), nullable=False),
        sa.Column("realized_value", sa.Numeric(precision=24, scale=6)),
        sa.Column("outcome", sa.String(), nullable=False),
        sa.Column("guidance_narrative", sa.Text(), nullable=False),
        sa.Column("realized_narrative", sa.Text()),
        sa.Column(
            "evaluated_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.current_timestamp(),
        ),
    )
    op.create_index(
        "ix_saydo_hist_ticker_target",
        "saydo_historical_metrics",
        ["ticker", "period_target"],
    )


def downgrade() -> None:
    op.drop_index("ix_saydo_hist_ticker_target", table_name="saydo_historical_metrics")
    op.drop_table("saydo_historical_metrics")
