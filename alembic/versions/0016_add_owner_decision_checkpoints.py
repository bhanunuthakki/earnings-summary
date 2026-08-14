"""Add atomic, idempotent owner-decision checkpoint envelopes.

Revision ID: 0016_add_owner_decision_checkpoints
Revises: 0015_add_thesis_episode_attention
"""

from __future__ import annotations

from alembic import op

revision = "0016_add_owner_decision_checkpoints"
down_revision = "0015_add_thesis_episode_attention"
branch_labels = None
depends_on = None


_TABLES = (
    "owner_decision_checkpoint_ledger_entries",
    "owner_decision_checkpoint_sizing_intents",
    "owner_decision_checkpoint_decisions",
    "owner_decision_checkpoints",
)


def _install_append_only_triggers(table: str) -> None:
    op.execute(
        f"CREATE TRIGGER trg_{table}_no_update BEFORE UPDATE ON {table} "
        "BEGIN SELECT RAISE(ABORT, 'owner decision checkpoint history is append-only'); END"
    )
    op.execute(
        f"CREATE TRIGGER trg_{table}_no_delete BEFORE DELETE ON {table} "
        "BEGIN SELECT RAISE(ABORT, 'owner decision checkpoint history is append-only'); END"
    )


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE owner_decision_checkpoints (
            id INTEGER PRIMARY KEY,
            user_id TEXT NOT NULL REFERENCES tenants(id),
            source_channel TEXT NOT NULL,
            source_event_id TEXT NOT NULL,
            checkpoint_schema_version TEXT NOT NULL,
            payload_sha256 TEXT NOT NULL,
            payload_json TEXT NOT NULL CHECK(json_valid(payload_json)),
            retrospective INTEGER NOT NULL DEFAULT 0
                CHECK(retrospective IN (0,1)),
            created_at TEXT NOT NULL,
            confirmed_at TEXT NOT NULL,
            UNIQUE(user_id,source_channel,source_event_id,checkpoint_schema_version),
            CHECK(length(trim(source_channel)) > 0),
            CHECK(length(trim(source_event_id)) > 0),
            CHECK(length(payload_sha256)=64 AND payload_sha256 NOT GLOB '*[^0-9a-f]*')
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_owner_decision_checkpoints_event ON "
        "owner_decision_checkpoints(source_channel,source_event_id)"
    )
    op.execute(
        """
        CREATE TABLE owner_decision_checkpoint_decisions (
            checkpoint_id INTEGER NOT NULL
                REFERENCES owner_decision_checkpoints(id),
            leg_id TEXT NOT NULL,
            leg_ordinal INTEGER NOT NULL CHECK(leg_ordinal >= 0),
            decision_id INTEGER NOT NULL REFERENCES decisions(id),
            recorded_at TEXT NOT NULL,
            PRIMARY KEY(checkpoint_id,leg_id),
            UNIQUE(checkpoint_id,leg_ordinal),
            UNIQUE(decision_id),
            CHECK(length(trim(leg_id)) > 0)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE owner_decision_checkpoint_sizing_intents (
            checkpoint_id INTEGER NOT NULL
                REFERENCES owner_decision_checkpoints(id),
            leg_id TEXT NOT NULL,
            leg_ordinal INTEGER NOT NULL CHECK(leg_ordinal >= 0),
            sizing_intent_id INTEGER NOT NULL REFERENCES position_sizing_intent(id),
            recorded_at TEXT NOT NULL,
            PRIMARY KEY(checkpoint_id,leg_id),
            UNIQUE(checkpoint_id,leg_ordinal),
            UNIQUE(sizing_intent_id),
            CHECK(length(trim(leg_id)) > 0)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE owner_decision_checkpoint_ledger_entries (
            checkpoint_id INTEGER NOT NULL
                REFERENCES owner_decision_checkpoints(id),
            ledger_entry_id INTEGER NOT NULL REFERENCES thesis_ledger_entries(id),
            recorded_at TEXT NOT NULL,
            PRIMARY KEY(checkpoint_id,ledger_entry_id),
            UNIQUE(ledger_entry_id)
        )
        """
    )
    for table in _TABLES:
        _install_append_only_triggers(table)


def downgrade() -> None:
    for table in _TABLES:
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_no_delete")
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_no_update")
    op.execute("DROP TABLE IF EXISTS owner_decision_checkpoint_ledger_entries")
    op.execute("DROP TABLE IF EXISTS owner_decision_checkpoint_sizing_intents")
    op.execute("DROP TABLE IF EXISTS owner_decision_checkpoint_decisions")
    op.execute("DROP INDEX IF EXISTS ix_owner_decision_checkpoints_event")
    op.execute("DROP TABLE IF EXISTS owner_decision_checkpoints")
