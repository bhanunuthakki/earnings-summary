"""Move historical earnings-prep prompts into lifecycle-aware analyst notes.

Revision ID: 0026_consolidate_earnings_prep_notes
Revises: 0025_add_sizing_intent_withdrawals
"""

from __future__ import annotations

from alembic import op

revision = "0026_consolidate_earnings_prep_notes"
down_revision = "0025_add_sizing_intent_withdrawals"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        INSERT OR IGNORE INTO analyst_notes(
            user_id,ticker,kind,status,body,anchor_type,anchor_key,
            source,source_ref,context_json,created_at,updated_at
        )
        SELECT
            entry.user_id,
            entry.ticker,
            'question',
            'open',
            entry.body,
            'ticker',
            entry.ticker,
            CASE WHEN entry.source_alert_id IS NULL THEN 'manual' ELSE 'alert' END,
            'legacy_thesis_ledger_entry:' || entry.id || char(58) || 'earnings_prep',
            json_object(
                'legacy_ledger_entry_id', entry.id,
                'source_alert_id', entry.source_alert_id,
                'purpose', 'earnings_call_open_question'
            ),
            entry.created_at,
            entry.created_at
        FROM thesis_ledger_entries AS entry
        WHERE entry.entry_kind='earnings_prep_append'
        """
    )


def downgrade() -> None:
    # Deliberately retain migrated notes: an owner may have resolved, archived,
    # or superseded them after upgrade. Deleting them would lose lifecycle work.
    pass
