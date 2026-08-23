"""Add immutable, idempotent audit receipts for governed alert actions.

Revision ID: 0022_add_governed_alert_action_receipts
Revises: 0021_managed_ir_publications
"""

from __future__ import annotations

from alembic import op

revision = "0022_add_governed_alert_action_receipts"
down_revision = "0021_managed_ir_publications"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE governed_alert_action_receipts (
            receipt_id TEXT PRIMARY KEY NOT NULL,
            idempotency_key TEXT NOT NULL UNIQUE,
            request_sha256 TEXT NOT NULL,
            actor TEXT NOT NULL,
            alert_id INTEGER NOT NULL REFERENCES alerts(id),
            source_ref TEXT NOT NULL,
            evidence_ref TEXT NOT NULL,
            action_type TEXT NOT NULL CHECK(action_type IN
                ('review','acknowledge','dismiss','defer','complete','supersede')),
            occurred_at TEXT NOT NULL,
            note_sha256 TEXT,
            dismiss_reason_sha256 TEXT,
            defer_until TEXT,
            decision_id INTEGER REFERENCES decisions(id),
            replacement_episode_id TEXT REFERENCES thesis_evaluation_episodes(episode_id),
            result_state TEXT NOT NULL CHECK(result_state IN
                ('reviewed','acknowledged','dismissed','deferred','completed','superseded')),
            CHECK(length(receipt_id)=86 AND substr(receipt_id,1,22)='governed-alert-action:'),
            CHECK(length(request_sha256)=64 AND request_sha256 NOT GLOB '*[^0-9a-f]*'),
            CHECK(length(evidence_ref)=64 AND evidence_ref NOT GLOB '*[^0-9a-f]*'),
            CHECK(note_sha256 IS NULL OR
                (length(note_sha256)=64 AND note_sha256 NOT GLOB '*[^0-9a-f]*')),
            CHECK(dismiss_reason_sha256 IS NULL OR
                (length(dismiss_reason_sha256)=64 AND dismiss_reason_sha256 NOT GLOB '*[^0-9a-f]*')),
            CHECK(source_ref='alert:' || alert_id),
            CHECK(length(occurred_at) BETWEEN 20 AND 40 AND datetime(occurred_at) IS NOT NULL),
            CHECK((action_type='review' AND result_state='reviewed') OR
                (action_type='acknowledge' AND result_state='acknowledged') OR
                (action_type='dismiss' AND result_state='dismissed') OR
                (action_type='defer' AND result_state='deferred') OR
                (action_type='complete' AND result_state='completed') OR
                (action_type='supersede' AND result_state='superseded')),
            CHECK((action_type='dismiss') = (dismiss_reason_sha256 IS NOT NULL)),
            CHECK((action_type='defer') = (defer_until IS NOT NULL)),
            CHECK(defer_until IS NULL OR
                (datetime(defer_until) IS NOT NULL AND datetime(defer_until) > datetime(occurred_at))),
            CHECK((action_type='complete') = (decision_id IS NOT NULL)),
            CHECK((action_type='supersede') = (replacement_episode_id IS NOT NULL)),
            CHECK(note_sha256 IS NULL OR action_type IN ('acknowledge','defer'))
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_governed_alert_action_receipts_alert_occurred "
        "ON governed_alert_action_receipts(alert_id,occurred_at DESC)"
    )
    op.execute(
        "CREATE TRIGGER trg_governed_alert_action_receipts_no_update "
        "BEFORE UPDATE ON governed_alert_action_receipts BEGIN "
        "SELECT RAISE(ABORT, 'governed alert action receipts append-only'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_governed_alert_action_receipts_no_delete "
        "BEFORE DELETE ON governed_alert_action_receipts BEGIN "
        "SELECT RAISE(ABORT, 'governed alert action receipts append-only'); END"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_governed_alert_action_receipts_no_delete")
    op.execute("DROP TRIGGER IF EXISTS trg_governed_alert_action_receipts_no_update")
    op.execute("DROP INDEX IF EXISTS ix_governed_alert_action_receipts_alert_occurred")
    op.execute("DROP TABLE IF EXISTS governed_alert_action_receipts")
