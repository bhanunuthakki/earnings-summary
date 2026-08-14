"""Add append-only operational Ask grounding traces.

Revision ID: 0016_add_ask_grounding_traces
Revises: 0015_add_thesis_episode_attention
Create Date: 2026-08-14
"""

from __future__ import annotations

from alembic import op

revision = "0016_add_ask_grounding_traces"
down_revision = "0015_add_thesis_episode_attention"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE ask_grounding_traces (
            trace_id TEXT PRIMARY KEY,
            idempotency_key TEXT NOT NULL UNIQUE,
            session_id TEXT,
            route TEXT NOT NULL,
            question_sha256 TEXT NOT NULL,
            scope_json TEXT NOT NULL,
            strategy TEXT NOT NULL,
            outcome TEXT NOT NULL,
            item_count INTEGER NOT NULL,
            item_set_json TEXT NOT NULL,
            item_set_sha256 TEXT NOT NULL,
            created_at TEXT NOT NULL,
            CONSTRAINT ck_ask_grounding_trace_route
              CHECK(route IN ('data','narrative')),
            CONSTRAINT ck_ask_grounding_trace_strategy
              CHECK(strategy IN ('sql_viewspec','sql_facts_and_lexical_documents')),
            CONSTRAINT ck_ask_grounding_trace_outcome
              CHECK(outcome IN ('ready','no_evidence','retrieval_error')),
            CONSTRAINT ck_ask_grounding_trace_count CHECK(item_count >= 0),
            CONSTRAINT ck_ask_grounding_trace_json
              CHECK(json_valid(scope_json) AND json_type(scope_json)='array'
                AND json_valid(item_set_json) AND json_type(item_set_json)='array'
                AND json_array_length(item_set_json)=item_count),
            CONSTRAINT ck_ask_grounding_trace_hashes
              CHECK(length(question_sha256)=64
                AND question_sha256 NOT GLOB '*[^0-9a-f]*'
                AND length(item_set_sha256)=64
                AND item_set_sha256 NOT GLOB '*[^0-9a-f]*')
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_ask_grounding_traces_session_created "
        "ON ask_grounding_traces(session_id,created_at)"
    )
    op.execute(
        "ALTER TABLE ask_turns ADD COLUMN grounding_trace_id TEXT "
        "REFERENCES ask_grounding_traces(trace_id) ON DELETE RESTRICT"
    )
    op.execute("CREATE INDEX ix_ask_turns_grounding_trace_id ON ask_turns(grounding_trace_id)")
    op.execute(
        """
        CREATE TRIGGER trg_ask_grounding_traces_append_only_update
        BEFORE UPDATE ON ask_grounding_traces
        BEGIN SELECT RAISE(ABORT, 'Ask grounding traces are append-only'); END
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_ask_grounding_traces_append_only_delete
        BEFORE DELETE ON ask_grounding_traces
        BEGIN SELECT RAISE(ABORT, 'Ask grounding traces are append-only'); END
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER trg_ask_grounding_traces_append_only_delete")
    op.execute("DROP TRIGGER trg_ask_grounding_traces_append_only_update")
    op.execute("DROP INDEX ix_ask_turns_grounding_trace_id")
    op.execute("ALTER TABLE ask_turns DROP COLUMN grounding_trace_id")
    op.execute("DROP INDEX ix_ask_grounding_traces_session_created")
    op.execute("DROP TABLE ask_grounding_traces")
