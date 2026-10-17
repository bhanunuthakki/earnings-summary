"""Materialize the resolution ledger's current fact selection.

Revision ID: 0049_current_fact_selection
Revises: 0048_metric_computation_output_observation
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0049_current_fact_selection"
down_revision = "0048_metric_computation_output_observation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "current_fact_selection",
        sa.Column("logical_key", sa.String(length=256), primary_key=True),
        sa.Column("fact_table", sa.String(length=32), nullable=False),
        sa.Column("fact_row_id", sa.Integer(), nullable=False),
        sa.Column("fact_revision", sa.Integer(), nullable=False),
        sa.Column("observation_id", sa.String(length=128), nullable=False),
        sa.Column("resolution_id", sa.String(length=128), nullable=False),
        sa.Column("resolution_revision", sa.Integer(), nullable=False),
        sa.Column("ticker", sa.String(length=16), nullable=False),
        sa.Column("concept_key", sa.Text(), nullable=False),
        sa.Column("period_end", sa.String(length=10), nullable=False),
        sa.Column("fiscal_period_type", sa.String(length=32), nullable=False),
        sa.CheckConstraint(
            "fact_table IN ('financial_facts', 'kpi_facts')",
            name="ck_current_fact_selection_table",
        ),
        sa.CheckConstraint("fact_row_id > 0", name="ck_current_fact_selection_row"),
        sa.CheckConstraint("fact_revision > 0", name="ck_current_fact_selection_fact_revision"),
        sa.CheckConstraint(
            "resolution_revision > 0", name="ck_current_fact_selection_resolution_revision"
        ),
        sa.ForeignKeyConstraint(
            ["observation_id"],
            ["reported_observations.observation_id"],
            name="fk_current_fact_selection_observation",
        ),
        sa.ForeignKeyConstraint(
            ["resolution_id"],
            ["observation_resolution_revisions.resolution_id"],
            name="fk_current_fact_selection_resolution",
        ),
    )
    op.create_index(
        "ix_current_fact_selection_reader",
        "current_fact_selection",
        ["ticker", "concept_key", "period_end", "fiscal_period_type"],
    )


def downgrade() -> None:
    # The table is a fully derived materialization of the immutable
    # resolution ledger: dropping it discards nothing the ledger cannot
    # re-derive, so unlike retained-identity migrations this downgrade is
    # unconditional. No index is added to fact_observation_revisions here or
    # anywhere in this revision: its composite primary-key autoindex already
    # anchors (fact_table, fact_row_id) lookups.
    op.drop_index("ix_current_fact_selection_reader", table_name="current_fact_selection")
    op.drop_table("current_fact_selection")
