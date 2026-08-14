"""Add thesis-episode acknowledgement and anti-nag delivery state.

Revision ID: 0015_add_thesis_episode_attention
Revises: 0014_add_thesis_evaluation_episodes
"""

from __future__ import annotations

from alembic import op

revision = "0015_add_thesis_episode_attention"
down_revision = "0014_add_thesis_evaluation_episodes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE thesis_evaluation_episodes ADD COLUMN attention_state TEXT "
        "NOT NULL DEFAULT 'unreviewed' CHECK(attention_state IN "
        "('unreviewed','acknowledged','acted_on','superseded'))"
    )
    op.execute("ALTER TABLE thesis_evaluation_episodes ADD COLUMN acknowledged_at TEXT")
    op.execute("ALTER TABLE thesis_evaluation_episodes ADD COLUMN acknowledgement_note TEXT")
    op.execute("ALTER TABLE thesis_evaluation_episodes ADD COLUMN next_review_at TEXT")
    op.execute(
        "ALTER TABLE thesis_evaluation_episodes ADD COLUMN acted_on_decision_id INTEGER "
        "REFERENCES decisions(id)"
    )
    op.execute(
        "ALTER TABLE thesis_evaluation_episodes ADD COLUMN superseded_by_episode_id TEXT "
        "REFERENCES thesis_evaluation_episodes(episode_id)"
    )
    op.execute("ALTER TABLE thesis_evaluation_episodes ADD COLUMN attention_updated_at TEXT")

    op.execute(
        "ALTER TABLE alerts ADD COLUMN thesis_evaluation_episode_id TEXT "
        "REFERENCES thesis_evaluation_episodes(episode_id)"
    )
    op.execute("ALTER TABLE alerts ADD COLUMN review_cycle_id TEXT")
    op.execute(
        "ALTER TABLE coach_pings ADD COLUMN thesis_evaluation_episode_id TEXT "
        "REFERENCES thesis_evaluation_episodes(episode_id)"
    )
    op.execute("ALTER TABLE coach_pings ADD COLUMN review_cycle_id TEXT")

    op.execute(
        """
        CREATE TABLE thesis_evaluation_episode_delivery_receipts (
            receipt_id TEXT PRIMARY KEY,
            episode_id TEXT NOT NULL
                REFERENCES thesis_evaluation_episodes(episode_id),
            review_cycle_id TEXT NOT NULL,
            channel TEXT NOT NULL,
            surface TEXT NOT NULL,
            status TEXT NOT NULL
                CHECK(status IN ('reserved','delivered','failed')),
            reserved_at TEXT NOT NULL,
            reservation_expires_at TEXT NOT NULL,
            attempt_token TEXT NOT NULL,
            attempt_count INTEGER NOT NULL,
            delivered_at TEXT,
            failed_at TEXT,
            external_ref TEXT,
            failure_reason TEXT,
            UNIQUE(episode_id,review_cycle_id,channel,surface),
            CHECK(length(attempt_token)=64 AND attempt_token NOT GLOB '*[^0-9a-f]*'),
            CHECK(attempt_count >= 1),
            CHECK(
                (status='reserved' AND delivered_at IS NULL AND failed_at IS NULL)
                OR (status='delivered' AND delivered_at IS NOT NULL AND failed_at IS NULL)
                OR (status='failed' AND delivered_at IS NULL AND failed_at IS NOT NULL)
            )
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_thesis_episode_delivery_status ON "
        "thesis_evaluation_episode_delivery_receipts(status,reservation_expires_at)"
    )
    op.execute(
        "CREATE INDEX ix_alerts_thesis_episode ON "
        "alerts(thesis_evaluation_episode_id,review_cycle_id)"
    )
    op.execute(
        "CREATE INDEX ix_coach_pings_thesis_episode ON "
        "coach_pings(thesis_evaluation_episode_id,review_cycle_id)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_coach_pings_thesis_episode")
    op.execute("DROP INDEX IF EXISTS ix_alerts_thesis_episode")
    op.execute("DROP INDEX IF EXISTS ix_thesis_episode_delivery_status")
    op.execute("DROP TABLE IF EXISTS thesis_evaluation_episode_delivery_receipts")
    op.execute("ALTER TABLE coach_pings DROP COLUMN review_cycle_id")
    op.execute("ALTER TABLE coach_pings DROP COLUMN thesis_evaluation_episode_id")
    op.execute("ALTER TABLE alerts DROP COLUMN review_cycle_id")
    op.execute("ALTER TABLE alerts DROP COLUMN thesis_evaluation_episode_id")
    op.execute("ALTER TABLE thesis_evaluation_episodes DROP COLUMN attention_updated_at")
    op.execute("ALTER TABLE thesis_evaluation_episodes DROP COLUMN superseded_by_episode_id")
    op.execute("ALTER TABLE thesis_evaluation_episodes DROP COLUMN acted_on_decision_id")
    op.execute("ALTER TABLE thesis_evaluation_episodes DROP COLUMN next_review_at")
    op.execute("ALTER TABLE thesis_evaluation_episodes DROP COLUMN acknowledgement_note")
    op.execute("ALTER TABLE thesis_evaluation_episodes DROP COLUMN acknowledged_at")
    op.execute("ALTER TABLE thesis_evaluation_episodes DROP COLUMN attention_state")
