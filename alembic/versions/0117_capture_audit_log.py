"""capture_audit_log — The Ledger's durable summary-level audit trail.

Raw capture transcripts are TRANSIENT (0115: blanked once the structured musing
exists). What survives — for evals, observability, and "what did the system do
with what I said?" — is this SUMMARY-level log: one row per meaningful action the
pipeline took (classified / structured / landed / researched / synthesized /
failed), recording a SUMMARY of the utterance (never the raw words), the action,
the LLM purpose/model/cost, and a content HASH of the prompt (never the prompt
body — the log must not become a second uncontrolled copy of sensitive thought).

  id                INTEGER PRIMARY KEY
  session_id        INTEGER — raw_capture_sessions.id (PLAIN int, code-level RI,
                      nullable: a research/synthesis action has no capture session)
  note_id           INTEGER — analyst_notes.id produced/acted on (nullable)
  channel           TEXT NOT NULL — telegram | gmail | tray | system
  utterance_summary TEXT NOT NULL — a SHORT paraphrase of the input, NOT the raw
                      utterance (privacy: the log paraphrases the signal, not the
                      thought)
  action            TEXT NOT NULL — captured | structured | landed | matched |
                      researched | synthesized | failed | purged
  detail            TEXT — short human detail of what the LLM chose (nullable)
  purpose           TEXT — the governed LLM purpose invoked, if any (nullable)
  model             TEXT — the model used (nullable)
  prompt_sha256     TEXT — hex sha256 of the prompt; NEVER the prompt body
  run_id            TEXT — correlation id tying a multi-step pipeline run together
  cost_usd          REAL NOT NULL DEFAULT 0
  created_at        TEXT NOT NULL — naive-UTC

Indices
  ix_capture_audit_created  (created_at) — windowed cost/observability reads.
  ix_capture_audit_note     (note_id) WHERE note_id IS NOT NULL — the per-note
      "what happened to this musing" trail.

Revision ID: 0117_capture_audit_log
Revises: 0116_raw_capture_sessions
Create Date: 2026-06-28
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0117_capture_audit_log"
down_revision: str | Sequence[str] | None = "0116_raw_capture_sessions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "capture_audit_log" in inspector.get_table_names():
        return  # idempotent

    op.create_table(
        "capture_audit_log",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("session_id", sa.Integer(), nullable=True),
        sa.Column("note_id", sa.Integer(), nullable=True),
        sa.Column("channel", sa.Text(), nullable=False),
        sa.Column("utterance_summary", sa.Text(), nullable=False),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column("purpose", sa.Text(), nullable=True),
        sa.Column("model", sa.Text(), nullable=True),
        sa.Column("prompt_sha256", sa.Text(), nullable=True),
        sa.Column("run_id", sa.Text(), nullable=True),
        sa.Column("cost_usd", sa.Float(), nullable=False, server_default=sa.text("0")),
        sa.Column("created_at", sa.Text(), nullable=False),
    )
    op.create_index("ix_capture_audit_created", "capture_audit_log", ["created_at"])
    op.create_index(
        "ix_capture_audit_note",
        "capture_audit_log",
        ["note_id"],
        sqlite_where=sa.text("note_id IS NOT NULL"),
    )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "capture_audit_log" not in inspector.get_table_names():
        return
    existing = {idx["name"] for idx in inspector.get_indexes("capture_audit_log")}
    for name in ("ix_capture_audit_created", "ix_capture_audit_note"):
        if name in existing:
            op.drop_index(name, table_name="capture_audit_log")
    op.drop_table("capture_audit_log")
