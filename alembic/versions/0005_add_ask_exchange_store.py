"""Add durable Ask session context and exchange orchestration.

Revision ID: 0005_add_ask_exchange_store
Revises: 0004_add_llm_circuit_breakers
Create Date: 2026-08-09
"""

from __future__ import annotations

from alembic import op

revision = "0005_add_ask_exchange_store"
down_revision = "0004_add_llm_circuit_breakers"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS ask_session_contexts (
            session_id TEXT PRIMARY KEY,
            schema_version TEXT NOT NULL,
            context_json TEXT NOT NULL,
            context_sha256 TEXT NOT NULL,
            revision INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(session_id) REFERENCES ask_sessions(id) ON DELETE CASCADE,
            CHECK(schema_version = 'session_context.v1'),
            CHECK(json_valid(context_json) AND json_type(context_json) = 'object'),
            CHECK(
                length(context_sha256) = 64
                AND lower(context_sha256) = context_sha256
                AND context_sha256 NOT GLOB '*[^0-9a-f]*'
            ),
            CHECK(revision >= 0)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS ask_exchanges (
            request_id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            payload_sha256 TEXT NOT NULL,
            status TEXT NOT NULL,
            user_turn_id INTEGER NOT NULL UNIQUE,
            assistant_turn_id INTEGER UNIQUE,
            error_code TEXT,
            session_revision INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            completed_at TEXT,
            failed_at TEXT,
            FOREIGN KEY(session_id) REFERENCES ask_sessions(id) ON DELETE CASCADE,
            FOREIGN KEY(user_turn_id) REFERENCES ask_turns(id) ON DELETE CASCADE,
            FOREIGN KEY(assistant_turn_id) REFERENCES ask_turns(id) ON DELETE CASCADE,
            CHECK(length(request_id) BETWEEN 1 AND 128),
            CHECK(
                length(payload_sha256) = 64
                AND lower(payload_sha256) = payload_sha256
                AND payload_sha256 NOT GLOB '*[^0-9a-f]*'
            ),
            CHECK(status IN ('pending', 'completed', 'failed')),
            CHECK(session_revision > 0),
            CHECK(error_code IS NULL OR length(error_code) BETWEEN 1 AND 128),
            CHECK(
                (status = 'pending' AND assistant_turn_id IS NULL
                    AND error_code IS NULL AND completed_at IS NULL AND failed_at IS NULL)
                OR
                (status = 'completed' AND assistant_turn_id IS NOT NULL
                    AND error_code IS NULL AND completed_at IS NOT NULL AND failed_at IS NULL)
                OR
                (status = 'failed' AND assistant_turn_id IS NULL
                    AND error_code IS NOT NULL AND completed_at IS NULL AND failed_at IS NOT NULL)
            )
        )
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS ask_exchange_artifacts (
            request_id TEXT PRIMARY KEY,
            schema_version TEXT NOT NULL,
            artifacts_json TEXT NOT NULL,
            artifacts_sha256 TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(request_id) REFERENCES ask_exchanges(request_id) ON DELETE CASCADE,
            CHECK(schema_version = 'exchange_artifacts.v1'),
            CHECK(json_valid(artifacts_json) AND json_type(artifacts_json) = 'object'),
            CHECK(
                length(artifacts_sha256) = 64
                AND lower(artifacts_sha256) = artifacts_sha256
                AND artifacts_sha256 NOT GLOB '*[^0-9a-f]*'
            )
        )
        """
    )
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_ask_exchanges_one_pending_per_session "
        "ON ask_exchanges(session_id) WHERE status = 'pending'"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_ask_exchanges_session_created "
        "ON ask_exchanges(session_id, created_at)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_ask_exchanges_status_updated "
        "ON ask_exchanges(status, updated_at)"
    )
    op.execute(
        """
        CREATE TRIGGER IF NOT EXISTS trg_ask_exchange_user_turn_exact
        BEFORE INSERT ON ask_exchanges
        WHEN NOT EXISTS (
            SELECT 1 FROM ask_turns turn
            WHERE turn.id = NEW.user_turn_id
              AND turn.session_id = NEW.session_id
              AND turn.role = 'user'
        )
        BEGIN
            SELECT RAISE(ABORT, 'Ask exchange user turn identity mismatch');
        END
        """
    )
    op.execute(
        """
        CREATE TRIGGER IF NOT EXISTS trg_ask_exchange_assistant_turn_exact
        BEFORE UPDATE OF assistant_turn_id ON ask_exchanges
        WHEN NEW.assistant_turn_id IS NOT NULL AND NOT EXISTS (
            SELECT 1 FROM ask_turns turn
            WHERE turn.id = NEW.assistant_turn_id
              AND turn.session_id = NEW.session_id
              AND turn.role = 'assistant'
        )
        BEGIN
            SELECT RAISE(ABORT, 'Ask exchange assistant turn identity mismatch');
        END
        """
    )
    op.execute(
        """
        CREATE TRIGGER IF NOT EXISTS trg_ask_exchange_complete_has_artifacts
        BEFORE UPDATE OF status ON ask_exchanges
        WHEN NEW.status = 'completed' AND NOT EXISTS (
            SELECT 1 FROM ask_exchange_artifacts artifact
            WHERE artifact.request_id = NEW.request_id
        )
        BEGIN
            SELECT RAISE(ABORT, 'completed Ask exchange requires artifacts');
        END
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_ask_exchange_complete_has_artifacts")
    op.execute("DROP TRIGGER IF EXISTS trg_ask_exchange_assistant_turn_exact")
    op.execute("DROP TRIGGER IF EXISTS trg_ask_exchange_user_turn_exact")
    op.execute("DROP INDEX IF EXISTS ix_ask_exchanges_status_updated")
    op.execute("DROP INDEX IF EXISTS ix_ask_exchanges_session_created")
    op.execute("DROP INDEX IF EXISTS uq_ask_exchanges_one_pending_per_session")
    op.execute("DROP TABLE IF EXISTS ask_exchange_artifacts")
    op.execute("DROP TABLE IF EXISTS ask_exchanges")
    op.execute("DROP TABLE IF EXISTS ask_session_contexts")
