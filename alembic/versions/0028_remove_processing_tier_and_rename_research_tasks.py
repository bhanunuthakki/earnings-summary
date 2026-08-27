"""Derive schedule class and normalize research-task audit names.

Revision ID: 0028_remove_processing_tier_and_rename_research_tasks
Revises: 0027_add_sizing_intent_supersessions
"""

from __future__ import annotations

from alembic import op

revision = "0028_remove_processing_tier_and_rename_research_tasks"
down_revision = "0027_add_sizing_intent_supersessions"
branch_labels = None
depends_on = None


def _columns(table: str) -> set[str]:
    bind = op.get_bind()
    rows = bind.exec_driver_sql(f"PRAGMA table_info({table})").fetchall()
    return {str(row[1]) for row in rows}


def _index_exists(name: str) -> bool:
    bind = op.get_bind()
    return (
        bind.exec_driver_sql(
            "SELECT 1 FROM sqlite_master WHERE type='index' AND name=?", (name,)
        ).fetchone()
        is not None
    )


def upgrade() -> None:
    bind = op.get_bind()
    research_columns = _columns("research_tasks")
    if "cost_usd" in research_columns and "estimated_cost_usd" not in research_columns:
        bind.exec_driver_sql(
            "ALTER TABLE research_tasks RENAME COLUMN cost_usd TO estimated_cost_usd"
        )
    if "run_id" in research_columns and "task_metadata_json" not in research_columns:
        bind.exec_driver_sql(
            "ALTER TABLE research_tasks RENAME COLUMN run_id TO task_metadata_json"
        )

    for index_name in (
        "idx_tracked_processing_tier",
        "ix_tracked_companies_processing_tier",
    ):
        if _index_exists(index_name):
            bind.exec_driver_sql(f"DROP INDEX {index_name}")
    if "processing_tier" in _columns("tracked_companies"):
        bind.exec_driver_sql("ALTER TABLE tracked_companies DROP COLUMN processing_tier")


def downgrade() -> None:
    bind = op.get_bind()
    research_columns = _columns("research_tasks")
    if "estimated_cost_usd" in research_columns and "cost_usd" not in research_columns:
        bind.exec_driver_sql(
            "ALTER TABLE research_tasks RENAME COLUMN estimated_cost_usd TO cost_usd"
        )
    if "task_metadata_json" in research_columns and "run_id" not in research_columns:
        bind.exec_driver_sql(
            "ALTER TABLE research_tasks RENAME COLUMN task_metadata_json TO run_id"
        )

    if "processing_tier" not in _columns("tracked_companies"):
        bind.exec_driver_sql(
            "ALTER TABLE tracked_companies ADD COLUMN processing_tier VARCHAR(8) DEFAULT 'P3' NOT NULL"
        )
    bind.exec_driver_sql(
        "UPDATE tracked_companies SET processing_tier = CASE "
        "WHEN list_type = 'portfolio' THEN 'P1' "
        "WHEN list_type IN ('watchlist', 'evaluation') THEN 'P2' "
        "ELSE 'P3' END"
    )
    bind.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS idx_tracked_processing_tier "
        "ON tracked_companies(processing_tier,last_built_at)"
    )
    bind.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_tracked_companies_processing_tier "
        "ON tracked_companies(processing_tier) WHERE archived_at IS NULL"
    )
