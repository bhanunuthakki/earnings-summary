"""raw_capture_sessions — The Ledger's TRANSIENT capture staging.

The Ledger captures a stream-of-consciousness musing (Telegram / Gmail / the
desktop tray, voice or text) and lands it as an ``analyst_notes`` row with
``kind='musing'``, ``source='capture'``. This table is the *staging* layer the
ingest pipeline writes to BEFORE the structured musing exists — the one place
the raw words live durably so a crash after capture never loses them.

It is deliberately EPHEMERAL, the opposite posture from the append-only notes
spine: ``transcript`` is the raw words at stage 1 and is BLANKED once the
structured musing is extracted (status -> 'landed' -> 'purged'); the durable
record is the ``analyst_notes`` row plus the summary-level ``capture_audit_log``
(0116). ``purge_after`` carries the retention TTL the purge sweep honours.

  id             INTEGER PRIMARY KEY
  channel        TEXT NOT NULL  — telegram | gmail | tray (the ingest adapter)
  media_kind     TEXT NOT NULL DEFAULT 'text' — text | voice
  transcript     TEXT NOT NULL DEFAULT '' — raw words; DURABLE at capture,
                   blanked once landed (privacy: raw is transient)
  status         TEXT NOT NULL DEFAULT 'captured'
                   — captured | structured | landed | purged | failed
  note_id        INTEGER — the analyst_notes row produced. PLAIN integer, NO
                   REFERENCES (open_conn runs PRAGMA foreign_keys=ON; an FK to a
                   table absent in a partial fixture schema poisons inserts —
                   same posture as 0086/0093/0095). Code-level RI only.
  external_ref   TEXT — the source's own id (telegram update_id / gmail message
                   id). The dedup key so re-polling the same message no-ops.
  total_llm_cost REAL NOT NULL DEFAULT 0 — rolled-up cost of this session's LLM
                   hops (transcription is local/free; structuring is metered).
  created_at     TEXT NOT NULL — naive-UTC write stamp
  updated_at     TEXT NOT NULL
  purge_after    TEXT — nullable retention TTL ('YYYY-MM-DD HH:MM:SS')

Indices
  ux_raw_capture_external  UNIQUE (channel, external_ref) WHERE external_ref IS
      NOT NULL — one session per (channel, source-message-id): makes the poller
      re-run-safe (re-ingesting the same Telegram update / Gmail message no-ops).
  ix_raw_capture_status    (status) — the purge sweep + the retry scan.

No CHECK constraints: channel / media_kind / status are validated in the write
path (src/capture/), matching the reversible-code posture of NOTE_KINDS /
NOTE_SOURCES (plain tuples, no DB CHECK) — extending the vocab stays a code edit.

Revision ID: 0116_raw_capture_sessions
Revises: 0115_analyst_notes_capture_vocab
Create Date: 2026-06-28
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0116_raw_capture_sessions"
down_revision: str | Sequence[str] | None = "0115_analyst_notes_capture_vocab"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "raw_capture_sessions" in inspector.get_table_names():
        return  # idempotent

    op.create_table(
        "raw_capture_sessions",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("channel", sa.Text(), nullable=False),
        sa.Column("media_kind", sa.Text(), nullable=False, server_default=sa.text("'text'")),
        sa.Column("transcript", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'captured'")),
        sa.Column("note_id", sa.Integer(), nullable=True),
        sa.Column("external_ref", sa.Text(), nullable=True),
        sa.Column("total_llm_cost", sa.Float(), nullable=False, server_default=sa.text("0")),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.Text(), nullable=False),
        sa.Column("purge_after", sa.Text(), nullable=True),
    )
    # Dedup: one session per (channel, source-message-id) — re-polling no-ops.
    op.create_index(
        "ux_raw_capture_external",
        "raw_capture_sessions",
        ["channel", "external_ref"],
        unique=True,
        sqlite_where=sa.text("external_ref IS NOT NULL"),
    )
    # The purge sweep + retry scan read by status.
    op.create_index("ix_raw_capture_status", "raw_capture_sessions", ["status"])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "raw_capture_sessions" not in inspector.get_table_names():
        return
    existing = {idx["name"] for idx in inspector.get_indexes("raw_capture_sessions")}
    for name in ("ux_raw_capture_external", "ix_raw_capture_status"):
        if name in existing:
            op.drop_index(name, table_name="raw_capture_sessions")
    op.drop_table("raw_capture_sessions")
