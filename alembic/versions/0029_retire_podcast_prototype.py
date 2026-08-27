"""Retire the podcast prototype's active LLM budget.

Revision ID: 0029_retire_podcast_prototype
Revises: 0028_remove_processing_tier_and_rename_research_tasks
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0029_retire_podcast_prototype"
down_revision = "0028_remove_processing_tier_and_rename_research_tasks"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if "llm_budgets" in set(sa.inspect(bind).get_table_names()):
        bind.execute(
            sa.text("DELETE FROM llm_budgets WHERE purpose = :purpose"),
            {"purpose": "podcast_takeaway_summary"},
        )


def downgrade() -> None:
    bind = op.get_bind()
    if "llm_budgets" not in set(sa.inspect(bind).get_table_names()):
        return
    op.execute(
        "INSERT INTO llm_budgets "
        "(purpose, monthly_cap_usd, warn_threshold_pct, hard_block, created_at, "
        "updated_at, notes, on_exceed) "
        "SELECT 'podcast_takeaway_summary', 5, 0.8, 0, CURRENT_TIMESTAMP, "
        "CURRENT_TIMESTAMP, 'restored by 0029 downgrade', 'skip' "
        "WHERE NOT EXISTS (SELECT 1 FROM llm_budgets "
        "WHERE purpose = 'podcast_takeaway_summary')"
    )
