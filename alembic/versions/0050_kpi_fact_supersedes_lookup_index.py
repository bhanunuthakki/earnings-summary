"""Bound KPI successor lookups by the superseded fact.

Revision ID: 0050_kpi_fact_supersedes_lookup_index
Revises: 0049_resolution_selected_observation_index
"""

from __future__ import annotations

from alembic import op

revision = "0050_kpi_fact_supersedes_lookup_index"
down_revision = "0049_resolution_selected_observation_index"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index("ix_kpi_facts_supersedes_id", "kpi_facts", ["supersedes_id"])


def downgrade() -> None:
    op.drop_index("ix_kpi_facts_supersedes_id", table_name="kpi_facts")
