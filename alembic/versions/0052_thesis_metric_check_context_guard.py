"""Require retained context for scalar and calculated thesis checks.

Revision ID: 0052_thesis_metric_check_context_guard
Revises: 0051_thesis_check_context
"""

from alembic import op

revision = "0052_thesis_metric_check_context_guard"
down_revision = "0051_thesis_check_context"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("DROP TRIGGER trg_thesis_check_context_shape")
    op.execute("""
        CREATE TRIGGER trg_thesis_check_context_shape BEFORE INSERT ON thesis_evaluation_episode_check_receipts
        WHEN (NEW.context_json IS NULL) != (NEW.context_sha256 IS NULL)
          OR ((SELECT evaluator_semantic_version FROM thesis_evaluation_episodes WHERE episode_id=NEW.episode_id) IN ('thesis-evaluator/v2','thesis-evaluator/v3') AND NEW.context_json IS NULL)
        BEGIN SELECT RAISE(ABORT, 'thesis check retained context required'); END
    """)


def downgrade() -> None:
    op.execute("DROP TRIGGER trg_thesis_check_context_shape")
    op.execute("""
        CREATE TRIGGER trg_thesis_check_context_shape BEFORE INSERT ON thesis_evaluation_episode_check_receipts
        WHEN (NEW.context_json IS NULL) != (NEW.context_sha256 IS NULL)
          OR ((SELECT evaluator_semantic_version FROM thesis_evaluation_episodes WHERE episode_id=NEW.episode_id)='thesis-evaluator/v2' AND NEW.context_json IS NULL)
        BEGIN SELECT RAISE(ABORT, 'thesis check retained context required'); END
    """)
