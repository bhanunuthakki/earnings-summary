"""Retain exact deterministic thesis context on existing execution checks.

Revision ID: 0049_thesis_check_context
Revises: 0048_metric_computation_output_observation
"""

from alembic import op

revision = "0049_thesis_check_context"
down_revision = "0048_metric_computation_output_observation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE thesis_evaluation_episode_check_receipts ADD COLUMN context_json TEXT CHECK(context_json IS NULL OR (json_valid(context_json) AND json_type(context_json)='object'))"
    )
    op.execute(
        "ALTER TABLE thesis_evaluation_episode_check_receipts ADD COLUMN context_sha256 TEXT CHECK(context_sha256 IS NULL OR (length(context_sha256)=64 AND context_sha256 NOT GLOB '*[^0-9a-f]*'))"
    )
    op.execute("""
        CREATE TRIGGER trg_thesis_check_context_shape BEFORE INSERT ON thesis_evaluation_episode_check_receipts
        WHEN (NEW.context_json IS NULL) != (NEW.context_sha256 IS NULL)
          OR ((SELECT evaluator_semantic_version FROM thesis_evaluation_episodes WHERE episode_id=NEW.episode_id)='thesis-evaluator/v2' AND NEW.context_json IS NULL)
        BEGIN SELECT RAISE(ABORT, 'thesis check retained context required'); END
    """)


def downgrade() -> None:
    op.execute("DROP TRIGGER trg_thesis_check_context_shape")
    op.execute("ALTER TABLE thesis_evaluation_episode_check_receipts DROP COLUMN context_sha256")
    op.execute("ALTER TABLE thesis_evaluation_episode_check_receipts DROP COLUMN context_json")
