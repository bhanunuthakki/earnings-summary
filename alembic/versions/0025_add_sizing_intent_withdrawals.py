"""Add append-only sizing-intent withdrawals and retire the rejected NU draft.

Revision ID: 0025_add_sizing_intent_withdrawals
Revises: 0024_add_operations_attention_findings
"""

from __future__ import annotations

from alembic import op

revision = "0025_add_sizing_intent_withdrawals"
down_revision = "0024_add_operations_attention_findings"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE position_sizing_intent_withdrawals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL REFERENCES tenants(id),
            sizing_intent_id INTEGER NOT NULL REFERENCES position_sizing_intent(id),
            reason TEXT NOT NULL CHECK(length(trim(reason)) > 0),
            created_at TEXT NOT NULL CHECK(datetime(created_at) IS NOT NULL),
            UNIQUE(user_id, sizing_intent_id)
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_sizing_intent_withdrawals_user_created ON "
        "position_sizing_intent_withdrawals(user_id,created_at DESC)"
    )
    op.execute(
        "CREATE TRIGGER trg_sizing_intent_withdrawals_no_update BEFORE UPDATE ON "
        "position_sizing_intent_withdrawals BEGIN "
        "SELECT RAISE(ABORT, 'sizing intent withdrawals are append-only'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_sizing_intent_withdrawals_no_delete BEFORE DELETE ON "
        "position_sizing_intent_withdrawals BEGIN "
        "SELECT RAISE(ABORT, 'sizing intent withdrawals are append-only'); END"
    )
    op.execute(
        """
        INSERT OR IGNORE INTO position_sizing_intent_withdrawals(
            user_id,sizing_intent_id,reason,created_at
        )
        SELECT
            intent.user_id,
            intent.id,
            'Owner rejected this machine-generated NU sizing intent on 2026-08-25.',
            '2026-08-25T00:00:00'
        FROM position_sizing_intent AS intent
        WHERE intent.user_id='bhanu'
          AND intent.ticker='NU'
          AND intent.intent_kind='add_rung'
          AND abs(intent.intent_value - 10.32) < 0.000001
          AND intent.narrative LIKE
              '[draft, pending owner review] Add-rung (Monthly Red Team PR9,%'
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_sizing_intent_withdrawals_no_delete")
    op.execute("DROP TRIGGER IF EXISTS trg_sizing_intent_withdrawals_no_update")
    op.execute("DROP INDEX IF EXISTS ix_sizing_intent_withdrawals_user_created")
    op.execute("DROP TABLE IF EXISTS position_sizing_intent_withdrawals")
