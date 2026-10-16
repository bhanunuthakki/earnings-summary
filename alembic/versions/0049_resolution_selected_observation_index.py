"""Index canonical resolution lookups by the selected observation.

Revision ID: 0049_resolution_selected_observation_index
Revises: 0048_metric_computation_output_observation
"""

from __future__ import annotations

from alembic import op

revision = "0049_resolution_selected_observation_index"
down_revision = "0048_metric_computation_output_observation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "ix_observation_resolution_selected_observation",
        "observation_resolution_revisions",
        ["selected_observation_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_observation_resolution_selected_observation",
        table_name="observation_resolution_revisions",
    )
