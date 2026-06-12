"""ask_sessions + ask_turns — server-side portfolio Ask thread persistence.

Why (S3, directives/fund_grade_build_2026_06.md):
  The portfolio Ask dock previously discarded every thread on the server;
  the client re-supplied an 8-turn tail each request.  That has two failure
  modes: (a) the client can lie / truncate / corrupt the history, meaning
  the model sees a different conversation than what actually happened; (b) the
  context silently vanishes on every page reload, making the dock useless for
  multi-day advisory research.

  These two tables give every portfolio Ask thread a durable, authoritative
  home:

  ``ask_sessions`` — one row per distinct thread.  ``scope`` is always
  ``'portfolio'`` for now (ticker threads keep their existing
  ``data/report_chats/<T>/<date>.json`` storage and are NOT migrated here).
  ``title`` is initially the first 60 chars of the first user question, then
  rename-able by the user.

  ``ask_turns`` — one row per user or assistant message inside a session.
  ``citations_json`` is a JSON-array blob (the chip payload list from the
  grounding module) or NULL.  ``model`` is the LLM model id for assistant
  turns (NULL for user turns).  The full thread is stored so future turns
  can be loaded server-side, eliminating the client-supplied-history trust
  gap.

Naive-UTC convention:  both tables use ISO 8601 TEXT timestamps without a
timezone suffix, matching the project-wide naive-UTC pattern (see
``src/alerts/store.py:_now_iso``).

Revision ID: 0085_ask_sessions_and_turns
Revises: 0084_model_eval_verdicts_and_pin_overrides
Create Date: 2026-06-11
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0085_ask_sessions_and_turns"
down_revision: str | Sequence[str] | None = "0084_model_eval_verdicts_and_pin_overrides"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing = set(inspector.get_table_names())

    if "ask_sessions" not in existing:
        op.create_table(
            "ask_sessions",
            # UUID4 hex string, e.g. "a1b2c3d4e5f6…".
            sa.Column("id", sa.Text(), primary_key=True),
            # 'portfolio' — ticker threads keep the json-file store for now.
            sa.Column("scope", sa.Text(), nullable=False, server_default=sa.text("'portfolio'")),
            # Auto-set to the first 60 chars of the first user question;
            # rename-able via PATCH /api/ask/sessions/<id>.
            sa.Column("title", sa.Text(), nullable=False, server_default=sa.text("''")),
            # Naive-UTC ISO strings.
            sa.Column("created_at", sa.Text(), nullable=False),
            sa.Column("updated_at", sa.Text(), nullable=False),
        )
        op.create_index(
            "ix_ask_sessions_scope_updated",
            "ask_sessions",
            ["scope", sa.text("updated_at DESC")],
        )

    if "ask_turns" not in existing:
        op.create_table(
            "ask_turns",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column(
                "session_id",
                sa.Text(),
                sa.ForeignKey("ask_sessions.id", ondelete="CASCADE"),
                nullable=False,
            ),
            # 'user' | 'assistant'
            sa.Column("role", sa.Text(), nullable=False),
            sa.Column("text", sa.Text(), nullable=False),
            # JSON array of chip payloads (same shape as the grounding module's
            # item.chip_payload() list) or NULL when no citations were returned.
            sa.Column("citations_json", sa.Text(), nullable=True),
            # The LLM model id for assistant turns; NULL for user turns.
            sa.Column("model", sa.Text(), nullable=True),
            # Naive-UTC ISO string.
            sa.Column("created_at", sa.Text(), nullable=False),
        )
        op.create_index("ix_ask_turns_session_id", "ask_turns", ["session_id"])
        op.create_index(
            "ix_ask_turns_session_created",
            "ask_turns",
            ["session_id", sa.text("created_at ASC")],
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing = set(inspector.get_table_names())

    if "ask_turns" in existing:
        for idx in ("ix_ask_turns_session_created", "ix_ask_turns_session_id"):
            if any(i["name"] == idx for i in inspector.get_indexes("ask_turns")):
                op.drop_index(idx, table_name="ask_turns")
        op.drop_table("ask_turns")

    if "ask_sessions" in existing:
        idx = "ix_ask_sessions_scope_updated"
        if any(i["name"] == idx for i in inspector.get_indexes("ask_sessions")):
            op.drop_index(idx, table_name="ask_sessions")
        op.drop_table("ask_sessions")
