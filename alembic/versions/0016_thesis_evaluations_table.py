"""thesis_evaluations_table — append-only history of run_thesis_evaluator outputs.

Each call to `persist_verdict` writes one row capturing the holding-level status
plus the per-rule status snapshot. Provides a time series so consumers can ask
"when did NOW first breach?" or "how long has MELI been on watch?" without
mutating thesis_state's current-state row.

Revision ID: 0016_thesis_evaluations_table
Revises: 0015_classify_10q_files
Create Date: 2026-05-03
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0016_thesis_evaluations_table"
down_revision: str | Sequence[str] | None = "0015_classify_10q_files"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "thesis_evaluations",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("ticker", sa.String(), nullable=False),
        sa.Column("evaluated_at", sa.DateTime(), nullable=False),
        sa.Column("overall_status", sa.String(), nullable=False),
        sa.Column("rule_evaluations_json", sa.Text(), nullable=False),
        sa.Column("run_id", sa.String()),
    )
    op.create_index(
        "ix_thesis_evaluations_ticker_evaluated_at",
        "thesis_evaluations",
        ["ticker", "evaluated_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_thesis_evaluations_ticker_evaluated_at", table_name="thesis_evaluations"
    )
    op.drop_table("thesis_evaluations")
