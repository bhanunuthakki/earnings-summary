"""Index extraction-run fact anchors for bounded sealing and verification.

Revision ID: 0258_fact_anchor_run_lookup_index
Revises: 0257_embedding_candidate_governance
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0258_fact_anchor_run_lookup_index"
down_revision: str | None = "0257_embedding_candidate_governance"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "fact_reported_observation_anchors_v2"
_INDEX = "ix_fact_reported_anchors_v2_extraction_observation"


def upgrade() -> None:
    op.create_index(
        _INDEX,
        _TABLE,
        ["extraction_run_id", "observation_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(_INDEX, table_name=_TABLE)
