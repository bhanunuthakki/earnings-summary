"""segment_junction — period+dimension tables for cross-tabulated segment data.

Today `segment_facts` is single-dim: one row carries one (segment_name, metric)
cell per (ticker, period). That's enough for the FMP product / geographic
endpoints (each is a flat 1-D breakdown) but loses information when filings
expose cross-tabs — e.g. "AWS revenue in US", "AWS revenue in EMEA" — which
have to be flattened into a single segment_name string today.

This migration adds two new tables ADDITIVELY (segment_facts is preserved
unchanged so existing loaders / renderers keep working):

  segment_periods     — one row per (ticker, period_end, fiscal_period_type,
                        source_doc_id) tuple, holding currency/unit/created_at
                        plus the provenance FK.
  segment_dimensions  — one row per (dim_type, dim_name, metric, value) cell
                        under a period. dim_type values: product, geography,
                        channel, customer_segment, business_unit. The
                        loader's `dims` arg uses self-joins to express
                        cross-tab queries.

Idempotent: re-running upgrade is safe; tables only get created if missing.
Chains off 0054_audit_columns (which already merged the sibling 0053 heads —
strategic_targets, thesis_evaluations_soft_rules, timeseries_signals — into
a single chain). Renamed from 0053_segment_junction to 0055 to avoid the
revision collision after those siblings landed first.

Revision ID: 0055_segment_junction
Revises: 0054_audit_columns
Create Date: 2026-05-25
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0055_segment_junction"
down_revision: str | Sequence[str] | None = "0054_audit_columns"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    existing_tables = set(sa.inspect(bind).get_table_names())

    if "segment_periods" not in existing_tables:
        op.create_table(
            "segment_periods",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("ticker", sa.String(length=16), nullable=False),
            sa.Column("period_end", sa.DateTime(), nullable=False),
            sa.Column("fiscal_period_type", sa.String(length=8), nullable=False),
            sa.Column(
                "source_doc_id",
                sa.Integer(),
                sa.ForeignKey("documents.id"),
                nullable=False,
            ),
            sa.Column("currency", sa.String(length=8), nullable=True),
            sa.Column("unit", sa.String(length=16), nullable=False),
            sa.Column(
                "created_at",
                sa.DateTime(),
                nullable=False,
                server_default=sa.func.current_timestamp(),
            ),
            sa.UniqueConstraint(
                "ticker",
                "period_end",
                "fiscal_period_type",
                "source_doc_id",
                name="uq_segment_periods_provenance",
            ),
        )
        op.create_index(
            "ix_segment_periods_ticker_period",
            "segment_periods",
            ["ticker", "period_end"],
        )

    if "segment_dimensions" not in existing_tables:
        op.create_table(
            "segment_dimensions",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column(
                "period_id",
                sa.Integer(),
                sa.ForeignKey("segment_periods.id"),
                nullable=False,
            ),
            sa.Column("dim_type", sa.String(length=16), nullable=False),
            sa.Column("dim_name", sa.String(length=128), nullable=False),
            sa.Column("value", sa.Numeric(precision=20, scale=4), nullable=False),
            sa.Column("metric", sa.String(length=32), nullable=False),
        )
        op.create_index(
            "ix_segment_dimensions_period_type_metric",
            "segment_dimensions",
            ["period_id", "dim_type", "metric"],
        )
        op.create_index(
            "ix_segment_dimensions_dim",
            "segment_dimensions",
            ["dim_type", "dim_name"],
        )


def downgrade() -> None:
    bind = op.get_bind()
    existing_tables = set(sa.inspect(bind).get_table_names())
    if "segment_dimensions" in existing_tables:
        op.drop_index(
            "ix_segment_dimensions_dim", table_name="segment_dimensions"
        )
        op.drop_index(
            "ix_segment_dimensions_period_type_metric",
            table_name="segment_dimensions",
        )
        op.drop_table("segment_dimensions")
    if "segment_periods" in existing_tables:
        op.drop_index("ix_segment_periods_ticker_period", table_name="segment_periods")
        op.drop_table("segment_periods")
