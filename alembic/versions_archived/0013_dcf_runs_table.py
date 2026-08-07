"""dcf_runs_table — persistent DCF valuation outputs.

Each invocation of compute.dcf.run_dcf_for_ticker persists a row capturing
the inputs (base revenue, growth assumptions, FCF margin, WACC, terminal
growth) and outputs (NPV, NPV/share). This makes valuations auditable and
re-runnable, and lets consumers compare DCFs over time.

Revision ID: 0013_dcf_runs_table
Revises: 0012_metrics_and_ratios_views
Create Date: 2026-05-03
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0013_dcf_runs_table"
down_revision: str | Sequence[str] | None = "0012_metrics_and_ratios_views"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "dcf_runs",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("ticker", sa.String(), nullable=False),
        sa.Column("valuation_date", sa.String(length=10), nullable=False),
        sa.Column("horizon_years", sa.Integer(), nullable=False),
        sa.Column("base_revenue", sa.Numeric(precision=24, scale=6)),
        sa.Column("revenue_growths_json", sa.Text(), nullable=False),
        sa.Column("fcf_margin", sa.Float(), nullable=False),
        sa.Column("wacc", sa.Float(), nullable=False),
        sa.Column("terminal_growth", sa.Float(), nullable=False),
        sa.Column("npv", sa.Numeric(precision=24, scale=6), nullable=False),
        sa.Column("npv_per_share", sa.Numeric(precision=24, scale=6)),
        sa.Column("shares_outstanding", sa.Numeric(precision=24, scale=6)),
        sa.Column("currency", sa.String(length=3)),
        sa.Column("notes", sa.Text()),
        sa.Column("run_id", sa.String()),
        sa.Column(
            "created_at", sa.DateTime(), nullable=False, server_default=sa.func.current_timestamp()
        ),
    )
    op.create_index("ix_dcf_runs_ticker_date", "dcf_runs", ["ticker", "valuation_date"])


def downgrade() -> None:
    op.drop_index("ix_dcf_runs_ticker_date", table_name="dcf_runs")
    op.drop_table("dcf_runs")
